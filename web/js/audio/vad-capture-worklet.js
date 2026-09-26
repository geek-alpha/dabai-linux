/* ============================================================
 * web/js/audio/vad-capture-worklet.js —— 音频线程 PCM 采集（模块 12 配套）
 * ------------------------------------------------------------
 * 为什么需要它：ScriptProcessorNode 的 onaudioprocess 跑在主线程，
 * 而 VR（WebXR 沉浸会话）里主线程被双目渲染 + wasm VAD 推理占满，
 * 音频回调被饿死 —— 录到的 PCM 时断时续，样本数还不足，最终表现为
 * 「戴上头显说什么都识别不出来，摘下就正常」。
 * AudioWorklet 的 process() 在独立音频线程上按 128 帧硬实时调度，
 * 主线程再忙也不影响采集完整性。这也是 W3C 废弃 ScriptProcessor 的理由。
 *
 * 职责边界：只负责「搬运」—— 累积 FRAME 帧原样 postMessage 给主线程，
 * 不做重采样、不做判定、不认识 Silero。降采样/环形缓冲/推理全留在
 * 主线程既有实现里，两条搬运路径共用同一份处理逻辑（见 12_vad_auto.onPCMFrame）。
 * ============================================================ */
const FRAME = 2048;

class VadCaptureProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this._buf = new Float32Array(FRAME);
    this._n = 0;
    this._alive = true;
    // 主线程 stopVADPCMCapture 时发 'stop'：立即停止搬运，不再往主线程投递
    this.port.onmessage = (ev) => {
      if (ev.data === 'stop') this._alive = false;
    };
  }

  process(inputs) {
    if (!this._alive) return false;
    const ch = inputs[0] && inputs[0][0];
    if (!ch || ch.length === 0) return true;
    const buf = this._buf;
    const room = FRAME - this._n;
    const take = ch.length < room ? ch.length : room;
    buf.set(ch.subarray(0, take), this._n);
    this._n += take;
    if (this._n === FRAME) {
      // transfer 后 buf.buffer 失效，必须换新缓冲，否则下一帧写入抛错
      this.port.postMessage(buf, [buf.buffer]);
      this._buf = new Float32Array(FRAME);
      this._n = 0;
    }
    return true;
  }
}

registerProcessor('vad-capture', VadCaptureProcessor);
