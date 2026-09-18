import type { AppKernel } from '../types/app-kernel.js';

export default (function init(App: AppKernel) {
  /* ============================================================
   *  事件绑定
   * ============================================================ */
  App.updateCameraSettingsUI = function updateCameraSettingsUI() {
    // 隐私护栏：非管理员的滑块下限直接抬到护栏值，避免「拉到底却没反应」
    if (window.__ROLE !== 'admin') {
      if (App.camHeightRange) App.camHeightRange.min = String(App.USER_MIN_CAM_HEIGHT);
      if (App.camDistanceRange) App.camDistanceRange.min = String(App.USER_MIN_CAM_DISTANCE);
    }
    if (App.camHeightRange) {
      App.camHeightRange.value = String(App.cameraHeight);
      App.camHeightVal!.textContent = App.cameraHeight.toFixed(2);
    }
    if (App.camDistanceRange) {
      App.camDistanceRange.value = String(App.cameraDistance);
      App.camDistanceVal!.textContent = App.cameraDistance.toFixed(2);
    }
    if (App.camTiltRange) {
      App.camTiltRange.value = String(App.cameraTiltDeg);
      App.camTiltVal!.textContent = App.cameraTiltDeg + '°';
    }
  };
  App.openCamSettingsModal = function openCamSettingsModal() {
    App.updateCameraSettingsUI();
    if (App.camSettingsModal) App.camSettingsModal.classList.add('show');
  };
  App.closeCamSettingsModal = function closeCamSettingsModal() {
    if (App.camSettingsModal) App.camSettingsModal.classList.remove('show');
  };
  App.bindEvents = function bindEvents() {
    // 文本发送
    function submitText() {
      const text = App.textInput!.value.trim();
      // 附件先取快照：上传中的不发送，留在待发条里等传完
      const atts = App.takeAttachments();
      if (!text && !atts.length) return;
      App.addUserMsg(text, false, atts);
      App.sendText(text, atts);
      App.clearAttachments();
      App.textInput!.value = '';
      App.textInput!.style.height = ''; // 清空后恢复单行高度
      App.setState(App.State.THINKING);
      App.showTyping();
      // 通知互动 RL 智能体 + 恋爱养成系统
      // 独立系统游戏（赛博公司）：文字消息由游戏内蜂群接管（addUserMsg hook），
      // 不触发大厅 RL（否则大厅角色会"听到"玩家消息而乱入）
      if (App._engagementRL) App._engagementRL.notifyUserMessage();
      if (App._datingSystem) App._datingSystem.notifyUserMessage(text);
    }
    App.sendBtn!.addEventListener('click', submitText);
    App.textInput!.addEventListener('keydown', e => {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        submitText();
      }
    });
    // 输入框随内容自动增高，保证打长句时能看到完整文字
    const autoGrowInput = () => {
      const el = App.textInput!;
      el.style.height = 'auto';
      el.style.height = Math.min(el.scrollHeight, 140) + 'px';
    };
    App.textInput!.addEventListener('input', autoGrowInput);
    App.textInput!.addEventListener('compositionend', autoGrowInput);

    // 输入聚焦 → 打字轻载（降到 20fps，不停帧）；失焦立即恢复。
    // 半屏/默认态下 3D 舞台仍然看得见，绝不能再走「停掉整个帧循环」那条路 ——
    // 那会让角色当场僵住、与全屏观感不一致。真正省 CPU 的是少画几帧，不是一帧不画。
    // 不透明全屏的静默总闸另有语义（看不见才全停），见 13_messages.setChatFullscreen。
    App.textInput!.addEventListener('focus', () => {
      if (App.setTypingLite) App.setTypingLite(true);
    });
    App.textInput!.addEventListener('blur', () => {
      if (App.setTypingLite) App.setTypingLite(false);
    });

    // 语音按钮 - 二合一：短按切换模式（按住说话 ↔ 自动对话），长按按住说话
    let voicePressed = false,
      pressY = 0,
      isLongPress = false,
      pressTimer: ReturnType<typeof setTimeout> | null = null;
    const pressStart = (e: TouchEvent | MouseEvent) => {
      e.preventDefault();
      if (voicePressed) return;
      voicePressed = true;
      isLongPress = false;
      pressY = 'touches' in e ? e.touches[0].clientY : e.clientY;
      // 超过阈值视为长按（按住说话）；否则松手视为短按（切换模式）
      pressTimer = setTimeout(() => {
        pressTimer = null;
        isLongPress = true;
        // 长按录音仅在按住说话模式生效；自动对话模式由 VAD 接管
        if (App.voiceMode === 'press') App.startRecording();
      }, 320);
    };
    const pressMove = (e: TouchEvent | MouseEvent) => {
      if (!voicePressed) return;
      // 上滑 60px 取消录音（无需弹窗提示，语音按钮样式变化即可指示状态）
      e.preventDefault();
    };
    const pressEnd = (e: TouchEvent | MouseEvent) => {
      if (!voicePressed) return;
      voicePressed = false;
      if (pressTimer) {
        clearTimeout(pressTimer);
        pressTimer = null;
      }
      const y = 'changedTouches' in e ? e.changedTouches[0].clientY : e.clientY;
      if (isLongPress) {
        isLongPress = false;
        // 仅在按住说话模式下有录音要停；自动模式长按无副作用
        if (App.voiceMode === 'press') {
          const cancel = pressY - y > 60;
          App.stopRecording(cancel);
        }
      } else {
        // 短按 → 切换模式
        const target = App.voiceMode === 'auto' ? 'press' : 'auto';
        App.setVoiceMode(target);
      }
    };
    App.voiceBtn!.addEventListener('touchstart', pressStart, {
      passive: false
    });
    App.voiceBtn!.addEventListener('touchmove', pressMove, {
      passive: false
    });
    App.voiceBtn!.addEventListener('touchend', pressEnd);
    App.voiceBtn!.addEventListener('touchcancel', () => {
      voicePressed = false;
      if (pressTimer) {
        clearTimeout(pressTimer);
        pressTimer = null;
      }
      if (isLongPress) {
        isLongPress = false;
        if (App.voiceMode === 'press') App.stopRecording(true);
      }
    });
    App.voiceBtn!.addEventListener('mousedown', pressStart);
    window.addEventListener('mousemove', pressMove);
    window.addEventListener('mouseup', pressEnd);

    // 背景场景管理
    App.bgBtn!.addEventListener('click', App.openBgModal);
    App.bgModalClose!.addEventListener('click', App.closeBgModal);
    App.bgModal!.querySelector('.modal-backdrop')!.addEventListener('click', App.closeBgModal);
    App.bgFileInput!.addEventListener('change', e => {
      const input = e.target as HTMLInputElement;
      const f = input.files![0];
      if (f) App.uploadBackgroundFile(f);
      input.value = '';
    });
    // 默认背景卡片在 renderBackgroundList 中动态渲染，点击已由 16_bg_ui.js 绑定

    // 在线音乐弹窗
    App.musicBtn!.addEventListener('click', App.openMusicModal);
    App.musicModalClose!.addEventListener('click', App.closeMusicModal);
    App.musicModal!.querySelector('.modal-backdrop')!.addEventListener('click', App.closeMusicModal);
    App.musicTabSearch!.addEventListener('click', () => App.switchMusicTab('search'));
    App.musicTabPlaylists!.addEventListener('click', () => App.switchMusicTab('playlists'));
    App.musicTabBoards!.addEventListener('click', () => App.switchMusicTab('boards'));
    App.musicSearchBtn!.addEventListener('click', () => App.musicSearch());
    App.musicSearchInput!.addEventListener('keydown', e => {
      if (e.key === 'Enter') App.musicSearch();
    });
    App.musicPlaylistName!.addEventListener('keydown', e => {
      if (e.key === 'Enter') App.createMusicPlaylist();
    });
    App.musicPlaylistCreate!.addEventListener('click', () => App.createMusicPlaylist());

    // 在线视频弹窗
    App.videoBtn!.addEventListener('click', App.openVideoModal);
    App.videoModalClose!.addEventListener('click', App.closeVideoModal);
    App.videoModal!.querySelector('.modal-backdrop')!.addEventListener('click', App.closeVideoModal);
    App.videoSearchBtn!.addEventListener('click', () => App.videoSearch());
    App.videoSearchInput!.addEventListener('keydown', e => {
      if (e.key === 'Enter') App.videoSearch();
    });
    // 视频收藏页签
    App.videoTabSearch!.addEventListener('click', () => App.switchVideoTab('search'));
    App.videoTabFavorites!.addEventListener('click', () => App.switchVideoTab('favorites'));
    App.videoTabHistory!.addEventListener('click', () => App.switchVideoTab('history'));
    App.videoTabSources!.addEventListener('click', () => App.switchVideoTab('sources'));
    App.videoFavCategoryCreate!.addEventListener('click', () => App.createVideoCategory());
    App.videoFavCategoryInput!.addEventListener('keydown', e => {
      if (e.key === 'Enter') App.createVideoCategory();
    });
    // 观看历史：清空
    App.videoHistoryClear!.addEventListener('click', () => App.clearVideoHistory());
    // 视频源：添加自定义源
    App.videoSrcAdd!.addEventListener('click', () => App.addVideoSource());
    App.videoSrcName!.addEventListener('keydown', e => {
      if (e.key === 'Enter') App.addVideoSource();
    });
    App.videoSrcUrl!.addEventListener('keydown', e => {
      if (e.key === 'Enter') App.addVideoSource();
    });
    // 连播队列：清空队列
    App.videoQueueClear!.addEventListener('click', () => App.videoQueueClearAll());

    // 工作区弹窗
    App.workspaceBtn!.addEventListener('click', App.openWorkspaceModal);
    App.workspaceModalClose!.addEventListener('click', App.closeWorkspaceModal);
    App.workspaceModal!.querySelector('.modal-backdrop')!.addEventListener('click', App.closeWorkspaceModal);
    App.workspaceSaveBtn!.addEventListener('click', () => App.saveWorkspace());
    App.workspaceUpBtn!.addEventListener('click', () => App.workspaceGoUp());
    App.workspacePathInput!.addEventListener('keydown', e => {
      if (e.key === 'Enter') App.saveWorkspace();
    });

    // 重置视角（状态复位统一走 App.resetViewState，刷新角色复用同一套基准）
    App.resetCamBtn!.addEventListener('click', () => {
      App.resetViewState();
      App.showToast('视角已重置');
      App.sendAIAction('（用户重置了视角，现在重新端详着你的样子，好好展示自己吧）', true);
    });

    // 相机设置
    if (App.camSettingsBtn) App.camSettingsBtn.addEventListener('click', App.openCamSettingsModal);
    if (App.camSettingsModalClose) App.camSettingsModalClose.addEventListener('click', App.closeCamSettingsModal);
    if (App.camSettingsModal) App.camSettingsModal.querySelector('.modal-backdrop')!.addEventListener('click', App.closeCamSettingsModal);
    if (App.camHeightRange) {
      App.camHeightRange.addEventListener('input', () => {
        App.cameraHeight = Math.max(parseFloat(App.camHeightRange!.value), App.userMinCamHeight());
        App.camHeightRange!.value = String(App.cameraHeight);
        App.camHeightVal!.textContent = App.cameraHeight.toFixed(2);
        App.saveCameraSettings();
        App.recordInteraction();
      });
    }
    if (App.camDistanceRange) {
      App.camDistanceRange.addEventListener('input', () => {
        App.cameraDistance = Math.max(parseFloat(App.camDistanceRange!.value), App.userMinCamDistance());
        App.camDistanceRange!.value = String(App.cameraDistance);
        App.camDistanceVal!.textContent = App.cameraDistance.toFixed(2);
        App.targetCamPos!.z = App.cameraDistance;
        App.DEFAULT_CAM_POS!.z = App.cameraDistance;
        App.saveCameraSettings();
        App.recordInteraction();
      });
    }
    if (App.camTiltRange) {
      App.camTiltRange.addEventListener('input', () => {
        App.cameraTiltDeg = parseInt(App.camTiltRange!.value, 10);
        App.camTiltVal!.textContent = App.cameraTiltDeg + '°';
        App.saveCameraSettings();
        App.recordInteraction();
      });
    }
    if (App.camSettingsSaveBtn) {
      App.camSettingsSaveBtn.addEventListener('click', () => {
        App.closeCamSettingsModal();
        App.showToast(`相机设置已保存：高度 ${App.cameraHeight.toFixed(2)}，距离 ${App.cameraDistance.toFixed(2)}，倾斜 ${App.cameraTiltDeg}°`);
      });
    }

    // 移动模式开关
    App.moveBtn!.addEventListener('click', () => App.setMoveMode(!App.moveMode));

    // 帧率调节按钮：点击循环切换 60/30/20fps
    const fpsBtn = document.getElementById('fps-btn');
    if (fpsBtn) fpsBtn.addEventListener('click', App.cyclePerfTier);

    // 沉浸模式按钮：隐藏所有工具栏，专注对话与角色
    const immerseBtn = document.getElementById('immerse-btn');
    if (immerseBtn) immerseBtn.addEventListener('click', App.toggleImmerseMode);

    // 第一人称探索开关
    App.fpvBtn!.addEventListener('click', App.toggleFPV);
    if (App.fpvExitBtn) App.fpvExitBtn.addEventListener('click', App.exitFPV);

    // VR模式开关（WebXR 沉浸会话；由 webxr-vr.js 统一调度）
    const gyroBtn = document.getElementById('gyro-btn')!;
    // 仅管理员 + 支持 WebXR 的设备才显示按钮（VR 是部署层能力，普通用户无入口）
    if (typeof navigator !== 'undefined' && navigator.xr && window.__ROLE === 'admin') {
      gyroBtn.style.display = '';
    }
    // VR模式：晃动强度 → AI 感知反馈（每帧调用，由RL系统统一调度反馈时机）
    App.updateVRShakeNotify = function updateVRShakeNotify() {
      // RL统一调度：冷却/概率/停止检测均由 unified-dating-system 决策。
      // 兼容实例路径：_datingSystem 可能是 UnifiedDatingSystem/DatingRLSystem（有 dispatchVRFeedback）
      // 或 EngagementRLAgent（需取内部 _unified）
      let core: any = App._datingSystem || App._engagementRL;
      if (core && !core.dispatchVRFeedback && core._unified) core = core._unified;
      if (!core || typeof core.dispatchVRFeedback !== 'function') return; // RL系统未就绪时不发送反馈
      const fb = core.dispatchVRFeedback(0); // dt由RL内部累计（以Date.now为准）
      if (!fb) return;
      if (fb.type === 'up') {
        App.sendAIAction(`（抱着你大幅度上下运动~${fb.intensity}）`, true);
      } else if (fb.type === 'left') {
        App.sendAIAction(`（抱着你大幅度左右运动~${fb.intensity}~）`, true);
      } else if (fb.type === 'stop') {
        App.sendAIAction('（用户停止了摇晃，慢慢安静下来，温柔地抱住了你）', true);
      }
    };

    // VR模式按钮：点击切换（WebXR 沉浸 ↔ 关闭），由 webxr-vr.js 调度
    gyroBtn.addEventListener('click', () => {
      if (App.cycleXrMode) App.cycleXrMode();
    });

    // ===== 全屏模式（布局已是全屏，仅触发浏览器原生全屏隐藏地址栏） =====
    function toggleFullscreen() {
      App.isFullscreen = !App.isFullscreen;
      const app = document.getElementById('app')!;
      if (App.isFullscreen) {
        app.classList.add('fullscreen');
        App.fullscreenBtn!.classList.add('active');
        if (document.documentElement.requestFullscreen) {
          document.documentElement.requestFullscreen().catch(() => {});
        }
        App.showToast('浏览器全屏 · 点击右下角气泡查看对话');
        App.sendAIAction('（用户进入了全屏模式，把所有的注意力都给了你，现在你是Ta眼中的全部）', true);
      } else {
        app.classList.remove('fullscreen');
        App.fullscreenBtn!.classList.remove('active');
        App.chatToggle!.classList.remove('has-new');
        if (document.fullscreenElement && document.exitFullscreen) {
          document.exitFullscreen().catch(() => {});
        }
      }
      setTimeout(App.onResize, 350);
    }
    App.fullscreenBtn!.addEventListener('click', toggleFullscreen);
    // 监听 ESC 退出全屏
    document.addEventListener('fullscreenchange', () => {
      if (!document.fullscreenElement && App.isFullscreen) {
        App.isFullscreen = false;
        document.getElementById('app')!.classList.remove('fullscreen');
        App.fullscreenBtn!.classList.remove('active');
        App.chatToggle!.classList.remove('has-new');
        setTimeout(App.onResize, 350);
      }
    });
    // 聊天切换按钮
    App.chatToggle!.addEventListener('click', () => {
      const panel = document.getElementById('chat-panel')!;
      const controls = document.getElementById('controls')!;
      const wasCollapsed = panel.classList.contains('collapsed');
      panel.classList.toggle('collapsed');
      controls.classList.toggle('collapsed');
      // 展开时按钮上移避免遮挡输入框，折叠时归位
      if (wasCollapsed) {
        App.chatToggle!.classList.add('shifted');
      } else {
        App.chatToggle!.classList.remove('shifted');
      }
      App.chatToggle!.classList.remove('has-new');
      setTimeout(App.onResize, 350);
    });

    // 聊天框初始收起隐藏（index.html 初始带 collapsed），切换按钮归位；
    // 点按钮展开到 38% 时按钮才上移（shifted），避免遮挡输入框
    if (!document.getElementById('chat-panel')!.classList.contains('collapsed')) {
      App.chatToggle!.classList.add('shifted');
    }

    // 拖拽导入到舞台
    let dragCounter = 0;
    const stage = App.$('stage')!;
    stage.addEventListener('dragenter', e => {
      e.preventDefault();
      dragCounter++;
      App.dropHint!.classList.add('show');
    });
    stage.addEventListener('dragover', e => {
      e.preventDefault();
    });
    stage.addEventListener('dragleave', e => {
      e.preventDefault();
      dragCounter--;
      if (dragCounter <= 0) {
        dragCounter = 0;
        App.dropHint!.classList.remove('show');
      }
    });
    stage.addEventListener('drop', e => {
      e.preventDefault();
      dragCounter = 0;
      App.dropHint!.classList.remove('show');
      const f = e.dataTransfer!.files[0];
      if (!f) return;
      // 背景弹窗打开时优先作为背景上传，否则作为角色模型
      if (App.bgModal!.classList.contains('show')) {
        App.uploadBackgroundFile(f);
      } else {
        App.uploadModelFile(f);
      }
    });

    // 首次点击解锁音频 + 主动开启语音模式（会弹出麦克风权限请求）
    // 默认按住说话：不自动开启聆听，用户点「自动对话」按钮才进入持续聆听
    document.addEventListener('click', () => {
      App.ensureAudioCtx();
      const savedMode = localStorage.getItem('dabai.voiceMode');
      if (savedMode === 'auto') {
        App.setVoiceMode('auto');
      } else {
        App.voiceMode = 'press';
        App.showToast('按住麦克风按钮说话即可');
      }
    }, {
      once: true
    });

    // 用户主动输入（文字/语音转文字成功）才通知 RL —— 见 notifyUserMessage 调用点。
    // 不再全局监听 click/touchstart：任何点击（播放AI回复、点场景等）都算
    // "用户交互"会污染时间模式、重置AI被动衰减、虚高互动计数，
    // 把非用户主动的行为误算成用户输入。

    // 防双击缩放 / 长按选中
    document.addEventListener('dblclick', e => e.preventDefault());
    document.addEventListener('selectstart', e => {
      if (e.target !== App.textInput) e.preventDefault();
    });
  };
  /* ============================================================
   *  TTS 语音合成设置
   * ============================================================ */
});
