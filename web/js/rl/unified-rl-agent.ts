/* ============================================================
 * UnifiedRLAgent — 统一强化学习智能体（Rainbow DQN-lite）
 *
 * 融合 Rainbow 七项技术：
 *   1) Double DQN      — online 选动作，target 评估
 *   2) Dueling DQN     — V(s)+A(s,a)-mean(A)
 *   3) PER             — SumTree 优先级采样
 *   4) N-step Returns  — 累积 n 步奖励
 *   5) NoisyNet        — 参数空间探索（来自 NeuralNetV2）
 *   6) Distributional  — 分类输出 + 投影（简化版）
 *   7) Adam/LayerNorm/梯度裁剪（来自 NeuralNetV2）
 *
 * 自动超参调优 + 在线 PBT
 * 统一接口：setMode('game'|'engagement')
 * 持久化：RLPersistence IndexedDB
 * ============================================================ */

import { NeuralNetV2 } from "./nn-advanced.ts";
import { RLPersistence } from "./rl-persistence.ts";
import { symlog } from "./reward-spec.ts";
import { softmaxPolicy } from "./behavior-cloning.ts";


// ==================== 类型 ====================

/** UnifiedRLAgent 构造选项（全部可选，与原 JSDoc 一致） */
interface UnifiedRLOptions {
  mode?: string;
  stateSize?: number;
  nActions?: number;
  hiddenLayers?: number[];
  storageKey?: string;
  lr?: number;
  gamma?: number;
  nStep?: number;
  useNoisy?: boolean;
  noisyStd?: number;
  useDistributional?: boolean;
  nAtoms?: number;
  usePER?: boolean;
  perAlpha?: number;
  perBeta0?: number;
  tau?: number;
  batchSize?: number;
  replayCapacity?: number;
  autoTune?: boolean;
  usePBT?: boolean;
  useSymlog?: boolean;
}

/** UnifiedRLAgent 训练统计（wmImagined/bcLoss 为可选附加字段） */
interface UnifiedAgentStats {
  episodes: number;
  updates: number;
  totalSteps: number;
  wins: number;
  deaths: number;
  totalReward: number;
  avgReward: number;
  lastEpisodeReward: number;
  bestReward: number;
  avgLoss: number;
  bestWinRate: number;
  recentWins: number;
  recentGames: number;
  maxQ: number;
  traceSize: number;
  returnMean: number;
  returnStd: number;
  normalizedReturn: number;
  wmImagined?: number;
  bcLoss?: number;
}

// ==================== 常量 ====================

const DEFAULT_LAYERS = [64, 64];
const PER_CAPACITY = 20000;
const BATCH_SIZE = 64;
const DEFAULT_N_STEP = 3;
const DEFAULT_N_ATOMS = 7;
const V_MIN = -10, V_MAX = 10;
const GAMMA = 0.95, TAU = 0.005;
const LEARNING_RATE = 0.001;
const PER_ALPHA = 0.6, PER_BETA_START = 0.4;
const WINDOW_SIZE = 50;
const PBT_INTERVAL = 10, AUTO_SAVE_INTERVAL = 5;

// ==================== SumTree — PER 优先级二叉树 ====================

class SumTree {
  constructor(capacity) {
    this.capacity = capacity;
    this.tree = new Float64Array(2 * capacity); // 根在 idx=1
    this.data = new Array(capacity);
    this.size = 0;
    this.writeIdx = 0;
    this.maxPriority = 1.0;
  }

  total() { return this.tree[1]; }

  /** 添加数据，初始优先级 maxPriority */
  add(priority, data) {
    const idx = this.writeIdx;
    this.data[idx] = data;
    this._update(idx, priority || this.maxPriority);
    this.writeIdx = (idx + 1) % this.capacity;
    if (this.size < this.capacity) this.size++;
  }

  /** 采样 n 个经验，返回 [{idx, data, weight}] */
  sample(n) {
    const total = this.total();
    const seg = total / n;
    const out = [];
    for (let i = 0; i < n; i++) {
      let s = seg * (i + Math.random());
      let ti = 1;
      while (ti < this.capacity) {
        if (s < this.tree[2 * ti]) ti = 2 * ti;
        else { s -= this.tree[2 * ti]; ti = 2 * ti + 1; }
      }
      const di = ti - this.capacity;
      const prob = this.tree[ti] / total;
      out.push({ idx: di, data: this.data[di], weight: 1.0 / (this.size * prob + 1e-8) });
    }
    return out;
  }

  /** 更新指定索引的优先级 */
  update(dataIdx, priority) {
    this._update(dataIdx, Math.abs(priority) + 1e-5);
  }

  _update(dataIdx, p) {
    let i = dataIdx + this.capacity;
    this.tree[i] = p;
    for (i >>= 1; i >= 1; i >>= 1)
      this.tree[i] = this.tree[2 * i] + this.tree[2 * i + 1];
    if (p > this.maxPriority) this.maxPriority = p;
  }

  // ===== 类型声明（仅类型注解，无运行时副作用） =====
  declare capacity: number;
  declare tree: Float64Array;
  declare data: any[];
  declare size: number;
  declare writeIdx: number;
  declare maxPriority: number;
}

// ==================== NStepBuffer — N-step 缓冲 ====================

class NStepBuffer {
  constructor(n = DEFAULT_N_STEP, gamma = GAMMA) {
    this.n = n;
    this.gamma = gamma;
    this.buf = [];
  }

  /** 加入一步，可能返回 n-step 元组数组 */
  push(state, action, reward, nextState, done) {
    this.buf.push({ state, action, reward });
    const out = [];
    if (done) {
      // 终局：全部转为 n-step
      while (this.buf.length > 0) {
        const t = this._emit(this.buf.length, nextState, true);
        if (t) out.push(t);
      }
      return out.length ? out : null;
    }
    if (this.buf.length >= this.n) {
      out.push(this._emit(this.n, nextState, false));
    }
    return out.length ? out : null;
  }

