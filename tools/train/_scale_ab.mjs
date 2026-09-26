/* 体型（scale）对照实验：腿长的人真的更容易劈叉吗？
 *
 * 机制猜想（代码依据）：
 *   - PD 比例增益 kp 是每段常数（ragdoll-config.ts:40-56），active-ragdoll.ts:220
 *     只乘 tuning.follow，**不随体型缩放**
 *   - 质量 ∝ scale³（active-ragdoll.ts:175），转动惯量 I ∝ m·L² ∝ scale⁵
 *   - 于是「角响应能力」 τ/I ∝ scale⁻⁵：体型越大，PD 相对越软
 * 前端模型实际 scale≈1.29，而确定性训练/自检环境是 1.0 —— 这个差必须先量化。
 *
 * 用法：node tools/train/_scale_ab.mjs [权重文件...]
 */
import { RagdollEnv, KIND_FLAT } from './env.mjs';
import { loadWeights } from './train.mjs';
import { ACT_DIM } from './build/ragdoll-core.mjs';

const FILE = process.argv[2] || 'web/generated/policy/ragdoll-v1.json';
const WINDOW = 120;
const SEEDS = [900001, 900002, 900003];
const SCALES = [0.9, 1.0, 1.15, 1.29, 1.4];
const SWAYS = [
  ['静止', 0],
  ['腿摆18°', 1.0],
];

const P = (bone) => {
  bone.updateWorldMatrix(true, false);
  const e = bone.matrixWorld.elements;
  return [e[12], e[13], e[14]];
};
const sub = (a, b) => [a[0] - b[0], a[1] - b[1], a[2] - b[2]];
const nrm = (v) => { const l = Math.hypot(v[0], v[1], v[2]) || 1; return [v[0] / l, v[1] / l, v[2] / l]; };
const dot = (a, b) => a[0] * b[0] + a[1] * b[1] + a[2] * b[2];

function sample(env, byName) {
  const hipsP = P(byName.hips.bone), chest = P(byName.chest.bone);
  const lu = P(byName.leftUpperLeg.bone), ru = P(byName.rightUpperLeg.bone);
  const ll = P(byName.leftLowerLeg.bone), rl = P(byName.rightLowerLeg.bone);
  const r = nrm(sub(ru, lu));
  const up = nrm(sub(chest, hipsP));
  const dL = nrm(sub(ll, lu)), dR = nrm(sub(rl, ru));
  return {
    lat: Math.max(Math.abs(dot(dL, r)), Math.abs(dot(dR, r))),
    cos: dot(dL, dR),
    hip: (hipsP[1] - env.rd.groundHeightAt(hipsP[0], hipsP[2])) / env.standHipY,
  };
}

const me = (a) => a.reduce((x, y) => x + y, 0) / a.length;
const f3 = (v) => v.toFixed(3);

/** 跑一组：scale 固定、摆动固定，返回指标均值 */
async function run(net, scale, legSway) {
  const lat = [], cos = [], hip = [];
  for (const seed of SEEDS) {
    const env = new RagdollEnv({ randomize: false });
    await env.init();
    env.forceTerrain = KIND_FLAT;
    env.forceLying = false;
    env.forceScale = scale;
    env.difficulty = 1;
    let obs = env.reset(seed);
    env.legSwayScale = legSway;
    env.swayScale = 1.0;
    const byName = {};
    for (const p of env.rd.getParts()) byName[p.bone.name] = p;

    const act = new Float32Array(ACT_DIM);
    for (let s = 0; s < WINDOW; s++) {
      if (net) {
        const m = net.policy.forward(obs);
        for (let j = 0; j < ACT_DIM; j++) act[j] = m[j];
      } else {
        act.fill(0);
      }
      env.step(act);
      const r = sample(env, byName);
      lat.push(r.lat); cos.push(r.cos); hip.push(r.hip);
      obs = env.obs;
    }
    env.dispose();
  }
  return { lat: me(lat), cos: me(cos), hip: me(hip), latMax: Math.max(...lat), cosMin: Math.min(...cos) };
}

const { net } = loadWeights(FILE);
console.log(`权重：${FILE}\n`);
console.log('scale   摆动     控制器  腿侧向(均值/峰值)  两腿夹角余弦(均值/最低)  髋高比');

for (const [swayLabel, sway] of SWAYS) {
  for (const scale of SCALES) {
    for (const [name, n] of [['PD', null], ['ML', net]]) {
      const r = await run(n, scale, sway);
      console.log(
        `${f3(scale)}   ${swayLabel.padEnd(7)} ${name.padEnd(7)} ` +
        `${f3(r.lat)} / ${f3(r.latMax)}        ${f3(r.cos)} / ${f3(r.cosMin)}        ${f3(r.hip)}`
      );
    }
  }
  console.log('');
}
