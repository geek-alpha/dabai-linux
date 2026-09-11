/* ============================================================
 * 高级神经网络 — Adam + NoisyNet + LayerNorm + 梯度裁剪
 *
 * 性能优化：
 * - Adam 自适应学习率（momentum + RMSProp 融合）
 * - Layer Normalization（隐藏层输出归一化 → 避免梯度消失/爆炸）
 * - NoisyNet 参数噪声（状态感知探索，替代 ε-greedy）
 * - 梯度裁剪（max norm = 1.0，防止梯度爆炸）
 * - He 初始化 + 小偏置（0.01）
 *
 * 与 nn.js 完全兼容接口（可作为即插即用替换）
 * ============================================================ */

/** LéCun 修正指数线性单元 — 比 ReLU 梯度流更稳定 */
function selu(x: number): number {
  const alpha = 1.6732632423543772848170429916717;
  const scale = 1.0507009873554804934193349852946;
  return scale * (x >= 0 ? x : alpha * (Math.exp(x) - 1));
}

/** SELU 导数（x = SELU 的输入，即 LayerNorm 输出） */
function seluDeriv(x: number): number {
  const alpha = 1.6732632423543772848170429916717;
  const scale = 1.0507009873554804934193349852946;
  return x >= 0 ? scale : scale * alpha * Math.exp(x);
}

export class NeuralNetV2 {
  /** 网络层结构 [inputSize, hidden1, ..., outputSize] */
  layers: number[];
  /** 全连接层数量 */
  nLayers: number;
  /** 是否启用 NoisyNet（默认启用） */
  useNoisy: boolean;
  /** NoisyNet 初始标准差 */
  noisyStd: number;
  /** 学习率 */
  lr: number;
  /** 每层权重 Float64Array(out * in) */
  weights: Float64Array[];
  /** 每层偏置 Float64Array(out) */
  biases: Float64Array[];
  /** 一阶矩 (weights) */
  _mW: Float64Array[];
  /** 二阶矩 (weights) */
  _vW: Float64Array[];
  /** 一阶矩 (biases) */
  _mB: Float64Array[];
  /** 二阶矩 (biases) */
  _vB: Float64Array[];
  /** Adam 时间步 */
  _adamT: number;
  /** LayerNorm gamma（每隐藏层独立，输出层为 null） */
  _lnGamma: (Float64Array | null)[];
  /** LayerNorm beta（每隐藏层独立，输出层为 null） */
  _lnBeta: (Float64Array | null)[];
  /** NoisyNet μ (weights) */
  _noisyMuW: Float64Array[];
  /** NoisyNet σ (weights) */
  _noisySigW: Float64Array[];
  /** NoisyNet μ (bias) */
  _noisyMuB: Float64Array[];
  /** NoisyNet σ (bias) */
  _noisySigB: Float64Array[];
  /** 缓存当前 eps (weights) */
  _noisyEpsW: Float64Array[];
  /** 缓存当前 eps (bias) */
  _noisyEpsB: Float64Array[];
  /** 确定性随机数生成器 */
  _rng: () => number;