  reset() { this.buf = []; }

  _emit(k, nextState, done) {
    const first = this.buf.shift();
    const total = Math.min(k, this.buf.length + 1);
    let nRwd = 0, gp = 1;
    for (let i = 0; i < total && i < this.n; i++) {
      nRwd += gp * (i === 0 ? first.reward : this.buf[i - 1].reward);
      gp *= this.gamma;
    }
    return {
      state: first.state, action: first.action, reward: nRwd,
      nextState: k <= this.n ? nextState : this.buf[k - 2].nextState, done,
    };
  }

  // ===== 类型声明（仅类型注解，无运行时副作用） =====
  declare n: number;
  declare gamma: number;
  declare buf: any[];
}

// ==================== UnifiedRLAgent ====================

export class UnifiedRLAgent {
  /**
   * @param {Object} opts
   * @param {string}  [opts.mode='game']           'game'|'engagement'
   * @param {number}  [opts.stateSize=12]          状态维度
   * @param {number}  [opts.nActions=6]            动作数
   * @param {number[]}[opts.hiddenLayers=[64,64]]  隐藏层
   * @param {string}  [opts.storageKey='unified_v1'] 持久化键
   * @param {number}  [opts.lr=0.001]              学习率
   * @param {number}  [opts.gamma=0.95]            折扣因子
   * @param {number}  [opts.nStep=3]               N-step
   * @param {boolean} [opts.useNoisy=true]          NoisyNet
   * @param {number}  [opts.noisyStd=0.1]           NoisyNet 标准差
   * @param {boolean} [opts.useDistributional=true] 分布 RL
   * @param {number}  [opts.nAtoms=7]              原子数
   * @param {boolean} [opts.usePER=true]            PER
   * @param {number}  [opts.perAlpha=0.6]           PER alpha
   * @param {number}  [opts.perBeta0=0.4]           PER beta0
   * @param {number}  [opts.tau=0.005]              软更新 tau
   * @param {number}  [opts.batchSize=64]           批次
   * @param {number}  [opts.replayCapacity=20000]   回放容量
   * @param {boolean} [opts.autoTune=true]          自动调参
   * @param {boolean} [opts.usePBT=true]            在线 PBT
   * @param {boolean} [opts.useSymlog=false]        奖励 symlog 变换（DreamerV3 域无关技巧）
   */
  constructor(opts: UnifiedRLOptions = {}) {
    this.mode = opts.mode ?? 'game';
    this.stateSize = opts.stateSize ?? 12;
    this.nActions = opts.nActions ?? 6;
    this.hiddenLayers = opts.hiddenLayers ?? DEFAULT_LAYERS.slice();
    this.storageKey = opts.storageKey ?? 'unified_v1';
    // P1-2 域无关技巧：奖励 symlog 变换（压缩量纲，跨游戏单配置稳定）
    this.useSymlog = opts.useSymlog === true;

    // 超参
    this.lr = opts.lr ?? LEARNING_RATE;
    this.gamma = opts.gamma ?? GAMMA;
    this.nStep = opts.nStep ?? DEFAULT_N_STEP;
    this.useNoisy = opts.useNoisy !== false;
    this.noisyStd = opts.noisyStd ?? 0.1;
    this.useDistributional = opts.useDistributional !== false;
    this.nAtoms = opts.nAtoms ?? DEFAULT_N_ATOMS;
    this._isDist = this.useDistributional && this.nAtoms > 1;
    this.usePER = opts.usePER !== false;
    this.tau = opts.tau ?? TAU;
    this.batchSize = opts.batchSize ?? BATCH_SIZE;
    this.replayCapacity = opts.replayCapacity ?? PER_CAPACITY;
    this.autoTune = opts.autoTune !== false;
    this.usePBT = opts.usePBT !== false;
    this.perBeta = opts.perBeta0 ?? PER_BETA_START;
    this.perAlpha = opts.perAlpha ?? PER_ALPHA;

    // 分布支持：V_MIN~V_MAX 均匀划分
    if (this._isDist) {
      this._support = new Float64Array(this.nAtoms);
      const d = (V_MAX - V_MIN) / (this.nAtoms - 1);
      for (let i = 0; i < this.nAtoms; i++) this._support[i] = V_MIN + i * d;
    }

    // 神经网络：输出 = nAtoms * (nActions+1) [Dueling+Dist] 或 nActions+1
    const oSize = this._isDist ? this.nAtoms * (this.nActions + 1) : this.nActions + 1;
    const spec = [this.stateSize, ...this.hiddenLayers, oSize];

    this.onlineNet = new NeuralNetV2(spec, {
      lr: this.lr, noisy: this.useNoisy, noisyStd: this.noisyStd,
      seed: this._seedFromKey(this.storageKey),
    }) as any;
    this.targetNet = new NeuralNetV2(spec, {
      lr: this.lr, noisy: false,
      seed: this._seedFromKey(this.storageKey + '_target'),
    }) as any;
    this.targetNet.copyFrom(this.onlineNet);

    // PER + N-step + 统计
    this.sumTree = new SumTree(this.replayCapacity);
    this._nStepBuf = new NStepBuffer(this.nStep, this.gamma);
    this._perBetaStep = 0;

    this.stats = { episodes:0, updates:0, totalSteps:0, wins:0, deaths:0,
      totalReward:0, avgReward:0, lastEpisodeReward:0, bestReward:-Infinity,
      avgLoss:0, bestWinRate:0, recentWins:0, recentGames:0, maxQ:0, traceSize:0,
      // P1-2 回报归一化统计（running mean/std，跨游戏比较与 PBT 用）
      returnMean:0, returnStd:0, normalizedReturn:0 };
    this._recentLosses = [];
    this._recentRewards = [];
    this.trainingCurve = [];
    // P1-2 回报归一化：Welford 在线统计
    this._retCount = 0;
    this._retMean = 0;
    this._retM2 = 0;

    // P2-1b 行为克隆（BC）先验：人类轨迹 → 行为分布对齐（可选用）
    this.bcPrior = null;
    this.bcAlpha = 0;          // BC 损失权重（相对 DQN 学习率的乘子）
    this.bcBatchSize = 16;     // 每次 train 采样的 BC 样本数
    this.bcKL = Infinity;      // 最近一次行为分布 KL 距离（验收指标）

    // P3-1 世界模型（DreamerV3 风格）：想象回放增强样本效率（可选用）
    this.worldModel = null;
    this.wmAlpha = 0;          // 想象样本训练权重（相对 DQN 学习率乘子）
    this.wmImagined = 0;       // 累计注入的想象样本数
    this.wmLoss = 0;           // 世界模型最近 loss（验收指标：可测且下降）
    this._wmSeenSteps = 0;

    // P3-3 RLHF-lite 对比反馈：奖励校准项（可选用）
    this.rlhf = null;
    this.rlhfAlpha = 0;        // shaping 奖励缩放
    this.rlhfShaping = 0;      // 累计 shaping 量

    // PBT
    this._pbtConfigs = [
      { lr:this.lr, gamma:this.gamma, noisyStd:this.noisyStd, avgRwd:0, count:0 },
      { lr:this.lr*1.5, gamma:Math.min(0.99,this.gamma+0.02), noisyStd:this.noisyStd*1.5, avgRwd:0, count:0 },
    ];
    this._currentPbtIdx = 0;
    this._pbtEpisodes = 0;

    // 持久化
    this._persistence = new RLPersistence();
    this._loaded = false;
    this._loadPersisted().catch(() => {});
  }

