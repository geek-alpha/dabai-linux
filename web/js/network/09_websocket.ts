import type { AppKernel, ScenePersistState, TTSEngine } from '../types/app-kernel.js';
import type {
  ClientMessage,
  HarnessTaskPush,
  RlDispatchPlan,
  RlStatusPayload,
  ServerMessage,
} from '../types/ws-protocol.js';

export default (function init(App: AppKernel) {
  const {
    THREE: THREE,
    GLTFLoader: GLTFLoader,
    VRMLoaderPlugin: VRMLoaderPlugin,
    VRMUtils: VRMUtils
  } = App;
  /* ============================================================
   *  WebSocket
   * ============================================================ */
  // 首次连接是否已渲染历史（热重载重连时不重复清空/重建消息区）
  let historyLoaded = false;
  // 是否已建立过连接：热重载/重启后的自动重连不再弹『已连接到 AI』，
  // 避免打断进行中的回合气泡与执行链路观感
  let wsEverConnected = false;
  /* ============================================================
   *  场景状态持久化（相机/模型位置/缩放）
   * ============================================================ */
  App.SCENE_KEY = 'dabai.sceneState';
  App.saveSceneState = function saveSceneState() {
    const state: ScenePersistState = {
      camZoom: App.camZoom,
      camOffsetX: App.camOffsetX,
      camOffsetY: App.camOffsetY,
      camOffsetZ: App.camOffsetZ,
      xrMode: App.xrMode,
      moveMode: App.moveMode,
      backgroundAutoRotate: App.backgroundAutoRotate
    };
    // 保存角色位置/缩放
    const avatarTarget = App.modelGroup;
    if (avatarTarget) {
      state.charPos = {
        x: avatarTarget.position.x,
        z: avatarTarget.position.z
      };
      state.charScale = avatarTarget.scale.x;
    }
    // 保存背景位置/缩放
    if (App.backgroundGroup) {
      state.bgPos = {
        x: App.backgroundGroup.position.x,
        z: App.backgroundGroup.position.z
      };
      state.bgScale = App.backgroundGroup.scale.x;
    }
    try {
      localStorage.setItem(App.SCENE_KEY, JSON.stringify(state));
    } catch (e) {}
  };
  App._saveTimer = null;
  App.debouncedSaveScene = function debouncedSaveScene() {
    clearTimeout(App._saveTimer);
    App._saveTimer = setTimeout(App.saveSceneState, 500);
  };
  App.restoreSceneState = function restoreSceneState() {
    try {
      const s = JSON.parse(localStorage.getItem(App.SCENE_KEY));
      if (!s) return;
      // 背景自转
      if (typeof s.backgroundAutoRotate === 'boolean') App.backgroundAutoRotate = s.backgroundAutoRotate;
      // 刷新页面 = 重置视角：存档里的视角（缩放/平移）与角色位置一律不还原，
      // 只留背景布局 —— 开机画面必须和按下「重置视角」的结果一致。
      window._restoredScene = { bgPos: s.bgPos, bgScale: s.bgScale, moveMode: s.moveMode };
    } catch (e) {}
  };
  App.loadCameraSettings = function loadCameraSettings() {
    try {
      const s = JSON.parse(localStorage.getItem(App.CAM_SETTINGS_KEY));
      if (!s) return;
      if (typeof s.cameraHeight === 'number') {
        App.cameraHeight = THREE.MathUtils.clamp(s.cameraHeight, App.userMinCamHeight(), 10.0);
      }
      if (typeof s.cameraTiltDeg === 'number') {
        App.cameraTiltDeg = THREE.MathUtils.clamp(s.cameraTiltDeg, -80, 80);
      }
      if (typeof s.cameraDistance === 'number') {
        App.cameraDistance = THREE.MathUtils.clamp(s.cameraDistance, App.userMinCamDistance(), 10.0);
        App.DEFAULT_CAM_POS.z = App.cameraDistance;
        App.targetCamPos.z = App.cameraDistance;
      }
    } catch (e) {}
  };
  App._camSettingsSaveTimer = null;
  App.saveCameraSettings = function saveCameraSettings() {
    clearTimeout(App._camSettingsSaveTimer);
    App._camSettingsSaveTimer = setTimeout(() => {
      try {
        localStorage.setItem(App.CAM_SETTINGS_KEY, JSON.stringify({
          cameraHeight: App.cameraHeight,
          cameraTiltDeg: App.cameraTiltDeg,
          cameraDistance: App.cameraDistance
        }));
      } catch (e) {}
    }, 300);
  };
  App.resetAvatarToOrigin = function resetAvatarToOrigin() {
    const avatar = App.currentAvatar || App.modelGroup;
    if (avatar) {
      let targetX = 0, targetY = avatar.position.y, targetZ = 0;
      if (App.backgroundGroup) {
        // 从背景包围盒中心往下射线，取最低命中点 = 真正地板
        const cx = App._bgCenterX || 0;
        const cz = App._bgCenterZ || 0;
        const floorY = App._findFloorY(cx, cz);
        targetX = cx; targetY = floorY + 2.0; targetZ = cz;
      }
      // 平滑过渡回原点（替代硬瞬移），距离过近直接落位
      const dx = targetX - avatar.position.x;
      const dz = targetZ - avatar.position.z;
      const dist = Math.hypot(dx, dz);
      if (dist < 0.3) {
        avatar.position.set(targetX, targetY, targetZ);
      } else {
        App._smoothTeleport = {
          x0: avatar.position.x, y0: avatar.position.y, z0: avatar.position.z,
          x1: targetX, y1: targetY, z1: targetZ,
          t: 0, dur: Math.min(0.7, 0.3 + dist * 0.04),
        };
      }
    }
    // 清除可能恢复旧位置的缓存
    if (window._restoredScene) {
      delete window._restoredScene.charPos;
    }
    // 停止当前 idle 移动，避免下一帧把角色拉回旧路径
    App.idleWalkTarget = null;
    App.idleWalkProgress = 0;
    App.walkPath = [];
    App.walkSegmentIndex = 0;
    App.currentAction = null;
    App.nextActionTimer = 0;
    App.saveSceneState();
  };
  /* 「重置视角」的可复用内核：相机与角色回到统一基准姿态。
   * 交互反馈（toast / AI 台词）由调用方给——刷新角色时不该再插一句台词。 */
  App.resetViewState = function resetViewState() {
    App.dragOrbitYaw = 0;
    App.dragOrbitPitch = 0;
    App.camZoom = 1.0;
    App.camOffsetX = 0;
    App.camOffsetY = 0;
    App.camOffsetZ = 0;
    // 基准机位 = 相机设置里配置的高度/倾斜/距离：刷新页面走的是同一套基准，两边结果必须一致
    App.DEFAULT_CAM_POS!.set(0, App.cameraHeight, App.cameraDistance);
    App.targetCamPos!.set(0, App.cameraHeight, App.cameraDistance);
    App.resetAvatarToOrigin();
    // 立刻面朝【目标】相机位置而非当前位置：否则相机从侧面 lerp 回默认位时，
    // 身体会从背影区转过来
    if (App.currentAvatar) {
      App.smoothRotY = App.computeBodyFaceCam(App.currentAvatar, App.targetCamPos);
      App.smoothRotX = 0;
    }
    // 一次短暂的心有灵犀凝视
    App.gazeBoostUntil = Date.now() / 1000 + 2;
    App.gazeHeadTiltAcc = 0.03;
    App.wasMutualGaze = true; // 已 snap 面朝相机，跳过转身动画
    App.recordInteraction();
    App.saveSceneState();
  };
  App.applySavedPositions = function applySavedPositions() {
    const s = window._restoredScene;
    if (!s) return;
    // 恢复角色位置（平滑过渡，替代硬瞬移）
    const avatar = App.modelGroup;
    if (s.charPos && avatar) {
      const dx = s.charPos.x - avatar.position.x;
      const dz = s.charPos.z - avatar.position.z;
      const dist = Math.hypot(dx, dz);
      if (dist < 0.3) {
        avatar.position.x = s.charPos.x;
        avatar.position.z = s.charPos.z;
      } else {
        App._smoothTeleport = {
          x0: avatar.position.x, y0: avatar.position.y, z0: avatar.position.z,
          x1: s.charPos.x, y1: avatar.position.y, z1: s.charPos.z,
          t: 0, dur: Math.min(0.7, 0.3 + dist * 0.04),
        };
      }
      if (s.charScale) avatar.scale.setScalar(s.charScale);
    }
    // 恢复背景位置
    if (s.bgPos && App.backgroundGroup) {
      App.backgroundGroup.position.x = s.bgPos.x;
      App.backgroundGroup.position.z = s.bgPos.z;
      if (s.bgScale) App.backgroundGroup.scale.setScalar(s.bgScale);
    }
    // 恢复移动模式
    if (s.moveMode) App.setMoveMode(true);
  };
  App.connectWS = function connectWS() {
    // 防止并发重连（超时回调和 onclose 各排一个 connectWS，双 timer 竞态）
    if (App._wsConnecting) return;
    App._wsConnecting = true;
    // 先关旧连接：不关的话旧 ws 的 onmessage 仍会触发，
    // 服务端 manager.active 里旧连接也未移除，广播时新旧各收一份 → 文字/语音/工具全部重复
    if (App.ws) {
      const oldWs = App.ws;
      oldWs.onopen = null;
      oldWs.onmessage = null;
      oldWs.onerror = null;
      oldWs.onclose = null;
      if (oldWs.readyState === WebSocket.OPEN || oldWs.readyState === WebSocket.CONNECTING) {
        oldWs.close();
      }
      App.ws = null;
    }
    // 清理旧的定时器
    if (App.wsHeartbeat) {
      clearInterval(App.wsHeartbeat);
      App.wsHeartbeat = null;
    }
    if (App.rlHeartbeat) {
      clearTimeout(App.rlHeartbeat);
      App.rlHeartbeat = null;
    }
    if (App.wsReconnectTimer) {
      clearTimeout(App.wsReconnectTimer);
      App.wsReconnectTimer = null;
    }
    if (App.wsConnTimeout) {
      clearTimeout(App.wsConnTimeout);
      App.wsConnTimeout = null;
    }
    App.statusBadge.textContent = '连接中…';
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';

    // 连接超时：3秒还没连上就触发重连
    App.wsConnTimeout = setTimeout(() => {
      if (App.ws && App.ws.readyState === WebSocket.CONNECTING) {
        App.ws.close();
        App.ws = null;
      }
      App._wsConnecting = false;
      App.statusBadge.textContent = '连接超时，重连中…';
      App.wsReconnectTimer = setTimeout(App.connectWS, 2000);
    }, 3000);
    try {
      App.ws = new WebSocket(`${proto}://${location.host}/ws`);
    } catch (e) {
      App._wsConnecting = false;
      App.statusBadge.textContent = '连接失败，重连中…';
      App.wsReconnectTimer = setTimeout(App.connectWS, 2000);
      return;
    }
    App.ws.onopen = () => {
      clearTimeout(App.wsConnTimeout);
      App.wsConnTimeout = null;
      App._wsConnecting = false;
      App.statusBadge.textContent = '已连接';
      App.setState(App.State.IDLE);
      // 只在首次连接提示；热重载/重启后的自动重连不刷屏
      if (!wsEverConnected) {
        wsEverConnected = true;
        App.addSystemMsg('已连接到 AI');
      }
      // 发送用户标识以恢复历史
      const uid = localStorage.getItem('dabai.userId') || 'u_' + Date.now().toString(36);
      localStorage.setItem('dabai.userId', uid);
      App.ws.send(JSON.stringify({
        type: 'set_user',
        user_id: uid
      } satisfies ClientMessage));

      // 同步当前角色形象与背景场景给 AI
      const savedModel = JSON.parse(localStorage.getItem('dabai.currentModel') || 'null');
      if (savedModel && savedModel.name) {
        App.ws.send(JSON.stringify({
          type: 'set_avatar',
          name: savedModel.name
        }));
        App._sentAvatarName = savedModel.name;
      }
      const savedBg2 = JSON.parse(localStorage.getItem('dabai.currentBackground') || 'null');
      if (savedBg2 && savedBg2.name) {
        App.ws.send(JSON.stringify({
          type: 'set_background',
          name: savedBg2.name
        } satisfies ClientMessage));
        App._sentBgName = savedBg2.name;
      }

      // 启动 WebSocket 心跳（每25秒发一次 ping，防止 NAT/代理超时断连）
      App.wsHeartbeat = setInterval(() => {
        if (App.ws && App.ws.readyState === WebSocket.OPEN) {
          // 尝试发送心跳，如果失败则触发重连
          try {
            App.ws.send(JSON.stringify({
              type: 'ping'
            } satisfies ClientMessage));
          } catch (e) {
            if (App.ws) {
              App.ws.close();
              App.ws = null;
            }
            App.connectWS();
          }
        } else if (!App.ws || App.ws.readyState > WebSocket.OPEN) {
          // 连接已不在 OPEN 状态，发起重连
          if (App.wsHeartbeat) {
            clearInterval(App.wsHeartbeat);
            App.wsHeartbeat = null;
          }
          App.connectWS();
        }
      }, 25000);

      // 如果之前在自动对话模式，重连后恢复 VAD
      if (App.voiceMode === 'auto' && !App.vadStream) {
        console.log('[WS] 重连后恢复自动对话模式');
        App.startVADMode().then(ok => {
          if (ok) {
            App.vadState = 'idle';
            App.vadLoop();
          }
        });
      }

      // 启动 RL 统一调度心跳（完全统摄：后端 proactive_tick 自动驱动）
      App.startRLHeartbeat();
    };
    App.ws.onmessage = e => {
      let msg: ServerMessage;
      try {
        msg = JSON.parse(e.data) as ServerMessage;
      } catch {
        return;
      }
      // 服务端 pong 响应，忽略
      if (msg.type === 'pong') return;
      App.handleWSMessage(msg);
    };
    App.ws.onerror = () => {
      clearTimeout(App.wsConnTimeout);
      App.wsConnTimeout = null;
      App._wsConnecting = false;
      App.statusBadge.textContent = '连接出错，重连中…';
    };
    App.ws.onclose = () => {
      clearTimeout(App.wsConnTimeout);
      App.wsConnTimeout = null;
      App._wsConnecting = false;
      if (App.wsHeartbeat) {
        clearInterval(App.wsHeartbeat);
        App.wsHeartbeat = null;
      }
      if (App.rlHeartbeat) {
        clearTimeout(App.rlHeartbeat);
        App.rlHeartbeat = null;
      }
      App.statusBadge.textContent = '连接断开，重连中…';
      App.wsReconnectTimer = setTimeout(App.connectWS, 2000);
    };
  };
  App.handleWSMessage = function handleWSMessage(msg) {
    switch (msg.type) {
      case 'server_restart':
        // 服务端计划内重启（改核心代码生效 / systemctl restart）：
        // 先把话说清楚，再让 onclose 的重连逻辑接管——同一件事，
        // 用户看到「大白正在升级」而不是「跑着跑着莫名掉线」
        if (App.setTurnStatus) App.setTurnStatus(msg.text || '大白正在升级，几秒后自动回来…');
        if (App.showToast) App.showToast(msg.text || '大白正在升级，几秒后自动回来…');
        break;
      case 'ready':
        App.setState(App.State.IDLE);
        break;
      case 'user_set':
        console.log('[WS] 用户已设置:', msg.user_id);
        App.removeTyping();
        if (!historyLoaded) {
          // 首次连接（页面加载/刷新）：复位回合气泡与工具链，渲染完整历史
          historyLoaded = true;
          App._turnMsgEl = null;
          if (App.toolChainReset) App.toolChainReset();
          if (msg.history && msg.history.length > 0) {
            App.messagesEl.innerHTML = '';
            App.addSystemMsg('已连接');
            for (const h of msg.history) {
              if (h.user) App.addUserMsg(h.user, false, (h as any).atts);
              if (h.ai) App.addAIMsg(h.ai);
            }
            // 历史恢复：强制定位到最新一条（绕过"上滑暂停跟随"）
            requestAnimationFrame(() => App.scrollToBottom(true));
          } else {
            App.addSystemMsg('已连接');
          }
        }
        // 热重载/重启重连：保留当前消息区与进行中的回合气泡、工具块，
        // 断点续跑事件（resume）继续在现有回合上展示，不清空不重刷
        break;
      case 'thinking':
        // currentReplySession 一旦设置，audio_chunk/audio_end 就会播放大厅 TTS
        // 并显示大厅 AI 回复气泡（乱入）。游戏内回复由蜂群 addAIMsg 独立渲染，
        // 不经过后端回复链路。
        if (App.noteTurnActivity) App.noteTurnActivity();
        App._toolRunningSince = 0;
        // 新一轮回复开始：清空旧队列与文本
        App.currentReplySession = msg.session_id || null;
        App._interruptedSession = null; // 新 session 开启，解除旧会话栅栏
        App.currentReplyText = '';
        App.currentReplySeg = '';
        App.audioQueue = [];
        App._streamTextOn = false; // 本轮是否走"即时文本流"（stream_text）
        App._turnTextDone = false; // 新轮开始：文本完成标志复位
        App.removeTyping();
        // 若分片已先于 thinking 到达并开始播放，不再切回思考态（避免"只显示省略号"）
        if (App.currentState !== App.State.SPEAKING) App.setState(App.State.THINKING);
        if (msg.resume && App._turnMsgEl && document.body.contains(App._turnMsgEl)) {
          // 断点续跑（热重载/重启后）：复用当前回合气泡继续展示，
          // 先静态收尾旧工具状态，再让后续 stream_text / tool_call 续上
          if (App.toolChainEndTurn) App.toolChainEndTurn();
        } else {
          // 普通新轮：思考 → 工具 → 回复 内联成一条回合气泡
          if (App.beginTurnBubble) App.beginTurnBubble(msg.session_id);
          if (App.appendTurnThinking && msg.text) App.appendTurnThinking(msg.text);
          // 工具链模块换新（上一轮自动收尾）
          if (App.toolChainBeginTurn) App.toolChainBeginTurn();
        }
        break;
      case 'thinking_text':
        // 思维链增量：追加进当前回合「思考段」（自动展开，可随时收起）
        if (App.noteTurnActivity) App.noteTurnActivity();
        if (App.appendTurnThinking) App.appendTurnThinking(msg.text || '');
        break;
      case 'reasoning':
        // 真实思维链增量（服务端节流后）：回复气泡底部「思考中」实时指示，
        // 一行持续更新，用户可判断对话仍在推进而非卡住/失败。
        // 过期 session 的思考指示不处理（防止旧轮次延迟事件覆盖当前气泡）
        if (App.currentReplySession && msg.session_id && msg.session_id !== App.currentReplySession) break;
        if (App.noteTurnActivity) App.noteTurnActivity();
        if (App.handleReasoning) App.handleReasoning(msg.text || '', msg.session_id);
        break;
      case 'stream_text':
        // 回复正文即时流出：不等语音，文字先到先显示
        if (App.noteTurnActivity) App.noteTurnActivity();
        if (App.handleStreamText) App.handleStreamText(msg.text || '');
        break;
      case 'retract_text':
        // 工具轮过程话：从主消息撤回（服务端已同时转入思考段）
        if (App.handleRetractText && msg.length) App.handleRetractText(msg.length);
        break;
      case 'turn_status':
        // 对话轮运行状态（网络波动自动重连等）：回合气泡底部一行提示，
        // 不打断流程、不朗读、不进正文；喂看门狗（重试等待 ≠ 卡死）
        if (App.noteTurnActivity) App.noteTurnActivity();
        if (App.setTurnStatus) App.setTurnStatus(msg.text || '');
        break;
      case 'turn_in_progress':
        // 页面刷新/断线重连：后台对话轮仍在执行 → 进度快照重建回合气泡，
        // 后续事件经广播接上，用户看到的是无缝继续而非从头再来
        if (App.noteTurnActivity) App.noteTurnActivity();
        if (App.takeoverTurnInProgress) App.takeoverTurnInProgress(msg);
        break;
      case 'listening':
        App.setState(App.State.LISTENING);
        break;
      case 'transcript':
        App.removeTyping();
        App.addUserMsg(msg.text, true);
        // 不触发大厅 RL 系统（约会/表情/参与），否则大厅角色会"听到玩家说话"而乱入
        // 记录用户活跃时间（语音消息也算，供 RL 统一状态取数）
        App._lastUserMessageTime = Date.now();
        App._lastUserInteractTime = Date.now();
        if (App._engagementRL) App._engagementRL.notifyUserMessage();
        if (App._datingSystem) App._datingSystem.notifyUserMessage(msg.text);
        if (App._expressionRL) App._expressionRL.notifyUserMessage();
        break;
      case 'audio_chunk':
        if (App.noteTurnActivity) App.noteTurnActivity();
        App.handleAudioChunk(msg);
        break;
      case 'turn_text_done':
        // 轮次结束的判据是「文本输出完成」：audio_end 要等 TTS 队列把所有分片
        // 发完才到，挂在它上面整轮就被语音拖住（TTS 慢/挂 → 工具期保护不解除、
        // 工具链不收尾、游戏回合不结算）
        if (App.noteTurnActivity) App.noteTurnActivity();
        App._turnInTools = false;   // 回合结束：工具执行期保护解除
        App._replyEmotionDone = false;
        App._turnTextDone = true;
        if (App.toolChainEndTurn) App.toolChainEndTurn();
        break;
      case 'audio_end':
        if (App.noteTurnActivity) App.noteTurnActivity();
        App._turnInTools = false;   // 兜底：文本完成事件没到时也要解除保护
        // 收尾兜底：正常路径已由 turn_text_done 收尾；toolChainEndTurn 会结算
        // 游戏回合，重复调用会重复加分，所以必须判标志
        if (!App._turnTextDone && App.toolChainEndTurn) App.toolChainEndTurn();
        App.handleAudioEnd(msg);
        break;
      case 'usage':
        // LLM 用量事件：头顶徽章 + 气泡 token 徽章
        if (App.handleUsageMessage) App.handleUsageMessage(msg);
        break;
      case 'interrupted':
        // 过期 session 的中断信号不处理：避免误清新一轮的队列/工具链
        if (App.currentReplySession && msg.session_id &&
            msg.session_id !== App.currentReplySession) break;
        App._turnInTools = false;   // 回合被取消：工具执行期保护解除
        App.handleInterrupted(msg);
        if (App.toolChainAbort) App.toolChainAbort();
        break;
      case 'system_msg':
        // 系统提示（如任务暂停后提示「说『继续』可以接着干」）
        if (App.addSystemMsg && msg.text) App.addSystemMsg(msg.text);
        break;
      case 'tool_call_start':
        // 工具调用开始：并入当前轮次的「工作流工具链」卡片
        if (App.noteTurnActivity) App.noteTurnActivity();
        App._toolRunningSince = Date.now();
        // 工具一旦开跑，语音就不再打断本轮（VAD 只停本地播报，服务端排队这句）
        App._turnInTools = true;
        App.removeTyping();
        // tool_desc：工具自己的说明，供中央字幕显示（前端不维护映射表）
        if (App.toolChainStart) App.toolChainStart(msg.tool_name, msg.arguments, msg.tool_desc);
        break;
      case 'tool_call_result':
        // 工具调用结果：回填到工具链对应步骤
        if (App.noteTurnActivity) App.noteTurnActivity();
        App._toolRunningSince = 0;
        if (App.toolChainResult) App.toolChainResult(msg.tool_name, msg.result, msg.success);
        break;
      case 'tool_call_progress':
        // 工具执行心跳：更新当前运行步骤的已运行时长（长任务反馈）
        if (App.noteTurnActivity) App.noteTurnActivity();
        if (App.toolChainProgress) App.toolChainProgress(msg.tool_name, msg.elapsed, msg.message);
        break;
      case 'session_list':
        // 会话列表
        App.renderSessionList(msg.sessions);
        break;
      case 'session_switched':
        // 会话切换成功：清空面板并渲染目标会话完整历史（先复位回合气泡与工具链）
        App.messagesEl.innerHTML = '';
        App._turnMsgEl = null;
        if (App.toolChainReset) App.toolChainReset();
        if (msg.summary) {
          App.addSystemMsg('已切换到会话 · 摘要：' + msg.summary.slice(0, 120));
        } else if (msg.history && msg.history.length > 0) {
          App.addSystemMsg('已切换到会话');
        }
        if (msg.history && msg.history.length > 0) {
          for (const h of msg.history) {
            if (h.user) App.addUserMsg(h.user, false, (h as any).atts);
            if (h.ai) App.addAIMsg(h.ai);
          }
          requestAnimationFrame(() => App.scrollToBottom(true));
        }
        // 弹窗开着时刷新列表（高亮新当前会话）
        if (App.sessionModalEl && App.sessionModalEl.classList.contains('show')) {
          App.renderSessionListFromState();
        }
        console.log('[WS] 切换到会话:', msg.session_id);
        break;
      case 'session_created':
        console.log('[WS] 新会话:', msg.session_id);
        // 删除/归档当前会话后的自动续新：清空对话栏
        if (msg.reason === 'auto_after_delete') {
          App.messagesEl.innerHTML = '';
          App.addSystemMsg('已开启新对话');
        }
        if (App.sessionModalEl && App.sessionModalEl.classList.contains('show')) {
          App.renderSessionListFromState();
        }
        break;
      case 'session_deleted':
        console.log('[WS] 会话已删除:', msg.session_id);
        if (msg.next_session_id) {
          // 删除的是当前会话：服务端已自动续新，清空对话栏
          App.messagesEl.innerHTML = '';
          App.addSystemMsg('已开启新对话');
        }
        if (App.sessionModalEl && App.sessionModalEl.classList.contains('show')) {
          App.renderSessionListFromState();
        }
        break;
      case 'session_renamed':
        console.log('[WS] 会话已重命名:', msg.session_id, msg.title);
        if (App.sessionModalEl && App.sessionModalEl.classList.contains('show')) {
          App.renderSessionListFromState();
        }
        break;
      case 'session_pinned':
        console.log('[WS] 会话置顶状态:', msg.session_id, msg.pinned);
        if (App.sessionModalEl && App.sessionModalEl.classList.contains('show')) {
          App.renderSessionListFromState();
        }
        break;
      case 'session_archived':
        console.log('[WS] 会话归档状态:', msg.session_id, msg.archived);
        if (msg.archived && msg.active_changed) {
          // 刚归档的是当前会话：对话栏清空，下次发消息自动续新
          App.messagesEl.innerHTML = '';
          App.addSystemMsg('当前对话已归档');
        }
        if (App.sessionModalEl && App.sessionModalEl.classList.contains('show')) {
          App.renderSessionListFromState();
        }
        break;
      case 'error':
        App.removeTyping();
        App.showToast(msg.message || '出错了');
        App.setState(App.State.IDLE);
        App.showSubtitle('');
        if (App.toolChainAbort) App.toolChainAbort();
        break;
      case 'restart_vad':
        // ffmpeg 转码失败 → 重建 VAD 自动对话模式（比刷新页面更快更轻量）
        console.warn('[WS] 重建 VAD:', msg.reason);
        App.showToast(msg.reason || '语音模式异常，正在重建…');
        App.stopVADMode();
        App.startVADMode().then(ok => {
          if (ok) {
            App.vadState = 'idle';
            App.vadLoop();
            App.showToast('语音模式已恢复');
          } else {
            // 麦克风也拿不到就没办法了
            App.showToast('无法恢复语音模式，请手动刷新页面');
          }
        });
        break;
      case 'bridge_confirm':
        // 角色请求了 AI 助手（DSH）执行任务 / 想中途问你一个问题 → 弹卡片
        App.showHarnessConfirm(msg.request_id, msg.task, msg.options);
        break;
      case 'bridge_status':
        if (App.updateHarnessStatus) App.updateHarnessStatus(msg);
        break;
      case 'bridge_say':
        // 反向通道：DSH 侧的 AI 助手给用户递话（经「大白」的信使角色）
        if (App.onBridgeSay) App.onBridgeSay(msg.text);
        break;
      case 'task_event':
        // 任务中心：实时进度/日志/结果增量 + 大屏投递 + DSH 聊天直播卡
        if (App.handleTaskEvent) App.handleTaskEvent(msg.event);
        if (App.taskBoardOnEvent) App.taskBoardOnEvent(msg.event);
        if (App.dshCardOnEvent) App.dshCardOnEvent(msg.event);
        break;
      case 'plan_stall':
        // 工作清单搁置：后台扫到「有活没干完却十来分钟没动」→ 聊天框里推一条提醒
        renderPlanStallCard(msg.text || '');
        break;
      case 'harness_task': {
        // harness 任务系统完成推送：长任务/批量任务到终态即弹出提示（无需轮询）
        const ht: HarnessTaskPush = msg.task || {};
        const htOk = ht.state === 'succeeded';
        const htText = `📋 后台任务「${ht.name || ht.id || ''}」` +
          (htOk ? '已完成' : `未成功（${ht.status || ht.state || ''}）`);
        try { if (App.showToast) App.showToast(htText); } catch (e) { /* toast 可选 */ }
        console.log('[WS] harness_task:', htText);
        break;
      }
      case 'task_tree':
        // 复杂任务结构卡片：聊天框内多层级展开/折叠展示
        if (App.addTaskTreeMsg) {
          const tree = msg.data || msg.tree || msg;
          App.addTaskTreeMsg(tree);
        } else if (App.maybeRenderTaskTree) {
          App.maybeRenderTaskTree(msg);
        }
        break;
      case 'screen_command':
        App.handleScreenCommand(msg);
        break;
      // === AI 自主行为命令 ===
      case 'ai_behavior_command':
        // 后端行为决策引擎发来的命令
        if (App.aiAutonomyController) {
          App.aiAutonomyController.receiveCommand(msg);
        } else {
          console.log('[AI自主] 收到命令但控制器未初始化:', msg.behavior);
        }
        break;
      // === RL 统一调度计划（统摄所有 Agent 派发） ===
      case 'rl_dispatch':
        {
          const plan = msg.data || {};
          if (!plan.agent_choice) break;
          App._rlLastDispatchTime = Date.now();
          console.log('[RL调度]', plan.agent_choice, '|', plan.reason || '', '| 模式:', plan.mode_name || '');
          // engagement：执行行为指令（自主行为）
          if (plan.agent_choice === 'engagement' && plan.behavior_cmd && App.aiAutonomyController) {
            App.aiAutonomyController.receiveCommand(plan.behavior_cmd);
          }
          // ai_agent：服务端已启动主动回复（rl_dispatch 仅作状态回显，避免重复触发）
        }
        break;
      // === RL 状态回显（rl_sync 的响应） ===
      case 'rl_status':
        {
          const st: RlStatusPayload = msg.data || {};
          if (App._rlStatusEl) App._rlStatusEl.textContent = JSON.stringify(st);
        }
        break;
    }
  }; // ========== 文件名字段模糊匹配（AI 给的名字可能与实际文件名略有差异） ==========
  /**
   * 模糊匹配文件名，返回服务器上的精确文件名，找不到返回 null。
   * @param {string} name - AI 传入的文件名
   * @param {string} endpoint - '/api/models' 或 '/api/backgrounds'
   */
  App.fuzzyMatchFile = async function fuzzyMatchFile(name, endpoint) {
    // 标准化比较：去扩展名、全小写
    const normalize = s => s.replace(/\.[^.]+$/, '').toLowerCase().trim();
    const target = normalize(name);
    if (!target) return null;
    try {
      const resp = await fetch(endpoint);
      if (!resp.ok) return null;
      const data = await resp.json();
      // API 返回格式: {models: [{name:..., ...}]} 或 {backgrounds: [{name:..., ...}]}
      const list = data.models || data.backgrounds || [];
      const filenames = list.map(f => f.name);

      // 1. 精确匹配
      if (filenames.includes(name)) return name;

      // 2. 忽略大小写精确匹配
      const lowerFiles = filenames.map(f => f.toLowerCase());
      const exactIdx = lowerFiles.indexOf(name.toLowerCase());
      if (exactIdx >= 0) return filenames[exactIdx];

      // 3. 去扩展名后匹配
      const noExtNames = filenames.map(f => normalize(f));
      const noExtIdx = noExtNames.indexOf(target);
      if (noExtIdx >= 0) return filenames[noExtIdx];

      // 4. 子串匹配（至少包含 3 个字符才匹配，避免过短关键词命中）
      if (target.length >= 3) {
        for (let i = 0; i < noExtNames.length; i++) {
          if (noExtNames[i].includes(target) || target.includes(noExtNames[i])) {
            console.log(`[FuzzyMatch] '${name}' → '${filenames[i]}'`);
            return filenames[i];
          }
        }
      }

      // 5. 中文关键词匹配：拆出中文部分，看是否被文件名包含
      const zhChars = target.replace(/[^\u4e00-\u9fff]/g, '');
      if (zhChars.length >= 2) {
        for (let i = 0; i < noExtNames.length; i++) {
          if (noExtNames[i].includes(zhChars)) {
            console.log(`[FuzzyMatch|ZH] '${name}' → '${filenames[i]}'`);
            return filenames[i];
          }
        }
      }
    } catch (e) {
      console.warn('[FuzzyMatch] 查询文件列表失败:', e);
    }
    return null;
  }; // ========== 屏幕控制命令处理（AI 通过 MCP 工具调用控制前端） ==========
  App.handleScreenCommand = async function handleScreenCommand(msg) {
    const {
      tool,
      args
    } = msg;
    console.log('[ScreenCmd] 收到 AI 屏幕指令:', tool, args);
    try {
      switch (tool) {
        case 'switch_character_model':
          {
            let name = args.model_name;
            // 模糊匹配：AI 给的文件名可能不精确，尝试找到最接近的实际文件
            const match = await App.fuzzyMatchFile(name, '/api/models');
            if (match) {
              name = match;
            } else {
              console.warn('[ScreenCmd] 未找到模型:', name, '，尝试直接加载');
            }
            const url = '/models/' + encodeURIComponent(name);
            await App.loadModelFromUrl(url, name);
            break;
          }
        case 'switch_background_scene':
          {
            let name = args.bg_name;
            // 传入 'default'（或未指定）→ 恢复默认星空背景
            if (!name || String(name).toLowerCase() === 'default') {
              App.useDefaultBackground();
              break;
            }
            // 模糊匹配：AI 给的文件名可能不精确
            const match = await App.fuzzyMatchFile(name, '/api/backgrounds');
            if (match) {
              name = match;
            } else {
              console.warn('[ScreenCmd] 未找到背景:', name, '，尝试直接加载');
            }
            const url = '/backgrounds/' + encodeURIComponent(name);
            await App.loadBackgroundFromUrl(url, name);
            break;
          }
        case 'switch_tts_settings':
          {
            if (args.engine) {
              App.switchTTSEngine(args.engine as TTSEngine);
            }
            const cfg: Record<string, any> = {};
            if (args.voice) cfg.edge_voice = args.voice;
            if (args.rate) cfg.edge_rate = args.rate;
            if (args.engine) cfg.engine = args.engine;
            if (Object.keys(cfg).length > 0) {
              try {
                const res = await fetch('/api/tts/config', {
                  method: 'POST',
                  headers: {
                    'Content-Type': 'application/json'
                  },
                  body: JSON.stringify(cfg)
                });
                if (res.ok) {
                  const voiceDesc = args.voice || '';
                  const rateDesc = args.rate ? ' 语速' + args.rate : '';
                  App.showToast('AI 切换了语音设置' + (voiceDesc ? ' · ' + voiceDesc : '') + rateDesc);
                }
              } catch (e) {
                console.warn('[ScreenCmd] TTS 配置保存失败:', e);
              }
            }
            break;
          }
        case 'switch_app_mode':
          {
            const mode = args.mode;
            switch (mode) {
              case 'auto_voice':
                App.setVoiceMode('auto');
                App.showToast('AI 切换到了自动对话模式 🎙️');
                break;
              case 'press_voice':
                App.setVoiceMode('press');
                App.showToast('AI 切换到了按住说话模式');
                break;
              case 'lock_screen':
                if (!App.lockMode) App.toggleLockMode();
                break;
              case 'normal':
                if (App.lockMode) App.toggleLockMode();
                break;
            }
            break;
          }
        case 'show_screen_toast':
          {
            App.showToast(args.message || '✨');
            break;
          }
        case 'play_music':
          {
            const vol = args.volume !== undefined ? args.volume : 0.8;
            App.setBGMVolume(vol);
            // 子智能体看护：server 会把 worker_id 注入屏幕指令，播完据此回报
            App._musicWorkerId = args.worker_id || null;
            const label = (args.title ? args.title : '在线音乐') + (args.artist ? ' - ' + args.artist : '');
            App.playMusicTrack(args.url, label);
            App.showToast('🎵 正在播放：' + label);
            break;
          }
        case 'stop_music':
          {
            App.stopBGM();
            App.showToast('已停止播放音乐');
            break;
          }
        case 'control_music':
          {
            // 在线音乐技能：播放控制 pause/resume/toggle/stop/volume
            const action = String(args.action || '').toLowerCase();
            if (action === 'pause') {
              App.pauseBGM();
              App.showToast('已暂停播放音乐');
            } else if (action === 'resume' || action === 'play') {
              App.resumeBGM();
              App.showToast('已继续播放音乐');
            } else if (action === 'toggle') {
              App.toggleBGM();
            } else if (action === 'stop') {
              App.stopBGM();
              App.showToast('已停止播放音乐');
            } else if (action === 'volume') {
              const v = args.value !== undefined ? Number(args.value) : 0.8;
              if (Number.isFinite(v)) {
                App.setBGMVolume(Math.max(0, Math.min(1, v)));
                App.showToast('音乐音量已调整为 ' + Math.round(App.getBGMState().volume * 100) + '%');
              }
            } else {
              console.warn('[ScreenCmd] control_music 未知 action:', action);
            }
            break;
          }
        case 'play_playlist':
          {
            App._musicWorkerId = args.worker_id || null; // 歌单子智能体看护
            if (App.playPlaylistCmd) {
              App.playPlaylistCmd(args);
            } else {
              console.warn('[ScreenCmd] play_playlist 处理器未初始化');
            }
            break;
          }
        case 'play_video':
          {
            // 在线视频技能：直播大屏「大白影院」带声播放（主服务原生流）
            // 子智能体看护：server 注入 worker_id，播完经 /api/video_hub/api/ended 带回
            App._videoWorkerId = args.worker_id || null;
            if (App.videoBoardPlay) {
              App.videoBoardPlay(args);
            } else {
              console.warn('[ScreenCmd] videoBoardPlay 处理器未初始化');
            }
            break;
          }
        case 'control_video':
          {
            // 在线视频技能：大屏播放控制 pause/resume/seek/volume/mute/stop/next
            if (args.action === 'stop') App._videoWorkerId = null;
            if (App.videoBoardControl) {
              App.videoBoardControl(args);
            } else {
              console.warn('[ScreenCmd] videoBoardControl 处理器未初始化');
            }
            break;
          }
      }
    } catch (e) {
      console.error('[ScreenCmd] 执行失败:', tool, e);
    }
  };
  // 兼容保留：旧接口统一转发到「工作流工具链」卡片，避免任何调用方再打出独立气泡
  App.addToolCallMsg = function addToolCallMsg(toolName, args, status) {
    if (App.toolChainStart) App.toolChainStart(toolName, args);
  };
  App.addToolCallResult = function addToolCallResult(toolName, result, success) {
    if (App.toolChainResult) App.toolChainResult(toolName, result, success);
  }; // ========== 会话管理 ==========
  App.sessionModalEl = undefined;
  App.sessionListEl = undefined;
  App.sessionBtn = undefined;
  App.sessionModalClose = undefined;
  App.newSessionBtn = undefined;
  App.sessionSearchInput = undefined;
  App.sessionArchiveToggle = undefined;
  App._sessionSearchTimer = undefined;
  App._sessionShowArchived = false;
  App.initSessionUI = function initSessionUI() {
    App.sessionModalEl = document.getElementById('session-modal');
    App.sessionListEl = document.getElementById('session-list');
    App.sessionBtn = document.getElementById('session-btn');
    App.sessionModalClose = document.getElementById('session-modal-close');
    App.newSessionBtn = document.getElementById('new-session-btn');
    App.sessionSearchInput = document.getElementById('session-search-input') as HTMLInputElement;
    App.sessionArchiveToggle = document.getElementById('session-archive-toggle');
    const open = () => {
      App.sessionModalEl.classList.add('show');
      if (App.sessionSearchInput) App.sessionSearchInput.value = '';
      App._sessionShowArchived = false;
      if (App.sessionArchiveToggle) App.sessionArchiveToggle.classList.remove('on');
      App.renderSessionListFromState();
    };
    App.sessionBtn.addEventListener('click', open);
    App.sessionModalClose.addEventListener('click', () => {
      App.sessionModalEl.classList.remove('show');
    });
    App.sessionModalEl.querySelector('.modal-backdrop').addEventListener('click', () => {
      App.sessionModalEl.classList.remove('show');
    });
    App.sessionSearchInput.addEventListener('input', () => {
      clearTimeout(App._sessionSearchTimer);
      App._sessionSearchTimer = setTimeout(() => {
        App.requestSessionList();
      }, 300);
    });
    App.sessionArchiveToggle.addEventListener('click', () => {
      App._sessionShowArchived = !App._sessionShowArchived;
      App.sessionArchiveToggle.classList.toggle('on', App._sessionShowArchived);
      App.requestSessionList();
    });
    const doNewSession = () => {
      if (App.ws && App.ws.readyState === WebSocket.OPEN) {
        App.ws.send(JSON.stringify({
          type: 'new_session'
        } satisfies ClientMessage));
        App.messagesEl.innerHTML = '';
      }
    };
    App.newSessionBtn.addEventListener('click', () => {
      doNewSession();
      App.sessionModalEl.classList.remove('show');
    });
    // 底部快捷「新会话」按钮（与弹窗内按钮共用同一逻辑，仅不关弹窗）
    const quickNewSessionBtn = document.getElementById('quick-new-session-btn');
    if (quickNewSessionBtn) quickNewSessionBtn.addEventListener('click', doNewSession);
  };
  App.requestSessionList = function requestSessionList() {
    if (!App.ws || App.ws.readyState !== WebSocket.OPEN) return;
    const q = (App.sessionSearchInput.value || '').trim();
    if (q) {
      App.ws.send(JSON.stringify({
        type: 'search_sessions',
        q
      } satisfies ClientMessage));
    } else {
      App.ws.send(JSON.stringify({
        type: 'list_sessions',
        ...(App._sessionShowArchived ? { include_archived: true } : {})
      } satisfies ClientMessage));
    }
  };
  App.renderSessionList = function renderSessionList(sessions) {
    if (!App.sessionListEl) return;
    App.sessionListEl.innerHTML = '';
    if (!sessions || sessions.length === 0) {
      const q = (App.sessionSearchInput.value || '').trim();
      App.sessionListEl.innerHTML = '<div style="text-align:center;color:var(--text-dim);padding:20px">' + (q ? '没有匹配的会话' : '暂无历史对话') + '</div>';
      return;
    }
    sessions.forEach(s => {
      const item = document.createElement('div');
      item.className = 'session-item' +
        (s.is_current ? ' active' : '') +
        (s.archived ? ' archived' : '');
      const metaParts = [
        s.updated_at ? new Date(s.updated_at).toLocaleString() : '',
        (s.message_count != null ? s.message_count + ' 条消息' : ''),
        (s.approx_tokens != null ? '约 ' + s.approx_tokens + ' token' : ''),
      ].filter(Boolean);
      const esc = (v) => String(v == null ? '' : v).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
      item.innerHTML =
        '<div class="session-icon">' +
          '<svg viewBox="0 0 24 24" width="16" height="16"><path fill="currentColor" d="M20 2H4c-1.1 0-1.99.9-1.99 2L2 22l4-4h14c1.1 0 2-.9 2-2V4c0-1.1-.9-2-2-2zm-2 12H6v-2h12v2zm0-3H6V9h12v2zm0-3H6V6h12v2z"/></svg>' +
        '</div>' +
        '<div class="session-info">' +
          '<div class="session-title-row">' +
            (s.pinned ? '<span class="session-pin-mark" title="已置顶">📌</span>' : '') +
            '<span class="session-title" title="' + esc(s.title || '对话') + '">' + esc(s.title || '对话') + '</span>' +
            (s.is_current ? '<span class="session-current-tag">当前</span>' : '') +
            (s.archived ? '<span class="session-current-tag" style="border-color:#888;color:#888">已归档</span>' : '') +
          '</div>' +
          (metaParts.length ? '<div class="session-meta">' + metaParts.join(' · ') + '</div>' : '') +
          (s.summary ? '<div class="session-summary" title="' + esc(s.summary) + '">' + esc(s.summary) + '</div>' : '') +
        '</div>' +
        '<div class="session-actions">' +
          '<button class="session-action-btn ' + (s.pinned ? 'pin-on' : '') + '" data-act="pin" data-sid="' + esc(s.id) + '" title="' + (s.pinned ? '取消置顶' : '置顶') + '">📌</button>' +
          '<button class="session-action-btn rename-btn" data-act="rename" data-sid="' + esc(s.id) + '" title="重命名">✏️</button>' +
          '<button class="session-action-btn archive-btn" data-act="archive" data-sid="' + esc(s.id) + '" title="' + (s.archived ? '取消归档' : '归档') + '">🗄</button>' +
          '<button class="session-action-btn delete-btn" data-act="delete" data-sid="' + esc(s.id) + '" title="删除">🗑</button>' +
        '</div>';

      item.addEventListener('click', e => {
        const actEl = (e.target as Element).closest('[data-act]');
        if (actEl) return;
        if (s.is_current) return;
        if (s.archived && !App._sessionShowArchived) return;
        if (App.ws && App.ws.readyState === WebSocket.OPEN) {
          App.ws.send(JSON.stringify({
            type: 'switch_session',
            session_id: s.id
          } satisfies ClientMessage));
          App.sessionModalEl.classList.remove('show');
        }
      });

      item.querySelectorAll('[data-act]').forEach(btn => {
        const btnEl = btn as HTMLElement;
        btnEl.addEventListener('click', e => {
          e.stopPropagation();
          const act = btnEl.dataset.act;
          const sid = btnEl.dataset.sid;
          if (!sid || !App.ws || App.ws.readyState !== WebSocket.OPEN) return;
          if (act === 'pin') {
            App.ws.send(JSON.stringify({
              type: 'pin_session',
              session_id: sid,
              pinned: !s.pinned
            } satisfies ClientMessage));
          } else if (act === 'rename') {
            const name = window.prompt('输入新标题：', s.title || '');
            if (name && name.trim()) {
              App.ws.send(JSON.stringify({
                type: 'rename_session',
                session_id: sid,
                title: name.trim().slice(0, 60)
              } satisfies ClientMessage));
            }
          } else if (act === 'archive') {
            App.ws.send(JSON.stringify({
              type: 'archive_session',
              session_id: sid,
              archived: !s.archived
            } satisfies ClientMessage));
          } else if (act === 'delete') {
            if (window.confirm('确定删除对话「' + (s.title || '对话') + '」？删除后该会话的消息与摘要将一并清除（长期记忆保留）。')) {
              App.ws.send(JSON.stringify({
                type: 'delete_session',
                session_id: sid
              } satisfies ClientMessage));
              App.showToast('对话已删除');
            }
          }
        });
      });
      App.sessionListEl.appendChild(item);
    });
  };
  App.renderSessionListFromState = function renderSessionListFromState() {
    App.requestSessionList();
  };
  App.sendText = function sendText(text, attachments) {
    if (!App.ws || App.ws.readyState !== WebSocket.OPEN) {
      App.showToast('未连接到服务器');
      return;
    }
    // 记录用户活跃时间（供 RL 统一状态/心跳取数）
    App._lastUserMessageTime = Date.now();
    App._lastUserInteractTime = Date.now();
    if (App._expressionRL) App._expressionRL.notifyUserMessage();
    // 用户文字输入可打断 AI 回复（唯一打断来源之一；AI action 一律不打断）
    if (App.currentState === App.State.SPEAKING || App.currentState === App.State.THINKING) {
      App.triggerInterrupt();
    }
    // AI 移动不再因用户发消息而打断（AI 可以边走边聊，活人感）
    App.ws.send(JSON.stringify({
      type: 'text',
      content: text,
      // 附件只传服务器返回的元数据（路径/名字/类型），文件本体已经上传完了
      attachments: attachments && attachments.length ? attachments : undefined
    } satisfies ClientMessage));
  };
  /* ============================================================
   *  RL 统一调度心跳（完全统摄：让后端 proactive_tick 自动跑起来）
   * ============================================================ */
  App._rlLastDispatchTime = 0;
  /** 构建统一状态快照并同步给后端 RL 协调器。
   *  wantDecision=true 时请求统一调度决策（rl_decision → rl_dispatch 闭环）。 */
  App.sendRLSync = function sendRLSync(wantDecision) {
    if (!App.ws || App.ws.readyState !== WebSocket.OPEN) return;
    // 关系状态快照（前端 UnifiedDatingSystem 与后端共享同一套语义）
    let affection = 0, trust = 0, intimacy = 0;
    const ds = App._datingSystem || App._engagementRL;
    if (ds && typeof ds.getDebugInfo === 'function') {
      try {
        const info = ds.getDebugInfo();
        const m = info.relationship && info.relationship.metrics;
        if (m) {
          affection = m.affection || 0;
          trust = m.trust || 0;
          intimacy = m.intimacy || 0;
        }
      } catch (e) {}
    }
    const lastMsg = App._lastUserMessageTime || 0;
    const data = {
      affection,
      trust,
      intimacy,
      emotion: 0,
      want_decision: !!wantDecision,
      event: 'proactive_tick',
      seconds_since_user_message: Math.max(0, (Date.now() - lastMsg) / 1000),
      user_engaged: (Date.now() - lastMsg) < 120000,
    };
    App.ws.send(JSON.stringify({ type: 'rl_sync', data } satisfies ClientMessage));
  };
  /** 启动 RL 统一调度心跳（动态间隔）：
   *  每 90s 同步，且仅当用户 5 分钟以上未互动时才请求主动决策，
   *  防止"自言自语"。 */
  App.startRLHeartbeat = function startRLHeartbeat() {
    if (App.rlHeartbeat) { clearTimeout(App.rlHeartbeat); App.rlHeartbeat = null; }
    const rlHeartbeatTick = () => {
      if (!App.ws || App.ws.readyState !== WebSocket.OPEN) { App.rlHeartbeat = null; return; }
      // AI 正在说话/思考时跳过，避免打断
      const busy = App.currentState === App.State.SPEAKING || App.currentState === App.State.THINKING;
      // 距上次派发不足 60 秒则跳过（服务端冷却 + 前端兜底，双保险）
      const canRequest = !busy && (Date.now() - (App._rlLastDispatchTime || 0)) >= 60000;
      // 用户 5 分钟以上未互动才请求主动决策；刚互动过只同步状态（AI 不说话）
      if (canRequest) {
        const secSinceMsg = (Date.now() - (App._lastUserMessageTime || 0)) / 1000;
        App.sendRLSync(secSinceMsg >= 300);
      }
      App.rlHeartbeat = setTimeout(rlHeartbeatTick, 90000);
    };
    App.rlHeartbeat = setTimeout(rlHeartbeatTick, 30000);
  };
  App.sendAudioBase64 = function sendAudioBase64(b64, mimeType) {
    if (!App.ws || App.ws.readyState !== WebSocket.OPEN) {
      App.showToast('未连接到服务器');
      return;
    }
    App.ws.send(JSON.stringify({
      type: 'audio',
      data: b64,
      mime_type: mimeType || 'audio/webm'
    } satisfies ClientMessage));
  };
  /* ============================================================
   *  DSH 桥接：AI 助手任务确认 / 进度 / 结果
   * ============================================================ */
  App.harnessRequestId = null;
  App._harnessPollTimer = null;
  App._harnessPolling = false;


  /* ---------- 工作清单搁置提醒：聊天框里一条提示卡（无需作答，只为被看见） ---------- */
  function renderPlanStallCard(text: string) {
    if (!App.messagesEl) return;
    const card = document.createElement('div');
    card.className = 'msg plan-stall-card';
    card.innerHTML =
      '<div class="stall-head">' +
        '<span class="stall-icon">📋</span>' +
        '<span class="stall-title">工作清单搁置提醒</span>' +
        '<span class="stall-status">⏸ 已中断</span>' +
      '</div>' +
      '<div class="stall-question"></div>';
    const qEl = card.querySelector('.stall-question') as HTMLElement | null;
    if (qEl) qEl.textContent = text || '清单很久没动了';
    App.messagesEl.appendChild(card);
    App._trimMessages();
    App.bumpNewMsg(card);
    App.scrollToBottom();
    App.notifyFullscreenChat();
    try { if (App.showToast) App.showToast('📋 ' + (text || '工作清单搁置了')); } catch (e) { /* toast 可选 */ }
  }




  /** 弹出卡片（bridge_confirm 触发）。request_id 前缀决定语义：
   *  tool_gate: → 工具确认卡（允许一次 / 总是允许 / 拒绝）
   *  其余       → 任务确认卡（DSH 执行任务前的人工闸门） */
  App.showHarnessConfirm = function showHarnessConfirm(requestId, task, options) {
    const modal = document.getElementById('harness-modal');
    if (!modal) return;
    const taskEl = modal.querySelector('.harness-task');
    const replyEl = document.getElementById('harness-reply');
    const hintEl = document.getElementById('harness-hint');
    const cancelBtn = document.getElementById('harness-cancel-btn');
    const approveBtn = document.getElementById('harness-approve-btn');
    const alwaysBtn = document.getElementById('harness-always-btn');
    const rid = typeof requestId === 'string' ? requestId : '';
    const isToolGate = rid.indexOf('tool_gate:') === 0;
    if (taskEl) taskEl.textContent = task || '（空任务）';
    if (replyEl) { replyEl.style.display = 'none'; replyEl.textContent = ''; }
    modal.style.display = 'flex';
    App.harnessRequestId = requestId;
    if (hintEl) hintEl.textContent = '角色「' + ((App.rcRoleName && App.rcRoleName.value) || '大白') + '」想请 AI 助手执行上面的任务，确认后才会真正动手，你也可以直接关闭拒绝。';
    cancelBtn.textContent = '拒绝';
    approveBtn.textContent = isToolGate ? '允许一次' : '确认执行';
    // 工具级确认卡（tool_gate: 前缀）才显示『总是允许』；任务卡不显示
    if (alwaysBtn) alwaysBtn.style.display = isToolGate ? '' : 'none';
    approveBtn.style.display = '';
    cancelBtn.style.display = '';
    App._harnessPolling = true;
    App.harnessPoll();
  };

  /** 轮询任务状态（确认卡片常驻期间每 1.5s 一次；bridge_status 推送也会调用） */
  App.harnessPoll = function harnessPoll() {
    if (!App.harnessRequestId || !App._harnessPolling) return;
    if (App._harnessPollTimer) clearTimeout(App._harnessPollTimer);
    fetch('/api/bridge/status?request_id=' + encodeURIComponent(App.harnessRequestId))
      .then(r => r.json())
      .then(data => {
        if (data && data.ok) App.updateHarnessStatus(data);
        if (App._harnessPolling) {
          App._harnessPollTimer = setTimeout(App.harnessPoll, 1500);
        }
      })
      .catch(() => {
        if (App._harnessPolling) {
          App._harnessPollTimer = setTimeout(App.harnessPoll, 3000);
        }
      });
  };

  /** 更新确认卡片状态（bridge_status 推送 + 轮询共用） */
  App.updateHarnessStatus = function updateHarnessStatus(msg) {
    if (!msg || msg.request_id !== App.harnessRequestId) return;
    const modal = document.getElementById('harness-modal');
    const replyEl = document.getElementById('harness-reply');
    const actionsEl = document.getElementById('harness-actions');
    const hintEl = document.getElementById('harness-hint');
    const cancelBtn = document.getElementById('harness-cancel-btn');
    const approveBtn = document.getElementById('harness-approve-btn');
    if (!modal) return;
    if (msg.status === 'running') {
      if (hintEl) hintEl.textContent = '🤖 智能体正在执行…（右侧任务中心可看实时进度，随时可中断）';
      cancelBtn.textContent = '中断';
      cancelBtn.style.display = '';
      approveBtn.style.display = 'none';
    } else if (msg.status === 'done') {
      App._harnessPolling = false;
      if (hintEl) hintEl.textContent = '';
      if (replyEl) {
        replyEl.style.display = 'block';
        replyEl.textContent = msg.reply || '（AI 助手没有返回文字）';
      }
      approveBtn.style.display = 'none';
      cancelBtn.textContent = '关闭';
      // 结果由聊天框的 DSH 直播卡展示（避免重复气泡）
      if (msg.reply && !App.dshCardExists(msg.request_id) && App.onBridgeSay) App.onBridgeSay(msg.reply);
    } else if (msg.status === 'error') {
      App._harnessPolling = false;
      if (hintEl) hintEl.textContent = '⚠️ ' + (msg.error || '执行失败');
      if (replyEl) replyEl.style.display = 'none';
      approveBtn.style.display = 'none';
      cancelBtn.textContent = '关闭';
    } else if (msg.status === 'cancelled') {
      App._harnessPolling = false;
      if (hintEl) hintEl.textContent = '已取消，任务没有执行。';
      approveBtn.style.display = 'none';
      cancelBtn.textContent = '关闭';
    }
  };

  /** 确认/拒绝调用（按钮事件在模块底部绑定一次）
   *  always=true 是确认卡第三选项：允许这一次 + 写进永久白名单（settings.json） */
  App.harnessApprove = function harnessApprove(approve, always) {
    if (!App.harnessRequestId) return;
    const rid = App.harnessRequestId;
    const hintEl = document.getElementById('harness-hint');
    if (hintEl) hintEl.textContent = always ? '已记住，同类操作以后不再询问。' : (approve ? '已确认，正在交给 AI 助手（DSH）执行…' : '已拒绝，任务未执行。');
    const ctrlAborter = new AbortController();
    const timer = setTimeout(() => ctrlAborter.abort(), 20000);
    fetch('/api/bridge/confirm', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ request_id: rid, approve, always: always ? true : undefined })
    }).then(r => r.json()).then(data => {
      clearTimeout(timer);
      if (data && data.ok && data.status === 'running') {
        if (App._harnessPolling) { App.harnessPoll(); }
      } else if (data && data.ok && data.status === 'pending') {
        // 入队排队中，继续轮询
        if (App._harnessPolling) App.harnessPoll();
      } else {
        // cancelled 等
        if (hintEl) hintEl.textContent = '已取消，任务没有执行。';
        App._harnessPolling = false;
      }
      // 拒绝后让 AI 自然回应（画图类会转回 image_gen_create），避免对话卡住
      if (!approve && App.notifyTaskDeclined) App.notifyTaskDeclined();
    }).catch(() => {
      clearTimeout(timer);
      if (hintEl) hintEl.textContent = '⚠️ 提交失败，请检查服务器。';
    });
  };

  /** 关闭确认卡片 */
  App.harnessClose = function harnessClose() {
    App._harnessPolling = false;
    if (App._harnessPollTimer) { clearTimeout(App._harnessPollTimer); App._harnessPollTimer = null; }
    App.harnessRequestId = null;
    const modal = document.getElementById('harness-modal');
    if (modal) modal.style.display = 'none';
  };

  /** 反向通道：AI 助手给用户递话（经信使，渲染到聊天） */
  App.onBridgeSay = function onBridgeSay(text) {
    if (!text) return;
    const el = document.createElement('div');
    el.className = 'msg system harness-msg';
    el.textContent = '🤖 AI助手：' + text;
    App.messagesEl.appendChild(el);
    App._trimMessages();
    App.scrollToBottom();
    App.notifyFullscreenChat();
  };

  /* 按钮事件绑定（模块加载时执行一次） */
  (function bindHarnessUI() {
    const confirmBtn = document.getElementById('harness-approve-btn');
    const cancelBtn = document.getElementById('harness-cancel-btn');
    const alwaysBtn = document.getElementById('harness-always-btn');
    const closeBtn = document.getElementById('harness-modal-close');
    const backdrop = document.querySelector('#harness-modal .modal-backdrop');
    if (confirmBtn) confirmBtn.addEventListener('click', () => App.harnessApprove(true, false));
    if (alwaysBtn) alwaysBtn.addEventListener('click', () => App.harnessApprove(true, true));
    if (cancelBtn) cancelBtn.addEventListener('click', () => {
      // 取消/关闭二合一：pending→拒绝；running→中断；done/error→关闭
      if (App.harnessRequestId && (App._harnessPolling || App.harnessStatus === 'running')) {
        const wasPolling = App._harnessPolling;
        fetch('/api/bridge/cancel', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ request_id: App.harnessRequestId })
        }).catch(() => {});
        if (wasPolling) App.harnessClose();
        else App.harnessClose();
      } else {
        App.harnessClose();
      }
    });
    const dismiss = () => {
      App.harnessClose();
    };
    if (closeBtn) closeBtn.addEventListener('click', dismiss);
    if (backdrop) backdrop.addEventListener('click', dismiss);
  })();

  /* ============================================================
   *  TTS 播放 + 口型同步
   * ============================================================ */
});
