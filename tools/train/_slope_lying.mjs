/* 临时自检：只统计躺姿开局、局数拉到 60 —— 旧口径下躺姿局只有 3 个，
 * 单局就能把均值拽走 0.1，「策略躺姿 0.76 vs PD 0.97」就是那 3 局的样子。
 * 用法：node tools/train/_slope_lying.mjs <权重文件> [局数] [flat|slope|voxel] */
import { RagdollEnv, EPISODE_STEPS, KIND_FLAT, KIND_SLOPE, KIND_VOXEL } from './env.mjs';
import { loadWeights } from './train.mjs';
import { ACT_DIM } from './build/ragdoll-core.mjs';

const file = process.argv[2];
const episodes = parseInt(process.argv[3] || '60', 10);
const terrainName = process.argv[4] || 'slope';
const TERRAINS = { flat: KIND_FLAT, slope: KIND_SLOPE, voxel: KIND_VOXEL };
const KIND = TERRAINS[terrainName];
if (KIND === undefined) throw new Error(`未知地形 ${terrainName}，可选 ${Object.keys(TERRAINS)}`);
const { net } = loadWeights(file);
const env = new RagdollEnv({ randomize: true });
await env.init();
env.difficulty = 1;
env.forceTerrain = KIND;

const act = new Float32Array(ACT_DIM);
const res = { policy: [], pd: [] };
for (const mode of ['policy', 'pd']) {
  for (let e = 0; e < episodes; e++) {
    let obs = env.reset(900001 + e * 131);
    if (!env.startLying) continue;
    const tail = [];
    let ret = 0;
    for (let s = 0; s < EPISODE_STEPS; s++) {
      if (mode === 'policy') {
        const m = net.policy.forward(obs);
        for (let j = 0; j < ACT_DIM; j++) act[j] = m[j];
      } else act.fill(0);
      const { reward, info } = env.step(act);
      ret += reward;
      tail.push(info);
      if (tail.length > 90) tail.shift();
      obs = env.obs;
    }
    const avg = (k) => tail.reduce((a, b) => a + b[k], 0) / tail.length;
    res[mode].push({ up: avg('upY'), hip: avg('hipRatio'), ret });
  }
}

for (const mode of ['policy', 'pd']) {
  const a = res[mode];
  if (!a.length) { console.log(`${mode} 无躺姿局`); continue; }
  const mean = (k) => a.reduce((x, y) => x + y[k], 0) / a.length;
  const sd = (k) => {
    const m = mean(k);
    return Math.sqrt(a.reduce((x, y) => x + (y[k] - m) ** 2, 0) / a.length);
  };
  const worst = Math.min(...a.map((x) => x.up));
  console.log(`[${terrainName}] ${mode} n=${a.length} 直立 ${mean('up').toFixed(3)}±${sd('up').toFixed(3)} (最差 ${worst.toFixed(3)}) 髋 ${mean('hip').toFixed(3)} 回报 ${mean('ret').toFixed(0)}`);
}

// 配对差：两个 mode 用同一批 seed，reset(seed) 确定 → 同一局一一对应。
// 不配对比均值会把「这一局本身就难」算进策略的账上，n=28 时能差出一倍 SE。
const n = Math.min(res.policy.length, res.pd.length);
if (n) {
  const diff = [];
  for (let i = 0; i < n; i++) diff.push(res.policy[i].up - res.pd[i].up);
  const m = diff.reduce((a, b) => a + b, 0) / n;
  const se = Math.sqrt(diff.reduce((a, b) => a + (b - m) ** 2, 0) / n / n);
  console.log(`[${terrainName}] 配对差(策略-PD) 直立 ${m >= 0 ? '+' : ''}${m.toFixed(3)} ±${se.toFixed(3)} (t=${(m / se).toFixed(2)}, n=${n})`);
}
env.dispose();