  // ==================== 核心 API ====================

  /** 选择动作（NoisyNet 隐式探索） */
  chooseAction(stateVec, validActions = null) {
    const allowed = (validActions && validActions.length)
      ? validActions : Array.from({ length: this.nActions }, (_, i) => i);
    const raw = this.onlineNet.predict(stateVec);
    const qValues = this._rawToQ(raw);

    let maxQ = qValues[0];
    for (let i = 1; i < qValues.length; i++) if (qValues[i] > maxQ) maxQ = qValues[i];
    this.stats.maxQ = this.stats.maxQ * 0.99 + maxQ * 0.01;

    let bestA = allowed[0], bestV = qValues[allowed[0]];
    for (let i = 1; i < allowed.length; i++) {
      const a = allowed[i];
      if (qValues[a] > bestV) { bestV = qValues[a]; bestA = a; }
    }
    return { action: bestA, wasRandom: false, qValues: Array.from(qValues) };
  }

  /** 存储经验（先入 N-step 缓冲，满则推入 PER） */
  store(state, action, reward, nextState, done) {
    // P3-3 RLHF-lite：叠加对比反馈校准项（修正手写奖励覆盖不到的行为）
    if (this.rlhf) {
      const shaping = this.rlhf.shaping(state, action, reward);
      reward += this.rlhfAlpha * shaping;
      this.rlhfShaping += shaping;
    }
    // P3-1 世界模型：异步吸收真实经验（想象回放数据源）
    if (this.worldModel) {
      this.worldModel.addExperience(state, action, reward, nextState);
      this._wmSeenSteps++;
    }
    // P1-2 域无关技巧：symlog 压缩奖励量纲（log 尺度下正负对称）
    if (this.useSymlog) reward = symlog(reward);
    const nst = this._nStepBuf.push(state, action, reward, nextState, done);
    if (nst) for (const t of nst) this.sumTree.add(this.sumTree.maxPriority, t);
    if (done) this._nStepBuf.reset();
    this.stats.totalSteps++;
  }

