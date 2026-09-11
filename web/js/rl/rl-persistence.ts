/* ============================================================
 * RL 持久化层 — 基于 IndexedDB 的永久存储
 *
 * 功能：
 * - 存储 RL 智能体网络权重（支持智能压缩）
 * - 存储训练数据（经验回放、智能体快照、比赛回放）
 * - 批量读写操作
 * - 自动版本管理，schema 变更时无感升级
 *
 * 技术特点：
 * - IndexedDB 异步存储，支持数百 MB 数据
 * - 权重专用压缩（量化 + 差分 + 游程编码 + VLQ Base64）
 * - 通用字符串压缩（LZ77 滑动窗口）
 * - 浏览器重启后数据不丢失
 * - 无外部依赖，即插即用
 * ============================================================ */

/** 默认数据库名称 */
const DB_NAME = 'trae_rl_db';

/** 默认对象存储名称列表 */
const DEFAULT_STORES = ['agents', 'replays', 'weights', 'training'];

/** 自动压缩阈值（JSON 序列化长度超过此值时启用压缩） */
const COMPRESS_THRESHOLD = 10240; // 10KB

/** VLQ Base64 字符表（64 字符，URL 安全） */
const B64_CHARS = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/';

/** IndexedDB 事务超时（毫秒） */
const TX_TIMEOUT = 30000;

// ==================== 内部压缩工具 ====================

/**
 * LZ-String 风格的压缩工具集
 * 提供网络权重专用压缩和通用字符串压缩
 */
