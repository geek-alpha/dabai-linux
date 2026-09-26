/* ============================================================
 * 手写 MLP + Adam —— 不引 torch，Node 里直接训
 * ------------------------------------------------------------
 * 这台机器 2 核 1G 内存，装 torch 就得几百 MB，训练还得另起进程
 * 传数据。策略网络只有 4 万参数，纯 JS 手写前向/反向完全够快
 * （单样本前向 ~20µs），而且训练和前端推理用同一份权重布局，
 * 导出即用，中间不存在任何格式转换环节。
 *
 * 参数扁平存在一个 Float32Array 里：前 nW 个是权重（按层拼接），
 * 后面是偏置。序列化时直接存数组，前端 setWeights 一次性灌进去。
 * ============================================================ */

/** 均匀分布初始化（Xavier 简化版：按扇入缩放） */
function initUniform(arr, from, to, fanIn, rng) {
  const lim = Math.sqrt(3 / fanIn);
  for (let i = from; i < to; i++) arr[i] = (rng() * 2 - 1) * lim;
}

export class MLP {
  /**
   * @param sizes 各层宽度，如 [144, 128, 128, 45]
   * @param rng 0~1 随机函数
   */
  constructor(sizes, rng, dtype = Float32Array) {
    this.sizes = sizes;
    this.dtype = dtype;
    const L = sizes.length;
    this.layerW = new Array(L).fill(0);
    this.layerB = new Array(L).fill(0);
    let nW = 0, nB = 0;
    for (let l = 1; l < L; l++) {
      this.layerW[l] = nW; nW += sizes[l - 1] * sizes[l];
      this.layerB[l] = nB; nB += sizes[l];
    }
    this.nW = nW;
    this.nParams = nW + nB;
    this.params = new dtype(this.nParams);
    this.grad = new dtype(this.nParams);
    this.m = new dtype(this.nParams);
    this.v = new dtype(this.nParams);
    this.t = 0;

    for (let l = 1; l < L; l++) {
      initUniform(this.params, this.layerW[l], this.layerW[l] + sizes[l - 1] * sizes[l], sizes[l - 1], rng);
      initUniform(this.params, nW + this.layerB[l], nW + this.layerB[l] + sizes[l], sizes[l - 1], rng);
    }

    this.acts = sizes.map((s) => new dtype(s));
    this.dActs = sizes.map((s) => new dtype(s));
  }

  /** 前向；返回最后一层激活（内部缓冲，勿长期持有） */
  forward(x) {
    const S = this.sizes, L = S.length - 1;
    this.acts[0].set(x.subarray(0, S[0]));
    for (let l = 1; l <= L; l++) {
      const nin = S[l - 1], nout = S[l];
      const prev = this.acts[l - 1], cur = this.acts[l];
      const wOff = this.layerW[l], bOff = this.nW + this.layerB[l];
      const last = l === L;
      for (let j = 0; j < nout; j++) {
        let s = this.params[bOff + j];
        const row = wOff + j * nin;
        for (let i = 0; i < nin; i++) s += this.params[row + i] * prev[i];
        cur[j] = last ? s : Math.tanh(s);
      }
    }
    return this.acts[L];
  }

  /** 反向：dOut 是损失对网络输出的梯度，累加到 this.grad */
  backward(dOut) {
    const S = this.sizes, L = S.length - 1;
    this.dActs[L].set(dOut);
    for (let l = L; l >= 1; l--) {
      const nin = S[l - 1], nout = S[l];
      const prev = this.acts[l - 1], dCur = this.dActs[l], dPrev = this.dActs[l - 1];
      dPrev.fill(0);
      const wOff = this.layerW[l], bOff = this.nW + this.layerB[l];
      for (let j = 0; j < nout; j++) {
        const g = dCur[j];
        if (g === 0) continue;
        this.grad[bOff + j] += g;
        const row = wOff + j * nin;
        for (let i = 0; i < nin; i++) {
          this.grad[row + i] += g * prev[i];
          dPrev[i] += g * this.params[row + i];
        }
      }
      // 前一层的 tanh 导数（l-1 == 0 是输入层，没有激活）
      if (l > 1) {
        for (let i = 0; i < nin; i++) {
          const a = prev[i];
          dPrev[i] *= 1 - a * a;
        }
      }
    }
  }

