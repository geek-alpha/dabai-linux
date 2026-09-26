/* 判「劈叉」的参考基线：动画目标姿态本身有多少张开？物理跟完之后又张开多少？
 *
 * 之前只报「腿侧向 0.44」这类绝对值，没有基线就判不了好坏 —— 静息站姿双脚本来就
 * 分开，大腿方向投影到骨盆侧向轴本来就不为 0。这里把两件事分开测：
 *   target = restorePose() 之后的骨骼（= 物理要追的目标，没经过物理）
 *   phys   = 跑 WINDOW 帧物理之后的骨骼（= 用户看到的）
 * 两者之差才是物理/策略引入的张开。
 *
 * 用法：node tools/train/_splay_ref.mjs [权重文件...]
 */
import { RagdollEnv, KIND_FLAT } from './env.mjs';
import { loadWeights } from './train.mjs';
import { ACT_DIM } from './build/ragdoll-core.mjs';

const FILES = process.argv.slice(2).length
  ? process.argv.slice(2)
  : ['web/generated/policy/ragdoll-v1.json', 'web/generated/policy/ragdoll-v2.json'];
const WINDOW = 120;
const SEEDS = [900001, 900002, 900003];

/** legSwayScale=1 时上腿摆 0.32rad≈18°，走路动画 25~35° → 1.4 / 1.85 两档 */
const CASES = [
  ['腿摆 0°', 0, 0],
  ['腿摆 18°', 1.0, 1.0],
  ['腿摆 26°', 1.4, 1.0],
  ['腿摆 34°', 1.85, 1.0],
];

const P = (bone) => {
  bone.updateWorldMatrix(true, false);
  const e = bone.matrixWorld.elements;
  return [e[12], e[13], e[14]];
};
const sub = (a, b) => [a[0] - b[0], a[1] - b[1], a[2] - b[2]];
const nrm = (v) => { const l = Math.hypot(v[0], v[1], v[2]) || 1; return [v[0] / l, v[1] / l, v[2] / l]; };
const dot = (a, b) => a[0] * b[0] + a[1] * b[1] + a[2] * b[2];

/** 采样一次姿态：腿侧向（投影到髋-髋轴）、两腿夹角余弦、腿偏离竖直的量 */
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
    down: Math.min(dot(dL, up), dot(dR, up)),
    hip: (hipsP[1] - env.rd.groundHeightAt(hipsP[0], hipsP[2])) / env.standHipY,
  };
}

const me = (a) => a.reduce((x, y) => x + y, 0) / a.length;
const f3 = (v) => v.toFixed(3);

for (const file of [...FILES, 'PD(零残差)']) {
  const isPd = file === 'PD(零残差)';
  const { net } = isPd ? { net: null } : loadWeights(file);
  console.log(`\n=== ${file} ===`);
  for (const [label, legSway, sway] of CASES) {
    const tgt = [], phy = [], hip = [], cos = [];
    for (const seed of SEEDS) {
      const env = new RagdollEnv({ randomize: false });
      await env.init();
      env.forceTerrain = KIND_FLAT;
      env.forceLying = false;
      env.difficulty = 1;
      let obs = env.reset(seed);
      env.legSwayScale = legSway;
      env.swayScale = sway;
      const byName = {};
      for (const p of env.rd.getParts()) byName[p.bone.name] = p;

      // 目标姿态：只写骨骼，不跑物理
      env.restorePose();
      tgt.push(sample(env, byName).lat);

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
        phy.push(r.lat); hip.push(r.hip); cos.push(r.cos);
        obs = env.obs;
      }
      env.dispose();
    }
    console.log(
      `[${label.padEnd(8)}] 目标姿态侧向 ${f3(me(tgt))}` +
      ` → 物理后 侧向 ${f3(me(phy))} 峰值 ${f3(Math.max(...phy))}` +
      ` | 两腿夹角余弦 ${f3(me(cos))} 最低 ${f3(Math.min(...cos))}` +
      ` | 髋高比 ${f3(me(hip))}`
    );
  }
}