const _Compression = {
  // ---- VLQ Base64 编解码 ----

  /**
   * 将整数数组编码为 VLQ (Variable Length Quantity) Base64 字符串
   * 每个整数使用 1~N 个 Base64 字符编码：
   * - 每字符携带 5 位数据 + 1 位继续标志（第 6 位）
   * - 使用 ZigZag 编码将负数映射为正整数
   * @param {number[]} ints 整数数组
   * @returns {string} VLQ Base64 字符串
   */
  _encodeVLQ(ints: number[]): string {
    let result = '';
    for (let idx = 0; idx < ints.length; idx++) {
      const n = ints[idx];
      // ZigZag: 0→0, -1→1, 1→2, -2→3, 2→4, ...
      let v = n >= 0 ? n * 2 : -n * 2 - 1;
      do {
        const chunk = v & 31;          // 低 5 位数据
        v >>>= 5;                       // 右移 5 位
        const flag = v > 0 ? 32 : 0;   // 继续标志位（bit 5）
        result += B64_CHARS[chunk | flag];
      } while (v > 0);
    }
    return result;
  },

  /**
   * 将 VLQ Base64 字符串解码为整数数组
   * @param {string} str VLQ Base64 字符串
   * @returns {number[]} 整数数组
   */
  _decodeVLQ(str: string): number[] {
    const ints = [];
    let i = 0;
    while (i < str.length) {
      let v = 0;
      let shift = 0;
      let chunk;
      do {
        chunk = B64_CHARS.indexOf(str[i++]);
        if (chunk === -1) {
          console.warn('[Compression] 非法 VLQ 字符:', str[i - 1]);
          break;
        }
        v |= (chunk & 31) << shift;
        shift += 5;
      } while (chunk >= 32); // 继续标志存在
      // Un-ZigZag
      ints.push(v & 1 ? -((v + 1) >> 1) : v >> 1);
    }
    return ints;
  },

  // ---- 权重专用压缩 ----

  /**
   * 压缩网络权重数组
   *
   * 策略：量化(Int16) → 差分编码 → 游程编码(RLE) → VLQ Base64
   *
   * 对于典型的神经网络权重（值域 [-1,1]），差分 + RLE 可大幅压缩
   * 连续相似权重产生的重复差分值。
   *
   * @param {Float64Array|Float32Array|number[]} weights 权重数组
   * @param {number} [precision=65536] 量化精度（默认 2^16=65536）
   * @returns {{encoded: string, originalType: string, length: number, precision: number}}
   */
  _compressWeights(weights: Float64Array | Float32Array | number[], precision: number = 65536) {
    const arr = Array.from(weights);
    const half = precision / 2;

    // 1. 量化：将 [-1, 1] 映射到 Int16 范围
    const ints = new Int32Array(arr.length);
    for (let i = 0; i < arr.length; i++) {
      // 裁剪到 [-1, 1] 避免极端值溢出
      const clipped = Math.max(-1, Math.min(1, arr[i]));
      ints[i] = Math.round(clipped * half);
    }

    // 2. 差分编码
    const deltas = new Int32Array(ints.length);
    deltas[0] = ints[0];
    for (let i = 1; i < ints.length; i++) {
      deltas[i] = ints[i] - ints[i - 1];
    }

    // 3. 游程编码：合并连续相同的差分值
    const rlePairs = [];
    let count = 1;
    for (let i = 1; i <= deltas.length; i++) {
      if (i < deltas.length && deltas[i] === deltas[i - 1] && count < 255) {
        count++;
      } else {
        rlePairs.push(count, deltas[i - 1]);
        count = 1;
      }
    }

    // 4. VLQ Base64 编码
    const encoded = this._encodeVLQ(rlePairs);

    // 记录原始类型
    let originalType = 'Array';
    if (weights instanceof Float64Array) originalType = 'Float64Array';
    else if (weights instanceof Float32Array) originalType = 'Float32Array';

    return { encoded, originalType, length: arr.length, precision };
  },

  /**
   * 解压网络权重数组
   * @param {Object} packed 压缩信息
   * @param {string} packed.encoded VLQ Base64 字符串
   * @param {string} packed.originalType 原始数组类型
   * @param {number} packed.length 原始长度
   * @param {number} packed.precision 量化精度
   * @returns {Float64Array} 解压后的权重数组
   */
  _decompressWeights(packed: { encoded: string; originalType: string; length: number; precision: number }): Float64Array {
    const { encoded, originalType, length, precision } = packed;
    const half = precision / 2;

    // 1. VLQ Base64 解码
    const rlePairs = this._decodeVLQ(encoded);

    // 2. 游程解码
    const deltas = [];
    for (let i = 0; i < rlePairs.length; i += 2) {
      const cnt = rlePairs[i];
      const val = rlePairs[i + 1];
      for (let j = 0; j < cnt; j++) deltas.push(val);
    }

    // 如果解码后的长度与预期不符，以实际为准
    // 3. 差分解码（累积和还原）
    const ints = new Int32Array(deltas.length);
    ints[0] = deltas[0];
    for (let i = 1; i < deltas.length; i++) {
      ints[i] = ints[i - 1] + deltas[i];
    }

    // 4. 反量化：Int16 → Float64
    const result = new Float64Array(ints.length);
    for (let i = 0; i < ints.length; i++) {
      result[i] = ints[i] / half;
    }

    // 如果指定了长度且结果较短，自动补零
    if (length && result.length < length) {
      const padded = new Float64Array(length);
      padded.set(result);
      return padded;
    }

    return result;
  },

  // ---- 通用字符串压缩（LZ77 风格） ----

  /**
   * 通用字符串压缩（LZ77 滑动窗口）
   *
   * 对 JSON 序列化后的字符串进行压缩。使用 4096 字节滑动窗口
   * 和 60 字符前瞻缓冲区，找到重复子串并用 (distance, length)
   * 引用替换。
   *
   * @param {string} str 原始字符串
   * @returns {string} VLQ Base64 压缩字符串
   */
  _compressString(str: string): string {
    if (!str || str.length < 64) {
      // 短数据直接返回（压缩无收益）
      return this._encodeVLQ([0]) + this._encodeVLQ(this._strToCodes(str));
    }

    const tokens = [];
    let i = 0;
    const windowSize = 4096;
    const lookaheadSize = 60;

    while (i < str.length) {
      let bestDist = 0;
      let bestLen = 0;
      const start = Math.max(0, i - windowSize);

      // 在滑动窗口中查找最长匹配
      for (let j = start; j < i; j++) {
        let len = 0;
        while (
          i + len < str.length &&
          str[j + len] === str[i + len] &&
          len < lookaheadSize
        ) {
          len++;
        }
        if (len > bestLen) {
          bestDist = i - j;
          bestLen = len;
        }
      }

      if (bestLen >= 3) {
        // 匹配引用: type=1, distance, length
        tokens.push(1, bestDist, bestLen);
        i += bestLen;
      } else {
        // 字面量: type=0, charCode
        tokens.push(0, str.charCodeAt(i));
        i++;
      }
    }

    // 编码格式：[flag: 1=压缩][tokens...]
    const encoded = this._encodeVLQ([1]) + this._encodeVLQ(tokens);
    return encoded;
  },

  /**
   * 通用字符串解压
   * @param {string} compressed VLQ Base64 压缩字符串
   * @returns {string} 原始字符串
   */
  _decompressString(compressed: string): string {
    // 解析第一个整数（压缩标志）
    let pos = 0;
    let flag = 0;
    let shift = 0;
    let chunk;
    do {
      chunk = B64_CHARS.indexOf(compressed[pos++]);
      if (chunk === -1) break;
      flag |= (chunk & 31) << shift;
      shift += 5;
    } while (chunk >= 32);
    // Un-ZigZag
    flag = flag & 1 ? -((flag + 1) >> 1) : flag >> 1;

    const payload = compressed.slice(pos);

    if (flag === 0) {
      // 未压缩模式：直接解码为字符串
      const codes = this._decodeVLQ(payload);
      return codes.map(c => String.fromCharCode(c)).join('');
    }

    // 已压缩模式：解码 tokens 并还原
    const tokens = this._decodeVLQ(payload);
    const result = [];
    let ti = 0;
    while (ti < tokens.length) {
      const type = tokens[ti++];
      if (type === 0) {
        // 字面量
        result.push(String.fromCharCode(tokens[ti++]));
      } else {
        // 匹配引用
        const dist = tokens[ti++];
        const len = tokens[ti++];
        const start = result.length - dist;
        for (let j = 0; j < len; j++) {
          result.push(result[start + j]);
        }
      }
    }
    return result.join('');
  },

  // ---- 工具方法 ----

  /**
   * 将字符串转换为 Unicode 码点数组
   * @param {string} str
   * @returns {number[]}
   */
  _strToCodes(str: string): number[] {
    const codes = [];
    for (let i = 0; i < str.length; i++) {
      codes.push(str.charCodeAt(i));
    }
    return codes;
  },

  /**
   * 获取数据的原始类型名称
   * @param {*} data
   * @returns {string}
   */
  _getDataType(data: any): string {
    if (data instanceof Float64Array) return 'Float64Array';
    if (data instanceof Float32Array) return 'Float32Array';
    if (data instanceof Int32Array) return 'Int32Array';
    if (data instanceof Uint8Array) return 'Uint8Array';
    if (Array.isArray(data)) return 'Array';
    return typeof data;
  },

  /**
   * 自动检测数据是否应使用权重压缩
   * @param {*} data
   * @returns {boolean}
   */
  _isWeightData(data: any): boolean {
    if (data instanceof Float64Array || data instanceof Float32Array) {
      return data.length > 50;
    }
    if (Array.isArray(data)) {
      return data.length > 100 && typeof data[0] === 'number';
    }
    return false;
  },

  /**
   * 准备压缩数据（自动选择压缩策略）
   * @param {*} data 原始数据
   * @returns {Object} 压缩包 { type, packed, meta }
   */
  pack(data: any) {
    if (this._isWeightData(data)) {
      const wc = this._compressWeights(data);
      return {
        type: 'weights',
        packed: wc.encoded,
        meta: {
          originalType: wc.originalType,
          length: wc.length,
          precision: wc.precision,
        },
      };
    }
    // 通用数据：序列化为 JSON 后压缩
    const json = JSON.stringify(data);
    const compressed = this._compressString(json);
    return {
      type: 'json',
      packed: compressed,
      meta: { originalType: this._getDataType(data) },
    };
  },

  /**
   * 解压数据（自动识别压缩策略）
   * @param {Object} pack 压缩包 { type, packed, meta }
   * @returns {*} 原始数据
   */
  unpack(pack: any): any {
    if (pack.type === 'weights') {
      return this._decompressWeights({
        encoded: pack.packed,
        originalType: pack.meta.originalType,
        length: pack.meta.length,
        precision: pack.meta.precision,
      });
    }
    const json = this._decompressString(pack.packed);
    return JSON.parse(json);
  },
};

