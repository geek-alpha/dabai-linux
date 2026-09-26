/* 临时自检：训练分布内（随机化）按 balance 分组，看策略的站立质量随「手工
 * 平衡辅助」怎么变 —— 前端把 balance 设成 0，这决定它是否落在策略能应付的区间。
 * 用法：node tools/train/_balance_ab.mjs <权重文件> */
import { RagdollEnv, EPISODE_STEPS, KIND_FLAT } from './env.mjs';
import { loadWeights } from './train.mjs';
import { ACT_DIM } from './build/ragdoll-core.mjs';

const file = process.argv[2] || 'web/generated/policy/ragdoll-v1.json';
const { net } = loadWeights(file);

const env = new RagdollEnv({ randomize: true });
await env.init();
env.forceTerrain = KIND_FLAT;
env.forceLying = false;
env.difficulty = 1;

const rows = [];
const act = new Float32Array(ACT_DIM);
for (let e = 0; e < 24; e++) {
  let obs = env.reset(900001 + e * 131);
  const tail = [];
  for (let s = 0; s < EPISODE_STEPS; s++) {
    const m = net.policy.forward(obs);
    for (let j = 0; j < ACT_DIM; j++) act[j] = m[j];
    const { info } = env.step(act);
    tail.push(info);
    obs = env.obs;
  }
  const t = tail.slice(-90);
  const avg = (k) => t.reduce((a, b) => a + b[k], 0) / t.length;
  rows.push({ bal: avg('balance'), hip: avg('hipRatio'), up: avg('upY') });
}
rows.sort((a, b) => a.bal - b.bal);
for (const r of rows) {
  console.log(`balance=${r.bal.toFixed(3)} 髋高比=${r.hip.toFixed(3)} 直立=${r.up.toFixed(3)}`);
}
const lo = rows.filter((r) => r.bal < 0.1);
const hi = rows.filter((r) => r.bal >= 0.1);
const m = (a, k) => (a.length ? (a.reduce((x, y) => x + y[k], 0) / a.length).toFixed(3) : '-');
console.log(`balance<0.1 (${lo.length}局) 髋高比均值 ${m(lo, 'hip')} | balance>=0.1 (${hi.length}局) ${m(hi, 'hip')}`);
env.dispose();