  /** 从 PER 采样训练，返回平均 loss */
  train() {
    if (this.sumTree.size < this.batchSize * 2) return 0;

    // PER β 退火
    this._perBetaStep++;
    this.perBeta = Math.min(1.0, PER_BETA_START + this._perBetaStep * 1e-5);

    const samples = this.sumTree.sample(this.batchSize);
    let maxW = 0;
    for (const s of samples) if (s.weight > maxW) maxW = s.weight;
    const invMaxW = maxW > 0 ? 1.0 / maxW : 1.0;

    const inputs = [], targets = [], indices = [];
    const tdErrors = new Float64Array(samples.length);

    for (let si = 0; si < samples.length; si++) {
      const s = samples[si];
      const { state, action, reward, nextState, done } = s.data;
      indices.push(s.idx);

      if (this._isDist) {
        // Distributional 模式：用 expected Q 做 Double DQN
        const nqOnline = this._rawToQ(this.onlineNet.predict(nextState));
        let bestA = 0, bestV = nqOnline[0];
        for (let j = 1; j < this.nActions; j++) if (nqOnline[j] > bestV) { bestV = nqOnline[j]; bestA = j; }
        const tDist = this._getDistForAction(this.targetNet.predict(nextState), bestA);
        const proj = done ? (() => { const p = new Float64Array(this.nAtoms); p[this._findSupportIndex(reward)] = 1.0; return p; })()
          : this._projectDistribution(tDist, reward);
        let targetQ = 0;
        for (let i = 0; i < this.nAtoms; i++) targetQ += proj[i] * this._support[i];
        const curQ = this._rawToQ(this.onlineNet.predict(state));
        tdErrors[si] = targetQ - curQ[action];
        const qArr = Array.from(curQ); qArr[action] = targetQ;
        inputs.push(state); targets.push(this._qToRaw(qArr));
      } else {
        // 标准 Dueling Double DQN
        let targetQ = reward;
        if (!done) {
          const nqOnline = this._rawToQ(this.onlineNet.predict(nextState));
          let bestA = 0, bestV = nqOnline[0];
          for (let j = 1; j < this.nActions; j++) if (nqOnline[j] > bestV) { bestV = nqOnline[j]; bestA = j; }
          targetQ = reward + this.gamma * this._rawToQ(this.targetNet.predict(nextState))[bestA];
        }
        const curQ = this._rawToQ(this.onlineNet.predict(state));
        tdErrors[si] = targetQ - curQ[action];
        const qArr = Array.from(curQ); qArr[action] = targetQ;
        inputs.push(state); targets.push(this._qToRaw(qArr));
      }
    }

    const loss = this.onlineNet.trainBatch(inputs, targets, this.lr);
    this.stats.updates++;
    this.stats.avgLoss = this.stats.avgLoss * 0.99 + loss * 0.01;
    this._recentLosses.push(loss);
    if (this._recentLosses.length > WINDOW_SIZE) this._recentLosses.shift();

    // 更新 PER 优先级
    for (let i = 0; i < indices.length; i++)
      this.sumTree.update(indices[i], Math.pow(Math.abs(tdErrors[i]) + 1e-5, this.perAlpha));

    // P2-1b 行为克隆：人类先验样本并入训练（提升人类动作 Q 的 DQN 式目标）。
    // 不改变 NeuralNetV2 接口：对每个人类样本 (s, a_human)，
    // target = 当前 Q 向量但把 Q(a_human) 抬到 maxQ + margin，使策略倾向人类动作。
    if (this.bcPrior && this.bcPrior.pairCount > 0) {
      const bcInputs = [], bcTargets = [];
      for (let i = 0; i < this.bcBatchSize; i++) {
        const p = this.bcPrior.sampleStateAction();
        if (!p) break;
        const curQ = this._rawToQ(this.onlineNet.predict(p.s));
        const qArr = Array.from(curQ);
        let maxQ = qArr[0];
        for (let j = 1; j < qArr.length; j++) if (qArr[j] > maxQ) maxQ = qArr[j];
        qArr[p.a] = maxQ + 1.0;   // margin=1.0：人类动作须显著高于当前最优
        bcInputs.push(p.s);
        bcTargets.push(this._qToRaw(qArr));
      }
      if (bcInputs.length) {
        const bcLoss = this.onlineNet.trainBatch(bcInputs, bcTargets, this.lr * this.bcAlpha);
        this.stats.bcLoss = this.stats.bcLoss
          ? this.stats.bcLoss * 0.99 + bcLoss * 0.01 : bcLoss;
      }
    }

    // P3-1 世界模型：想象回放增强样本效率。
    // 用世界模型 rollout 出的想象样本训练策略（同样的 DQN 式目标），
    // 使同一批真实经验能"多走几步" → 样本效率提升。
    if (this.worldModel && this._wmSeenSteps >= 8) {
      const imagineCount = this.wmImagineSteps ?? 8;
      const imagined = this._imagineSamples(imagineCount);
      if (imagined.length >= 2) {
        const wmInputs = [], wmTargets = [];
        for (const im of imagined) {
          let targetQ = im.r;
          if (!im.done) {
            const nqOnline = this._rawToQ(this.onlineNet.predict(im.s2));
            let bestA = 0, bestV = nqOnline[0];
            for (let j = 1; j < this.nActions; j++) if (nqOnline[j] > bestV) { bestV = nqOnline[j]; bestA = j; }
            targetQ = im.r + this.gamma * this._rawToQ(this.targetNet.predict(im.s2))[bestA];
          }
          const curQ = this._rawToQ(this.onlineNet.predict(im.s));
          const qArr = Array.from(curQ); qArr[im.a] = targetQ;
          wmInputs.push(im.s);
          wmTargets.push(this._qToRaw(qArr));
        }
        if (wmInputs.length) {
          this.onlineNet.trainBatch(wmInputs, wmTargets, this.lr * this.wmAlpha);
          this.wmImagined += wmInputs.length;
          this.stats.wmImagined = this.wmImagined;
        }
      }
      // 世界模型自身也同步训练（吸收真实经验，loss 可测且下降）
      this.trainWorldModel(32);
    }

    this.targetNet.softUpdateFrom(this.onlineNet, this.tau);
    if (this.autoTune) this._autoTune();
    this.stats.traceSize = this.sumTree.size;
    return loss;
  }