// ==================== 持久化主类 ====================

/**
 * RL 持久化层
 *
 * 基于 IndexedDB 的永久存储，用于保存 RL 智能体的网络权重、
 * 训练数据和比赛回放。支持大容量数据（数百 MB）和浏览器重启后恢复。
 *
 * @example
 * ```js
 * const rlp = new RLPersistence();
 * await rlp.open();
 * await rlp.save('weights', 'policy_net', new Float64Array([...]));
 * const data = await rlp.load('weights', 'policy_net');
 * ```
 */
export class RLPersistence {
  /** 数据库名称 */
  dbName: string;
  /** 对象存储名称列表 */
  storeNames: string[];
  /** IndexedDB 数据库连接 */
  db: IDBDatabase | null;
  /** 当前数据库版本号 */
  _version: number;
  /** 是否已完成初始化（数据库已打开且 schema 校验通过） */
  _ready: boolean;

  /**
   * @param {string} [dbName='trae_rl_db'] 数据库名称
   * @param {string[]} [storeNames] 对象存储名称列表，默认 ['agents','replays','weights','training']
   */
  constructor(dbName: string = DB_NAME, storeNames: string[] = DEFAULT_STORES) {
    /** 数据库名称 */
    this.dbName = dbName;
    /** 对象存储名称列表 */
    this.storeNames = storeNames;
    /** IndexedDB 数据库连接 */
    this.db = null;
    /** 当前数据库版本号 */
    this._version = 1;
    /** 是否已完成初始化（数据库已打开且 schema 校验通过） */
    this._ready = false;
  }

