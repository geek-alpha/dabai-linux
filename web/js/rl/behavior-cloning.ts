/* ============================================================
 * BehaviorCloningPrior — 人类行为先验（P2-1b，IDM 风格轻量版）
 *
 * 目标（对应方案报告 P2-1）：
 * - 从人类轨迹构建"状态→动作"先验分布（状态离散桶 + 频率统计）
 * - 作为行为克隆（BC）先验混入 DQN 训练：AI 行为向人类分布对齐
 * - 提供行为分布距离度量（KL），验收标准："距离可测且下降"
 *
 * 设计说明：
 * - 状态桶：对观察向量逐维量化（bucketBits 位）组合成字符串键，
 *   近邻状态共享一个动作分布（泛化 + 稀疏缓解）
 * - 动作映射：人类轨迹记录的是世界方向输入向量，通过 actionSpec 的
 *   dir 向量（或语义名推断轴向）映射为离散动作索引
 * - 平滑：加性平滑（smoothing）避免零概率；无匹配桶时回退均匀分布
 * - KL 测量：softmax(Q) 作为策略分布，与先验分布算 KL(prior || agent)
 *
 * 用法：
 *   const prior = new BehaviorCloningPrior({ stateSize: 13, nActions: 4 });
 *   for (const traj of trajectories) prior.addTrajectory(traj, game.getActionSpec());
 *   agent.attachBCPrior(prior, 0.1);          // 混入训练
 *   const kl = agent.measureBCKL();           // 行为分布距离（可测、监控下降）
 * ============================================================ */

/** 状态桶量化位数（每维 2^bits 个桶） */
const DEFAULT_BUCKET_BITS = 3;
/** 加性平滑系数 */
const DEFAULT_SMOOTHING = 0.1;
/** (s,a) 采样池上限（BC batch 采样用） */
const PAIR_POOL_MAX = 5000;
/** 输入向量与动作方向匹配的余弦阈值（低于则视为不匹配） */
const MATCH_COS_THRESHOLD = 0.3;

/**
 * 状态向量 → 离散桶 key。
 * 每维量化到 [0, 2^bits) 再转 36 进制拼接，长度紧凑。
 * @param {ArrayLike<number>} stateVec
 * @param {number} bits
 * @returns {string}
 */
export function bucketKey(stateVec: ArrayLike<number>, bits: number = DEFAULT_BUCKET_BITS): string {
  const levels = 2 ** bits;
  let k = '';
  for (let i = 0; i < stateVec.length; i++) {
    const v = Math.max(0, Math.min(0.999999, stateVec[i]));
    k += ((v * levels) | 0).toString(36);
  }
  return k;
}

/**
 * 世界方向输入向量 → 离散动作索引。
 * 优先用 actionSpec 的 dir 向量做余弦匹配；无 dir 时按语义名推断轴向。
 * @param {{x:number, z:number}} inputVec 归一化世界方向
 * @param {Array<{id:number, name:string, dir?:number[]}>} actionSpec
 * @returns {number} 动作索引；无法匹配返回 -1
 */
export function inputVecToAction(inputVec: { x: number; z: number }, actionSpec: Array<{ id: number; name?: string; dir?: number[] }>): number {
  if (!inputVec || !actionSpec || !actionSpec.length) return -1;
  const { x, z } = inputVec;
  const len = Math.hypot(x, z);
  if (len < 1e-4) return -1;
  const nx = x / len, nz = z / len;

  let best = -1, bestCos = -Infinity;
  for (const a of actionSpec) {
    let cos;
    if (a.dir) {
      const [dx, dz] = a.dir;
      cos = nx * dx + nz * dz;
    } else {
      // 语义动作：按名字推断轴向（up/down/left/right）
      const name = String(a.name || '');
      let ax = 0, az = 0;
      if (name.includes('right')) ax = 1;
      else if (name.includes('left')) ax = -1;
      else if (name.includes('up')) az = -1;
      else if (name.includes('down')) az = 1;
      else continue;
      cos = nx * ax + nz * az;
    }
    if (cos > bestCos) { bestCos = cos; best = a.id; }
  }
  return bestCos > MATCH_COS_THRESHOLD ? best : -1;
}

/** softmax 策略分布（DQN Q 值 → 动作概率，BC 距离测量用） */
export function softmaxPolicy(qValues: number[] | Float64Array, temperature: number = 1.0): Float64Array {
  const out = new Float64Array(qValues.length);
  let maxQ = -Infinity;
  for (const v of qValues) if (v > maxQ) maxQ = v;
  let sum = 0;
  for (let i = 0; i < qValues.length; i++) {
    out[i] = Math.exp((qValues[i] - maxQ) / temperature);
    sum += out[i];
  }
  if (sum <= 0 || !Number.isFinite(sum)) { out.fill(1 / qValues.length); return out; }
  for (let i = 0; i < qValues.length; i++) out[i] /= sum;
  return out;
}

export class BehaviorCloningPrior {
  /** 观察维度（信息性） */
  stateSize: number;
  /** 动作数 */
  nActions: number;
  /** 状态桶量化位数 */
  bucketBits: number;
  /** 加性平滑系数 */
  smoothing: number;
  /** bucketKey -> Float64Array(nActions) 动作频次 */
  _buckets: Map<string, Float64Array>;
  /** 总样本数 */
  _total: number;
  /** (s, a) 采样池（BC batch 用） */
  _pairs: { s: number[]; a: number }[];
  /** KL 历史（监控"距离下降"） */
  _klHistory: number[];
  /** 最近一次 KL */
  _lastKL: number;

