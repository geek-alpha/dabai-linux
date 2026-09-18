import type { AppKernel, VoiceMode } from '../types/app-kernel.js';

export default (function init(App: AppKernel) {
  /* ============================================================
   *  VAD 自动对话模式（无需按住，说话即录、停顿即发）
   * ============================================================ */
  let vadEmaVol = 0;       // 指数移动平均音量（平滑）
  let vadVoiceEma = 0;     // 语音特性评分 EMA（平滑，防单帧抖动）
  let vadNoiseFloor = 0;   // 自适应环境噪声底（缓慢跟踪非语音段音量）
  let vadRecordStart = 0;  // 本次录音开始时间（自适应静音超时用）
  let vadSpeechLevel = 0;  // 录音期间语音音量水平（峰值保持+慢衰减，静音判定相对基准）
  const VAD_EMA_ALPHA = 0.35;  // EMA 平滑系数（越低越平滑，越高越灵敏）
  // 语音评分 EMA：攻击快、释放慢 —— 人声一来立刻确认（提速），噪声走后缓慢回落（防误判）
  const VAD_VOICE_EMA_ATTACK = 0.80;
  const VAD_VOICE_EMA_RELEASE = 0.12;
  const VAD_NOISE_FLOOR_ALPHA = 0.02; // 噪声底跟踪速度（很慢，防把说话当噪声）
  const VAD_NOISE_MARGIN = 1.6;       // 环境吵闹时：有效阈值 = 噪声底 × 此倍数
  const VAD_NOISE_FLOOR_MAX = 0.10;   // 噪声底上限，防止异常累积导致完全失灵
  // 自适应静音超时：说话越久意图越明确，超时越短（短句多等、长句快切）。
  // 阈值取"保守档"：自然语速的句内停顿可达 0.5~1s，超时过短会把长句切在中间，
  // 导致只识别出一两个字。
  const VAD_SILENCE_SHORT_MS = 1.2 * 1000;  // 短句（<1.2s）保持默认宽松超时
  const VAD_SILENCE_MID_MS = 800;           // 中等长度（1.2~6s）：有预滚回溯兜底后可收紧，结尾不再拖沓
  const VAD_SILENCE_LONG_MS = 700;          // 长句（>6s）：说话越久意图越明确，快速切出
  const VAD_SILENCE_MID_BOUND_MS = 6.0 * 1000; // 中/长句分界
  // 神经 VAD 判停档：Silero 直接回答「还有没有人在说」。
  // 小智服务端同款内核默认 200ms，那是设备端 Opus 直连、无需考虑浏览器上传链路；
  // 真人说话的句内自然停顿（换气、想词）实测可达 0.4~0.8s，阈值取 900~1200ms 留足余量，
  // 否则会在句中切断，把一句话拆成两段识别。
  const VAD_SILENCE_NEURAL_SHORT_MS = 1200;
  const VAD_SILENCE_NEURAL_MID_MS = 1000;
  const VAD_SILENCE_NEURAL_LONG_MS = 900;
  const VAD_SPEECH_DECAY = 0.997;            // 语音水平每帧衰减（约 4s 回落到 30%）
  const VAD_SILENCE_RATIO = 0.30;            // 静音判定 = 语音水平 × 此比例（相对阈值）
  const VAD_MAX_RECORD_MS = 30000;           // 录音上限（防噪声环境永远切不断）

  /**
   * 人声特性检测（稳健版）：基于"语音频段能量占比 + 频谱起伏度"。
   * 人声特性：
   *   1) 能量集中在 200~3500Hz 语音频段（低频环境噪音/高频键盘声占比低）
   *   2) 频谱有明显起伏（元音谐波峰谷交替），而非平坦白噪
   * 用相对量（占比/方差），不依赖绝对量纲，对真人声宽容、对稳态噪音严格。
   * 返回 0~1 语音特性评分。
   */
  App.vadIsVoice = function vadIsVoice() {
    if (!App.vadAnalyser || !App.vadData) return 0;
    App.vadAnalyser.getByteFrequencyData(App.vadData);
    const N = App.vadData.length;
    const sampleRate = App.audioCtx ? App.audioCtx.sampleRate : 48000;
    const binHz = sampleRate / 2 / N;

    // 1) 各频段能量
    const i200 = Math.max(1, Math.floor(200 / binHz));   // 语音频段下限
    const i3500 = Math.min(N, Math.ceil(3500 / binHz));  // 语音频段上限
    const i100 = Math.max(1, Math.floor(100 / binHz));   // 全频段下限（含低频噪音）
    const i8000 = Math.min(N, Math.ceil(8000 / binHz));  // 全频段上限（含高频噪音）

    let voiceEnergy = 0, fullEnergy = 0;
    for (let i = i100; i < i8000; i++) fullEnergy += App.vadData[i];
    for (let i = i200; i < i3500; i++) voiceEnergy += App.vadData[i];
    if (fullEnergy < (i8000 - i100) * 8) return 0; // 能量太低，无有效信号（阈值随 bin 数缩放）

    // 2) 语音频段占比：人声能量高度集中在 200~3500Hz
    const bandRatio = fullEnergy > 0 ? voiceEnergy / fullEnergy : 0;

    // 3) 频谱起伏度：语音频段内相邻 bin 差分的平均绝对值（人声谐波峰谷交替大）
    let diffSum = 0, diffCnt = 0;
    let prev = App.vadData[i200];
    for (let i = i200 + 1; i < i3500; i++) {
      diffSum += Math.abs(App.vadData[i] - prev);
      prev = App.vadData[i];
      diffCnt++;
    }
    const meanBin = fullEnergy / (i8000 - i100); // 平均 bin 能量
    const variance = diffCnt > 0 && meanBin > 0 ? (diffSum / diffCnt) / meanBin : 0;

    // 4) 综合评分：语音频段占比（主）+ 频谱起伏（辅）
    //    人声：bandRatio≈0.8~0.95, variance≈0.8~2.5 → 评分 0.6~0.9
    //    白噪：bandRatio≈0.45, variance≈0.2 → 评分 ≈0.3
    //    空调/风扇（低频隆隆）：bandRatio≈0.3~0.5, variance≈0.2 → 评分 <0.35
    //    键盘/金属（高频尖刺）：bandRatio≈0.3~0.5 → 评分 <0.4
    let score = bandRatio * 0.65 + Math.min(1, variance / 1.5) * 0.35;

    // 5) 惩罚项：低频(100~200Hz)能量异常集中（空调/风扇/引擎的隆隆声）
    let lowEnergy = 0;
    for (let i = i100; i < i200; i++) lowEnergy += App.vadData[i];
    if (lowEnergy > fullEnergy * 0.55) score *= 0.4;

    return Math.max(0, Math.min(1, score));
  };

  /** 综合判断当前是否为"人声"：能量达标 + 语音特性评分达标 */
  App.vadIsHumanVoice = function vadIsHumanVoice() {
    if (!App.VAD_VOICE_ENABLED) return true; // 关闭过滤 = 原逻辑（仅能量）
    // 神经 VAD 优先：Silero 对稳态噪声（空调/键盘/音乐）的判别力远超频谱启发式
    const neural = App.sileroVadIsVoice();
    if (neural !== null) return neural;
    const score = App.vadIsVoice();
    // 不对称 EMA：人声一来快速上升（缩短确认时间），噪声走后缓慢回落（防误判）
    const alpha = score > vadVoiceEma ? VAD_VOICE_EMA_ATTACK : VAD_VOICE_EMA_RELEASE;
    vadVoiceEma = alpha * score + (1 - alpha) * vadVoiceEma;
    return vadVoiceEma >= App.VAD_VOICE_SCORE_THRESHOLD;
  };
  App.vadResetVoiceEma = function vadResetVoiceEma() { vadVoiceEma = 0; };

  /** 自适应人声确认窗口：音量越强（清晰大声）确认越快，最小 50ms */
  App.vadGetConfirmMs = function vadGetConfirmMs(vol: number) {
    if (vol > App.VAD_THRESHOLD * 2) return 50;
    return App.VAD_VOICE_CONFIRM_MS;
  };

  /** 自适应静音超时：短句宽容（防切断），长句适度快速切出（提速） */
  App.vadGetSilenceMs = function vadGetSilenceMs() {
    const neural = App.sileroVadReady();
    if (!vadRecordStart) return neural ? VAD_SILENCE_NEURAL_SHORT_MS : App.VAD_SILENCE_MS;
    const dur = performance.now() - vadRecordStart;
    if (neural) {
      if (dur < VAD_SILENCE_SHORT_MS) return VAD_SILENCE_NEURAL_SHORT_MS;
      if (dur < VAD_SILENCE_MID_BOUND_MS) return VAD_SILENCE_NEURAL_MID_MS;
      return VAD_SILENCE_NEURAL_LONG_MS;
    }
    if (dur < VAD_SILENCE_SHORT_MS) return App.VAD_SILENCE_MS;
    if (dur < VAD_SILENCE_MID_BOUND_MS) return VAD_SILENCE_MID_MS;
    return VAD_SILENCE_LONG_MS;
  };

  /* ------------------------------------------------------------
   *  PCM 预滚环形缓冲（解决"开头几个字被漏掉"的根因）
   *  旧链路：VAD 确认人声 → 才 new MediaRecorder().start()
   *          → EMA 平滑(≈2帧) + 确认窗口(50~80ms) + 编码器启动(100~300ms)
   *          → 这 300ms 左右的音频物理上不存在，开头 2~3 个字必然丢。
   *  新链路：常驻 ScriptProcessor 持续采集 PCM 进环形缓冲，
   *          检测到说话时"回溯"取起点之前 VAD_PREROLL_MS 的音频，
   *          与后续录音一起编码成 16k WAV 上传 → 开头零丢失。
   *          同时不再依赖 MediaRecorder 的异步 stop/onstop（结尾更快）。
   * ------------------------------------------------------------ */
  const VAD_PREROLL_MS = 550;        // 起点回溯时长（覆盖确认窗口 + 检测延迟）
  const VAD_PREROLL_INTERRUPT_MS = 120; // 打断 AI 后起录的回溯（只补检测延迟，不带回 AI 尾音）
  const VAD_PCM_SAMPLE_RATE = 16000; // 上传采样率（STT 原生 16k，后端免重采样）
  const VAD_PCM_MIN_SPEECH_MS = 150; // PCM 路径最短语音时长（短于此视为误触发，丢弃）
  let vadPcmNode: ScriptProcessorNode | null = null;
  let vadPcmSink: GainNode | null = null;
  let vadPreRoll: Float32Array | null = null; // 环形预滚缓冲（原始采样率）
  let vadPreRollWrite = 0;
  let vadPcmChunks: Float32Array[] = [];      // 本次录音的 PCM 帧（含回溯段）
  let vadPcmActive = false;
  let vadPcmRate = 0;
  let vadPcmUsePcm = false;   // 本次录音是否走 PCM 路径
  let vadPcmSpeechSamples = 0; // 本次录音中「检测后」采集到的样本数（不含回溯）
  let vadPrerollMs = VAD_PREROLL_MS; // 本次起点回溯时长（打断场景缩短，防带入 AI 尾音）

  /** 启动常驻 PCM 采集（在 startVADMode 里麦克风源建立后调用） */
  function startVADPCMCapture(src: AudioNode) {
    stopVADPCMCapture();
    const ctx = App.audioCtx;
    if (!ctx) return;
    vadPcmRate = ctx.sampleRate || 48000;
    const cap = Math.max(1, Math.round(vadPcmRate * (VAD_PREROLL_MS / 1000)));
    vadPreRoll = new Float32Array(cap);
    vadPreRollWrite = 0;
    vadPcmChunks = [];
    vadPcmActive = false;
    try {
      const node = ctx.createScriptProcessor(2048, 1, 1);
      node.onaudioprocess = (e: AudioProcessingEvent) => {
        const inp = e.inputBuffer.getChannelData(0);
        App.sileroVadPush(inp, vadPcmRate);
        if (!vadPreRoll) return;
        const capN = vadPreRoll.length;
        const first = Math.min(inp.length, capN - vadPreRollWrite);
        vadPreRoll.set(inp.subarray(0, first), vadPreRollWrite);
        if (inp.length > first) vadPreRoll.set(inp.subarray(first), 0);
        vadPreRollWrite = (vadPreRollWrite + inp.length) % capN;
        if (vadPcmActive) { vadPcmChunks.push(new Float32Array(inp)); vadPcmSpeechSamples += inp.length; }
      };
      // ScriptProcessor 必须接到 destination 才会被调度；零增益避免麦克风外放
      const sink = ctx.createGain();
      sink.gain.value = 0;
      node.connect(sink);
      sink.connect(ctx.destination);
      src.connect(node);
      vadPcmNode = node;
      vadPcmSink = sink;
    } catch (e) {
      console.warn('[VAD] PCM 预滚采集不可用，回退 MediaRecorder:', e);
      vadPcmNode = null;
      vadPcmSink = null;
      vadPreRoll = null;
    }
  }

  /** 释放 PCM 采集（stopVADMode / 重建时调用） */
  function stopVADPCMCapture() {
    if (vadPcmNode) {
      try { vadPcmNode.disconnect(); } catch (e) {}
      vadPcmNode.onaudioprocess = null;
      vadPcmNode = null;
    }
    if (vadPcmSink) {
      try { vadPcmSink.disconnect(); } catch (e) {}
      vadPcmSink = null;
    }
    vadPreRoll = null;
    vadPcmChunks = [];
    vadPcmActive = false;
    vadPcmUsePcm = false;
    vadPcmSpeechSamples = 0;
  }

  /** 取环形缓冲里最近 VAD_PREROLL_MS 的样本（说话起点之前的音频） */
  function takeVADPreRoll(): Float32Array {
    if (!vadPreRoll || vadPreRoll.length === 0) return new Float32Array(0);
    const cap = vadPreRoll.length;
    const want = Math.max(0, Math.min(vadPrerollMs, VAD_PREROLL_MS));
    const n = Math.min(cap, Math.round(vadPcmRate * (want / 1000)));
    const out = new Float32Array(n);
    const start = (vadPreRollWrite - n + cap) % cap;
    for (let i = 0; i < n; i++) out[i] = vadPreRoll[(start + i) % cap];
    return out;
  }

  /** PCM 帧 → 16k 单声道 16bit WAV Blob（线性插值重采样，等效抗混叠低通） */
  function encodeVADPcmToWav(frames: Float32Array[], srcRate: number): Blob | null {
    let total = 0;
    for (const f of frames) total += f.length;
    if (total <= 0) return null;
    const merged = new Float32Array(total);
    let off = 0;
    for (const f of frames) { merged.set(f, off); off += f.length; }
    const dstRate = VAD_PCM_SAMPLE_RATE;
    const ratio = srcRate > dstRate ? srcRate / dstRate : 1;
    const outLen = Math.max(1, Math.floor(merged.length / ratio));
    const pcm = new Int16Array(outLen);
    for (let i = 0; i < outLen; i++) {
      const pos = i * ratio;
      const idx = Math.floor(pos);
      const frac = pos - idx;
      const a = merged[idx];
      const b = idx + 1 < merged.length ? merged[idx + 1] : a;
      let s = a + (b - a) * frac;
      if (s > 1) s = 1; else if (s < -1) s = -1;
      pcm[i] = s < 0 ? s * 32768 : s * 32767;
    }
    const buf = new ArrayBuffer(44 + pcm.length * 2);
    const dv = new DataView(buf);
    const wr = (o: number, str: string) => {
      for (let i = 0; i < str.length; i++) dv.setUint8(o + i, str.charCodeAt(i));
    };
    wr(0, 'RIFF');
    dv.setUint32(4, 36 + pcm.length * 2, true);
    wr(8, 'WAVE');
    wr(12, 'fmt ');
    dv.setUint32(16, 16, true);
    dv.setUint16(20, 1, true);      // PCM
    dv.setUint16(22, 1, true);      // mono
    dv.setUint32(24, dstRate, true);
    dv.setUint32(28, dstRate * 2, true);
    dv.setUint16(32, 2, true);
    dv.setUint16(34, 16, true);
    wr(36, 'data');
    dv.setUint32(40, pcm.length * 2, true);
    new Int16Array(buf, 44).set(pcm);
    return new Blob([buf], { type: 'audio/wav' });
  }

  /** Blob → base64（分块 String.fromCharCode，避免大数组爆栈） */
  function vadBlobToBase64Sync(blob: Blob): Promise<string> {
    return blob.arrayBuffer().then(ab => {
      const u8 = new Uint8Array(ab);
      const CHUNK = 0x8000;
      let binary = '';
      for (let i = 0; i < u8.length; i += CHUNK) {
        binary += String.fromCharCode.apply(null, Array.from(u8.subarray(i, i + CHUNK)) as unknown as number[]);
      }
      return btoa(binary);
    });
  }


  App.startVADMode = async function startVADMode() {
    if (App.vadStream) return true;
    try {
      const { stream } = await App._acquireMicStream();
      // 校验流状态
      const audioTracks = stream.getAudioTracks();
      if (audioTracks.length === 0 || audioTracks.every(t => t.readyState !== 'live')) {
        throw new Error('麦克风未就绪');
      }
      App.vadStream = stream;
      App.micStream = stream;
    } catch (err) {
      const e = err as DOMException;
      console.error('[VAD] 启动失败:', e.name, e.message);
      App.vadStream = null;
      App.micStream = null;
      let msg = '无法访问麦克风：' + (e.message || e.name);
      if (e.name === 'NotAllowedError' || e.name === 'PermissionDeniedError') {
        msg = '麦克风权限被拒绝，请在浏览器地址栏点击锁形图标重新授权';
      } else if (e.name === 'NotReadableError') {
        msg = '麦克风被其他应用占用，请关闭其他使用麦克风的程序';
      } else if (e.name === 'NotFoundError') {
        msg = '未找到麦克风设备';
      } else if (e.message === '麦克风未就绪') {
        msg = '麦克风未就绪，请重新开启自动对话';
      }
      App.showToast(msg);
      return false;
    }
    App.ensureAudioCtx();
    App.vadAnalyser = App.audioCtx!.createAnalyser();
    // 高帧率档用 2048 点 FFT：频率分辨率更高，人声特性判断更准（bin 带宽 ≈47Hz）
    App.vadAnalyser.fftSize = App.perfTier === 'high' ? 2048 : 1024;
    App.vadAnalyser.smoothingTimeConstant = 0.2;
    App.vadData = new Uint8Array(App.vadAnalyser.frequencyBinCount);
    const src = App.audioCtx!.createMediaStreamSource(App.vadStream!);
    src.connect(App.vadAnalyser);
    // 常驻 PCM 采集 + 预滚环形缓冲：让"开头"在检测到人声之前就已经被录下来
    startVADPCMCapture(src);
    // 神经 VAD（Silero）后台加载：加载完成前由频谱启发式顶着，不阻塞自动对话
    App.sileroVadInit();
    vadEmaVol = 0;
    vadVoiceEma = 0; // 重置语音特性评分
    vadNoiseFloor = 0; // 重置噪声底
    vadRecordStart = 0;
    vadSpeechLevel = 0;
    console.log('[VAD] 自动对话模式已启动 (fftSize=' + App.vadAnalyser.fftSize + ')');
    return true;
  };
  /* VAD 帧调度：非静默走 rAF（本来就在出帧，白捡的节拍）；聊天全屏静默时改用定时器 ——
   * 00_quiet 已经把 3D 帧循环停掉，此时 rAF 每帧仍会逼浏览器产出一个 vsync 帧：
   * 屏幕上一个像素都不变，却把显示管线一直叫醒，手机进不了低功耗（用户反馈「全屏了还是烫」）。
   * 定时器不绑 vsync，节拍间隔按档位换算，检测频率与 rAF 下完全一致。 */
  const VAD_FRAME_MS = 16.7;
  let vadTimer: ReturnType<typeof setTimeout> | null = null;
  // 走定时器还是走 rAF，全模块只此一处判据（调度与检测门控必须用同一条，否则节奏会重叠）
  const useTimerSchedule = () => App.chatQuiet && !document.hidden;
  const scheduleVAD = () => {
    // 页面不可见时仍走 rAF：浏览器会把挂起的 rAF 彻底暂停（零唤醒），定时器只会被压到 1Hz
    // —— 手机锁屏后每秒白醒一次。可见时再用定时器，那时才是「rAF 逼着出帧」的代价。
    if (useTimerSchedule()) {
      App.vadRAF = null;
      vadTimer = setTimeout(App.vadLoop, Math.round(VAD_FRAME_MS * Math.max(1, App._vadFrameSkip || 1)));
    } else {
      vadTimer = null;
      App.vadRAF = requestAnimationFrame(App.vadLoop);
    }
  };
  const cancelVADSchedule = () => {
    if (App.vadRAF) { cancelAnimationFrame(App.vadRAF); App.vadRAF = null; }
    if (vadTimer) { clearTimeout(vadTimer); vadTimer = null; }
  };
  // 静默开关切换 = 换调度器：取消挂着的那一个，立刻用新调度器续上（VAD 不能断档）
  if (App.onQuiet) App.onQuiet(() => {
    if (App.voiceMode === 'auto' && App.vadAnalyser) { cancelVADSchedule(); scheduleVAD(); }
  });

  App.stopVADMode = function stopVADMode() {
    cancelVADSchedule();
    // 停止当前录音（如果有的话）
    if (App.vadRecorder && App.vadRecorder.state !== 'inactive') {
      try { App.vadRecorder.stop(); } catch (e) {}
    }
    App.vadRecorder = null;
    // 释放常驻 PCM 预滚采集
    stopVADPCMCapture();
    // 停止克隆的轨（如果有的话）
    if (App._vadClonedTrack) {
      try { App._vadClonedTrack.stop(); } catch (e) {}
      App._vadClonedTrack = null;
    }
    App.vadStream = null;
    App.vadAnalyser = null;
    App.vadData = null;
    App.vadState = 'idle';
    App.vadSilenceStart = 0;
    App.vadInterruptStart = 0;
    App.vadVoiceStart = 0;
    vadEmaVol = 0;
    vadVoiceEma = 0; // 重置语音特性评分
    vadNoiseFloor = 0; // 重置噪声底
    vadRecordStart = 0;
    vadSpeechLevel = 0;

    // 延迟释放麦克风流，给切回按住模式后立刻录音留出复用窗口
    if (App._micStreamReleaseTimer) clearTimeout(App._micStreamReleaseTimer);
    App._micStreamReleaseTimer = setTimeout(() => {
      App._micStreamReleaseTimer = null;
      // 如果当前仍在自动模式或正在录音，不要释放
      if (App.voiceMode === 'auto' || App.isRecording) return;
      if (App.micStream) {
        console.log('[VAD] 延迟释放麦克风流');
        App.micStream.getTracks().forEach(t => t.stop());
        App.micStream = null;
      }
    }, 3000);
  };
  App.setVoiceMode = function setVoiceMode(mode: VoiceMode) {
    App.voiceMode = mode;
    localStorage.setItem('dabai.voiceMode', mode);
    if (mode === 'auto') {
      App.startVADMode().then(ok => {
        if (!ok) {
          App.voiceMode = 'press';
          localStorage.setItem('dabai.voiceMode', 'press');
          App.voiceBtn!.classList.remove('auto');
          App.voiceBtn!.title = '点击切换模式 · 长按说话';
          return;
        }
        App.vadState = 'idle';
        App.vadLoop();
        App.voiceBtn!.classList.add('auto');
        App.voiceBtn!.title = '自动对话中 · 点击切回按住说话';
        App.showToast('已切换为自动对话 · 直接说话即可');
        App.sendAIAction('（用户解放了双手，现在你能一直听到Ta的声音了，可以更自然随意地聊天）', true);
      });
    } else {
      App.stopVADMode();
      App.voiceBtn!.classList.remove('auto');
      App.voiceBtn!.title = '点击切换模式 · 长按说话';
      App.showToast('已切换为按住说话');
      App.sendAIAction('（用户切换了对话方式，现在需要按住按钮才能听到Ta说话，等Ta准备好再说）', true);
    }
  };
  /* 频率域能量检测：比时域 RMS 更稳定，能综合捕捉全频段语音能量 */
  App.vadGetVolume = function vadGetVolume() {
    if (!App.vadAnalyser || !App.vadData) return 0;
    App.vadAnalyser.getByteFrequencyData(App.vadData);
    let sum = 0;
    // 只统计语音频段 (100Hz ~ 3500Hz)：
    // 低于 100Hz 是空调/风扇隆隆声，高于 3500Hz 是键盘/鼠标/金属噪音，
    // 都不计入，避免环境噪音抬高音量导致误触发
    const sampleRate = App.audioCtx ? App.audioCtx.sampleRate : 48000;
    const binHz = sampleRate / 2 / App.vadData.length;
    const minBin = Math.max(1, Math.floor(100 / binHz));
    const maxBin = Math.min(Math.ceil(3500 / binHz), App.vadData.length);
    for (let i = minBin; i < maxBin; i++) {
      sum += App.vadData[i];
    }
    const raw = sum / (maxBin - minBin) / 255;  // 归一化到 0~1
    // EMA 平滑，减少单帧抖动
    vadEmaVol = VAD_EMA_ALPHA * raw + (1 - VAD_EMA_ALPHA) * vadEmaVol;
    return vadEmaVol;
  };
App.vadLoop = function vadLoop() {
    // 'auto'（自动对话）是唯一需要 VAD 持续聆听的模式
    if (App.voiceMode !== 'auto') return;

    // 检查 AudioContext 是否被浏览器暂停（长时间空闲后浏览器会挂起音频上下文）
    if (App.audioCtx && App.audioCtx.state === 'suspended') {
      App.audioCtx.resume().then(() => console.log('[VAD] AudioContext 已自动恢复'));
      // 等待下一个周期再检测，让 resume 生效
      scheduleVAD();
      return;
    }
    if (!App.vadAnalyser) return;

    // 检查 vadStream 是否还活着（浏览器长时间后台可能回收麦克风流）
    if (App.vadStream && !App.vadStream.active) {
      console.warn('[VAD] 麦克风流已失效，尝试重建…');
      App.stopVADMode();
      App.startVADMode().then(ok => {
        if (ok) {
          App.vadState = 'idle';
          App.vadLoop();
        } else {
          App.showToast('自动对话已断开，请切回按住说话');
          App.voiceMode = 'press';
        }
      });
      return;
    }
    scheduleVAD();

    // 性能分级：降频VAD检测以节省CPU。
    // 走定时器时间隔已经按 skip 换算过，每次 tick 都该检测 —— 再 skip 一次等于把频率砍半，
    // 打断会变迟钝，而全屏聊天恰恰是最需要随时打断的场景。
    if (!useTimerSchedule() && !App.shouldVADFrame()) return;

    const vol = App.vadGetVolume();
    const now = performance.now();
    // 自动对话模式：AI 说话/思考时同样可被用户语音打断

    // 有效音量阈值：安静环境用固定下限；环境吵闹时抬高到「噪声底 × 倍数」，
    // 保证说话声必须显著高于环境噪声才触发（自适应，不依赖单一固定值）
    const effThreshold = vadNoiseFloor > App.VAD_THRESHOLD
      ? Math.min(vadNoiseFloor * VAD_NOISE_MARGIN, VAD_NOISE_FLOOR_MAX)
      : App.VAD_THRESHOLD;

    // AI 说话中：检测打断。
    // 打断判定用"音量 + 稍长确认窗口"（用户开口说话必然高音量），
    // 不用人声特性评分（评分EMA有爬升延迟，会导致打断迟钝/失效）。
    // 用户 VAD 输入必须能随时打断 AI 输出。
    if (App.currentState === App.State.SPEAKING) {
      if (vol > App.VAD_INTERRUPT_THRESHOLD) {
        if (App.vadInterruptStart === 0) App.vadInterruptStart = now;
        if (now - App.vadInterruptStart > App.VAD_INTERRUPT_MS) {
          if (App._turnInTools) {
            // 工具任务执行中：只停本地播报，不发 interrupt——服务端把这句排队，
            // 等工具干完再回（语音不得掐掉正在跑的工具任务）
            console.log('[VAD] 工具任务执行中，语音排队不打断 vol=', vol.toFixed(3));
            App.clearAudioQueue();
          } else {
            console.log('[VAD] 检测到用户输入，打断AI输出 vol=', vol.toFixed(3));
            App.triggerInterrupt();
          }
          App.vadInterruptStart = 0;
          // 立即切到 IDLE，防止本函数下一帧再次进打断分支
          App.currentState = App.State.IDLE;
          if (App.vadState === 'idle') { vadPrerollMs = VAD_PREROLL_INTERRUPT_MS; App.startVADRecording(); }
        }
      } else {
        App.vadInterruptStart = 0;
      }
      return;
    }

    // AI 思考中：不播放声音，可被用户语音打断（说句话把 AI 从思考中拉回聆听）。
    // 思考时 AI 无声 → 不存在自打断风险，用"音量 + 人声特性"双确认：
    // 环境噪音/音乐被语音评分过滤，只有真人声才打断思考。
    if (App.currentState === App.State.THINKING) {
      const isVoice = App.vadIsHumanVoice();
      const voiceDetected = vol > App.VAD_THRESHOLD && isVoice;
      if (voiceDetected) {
        if (App.vadInterruptStart === 0) App.vadInterruptStart = now;
        if (now - App.vadInterruptStart > App.VAD_INTERRUPT_MS) {
          if (App._turnInTools) {
            // 工具任务执行中：只停本地播报，不发 interrupt——服务端把这句排队
            console.log('[VAD] 工具任务执行中，语音排队不打断（思考态） vol=', vol.toFixed(3));
            App.clearAudioQueue();
          } else {
            console.log('[VAD] 检测到用户输入，打断AI思考，进入聆听 vol=', vol.toFixed(3));
            App.triggerInterrupt();
          }
          App.vadInterruptStart = 0;
          // 立即切到 IDLE，防止本函数下一帧再次进打断分支
          App.currentState = App.State.IDLE;
          if (App.vadState === 'idle') { vadPrerollMs = VAD_PREROLL_INTERRUPT_MS; App.startVADRecording(); }
        }
      } else {
        App.vadInterruptStart = 0;
      }
      return;
    }

    // IDLE：自动检测说话开始（人声特性 + 音量达标，连续确认防误判）
    if (App.vadState === 'idle') {
      const isVoice = App.vadIsHumanVoice();
      const voiceDetected = vol > effThreshold && isVoice;
      if (voiceDetected) {
        // 连续多帧确认：防止单帧噪声/偶发误判（音量越大确认越快）
        if (App.vadVoiceStart === 0) App.vadVoiceStart = now;
        if (now - App.vadVoiceStart > App.vadGetConfirmMs(vol)) {
          App.vadVoiceStart = 0;
          App.startVADRecording();
        }
      } else {
        App.vadVoiceStart = 0;
        // 无语音时缓慢跟踪噪声底（只在安静段更新，避免把说话声当噪声）
        if (vadNoiseFloor === 0 || vol < vadNoiseFloor) {
          vadNoiseFloor = vol;
        } else {
          vadNoiseFloor += (vol - vadNoiseFloor) * VAD_NOISE_FLOOR_ALPHA;
        }
        vadNoiseFloor = Math.min(vadNoiseFloor, VAD_NOISE_FLOOR_MAX);
      }
    } else if (App.vadState === 'recording') {
      // 录音上限：防止噪声环境永远切不断
      const maxRecord = VAD_MAX_RECORD_MS;
      if (vadRecordStart && now - vadRecordStart > maxRecord) {
        console.log('[VAD] 录音达上限，强制结束');
        App.stopVADRecording();
        App.vadSilenceStart = 0;
      }
      // 跟踪语音音量水平：峰值保持 + 缓慢衰减（适应麦克风增益/说话音量，
      // 轻声细语时静音阈值也跟着降低，不会把说话中的小停顿误判为静音）
      if (vol > vadSpeechLevel) {
        vadSpeechLevel = vol;
      } else {
        vadSpeechLevel *= VAD_SPEECH_DECAY;
      }
      // 相对静音阈值 = 语音水平的 30%（兜底下限 = 环境阈值的一半）
      const silThr = Math.max(effThreshold * 0.5, vadSpeechLevel * VAD_SILENCE_RATIO);
      // 判停阈值再与"噪声底附近"取大：环境噪声较大时，噪声本身就会把音量顶在
      // silThr 之上导致永远录不完（直到 30s 上限），此时以噪声底为基准判定停顿
      const pauseThr = Math.max(silThr, vadNoiseFloor * 1.25);
      // 判停内核走神经 VAD（同小智）：它直接回答「还有没有人在说」，不受麦克风增益、
      // 峰值保持衰减（vadSpeechLevel 需 ~6s 才回落到 30%）影响——旧逻辑正是「话说完半天切不断」的根因。
      // 神经可用时以它为准；音量条件只在神经不可用时兜底。二者曾用 || 并联，
      // 导致神经判定「还在说话」却被音量单方面掐断——句中轻声/换气正是这样被切成两段的。
      const quiet = App.sileroVadReady()
        ? App.sileroVadIsVoice() === false
        : vol < pauseThr;
      if (quiet) {
        if (App.vadSilenceStart === 0) App.vadSilenceStart = now;
        if (now - App.vadSilenceStart > App.vadGetSilenceMs()) {
          App.stopVADRecording();
          App.vadSilenceStart = 0;
        }
      } else {
        App.vadSilenceStart = 0;
      }
    }
  };
  App.startVADRecording = function startVADRecording() {
    if (!App.vadStream) return;
    // 防止上一个 recorder 还没完全停止就创建新的
    if (App.vadRecorder && App.vadRecorder.state === 'recording') {
      try { App.vadRecorder.stop(); } catch (e) {}
    }
    App.vadState = 'recording';
    App.vadChunks = [];
    App.vadSilenceStart = 0;
    vadRecordStart = performance.now(); // 自适应静音超时基准
    vadNoiseFloor = 0; // 录音中环境已变化，重置噪声底
    vadSpeechLevel = 0; // 重置语音水平基准

    /* 首选路径：PCM 环形缓冲 + 预滚回溯 —— 把"检测到人声之前"的音频也接上，
       开头零丢失；停止时无需等 MediaRecorder 异步 onstop，结尾也不拖沓 */
    if (vadPcmNode && vadPreRoll) {
      vadPcmUsePcm = true;
      vadPcmSpeechSamples = 0;
      vadPrerollMs = VAD_PREROLL_MS;
      vadPcmChunks = [takeVADPreRoll()];
      vadPcmActive = true;
      App.setState(App.State.LISTENING);
      return;
    }

    // 回退路径：PCM 采集不可用时仍用 MediaRecorder
    vadPcmUsePcm = false;
    // 克隆音频轨创建独立的 MediaStream，Chrome 会为新流写入完整 EBML 头部
    try {
      const originalTrack = App.vadStream.getAudioTracks()[0];
      if (!originalTrack || originalTrack.readyState !== 'live') {
        console.warn('[VAD] 原始音频轨不可用，重建 VAD');
        App.stopVADMode();
        App.startVADMode().then(ok => {
          if (ok) { App.vadState = 'idle'; App.vadLoop(); }
          else { App.showToast('自动对话已断开'); App.voiceMode = 'press'; }
        });
        return;
      }
      // 停止上一个克隆轨
      if (App._vadClonedTrack) {
        try { App._vadClonedTrack.stop(); } catch (e) {}
        App._vadClonedTrack = null;
      }
      App._vadClonedTrack = originalTrack.clone();
      const clonedStream = new MediaStream([App._vadClonedTrack]);

      const mime = App.pickRecorderMime();
      // 语音识别用 96kbps Opus：比 48k 保留更多辅音细节，提升识别准确率
      // （文件仍足够小，上传/转码速度几乎无感）
      App.vadRecorder = mime
        ? new MediaRecorder(clonedStream, { mimeType: mime, audioBitsPerSecond: 96000 })
        : new MediaRecorder(clonedStream);
    } catch (e) {
      console.warn('[VAD] MediaRecorder 创建失败，尝试重建流:', e);
      App.stopVADMode();
      App.vadState = 'idle';
      App.startVADMode().then(ok => {
        if (ok) App.vadLoop();
        else { App.showToast('自动对话已断开'); App.voiceMode = 'press'; }
      });
      return;
    }
    App.vadRecorder!.ondataavailable = e => {
      if (e.data && e.data.size > 0) App.vadChunks.push(e.data);
    };
    // 录音过程出错（设备被抢占/克隆轨中断等）→ 静默重建 VAD，不惊扰用户
    App.vadRecorder!.onerror = ev => {
      console.error('[VAD] MediaRecorder 出错:', (ev.error && ev.error.name) || 'unknown');
      try { App.vadRecorder!.stop(); } catch (e2) {}
    };
    App.vadRecorder!.onstop = () => {
      const chunks = App.vadChunks;
      App.vadChunks = [];
      const mimeType = App.vadRecorder!.mimeType || 'audio/webm';
      const blob = new Blob(chunks, { type: mimeType });
      // 停止并释放克隆轨
      if (App._vadClonedTrack) {
        try { App._vadClonedTrack.stop(); } catch (e) {}
        App._vadClonedTrack = null;
      }
      const reader = new FileReader();
      reader.onloadend = () => {
        App.sendAudioBase64(reader.result as string, mimeType);
        App.showTyping();
      };
      reader.readAsDataURL(blob);
    };
    App.vadRecorder!.start(200);
    App.setState(App.State.LISTENING);
  };
  App.stopVADRecording = function stopVADRecording() {
    App.vadState = 'idle';
    vadRecordStart = 0;
    vadSpeechLevel = 0;
    if (vadPcmUsePcm) {
      vadPcmUsePcm = false;
      vadPcmActive = false;
      const frames = vadPcmChunks;
      vadPcmChunks = [];
      const rate = vadPcmRate || 48000;
      // 最短语音时长校验用「检测后」的样本（不含回溯）：只需确认确实说过话，
      // 过严会把短促回答整条丢掉 → 表现为"它没听到我说话"
      if (vadPcmSpeechSamples / rate * 1000 < VAD_PCM_MIN_SPEECH_MS) {
        vadPcmSpeechSamples = 0;
        return;
      }
      vadPcmSpeechSamples = 0;
      const blob = encodeVADPcmToWav(frames, rate);
      if (!blob) return;
      vadBlobToBase64Sync(blob).then(b64 => {
        App.sendAudioBase64(b64, 'audio/wav');
        App.showTyping();
      });
      return;
    }
    if (!App.vadRecorder || App.vadRecorder.state === 'inactive') return;
    try {
      if (App.vadRecorder.state === 'recording') {
        App.vadRecorder.requestData();
      }
      App.vadRecorder.stop();
    } catch (e) {}
  };
  /* AI 说完后恢复监听（VAD 自动模式）：重置计时避免把尾音当用户说话 */
  App.vadResumeAfterSpeak = function vadResumeAfterSpeak() {
    App.vadSilenceStart = 0;
    App.vadInterruptStart = 0;
    App.vadVoiceStart = 0;
    App.vadState = 'idle';
    vadEmaVol = 0;  // 重置 EMA 避免残留
    vadVoiceEma = 0; // 重置语音特性评分，避免把 AI 自己的尾音当用户人声
    vadNoiseFloor = 0; // 重置噪声底，重新适应当前环境
    vadSpeechLevel = 0;
    App.sileroVadReset(); // LSTM 状态里可能残留 AI 尾音，重置后再判人声
    // 丢弃残留的半截 PCM 录音，避免把 AI 尾音/静音尾巴拼进下一句开头
    vadPcmActive = false;
    vadPcmUsePcm = false;
    vadPcmChunks = [];
    vadPcmSpeechSamples = 0;
  };
});
