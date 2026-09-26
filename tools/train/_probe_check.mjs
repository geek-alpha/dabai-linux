/* 临时自检：地形探针是否真读到地形、躺姿局奖励增量多大。跑完即删。 */
import { RagdollEnv, KIND_FLAT, KIND_SLOPE, KIND_VOXEL } from './env.mjs';
import { OBS_DIM, TERRAIN_OBS } from './build/ragdoll-core.mjs';

const env = new RagdollEnv({ randomize: false });
await env.init();

const stat = (arr) => {
  let mn = 1e9, mx = -1e9, s = 0;
  for (const v of arr) { mn = Math.min(mn, v); mx = Math.max(mx, v); s += Math.abs(v); }
  return `min=${mn.toFixed(3)} max=${mx.toFixed(3)} |sum|=${s.toFixed(3)}`;
};

for (const kind of [KIND_FLAT, KIND_SLOPE, KIND_VOXEL]) {
  env.forceTerrain = kind;
  env.difficulty = 1;
  const obs = env.reset(12345);
  const probe = Array.from(obs.slice(OBS_DIM - TERRAIN_OBS, OBS_DIM));
  console.log(`[reset] kind=${kind} 探针 ${stat(probe)}`);
  let last = probe;
  for (let i = 0; i < 40; i++) {
    const act = new Float32Array(45);
    const { info } = env.step(act);
    last = Array.from(env.obs.slice(OBS_DIM - TERRAIN_OBS, OBS_DIM));
    if (i === 39) {
      const add = 2.0 * Math.max(0, Math.min(1, (info.upY + 1) * 0.5)) + 1.5 * Math.max(0, Math.min(1, info.hipRatio));
      console.log(`[step40] kind=${kind} lying=${info.lying} upY=${info.upY.toFixed(3)} hipRatio=${info.hipRatio.toFixed(3)} 探针 ${stat(last)} 躺姿追加奖励=${add.toFixed(3)}`);
    }
  }
}
env.dispose();
