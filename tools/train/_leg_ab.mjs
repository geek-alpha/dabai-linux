/* 临时自检：腿为什么向侧面张（劈叉）。
 * 用关节世界位置差算大腿指向（不依赖骨骼轴约定），再投影到骨盆侧向轴：
 * 站立/迈步时侧向分量 ≈ 0，劈叉时接近 ±1。
 *
 * 只统计前 WINDOW 帧 —— 600 帧全跑会跑到倒地姿态，那时「腿张开」是躺姿不是步态，
 * 拿它当判据会把「倒地」误读成「劈叉」（实测过：全窗口均值 0.42 其实来自 0.185 的髋高）。
 * 用法：node tools/train/_leg_ab.mjs [权重文件] */
import { RagdollEnv, KIND_FLAT } from './env.mjs';
import { loadWeights } from './train.mjs';
import { ACT_DIM } from './build/ragdoll-core.mjs';

const file = process.argv[2] || 'web/generated/policy/ragdoll-v1.json';
const { net } = loadWeights(file);
const WINDOW = 120;

const P = (bone) => {
  bone.updateWorldMatrix(true, false);
  const e = bone.matrixWorld.elements;
  return [e[12], e[13], e[14]];
};
const sub = (a, b) => [a[0] - b[0], a[1] - b[1], a[2] - b[2]];
const nrm = (v) => { const l = Math.hypot(v[0], v[1], v[2]) || 1; return [v[0] / l, v[1] / l, v[2] / l]; };
const dot = (a, b) => a[0] * b[0] + a[1] * b[1] + a[2] * b[2];

/** 腿段在 SEGMENTS 里的起始索引（leftUpperLeg）→ 动作偏移 9*3 */
const LEG_ACT_FROM = 9 * 3;

const CASES = [
  ['训练口径 T1.8', { dt: 1 / 60, sub: 1 }],
  ['训练口径 T40', { dt: 1 / 60, sub: 1, tune: { maxTorquePerKg: 40 } }],
  ['训练口径 腿残差置零', { dt: 1 / 60, sub: 1, legOff: true }],
  ['前端 dt1/30 T1.8', { dt: 1 / 30, sub: 1 }],
];

for (const [label, cfg] of CASES) {
  const env = new RagdollEnv({ randomize: false });
  await env.init();
  env.forceTerrain = KIND_FLAT;
  env.forceLying = false;
  env.difficulty = 1;
  env.dt = cfg.dt;
  let obs = env.reset(900001);
  env.rd.setTuning({ subSteps: cfg.sub, ...(cfg.tune || {}) });
  const parts = env.rd.getParts();
  const byName = {};
  for (const p of parts) byName[p.bone.name] = p;
  const act = new Float32Array(ACT_DIM);
  const sides = [], legcs = [], ups = [], ratios = [];
  for (let s = 0; s < WINDOW; s++) {
    const m = net.policy.forward(obs);
    for (let j = 0; j < ACT_DIM; j++) act[j] = m[j];
    if (cfg.legOff) for (let j = LEG_ACT_FROM; j < ACT_DIM; j++) act[j] = 0;
    env.step(act);
    const hipsP = P(byName.hips.bone), chest = P(byName.chest.bone);
    const lu = P(byName.leftUpperLeg.bone), ru = P(byName.rightUpperLeg.bone);
    const ll = P(byName.leftLowerLeg.bone), rl = P(byName.rightLowerLeg.bone);
    const r = nrm(sub(ru, lu)), u = nrm(sub(chest, hipsP));
    const dL = nrm(sub(ll, lu)), dR = nrm(sub(rl, ru));
    sides.push(Math.max(Math.abs(dot(dL, r)), Math.abs(dot(dR, r))));
    legcs.push(dot(dL, dR));
    ups.push(Math.min(dot(dL, u), dot(dR, u)));
    ratios.push((hipsP[1] - env.rd.groundHeightAt(hipsP[0], hipsP[2])) / env.standHipY);
    obs = env.obs;
  }
  const me = (a) => a.reduce((x, y) => x + y, 0) / a.length;
  console.log(
    `[${label}] 前${WINDOW}帧 髋高比 ${Math.min(...ratios).toFixed(2)}/${me(ratios).toFixed(2)}` +
    ` | 腿侧向 max ${Math.max(...sides).toFixed(3)} 均值 ${me(sides).toFixed(3)}` +
    ` | 两腿夹角余弦 min ${Math.min(...legcs).toFixed(3)}` +
    ` | 腿最不向下 max ${Math.max(...ups).toFixed(3)}`
  );
  env.dispose();
}