  // ==================== 数据库连接管理 ====================

  /**
   * 检测当前环境是否支持 IndexedDB
   * @returns {boolean}
   */
  static isSupported(): boolean {
    try {
      return (
        typeof indexedDB !== 'undefined' &&
        indexedDB !== null &&
        typeof indexedDB.open === 'function'
      );
    } catch (e) {
      return false;
    }
  }

  /**
   * 打开数据库连接
   *
   * 自动检测 schema 变更并递增版本号触发升级。
   * 最多尝试 10 次版本协商。
   *
   * @returns {Promise<IDBDatabase>}
   * @throws {Error} 如果浏览器不支持 IndexedDB 或版本协商失败
   */
  async open(): Promise<IDBDatabase> {
    if (this.db && this._ready) return this.db;

    if (!RLPersistence.isSupported()) {
      throw new Error('[RLPersistence] 当前浏览器不支持 IndexedDB，请使用现代浏览器');
    }

    for (let attempt = 0; attempt < 10; attempt++) {
      const version = this._version + attempt;
      try {
        this.db = await this._openDb(version);
        if (this._isSchemaValid()) {
          this._version = version;
          this._ready = true;
          return this.db;
        }
        // Schema 不匹配：关闭连接，递增版本重试
        console.log(
          `[RLPersistence] Schema 变更，从 v${version} 升级到 v${version + 1}`
        );
        this.db.close();
        this.db = null;
      } catch (e) {
        console.warn(`[RLPersistence] 打开数据库失败 (v${version}):`, e.message);
        throw e;
      }
    }
    throw new Error('[RLPersistence] 无法打开数据库：版本协商失败（超过最大尝试次数）');
  }