  /** 局终结算 */
  endEpisode(episodeReward = 0, result: { win?: boolean } = {}) {
    this.stats.episodes++;
    this.stats.lastEpisodeReward = episodeReward;
    this.stats.totalReward += episodeReward;
    this.stats.avgReward = this.stats.episodes > 1
      ? this.stats.avgReward + (episodeReward - this.stats.avgReward) / this.stats.episodes
      : episodeReward;

    // P1-2 回报归一化（Welford 在线算法）：为 PBT 自动调参与跨游戏比较提供尺度无关指标
    this._retCount++;
    const delta = episodeReward - this._retMean;
    this._retMean += delta / this._retCount;
    const delta2 = episodeReward - this._retMean;
    this._retM2 += delta * delta2;
    this.stats.returnMean = this._retMean;
    this.stats.returnStd = this._retCount > 1 ? Math.sqrt(this._retM2 / (this._retCount - 1)) : 0;
    this.stats.normalizedReturn = this.stats.returnStd > 1e-6
      ? (episodeReward - this._retMean) / this.stats.returnStd : 0;

    if (episodeReward > this.stats.bestReward) this.stats.bestReward = episodeReward;
    if (result.win) this.stats.wins++; else this.stats.deaths++;

    this.stats.recentGames++;
    if (result.win) this.stats.recentWins++;
    if (this.stats.recentGames > WINDOW_SIZE) {
      this.stats.recentWins = Math.max(0, this.stats.recentWins - (this.stats.recentWins / this.stats.recentGames > 0.5 ? 1 : 0));
      this.stats.recentGames = WINDOW_SIZE;
    }

    this.trainingCurve.push(episodeReward);
    if (this.trainingCurve.length > 100) this.trainingCurve.shift();
    this._recentRewards.push(episodeReward);
    if (this._recentRewards.length > WINDOW_SIZE) this._recentRewards.shift();

    const wr = this.stats.recentGames > 10 ? this.stats.recentWins / this.stats.recentGames : 0;
    if (wr > this.stats.bestWinRate && this.stats.episodes >= 20) {
      this.stats.bestWinRate = wr;
      this._saveBest();
    }

    this._pbtEpisodes++;
    if (this.usePBT && this._pbtEpisodes % PBT_INTERVAL === 0) this._pbtCompare();
    if (this.stats.episodes % AUTO_SAVE_INTERVAL === 0) this.flush();
  }

  /** 获取 Q 值（可视化用） */
  getQValues(stateVec) {
    return Array.from(this._rawToQ(this.onlineNet.predict(stateVec)));
  }

  // ==================== P2-1b 行为克隆（BC）先验 ====================

  /**
   * 挂接人类行为先验（BC 混入训练）。
   * @param {Object} prior - BehaviorCloningPrior 实例
   * @param {number} [alpha=0.1] BC 权重（相对学习率乘子）
   * @param {number} [batchSize=16] 每次 train 的 BC 样本数
   */
  attachBCPrior(prior, alpha = 0.1, batchSize = 16) {
    this.bcPrior = prior;
    this.bcAlpha = alpha;
    this.bcBatchSize = batchSize;
    console.log(`[UnifiedRL] BC 先验已挂接: ${prior.pairCount} 对人类样本, alpha=${alpha}`);
    return this;
  }

  /**
   * 测量行为分布距离（KL(人类先验 || 当前策略)），并记录历史。
   * 验收标准：该距离可测且随训练下降。
   * @returns {number} 平均 KL；无先验/无数据时返回 Infinity
   */
  measureBCKL() {
    if (!this.bcPrior || !this.bcPrior.pairCount || !this.sumTree.size) return this.bcKL;
    const states = [];
    const n = Math.min(32, this.sumTree.size);
    for (let i = 0; i < n; i++) states.push(this.sumTree.sample(1)[0].data.state);
    const policyFn = (s) => softmaxPolicy(this._rawToQ(this.onlineNet.predict(s)), 1.0);
    const kl = this.bcPrior.measureKL(policyFn, states);
    this.bcKL = this.bcPrior.recordKL(kl);
    return this.bcKL;
  }

  // ==================== P3-1 世界模型（想象回放） ====================

  /**
   * 挂接世界模型：train() 时吸收真实经验 + 注入想象样本。
   * @param {Object} wm - WorldModel 实例
   * @param {number} [alpha=0.5] 想象样本训练权重（相对 DQN 学习率乘子）
   * @param {number} [imagineSteps=8] 每次 train 的想象 rollout 步数
   */
  attachWorldModel(wm, alpha = 0.5, imagineSteps = 8) {
    this.worldModel = wm;
    this.wmAlpha = alpha;
    this.wmImagineSteps = imagineSteps;
    console.log(`[UnifiedRL] 世界模型已挂接: stateSize=${wm.stateSize}, alpha=${alpha}`);
    return this;
  }

  /**
   * 训练世界模型（吸收真实经验）。验收指标：wmLoss 可测且随训练下降。
   * @param {number} [batchSize=32]
   * @returns {number} 世界模型最近 loss
   */
  trainWorldModel(batchSize = 32) {
    if (!this.worldModel) return 0;
    const loss = this.worldModel.train(batchSize);
    this.wmLoss = this.worldModel.getLoss();
    return loss;
  }

  /** 从世界模型取样想象回放（供 train() 内部调用） */
  _imagineSamples(count) {
    if (!this.worldModel || !this.sumTree.size) return [];
    const seeds = [];
    const n = Math.min(4, this.sumTree.size);
    for (let i = 0; i < n; i++) seeds.push(this.sumTree.sample(1)[0].data.state);
    const horizon = Math.max(1, Math.ceil(count / Math.max(1, seeds.length)));
    const policyFn = (s) => this.chooseAction(s).action;
    return this.worldModel.imagineRollout(seeds, policyFn, horizon);
  }

  // ==================== P3-3 RLHF-lite 对比反馈 ====================

  /**
   * 挂接 RLHF 对比反馈校准器：store() 时叠加 shaping 奖励项。
   * @param {Object} rlhf - RLHFLite 实例
   * @param {number} [alpha=0.3] shaping 奖励缩放
   */
  attachRLHF(rlhf, alpha = 0.3) {
    this.rlhf = rlhf;
    this.rlhfAlpha = alpha;
    console.log(`[UnifiedRL] RLHF 已挂接: stateSize=${rlhf.stateSize}, alpha=${alpha}`);
    return this;
  }

  /** 当前 shaping 项（累计量，验收指标） */
  getRLHFStats() {
    return { shaping: this.rlhfShaping, pairs: this.rlhf ? this.rlhf.stats.pairs : 0, sep: this.rlhf ? this.rlhf.stats.sep : 0 };
  }

