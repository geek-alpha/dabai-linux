/* 临时自检：同一权重、同一开局、同一动作序列来源，只改物理口径 ——
 * 判定前端「走路踉跄」是策略没学好，还是前端物理参数跑出了训练分布。
 * 用法：node tools/train/_tuning_ab.mjs <权重文件> */
import { RagdollEnv, EPISODE_STEPS, KIND_FLAT } from './env.mjs';
import { loadWeights } from './train.mjs';
import { ACT_DIM } from './build/ragdoll-core.mjs';

const file = process.argv[2] || 'web/generated/policy/ragdoll-v1.json';
const { net } = loadWeights(file);

const CASES = [
  ['训练口径 站姿 无随机', { dt: 1 / 60, patch: null, randomize: false }],
  ['训练口径 站姿 随机化', { dt: 1 / 60, patch: null, randomize: true }],
  ['纯PD   站姿 无随机', { dt: 1 / 60, patch: null, randomize: false, pd: true }],
  ['前端口径 sub2 T40', { dt: 1 / 60, patch: { subSteps: 2, maxTorquePerKg: 40, downedHeightRatio: 0.62 }, randomize: false }],
  ['前端+30fps', { dt: 1 / 30, patch: { subSteps: 2, maxTorquePerKg: 40, downedHeightRatio: 0.62 }, randomize: false }],
];

const stat = (a) => {
  const m = a.reduce((x, y) => x + y, 0) / a.length;
  const sd = Math.sqrt(a.reduce((x, y) => x + (y - m) ** 2, 0) / a.length);
  return `${Math.min(...a).toFixed(2)}/${m.toFixed(2)}/${Math.max(...a).toFixed(2)}(sd${sd.toFixed(2)})`;
};

for (const [label, cfg] of CASES) {
  const env = new RagdollEnv({ randomize: !!cfg.randomize });
  await env.init();
  env.forceTerrain = KIND_FLAT;
  env.forceLying = false;
  env.difficulty = 1;
  env.dt = cfg.dt;
  let obs = env.reset(900001);
  if (cfg.patch) env.rd.setTuning(cfg.patch);
  const standHipY = env.standHipY;

  const act = new Float32Array(ACT_DIM);
  const hip = env.rd.getParts()[0];
  const p0 = { ...hip.body.translation() };
  const ratios = [], sp = [];
  for (let s = 0; s < EPISODE_STEPS; s++) {
    if (cfg.pd) act.fill(0);
    else {
      const m = net.policy.forward(obs);
      for (let j = 0; j < ACT_DIM; j++) act[j] = m[j];
    }
    env.step(act);
    const t = hip.body.translation();
    const v = hip.body.linvel();
    ratios.push((t.y - env.rd.groundHeightAt(t.x, t.z)) / standHipY);
    sp.push(Math.hypot(v.x, v.y, v.z));
    obs = env.obs;
  }
  const pEnd = hip.body.translation();
  const d = Math.hypot(pEnd.x - p0.x, pEnd.z - p0.z);
  console.log(`[${label}] 髋高比 ${stat(ratios)} 速度 ${stat(sp)} 位移 ${d.toFixed(2)}m (站立髋高基准 ${standHipY.toFixed(3)}m)`);
  env.dispose();
}