  /**
   * 内部打开 IndexedDB
   * @param {number} version
   * @returns {Promise<IDBDatabase>}
   */
  _openDb(version: number): Promise<IDBDatabase> {
    return new Promise((resolve, reject) => {
      const request = indexedDB.open(this.dbName, version);

      request.onupgradeneeded = (event) => {
        const db = (event.target as IDBRequest).result as IDBDatabase;
        // 创建缺失的对象存储（幂等操作）
        for (const name of this.storeNames) {
          if (!db.objectStoreNames.contains(name)) {
            console.log(`[RLPersistence] v${version}: 创建对象存储 "${name}"`);
            db.createObjectStore(name, { keyPath: 'key' });
          }
        }
      };

      request.onsuccess = () => {
        resolve(request.result);
      };

      request.onerror = () => {
        reject(
          new Error(
            `IndexedDB 打开失败: ${request.error?.message || '未知错误'}`
          )
        );
      };

      request.onblocked = () => {
        console.warn(
          '[RLPersistence] 数据库被阻塞，请关闭其他使用该数据库的标签页'
        );
      };
    });
  }

  /**
   * 校验当前数据库的 schema 是否满足配置
   * @returns {boolean}
   */
  _isSchemaValid(): boolean {
    if (!this.db) return false;
    const existing = Array.from(this.db.objectStoreNames);
    return this.storeNames.every((name) => existing.includes(name));
  }

  /**
   * 确保数据库已打开就绪
   * @returns {Promise<void>}
   */
  async _ensureOpen(): Promise<void> {
    if (!this.db || !this._ready) {
      await this.open();
    }
  }

  /**
   * 获取对象存储的事务引用
   * @param {string} storeName 存储名称
   * @param {string} [mode='readonly'] 事务模式
   * @returns {IDBObjectStore}
   */
  _getStore(storeName: string, mode: IDBTransactionMode = 'readonly'): IDBObjectStore {
    if (!this.db) throw new Error('[RLPersistence] 数据库未打开，请先调用 open()');
    if (!this.storeNames.includes(storeName)) {
      throw new Error(`[RLPersistence] 未知的对象存储: "${storeName}"`);
    }
    const tx = this.db.transaction(storeName, mode);
    return tx.objectStore(storeName);
  }

  // ==================== 核心 CRUD ====================

  /**
   * 保存数据到指定对象存储
   *
   * 大型数据（JSON 序列化 >10KB）自动启用压缩：
   * - Float64Array / Float32Array 使用权重专用压缩
   * - 其他数据使用通用字符串压缩
   *
   * @param {string} storeName 存储名称
   * @param {string} key 记录键
   * @param {*} data 数据
   * @returns {Promise<void>}
   */
  async save(storeName: string, key: string, data: any): Promise<void> {
    try {
      await this._ensureOpen();
      const store = this._getStore(storeName, 'readwrite');

      const entry: { key: string; compressed: boolean; data: any; timestamp: number; dataType: string; compressedSize?: number } = {
        key,
        compressed: false,
        data,
        timestamp: Date.now(),
        dataType: _Compression._getDataType(data),
      };

      // 检查是否需要压缩
      let jsonSize;
      try {
        jsonSize = JSON.stringify(data).length;
      } catch (e) {
        // 某些类型（如 Float64Array）的 JSON 序列化可能产生巨大字符串
        // 先估计一个保守值
        if (data && typeof data.length === 'number') {
          jsonSize = data.length * 30; // 每个数约 30 字符
        } else {
          jsonSize = COMPRESS_THRESHOLD + 1; // 强制压缩
        }
      }

      if (jsonSize > COMPRESS_THRESHOLD) {
        // 启用压缩
        entry.compressed = true;
        const pack = _Compression.pack(data);
        entry.data = pack;
        entry.compressedSize = pack.packed.length;
      }

      return new Promise((resolve, reject) => {
        const req = store.put(entry);

        req.onsuccess = () => resolve();

        req.onerror = () => {
          // 存储配额满时的友好提示
          if (
            req.error &&
            (req.error.name === 'QuotaExceededError' ||
             req.error.name === 'NS_ERROR_DOM_QUOTA_REACHED')
          ) {
            console.warn(
              `[RLPersistence] 存储空间不足 [${storeName}/${key}], ` +
              '请清理旧数据或增大浏览器存储配额'
            );
          }
          reject(
            new Error(
              `保存失败 [${storeName}/${key}]: ${req.error?.message || '未知错误'}`
            )
          );
        };
      });
    } catch (e) {
      console.warn(`[RLPersistence] 保存失败 [${storeName}/${key}]:`, e.message);
      throw e;
    }
  }

