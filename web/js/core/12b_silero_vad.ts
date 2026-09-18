import type { AppKernel } from '../types/app-kernel.js';

/* Silero VAD v5 神经 VAD —— 与小智后端 xiaozhi-esp32-server 同一内核。
 * 模型 silero_vad.onnx：input=[1,576]（64 上下文 + 512 新样本）、state=[2,1,128]、sr=16000，
 * 输出 output=语音概率、stateN=新的 LSTM 状态。运行时 onnxruntime-web（wasm，单线程）。
 * 模型未就绪 / 加载失败时 sileroVadIsVoice() 返回 null，由 12_vad_auto 的频谱启发式兜底。 */
export default (function init(App: AppKernel) {
  const ORT_URL = '/static/vendor/onnxruntime-web/ort.min.mjs';
  const WASM_DIR = '/static/vendor/onnxruntime-web/';
  const MODEL_URL = '/static/vendor/silero/silero_vad.onnx';

  const SR = 16000;
  const CHUNK = 512;          // 每帧 512 样本 = 32ms
  const CTX = 64;             // v5 上下文样本（上一帧尾部）
  const QUEUE_MAX = 10;       // 推理积压上限（≈320ms），超了丢最旧
  const PROB_STALE_MS = 250;  // 概率过期阈值：久无新结果即视为不可用
  const TH_ON = 0.5;          // 双阈值迟滞（对齐小智 config.yaml：threshold 0.5 / threshold_low 0.3）
  const TH_OFF = 0.3;

  let ortRef: any = null;
  let session: any = null;
  let loading = false;
  let broken = false;

  let stateData: Float32Array | null = null;  // LSTM 状态 [2,1,128]
  let srTensor: any = null;
  let ctxBuf = new Float32Array(CTX);
  let pend = new Float32Array(CHUNK * 2);     // 重采样后待切块的样本
  let pendLen = 0;
  const queue: Float32Array[] = [];
  let pumping = false;

  let prob = -1;
  let probAt = 0;
  let hold = false;           // 迟滞区间内的保持状态

  function resetState() {
    if (stateData) stateData.fill(0);
    ctxBuf.fill(0);
    pendLen = 0;
    queue.length = 0;
    prob = -1;
    probAt = 0;
    hold = false;
  }

  async function pump() {
    if (pumping) return;
    pumping = true;
    try {
      while (queue.length > 0 && session && stateData && ortRef) {
        const chunk = queue.shift() as Float32Array;
        const inputData = new Float32Array(CTX + CHUNK);
        inputData.set(ctxBuf, 0);
        inputData.set(chunk, CTX);
        const feeds: any = {
          input: new ortRef.Tensor('float32', inputData, [1, CTX + CHUNK]),
          state: new ortRef.Tensor('float32', stateData, [2, 1, 128]),
          sr: srTensor,
        };
        const out = await session.run(feeds);
        const names = session.outputNames;
        prob = (out[names[0]].data as Float32Array)[0];
        probAt = performance.now();
        stateData.set(out[names[1]].data as Float32Array);
        ctxBuf.set(inputData.subarray(CHUNK));
      }
    } catch (e) {
      broken = true;
      session = null;
      console.warn('[Silero] 推理失败，回退频谱启发式:', e);
    } finally {
      pumping = false;
    }
  }

  /** 后台加载运行时 + 模型（幂等；失败静默降级，不影响自动对话可用性） */
  App.sileroVadInit = function sileroVadInit() {
    if (session || loading || broken) return;
    loading = true;
    void (async () => {
      try {
        const ortMod: any = await import(ORT_URL);
        ortMod.env.wasm.wasmPaths = WASM_DIR;
        // 单线程：多线程 wasm 需要 SharedArrayBuffer（COOP/COEP 响应头），本地静态服务没有
        ortMod.env.wasm.numThreads = 1;
        ortMod.env.logLevel = 'error';
        const sess = await ortMod.InferenceSession.create(MODEL_URL, { executionProviders: ['wasm'] });
        srTensor = new ortMod.Tensor('int64', BigInt64Array.from([BigInt(SR)]), []);
        stateData = new Float32Array(2 * 1 * 128);
        ortRef = ortMod;
        session = sess;
        console.log('[Silero] 神经 VAD 就绪:', sess.inputNames.join(','), '→', sess.outputNames.join(','));
      } catch (e) {
        broken = true;
        console.warn('[Silero] 加载失败，回退频谱启发式:', e);
      } finally {
        loading = false;
      }
    })();
  };

  /** 喂入麦克风 PCM（任意采样率，内部线性插值降到 16k） */
  App.sileroVadPush = function sileroVadPush(samples: Float32Array, rate: number) {
    if (!session || !stateData || !rate) return;
    const n = Math.max(1, Math.round(samples.length * SR / rate));
    const need = pendLen + n;
    if (need > pend.length) {
      let cap = pend.length * 2;
      while (cap < need) cap *= 2;
      const grown = new Float32Array(cap);
      grown.set(pend.subarray(0, pendLen), 0);
      pend = grown;
    }
    const ratio = rate / SR;
    for (let i = 0; i < n; i++) {
      const pos = i * ratio;
      const i0 = pos | 0;
      const frac = pos - i0;
      const a = samples[i0] || 0;
      const b = i0 + 1 < samples.length ? samples[i0 + 1] : a;
      pend[pendLen + i] = a + (b - a) * frac;
    }
    pendLen += n;
    while (pendLen >= CHUNK) {
      queue.push(pend.slice(0, CHUNK));
      pend.copyWithin(0, CHUNK, pendLen);
      pendLen -= CHUNK;
    }
    if (queue.length > QUEUE_MAX) queue.splice(0, queue.length - QUEUE_MAX);
    void pump();
  };

  /** 人声判定：true/false = 神经 VAD 结论；null = 不可用（调用方走启发式） */
  App.sileroVadIsVoice = function sileroVadIsVoice(): boolean | null {
    if (prob < 0 || performance.now() - probAt > PROB_STALE_MS) return null;
    if (prob >= TH_ON) { hold = true; return true; }
    if (prob <= TH_OFF) { hold = false; return false; }
    return hold;
  };

  /** 最近一次语音概率（-1 = 不可用） */
  App.sileroVadProb = function sileroVadProb() {
    return (prob < 0 || performance.now() - probAt > PROB_STALE_MS) ? -1 : prob;
  };

  App.sileroVadReady = function sileroVadReady() { return session !== null; };
  App.sileroVadReset = function sileroVadReset() { resetState(); };
});