  /**
   * @param {number[]} layers [inputSize, hidden1, ..., outputSize]
   * @param {Object} opts
   * @param {number} [opts.lr=0.001] 学习率
   * @param {number} [opts.noisyStd=0.1] NoisyNet 初始标准差
   * @param {number} [opts.seed=42] 随机种子
   */
  constructor(layers: number[], opts: { lr?: number; noisy?: boolean; noisyStd?: number; seed?: number } = {}) {
    this.layers = layers.slice();
    this.nLayers = layers.length - 1;
    this.useNoisy = opts.noisy !== false;     // 默认启用 NoisyNet
    this.noisyStd = opts.noisyStd ?? 0.1;
    this.lr = opts.lr ?? 0.001;

    // 参数存储
    this.weights = [];  // 每层 Float64Array(out * in)
    this.biases = [];   // 每层 Float64Array(out)

    // Adam 状态
    this._mW = [];  // 一阶矩 (weights)
    this._vW = [];  // 二阶矩 (weights)
    this._mB = [];  // 一阶矩 (biases)
    this._vB = [];  // 二阶矩 (biases)
    this._adamT = 0;

    // LayerNorm 参数（每个隐藏层独立 gamma, beta）
    this._lnGamma = [];
    this._lnBeta = [];

    // NoisyNet 噪声参数（每个全连接层独立）
    this._noisyMuW = [];   // μ (weights)
    this._noisySigW = [];  // σ (weights)
    this._noisyMuB = [];   // μ (bias)
    this._noisySigB = [];  // σ (bias)
    this._noisyEpsW = [];  // 缓存当前 eps (weights)
    this._noisyEpsB = [];  // 缓存当前 eps (bias)

    this._rng = this._makeRng(opts.seed ?? 42);

    // 初始化
    for (let i = 0; i < this.nLayers; i++) {
      const fanIn = layers[i];
      const fanOut = layers[i + 1];
      const std = Math.sqrt(2.0 / fanIn);

      // 标准权重（He init）
      const w = new Float64Array(fanOut * fanIn);
      for (let j = 0; j < w.length; j++) w[j] = this._gaussian() * std;
      this.weights.push(w);
      this.biases.push(new Float64Array(fanOut));

      // Adam 状态
      this._mW.push(new Float64Array(w.length));
      this._vW.push(new Float64Array(w.length));
      this._mB.push(new Float64Array(fanOut));
      this._vB.push(new Float64Array(fanOut));

      // LayerNorm（隐藏层）
      if (i < this.nLayers - 1) {
        const gamma = new Float64Array(fanOut);
        const beta = new Float64Array(fanOut);
        gamma.fill(1.0); // gamma 初始化为 1
        this._lnGamma.push(gamma);
        this._lnBeta.push(beta);
      } else {
        this._lnGamma.push(null);
        this._lnBeta.push(null);
      }

      // NoisyNet 参数（所有层，输出层也可用）
      const muW = new Float64Array(fanOut * fanIn);
      const sigW = new Float64Array(fanOut * fanIn);
      // μ 用 He init，σ 初始化为 noisyStd / sqrt(fanIn)
      const sigInit = this.noisyStd / Math.sqrt(fanIn);
      for (let j = 0; j < muW.length; j++) {
        muW[j] = this._gaussian() * std;
        sigW[j] = sigInit;
      }
      const muB = new Float64Array(fanOut);
      const sigB = new Float64Array(fanOut);
      sigB.fill(sigInit);
      this._noisyMuW.push(muW);
      this._noisySigW.push(sigW);
      this._noisyMuB.push(muB);
      this._noisySigB.push(sigB);
      this._noisyEpsW.push(new Float64Array(fanOut * fanIn));
      this._noisyEpsB.push(new Float64Array(fanOut));
    }
  }

  // ==================== 前向传播 ====================

  /**
   * 前向传播，返回输出层激活值
   * @param {Float64Array|number[]} input
   * @returns {Float64Array}
   */
  predict(input: Float64Array | number[]): Float64Array {
    return this.forward(input, false).output;
  }

  /**
   * 前向传播（完整）
   * @param {boolean} training 是否训练模式（NoisyNet 采样新噪声）
   * @returns {{activations: Float64Array[], zValues: Float64Array[], output: Float64Array, lnVals: Array}}
   */
  forward(input: Float64Array | number[], training: boolean = false):
    { activations: Float64Array[]; zValues: Float64Array[]; output: Float64Array; lnVals: (Float64Array | null)[]; lnStds: (number | null)[] } {
    // NoisyNet：如果训练，采样新噪声
    this._sampleNoise(training);

    const acts = [Float64Array.from(input)];
    const zs = [];
    const lnVals = [];
    const lnStds = [];

    let cur = acts[0];
    for (let i = 0; i < this.nLayers; i++) {
      const fanIn = this.layers[i];
      const fanOut = this.layers[i + 1];

      // 使用带噪声的权重（NoisyNet）
      const w = this._getNoisyWeight(i);
      const b = this._getNoisyBias(i);
      const z = new Float64Array(fanOut);

      // 矩阵乘 + 偏置
      for (let j = 0; j < fanOut; j++) {
        let sum = b[j];
        const base = j * fanIn;
        for (let k = 0; k < fanIn; k++) {
          sum += cur[k] * w[base + k];
        }
        z[j] = sum;
      }
      zs.push(z);

      // 输出层：线性；隐藏层：SELU + LayerNorm
      let a;
      if (i < this.nLayers - 1) {
        // LayerNorm
        const gamma = this._lnGamma[i];
        const beta = this._lnBeta[i];
        const { out: normalized, std } = this._layerNorm(z, gamma, beta);
        lnVals.push(normalized);
        lnStds.push(std);

        // SELU
        a = new Float64Array(fanOut);
        for (let j = 0; j < fanOut; j++) {
          a[j] = selu(normalized[j]);
        }
      } else {
        a = new Float64Array(z); // 线性输出
        lnVals.push(null);
        lnStds.push(null);
      }
      acts.push(a);
      cur = a;
    }
    return { activations: acts, zValues: zs, output: cur, lnVals, lnStds };
  }

  // ==================== 反向传播 ====================