  zeroGrad() { this.grad.fill(0); }

  /** 按梯度范数裁剪后 Adam 更新 */
  step(lr, clip = 1.0, beta1 = 0.9, beta2 = 0.999, eps = 1e-8) {
    let sq = 0;
    for (let i = 0; i < this.nParams; i++) sq += this.grad[i] * this.grad[i];
    const norm = Math.sqrt(sq);
    const scale = norm > clip ? clip / norm : 1;
    this.t++;
    const c1 = 1 - Math.pow(beta1, this.t);
    const c2 = 1 - Math.pow(beta2, this.t);
    for (let i = 0; i < this.nParams; i++) {
      const g = this.grad[i] * scale;
      this.m[i] = beta1 * this.m[i] + (1 - beta1) * g;
      this.v[i] = beta2 * this.v[i] + (1 - beta2) * g * g;
      this.params[i] -= lr * (this.m[i] / c1) / (Math.sqrt(this.v[i] / c2) + eps);
    }
  }
}

/* ---------------- 数值梯度自检 ---------------- */

/**
 * 用中心差分校验反向传播。写梯度代码最怕「看起来收敛了但梯度是错的」，
 * 所以这一步必须跑过才敢开训。
 * @returns {{ maxRelErr:number, ok:boolean, detail:object }}
 */
export function gradCheck(sizes = [5, 7, 6, 4], seed = 12345, eps = 1e-6) {
  let s = seed;
  const rng = () => {
    s = (s * 1103515245 + 12345) & 0x7fffffff;
    return s / 0x7fffffff;
  };
  // 用 float64 做校验：float32 的差分噪声本身就能到 1e-3，会把真 bug 和
  // 精度噪声混在一起，分不出对错
  const net = new MLP(sizes, rng, Float64Array);
  const x = new Float64Array(sizes[0]);
  for (let i = 0; i < x.length; i++) x[i] = rng() * 2 - 1;

  // 损失 = sum(w_i * out_i²)，w 用伪随机权重，避免某些分量恰好抵消
  const w = new Float64Array(sizes[sizes.length - 1]);
  for (let i = 0; i < w.length; i++) w[i] = rng() * 2 - 1;

  const lossAt = () => {
    const out = net.forward(x);
    let l = 0;
    for (let i = 0; i < w.length; i++) l += w[i] * out[i] * out[i];
    return l;
  };

  net.zeroGrad();
  const out = net.forward(x);
  const dOut = new Float64Array(w.length);
  for (let i = 0; i < w.length; i++) dOut[i] = 2 * w[i] * out[i];
  net.backward(dOut);

  const analytic = Float64Array.from(net.grad);
  let maxRel = 0, worst = -1;
  const samples = [];
  const rels = [];
  const N = net.nParams;
  const idxs = [];
  for (let k = 0; k < 40; k++) idxs.push(Math.floor(rng() * N));
  for (const i of idxs) {
    const orig = net.params[i];
    net.params[i] = orig + eps;
    const lp = lossAt();
    net.params[i] = orig - eps;
    const lm = lossAt();
    net.params[i] = orig;
    const num = (lp - lm) / (2 * eps);
    const absErr = Math.abs(num - analytic[i]);
    // 分母带下限：梯度本身接近 0 时相对误差会被噪声放大，那是数值问题不是代码问题
    const rel = absErr / Math.max(1e-3, Math.abs(num), Math.abs(analytic[i]));
    rels.push(rel);
    if (samples.length < 3) samples.push({ i, num: +num.toFixed(6), ana: +analytic[i].toFixed(6), abs: +absErr.toExponential(2) });
    if (rel > maxRel) { maxRel = rel; worst = i; }
  }
  rels.sort((a, b) => a - b);
  const median = rels[rels.length >> 1];
  return { maxRelErr: maxRel, medianRelErr: median, ok: median < 1e-4 && maxRel < 1e-2, detail: { worst, nParams: N, checked: idxs.length, samples } };
}