  /**
   * @param {Object} opts
   * @param {number} [opts.stateSize] 观察维度（信息性）
   * @param {number} opts.nActions 动作数
   * @param {number} [opts.bucketBits] 状态桶量化位数
   * @param {number} [opts.smoothing] 加性平滑
   */
  constructor({ stateSize = 0, nActions = 4, bucketBits = DEFAULT_BUCKET_BITS,
                smoothing = DEFAULT_SMOOTHING }: { stateSize?: number; nActions?: number; bucketBits?: number; smoothing?: number } = {}) {
    this.stateSize = stateSize;
    this.nActions = nActions;
    this.bucketBits = bucketBits;
    this.smoothing = smoothing;
    /** bucketKey -> Float64Array(nActions) 动作频次 */
    this._buckets = new Map();
    /** 总样本数 */
    this._total = 0;
    /** (s, a) 采样池（BC batch 用） */
    this._pairs = [];
    /** KL 历史（监控"距离下降"） */
    this._klHistory = [];
    this._lastKL = Infinity;
  }

  /** 已吸收的有效 (s,a) 对数量 */
  get pairCount(): number { return this._pairs.length; }

  /** KL 历史（评估/仪表用） */
  get klHistory(): number[] { return this._klHistory.slice(); }

  get lastKL(): number { return this._lastKL; }

  /**
   * 吸收一条人类轨迹。
   * @param {{frames: Array<{s: number[]|null, v: {x,z}}>}} traj
   * @param {Array} actionSpec 游戏动作规格
   * @returns {number} 吸收的有效样本数
   */
  addTrajectory(traj: { frames: Array<{ s: number[] | null; v: { x: number; z: number } }> }, actionSpec: any): number {
    if (!traj || !Array.isArray(traj.frames)) return 0;
    let added = 0;
    for (const f of traj.frames) {
      if (!f || !Array.isArray(f.s)) continue;
      const a = inputVecToAction(f.v, actionSpec);
      if (a < 0 || a >= this.nActions) continue;
      const key = bucketKey(f.s, this.bucketBits);
      let counts = this._buckets.get(key);
      if (!counts) { counts = new Float64Array(this.nActions); this._buckets.set(key, counts); }
      counts[a]++;
      this._total++;
      if (this._pairs.length < PAIR_POOL_MAX) this._pairs.push({ s: f.s, a });
      added++;
    }
    return added;
  }

  /**
   * 状态 → 先验动作分布（加性平滑；无匹配桶回退均匀）。
   * @param {ArrayLike<number>} stateVec
   * @returns {Float64Array}
   */
  actionDistribution(stateVec: ArrayLike<number>): Float64Array {
    const out = new Float64Array(this.nActions);
    if (!this._total) { out.fill(1 / this.nActions); return out; }
    const counts = this._buckets.get(bucketKey(stateVec, this.bucketBits));
    if (!counts) { out.fill(1 / this.nActions); return out; }
    let sum = 0;
    for (let i = 0; i < this.nActions; i++) { out[i] = counts[i] + this.smoothing; sum += out[i]; }
    for (let i = 0; i < this.nActions; i++) out[i] /= sum;
    return out;
  }

  /** 随机采样一个人类 (s, a) 对（BC 训练用）；无数据返回 null */
  sampleStateAction(): { s: number[]; a: number } | null {
    if (!this._pairs.length) return null;
    const p = this._pairs[(Math.random() * this._pairs.length) | 0];
    return { s: p.s, a: p.a };
  }

  /**
   * KL(P_human || P_agent) 平均距离。
   * @param {(s: number[]) => ArrayLike<number>} agentPolicyFn 策略分布函数
   * @param {ArrayLike<ArrayLike<number>>} sampleStates 采样状态集
   * @returns {number}
   */
  measureKL(agentPolicyFn: (s: ArrayLike<number>) => ArrayLike<number>, sampleStates: Array<ArrayLike<number>>): number {
    if (!this._total || !sampleStates || !sampleStates.length) return Infinity;
    let kl = 0, n = 0;
    for (const s of sampleStates) {
      const ph = this.actionDistribution(s);
      let pa;
      try { pa = agentPolicyFn(s); } catch (e) { continue; }
      if (!pa || pa.length < this.nActions) continue;
      let k = 0;
      for (let i = 0; i < this.nActions; i++) {
        const p = Math.max(pa[i], 1e-9);
        if (ph[i] > 1e-9) k += ph[i] * Math.log(ph[i] / p);
      }
      kl += k;
      n++;
    }
    return n ? kl / n : Infinity;
  }

  /** 记录一次 KL 并更新 lastKL（由 agent.measureBCKL 调用） */
  recordKL(kl: number): number {
    if (Number.isFinite(kl)) {
      this._lastKL = kl;
      this._klHistory.push(kl);
      if (this._klHistory.length > 200) this._klHistory.shift();
    }
    return this._lastKL;
  }
}

export default BehaviorCloningPrior;