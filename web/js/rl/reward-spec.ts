/* ============================================================
 * 奖励规格工具 —— 统一符号策略 + 域无关归一化（P0-3）
 *
 * 目标：
 * 1. 统一"符号策略"：同一语义的奖励在跨游戏中保持同一符号与量纲习惯
 *    （生存激励 = 正步进，时间代价 = 负步进，重大事件 = 大额正负）
 * 2. 引入 DreamerV3 的 symlog 变换，把大数值量级压缩到稳定范围，
 *    使同一超参在不同奖励量纲下稳定（"单配置跨游戏"的关键技巧）
 * ============================================================ */

/** 统一语义 → 符号习惯（供各游戏声明奖励表时对齐） */
export const REWARD_CONVENTIONS = {
  // 生存/存活激励：正步进（小）
  SURVIVE_STEP: { sign: '+', magnitude: 'small' },
  // 前进/收集小收益：正步进（小）
  PROGRESS_STEP: { sign: '+', magnitude: 'small' },
  // 时间/无效动作代价：负步进（小）
  TIME_PENALTY: { sign: '-', magnitude: 'small' },
  // 关键事件（金币/击杀/推塔/通关）：正大额
  KEY_EVENT_GAIN: { sign: '+', magnitude: 'large' },
  // 严重事件（受伤/掉坑/失败）：负大额
  CRITICAL_LOSS: { sign: '-', magnitude: 'large' },
  // 内在好奇（ICM 预测误差/新颖性）：正小步
  CURIOSITY: { sign: '+', magnitude: 'small' },
};

/**
 * symlog 变换（DreamerV3 域无关技巧）：
 * 接近 0 时近似线性，远离 0 时近似对数，压缩量纲。
 * symlog(x) = sign(x) * ln(1 + |x|)
 */
export function symlog(x) {
  const v = Number(x) || 0;
  return Math.sign(v) * Math.log1p(Math.abs(v));
}

/** symlog 逆变换 */
export function symexp(x) {
  const v = Number(x) || 0;
  return Math.sign(v) * (Math.expm1(Math.abs(v)));
}

/** 两段式归一化：先 symlog 压缩量纲，再线性缩放到 [0,1] */
export function normalizeReward(x, scale = 5.0) {
  if (!scale || scale <= 0) return symlog(x);
  return Math.max(-1, Math.min(1, symlog(x) / Math.log1p(scale)));
}

/** 奖励裁剪（防极端值污染经验） */
export function clipReward(x, min = -1, max = 1) {
  return Math.max(min, Math.min(max, x));
}

/** 奖励规格统一工具：给定语义 + 数值，返回符合符号习惯的奖励 */
export function rewardValue(convention, magnitude, rawValue = 1) {
  const { sign, magnitude: mag } = REWARD_CONVENTIONS[convention] || REWARD_CONVENTIONS.PROGRESS_STEP;
  const scale = mag === 'large' ? 10 : 1;
  const signed = sign === '-' ? -Math.abs(rawValue) : Math.abs(rawValue);
  return signed * scale * (magnitude === 'raw' ? 1 : 1);
}

export default { REWARD_CONVENTIONS, symlog, symexp, normalizeReward, clipReward, rewardValue };