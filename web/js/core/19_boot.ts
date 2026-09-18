import { EngagementRLAgent } from "./../rl/engagement-rl-agent.ts";
import { DatingRLSystem } from "./../rl/dating-rl-system.ts";
import { ExpressionRLAgent } from "./../rl/expression-rl-agent.ts";
import type { AppKernel } from '../types/app-kernel.js';

export default (function init(App: AppKernel) {
  /* ============================================================
   *  启动
   * ============================================================ */
  window.addEventListener('load', async () => {
    // 设备性能自动检测（必须在 initThree 之前，影响渲染器参数）
    App.detectPerfTier();

    // 检查是否上次为锁屏模式（防误触，仅语音对话）
    if (localStorage.getItem(App.LOCK_KEY) === '1') {
      App.lockMode = true;
      // CSS 接管交互锁定：画布手势禁用 + 隐藏全部按钮（仅留锁屏键）
      document.body.classList.add('locked');
      const lockBtn = App.$('lock-mode-btn');
      if (lockBtn) lockBtn.classList.add('active');
      const chatPanel = document.getElementById('chat-panel');
      if (chatPanel) chatPanel.classList.add('collapsed');
      console.log('[Lock] 恢复锁屏模式（持久化）');
    }

    // 加载相机设置，必须在 initThree 之前
    App.loadCameraSettings();
    App.updateCameraSettingsUI();

    // 始终初始化3D场景（低功耗模式也需要渲染角色形象）
    App.initThree();
    // 默认态 = 非透明全屏：不等模型加载完就能直接对话；全屏静默停掉 3D 帧循环
    // （省电、手机不发热）。锁屏模式保持原样 —— 锁屏本就是「界面收起、仅语音」。
    if (localStorage.getItem(App.LOCK_KEY) !== '1' && App.setChatFullscreen) {
      App.setChatFullscreen(true);
    }
    App.restoreSceneState();
    setTimeout(() => {
      App.applySavedPositions();
    }, 500);
    App.bindEvents();
    App.initTTSConfig();
    App.initNameConfig();
    App.initRoleCards();
    App.connectWS();
    App.initSessionUI();
    App.addSystemMsg('正在连接服务器…');

    // 监听任意用户交互，用于 AI 自主触发计时
    ['click', 'touchstart', 'keydown', 'mousemove'].forEach(evt => {
      document.addEventListener(evt, () => {
        App.lastUserActivityTime = Date.now();
      }, {
        passive: true
      });
    });

    // 锁屏按钮事件（锁屏模式下唯一的出口：再按一次解锁）
    const lockBtn = App.$('lock-mode-btn');
    if (lockBtn) lockBtn.addEventListener('click', App.toggleLockMode);

    // 自动加载模型：优先按服务端当前激活的角色卡片配置加载（单系统模式：
    // 所有设备共享同一角色设定），否则恢复上次模型，否则加载列表第一个
    const activeCard: any = await App.restoreActiveRoleCard();
    // 恢复活动卡片的专属动作配置（未配置 → 执行全部动作）
    if (activeCard && App.setRoleAnimationConfig) {
      App.setRoleAnimationConfig(activeCard.animations);
    }
    let modelLoaded = false;
    const saved = JSON.parse(localStorage.getItem('dabai.currentModel') || 'null');
    const preferModel = activeCard && activeCard.model_url
      ? { url: activeCard.model_url as string, name: (activeCard.model_name || activeCard.model_url.split('/').pop()) as string }
      : null;
    try {
      const res = await fetch('/api/models');
      const data = await res.json();
      const models: { url: string; name?: string }[] = data.models || [];
      // 1. 角色卡片指定了外形 → 以卡片配置为准
      if (preferModel && models.some(m => m.url === preferModel.url)) {
        await App.loadModelFromUrl(preferModel.url, preferModel.name);
        modelLoaded = true;
      }
      // 2. 否则恢复上次使用的模型
      if (!modelLoaded && saved && saved.url && models.some(m => m.url === saved.url)) {
        await App.loadModelFromUrl(saved.url, saved.name);
        modelLoaded = true;
      } else if (saved && saved.url) {
        localStorage.removeItem('dabai.currentModel');
      }
      // 3. 都没有 → 加载列表第一个
      if (!modelLoaded && models.length > 0) {
        const first = models[0];
        await App.loadModelFromUrl(first.url, first.name);
        modelLoaded = true;
      }
    } catch {
      // 离线/出错则忽略
    }

    // 自动恢复上次使用的背景
    const savedBg = JSON.parse(localStorage.getItem('dabai.currentBackground') || 'null');
    if (savedBg && savedBg.url) {
      try {
        const res = await fetch('/api/backgrounds');
        const data = await res.json();
        const exists = (data.backgrounds || []).some((m: { url: string }) => m.url === savedBg.url);
        if (exists) {
          await App.loadBackgroundFromUrl(savedBg.url, savedBg.name);
        } else {
          localStorage.removeItem('dabai.currentBackground');
        }
      } catch {
        // 离线/出错则忽略，使用默认背景
      }
    }

    // 启动完成，之后模型/背景切换可以正常触发 AI 动作
    App._isBooting = false;

    // 沉浸模式：初始化长按退出手势
    App.initImmerseLongPress();

    // 互动强化学习（后台运行）
    App.initEngagementRL();

    // RL 自我表达：视觉层表情动作智能体（随互动/恋爱系统自动开启）
    App.initExpressionRL();
    if (App.engagementRLActive) {
      App._expressionRL?.start();
    }

    // 恋爱养成系统（默认跟随互动 RL 开启）
    const savedDating = localStorage.getItem('dabai.datingMode');
    if (savedDating === '1' || !savedDating) {
      // 首次使用或之前开启过 → 自动启动
      App.initDatingSystem();
      App.datingSystemActive = true;
      if (App._datingSystem && typeof (App._datingSystem as any).startSession === 'function') {
        App._datingSystem.startSession();
      } else {
        console.warn('[DatingRL] 无可用会话接口，跳过自动开启（不影响主功能）');
      }
      if (!savedDating) localStorage.setItem('dabai.datingMode', '1');
    }

    setTimeout(() => {
      // 只有没有历史消息时才显示欢迎语
      if (App.messagesEl!.children.length <= 1) {
        App.addAIMsg(`嗨~ 我是${(activeCard && (activeCard.role_name || activeCard.name)) || '大白'}，按住麦克风按钮或打字就能和我聊天啦`);
      }
    }, 1500);
  });

  // ==================== 互动 RL ====================
  App.initEngagementRL = function () {
    try {
      // 统一架构：与恋爱养成系统共享同一 UnifiedDatingSystem 实例
      if (App._datingSystem) {
        App._engagementRL = App._datingSystem;
        console.log('[EngagementRL] 复用统一RL系统实例（与恋爱养成共享）');
      } else {
        App._engagementRL = new EngagementRLAgent(App);
      }
      App.engagementRLActive = true;
      console.log('[EngagementRL] 互动智能体已启动');
    } catch (e) {
      console.warn('[EngagementRL] 初始化失败:', (e as Error).message);
      App.engagementRLActive = false;
    }
  };

  // ==================== RL 自我表达（视觉层表情动作） ====================
  App.initExpressionRL = function () {
    try {
      if (!App._expressionRL) {
        App._expressionRL = new ExpressionRLAgent(App);
      }
      console.log('[ExpressionRL] 自我表达智能体已初始化');
    } catch (e) {
      console.warn('[ExpressionRL] 初始化失败:', (e as Error).message);
    }
  };

  /** 切换 RL 自我表达（开/关） */
  App.toggleExpressionRL = function () {
    if (!App._expressionRL) App.initExpressionRL();
    const on = App._expressionRL.toggle();
    console.log('[ExpressionRL] 自我表达', on ? '已开启' : '已关闭');
    return on;
  };

  // ==================== 恋爱养成强化学习系统 ====================
  App.initDatingSystem = function () {
    try {
      // 统一架构：与互动RL共享同一 UnifiedDatingSystem 实例
      // （若互动 RL 先创建了无 startSession 的 EngagementRLAgent，则单独
      //  创建 DatingRLSystem，避免 boot 阶段调用 startSession 时报错）
      if (App._engagementRL && typeof (App._engagementRL as any).startSession === 'function') {
        App._datingSystem = App._engagementRL;
        console.log('[DatingRL] 复用统一RL系统实例（与互动RL共享）');
      } else {
        App._datingSystem = new DatingRLSystem(App);
        if (App._engagementRL) console.log('[DatingRL] 互动RL为旧版实例，恋爱系统独立创建');
      }
      App.datingSystemActive = true;
      console.log('[DatingRL] 恋爱养成系统已启动');
    } catch (e) {
      console.warn('[DatingRL] 初始化失败:', (e as Error).message);
      App.datingSystemActive = false;
    }
  };

  /** 切换恋爱养成模式（开/关） */
  App.toggleDatingMode = function () {
    if (!App._datingSystem) {
      App.initDatingSystem();
    }
    App.datingSystemActive = !App.datingSystemActive;
    if (App.datingSystemActive) {
      App._datingSystem.startSession();
      console.log('[DatingRL] 恋爱养成模式已开启');
    } else {
      App._datingSystem.endSession();
      App._datingSystem.flush();
      console.log('[DatingRL] 恋爱养成模式已关闭，数据已保存');
    }
    localStorage.setItem('dabai.datingMode', App.datingSystemActive ? '1' : '0');
    return App.datingSystemActive;
  };

  // DEBUG expose（使用 getter 确保访问到 initThree 后创建的实例）
  Object.defineProperty(window, '_scene', {
    get: () => App.scene
  });
  Object.defineProperty(window, '_camera', {
    get: () => App.camera
  });
  Object.defineProperty(window, '_modelGroup', {
    get: () => App.modelGroup
  });
  Object.defineProperty(window, '_smoothRotY', {
    get: () => App.smoothRotY
  });
  Object.defineProperty(window, '_currentAvatar', {
    get: () => App.currentAvatar
  });
});
