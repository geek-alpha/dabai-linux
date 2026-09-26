/* 前端口径对照：同一份策略权重、同一开局，只改力矩上限 ——
 * 复现「腿在空中乱扒拉、不落地」到底是策略没学好，还是前端把 maxTorquePerKg
 * 的改动吞掉了（setTuning 原先不重算 maxTorque，身体停在构造时的 40）。
 *
 *   A 修复前：maxTorquePerKg 名义 2.0，实际身体仍按 40 跑（扭矩差 20 倍）
 *   B 修复后：maxTorquePerKg 真的 2.0（训练分布 1.0~3.0 内）
 *
 * 判据：髋高比（站没站住）、双脚离地比例（落地没有）、水平位移。
 * 用法：node tools/train/_frontend_ab.mjs <权重文件> */
import { RagdollEnv, EPISODE_STEPS, KIND_FLAT } from './env.mjs';
import { loadWeights } from './train.mjs';
import { ACT_DIM } from './build/ragdoll-core.mjs';

const file = process.argv[2] || 'web/generated/policy/ragdoll-v1.json';
const { net } = loadWeights(file);

const ML = { balance: 0.25, subSteps: 1, downedHeightRatio: 0 };
const CASES = [
  ['A 修复前 实际40', { ...ML, maxTorquePerKg: 40 }],
  ['B 修复后 真2.0', { ...ML, maxTorquePerKg: 2.0 }],
  // 髋部线性弹簧是「手工支撑」：它替脚承担体重时，策略没有任何理由让脚落地。
  ['C 无弹簧 bal0', { ...ML, balance: 0, maxTorquePerKg: 2.0 }],
];

const SEEDS = [900001, 900002, 900003];

// 基线：踝关节骨骼本身就长在脚底上方（人脚踝约 7cm），所以「踝离地 5~8cm」
// 未必是腾空。先量站立动画姿态下踝的世界高度，低于它的才算真离地。
let STAND_ANKLE = 0;
{
  const env = new RagdollEnv({ randomize: false });
  await env.init();
  env.forceTerrain = KIND_FLAT;
  env.forceLying = false;
  env.reset(SEEDS[0]);
  const g = env.rd.groundHeightAt(0, 0);
  const base = env.rd.getParts()
    .filter((p) => p.def.bone.endsWith('Foot'))
    .map((p) => p.body.translation().y - g);
  STAND_ANKLE = base.reduce((a, b) => a + b, 0) / base.length;
  console.log(`[基线] 站立动画姿态踝高 ${(STAND_ANKLE * 100).toFixed(1)} cm —— 高于它的 1.25 倍才算腾空`);
  env.dispose();
}
// 绝对阈值会误判：正常站立踝就在 12cm，4cm 阈值把任何站姿都算成腾空（实测 98%）
const AIRBORNE = STAND_ANKLE * 1.25;

for (const [label, patch] of CASES) {
  const env = new RagdollEnv({ randomize: false });
  await env.init();
  env.forceTerrain = KIND_FLAT;
  env.forceLying = false;
  env.difficulty = 1;
  env.dt = 1 / 30; // 前端 pdt 封顶
  let obs = env.reset(SEEDS[0]);
  env.rd.setTuning(patch);

  let hipSum = 0, airFrames = 0, frames = 0, lowest = 0;
  const act = new Float32Array(ACT_DIM);
  for (const seed of SEEDS) {
    obs = env.reset(seed);
    env.rd.setTuning(patch); // reset 重建了物理身体，口径要重打
    for (let s = 0; s < EPISODE_STEPS; s++) {
      const m = net.policy.forward(obs);
      for (let j = 0; j < ACT_DIM; j++) act[j] = m[j];
      const { info } = env.step(act);
      obs = env.obs;
      if (s < 30) continue; // 开局姿势过渡不算
      const feet = env.rd.getParts().filter((p) => p.def.bone.endsWith('Foot'));
      const ground = env.rd.groundHeightAt(0, 0);
      const lift = Math.min(...feet.map((p) => p.body.translation().y - ground));
      lowest += lift;
      if (lift > AIRBORNE) airFrames++;
      hipSum += info.hipRatio;
      frames++;
    }
  }
  console.log(
    `[${label}] 髋高比均值 ${(hipSum / frames).toFixed(3)} | ` +
    `双脚腾空帧 ${(100 * airFrames / frames).toFixed(1)}% | 最低脚离地均值 ${(lowest / frames * 100).toFixed(1)}cm`
  );
  env.dispose();
}