  /**
   * 从指定对象存储加载数据
   *
   * 自动识别并解压压缩数据。
   *
   * @param {string} storeName 存储名称
   * @param {string} key 记录键
   * @returns {Promise<*>} 数据，若不存在返回 null
   */
  async load(storeName: string, key: string): Promise<any> {
    try {
      await this._ensureOpen();
      const store = this._getStore(storeName);

      return new Promise((resolve, reject) => {
        const req = store.get(key);

        req.onsuccess = () => {
          const entry = req.result;
          if (!entry) {
            resolve(null);
            return;
          }

          if (entry.compressed && entry.data && entry.data.type) {
            // 解压数据
            try {
              const decompressed = _Compression.unpack(entry.data);
              resolve(decompressed);
            } catch (de) {
              console.warn(
                `[RLPersistence] 解压失败 [${storeName}/${key}], 返回原始压缩数据:`,
                de.message
              );
              resolve(entry.data);
            }
          } else {
            resolve(entry.data);
          }
        };

        req.onerror = () => {
          reject(
            new Error(
              `加载失败 [${storeName}/${key}]: ${req.error?.message || '未知错误'}`
            )
          );
        };
      });
    } catch (e) {
      console.warn(`[RLPersistence] 加载失败 [${storeName}/${key}]:`, e.message);
      return null;
    }
  }

  /**
   * 删除指定对象存储中的一条记录
   * @param {string} storeName 存储名称
   * @param {string} key 记录键
   * @returns {Promise<void>}
   */
  async delete(storeName: string, key: string): Promise<void> {
    try {
      await this._ensureOpen();
      const store = this._getStore(storeName, 'readwrite');

      return new Promise((resolve, reject) => {
        const req = store.delete(key);

        req.onsuccess = () => resolve();

        req.onerror = () => {
          reject(
            new Error(
              `删除失败 [${storeName}/${key}]: ${req.error?.message || '未知错误'}`
            )
          );
        };
      });
    } catch (e) {
      console.warn(`[RLPersistence] 删除失败 [${storeName}/${key}]:`, e.message);
    }
  }

  // ==================== 智能体管理 ====================

  /**
   * 保存智能体完整数据
   *
   * 智能体数据通常包含：
   * - network: 网络权重（Float64Array）
   * - hyperparams: 超参数对象
   * - stats: 训练统计
   * - replayBuffer: 经验回放数据（可选）
   *
   * @param {string} name 智能体名称
   * @param {Object} agentData 智能体数据
   * @returns {Promise<void>}
   */
  async saveAgent(name: string, agentData: any): Promise<void> {
    await this.save('agents', name, agentData);
  }

  /**
   * 加载智能体数据
   * @param {string} name 智能体名称
   * @returns {Promise<Object|null>}
   */
  async loadAgent(name: string): Promise<any> {
    return this.load('agents', name);
  }

