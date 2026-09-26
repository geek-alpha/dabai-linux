/* ============================================================
 * 训练用骨架 —— 数据取自 models/白头凤.vrm 的真实 raw 骨骼
 * ------------------------------------------------------------
 * tools/train/dump_vrm_skeleton.mjs 直接从 VRM 节点树算局部变换，
 * 不经浏览器、不靠「大概照人体比例估一个」。这样训练骨架的段长、
 * 关节朝向、静止姿态与前端物理看到的完全一致。
 *
 * 尺度口径：前端把模型统一缩放到 2.2 高（web/js/core/03_model_load_gltf_vrm.ts），
 * 所以 buildRig(scale) 的 scale = 2.2 / 原始身高 = 2.2 / 1.60483 ≈ 1.371。
 * 训练时随机化 0.9~1.45 把该值包在中间，并覆盖身高差异带来的尺度变化。
 * ============================================================ */

import * as THREE from 'three';

/* 骨架数据 = 白头凤.vrm 的 humanoid raw 骨骼局部变换（模型原始尺度），
 * 由 tools/train/dump_vrm_skeleton.mjs 生成；hips 已上移到「脚底网格贴 y=0」。
 * 旧数据实为 dabai.vrm（髋高占身高 28%、大腿 0.20m），而前端渲染的白头凤是
 * 髋高 49.9%、大腿 0.40m —— 段长差一倍，策略观测整个落在训练分布外。 */
export const SKELETON = [
  { bone: 'hips', parent: null, pos: [0, 0.80023, -0.03516], quat: [0, 0, 0, 1] },
  { bone: 'spine', parent: 'hips', pos: [0, 0.09675, -0.00842], quat: [0, 0, 0, 1] },
  { bone: 'chest', parent: 'spine', pos: [0, 0.13487, -0.00164], quat: [0, 0, 0, 1] },
  { bone: 'neck', parent: 'chest', pos: [0, 0.22551, 0.03828], quat: [0, 0, 0, 1] },
  { bone: 'head', parent: 'neck', pos: [0, 0.04557, 0.00267], quat: [0, 0, 0, 1] },
  { bone: 'leftUpperArm', parent: 'chest', pos: [-0.08935, 0.18534, 0.04522], quat: [0, 0, 0, 1] },
  { bone: 'leftLowerArm', parent: 'leftUpperArm', pos: [-0.22535, 0.00204, 0], quat: [0, 0, 0, 1] },
  { bone: 'rightUpperArm', parent: 'chest', pos: [0.08935, 0.18534, 0.04522], quat: [0, 0, 0, 1] },
  { bone: 'rightLowerArm', parent: 'rightUpperArm', pos: [0.22535, 0.00204, 0], quat: [0, 0, 0, 1] },
  { bone: 'leftUpperLeg', parent: 'hips', pos: [-0.08065, 0.05479, 0.01521], quat: [0, 0, 0, 1] },
  { bone: 'leftLowerLeg', parent: 'leftUpperLeg', pos: [0.00453, -0.4021, 0.00641], quat: [0, 0, 0, 1] },
  { bone: 'leftFoot', parent: 'leftLowerLeg', pos: [-0.01346, -0.35548, 0.04812], quat: [0, 0, 0, 1] },
  { bone: 'rightUpperLeg', parent: 'hips', pos: [0.08065, 0.05479, 0.01521], quat: [0, 0, 0, 1] },
  { bone: 'rightLowerLeg', parent: 'rightUpperLeg', pos: [-0.00453, -0.4021, 0.00641], quat: [0, 0, 0, 1] },
  { bone: 'rightFoot', parent: 'rightLowerLeg', pos: [0.01346, -0.35548, 0.04812], quat: [0, 0, 0, 1] },
];

/** 只用于量段长的末端节点（SEGMENTS 里 child 字段指向它们） */
const END_NODES = [
  { bone: 'leftHand', parent: 'leftLowerArm', pos: [-0.17244, -0.00059, 0.00744] },
  { bone: 'rightHand', parent: 'rightLowerArm', pos: [0.17244, -0.00059, 0.00744] },
  { bone: 'leftToes', parent: 'leftFoot', pos: [-0.00281, -0.06672, -0.07341] },
  { bone: 'rightToes', parent: 'rightFoot', pos: [0.00281, -0.06672, -0.07341] },
];

export const BASE_HEIGHT = 1.60483;
/** 静止姿态髋高（白头凤原始尺度，脚底贴地） */
export const STAND_HIP_Y = 0.80023;

/**
 * 建一副骨架（Object3D 树）与对应的 RagdollRig。
 * @param scale 整体缩放，等价于前端 scaleFor(模型身高)
 */
export function buildRig(scale = 1) {
  const root = new THREE.Group();
  root.name = 'ragdoll-root';
  const bones = {};

  const make = (def, s) => {
    const o = new THREE.Object3D();
    o.name = def.bone;
    o.position.set(def.pos[0] * s, def.pos[1] * s, def.pos[2] * s);
    if (def.quat) o.quaternion.set(def.quat[0], def.quat[1], def.quat[2], def.quat[3]);
    bones[def.bone] = o;
    return o;
  };

  for (const def of SKELETON) make(def, scale);
  for (const def of END_NODES) make(def, scale);

  for (const def of SKELETON) {
    const node = bones[def.bone];
    const p = def.parent ? bones[def.parent] : root;
    p.add(node);
  }
  for (const def of END_NODES) bones[def.parent].add(bones[def.bone]);

  root.updateWorldMatrix(true, true);
  return {
    root,
    bones,
    rig: { bones, height: BASE_HEIGHT * scale, groundY: 0 },
    standHipY: STAND_HIP_Y * scale,
  };
}

/** 每帧把骨架恢复到目标动画姿态（物理写回会污染骨骼，训练时必须还原） */
export function snapshotPose(bones) {
  const pose = {};
  for (const name of Object.keys(bones)) {
    const b = bones[name];
    pose[name] = { p: b.position.clone(), q: b.quaternion.clone() };
  }
  return pose;
}

export function applyPose(bones, pose, root) {
  for (const name of Object.keys(pose)) {
    const b = bones[name];
    if (!b) continue;
    b.position.copy(pose[name].p);
    b.quaternion.copy(pose[name].q);
  }
  root.updateWorldMatrix(true, true);
}
