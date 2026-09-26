/* ============================================================
 * 策略网络前向（浏览器侧）
 * ------------------------------------------------------------
 * 权重由 tools/train/train.mjs 训练后写成 JSON：参数按层拼接在一个
 * 扁平数组里，前 nW 个是权重、后面是偏置 —— 与训练侧 MLP 的内存布局
 * 逐字节一致，所以这里只需要一个双层循环，不存在任何格式转换。
 *
 * 只做前向：推理时不需要 logStd（那是训练时的探索噪声），
 * 但保留在 payload 里，方便复现训练时的分布。
 * ============================================================ */

export interface PolicyPayload {
  version: number;
  kind: string;
  obsDim: number;
  actDim: number;
  partCount: number;
  actionLimit: number;
  sizes: number[];
  weights: number[];
  logStd: number[];
  meta?: Record<string, unknown>;
}

export class PolicyNet {
  readonly sizes: number[];
  readonly obsDim: number;
  readonly actDim: number;
  readonly actionLimit: number;
  readonly meta: Record<string, unknown>;

  private params: Float32Array;
  private layerW: number[];
  private layerB: number[];
  private nW: number;
  private acts: Float32Array[];

  constructor(p: PolicyPayload) {
    if (p.kind !== 'ragdoll-policy') throw new Error(`权重类型不符: ${p.kind}`);
    this.sizes = p.sizes;
    this.obsDim = p.obsDim;
    this.actDim = p.actDim;
    this.actionLimit = p.actionLimit;
    this.meta = p.meta || {};
    this.params = new Float32Array(p.weights);
    this.acts = p.sizes.map((s) => new Float32Array(s));

    const L = p.sizes.length;
    this.layerW = new Array(L).fill(0);
    this.layerB = new Array(L).fill(0);
    let nW = 0, nB = 0;
    for (let l = 1; l < L; l++) {
      this.layerW[l] = nW; nW += p.sizes[l - 1] * p.sizes[l];
      this.layerB[l] = nB; nB += p.sizes[l];
    }
    this.nW = nW;
    if (p.weights.length !== nW + nB) {
      throw new Error(`权重长度不符: 期望 ${nW + nB}，实际 ${p.weights.length}`);
    }
  }

  static async load(url: string): Promise<PolicyNet> {
    const res = await fetch(url);
    if (!res.ok) throw new Error(`策略权重加载失败 HTTP ${res.status}`);
    return new PolicyNet(await res.json() as PolicyPayload);
  }

  /** 前向，结果写入 out（长度须 >= actDim） */
  forward(x: Float32Array, out: Float32Array): void {
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
    out.set(this.acts[L].subarray(0, this.actDim));
  }
}
