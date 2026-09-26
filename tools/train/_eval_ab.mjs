/* 临时自检：同一权重下，「旧口径评估」（12 局、躺姿由 reset 随机决定）与
 * 「新口径评估」（16 局、站躺各半强制）差多少 —— 验证「斜坡躺姿 0.76 vs 0.97」
 * 是判据噪声还是策略真差。用法：node tools/train/_eval_ab.mjs <权重文件> */
import { RagdollEnv, EPISODE_STEPS, KIND_FLAT, KIND_SLOPE, KIND_VOXEL, KIND_NAMES } from './env.mjs';
import { evaluate, loadWeights } from './train.mjs';
import { ACT_DIM } from './build/ragdoll-core.mjs';

const file = process.argv[2];
const { net } = loadWeights(file);
const env = new RagdollEnv({ randomize: true });
await env.init();
env.difficulty = 1;

const agg = (a) => (a.length
  ? {
    n: a.length,
    up: a.reduce((x, y) => x + y.up, 0) / a.length,
    hip: a.reduce((x, y) => x + y.hip, 0) / a.length,
    ret: a.reduce((x, y) => x + y.ret, 0) / a.length,
  }
  : null);

function oldEval(episodes, baseSeed) {
  const res = {};
  for (const kind of [KIND_FLAT, KIND_SLOPE, KIND_VOXEL]) {
    env.forceTerrain = kind;
    env.forceLying = null;
    const out = { policy: { stand: [], lying: [] }, pd: { stand: [], lying: [] } };
    const act = new Float32Array(ACT_DIM);
    for (const mode of ['policy', 'pd']) {
      for (let e = 0; e < episodes; e++) {
        let obs = env.reset(baseSeed + e * 131);
        const lying = env.startLying;
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
        out[mode][lying ? 'lying' : 'stand'].push({ up: avg('upY'), hip: avg('hipRatio'), ret });
      }
    }
    res[KIND_NAMES[kind]] = { pl: agg(out.policy.lying), pd: agg(out.pd.lying) };
  }
  env.forceTerrain = null;
  return res;
}

const fmt = (o) => (o ? `n=${o.n} 直立${o.up.toFixed(3)} 髋${o.hip.toFixed(3)} 回报${o.ret.toFixed(0)}` : '-');

const a = oldEval(12, 900001);
for (const k of KIND_NAMES) console.log(`[旧口径 12局] ${k} 躺 策略 ${fmt(a[k].pl)} | PD ${fmt(a[k].pd)}`);

const b = evaluate(env, net, 16, 900001);
for (const k of KIND_NAMES) {
  console.log(`[新口径 16局] ${k} 躺 策略 ${fmt(b[k].lyingPolicy)} | PD ${fmt(b[k].lyingPd)}`);
}
env.dispose();
