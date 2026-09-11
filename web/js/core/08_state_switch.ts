import type { AppKernel, PerfTier } from '../types/app-kernel.js';

const PERF_TIERS: PerfTier[] = ['high', 'default', 'low'];

export default (function init(App: AppKernel) {
  /* ============================================================
   *  状态切换 / 性能分级系统
   * ============================================================ */
  // 性能分级: 'high'=高性能60fps(手动选择) | 'default'=均衡省电30fps(默认) | 'low'=极致省电20fps
  // 2026-09-10 需求：角色清晰度优先 —— 三档 DPR 全部锁定原生物理像素（钳制上限2），
  // 不再降分辨率换取帧率；性能压力转由环境降载（星空/光晕）与帧率档位承担。
  // 档位差异仅剩：帧率(60/30/20fps) + 星空粒子 + VAD 采样率。
  App.perfTier = 'default';
  App._renderFrameSkip = 1;       // 每N帧渲染1次 (1=每帧, 2=隔帧30fps, 3=20fps)
  App._renderFrameCount = 0;     // 帧计数器
  App._vadFrameSkip = 1;          // VAD检测每N帧执行1次
  App._vadFrameCount = 0;
  App._baseFrameSkip = 1;         // 当前档位的基准帧率（自适应降帧后可恢复）
  App._envLevel = 0;              // 环境降载等级 0=全开 1=隐星空 2=隐光晕阴影 3=再降帧率

  /** 角色清晰度基石：原生渲染分辨率（钳制上限2，3x屏边际收益小且GPU压力大增） */
  App._nativeDPR = function _nativeDPR() {
    return Math.min(window.devicePixelRatio || 1, 2);
  };

  /** 自动检测设备性能级别 */
  App.detectPerfTier = function detectPerfTier() {
    // 优先使用用户手动选择保存的级别
    try {
      const saved = localStorage.getItem('dabai.perfTier');
      if (saved && PERF_TIERS.includes(saved as PerfTier)) {
        App.setPerfTier(saved as PerfTier);
        return;
      }
    } catch (_) {}
    // 检查是否为移动设备
    const isMobile = /Android|iPhone|iPad|iPod|webOS/i.test(navigator.userAgent) ||
      ('ontouchstart' in window && window.innerWidth < 1024);
    // 检查是否低端设备（内存<4GB粗略判断）
    const lowMemory = navigator.deviceMemory && navigator.deviceMemory < 4;

    if (isMobile && lowMemory) {
      App.setPerfTier('low');
    } else {
      // 桌面/移动默认都走均衡省电档（30fps），降低常驻功耗；高性能档保留手动切换
      App.setPerfTier('default');
    }
  };

  /** 设置性能级别并应用对应优化 */
  App.setPerfTier = function setPerfTier(tier: PerfTier) {
    App.perfTier = tier;
    // 清晰度统一策略：渲染分辨率 = 原生物理像素（上限2）。
    // 低分屏(DPR<1.75，如桌面1x显示器)补开MSAA抗锯齿；高分屏像素密度已足够，关AA省GPU。
    App._targetDPR = App._nativeDPR();
    App._useAA = App._targetDPR < 1.75;
    switch (tier) {
      case 'high':
        App._renderFrameSkip = 1;   // 60fps
        App._vadFrameSkip = 1;      // 全速VAD
        App._starCount = 120;       // 星空粒子
        break;
      case 'default':
        App._renderFrameSkip = 2;   // 30fps（清晰度无损：降的是时间帧率不是空间分辨率）
        App._vadFrameSkip = 2;      // 半速VAD (~30fps)
        App._starCount = 80;
        break;
      case 'low':
        App._renderFrameSkip = 3;   // 20fps
        App._vadFrameSkip = 3;      // 1/3速VAD (~20fps)
        App._starCount = 0;         // 低功耗下去掉粒子
        break;
    }
    App._baseFrameSkip = App._renderFrameSkip;
    App._envLevel = 0;
    // 运行时更新渲染器
    if (App.renderer) {
      App.renderer.setPixelRatio(App._targetDPR);
      // 低功耗下去掉星空粒子
      if (App.starField) {
        App.starField.visible = App._starCount > 0;
      }
    }
    // 更新帧率按钮显示
    const fpsLabel = document.getElementById('fps-label');
    if (fpsLabel) {
      fpsLabel.textContent = String(Math.round(60 / App._renderFrameSkip));
    }
    // 持久化
    try { localStorage.setItem('dabai.perfTier', tier); } catch (_) {}
    console.log('[Perf] 性能级别:', tier,
      '渲染:', Math.round(60 / App._renderFrameSkip) + 'fps',
      'VAD:', Math.round(60 / App._vadFrameSkip) + 'fps',
      'DPR:', App._targetDPR, '(原生,清晰度锁定)',
      'AA:', App._useAA);
  };

  /** 循环切换性能级别：high → default → low → high */
  App.cyclePerfTier = function cyclePerfTier() {
    const idx = PERF_TIERS.indexOf(App.perfTier);
    const next = PERF_TIERS[(idx + 1) % PERF_TIERS.length];
    App.setPerfTier(next);
    const fps = Math.round(60 / App._renderFrameSkip);
    App.showToast('帧率: ' + fps + 'fps · ' + (fps <= 20 ? '极致省电' : fps <= 30 ? '流畅省电' : '最佳画质'));
  };

  /** 渲染帧节流：返回true表示本帧应该渲染 */
  App.shouldRenderFrame = function shouldRenderFrame() {
    App._renderFrameCount++;
    if (App._renderFrameCount >= App._renderFrameSkip) {
      App._renderFrameCount = 0;
      return true;
    }
    return false;
  };

  /** VAD帧节流：返回true表示本帧应该执行VAD检测 */
  App.shouldVADFrame = function shouldVADFrame() {
    App._vadFrameCount++;
    if (App._vadFrameCount >= App._vadFrameSkip) {
      App._vadFrameCount = 0;
      return true;
    }
    return false;
  };

  /* ============================================================
   * 动态环境降载 + 轻量内存巡检
   * ============================================================ */
  // 依据实测帧率自动降载：帧率低 → 依次牺牲环境视觉（星空→光晕/阴影→帧率），
  // 渲染分辨率(DPR)钉死原生物理像素永不下降 —— 角色清晰度是第一优先级。
  // 帧率恢复 → 逆向逐级还原。只在性能档位区间内浮动，不改变任何场景/游戏逻辑。
  App._adaptiveDPR = true;        // 动态降载总开关（历史命名，语义=自适应降载）
  App._fpsAccum = 0;
  App._fpsCount = 0;
  App._lastFpsCheck = 0;
  App._dprAdjustAt = 0;          // 上次降/升载时间（历史命名，语义=上次调整时间）

  /** 应用环境降载等级（只动环境元素可见性，角色模型与相机永远全保真） */
  App._applyEnvLevel = function _applyEnvLevel() {
    const lv = App._envLevel;
    try {
      // 星空粒子：lv>=1 隐藏
      if (App.starField) App.starField.visible = lv < 1 && App._starCount > 0;
      // 地面光晕 + 接触阴影：lv>=2 隐藏（纯装饰，对角色表现无影响）
      if (App.parts && App.parts.glow) App.parts.glow.visible = lv < 2;
      if (App.parts && App.parts.contactShadow) App.parts.contactShadow.visible = lv < 2;
    } catch (_) { /* 环境降载失败不影响主流程 */ }
  };

  /** 每帧调用（仅渲染帧）：统计真实帧率，按需动态降载环境（不降分辨率） */
  App.adaptiveFrame = function adaptiveFrame(dt: number) {
    if (!App._adaptiveDPR || !App.renderer || App.perfTier === 'low') return;
    // XR 会话中帧缓冲尺寸由头显接管，setPixelRatio/setSize 是无效调用
    // （three.js 会告警并返回），还会基于 XR 高帧率误判 —— 直接跳过
    if (App.renderer.xr && App.renderer.xr.isPresenting) return;
    App._fpsAccum += dt;
    App._fpsCount++;
    const now = performance.now();
    if (now - App._lastFpsCheck < 2000) return;
    const avg = App._fpsAccum / Math.max(1, App._fpsCount);
    const fps = 1 / Math.max(avg, 0.001);
    const target = App.perfTier === 'high' ? 48 : 27;
    if (fps < target * 0.8 && App._envLevel < 3 && now - App._dprAdjustAt > 2500) {
      App._envLevel++;
      if (App._envLevel === 3) {
        // 最后手段：降帧率档（清晰度无损，只是流畅度让步），最高 20fps
        App._renderFrameSkip = Math.min(App._baseFrameSkip + 1, 3);
        const fpsLabel = document.getElementById('fps-label');
        if (fpsLabel) fpsLabel.textContent = String(Math.round(60 / App._renderFrameSkip));
      }
      App._applyEnvLevel();
      App._dprAdjustAt = now;
      console.log('[Perf] 帧率 ' + Math.round(fps) + 'fps 偏低，环境降载 → level ' + App._envLevel +
        (App._envLevel === 3 ? '（帧率 ' + Math.round(60 / App._renderFrameSkip) + 'fps）' : '') +
        ' · DPR ' + App._targetDPR.toFixed(2) + ' 保持不变');
    } else if (fps >= target && App._envLevel > 0 && now - App._dprAdjustAt > 6000) {
      App._envLevel--;
      if (App._envLevel === 2) {
        // 从降帧档恢复
        App._renderFrameSkip = App._baseFrameSkip;
        const fpsLabel = document.getElementById('fps-label');
        if (fpsLabel) fpsLabel.textContent = String(Math.round(60 / App._renderFrameSkip));
      }
      App._applyEnvLevel();
      App._dprAdjustAt = now;
    }
    App._fpsAccum = 0;
    App._fpsCount = 0;
    App._lastFpsCheck = now;
  };

  /** 回到当前性能档位的基准状态（进/出游戏、切换档位时调用）：分辨率重钉原生 + 降载复位 */
  App.resetAdaptiveDPR = function resetAdaptiveDPR() {
    App._targetDPR = App._nativeDPR();
    if (App.renderer) App.renderer.setPixelRatio(App._targetDPR);
    App._renderFrameSkip = App._baseFrameSkip;
    App._envLevel = 0;
    App._applyEnvLevel();
    App._fpsAccum = 0;
    App._fpsCount = 0;
    App._lastFpsCheck = 0;
    App._dprAdjustAt = 0;
    const fpsLabel = document.getElementById('fps-label');
    if (fpsLabel) fpsLabel.textContent = String(Math.round(60 / App._renderFrameSkip));
  };

  /** 轻量内存巡检：主循环每 30 秒调用一次，释放可安全回收的资源 */
  App.memoryTick = function memoryTick() {
    try {
      // 释放已结束/暂停的 TTS blob URL（播放中不打断）
      if (App.currentAudio && App.currentAudio._blobUrl
        && App.currentState !== App.State.SPEAKING && App.currentAudio.paused) {
        try {
          URL.revokeObjectURL(App.currentAudio._blobUrl);
          App.currentAudio._blobUrl = null;
          App.currentAudio.src = '';
        } catch (e) { /* ignore */ }
      }
      // WebGL 统计信息自动归零，避免 info 计数长期累积（不影响渲染）
      if (App.renderer && App.renderer.info) App.renderer.info.autoReset = true;
    } catch (e) { /* 巡检失败不影响主流程 */ }
  };

  /** 进入游戏前调用：释放大厅残留资源 + 复位自适应分辨率 */
  App.prepareForGame = function prepareForGame() {
    App.resetAdaptiveDPR();
    if (App.memoryTick) App.memoryTick();
  };

  /** 退出游戏后调用：复位自适应分辨率，回到大厅最佳档位 */
  App.prepareForLobby = function prepareForLobby() {
    App.resetAdaptiveDPR();
    if (App.memoryTick) App.memoryTick();
  };

  /* ============================================================
   *  锁屏模式（防误触）
   *  锁定玩家的一切操作与互动（拖拽/点击/按钮/打字），仅保留语音对话；
   *  角色动画与 AI 自主行为照常运行；只有再按锁屏按钮才能解除。
   * ============================================================ */
  App.enterLockMode = function enterLockMode() {
    App.lockMode = true;
    localStorage.setItem(App.LOCK_KEY, '1');

    // 退出玩家操控类模式（锁屏下玩家不可操作，角色自由移动）
    if (App.fpvMode) {
      App.exitFPV();
    }
    if (App.moveMode) {
      App.setMoveMode(false);
    }

    // 退出 VR 模式（WebXR 会话结束）
    if (App.exitXrMode && App.xrMode !== 'off') {
      App.exitXrMode();
    }
    // 清理VR晃动强度状态（晃动回调因 vs 为 null 直接返回，避免残留误加）
    App.vrShake = null;

    // 关闭已打开的弹窗
    document.querySelectorAll('.modal.show').forEach(m => m.classList.remove('show'));

    // 折叠聊天面板（消息经字幕/气泡仍可见）；输入栏保留语音按钮，文本框由 CSS 隐藏
    const chatPanel = document.getElementById('chat-panel');
    if (chatPanel) chatPanel.classList.add('collapsed');

    // CSS 接管交互锁定：画布手势禁用 + 隐藏全部按钮（仅留锁屏键）
    document.body.classList.add('locked');
    const lockBtn = App.$('lock-mode-btn');
    if (lockBtn) lockBtn.classList.add('active');

    App.showToast('已锁屏 · 防误触，仅语音对话');
    console.log('[Lock] 锁屏模式已开启 —— 操作已锁定，角色照常活动，仅可语音对话');
  };
  App.exitLockMode = function exitLockMode() {
    App.lockMode = false;
    localStorage.removeItem(App.LOCK_KEY);
    document.body.classList.remove('locked');
    const lockBtn = App.$('lock-mode-btn');
    if (lockBtn) lockBtn.classList.remove('active');

    // 恢复聊天面板与未读标记
    const chatPanel = document.getElementById('chat-panel');
    if (chatPanel) chatPanel.classList.remove('collapsed');
    if (App.chatToggle) App.chatToggle.classList.remove('has-new');

    // 动画循环全程未停止，无需恢复；仅同步一次画布尺寸兜底
    if (App.onResize) App.onResize();

    App.showToast('已解锁');
    console.log('[Lock] 锁屏模式已解除 —— 操作已恢复');
  };

  /* ============================================================
   *  沉浸模式：隐藏所有UI按钮，防误触，长按屏幕退出
   * ============================================================ */
  App.immerseMode = false;
  App._immersePressTimer = null;

  App.toggleImmerseMode = function toggleImmerseMode() {
    App.immerseMode = !App.immerseMode;
    if (App.immerseMode) {
      document.body.classList.add('immersed');
      App.showToast('长按屏幕任意位置退出沉浸模式');
    } else {
      document.body.classList.remove('immersed');
    }
    console.log('[Immerse]', App.immerseMode ? '沉浸模式开启' : '沉浸模式关闭');
  };

  /** 长按屏幕退出沉浸模式（仅单指，双指手势不触发，微移容差5px） */
  App.initImmerseLongPress = function initImmerseLongPress() {
    let activePointers = 0;
    let startX = 0, startY = 0;
    const MOVE_THRESHOLD = 5; // 像素容差，避免手指微颤取消长按

    const clearTimer = () => {
      if (App._immersePressTimer) {
        clearTimeout(App._immersePressTimer);
        App._immersePressTimer = null;
      }
    };

    document.body.addEventListener('pointerdown', (e) => {
      if (!App.immerseMode) return;
      activePointers++;
      // 只有单指触摸才开始计时，多指按下时取消
      if (activePointers > 1) {
        clearTimer();
        return;
      }
      startX = e.clientX;
      startY = e.clientY;
      App._immersePressTimer = setTimeout(() => {
        App.toggleImmerseMode();
        App.showToast('已退出沉浸模式');
      }, 800);
    });

    document.body.addEventListener('pointerup', () => {
      activePointers = Math.max(0, activePointers - 1);
      clearTimer();
    });
    document.body.addEventListener('pointercancel', () => {
      activePointers = Math.max(0, activePointers - 1);
      clearTimer();
    });
    // 移动超过阈值才取消（允许手指微颤）
    document.body.addEventListener('pointermove', (e) => {
      if (!App._immersePressTimer) return;
      const dx = e.clientX - startX;
      const dy = e.clientY - startY;
      if (dx * dx + dy * dy > MOVE_THRESHOLD * MOVE_THRESHOLD) {
        clearTimer();
      }
    });
  };
  App.toggleLockMode = function toggleLockMode() {
    if (App.lockMode) {
      App.exitLockMode();
    } else {
      App.enterLockMode();
    }
  };
  App.setState = function setState(s: AppKernel['currentState']) {
    // 状态切换时重置口型平滑值，防止从 SPEAKING 切出后嘴部残留
    if (s !== App.State.SPEAKING && App.smoothMouth !== undefined) {
      App.smoothMouth = 0;
    }
    App.currentState = s;
    App.statusBadge!.className = 'status-badge';
    const dot = App.statusBadge!.querySelector('.status-dot');
    const map: Record<string, { t: string; c: string }> = {
      [App.State.IDLE]: {
        t: '在线',
        c: 'online'
      },
      [App.State.THINKING]: {
        t: '思考中',
        c: 'thinking'
      },
      [App.State.LISTENING]: {
        t: '聆听中',
        c: 'listening'
      },
      [App.State.SPEAKING]: {
        t: '说话中',
        c: 'speaking'
      }
    };
    const v = map[s]!;
    // 保留指示灯，仅更新文字
    if (dot) {
      App.statusBadge!.innerHTML = '';
      App.statusBadge!.appendChild(dot);
      App.statusBadge!.appendChild(document.createTextNode(' ' + v.t));
    } else {
      App.statusBadge!.textContent = v.t;
    }
    App.statusBadge!.classList.add(v.c);
    // 统一调度纪律：AI 空闲时补发排队中的 AI action（不互相打扰、不打断用户）
    if (s === App.State.IDLE && App._flushPendingAIActions) {
      // 延迟一帧补发，避免与刚结束的回复时序冲突
      setTimeout(App._flushPendingAIActions, 50);
    }
  };
  App.showSubtitle = function showSubtitle(text: string) {
    if (!text) {
      App.subtitle!.classList.remove('show');
      return;
    }
    App.subtitle!.textContent = text;
    App.subtitle!.classList.add('show');
  };
  /* ============================================================
   *  WebSocket
   * ============================================================ */
  /* ============================================================
   *  场景状态持久化（相机/模型位置/缩放）
   * ============================================================ */
});
