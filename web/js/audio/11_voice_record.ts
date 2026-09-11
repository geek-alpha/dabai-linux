import type { AppKernel } from '../types/app-kernel.js';

export default (function init(App: AppKernel) {
  /* ============================================================
   *  语音录制
   * ============================================================ */
  /* 录音格式优先级：兼容 iOS Safari（mp4）+ Chrome/Firefox（webm） */
  App.pickRecorderMime = function pickRecorderMime() {
    const candidates = ['audio/webm;codecs=opus', 'audio/webm', 'audio/ogg;codecs=opus', 'audio/mp4' // iOS Safari
    ];
    for (const m of candidates) {
      if (m && MediaRecorder.isTypeSupported(m)) return m;
    }
    return '';
  };
  /* 麦克风约束：单声道 + 强降噪/回声消除，提升识别准确率。
   * 采样率用 ideal 请求原生 48k：避免浏览器强制降到 16k 损失音频细节，
   * 由服务端 ffmpeg(soxr) 高质量重采样到 16k，识别准确率更高；
   * ideal 写法兼容不支持该约束的浏览器（不会 OverconstrainedError）。 */
  App.MIC_CONSTRAINTS = {
    echoCancellation: true,
    noiseSuppression: true,
    autoGainControl: true,
    channelCount: 1,
    sampleRate: { ideal: 48000 },
    sampleSize: { ideal: 16 }
  };

  /**
   * 校验并持有共享麦克风流，给轨道绑定 onended 监听以便自动清理。
   * 返回 { stream, fresh: boolean }，fresh=true 表示是新获取的流。
   */
  App._acquireMicStream = async function _acquireMicStream(): Promise<{ stream: MediaStream; fresh: boolean }> {
    const isLive = (s: MediaStream | null | undefined) => s && s.active && s.getAudioTracks().some(t => t.readyState === 'live');
    if (isLive(App.micStream)) {
      console.log('[Voice] 复用已有麦克风流');
      return { stream: App.micStream!, fresh: false };
    }
    // 丢弃旧流引用（同时清理 VAD 引用，避免引用已停止的流）
    if (App.micStream) {
      try { App.micStream.getTracks().forEach(t => t.stop()); } catch (e) {}
      if (App.vadStream === App.micStream) App.vadStream = null;
      App.micStream = null;
    }
    const stream = await navigator.mediaDevices.getUserMedia({
      audio: App.MIC_CONSTRAINTS
    });
    // 绑定轨道结束监听，意外结束时自动清理
    stream.getAudioTracks().forEach(track => {
      track.onended = () => {
        console.warn('[Voice] 麦克风流意外结束');
        if (App.micStream === stream) App.micStream = null;
        if (App.vadStream === stream) App.vadStream = null;
      };
    });
    App.micStream = stream;
    return { stream, fresh: true };
  };

  App.startRecording = async function startRecording() {
    // 按下麦克风即打断当前 AI 回复（让用户随时插话）
    App.triggerInterrupt();
    try {
      const { stream } = await App._acquireMicStream();

      // 再次校验流状态（某些浏览器 getUserMedia 可能返回已结束的流）
      const audioTracks = stream.getAudioTracks();
      if (audioTracks.length === 0 || audioTracks.every(t => t.readyState !== 'live')) {
        throw new Error('麦克风未就绪');
      }

      App.audioChunks = [];
      const mime = App.pickRecorderMime();
      App.mediaRecorder = mime ? new MediaRecorder(stream, {
        mimeType: mime,
        audioBitsPerSecond: 128000
      }) : new MediaRecorder(stream);
      App.mediaRecorder.ondataavailable = e => {
        if (e.data && e.data.size > 0) App.audioChunks.push(e.data);
      };
      // 录音过程出错（设备被抢占/轨道中断等）→ 容错清理而不是卡死
      App.mediaRecorder.onerror = ev => {
        console.error('[Voice] MediaRecorder 出错:', (ev.error && ev.error.name) || 'unknown');
        App.showToast('录音出错，请重新按住说话');
        App.audioChunks = [];
        App.isRecording = false;
        App.voiceBtn!.classList.remove('recording');
        if (App.micStream) {
          try { App.micStream.getTracks().forEach(tr => tr.stop()); } catch (e2) {}
          App.micStream = null;
        }
        App.setState(App.State.IDLE);
      };
      App.mediaRecorder.onstop = () => {
        const mimeType = App.mediaRecorder!.mimeType || 'audio/webm';
        // 按住模式结束后释放流；自动模式由 VAD 管理，不在此处释放
        if (App.voiceMode !== 'auto' && App.micStream) {
          App.micStream.getTracks().forEach(tr => tr.stop());
          App.micStream = null;
        }
        if (!App.audioChunks || App.audioChunks.length === 0) {
          App.showToast('录音数据为空，请重试');
          App.setState(App.State.IDLE);
          return;
        }
        const blob = new Blob(App.audioChunks, { type: mimeType });
        if (blob.size < 3000) {
          App.showToast('录音太短，请按住多说一下');
          App.setState(App.State.IDLE);
          return;
        }
        const reader = new FileReader();
        reader.onloadend = () => App.sendAudioBase64(reader.result as string, mimeType);
        reader.readAsDataURL(blob);
      };
      App.mediaRecorder.start(100);
      App.isRecording = true;
      App.voiceBtn!.classList.add('recording');
      App.setState(App.State.LISTENING);
    } catch (err) {
      const e = err as DOMException;
      console.error('[Voice] 录音启动失败:', e.name, e.message);
      let msg = '无法访问麦克风：' + (e.message || e.name);
      if (e.name === 'NotAllowedError' || e.name === 'PermissionDeniedError') {
        msg = '麦克风权限被拒绝，请在浏览器地址栏点击锁形图标重新授权';
      } else if (e.name === 'NotReadableError') {
        msg = '麦克风被其他应用占用，请关闭其他使用麦克风的程序';
      } else if (e.name === 'NotFoundError') {
        msg = '未找到麦克风设备';
      } else if (e.message === '麦克风未就绪') {
        msg = '麦克风未就绪，请重新点击麦克风按钮';
      }
      App.showToast(msg);
      App.isRecording = false;
      App.voiceBtn!.classList.remove('recording');
      App.setState(App.State.IDLE);
    }
  };
  App.stopRecording = function stopRecording(cancel = false) {
    if (!App.mediaRecorder || App.mediaRecorder.state === 'inactive') {
      App.isRecording = false;
      App.voiceBtn!.classList.remove('recording');
      return;
    }
    if (cancel) {
      App.audioChunks = [];
      App.mediaRecorder.ondataavailable = null;
      App.mediaRecorder.onstop = () => {
        // 按住模式下取消录音也释放流；自动模式由 VAD 管理
        if (App.voiceMode !== 'auto' && App.micStream) {
          App.micStream.getTracks().forEach(tr => tr.stop());
          App.micStream = null;
        }
      };
    }
    // 强制刷新最后一帧数据，防止 webm 文件不完整
    if (App.mediaRecorder.state === 'recording') {
      App.mediaRecorder.requestData();
    }
    App.mediaRecorder.stop();
    App.isRecording = false;
    App.voiceBtn!.classList.remove('recording');
    if (!cancel) {
      // 发送后进入「思考中」状态（服务端流式回复会接力 thinking/listening）
      App.setState(App.State.THINKING);
      App.showTyping();
    } else {
      App.setState(App.State.IDLE);
    }
  };
  /* ============================================================
   *  VAD 自动对话模式（无需按住，说话即录、停顿即发）
   * ============================================================ */

  // 监控麦克风权限变化，权限被撤销时提示用户
  if (navigator.permissions && navigator.permissions.query) {
    try {
      navigator.permissions.query({ name: 'microphone' }).then(permissionStatus => {
        permissionStatus.onchange = () => {
          console.log('[Voice] 麦克风权限变化:', permissionStatus.state);
          if (permissionStatus.state === 'denied') {
            App.showToast('麦克风权限已被拒绝，请在浏览器设置中重新授权');
            // 清理可能持有的流
            if (App.micStream) {
              App.micStream.getTracks().forEach(t => t.stop());
              App.micStream = null;
            }
            if (App.voiceMode === 'auto') App.setVoiceMode('press');
          }
        };
      });
    } catch (e) {
      // 部分浏览器不支持查询麦克风权限，静默忽略
    }
  }
});
