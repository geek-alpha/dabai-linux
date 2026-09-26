/* ============================================================
 * 策略 IO 规格（观测 / 动作）—— 训练环境与浏览器共用同一份定义
 * ------------------------------------------------------------
 * 这份文件是 sim2sim 的合同：训练侧（tools/train）与推理侧
 * （ragdoll-policy.ts）都调这里的函数组装观测、写入动作。
 * 任何一边改了维度或归一化，另一边跟着变，不会出现「训练时喂
 * 的数和推理时喂的数不是一套」这种最难查的漂移。
 *
 * 观测 = 每段 6 维 × 15 段 + 根状态 9 维 + 地形探针 16 维 = 115 维
 *   每段：[世界角误差 rotvec(3, 弧度), 角速度(3, /8 rad/s)]
 *   根状态：[髋高比偏差, 髋线速度(3,/3 m/s), 髋水平偏差(2,/0.4 m),
 *            相位 sin/cos, 时间比例]
 *   地形探针：[8 方向 × 2 半径] 相对脚下地面的高差（/0.4 m）
 * 动作 = 每段 3 维旋转向量残差（世界系，限幅 ±0.6 rad），零 = 原 PD 行为
 * 完整输入 = 观测 115 + 上一帧动作 45 = 160 维（PPO 训练时的实际输入）
 * ============================================================ */

import * as THREE from 'three';
import { SEGMENTS } from './ragdoll-config.js';
import type { ActiveRagdoll } from './active-ragdoll.js';

export const PART_COUNT = SEGMENTS.length;
export const OBS_PER_PART = 6;
export const ROOT_OBS = 9;
/** 地形探针：8 个方向 × 2 个半径，相对脚下地面的高差 —— 补上「盲走」缺的那只眼 */
export const PROBE_DIRS = 8;
export const PROBE_RADII = [0.4, 0.8];
export const TERRAIN_OBS = PROBE_DIRS * PROBE_RADII.length;
/** 探针高差归一化：实测最大高差 0.72m（坑底望高台），除 0.4 后落在 ±1.8，
 *  与观测里其他项的 O(1) 量级对齐；单层台阶（8cm）读数 0.2，仍能分辨 */
export const PROBE_SCALE = 1 / 0.4;
/** 不含上一帧动作的观测维度 */
export const OBS_DIM = PART_COUNT * OBS_PER_PART + ROOT_OBS + TERRAIN_OBS;
export const ACT_DIM = PART_COUNT * 3;
/** 网络实际输入维度（含上一帧动作） */
export const OBS_DIM_FULL = OBS_DIM + ACT_DIM;

export const ANG_SCALE = 1.0;
export const ANGVEL_SCALE = 1 / 8;
export const VEL_SCALE = 1 / 3;
export const POS_SCALE = 1 / 0.4;
/** 单段旋转残差上限（弧度）—— 太大会让 PD 目标飞掉，物理发散 */
export const ACTION_LIMIT = 0.6;

const _q1 = new THREE.Quaternion();
const _q2 = new THREE.Quaternion();
const _q3 = new THREE.Quaternion();
const _v1 = new THREE.Vector3();

/** 旋转向量（轴角，世界系）写入 out[k..k+2]；用 sinc 形式避免除零 */
function writeRotvec(q: THREE.Quaternion, out: Float32Array, k: number, scale: number) {
  if (q.w < 0) _q3.set(-q.x, -q.y, -q.z, -q.w);
  else _q3.copy(q);
  const w = Math.min(1, Math.max(-1, _q3.w));
  const ang = 2 * Math.acos(w);
  const s = Math.sqrt(Math.max(0, 1 - w * w));
  const f = s > 1e-5 && ang > 1e-5 ? (ang / s) * scale : 0;
  out[k] = _q3.x * f;
  out[k + 1] = _q3.y * f;
  out[k + 2] = _q3.z * f;
}

/**
 * 地形探针：以脚下地形为基准，沿躯干朝向均布 8 个方向、2 个半径各采一次高差。
 * 没有地形钩子时（浏览器平地）全部读出 0 —— 那是「脚下是平的」的正确读数，
 * 不是缺失值。
 */