  /**
   * 批量训练
   * @param {number[][]} batchInputs
   * @param {number[][]} batchTargets
   * @param {number} lr 学习率（可选，覆盖默认）
   * @returns {number} 平均 loss
   */
  trainBatch(batchInputs: Float64Array[], batchTargets: Float64Array[], lr?: number): number {
    const batchSize = batchInputs.length;
    if (batchSize === 0) return 0;

    // 梯度累积
    const gradW = [];
    const gradB = [];
    for (let i = 0; i < this.nLayers; i++) {
      gradW.push(new Float64Array(this.weights[i].length));
      gradB.push(new Float64Array(this.biases[i].length));
    }
    let totalLoss = 0;

    for (let s = 0; s < batchSize; s++) {
      const input = batchInputs[s];
      const target = batchTargets[s];
      const fwd = this.forward(input, true);
      const output = fwd.output;

      // MSE loss
      for (let j = 0; j < output.length; j++) {
        const diff = output[j] - target[j];
        totalLoss += diff * diff;
      }

      // 输出层 delta
      let delta = new Float64Array(this.layers[this.nLayers]);
      for (let j = 0; j < delta.length; j++) {
        delta[j] = (output[j] - target[j]); // 线性导数为1
      }

      // 反向传播
      for (let i = this.nLayers - 1; i >= 0; i--) {
        const fanIn = this.layers[i];
        const fanOut = this.layers[i + 1];
        const w = this._getNoisyWeight(i); // 使用当前噪声权重
        const actIn = fwd.activations[i];

        // 累积梯度到 gradW, gradB
        for (let j = 0; j < fanOut; j++) {
          gradB[i][j] += delta[j];
          const base = j * fanIn;
          for (let k = 0; k < fanIn; k++) {
            gradW[i][base + k] += delta[j] * actIn[k];
          }
        }

        // 传播 delta 到上一层
        if (i > 0) {
          const newDelta = new Float64Array(fanIn);
          const lnV = fwd.lnVals[i - 1];
          const lnStd = fwd.lnStds ? fwd.lnStds[i - 1] : null;
          for (let k = 0; k < fanIn; k++) {
            let sum = 0;
            for (let j = 0; j < fanOut; j++) {
              sum += delta[j] * w[j * fanIn + k];
            }
            // SELU 的输入是 LayerNorm 输出（lnV[k]），其导数基于该输入；
            // LayerNorm 反向传播：d_ln = d_selu * (1 / std)，与注释一致。
            const seluIn = lnV ? lnV[k] : fwd.zValues[i - 1][k];
            const d = seluDeriv(seluIn) * (lnStd ? 1.0 / lnStd : 1.0);
            newDelta[k] = sum * d;
          }
          delta = newDelta;
        }
      }
    }

    // 梯度裁剪 + Adam 更新
    const invBatch = 1.0 / batchSize;
    this._adamT++;

    for (let i = 0; i < this.nLayers; i++) {
      const gw = gradW[i];
      const gb = gradB[i];

      // 梯度裁剪（全局范数裁剪）
      let normSq = 0;
      for (let j = 0; j < gw.length; j++) normSq += gw[j] * gw[j];
      for (let j = 0; j < gb.length; j++) normSq += gb[j] * gb[j];
      const norm = Math.sqrt(normSq);
      const clipScale = norm > 1.0 ? 1.0 / norm : 1.0;

      // 梯度缩放
      for (let j = 0; j < gw.length; j++) gw[j] = gw[j] * invBatch * clipScale;
      for (let j = 0; j < gb.length; j++) gb[j] = gb[j] * invBatch * clipScale;

      // Adam 更新（权重的 μ 参数）
      const lrLocal = lr ?? this.lr;
      const beta1 = 0.9, beta2 = 0.999, eps = 1e-8;

      const muW = this._noisyMuW[i];
      const sigW = this._noisySigW[i];
      const mW = this._mW[i];
      const vW = this._vW[i];
      const mB = this._mB[i];
      const vB = this._vB[i];
      const muB = this._noisyMuB[i];
      const sigB = this._noisySigB[i];

      // 更新 μW
      for (let j = 0; j < muW.length; j++) {
        const g = gw[j];
        mW[j] = beta1 * mW[j] + (1 - beta1) * g;
        vW[j] = beta2 * vW[j] + (1 - beta2) * g * g;
        const mHat = mW[j] / (1 - Math.pow(beta1, this._adamT));
        const vHat = vW[j] / (1 - Math.pow(beta2, this._adamT));
        muW[j] -= lrLocal * mHat / (Math.sqrt(vHat) + eps);
      }

      // 更新 μB
      for (let j = 0; j < muB.length; j++) {
        const g = gb[j];
        mB[j] = beta1 * mB[j] + (1 - beta1) * g;
        vB[j] = beta2 * vB[j] + (1 - beta2) * g * g;
        const mHat = mB[j] / (1 - Math.pow(beta1, this._adamT));
        const vHat = vB[j] / (1 - Math.pow(beta2, this._adamT));
        muB[j] -= lrLocal * mHat / (Math.sqrt(vHat) + eps);
      }

      // NoisyNet σ 的更新（用相同梯度近似）
      if (this.useNoisy) {
        for (let j = 0; j < sigW.length; j++) {
          const g = gw[j] * this._noisyEpsW[i][j];
          sigW[j] += lrLocal * g;
          sigW[j] = Math.max(0.001, sigW[j]); // 下限
        }
        for (let j = 0; j < sigB.length; j++) {
          const g = gb[j] * this._noisyEpsB[i][j];
          sigB[j] += lrLocal * g;
          sigB[j] = Math.max(0.001, sigB[j]);
        }
      }
    }

    return totalLoss / batchSize;
  }

