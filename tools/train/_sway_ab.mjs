/* 临时自检：动画目标幅度是否和策略「打架」导致劈叉。
 *
 * 训练里腿部 SWAY 只有 0.03~0.05 rad（≈2°，绕 X 前后摆），而走路动画的腿
 * 前后摆 ±25~35°。物理是软驱动（PD 拉向动画目标）+ 策略残差叠加，两者都在
 * 同一条腿上使劲 —— 目标幅度超出训练分布时，残差会把腿推开。
 *
 * 做法：不重写 env.step，只把 env.restorePose 包一层，在「动画目标已写好、
 * 物理步进之前」注入放大版腿部摆动。这正是前端每帧的真实时序。
 * 用法：node tools/train/_sway_ab.mjs [权重文件]
 */
import * as THREE from 'three';
import { RagdollEnv, KIND_FLAT } from './env.mjs';
import { loadWeights } from './train.mjs';
import { ACT_DIM } from './build/ragdoll-core.mjs';

const file = process.argv[2] || 'web/generated/policy/ragdoll-v1.json';
const { net } = loadWeights(file);
const WINDOW = 120;
const AMP = [0, 0.1, 0.3, 0.6];

const P = (bone) => {
  bone.updateWorldMatrix(true, false);
  const e = bone.matrixWorld.elements;
  return [e[12], e[13], e[14]];
};
const sub = (a, b) => [a[0] - b[0], a[1] - b[1], a[2] - b[2]];
const nrm = (v) => { const l = Math.hypot(v[0], v[1], v[2]) || 1; return [v[0] / l, v[1] / l, v[2] / l]; };
const dot = (a, b) => a[0] * b[0] + a[1] * b[1] + a[2] * b[2];

const _q = new THREE.Quaternion();
const _ax = new THREE.Vector3(1, 0, 0);
const LEGS = [['leftUpperLeg', 1], ['rightUpperLeg', -1], ['leftLowerLeg', -1], ['rightLowerLeg', 1]];

function injectLegSway(env, amp, ph) {
  const s = Math.sin(ph);
  for (const [name, sign] of LEGS) {
    const b = env.bones[name];
    if (!b) continue;
    _q.setFromAxisAngle(_ax, amp * sign * s);
    b.quaternion.multiply(_q);
  }
  env.root.updateWorldMatrix(true, true);
}

for (const amp of AMP) {
  const env = new RagdollEnv({ randomize: false });
  await env.init();
  env.forceTerrain = KIND_FLAT;
  env.forceLying = false;
  env.difficulty = 1;
  env.dt = 1 / 60;
  const origRestore = env.restorePose.bind(env);
  // ph 必须用秒：t 是整数帧号，ph = t·2π 时 sin 每帧都是 0（四组结果逐位相同就是这么来的）
  if (amp > 0) env.restorePose = () => { origRestore(); injectLegSway(env, amp, env.t * (Math.PI * 2 / 60)); };
  let obs = env.reset(900001);
  env.rd.setTuning({ subSteps: 1 });
  const parts = env.rd.getParts();
  const byName = {};
  for (const p of parts) byName[p.bone.name] = p;
  const act = new Float32Array(ACT_DIM);
  const sides = [], legcs = [], ratios = [];
  for (let s = 0; s < WINDOW; s++) {
    const m = net.policy.forward(obs);
    for (let j = 0; j < ACT_DIM; j++) act[j] = m[j];
    env.step(act);
    const hipsP = P(byName.hips.bone), chest = P(byName.chest.bone);
    const lu = P(byName.leftUpperLeg.bone), ru = P(byName.rightUpperLeg.bone);
    const ll = P(byName.leftLowerLeg.bone), rl = P(byName.rightLowerLeg.bone);
    const r = nrm(sub(ru, lu)), u = nrm(sub(chest, hipsP));
    const dL = nrm(sub(ll, lu)), dR = nrm(sub(rl, ru));
    sides.push(Math.max(Math.abs(dot(dL, r)), Math.abs(dot(dR, r))));
    legcs.push(dot(dL, dR));
    ratios.push((hipsP[1] - env.rd.groundHeightAt(hipsP[0], hipsP[2])) / env.standHipY);
    obs = env.obs;
  }
  const me = (a) => a.reduce((x, y) => x + y, 0) / a.length;
  console.log(
    `[腿摆 ${(amp * 180 / Math.PI).toFixed(0)}°] 髋高比 min ${Math.min(...ratios).toFixed(2)} 均值 ${me(ratios).toFixed(2)}` +
    ` | 腿侧向 max ${Math.max(...sides).toFixed(3)} 均值 ${me(sides).toFixed(3)}` +
    ` | 两腿夹角余弦 min ${Math.min(...legcs).toFixed(3)}`
  );
  env.dispose();
}
