/* ============================================================
 * PPO 训练器 —— 在浏览器同款物理上，用手写网络训一个布娃娃控制器
 * ------------------------------------------------------------
 * 策略：π(a|s) = N(mean(s), exp(logStd))，mean 来自 3 层 MLP(tanh)
 * 动作：15 段 × 3 维旋转残差，叠在 PD 驱动目标上（零动作 = 原 PD 行为）
 * 价值：独立 MLP 头，只在训练时存在，导出时丢掉
 *
 * 奖励与任务定义在 env.mjs 里；这里只管把轨迹变成梯度。
 * 用法：
 *   node tools/train/train.mjs --steps 1000000
 *   node tools/train/train.mjs --eval-only          # 只评估已有权重
 * ============================================================ */

import { writeFileSync, mkdirSync, readFileSync, existsSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { RagdollEnv, EPISODE_STEPS, KIND_FLAT, KIND_SLOPE, KIND_VOXEL, KIND_NAMES } from './env.mjs';
import { MLP, gradCheck } from './nn.mjs';
import { OBS_DIM_FULL, ACT_DIM, ACTION_LIMIT } from './build/ragdoll-core.mjs';

const here = path.dirname(fileURLToPath(import.meta.url));
const OUT_DIR = path.join(here, '..', '..', 'web', 'generated', 'policy');
let OUT_FILE = path.join(OUT_DIR, 'ragdoll-v1.json');

/** 换输出文件：重训产生的新权重另存一份，别把线上在用的 v1 覆盖掉 ——
 *  覆盖后想回退只能翻备份，而前端读的就是这个路径。 */
export function setOutFile(p) {
  OUT_FILE = path.resolve(p);
  return OUT_FILE;
}

export const CFG = {
  hidden: [64, 64],
  stepsPerIter: 4096,
  epochs: 4,
  minibatch: 512,
  gamma: 0.99,
  lambda: 0.95,
  clip: 0.2,
  lr: 3e-4,
  lrEnd: 5e-5,
  entropyCoef: 0.002,
  /** 熵系数退火终点：σ 一直卡在 0.25 不降，策略整轮都在 ±0.26 弧度的噪声里
   *  采样，优势信号被自己搅浑，mean 永远收敛不到「零残差」 */
  entropyEnd: 0.0002,
  valueCoef: 0.5,
  initLogStd: Math.log(0.25),
  minLogStd: -3.2,
  maxLogStd: 0.7,
  totalSteps: 1_000_000,
  seed: 20260924,
  evalEvery: 15,
};

const LOG2PI_HALF = 0.5 * Math.log(2 * Math.PI);

function makeRng(seed) {
  let s = seed >>> 0;
  return () => {
    s = (s * 1664525 + 1013904223) >>> 0;
    return s / 4294967296;
  };
}

/** 高斯噪声（Box-Muller，缓存第二个样本） */
function makeGauss(rng) {
  let spare = null;
  return () => {
    if (spare !== null) { const v = spare; spare = null; return v; }
    let u = 0, v = 0, s = 0;
    do { u = rng() * 2 - 1; v = rng() * 2 - 1; s = u * u + v * v; } while (s >= 1 || s === 0);
    const m = Math.sqrt(-2 * Math.log(s) / s);
    spare = v * m;
    return u * m;
  };
}

/** 输出层清零：让初始策略恰等于纯 PD 基线（零残差）。
 *  随机初始化时 64 维隐层 × rms 0.12 会给出约 0.3 弧度的残差，开局就把 PD
 *  拖垮；策略得先学会「不动」才谈得上改进 —— 实测 100 万步都没学会，回报
 *  只有 PD 的一半。清零后初始动作恰为 0，梯度只负责改进。 */
function zeroInitOutput(net) {
  const L = net.sizes.length - 1;
  const nin = net.sizes[L - 1], nout = net.sizes[L];
  net.params.fill(0, net.layerW[L], net.layerW[L] + nin * nout);
  net.params.fill(0, net.nW + net.layerB[L], net.nW + net.layerB[L] + nout);
}

export function createNets(rng) {
  const policy = new MLP([OBS_DIM_FULL, ...CFG.hidden, ACT_DIM], rng);
  const value = new MLP([OBS_DIM_FULL, ...CFG.hidden, 1], rng);
  const logStd = new Float64Array(ACT_DIM).fill(CFG.initLogStd);
  zeroInitOutput(policy);
  return { policy, value, logStd };
}

/** 策略前向：返回均值（policy.acts 最后一层的引用） */
function policyMean(net, obs) {
  return net.policy.forward(obs);
}

/* ---------------- 采样 ---------------- */

export class Rollout {
  constructor(net, rng) {
    this.net = net;
    this.gauss = makeGauss(rng);
    const N = CFG.stepsPerIter;
    this.obs = new Float32Array(N * OBS_DIM_FULL);
    this.act = new Float32Array(N * ACT_DIM);
    this.logp = new Float32Array(N);
    this.rew = new Float32Array(N);
    this.val = new Float32Array(N);
    this.done = new Uint8Array(N);
    this.mean = new Float32Array(ACT_DIM);
    this.action = new Float32Array(ACT_DIM);
    this.dMean = new Float32Array(ACT_DIM);
    this.adv = new Float32Array(N);
    this.ret = new Float32Array(N);
    this.N = N;
  }

  /** 采一轮轨迹；env 在 done 时自动换局 */
  collect(env, startSeed) {
    const N = this.N, net = this.net;
    let obs = env.reset(startSeed);
    let seed = startSeed;
    let epRew = 0, epLen = 0;
    const epStats = [];
    const logStd = net.logStd;

    for (let i = 0; i < N; i++) {
      this.obs.set(obs, i * OBS_DIM_FULL);
      const mean = policyMean(net, obs);
      let lp = 0;
      for (let j = 0; j < ACT_DIM; j++) {
        const ls = logStd[j];
        const sd = Math.exp(ls);
        const a = mean[j] + sd * this.gauss();
        this.act[i * ACT_DIM + j] = a;
        const z = (a - mean[j]) / sd;
        lp += -0.5 * z * z - ls - LOG2PI_HALF;
      }
      this.logp[i] = lp;
      this.val[i] = net.value.forward(obs)[0];

      for (let j = 0; j < ACT_DIM; j++) this.action[j] = this.act[i * ACT_DIM + j];
      const { reward, done, info } = env.step(this.action);
      this.rew[i] = reward;
      this.done[i] = done ? 1 : 0;
      epRew += reward; epLen++;
      if (done) {
        epStats.push({ rew: epRew, len: epLen, hip: info.hipRatio, up: info.upY, ang: info.angErr, lying: info.lying });
        epRew = 0; epLen = 0;
        seed++;
        obs = env.reset(seed);
      } else {
        obs = env.obs;
      }
    }
    this.nextSeed = seed;
    this.lastEp = epStats;
    // 最后一步的 bootstrap 价值
    this.lastValue = net.value.forward(obs)[0];
    return epStats;
  }

  /** GAE-λ 优势估计 */
  computeGae() {
    const { gamma, lambda } = CFG;
    let lastAdv = 0;
    for (let i = this.N - 1; i >= 0; i--) {
      const nonTerm = this.done[i] ? 0 : 1;
      const nextV = this.done[i] ? 0 : (i + 1 < this.N ? this.val[i + 1] : this.lastValue);
      const delta = this.rew[i] + gamma * nextV - this.val[i];
      lastAdv = delta + gamma * lambda * nonTerm * lastAdv;
      this.adv[i] = lastAdv;
      this.ret[i] = lastAdv + this.val[i];
    }
    let sum = 0, sq = 0;
    for (let i = 0; i < this.N; i++) sum += this.adv[i];
    const mean = sum / this.N;
    for (let i = 0; i < this.N; i++) { const d = this.adv[i] - mean; sq += d * d; }
    const std = Math.sqrt(sq / this.N) + 1e-8;
    for (let i = 0; i < this.N; i++) this.adv[i] = (this.adv[i] - mean) / std;
  }
}

/* ---------------- 训练 ---------------- */

export function trainIteration(env, net, roll, order, lr) {
  const { policy, value, logStd } = net;
  const { epochs, minibatch, clip, valueCoef, entropyCoef } = CFG;
  const N = roll.N;
  const mbCount = Math.floor(N / minibatch);

  // logStd 的 Adam 状态
  if (!net.lsM) { net.lsM = new Float64Array(ACT_DIM); net.lsV = new Float64Array(ACT_DIM); net.lsT = 0; }
  const dLogStd = new Float64Array(ACT_DIM);

  let policyLoss = 0, valueLoss = 0, klSum = 0, clipFrac = 0, nSamples = 0;
  const dvBuf = new Float32Array(1);

  for (let ep = 0; ep < epochs; ep++) {
    // Fisher-Yates 打乱
    for (let i = N - 1; i > 0; i--) {
      const j = Math.floor(order.rng() * (i + 1));
      const t = order.arr[i]; order.arr[i] = order.arr[j]; order.arr[j] = t;
    }
    for (let mb = 0; mb < mbCount; mb++) {
      policy.zeroGrad(); value.zeroGrad();
      dLogStd.fill(0);
      const base = mb * minibatch;
      for (let k = 0; k < minibatch; k++) {
        const i = order.arr[base + k];
        const obsOff = i * OBS_DIM_FULL;
        const actOff = i * ACT_DIM;
        const obs = roll.obs.subarray(obsOff, obsOff + OBS_DIM_FULL);
        const mean = policy.forward(obs);
        let lp = 0;
        for (let j = 0; j < ACT_DIM; j++) {
          const ls = logStd[j];
          const sd = Math.exp(ls);
          const z = (roll.act[actOff + j] - mean[j]) / sd;
          lp += -0.5 * z * z - ls - LOG2PI_HALF;
        }
        const adv = roll.adv[i];
        const ratio = Math.exp(lp - roll.logp[i]);
        const clipped = ratio < 1 - clip ? 1 - clip : ratio > 1 + clip ? 1 + clip : ratio;
        const useUnclipped = ratio * adv <= clipped * adv;
        const g = useUnclipped ? -adv * ratio : 0;   // dL/dlogπ
        if (!useUnclipped) clipFrac++;
        klSum += roll.logp[i] - lp;
        nSamples++;

        for (let j = 0; j < ACT_DIM; j++) {
          const ls = logStd[j];
          const sd = Math.exp(ls);
          const z = (roll.act[actOff + j] - mean[j]) / sd;
          // 熵 H = logσ + const，对 logσ 的导数是 1（与样本无关）；
          // 对 mean 的导数为 0，所以熵项只出现在 logStd 上
          roll.dMean[j] = g * z / sd;
          dLogStd[j] += g * (z * z - 1) - entropyCoef;
        }
        policy.backward(roll.dMean);

        const v = value.forward(obs)[0];
        dvBuf[0] = valueCoef * Math.max(-10, Math.min(10, v - roll.ret[i]));
        value.backward(dvBuf);
        valueLoss += Math.abs(dvBuf[0]);
        policyLoss += useUnclipped ? -ratio * adv : -clipped * adv;
      }
      policy.step(lr);
      value.step(lr);

      // logStd 的 Adam
      net.lsT++;
      const c1 = 1 - Math.pow(0.9, net.lsT);
      const c2 = 1 - Math.pow(0.999, net.lsT);
      for (let j = 0; j < ACT_DIM; j++) {
        const g = dLogStd[j] / minibatch;
        net.lsM[j] = 0.9 * net.lsM[j] + 0.1 * g;
        net.lsV[j] = 0.999 * net.lsV[j] + 0.001 * g * g;
        logStd[j] -= lr * (net.lsM[j] / c1) / (Math.sqrt(net.lsV[j] / c2) + 1e-8);
        if (logStd[j] < CFG.minLogStd) logStd[j] = CFG.minLogStd;
        else if (logStd[j] > CFG.maxLogStd) logStd[j] = CFG.maxLogStd;
      }
    }
  }
  return {
    policyLoss: policyLoss / nSamples,
    valueLoss: valueLoss / nSamples,
    kl: klSum / nSamples,
    clipFrac: clipFrac / nSamples,
    meanStd: Math.exp(Array.from(logStd).reduce((a, b) => a + b, 0) / ACT_DIM),
  };
}

/* ---------------- 评估 ---------------- */

/**
 * 分地形评估：同一批 seed 下，策略与 PD 在平地/楼梯坡/体素地形上各跑一遍。
 * 地形必须分开报 —— 合成一个平均数会把「策略在台阶上救回来」和「PD 在平地上
 * 本来就很强」混成一个看不出差别的数字。
 */
export function evaluate(env, net, episodes = 10, baseSeed = 900001) {
  const prevForce = env.forceTerrain;
  const prevDiff = env.difficulty;
  // 评估永远用满难度：否则课程早期的「好成绩」是地形还没长起来换来的，
  // 跨阶段根本没法比，也会让选权重的分数挑中一个其实很弱的策略
  env.difficulty = 1;
  const result = {};
  try {
    for (const kind of [KIND_FLAT, KIND_SLOPE, KIND_VOXEL]) {
      env.forceTerrain = kind;
      const out = { policy: { stand: [], lying: [] }, pd: { stand: [], lying: [] } };
      const act = new Float32Array(ACT_DIM);
      // 站姿/躺姿各跑一半：躺姿局数交给 reset 的随机概率时，4 局里往往只有
      // 1~2 局躺姿 —— 「斜坡躺姿直立 0.75 还是 0.76」这种判据其实是 1 局的噪声
      const perMode = Math.max(1, Math.round(episodes / 2));
      for (const mode of ['policy', 'pd']) {
        for (const startLying of [false, true]) {
          env.forceLying = startLying;
          for (let e = 0; e < perMode; e++) {
            let obs = env.reset(baseSeed + e * 131);
            const lying = env.startLying;
          const tail = [];
          let ret = 0;
          for (let s = 0; s < EPISODE_STEPS; s++) {
            if (mode === 'policy') {
              const m = policyMean(net, obs);
              for (let j = 0; j < ACT_DIM; j++) act[j] = m[j];
            } else {
              act.fill(0);
            }
            const { reward, info } = env.step(act);
            ret += reward;
            tail.push(info);
            if (tail.length > 90) tail.shift();
            obs = env.obs;
          }
          const avg = (k) => tail.reduce((a, b) => a + b[k], 0) / tail.length;
            out[mode][lying ? 'lying' : 'stand'].push({ hip: avg('hipRatio'), up: avg('upY'), ang: avg('angErr'), ret });
          }
        }
      }
      const agg = (arr) => {
        if (!arr.length) return null;
        return {
          n: arr.length,
          hip: arr.reduce((a, b) => a + b.hip, 0) / arr.length,
          up: arr.reduce((a, b) => a + b.up, 0) / arr.length,
          ang: arr.reduce((a, b) => a + b.ang, 0) / arr.length,
          ret: arr.reduce((a, b) => a + b.ret, 0) / arr.length,
        };
      };
      result[KIND_NAMES[kind]] = {
        standPolicy: agg(out.policy.stand), standPd: agg(out.pd.stand),
        lyingPolicy: agg(out.policy.lying), lyingPd: agg(out.pd.lying),
      };
    }
  } finally {
    env.forceTerrain = prevForce;
    env.forceLying = null;
    env.difficulty = prevDiff;
  }
  return result;
}

/* ---------------- 权重持久化 ---------------- */

export function saveWeights(net, meta) {
  mkdirSync(OUT_DIR, { recursive: true });
  const payload = {
    version: 1,
    kind: 'ragdoll-policy',
    obsDim: OBS_DIM_FULL,
    actDim: ACT_DIM,
    partCount: 15,
    actionLimit: ACTION_LIMIT,
    sizes: net.policy.sizes,
    weights: Array.from(net.policy.params, (v) => +v.toFixed(6)),
    logStd: Array.from(net.logStd, (v) => +v.toFixed(5)),
    meta,
  };
  writeFileSync(OUT_FILE, JSON.stringify(payload));
  return OUT_FILE;
}

export function loadWeights(file = OUT_FILE) {
  if (!existsSync(file)) return null;
  const p = JSON.parse(readFileSync(file, 'utf8'));
  const rng = makeRng(1);
  const net = createNets(rng);
  net.policy.params.set(p.weights);
  net.logStd.set(p.logStd);
  return { net, meta: p.meta, payload: p };
}

/* ---------------- 主入口 ---------------- */

async function main() {
  const args = process.argv.slice(2);
  const getArg = (name, def) => {
    const i = args.indexOf(name);
    return i >= 0 && args[i + 1] ? args[i + 1] : def;
  };
  const has = (name) => args.includes(name);

  if (has('--gradcheck')) {
    const r = gradCheck([OBS_DIM_FULL, ...CFG.hidden, ACT_DIM]);
    console.log('梯度自检:', JSON.stringify(r.detail), '中位相对误差', r.medianRelErr.toExponential(2), r.ok ? '✅' : '❌');
    if (!r.ok) process.exit(1);
    return;
  }

  const totalSteps = parseInt(getArg('--steps', String(CFG.totalSteps)), 10);
  const logEvery = parseInt(getArg('--log-every', '20'), 10);
  const outArg = getArg('--out', '');
  if (outArg) console.log('输出权重 →', setOutFile(outArg));

  const env = new RagdollEnv({ randomize: true });
  await env.init();

  const rng = makeRng(CFG.seed);
  let net, startStep = 0;
  if (has('--resume')) {
    const loaded = loadWeights();
    if (loaded) {
      net = loaded.net;
      startStep = loaded.meta?.steps || 0;
      console.log(`续训：从 ${startStep} 步继续`);
    }
  }
  if (!net) net = createNets(rng);

  const fmt = (o) => o ? `髋${o.hip.toFixed(2)} 直立${o.up.toFixed(2)} 角${o.ang.toFixed(2)} 回报${o.ret.toFixed(0)}` : '-';
  const printEval = (ev) => {
    for (const name of KIND_NAMES) {
      const t = ev[name];
      if (!t) continue;
      console.log(`   └ [${name}] 策略 站 ${fmt(t.standPolicy)} | 躺 ${fmt(t.lyingPolicy)}`);
      console.log(`   └ [${name}] PD   站 ${fmt(t.standPd)} | 躺 ${fmt(t.lyingPd)}`);
    }
  };

  if (has('--eval-only')) {
    // 必须显式加载：以前这里漏了这一步，net 还是刚 createNets 的零初始化输出，
    // 策略恒输出零残差 —— 评估出来的「策略」其实是 PD 自己，两边数据逐位相同。
    const loaded = loadWeights();
    if (loaded) {
      net = loaded.net;
      console.log(`评估权重：${loaded.meta?.steps ?? '未知'} 步`);
    } else {
      console.log('未找到权重文件，评估的是零初始化策略（等价 PD）');
    }
    const ev = evaluate(env, net, 16);
    printEval(ev);
    console.log('评估:', JSON.stringify(ev));
    return;
  }

  const roll = new Rollout(net, rng);
  const order = { arr: new Uint32Array(roll.N), rng };
  for (let i = 0; i < roll.N; i++) order.arr[i] = i;

  const iters = Math.ceil(totalSteps / CFG.stepsPerIter);
  const ENT_START = CFG.entropyCoef;
  let seed = 1000 + startStep;
  const t0 = Date.now();
  let bestRet = -1e9;

  for (let it = 0; it < iters; it++) {
    const frac = (it * CFG.stepsPerIter) / totalSteps;
    const lr = CFG.lr + (CFG.lrEnd - CFG.lr) * Math.min(1, frac);
    CFG.entropyCoef = ENT_START + (CFG.entropyEnd - ENT_START) * Math.min(1, frac);
    // 课程学习：地形难度从 0 拉到 1。一上来就是 40cm 落差的方块地形 + 弱扭矩
    // + 躺姿开局，策略连「站着不动」都学不出来 —— 实测 60 万步回报从 1293 退到 969，
    // 评估里髋高比只剩 0.36。先平地站稳，再逐步加大落差，能力是堆上去的。
    env.difficulty = Math.min(1, frac * 1.6);
    const epStats = roll.collect(env, seed);
    seed = roll.nextSeed;
    roll.computeGae();
    const info = trainIteration(env, net, roll, order, lr);

    if (it % logEvery === 0 || it === iters - 1) {
      const avg = (k) => epStats.length ? (epStats.reduce((a, b) => a + b[k], 0) / epStats.length) : 0;
      const stand = epStats.filter((e) => !e.lying), lying = epStats.filter((e) => e.lying);
      const seg = (arr, k) => arr.length ? arr.reduce((a, b) => a + b[k], 0) / arr.length : 0;
      const stepsDone = startStep + (it + 1) * CFG.stepsPerIter;
      const el = (Date.now() - t0) / 1000;
      console.log(
        `[${String(stepsDone).padStart(8)}] 局${epStats.length} 回报 ${avg('rew').toFixed(1)} | ` +
        `髋高比 站${seg(stand, 'hip').toFixed(2)}/躺${seg(lying, 'hip').toFixed(2)} | ` +
        `角误差 ${avg('ang').toFixed(2)} | KL ${info.kl.toFixed(3)} clip ${(info.clipFrac * 100).toFixed(0)}% σ ${info.meanStd.toFixed(2)} | ` +
        `${(stepsDone / el).toFixed(0)} 步/秒`
      );
    }

    if (it % CFG.evalEvery === 0 || it === iters - 1) {
      const ev = evaluate(env, net, 8);
      printEval(ev);
      // 分数：髋高与直立度越高越好、角误差越低越好；三种地形、两种开局都要兼顾
      const sc = (o) => o ? o.hip + o.up * 0.5 - o.ang * 0.35 : -9;
      let score = 0;
      for (const name of KIND_NAMES) {
        const t = ev[name];
        if (!t) continue;
        score += sc(t.lyingPolicy) + sc(t.standPolicy);
      }
      if (score > bestRet) {
        bestRet = score;
        saveWeights(net, { steps: startStep + (it + 1) * CFG.stepsPerIter, score, eval: ev, ts: new Date().toISOString() });
      }
    }
  }

  const finalEv = evaluate(env, net, 16);
  const file = saveWeights(net, { steps: startStep + iters * CFG.stepsPerIter, eval: finalEv, ts: new Date().toISOString() });
  console.log('训练完成 →', file);
  console.log('最终评估:', JSON.stringify(finalEv, null, 1));
  env.dispose();
}

// 只有被当脚本直接跑才进 main：被 import 时（方差/口径自检脚本）只导出 evaluate、loadWeights
if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main().catch((e) => { console.error(e); process.exit(1); });
}