  /** 完整重置 */
  reset() {
    const oSize = this._isDist ? this.nAtoms * (this.nActions + 1) : this.nActions + 1;
    const spec = [this.stateSize, ...this.hiddenLayers, oSize];
    this.onlineNet = new NeuralNetV2(spec, { lr:this.lr, noisy:this.useNoisy, noisyStd:this.noisyStd, seed:this._seedFromKey(this.storageKey) }) as any;
    this.targetNet = new NeuralNetV2(spec, { lr:this.lr, noisy:false, seed:this._seedFromKey(this.storageKey+'_target') }) as any;
    this.targetNet.copyFrom(this.onlineNet);
    this.sumTree = new SumTree(this.replayCapacity);
    this._nStepBuf = new NStepBuffer(this.nStep, this.gamma);
    this._recentLosses = [];
    this._recentRewards = [];
    this.trainingCurve = [];
    this._perBetaStep = 0;
    this.perBeta = PER_BETA_START;
    this._pbtEpisodes = 0;
    this.stats = { episodes:0, updates:0, totalSteps:0, wins:0, deaths:0,
      totalReward:0, avgReward:0, lastEpisodeReward:0, bestReward:-Infinity,
      avgLoss:0, bestWinRate:0, recentWins:0, recentGames:0, maxQ:0, traceSize:0,
      returnMean:0, returnStd:0, normalizedReturn:0, wmImagined:0, bcLoss:0 };
    this._wmSeenSteps = 0;
    this.wmImagined = 0;
    this.wmLoss = 0;
    this.rlhfShaping = 0;
    this.flush();
  }

  /** 恢复最佳策略快照 */
  async restoreBest() {
    try {
      const data = await this._persistence.load(this.storageKey + '_best');
      if (!data) return false;
      this.onlineNet.fromJSON(data.net);
      this.targetNet.copyFrom(this.onlineNet);
      if (data.stats) {
        this.stats.bestWinRate = data.stats.bestWinRate ?? 0;
        if (data.stats.bestReward != null) this.stats.bestReward = data.stats.bestReward;
      }
      this.flush();
      return true;
    } catch (e) { console.warn('[UnifiedRL] restoreBest:', e.message); return false; }
  }

  /** 立即保存 */
  async flush() {
    try {
      await this._persistence.save(this.storageKey + '_net', this.onlineNet.toJSON());
      await this._persistence.save(this.storageKey + '_stats', {
        stats: this.stats, hyper: { lr:this.lr, gamma:this.gamma, noisyStd:this.noisyStd, perBeta:this.perBeta },
        perBetaStep: this._perBetaStep, pbtEpisodes: this._pbtEpisodes, trainingCurve: this.trainingCurve,
      });
    } catch (e) { console.warn('[UnifiedRL] flush:', e.message); }
  }

  /** 切换模式（game / engagement） */
  setMode(mode, stateSize, nActions) {
    this.mode = mode;
    this.stateSize = stateSize ?? this.stateSize;
    this.nActions = nActions ?? this.nActions;
    const oSize = this._isDist ? this.nAtoms * (this.nActions + 1) : this.nActions + 1;
    const spec = [this.stateSize, ...this.hiddenLayers, oSize];
    const oldOnline = this.onlineNet;
    this.onlineNet = new NeuralNetV2(spec, { lr:this.lr, noisy:this.useNoisy, noisyStd:this.noisyStd, seed:this._seedFromKey(this.storageKey+'_'+mode) }) as any;
    this.targetNet = new NeuralNetV2(spec, { lr:this.lr, noisy:false, seed:this._seedFromKey(this.storageKey+'_'+mode+'_target') }) as any;
    try {
      if (oldOnline && oldOnline.layers[0] === this.stateSize &&
          oldOnline.layers[oldOnline.nLayers] === oSize &&
          oldOnline.nLayers === this.onlineNet.nLayers)
        this.onlineNet.copyFrom(oldOnline);
    } catch (_) {}
    this.targetNet.copyFrom(this.onlineNet);
    this._nStepBuf = new NStepBuffer(this.nStep, this.gamma);
  }

  /** 获取当前统计副本 */
  getStats() { return { ...this.stats }; }

  /** 完整导出 */
  exportData() {
    return {
      version: 2, mode: this.mode, stateSize: this.stateSize, nActions: this.nActions,
      hiddenLayers: this.hiddenLayers,
      hyper: { lr:this.lr, gamma:this.gamma, nStep:this.nStep, useNoisy:this.useNoisy,
        noisyStd:this.noisyStd, useDistributional:this.useDistributional, nAtoms:this.nAtoms,
        perAlpha:this.perAlpha, perBeta:this.perBeta },
      stats: this.stats, trainingCurve: this.trainingCurve, net: this.onlineNet.toJSON(),
    };
  }