function writeTerrainProbe(
  rd: ActiveRagdoll,
  out: Float32Array,
  k: number,
  x: number,
  z: number,
  q: { x: number; y: number; z: number; w: number }
): void {
  // 躯干局部 +Z 的水平投影当「前方」：躯干躺平时它指向天空、投影退化，用世界
  // +X 兜底 —— 整体方向旋转只是探针轮换，8 方向环绕下信息量不变
  let fx = 2 * (q.x * q.z + q.w * q.y);
  let fz = 1 - 2 * (q.x * q.x + q.y * q.y);
  const flen = Math.sqrt(fx * fx + fz * fz);
  if (flen > 1e-3) { fx /= flen; fz /= flen; } else { fx = 1; fz = 0; }
  const base = rd.terrainHeightAt(x, z);
  for (let ri = 0; ri < PROBE_RADII.length; ri++) {
    const rad = PROBE_RADII[ri];
    for (let d = 0; d < PROBE_DIRS; d++) {
      const a = (d / PROBE_DIRS) * Math.PI * 2;
      const ca = Math.cos(a), sa = Math.sin(a);
      const dx = fx * ca - fz * sa;
      const dz = fz * ca + fx * sa;
      out[k++] = (rd.terrainHeightAt(x + dx * rad, z + dz * rad) - base) * PROBE_SCALE;
    }
  }
}

/**
 * 组装观测（不含上一帧动作），返回写入结束位置。
 * @param standHipY 站立时髋部高度（世界单位）—— 归一化基准
 * @param phase 步态相位（弧度），timeRatio 0~1 的 episode 进度
 */
export function buildObservation(
  rd: ActiveRagdoll,
  out: Float32Array,
  offset: number,
  standHipY: number,
  phase: number,
  timeRatio: number
): number {
  const parts = rd.getParts();
  let k = offset;
  for (let i = 0; i < parts.length; i++) {
    const p = parts[i];
    const r = p.body.rotation();
    _q1.set(r.x, r.y, r.z, r.w);
    // 世界系角误差 qErr = driveQ × current⁻¹，与 applyDrive 里的误差口径一致
    _q2.copy(p.driveQ).multiply(_q3.copy(_q1).invert());
    writeRotvec(_q2, out, k, ANG_SCALE);
    k += 3;
    const w = p.body.angvel();
    out[k++] = w.x * ANGVEL_SCALE;
    out[k++] = w.y * ANGVEL_SCALE;
    out[k++] = w.z * ANGVEL_SCALE;
  }

  const hip = parts[0];
  const t = hip.body.translation();
  const v = hip.body.linvel();
  out[k++] = (t.y - rd.groundHeightAt(t.x, t.z)) / Math.max(1e-3, standHipY) - 1;
  out[k++] = v.x * VEL_SCALE;
  out[k++] = v.y * VEL_SCALE;
  out[k++] = v.z * VEL_SCALE;
  out[k++] = (t.x - hip.driveP.x) * POS_SCALE;
  out[k++] = (t.z - hip.driveP.z) * POS_SCALE;
  out[k++] = Math.sin(phase);
  out[k++] = Math.cos(phase);
  out[k++] = timeRatio;
  writeTerrainProbe(rd, out, k, t.x, t.z, hip.body.rotation());
  k += TERRAIN_OBS;
  return k;
}

/** 把动作写进物理侧残差缓冲（自动限幅、复用缓冲避免每帧分配） */
export function writeAction(rd: ActiveRagdoll, act: Float32Array, offset = 0, count = ACT_DIM) {
  let res = rd.policyResidual;
  if (!res || res.length !== count) {
    res = new Float32Array(count);
    rd.policyResidual = res;
  }
  for (let i = 0; i < count; i++) {
    const v = act[offset + i];
    res[i] = v > ACTION_LIMIT ? ACTION_LIMIT : v < -ACTION_LIMIT ? -ACTION_LIMIT : v;
  }
}

/** 观测 + 上一帧动作拼成网络输入（prevAct 为 null 时补零） */
export function buildNetInput(
  rd: ActiveRagdoll,
  out: Float32Array,
  prevAct: Float32Array | null,
  standHipY: number,
  phase: number,
  timeRatio: number
) {
  const k = buildObservation(rd, out, 0, standHipY, phase, timeRatio);
  for (let i = 0; i < ACT_DIM; i++) out[k + i] = prevAct ? prevAct[i] : 0;
}

/** 站立髋高：从当前骨骼动画姿态的髋位置估（reset 后、物理还没跑时调） */
export function measureStandHipY(rd: ActiveRagdoll): number {
  const hip = rd.getParts()[0];
  return Math.max(1e-3, hip.driveP.y - rd.groundHeightAt(hip.driveP.x, hip.driveP.z));
}

/** 躯干倾角（弧度）：髋段世界朝向与驱动目标朝向的夹角 */
export function torsoTilt(rd: ActiveRagdoll): number {
  const hip = rd.getParts()[0];
  const r = hip.body.rotation();
  _q1.set(r.x, r.y, r.z, r.w);
  _q2.copy(hip.driveQ).multiply(_q3.copy(_q1).invert());
  const w = Math.min(1, Math.abs(_q2.w));
  void _v1;
  return 2 * Math.acos(w);
}