  /**
   * 列出所有已保存的智能体
   * @returns {Promise<string[]>} 智能体名称列表
   */
  async listAgents(): Promise<string[]> {
    try {
      await this._ensureOpen();
      const store = this._getStore('agents');

      return new Promise((resolve, reject) => {
        const req = store.getAllKeys();

        req.onsuccess = () => {
          const keys = Array.from(req.result);
          // 过滤出字符串类型的键（排除内部元数据键）
          resolve(keys.filter((k) => typeof k === 'string'));
        };

        req.onerror = () => {
          reject(
            new Error(
              `列出智能体失败: ${req.error?.message || '未知错误'}`
            )
          );
        };
      });
    } catch (e) {
      console.warn('[RLPersistence] 列出智能体失败:', e.message);
      return [];
    }
  }

  /**
   * 列出指定对象存储的全部记录键（P2-1a：人类轨迹加载用）
   * @param {string} storeName 对象存储名称
   * @returns {Promise<Array<string|number>>} 记录键列表；失败返回 []
   */
  async listKeys(storeName: string): Promise<Array<string | number>> {
    try {
      await this._ensureOpen();
      if (!this.storeNames.includes(storeName)) {
        throw new Error(`[RLPersistence] 未知的对象存储: "${storeName}"`);
      }
      const store = this._getStore(storeName);

      return new Promise((resolve, reject) => {
        const req = store.getAllKeys();
        req.onsuccess = () => resolve(Array.from(req.result) as (string | number)[]);
        req.onerror = () => {
          reject(
            new Error(
              `列出记录失败 [${storeName}]: ${req.error?.message || '未知错误'}`
            )
          );
        };
      });
    } catch (e) {
      console.warn(`[RLPersistence] 列出记录失败 [${storeName}]:`, e.message);
      return [];
    }
  }

  /**
   * 删除智能体
   * @param {string} name 智能体名称
   * @returns {Promise<void>}
   */
  async deleteAgent(name: string): Promise<void> {
    await this.delete('agents', name);
  }

  // ==================== 回放管理 ====================

  /**
   * 保存比赛回放数据
   *
   * 回放数据通常包含逐帧状态/动作序列，数据量较大，
   * 会自动启用压缩存储。
   *
   * @param {string} name 回放名称
   * @param {Object} replayData 回放数据 { frames, events, metadata, ... }
   * @returns {Promise<void>}
   */
  async saveReplay(name: string, replayData: any): Promise<void> {
    await this.save('replays', name, replayData);
  }

  /**
   * 加载比赛回放数据
   * @param {string} name 回放名称
   * @returns {Promise<Object|null>}
   */
  async loadReplay(name: string): Promise<any> {
    return this.load('replays', name);
  }

  // ==================== 批量操作 ====================

  /**
   * 批量保存多条记录
   *
   * 使用同一事务批量写入，比逐条 save() 更高效。
   *
   * @param {string} storeName 存储名称
   * @param {Array<{key: string, data: *}>} entries 条目列表
   * @returns {Promise<void>}
   */
  async saveMultiple(storeName: string, entries: Array<{ key: string; data: any }>): Promise<void> {
    if (!entries || entries.length === 0) return;

    try {
      await this._ensureOpen();
      if (!this.storeNames.includes(storeName)) {
        throw new Error(`[RLPersistence] 未知的对象存储: "${storeName}"`);
      }

      const tx = this.db.transaction(storeName, 'readwrite');
      const store = tx.objectStore(storeName);

      // 写入所有条目
      for (const { key, data } of entries) {
        const entry: { key: string; compressed: boolean; data: any; timestamp: number; dataType: string; compressedSize?: number } = {
          key,
          compressed: false,
          data,
          timestamp: Date.now(),
          dataType: _Compression._getDataType(data),
        };

        // 自动压缩
        let jsonSize;
        try {
          jsonSize = JSON.stringify(data).length;
        } catch (e) {
          jsonSize =
            data && typeof data.length === 'number'
              ? data.length * 30
              : COMPRESS_THRESHOLD + 1;
        }

        if (jsonSize > COMPRESS_THRESHOLD) {
          entry.compressed = true;
          const pack = _Compression.pack(data);
          entry.data = pack;
          entry.compressedSize = pack.packed.length;
        }

        store.put(entry);
      }

      return new Promise((resolve, reject) => {
        tx.oncomplete = () => resolve();
        tx.onerror = () => {
          reject(
            new Error(
              `批量保存失败 [${storeName}]: ${tx.error?.message || '未知错误'}`
            )
          );
        };
        tx.onabort = () => {
          reject(new Error(`批量保存事务中止 [${storeName}]`));
        };
      });
    } catch (e) {
      console.warn(`[RLPersistence] 批量保存失败 [${storeName}]:`, e.message);
      throw e;
    }
  }