  /** 完整导入 */
  importData(jsonStr) {
    try {
      const data = typeof jsonStr === 'string' ? JSON.parse(jsonStr) : jsonStr;
      if (data.version !== 2) return false;
      this.mode = data.mode ?? this.mode;
      this.stateSize = data.stateSize ?? this.stateSize;
      this.nActions = data.nActions ?? this.nActions;
      if (data.hiddenLayers) this.hiddenLayers = data.hiddenLayers;
      if (data.hyper) {
        this.lr = data.hyper.lr ?? this.lr; this.gamma = data.hyper.gamma ?? this.gamma;
        this.nStep = data.hyper.nStep ?? this.nStep;
        this.useNoisy = data.hyper.useNoisy ?? this.useNoisy; this.noisyStd = data.hyper.noisyStd ?? this.noisyStd;
        this.useDistributional = data.hyper.useDistributional ?? this.useDistributional;
        this.nAtoms = data.hyper.nAtoms ?? this.nAtoms; this._isDist = this.useDistributional && this.nAtoms > 1;
        this.perAlpha = data.hyper.perAlpha ?? this.perAlpha;
      }
      if (data.stats) Object.assign(this.stats, data.stats);
      if (data.trainingCurve) this.trainingCurve = data.trainingCurve;

      const oSize = this._isDist ? this.nAtoms * (this.nActions + 1) : this.nActions + 1;
      const spec = [this.stateSize, ...this.hiddenLayers, oSize];
      this.onlineNet = new NeuralNetV2(spec, { lr:this.lr, noisy:this.useNoisy, noisyStd:this.noisyStd }) as any;
      if (data.net) this.onlineNet.fromJSON(data.net);
      this.targetNet = new NeuralNetV2(spec, { lr:this.lr, noisy:false }) as any;
      this.targetNet.copyFrom(this.onlineNet);
      return true;
    } catch (e) { console.warn('[UnifiedRL] import:', e.message); return false; }
  }

  // ==================== 内部方法 ====================

  /** 网络原始输出 → 各动作 Q 值（支持标准 Dueling 和分布模式） */
  _rawToQ(raw) {
    if (this._isDist) return this._distributionalToQ(raw);
    const n = this.nActions, q = new Float64Array(n);
    const v = raw[n];
    let mA = 0;
    for (let i = 0; i < n; i++) mA += raw[i];
    mA /= n;
    for (let i = 0; i < n; i++) q[i] = v + raw[i] - mA;
    return q;
  }

  /** Q 值 → 网络原始输出（Dueling 格式） */
  _qToRaw(q) {
    const n = this.nActions, raw = new Float64Array(n + 1);
    let mQ = 0;
    for (let i = 0; i < n; i++) mQ += q[i];
    mQ /= n; raw[n] = mQ;
    for (let i = 0; i < n; i++) raw[i] = q[i] - mQ;
    return raw;
  }

  /** 分布模式：原始输出 → 各动作期望 Q */
  _distributionalToQ(raw) {
    const n = this.nActions, nA = this.nAtoms;
    const q = new Float64Array(n);
    const vStart = n * nA;
    const vA = raw.subarray ? raw.subarray(vStart, vStart + nA) : raw.slice(vStart, vStart + nA);
    const mA = new Float64Array(nA);
    for (let a = 0; a < n; a++) for (let i = 0; i < nA; i++) mA[i] += raw[a * nA + i];
    for (let i = 0; i < nA; i++) mA[i] /= n;

    for (let a = 0; a < n; a++) {
      const base = a * nA;
      let maxL = -Infinity;
      const logits = new Float64Array(nA);
      for (let i = 0; i < nA; i++) {
        logits[i] = vA[i] + raw[base + i] - mA[i];
        if (logits[i] > maxL) maxL = logits[i];
      }
      let sumE = 0;
      for (let i = 0; i < nA; i++) { logits[i] = Math.exp(logits[i] - maxL); sumE += logits[i]; }
      const inv = 1.0 / (sumE + 1e-10);
      let eQ = 0;
      for (let i = 0; i < nA; i++) eQ += logits[i] * inv * this._support[i];
      q[a] = eQ;
    }
    return q;
  }

  /** 获取指定动作的分布概率 */
  _getDistForAction(raw, action) {
    const n = this.nActions, nA = this.nAtoms;
    const vStart = n * nA;
    const vA = raw.subarray ? raw.subarray(vStart, vStart + nA) : raw.slice(vStart, vStart + nA);
    const mA = new Float64Array(nA);
    for (let a = 0; a < n; a++) for (let i = 0; i < nA; i++) mA[i] += raw[a * nA + i];
    for (let i = 0; i < nA; i++) mA[i] /= n;

    const base = action * nA;
    let maxL = -Infinity;
    const logits = new Float64Array(nA);
    for (let i = 0; i < nA; i++) {
      logits[i] = vA[i] + raw[base + i] - mA[i];
      if (logits[i] > maxL) maxL = logits[i];
    }
    let sumE = 0;
    for (let i = 0; i < nA; i++) { logits[i] = Math.exp(logits[i] - maxL); sumE += logits[i]; }
    const inv = 1.0 / (sumE + 1e-10);
    const probs = new Float64Array(nA);
    for (let i = 0; i < nA; i++) probs[i] = logits[i] * inv;
    return probs;
  }

  /** Bellman 投影：将 r + γ * Z_next 投影到原子支持上 */
  _projectDistribution(nextDist, reward) {
    const nA = this.nAtoms;
    const proj = new Float64Array(nA);
    const d = (V_MAX - V_MIN) / (nA - 1);
    for (let i = 0; i < nA; i++) {
      let idx = Math.round((reward + this.gamma * this._support[i] - V_MIN) / d);
      idx = Math.max(0, Math.min(nA - 1, idx));
      proj[idx] += nextDist[i];
    }
    let sum = 0; for (let i = 0; i < nA; i++) sum += proj[i];
    if (sum > 0) { const inv = 1.0 / sum; for (let i = 0; i < nA; i++) proj[i] *= inv; }
    return proj;
  }

  _findSupportIndex(value) {
    const clamped = Math.max(V_MIN, Math.min(V_MAX, value));
    return Math.round((clamped - V_MIN) / ((V_MAX - V_MIN) / (this.nAtoms - 1)));
  }

  // ———— 自动超参调优 ————

