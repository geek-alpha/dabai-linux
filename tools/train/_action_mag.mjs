/* 临时自检：策略到底在「动」还是在「不动」。
 * 动作是 PD 之上的残差，零 = 原 PD 行为 —— 若 |a| 常年贴地，那策略等于把控制权
 * 还给了 PD，评估里「策略≈PD」就不是巧合，而是没学到东西的直接证据。
 * 用法：node tools/train/_action_mag.mjs <权重文件> [局数] */
import { RagdollEnv, EPISODE_STEPS, KIND_FLAT, KIND_SLOPE, KIND_VOXEL } from './env.mjs';
import { loadWeights } from './train.mjs';
import { ACT_DIM, ACTION_LIMIT, OBS_DIM_FULL, PART_COUNT, OBS_PER_PART, ROOT_OBS, TERRAIN_OBS } from './build/ragdoll-core.mjs';

const file = process.argv[2];
const episodes = parseInt(process.argv[3] || '8', 10);
const { net } = loadWeights(file);
const env = new RagdollEnv({ randomize: true });
await env.init();
env.difficulty = 1;

const act = new Float32Array(ACT_DIM);
for (const [name, KIND] of [['flat', KIND_FLAT], ['slope', KIND_SLOPE], ['voxel', KIND_VOXEL]]) {
  env.forceTerrain = KIND;
  for (const lying of [false, true]) {
    env.forceLying = lying;
    const mags = [];
    const absMax = [];
    // 探针消融：同一帧，把观测里探针那 16 维置零再前向一次。差值 = 网络实际
    // 用了多少地形信息。为 0 就说明这 16 维是白加的，也就解释了探针版为何没收益。
    let probeDelta = 0, probeBase = 0, probeAbs = 0, probeN = 0;
    const zeroed = new Float32Array(OBS_DIM_FULL);
    const a1 = new Float32Array(ACT_DIM);
    const PROBE_AT = PART_COUNT * OBS_PER_PART + ROOT_OBS;
    for (let e = 0; e < episodes; e++) {
      let obs = env.reset(900001 + e * 131);
      let sum = 0, n = 0, peak = 0;
      for (let s = 0; s < EPISODE_STEPS; s++) {
        const m = net.policy.forward(obs);
        // MLP.forward 返回的是内部缓冲（nn.mjs:52），不拷走的话下一次前向会把
        // 上一次的结果覆盖掉 —— 消融差会永远读成 0，看上去像「探针没用」
        for (let j = 0; j < ACT_DIM; j++) a1[j] = m[j];
        for (let i = 0; i < TERRAIN_OBS; i++) { probeAbs += Math.abs(obs[PROBE_AT + i]); probeN++; }
        zeroed.set(obs);
        zeroed.fill(0, PROBE_AT, PROBE_AT + TERRAIN_OBS);
        const m2 = net.policy.forward(zeroed);
        for (let j = 0; j < ACT_DIM; j++) {
          act[j] = a1[j];
          const a = Math.abs(a1[j]);
          sum += a; n++;
          if (a > peak) peak = a;
          probeDelta += Math.abs(a1[j] - m2[j]);
          probeBase += a;
        }
        env.step(act);
        obs = env.obs;
      }
      mags.push(sum / n);
      absMax.push(peak);
    }
    const mean = mags.reduce((a, b) => a + b, 0) / mags.length;
    const peak = absMax.reduce((a, b) => a + b, 0) / absMax.length;
    const frac = (mean / ACTION_LIMIT) * 100;
    console.log(
      `[${name}] ${lying ? '躺' : '站'} 平均|a| ${mean.toFixed(4)} rad = 限幅的 ${frac.toFixed(1)}%` +
      ` | 峰值|a| ${peak.toFixed(3)} | 探针读数|p| ${(probeAbs / probeN).toFixed(3)}` +
      ` | 探针消融 Δ|a| ${(probeDelta / probeBase * 100).toFixed(2)}%`
    );
  }
}
env.dispose();