  /**
   * 批量加载多条记录
   *
   * 在单个事务中并行加载指定键的记录，自动解压。
   * 部分键不存在或加载失败时返回 null 值，不影响其他结果。
   *
   * @param {string} storeName 存储名称
   * @param {string[]} keys 键列表
   * @returns {Promise<Map<string, *>>} 键到数据的映射
   */
  async loadMultiple(storeName: string, keys: string[]): Promise<Map<string, any>> {
    if (!keys || keys.length === 0) return new Map();

    try {
      await this._ensureOpen();
      if (!this.storeNames.includes(storeName)) {
        throw new Error(`[RLPersistence] 未知的对象存储: "${storeName}"`);
      }

      const tx = this.db.transaction(storeName, 'readonly');
      const store = tx.objectStore(storeName);
      const results = new Map();

      return new Promise((resolve, reject) => {
        let completed = 0;
        const total = keys.length;

        for (const key of keys) {
          const req = store.get(key);

          req.onsuccess = () => {
            const entry = req.result;
            if (!entry) {
              results.set(key, null);
            } else if (entry.compressed && entry.data && entry.data.type) {
              try {
                results.set(key, _Compression.unpack(entry.data));
              } catch (de) {
                console.warn(
                  `[RLPersistence] 批量加载解压失败 [${storeName}/${key}]:`,
                  de.message
                );
                results.set(key, entry.data);
              }
            } else {
              results.set(key, entry.data);
            }
            completed++;
            if (completed === total) resolve(results);
          };

          req.onerror = () => {
            console.warn(
              `[RLPersistence] 批量加载失败 [${storeName}/${key}]:`,
              req.error?.message
            );
            results.set(key, null);
            completed++;
            if (completed === total) resolve(results);
          };
        }
      });
    } catch (e) {
      console.warn(`[RLPersistence] 批量加载失败 [${storeName}]:`, e.message);
      return new Map();
    }
  }

  // ==================== 维护操作 ====================

  /**
   * 清除所有对象存储中的数据
   *
   * 注意：此操作不可逆，会删除所有已保存的智能体、回放和训练数据。
   *
   * @returns {Promise<void>}
   */
  async clearAll(): Promise<void> {
    try {
      await this._ensureOpen();
      const tx = this.db.transaction(this.storeNames, 'readwrite');

      return new Promise((resolve, reject) => {
        for (const name of this.storeNames) {
          tx.objectStore(name).clear();
        }

        tx.oncomplete = () => {
          console.log('[RLPersistence] 所有数据已清除');
          resolve();
        };

        tx.onerror = () => {
          reject(
            new Error(
              `清除数据失败: ${tx.error?.message || '未知错误'}`
            )
          );
        };
      });
    } catch (e) {
      console.warn('[RLPersistence] 清除数据失败:', e.message);
    }
  }

  /**
   * 关闭数据库连接
   *
   * 建议在页面卸载或不再需要持久化时调用，
   * 以释放 IndexedDB 连接资源。
   */
  close(): void {
    if (this.db) {
      this.db.close();
      this.db = null;
      this._ready = false;
    }
  }
}

export default RLPersistence;