  _autoTune() {
    // 根据 loss 振荡调整 lr
    if (this._recentLosses.length >= 20) {
      const r = this._recentLosses.slice(-20);
      let m = 0; for (const v of r) m += v; m /= r.length;
      let v = 0; for (const x of r) v += (x - m) ** 2; v /= r.length;
      const cv = Math.sqrt(v) / (m + 1e-8);
      if (cv > 0.5) this.lr *= 0.95;
      else if (cv < 0.1 && this.stats.avgLoss < 0.5) this.lr *= 1.01;
      this.lr = Math.max(1e-5, Math.min(0.01, this.lr));
    }
    // 根据 reward 停滞调整噪声
    if (this._recentRewards.length >= 20) {
      const r = this._recentRewards.slice(-20);
      let m = 0; for (const v of r) m += v; m /= r.length;
      if (this._recentRewards.length >= 40 && this.stats.episodes > 30) {
        const p = this._recentRewards.slice(-40, -20);
        let pm = 0; for (const v of p) pm += v; pm /= p.length;
        this.noisyStd = m <= pm * 1.05
          ? Math.min(0.5, this.noisyStd * 1.05)
          : Math.max(0.01, this.noisyStd * 0.995);
      }
    }
  }

  // ———— 在线 PBT ————

  _pbtCompare() {
    const cur = this._pbtConfigs[this._currentPbtIdx];
    const other = this._pbtConfigs[1 - this._currentPbtIdx];
    const recentAvg = this._recentRewards.length >= 10
      ? this._recentRewards.slice(-10).reduce((a, b) => a + b, 0) / 10 : -Infinity;
    cur.avgRwd = cur.avgRwd * 0.9 + recentAvg * 0.1;
    cur.count++;
    if (cur.count >= 2 && other.count >= 2 && other.avgRwd > cur.avgRwd * 1.1) {
      this._currentPbtIdx = 1 - this._currentPbtIdx;
      this.lr = other.lr; this.gamma = other.gamma; this.noisyStd = other.noisyStd;
      cur.count = 0; other.count = 0; cur.avgRwd = 0; other.avgRwd = 0;
    }
  }

  // ———— 持久化内部 ————

  async _loadPersisted() {
    try {
      const netData = await this._persistence.load(this.storageKey + '_net');
      if (netData) { this.onlineNet.fromJSON(netData); this.targetNet.copyFrom(this.onlineNet); }
      const statsData = await this._persistence.load(this.storageKey + '_stats');
      if (statsData) {
        if (statsData.stats) Object.assign(this.stats, statsData.stats);
        if (statsData.hyper) {
          this.lr = statsData.hyper.lr ?? this.lr; this.gamma = statsData.hyper.gamma ?? this.gamma;
          this.noisyStd = statsData.hyper.noisyStd ?? this.noisyStd; this.perBeta = statsData.hyper.perBeta ?? this.perBeta;
        }
        if (statsData.perBetaStep != null) this._perBetaStep = statsData.perBetaStep;
        if (statsData.pbtEpisodes != null) this._pbtEpisodes = statsData.pbtEpisodes;
        if (statsData.trainingCurve) this.trainingCurve = statsData.trainingCurve;
      }
      this._loaded = true;
      console.log('[UnifiedRL] 加载完成, 已训练', this.stats.episodes, '局');
    } catch (e) { console.warn('[UnifiedRL] 加载失败:', e.message); }
  }

  async _saveBest() {
    try {
      await this._persistence.save(this.storageKey + '_best', {
        net: this.onlineNet.toJSON(),
        stats: { bestWinRate: this.stats.bestWinRate, bestReward: this.stats.bestReward },
      });
    } catch (e) { console.warn('[UnifiedRL] saveBest:', e.message); }
  }

  _seedFromKey(key) {
    let h = 0;
    for (let i = 0; i < key.length; i++) { h = ((h << 5) - h) + key.charCodeAt(i); h |= 0; }
    return Math.abs(h) % 2147483647 + 1;
  }

  // ===== 类型声明（仅类型注解，无运行时副作用） =====
  declare mode: string;
  declare stateSize: number;
  declare nActions: number;
  declare hiddenLayers: number[];
  declare storageKey: string;
  declare useSymlog: boolean;
  declare lr: number;
  declare gamma: number;
  declare nStep: number;
  declare useNoisy: boolean;
  declare noisyStd: number;
  declare useDistributional: boolean;
  declare nAtoms: number;
  declare _isDist: boolean;
  declare usePER: boolean;
  declare tau: number;
  declare batchSize: number;
  declare replayCapacity: number;
  declare autoTune: boolean;
  declare usePBT: boolean;
  declare perBeta: number;
  declare perAlpha: number;
  declare _support: Float64Array;
  declare onlineNet: NeuralNetV2;
  declare targetNet: NeuralNetV2;
  declare sumTree: SumTree;
  declare _nStepBuf: NStepBuffer;
  declare _perBetaStep: number;
  declare stats: UnifiedAgentStats;
  declare _recentLosses: number[];
  declare _recentRewards: number[];
  declare trainingCurve: number[];
  declare _retCount: number;
  declare _retMean: number;
  declare _retM2: number;
  declare bcPrior: any;
  declare bcAlpha: number;
  declare bcBatchSize: number;
  declare bcKL: number;
  declare worldModel: any;
  declare wmAlpha: number;
  declare wmImagined: number;
  declare wmLoss: number;
  declare wmImagineSteps: number | undefined;
  declare _wmSeenSteps: number;
  declare rlhf: any;
  declare rlhfAlpha: number;
  declare rlhfShaping: number;
  declare _pbtConfigs: Array<{ lr: number; gamma: number; noisyStd: number; avgRwd: number; count: number }>;
  declare _currentPbtIdx: number;
  declare _pbtEpisodes: number;
  declare _persistence: any;
  declare _loaded: boolean;
}

export default UnifiedRLAgent;