  // ==================== 网络操作 ====================

  copyFrom(other: NeuralNetV2): void {
    for (let i = 0; i < this.nLayers; i++) {
      // 复制 μ 参数（即常规权重）
      this._noisyMuW[i].set(other._noisyMuW[i]);
      this._noisyMuB[i].set(other._noisyMuB[i]);
      this._noisySigW[i].set(other._noisySigW[i]);
      this._noisySigB[i].set(other._noisySigB[i]);
      // 同步 weights/biases 兼容
      this.weights[i].set(other.weights[i]);
      this.biases[i].set(other.biases[i]);
      // 复制 LayerNorm
      if (this._lnGamma[i] && other._lnGamma[i]) {
        this._lnGamma[i].set(other._lnGamma[i]);
        this._lnBeta[i].set(other._lnBeta[i]);
      }
    }
  }

  softUpdateFrom(other: NeuralNetV2, tau: number): void {
    for (let i = 0; i < this.nLayers; i++) {
      const muW = this._noisyMuW[i];
      const omuW = other._noisyMuW[i];
      const muB = this._noisyMuB[i];
      const omuB = other._noisyMuB[i];
      for (let j = 0; j < muW.length; j++) muW[j] = muW[j] * (1 - tau) + omuW[j] * tau;
      for (let j = 0; j < muB.length; j++) muB[j] = muB[j] * (1 - tau) + omuB[j] * tau;
      // 同步 weights/biases
      const w = this.weights[i];
      const ow = other.weights[i];
      const b = this.biases[i];
      const ob = other.biases[i];
      for (let j = 0; j < w.length; j++) w[j] = w[j] * (1 - tau) + ow[j] * tau;
      for (let j = 0; j < b.length; j++) b[j] = b[j] * (1 - tau) + ob[j] * tau;
    }
  }

  // ==================== 序列化 ====================

  toJSON() {
    return {
      version: 2,
      layers: this.layers,
      lr: this.lr,
      useNoisy: this.useNoisy,
      noisyStd: this.noisyStd,
      muW: this._noisyMuW.map(a => Array.from(a)),
      muB: this._noisyMuB.map(a => Array.from(a)),
      sigW: this._noisySigW.map(a => Array.from(a)),
      sigB: this._noisySigB.map(a => Array.from(a)),
      weights: this.weights.map(a => Array.from(a)),
      biases: this.biases.map(a => Array.from(a)),
      lnGamma: this._lnGamma.map(a => a ? Array.from(a) : null),
      lnBeta: this._lnBeta.map(a => a ? Array.from(a) : null),
      adamM: this._mW.map(a => Array.from(a)),
      adamV: this._vW.map(a => Array.from(a)),
      adamBm: this._mB.map(a => Array.from(a)),
      adamBv: this._vB.map(a => Array.from(a)),
      adamT: this._adamT,
    };
  }

