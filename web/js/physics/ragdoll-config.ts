/* ============================================================
 * 主动布娃娃参数表（active ragdoll）
 * ------------------------------------------------------------
 * 只放数据与纯函数：不 import three / rapier，方便 node 侧仿真
 * 校验脚本（tools/ragdoll_sim_check.mjs）复用同一份参数——
 * 参数改了浏览器与仿真同时变，不会出现「调参调的是另一套数」。
 *
 * 尺寸以 1.7m / 71kg 人体为基准，运行时按模型实际身高等比缩放。
 * ============================================================ */

/** 单段身体段定义 */
export interface SegmentDef {
  /** VRM humanoid 骨骼名（用 raw 骨骼节点，世界位姿即物理体位姿） */
  bone: string;
  /** 父段骨骼名；null = 根段（髋） */
  parent: string | null;
  /** 用于量段长的子骨骼名；null = 固定长度段（如头） */
  child: string | null;
  /** 胶囊半径（米，1.7m 基准） */
  radius: number;
  /** 质量（kg，1.7m 基准） */
  mass: number;
  /** 角 PD 比例增益（N·m/rad） */
  kp: number;
  /** 角 PD 微分增益（N·m·s/rad） */
  kd: number;
  /** 相对父段的软限位：偏离静息相对姿态超过该角度（度）就产生回正扭矩 */
  maxDeviation: number;
  /** 球形碰撞体（头）：沿 +Y 的偏移（米） */
  ballOffsetY?: number;
  /** 无子骨骼时的段长（米） */
  fixedLen?: number;
}

export const HEIGHT_REF = 1.7;

/* 15 段链：髋 → 脊 → 胸 → 颈 → 头 + 双臂 + 双腿。
 * 肩、手、脚趾不单独建体（VRM 里这些骨骼的位移量小，建体只增开销不增观感）。 */
export const SEGMENTS: SegmentDef[] = [
  { bone: 'hips', parent: null, child: 'spine', radius: 0.105, mass: 12, kp: 1200, kd: 80, maxDeviation: 100 },
  { bone: 'spine', parent: 'hips', child: 'chest', radius: 0.11, mass: 8, kp: 900, kd: 60, maxDeviation: 40 },
  { bone: 'chest', parent: 'spine', child: 'neck', radius: 0.115, mass: 10, kp: 900, kd: 60, maxDeviation: 40 },
  { bone: 'neck', parent: 'chest', child: 'head', radius: 0.05, mass: 2, kp: 400, kd: 20, maxDeviation: 45 },
  { bone: 'head', parent: 'neck', child: null, radius: 0.1, mass: 5, kp: 300, kd: 18, maxDeviation: 50, ballOffsetY: 0.03 },

  { bone: 'leftUpperArm', parent: 'chest', child: 'leftLowerArm', radius: 0.045, mass: 2, kp: 250, kd: 12, maxDeviation: 170 },
  { bone: 'leftLowerArm', parent: 'leftUpperArm', child: 'leftHand', radius: 0.04, mass: 1.5, kp: 250, kd: 12, maxDeviation: 150 },
  { bone: 'rightUpperArm', parent: 'chest', child: 'rightLowerArm', radius: 0.045, mass: 2, kp: 250, kd: 12, maxDeviation: 170 },
  { bone: 'rightLowerArm', parent: 'rightUpperArm', child: 'rightHand', radius: 0.04, mass: 1.5, kp: 250, kd: 12, maxDeviation: 150 },

  { bone: 'leftUpperLeg', parent: 'hips', child: 'leftLowerLeg', radius: 0.075, mass: 8, kp: 900, kd: 55, maxDeviation: 100 },
  { bone: 'leftLowerLeg', parent: 'leftUpperLeg', child: 'leftFoot', radius: 0.06, mass: 4, kp: 700, kd: 45, maxDeviation: 150 },
  { bone: 'leftFoot', parent: 'leftLowerLeg', child: 'leftToes', radius: 0.045, mass: 1.5, kp: 300, kd: 20, maxDeviation: 50, fixedLen: 0.14 },
  { bone: 'rightUpperLeg', parent: 'hips', child: 'rightLowerLeg', radius: 0.075, mass: 8, kp: 900, kd: 55, maxDeviation: 100 },
  { bone: 'rightLowerLeg', parent: 'rightUpperLeg', child: 'rightFoot', radius: 0.06, mass: 4, kp: 700, kd: 45, maxDeviation: 150 },
  { bone: 'rightFoot', parent: 'rightLowerLeg', child: 'rightToes', radius: 0.045, mass: 1.5, kp: 300, kd: 20, maxDeviation: 50, fixedLen: 0.14 },
];

/** 可在线调的参数（面板滑块直接改这些） */
export interface RagdollTuning {
  /** 重力倍率 */
  gravityScale: number;
  /** 角跟随强度倍率（0=软面条，1=正常，2=僵硬跟随） */
  follow: number;
  /** 髋部支撑强度：1=动画把身体托住，0=纯重力（会倒，需要学出来的策略才站得住） */
  balance: number;
  /** 地面摩擦 */
  groundFriction: number;
  /** 单段最大扭矩（N·m/kg，按体重缩放） */
  maxTorquePerKg: number;
  /** 每帧物理子步数 */
  subSteps: number;
  /** 判定「倒下」的髋高比例（相对站立髋高） */
  downedHeightRatio: number;
  /** 倒下后保持躺姿的秒数 */
  recoverDelay: number;
  /** 起身过渡秒数 */
  recoverRamp: number;
  /** 髋部线性弹簧刚度（1/s²）与阻尼（1/s） */
  hipKp: number;
  hipKd: number;
  /** 是否建内置大地面。训练时关掉，让地形生成器全权接管（否则坑挖不下去） */
  groundEnabled: boolean;
}

export const DEFAULT_TUNING: RagdollTuning = {
  gravityScale: 1,
  follow: 1,
  balance: 0.9,
  groundFriction: 1.0,
  maxTorquePerKg: 40,
  subSteps: 2,
  downedHeightRatio: 0.62,
  recoverDelay: 1.2,
  recoverRamp: 1.4,
  hipKp: 420,
  hipKd: 42,
  groundEnabled: true,
};

/** 身高 → 缩放系数（限制在 0.5~2.0 倍，避免异常模型把物理参数拉到不可用区间） */
export function scaleFor(height: number): number {
  const s = height / HEIGHT_REF;
  if (!isFinite(s) || s <= 0) return 1;
  return Math.min(2, Math.max(0.5, s));
}
