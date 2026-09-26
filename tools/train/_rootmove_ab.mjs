/* root 水平平移对照：浏览器里角色被行走系统推着走，仿真里 root 永远钉死。
 *
 * 背景（实测）：
 *   - 训练环境 balance=0（env.mjs 的 randomize=false 分支），髋部没有线性弹簧托着；
 *     前端 ML 模式跑 balance=0.25（web/js/physics/43_ragdoll_mode.ts:94）。
 *   - 探针 _rootmove_probe.mjs 确认 root 平移会传导到髋的 driveP（移动靶），
 *     但 balance=0 时髋 body 纹丝不动（0.017→0.10 的目标位移下 body 只抖 ±0.007）。
 *
 * 所以本脚本把「root 平移」与「髋部支撑强度」放一起测：单看 root 平移没意义，
 * 要看它在前端那种「弹簧托着移动靶」的口径下会不会把腿拖开。
 *
 * 用法：node tools/train/_rootmove_ab.mjs [权重文件]
 */
import { RagdollEnv, KIND_FLAT } from './env.mjs';
import { loadWeights } from './train.mjs';
import { ACT_DIM } from './build/ragdoll-core.mjs';

const FILE = process.argv[2] || 'web/generated/policy/ragdoll-v1.json';
const WINDOW = 300;
const SEEDS = [900001, 900002, 900003];

/** [标签, tuning patch, dt] —— 前端 ML 模式口径见 43_ragdoll_mode.ts:94 */
const CASES = [
  ['前端口径 bal.25', { balance: 0.25 }, 1 / 30],
];
/** m/s：2.4 是快步量级，用来测「动画目标一直往前跑」会不会把身体拖垮 */
const SPEEDS = [0, 1.2, 2.4];

const { net } = loadWeights(FILE);

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

console.log(`权重：${FILE}`);
console.log('（腿侧向 = 腿轴在骨盆侧向轴上的投影，越大越像劈叉；cos = 两腿夹角余弦，越小越张开）\n');

for (const [label, patch, dt] of CASES) {
  for (const speed of SPEEDS) {
    for (const ctrl of ['PD', 'ML']) {
      const lat = [], cos = [], hip = [], slip = [];
      for (const seed of SEEDS) {
        const env = new RagdollEnv({ randomize: false });
        await env.init();
        env.forceTerrain = KIND_FLAT;
        env.forceLying = false;
        env.difficulty = 1;
        env.dt = dt;
        let obs = env.reset(seed);
        env.rd.setTuning(patch);
        env.legSwayScale = 1.0;
        env.swayScale = 1.0;
        env.rootSpeed = speed;
        const byName = {};
        for (const p of env.rd.getParts()) byName[p.bone.name] = p;

        const act = new Float32Array(ACT_DIM);
        for (let s = 0; s < WINDOW; s++) {
          if (ctrl === 'ML') {
            const m = net.policy.forward(obs);
            for (let j = 0; j < ACT_DIM; j++) act[j] = m[j];
          } else {
            act.fill(0);
          }
          env.step(act);
          const r = sample(env, byName);
          lat.push(r.lat); cos.push(r.cos); hip.push(r.hip);
          const hb = env.rd.getParts()[0].body.translation();
          slip.push(Math.abs(P(byName.hips.bone)[0] - hb.x));
          obs = env.obs;
        }
        env.dispose();
      }
      console.log(
        `[${label} 速度${speed}] ${ctrl}  腿侧向 ${f3(me(lat))}/峰${f3(Math.max(...lat))}` +
        `  两腿cos ${f3(me(cos))}/低${f3(Math.min(...cos))}  髋高比 ${f3(me(hip))}  动画目标与身体错位 ${f3(me(slip))}`
      );
    }
  }
}