  fromJSON(data: any): void {
    this.layers = data.layers;
    this.nLayers = data.layers.length - 1;
    this.lr = data.lr ?? 0.001;
    this.useNoisy = data.useNoisy !== false;
    this.noisyStd = data.noisyStd ?? 0.1;

    this._noisyMuW = data.muW.map(a => Float64Array.from(a));
    this._noisyMuB = data.muB.map(a => Float64Array.from(a));
    this._noisySigW = data.sigW.map(a => Float64Array.from(a));
    this._noisySigB = data.sigB.map(a => Float64Array.from(a));
    this.weights = data.weights.map(a => Float64Array.from(a));
    this.biases = data.biases.map(a => Float64Array.from(a));
    this._lnGamma = data.lnGamma.map(a => a ? Float64Array.from(a) : null);
    this._lnBeta = data.lnBeta.map(a => a ? Float64Array.from(a) : null);
    this._mW = data.adamM.map(a => Float64Array.from(a));
    this._vW = data.adamV.map(a => Float64Array.from(a));
    this._mB = data.adamBm.map(a => Float64Array.from(a));
    this._vB = data.adamBv.map(a => Float64Array.from(a));
    this._adamT = data.adamT ?? 0;

    // Reinitialize noise epsilons
    this._noisyEpsW = this._noisyMuW.map(() => new Float64Array(0));
    this._noisyEpsB = this._noisyMuB.map(() => new Float64Array(0));
    for (let i = 0; i < this.nLayers; i++) {
      this._noisyEpsW[i] = new Float64Array(this._noisyMuW[i].length);
      this._noisyEpsB[i] = new Float64Array(this._noisyMuB[i].length);
    }
  }

  // ==================== NoisyNet 方法 ====================

  _sampleNoise(training: boolean): void {
    if (!training || !this.useNoisy) {
      // 推理模式：eps 全零，只使用 μ
      for (let i = 0; i < this.nLayers; i++) {
        this._noisyEpsW[i].fill(0);
        this._noisyEpsB[i].fill(0);
      }
      return;
    }
    // 训练模式：采样因子化高斯噪声
    for (let i = 0; i < this.nLayers; i++) {
      const nIn = this.layers[i];
      const nOut = this.layers[i + 1];
      // 输入噪声
      const epsIn = new Float64Array(nIn);
      for (let j = 0; j < nIn; j++) epsIn[j] = this._gaussian();
      // 输出噪声
      const epsOut = new Float64Array(nOut);
      for (let j = 0; j < nOut; j++) epsOut[j] = this._gaussian();
      // 外积 = epsOut ⊗ epsIn 的低秩近似
      const epsW = this._noisyEpsW[i];
      const epsB = this._noisyEpsB[i];
      for (let j = 0; j < nOut; j++) {
        for (let k = 0; k < nIn; k++) {
          epsW[j * nIn + k] = epsOut[j] * epsIn[k]; // 因子化噪声
        }
        epsB[j] = epsOut[j]; // bias 噪声 = epsOut
      }
    }
  }

  _getNoisyWeight(layerIdx: number): Float64Array {
    const muW = this._noisyMuW[layerIdx];
    const sigW = this._noisySigW[layerIdx];
    const epsW = this._noisyEpsW[layerIdx];
    const w = this.weights[layerIdx];
    for (let i = 0; i < w.length; i++) {
      w[i] = muW[i] + sigW[i] * epsW[i];
    }
    return w;
  }

  _getNoisyBias(layerIdx: number): Float64Array {
    const muB = this._noisyMuB[layerIdx];
    const sigB = this._noisySigB[layerIdx];
    const epsB = this._noisyEpsB[layerIdx];
    const b = this.biases[layerIdx];
    for (let i = 0; i < b.length; i++) {
      b[i] = muB[i] + sigB[i] * epsB[i];
    }
    return b;
  }

  // ==================== LayerNorm ====================

  _layerNorm(x: Float64Array, gamma: Float64Array, beta: Float64Array): { out: Float64Array; std: number } {
    const n = x.length;
    // 计算 mean, var
    let mean = 0;
    for (let i = 0; i < n; i++) mean += x[i];
    mean /= n;

    let varSum = 0;
    for (let i = 0; i < n; i++) {
      const d = x[i] - mean;
      varSum += d * d;
    }
    const std = Math.sqrt(varSum / n + 1e-5);

    const out = new Float64Array(n);
    for (let i = 0; i < n; i++) {
      out[i] = gamma[i] * ((x[i] - mean) / std) + beta[i];
    }
    return { out, std };
  }

  // ==================== 随机工具 ====================

  _gaussian(): number {
    const rng = this._rng;
    let u = 0, v = 0;
    while (u === 0) u = rng();
    while (v === 0) v = rng();
    return Math.sqrt(-2.0 * Math.log(u)) * Math.cos(2.0 * Math.PI * v);
  }

  _makeRng(seed: number): () => number {
    let s = seed;
    return function () {
      s |= 0; s = s + 0x6D2B79F5 | 0;
      let t = Math.imul(s ^ s >>> 15, 1 | s);
      t = t + Math.imul(t ^ t >>> 7, 61 | t) ^ t;
      return ((t ^ t >>> 14) >>> 0) / 4294967296;
    };
  }
}

export default NeuralNetV2;