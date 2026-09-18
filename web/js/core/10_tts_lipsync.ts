import type { AppKernel } from '../types/app-kernel.js';
import type { AudioChunkMessage } from '../types/ws-protocol.js';

export default (function init(App: AppKernel) {
  /* ============================================================
   *  TTS 播放 + 口型同步
   * ============================================================ */
  App.ensureAudioCtx = function ensureAudioCtx() {
    if (!App.audioCtx) {
      App.audioCtx = new (window.AudioContext || window.webkitAudioContext)!();
      App.analyser = App.audioCtx.createAnalyser();
      App.analyser.fftSize = 256;
      App.analyser.smoothingTimeConstant = 0.35; // 适中平滑：既能跟踪音节，又能让字间快速归零
      App.analyserData = new Uint8Array(App.analyser.frequencyBinCount);
      // 分析器只需连接一次 destination
      App.analyser.connect(App.audioCtx.destination);
      console.log('[Audio] AudioContext + Analyser 已就绪');
    }
    if (App.audioCtx.state === 'suspended') {
      App.audioCtx.resume().then(() => console.log('[Audio] AudioContext resumed')).catch(() => {});
    }
  };
  App.playNextAudio = function playNextAudio() {
    if (App.audioQueue.length === 0) {
      // 队列暂时空但未收到 end → 允许后续 chunk 重新触发播放
      App.isPlayingQueue = false;
      return;
    }
    const chunk = App.audioQueue.shift()!;
    // 哨兵：整个回复结束
    if (chunk.end) {
      // 说话结束：气泡淡出
      if (App.hideChatBubble) App.hideChatBubble();
      // 服务端在 audio_end 一次性下发完整文本：等全部语音播完才应用，
      // 保证字幕/气泡严格跟随语音，最后才补全为整段回复（消息区显示最终全文）
      if (App._pendingFullText) {
        if (App.pendingAIMsgEl) {
          // 富文本渲染（thinking/工具/turn-text 共存时只写文本区；普通气泡直接整条渲染）
          if (App.renderTurnText) {
            App.renderTurnText(App._pendingFullText);
          } else if (App.renderMsgMedia) {
            App.renderMsgMedia(App.pendingAIMsgEl, App._pendingFullText);
          } else {
            App.pendingAIMsgEl.textContent = App._pendingFullText;
          }
          if (App.taskBoardAddMedia) {
            const urls = App.extractMediaUrls ? App.extractMediaUrls(App._pendingFullText) : [];
            if (urls.length) App.taskBoardAddMedia(urls);
          }
          // 最近的交互信息 → 大屏全屏焦点（每条停留 1 分钟，无新信息回待机）
          if (App.taskBoardOnInteraction) {
            App.taskBoardOnInteraction({
              kind: 'msg',
              title: '💬 大白 · 刚刚回复',
              text: App._pendingFullText,
              accent: '#7c5cff',
              tag: '💬 大白回复'
            });
          }
          // textContent 会清掉子元素，token 徽章必须在赋值之后追加
          if (App.attachMsgTokenBadge) App.attachMsgTokenBadge(App.pendingAIMsgEl);
        }
        App.currentReplyText = App._pendingFullText;
        App._pendingFullText = null;
      }
      App.isPlayingQueue = false;
      if (App.pendingAIMsgEl) {
        App.pendingAIMsgEl.classList.remove('streaming');
        App.pendingAIMsgEl = null;
      }
      App.currentReplyText = '';
      App.currentReplySeg = '';
      App.currentReplySession = null;
      App.setState(App.State.IDLE);
      App.showSubtitle('');
      // VAD 自动对话：AI 说完后恢复监听（清掉 TTS 尾音回声对语音评分的残留）
      if (App.voiceMode === 'auto') App.vadResumeAfterSpeak();
      return;
    }
    App.isPlayingQueue = true;

    // 本段语音开始播放才揭示文本：字幕/气泡严格跟随语音进度。
    // 服务端可能在 LLM 结束后一次性推完所有分片（TTS 批量完成），
    // 若在分片到达时就拼接，气泡会瞬间显示整段回复，视觉上"不跟随语音"。
    if (chunk.text) {
      // 即时文本流（stream_text）已接管主消息区：audio_chunk 的文本只驱动
      // 跟随语音的浮动气泡；旧服务端（无 stream_text）仍由音频逐句揭示文字
      // 思考语音（thinking）只播放/显示气泡，绝不拼进主回复文本
      if (!App._streamTextOn && !chunk.thinking) {
        App.currentReplyText += chunk.text;
        // 回合气泡：只写 .turn-text（思考/工具段不被 textContent 清掉）；普通气泡整条写
        if (App.pendingAIMsgEl) {
          if (App.setTurnStreamText) App.setTurnStreamText(chunk.text);
          else App.pendingAIMsgEl.textContent = App.currentReplyText;
        }
      }
      // 气泡与语音一一对应：只显示本句正在说的分句文本（chunk.text），不累积整段回复；
      // 换句播放时气泡文本整体刷新，聊天区消息仍按整段累积（App.currentReplyText）
      // seq + session 一并传入：气泡内部据此丢弃乱序/过期的迟到分句（高并发防串台）
      if (App.showChatBubble) App.showChatBubble(chunk.text, chunk.seq, chunk.session_id);
      console.log('[TTS] 播放揭示: 累积长度', App.currentReplyText.length);
    } else if (App.hideChatBubble) {
      // 纯音频/无文本分片：当前句已说完，气泡随之隐藏，保持一一对应
      App.hideChatBubble();
    }

    // 无音频数据（TTS 失败），跳过只保留文本（文本已在上方揭示）
    if (!chunk.audio_b64) {
      console.warn('[TTS] 本分片无音频，跳过（仅显示文本）');
      // 连片无音频时不再无界递归自调用：服务端一次性冲刷上百个分片会深递归到栈溢出。
      // 超过 32 层就让出一次事件循环；气泡绘制已按帧合并，突发冲刷只会重绘 1 次。
      App._ttsSkipDepth = (App._ttsSkipDepth || 0) + 1;
      if (App._ttsSkipDepth > 32) {
        App._ttsSkipDepth = 0;
        setTimeout(() => App.playNextAudio(), 0);
      } else {
        App.playNextAudio();
      }
      return;
    }
    App._ttsSkipDepth = 0; // 进入真实播放：递归链断开
    App.ensureAudioCtx();
    App.setState(App.State.SPEAKING);
    // 字幕跟随语音当前句；旧服务端无即时文本流时仍显示累积文本
    if (chunk.thinking) App.showSubtitle(chunk.text || '');
    else App.showSubtitle(App._streamTextOn && chunk.text ? chunk.text : App.currentReplyText);

    // 释放上一个 Audio + MediaElementSource + 销毁旧的 blob URL
    if (App.currentAudio) {
      App.currentAudio.onended = null;
      App.currentAudio.onerror = null;
      App.currentAudio.pause();
      // 断开 MediaElementSourceNode，防止 AudioContext 节点泄露
      if (App.currentAudioSource) {
        try { App.currentAudioSource.disconnect(); } catch (e) { /* 已断开 */ }
        App.currentAudioSource = null;
      }
      if (App.currentAudio._blobUrl) {
        URL.revokeObjectURL(App.currentAudio._blobUrl);
        App.currentAudio._blobUrl = null;
      }
      App.currentAudio.src = '';
    }

    // base64 → Blob → blob URL（播放完即时销毁，不积压缓存）
    const binary = atob(chunk.audio_b64);
    const bytes = new Uint8Array(binary.length);
    for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
    const blob = new Blob([bytes], { type: chunk.audio_mime || 'audio/mpeg' });
    const blobUrl = URL.createObjectURL(blob);

    App.currentAudio = new Audio(blobUrl);
    App.currentAudio._blobUrl = blobUrl; // 记录以便后续销毁
    // 每个 Audio 元素只能 createMediaElementSource 一次
    try {
      App.currentAudioSource = App.audioCtx!.createMediaElementSource(App.currentAudio);
      App.currentAudioSource.connect(App.analyser!);
    } catch (e) {
      console.warn('[Audio] createMediaElementSource 失败:', (e as Error).message);
    }
    App.currentAudio.onplay = () => {
      if (App.isPlayingQueue) App.setState(App.State.SPEAKING);
    };
    App.currentAudio.onended = () => {
      // 播完立即销毁 blob URL，不留缓存
      if (App.currentAudio && App.currentAudio._blobUrl) {
        URL.revokeObjectURL(App.currentAudio._blobUrl);
        App.currentAudio._blobUrl = null;
      }
      App.playNextAudio();
    };
    App.currentAudio.onerror = () => {
      console.warn('[Audio] 播放失败，跳过');
      if (App.currentAudio && App.currentAudio._blobUrl) {
        URL.revokeObjectURL(App.currentAudio._blobUrl);
        App.currentAudio._blobUrl = null;
      }
      App.playNextAudio();
    };
    App.currentAudio.play().catch(e => {
      console.warn('[Audio] play() rejected:', e);
      if (App.audioCtx && App.audioCtx.state === 'suspended') {
        App.showToast('点击页面任意位置以启用音频');
      }
      App.playNextAudio();
    });
  };
  /** 创建/复用回复占位消息（回合气泡优先，否则普通气泡） */
  function ensurePendingMsg() {
    if (App.pendingAIMsgEl) return App.pendingAIMsgEl;
    const turnB = App._turnMsgEl;
    if (turnB && document.body.contains(turnB)) {
      App.pendingAIMsgEl = turnB;
      App.pendingAIMsgEl.classList.add('streaming');
    } else {
      App.pendingAIMsgEl = document.createElement('div');
      App.pendingAIMsgEl.className = 'msg ai streaming';
      App.messagesEl!.appendChild(App.pendingAIMsgEl);
      if (App.bumpNewMsg) App.bumpNewMsg(App.pendingAIMsgEl); // 用户在翻历史：登记未读但不打扰
      App.scrollToBottom();
    }
    return App.pendingAIMsgEl;
  }
  /* 即时文本流：回复正文到达立即显示，不等语音（服务端 stream_text） */
  App.handleStreamText = function handleStreamText(text: string) {
    if (!text) return;
    // 正文开始流式 = 思考结束：收掉「思考中」实时指示行
    if (App.clearReasoningLine) App.clearReasoningLine();
    App._streamTextOn = true;
    App.removeTyping();
    App.setState(App.State.SPEAKING);
    ensurePendingMsg();
    App.currentReplyText += text;
    if (App.pendingAIMsgEl) {
      // 增量文本流式写入当前正文段（Markdown + 媒体内联渲染）
      const container = App.turnTextContainer ? App.turnTextContainer() : App.pendingAIMsgEl;
      if (App.setTurnStreamText) App.setTurnStreamText(text);
      else if (container && App.mdToHtml) container.innerHTML = App.mdToHtml(App.currentReplyText);
      else if (container) container.textContent = App.currentReplyText;
    }
    App.scrollToBottom();
  };
  /* 工具轮过程话撤回：把刚实时流出的文字从主回复移出（服务端已转成 thinking_text） */
  App.handleRetractText = function handleRetractText(length: number) {
    if (!length || !App.currentReplyText) return;
    App.currentReplyText = App.currentReplyText.slice(
      0, Math.max(0, App.currentReplyText.length - length));
    const container = App.turnTextContainer ? App.turnTextContainer() : App.pendingAIMsgEl;
    if (container) {
      // 旧服务端兼容路径：正文段按剩余文本重渲染（走实时 Markdown，保持结构化）
      container.dataset.raw = App.currentReplyText;
      if (App.setTurnStreamText) App.setTurnStreamText('');
      else container.textContent = App.currentReplyText;
    }
    App.scrollToBottom();
  };
  /* 流式音频分片到达 */
  App.handleAudioChunk = function handleAudioChunk(msg: AudioChunkMessage) {
    // 过滤过期 session 的消息
    if (App.currentReplySession && msg.session_id && msg.session_id !== App.currentReplySession) {
      console.warn('[TTS] 分片 session 不匹配被丢弃', msg.session_id, '当前', App.currentReplySession);
      return;
    }
    // 拦截被打断 session 的迟到分片（见 triggerInterrupt 的会话栅栏）
    if (App._interruptedSession && msg.session_id === App._interruptedSession) {
      console.warn('[TTS] 被打断 session 的迟到分片被丢弃', msg.session_id);
      return;
    }
    console.log('[TTS] chunk', msg.seq, 'text=', JSON.stringify((msg.text || '').slice(0, 40)),
      'audio=', msg.audio_b64 ? msg.audio_b64.length + 'B' : 'null', 'session=', msg.session_id);
    App.removeTyping();
    // 首句到达即进入说话态：即使 TTS 音频为空（合成失败），
    // 气泡/字幕也能立即显示回复文本，而不是停留在思考省略号
    App.setState(App.State.SPEAKING);
    // 首句到达时创建/复用占位消息：已有回合气泡（思考/工具内联）则直接
    // 在其 .turn-text 上继续，保持「思考 → 工具 → 回复」同一条气泡；否则新建普通气泡
    ensurePendingMsg();
    App.audioQueue.push({
      seq: msg.seq!,
      text: msg.text,
      audio_b64: msg.audio_b64,
      audio_mime: msg.audio_mime,
      thinking: !!msg.thinking,
      session_id: msg.session_id // 供气泡做「同会话序号单调」守卫
    });
    App.currentReplySeg = msg.text || ''; // 流式分句（嘴型用）；气泡/字幕文本在播放时揭示
    App.scrollToBottom();

    // 情绪驱动：从累积回复文本检测情绪，每轮回复只触发一次
    if (!msg.thinking && App.detectReplyEmotion && App.onReplyEmotion && !App._replyEmotionDone) {
      const e = App.detectReplyEmotion((App.currentReplyText || '') + (msg.text || ''));
      if (e) {
        App.onReplyEmotion(e);
        App._replyEmotionDone = true;
      }
    }

    // 首句立即播放
    if (!App.isPlayingQueue) App.playNextAudio();
  };
  /* 整个回复结束 */
  App.handleAudioEnd = function handleAudioEnd(msg: AudioChunkMessage) {
    if (App.currentReplySession && msg.session_id && msg.session_id !== App.currentReplySession) return;
    if (App._interruptedSession && msg.session_id === App._interruptedSession) return;
    console.log('[TTS] audio_end, full_text=', JSON.stringify((msg.full_text || '').slice(0, 40)));
    // 收尾兜底：无论正文是否流式过，回复结束都清掉思考指示
    if (App.clearReasoningLine) App.clearReasoningLine();
    App._toolRunningSince = 0;
    // 完整文本先暂存：等语音队列全部播完（哨兵）再应用，
    // 避免音频还在逐句播放时全文提前刷出（字幕要跟随语音进度）
    if (msg.full_text && msg.full_text.trim()) {
      App._pendingFullText = msg.full_text.trim();
    }
    // 本轮回复结束：允许下一轮重新触发情绪检测
    App._replyEmotionDone = false;
    // 推入结束哨兵
    App.audioQueue.push({
      seq: Infinity,
      text: '',
      audio_b64: null,
      audio_mime: null,
      end: true
    });
    if (!App.isPlayingQueue) App.playNextAudio();
    // 本轮回复完成：工作流工具链卡片收尾（步骤标绿/标错）
    if (App.toolChainEndTurn) App.toolChainEndTurn();
  };
  /* 被打断 */
  App.handleInterrupted = function handleInterrupted() {
    if (App.clearReasoningLine) App.clearReasoningLine();
    if (App.clearStuckHint) App.clearStuckHint();
    App._toolRunningSince = 0;
    App.clearAudioQueue();
    // 服务端确认取消后，该 session 不会再发分片，栅栏可解除
    App._interruptedSession = null;
    // 如果 VAD 正在录音中，不要覆盖录音状态；否则回到 IDLE
    if (App.vadState !== 'recording' && !App.isRecording) {
      App.setState(App.State.IDLE);
    }
    App.showSubtitle('');
    if (App.pendingAIMsgEl) {
      if (App.currentReplyText) App.pendingAIMsgEl.classList.remove('streaming');
      // 回合气泡（思考/工具/回复内联）不因“还没出文字”就被删掉，
      // 保留给 finishTurn 标记“已中断”；普通占位气泡才移除
      else if (App._turnMsgEl !== App.pendingAIMsgEl) App.pendingAIMsgEl.remove();
      App.pendingAIMsgEl = null;
    }
    App.currentReplyText = '';
    App.currentReplySeg = '';
    App.currentReplySession = null;
    App._pendingFullText = null;
    // 回复被打断：工具链卡片标记为已中断
    if (App.toolChainAbort) App.toolChainAbort();
  };
  /* 清空播放队列 + 停止当前播放 */
  App.clearAudioQueue = function clearAudioQueue() {
    App.audioQueue = [];
    App.isPlayingQueue = false;
    if (App.currentAudio) {
      App.currentAudio.onended = null;
      App.currentAudio.onerror = null;
      App.currentAudio.pause();
      // 断开 MediaElementSourceNode，防止 AudioContext 节点泄露
      if (App.currentAudioSource) {
        try { App.currentAudioSource.disconnect(); } catch (e) { /* 已断开 */ }
        App.currentAudioSource = null;
      }
      if (App.currentAudio._blobUrl) {
        URL.revokeObjectURL(App.currentAudio._blobUrl);
        App.currentAudio._blobUrl = null;
      }
      App.currentAudio.src = '';
      App.currentAudio = null;
    }
  };
  /* 主动打断：停止本地播放 + 通知服务端取消
   * force=true：用户显式点「中断」按钮 → 服务端即使正在跑工具任务也照取消；
   * 缺省 false：VAD/看门狗等自动来源，工具任务执行中会被服务端忽略（改排队）。 */
  App.triggerInterrupt = function triggerInterrupt(force?: boolean) {
    App.clearAudioQueue();
    // 会话栅栏：记住被打断的 session，拦截其迟到的 audio_chunk/audio_end
    // （服务端任务被取消前可能仍会冲刷出残余 TTS 分片，若不拦截，
    // 残余语音会播放出来并混入 VAD 录音，导致"打断思考"后录音被污染）
    if (App.currentReplySession) App._interruptedSession = App.currentReplySession;
    if (App.pendingAIMsgEl) {
      if (App.currentReplyText) App.pendingAIMsgEl.classList.remove('streaming');
      // 回合气泡不因“还没出文字”被删掉，留给 finishTurn 标记“已中断”
      else if (App._turnMsgEl !== App.pendingAIMsgEl) App.pendingAIMsgEl.remove();
      App.pendingAIMsgEl = null;
    }
    App.currentReplyText = '';
    App.currentReplySeg = '';
    App.currentReplySession = null;
    App._pendingFullText = null;
    // 立即复位状态徽章：不等服务端确认也要从「说话中」恢复，
    // 避免打断后一直卡在说话中（服务端 interrupted 回执到达时再兜底一次）
    if (App.vadState !== 'recording' && !App.isRecording) {
      App.setState(App.State.IDLE);
    }
    if (App.ws && App.ws.readyState === WebSocket.OPEN) {
      App.ws.send(JSON.stringify({
        type: 'interrupt',
        force: !!force
      }));
    }
  };
});
