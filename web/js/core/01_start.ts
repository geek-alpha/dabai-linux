import type { AppKernel } from '../types/app-kernel.js';

export default (function init(App: AppKernel) {
  /* ---------- DOM ---------- */
  App.$ = (id: string) => document.getElementById(id);
  App.canvas = App.$('three-canvas') as HTMLCanvasElement | null;
  App.statusBadge = App.$('status-badge') as HTMLDivElement | null;
  App.subtitle = App.$('subtitle') as HTMLDivElement | null;
  App.messagesEl = App.$('messages') as HTMLDivElement | null;
  App.scrollHint = App.$('scroll-hint') as HTMLDivElement | null;
  App.textInput = App.$('text-input') as HTMLTextAreaElement | null;
  App.sendBtn = App.$('send-btn') as HTMLButtonElement | null;
  App.voiceBtn = App.$('voice-btn') as HTMLButtonElement | null;
  App.toastEl = App.$('toast') as HTMLDivElement | null;
  App.resetCamBtn = App.$('reset-cam-btn') as HTMLButtonElement | null;
  App.fullscreenBtn = App.$('fullscreen-btn') as HTMLButtonElement | null;
  App.chatToggle = App.$('chat-toggle') as HTMLButtonElement | null;
  App.dropHint = App.$('drop-hint') as HTMLDivElement | null;
  App.modelLoading = App.$('model-loading') as HTMLDivElement | null;
  App.modelLoadingText = App.$('model-loading-text') as HTMLDivElement | null; // 背景场景
  App.bgBtn = App.$('bg-btn') as HTMLButtonElement | null;
  App.bgModal = App.$('bg-modal') as HTMLDivElement | null;
  App.bgModalClose = App.$('bg-modal-close') as HTMLButtonElement | null;
  App.bgListEl = App.$('bg-list') as HTMLDivElement | null;
  App.bgFileInput = App.$('bg-file-input') as HTMLInputElement | null; // 移动模式
  App.moveBtn = App.$('move-btn') as HTMLButtonElement | null; // 第一人称探索
  App.fpvBtn = App.$('fpv-btn') as HTMLButtonElement | null;
  App.fpvCrosshair = App.$('fpv-crosshair'); // 运行时动态创建
  App.fpvJoystick = App.$('fpv-joystick'); // 运行时动态创建
  App.fpvJoystickThumb = App.$('fpv-joystick-thumb'); // 运行时动态创建
  App.fpvExitBtn = App.$('fpv-exit') as HTMLButtonElement | null; // 对我的称呼（已集成到角色卡片）
  // 相机设置
  App.roleCardBtn = App.$('role-card-btn') as HTMLButtonElement | null;
  App.roleCardModal = App.$('role-card-modal') as HTMLDivElement | null;
  App.roleCardModalClose = App.$('role-card-modal-close') as HTMLButtonElement | null;
  App.roleCardList = App.$('role-card-list') as HTMLDivElement | null;
  App.roleCardCreateBtn = App.$('role-card-create-btn') as HTMLButtonElement | null;
  App.roleCardEditModal = App.$('role-card-edit-modal') as HTMLDivElement | null;
  App.roleCardEditClose = App.$('role-card-edit-close') as HTMLButtonElement | null;
  App.rcName = App.$('rc-name') as HTMLInputElement | null;
App.rcRoleName = App.$('rc-role-name') as HTMLInputElement | null;
  App.rcUserName = App.$('rc-user-name') as HTMLInputElement | null;
  App.rcModelSelect = App.$('rc-model-select') as HTMLSelectElement | null;
  App.rcModelUploadBtn = App.$('rc-model-upload-btn') as HTMLButtonElement | null;
  App.rcModelFileInput = App.$('rc-model-file-input') as HTMLInputElement | null;
  // 模型供应商（全局资源）+ 角色卡片选供应商/模型
  App.llmProviderBtn = App.$('llm-provider-btn') as HTMLButtonElement | null;
  App.providerModal = App.$('provider-modal') as HTMLDivElement | null;
  App.providerModalClose = App.$('provider-modal-close') as HTMLButtonElement | null;
  App.providerModalActive = App.$('provider-modal-active') as HTMLSpanElement | null;
  App.providerList = App.$('provider-list') as HTMLDivElement | null;
  App.providerCreateBtn = App.$('provider-create-btn') as HTMLButtonElement | null;
  App.providerEditModal = App.$('provider-edit-modal') as HTMLDivElement | null;
  App.providerEditClose = App.$('provider-edit-close') as HTMLButtonElement | null;
  App.providerEditTitle = App.$('provider-edit-title') as HTMLElement | null;
  App.providerName = App.$('provider-name') as HTMLInputElement | null;
  App.providerKind = App.$('provider-kind') as HTMLSelectElement | null;
  App.providerBaseUrl = App.$('provider-base-url') as HTMLInputElement | null;
  App.providerApiKey = App.$('provider-api-key') as HTMLInputElement | null;
  App.providerDefaultModel = App.$('provider-default-model') as HTMLInputElement | null;
  App.providerVision = App.$('provider-vision') as HTMLSelectElement | null;
  App.providerTestBtn = App.$('provider-test-btn') as HTMLButtonElement | null;
  App.providerTestResult = App.$('provider-test-result') as HTMLSpanElement | null;
  App.providerModels = App.$('provider-models') as HTMLSelectElement | null;
  App.providerSaveBtn = App.$('provider-save-btn') as HTMLButtonElement | null;
  App.providerDeleteBtn = App.$('provider-delete-btn') as HTMLButtonElement | null;
  // 云端 API 网络代理（LLM / TTS / STT / 画图）
  App.llmProxyMode = App.$('llm-proxy-mode') as HTMLSelectElement | null;
  App.llmProxyUrl = App.$('llm-proxy-url') as HTMLInputElement | null;
  App.llmProxySaveBtn = App.$('llm-proxy-save') as HTMLButtonElement | null;
  App.llmProxyStatus = App.$('llm-proxy-status') as HTMLDivElement | null;
  // 角色卡片 TTS：API 供应商（应用配置全部在卡片）
  App.rcTtsApiPanel = App.$('rc-tts-api-panel') as HTMLDivElement | null;
  App.rcTtsApiUrl = App.$('rc-tts-api-url') as HTMLInputElement | null;
  App.rcTtsApiKey = App.$('rc-tts-api-key') as HTMLInputElement | null;
  App.rcTtsApiModel = App.$('rc-tts-api-model') as HTMLInputElement | null;
  App.rcTtsApiVoice = App.$('rc-tts-api-voice') as HTMLInputElement | null;
  // 角色卡片内的大语言模型（供应商 + 模型）
  App.rcLlmProviderSelect = App.$('rc-llm-provider-select') as HTMLSelectElement | null;
  App.rcLlmManageBtn = App.$('rc-llm-manage-btn') as HTMLButtonElement | null;
  App.rcLlmModel = App.$('rc-llm-model') as HTMLSelectElement | null;
  App.rcLlmRefreshBtn = App.$('rc-llm-refresh-btn') as HTMLButtonElement | null;
  App.rcLlmTip = App.$('rc-llm-tip') as HTMLDivElement | null;
  App.rcLlmTemperature = App.$('rc-llm-temperature') as HTMLInputElement | null;
  App.rcLlmTempVal = App.$('rc-llm-temp-val') as HTMLSpanElement | null;
  App.rcLlmVision = App.$('rc-llm-vision') as HTMLSelectElement | null;
  App.rcLlmVisionTip = App.$('rc-llm-vision-tip') as HTMLDivElement | null;
  App.rcTtsTabs = App.$('rc-tts-tabs') as HTMLDivElement | null;
  App.rcTtsEdgePanel = App.$('rc-tts-edge-panel') as HTMLDivElement | null;
  App.rcTtsGsoPanel = App.$('rc-tts-gso-panel') as HTMLDivElement | null;
  App.rcVoiceSelect = App.$('rc-voice-select') as HTMLSelectElement | null;
  App.rcRateRange = App.$('rc-rate-range') as HTMLInputElement | null;
  App.rcRateVal = App.$('rc-rate-val') as HTMLLabelElement | null;
  App.rcGsoUrl = App.$('rc-gso-url') as HTMLInputElement | null;
  App.rcGsoRef = App.$('rc-gso-ref') as HTMLInputElement | null;
  App.rcGsoChar = App.$('rc-gso-char') as HTMLInputElement | null;
  // 语音识别（STT）独立设置
  App.rcSttTabs = App.$('rc-stt-tabs') as HTMLDivElement | null;
  App.rcSttCloudPanel = App.$('rc-stt-cloud-panel') as HTMLDivElement | null;
  App.rcSttApiUrl = App.$('rc-stt-api-url') as HTMLInputElement | null;
  App.rcSttApiKey = App.$('rc-stt-api-key') as HTMLInputElement | null;
  App.rcSttModel = App.$('rc-stt-model') as HTMLInputElement | null;
  // 识别方式切换 tab 绑定点击（仅云端 / 自动）
  App.rcSttTabs?.querySelectorAll('.tts-tab').forEach(tab => {
    tab.addEventListener('click', () => {
      App.switchRcSttProvider((tab as HTMLElement).dataset.provider || 'cloud');
    });
  });
  App.rcSttSaveBtn = App.$('rc-stt-save-btn') as HTMLButtonElement | null;
  App.rcSttTip = App.$('rc-stt-tip') as HTMLSpanElement | null;
  App.rcSystemPrompt = App.$('rc-system-prompt') as HTMLTextAreaElement | null;
  App.rcApplyBtn = App.$('rc-apply-btn') as HTMLButtonElement | null;
  App.rcDeleteBtn = App.$('rc-delete-btn') as HTMLButtonElement | null;
  App.rcToolsEnabled = App.$('rc-tools-enabled') as HTMLInputElement | null;
  App.rcToolsField = App.$('rc-tools-field') as HTMLDivElement | null;
  App.rcToolsList = App.$('rc-tools-list') as HTMLDivElement | null; // 相机设置
  App.rcAnimEnabled = App.$('rc-anim-enabled') as HTMLInputElement | null;
  App.rcAnimField = App.$('rc-anim-field') as HTMLDivElement | null;
  App.rcAnimList = App.$('rc-anim-list') as HTMLDivElement | null;
  App.camSettingsBtn = App.$('cam-settings-btn') as HTMLButtonElement | null;
  App.camSettingsModal = App.$('cam-settings-modal') as HTMLDivElement | null;
  App.camSettingsModalClose = App.$('cam-settings-modal-close') as HTMLButtonElement | null;
  App.camHeightRange = App.$('cam-height-range') as HTMLInputElement | null;
  App.camHeightVal = App.$('cam-height-val') as HTMLLabelElement | null;
  App.camDistanceRange = App.$('cam-distance-range') as HTMLInputElement | null;
  App.camDistanceVal = App.$('cam-distance-val') as HTMLLabelElement | null;
  App.camTiltRange = App.$('cam-tilt-range') as HTMLInputElement | null;
  App.camTiltVal = App.$('cam-tilt-val') as HTMLLabelElement | null;
  // 保存按钮绑定点击
  App.rcSttSaveBtn?.addEventListener('click', () => { App.saveRcSttConfig(); });
  App.camSettingsSaveBtn = App.$('cam-settings-save-btn') as HTMLButtonElement | null;
  // 在线音乐
  App.musicBtn = App.$('music-btn') as HTMLButtonElement | null;
  App.musicModal = App.$('music-modal') as HTMLDivElement | null;
  App.musicModalClose = App.$('music-modal-close') as HTMLButtonElement | null;
  App.musicTabSearch = App.$('music-tab-search') as HTMLButtonElement | null;
  App.musicTabPlaylists = App.$('music-tab-playlists') as HTMLButtonElement | null;
  App.musicTabBoards = App.$('music-tab-boards') as HTMLButtonElement | null;
  App.musicPaneSearch = App.$('music-pane-search') as HTMLDivElement | null;
  App.musicPanePlaylists = App.$('music-pane-playlists') as HTMLDivElement | null;
  App.musicPaneBoards = App.$('music-pane-boards') as HTMLDivElement | null;
  App.musicBoardsEl = App.$('music-boards') as HTMLDivElement | null;
  App.musicSearchInput = App.$('music-search-input') as HTMLInputElement | null;
  App.musicSearchBtn = App.$('music-search-btn') as HTMLButtonElement | null;
  App.musicSearchResults = App.$('music-search-results') as HTMLDivElement | null;
  App.musicPlaylistName = App.$('music-playlist-name') as HTMLInputElement | null;
  App.musicPlaylistCreate = App.$('music-playlist-create') as HTMLButtonElement | null;
  App.musicPlaylistsEl = App.$('music-playlists') as HTMLDivElement | null;
  // 音乐「正在播放」控制条
  App.musicNowPlaying = App.$('music-now-playing') as HTMLDivElement | null;
  App.musicNpTitle = App.$('music-np-title') as HTMLSpanElement | null;
  App.musicNpState = App.$('music-np-state') as HTMLSpanElement | null;
  App.musicNpToggle = App.$('music-np-toggle') as HTMLButtonElement | null;
  App.musicNpStop = App.$('music-np-stop') as HTMLButtonElement | null;
  App.musicNpVol = App.$('music-np-vol') as HTMLInputElement | null;
  App.musicNpVolLabel = App.$('music-np-vol-label') as HTMLSpanElement | null;
  App.musicNpTrack = App.$('music-np-track') as HTMLDivElement | null;
  App.musicNpFill = App.$('music-np-fill') as HTMLDivElement | null;
  App.musicNpKnob = App.$('music-np-knob') as HTMLDivElement | null;
  App.musicNpTime = App.$('music-np-time') as HTMLSpanElement | null;
  // 在线视频
  App.videoBtn = App.$('video-btn') as HTMLButtonElement | null;
  App.videoModal = App.$('video-modal') as HTMLDivElement | null;
  App.videoModalClose = App.$('video-modal-close') as HTMLButtonElement | null;
  App.videoSearchInput = App.$('video-search-input') as HTMLInputElement | null;
  App.videoSearchBtn = App.$('video-search-btn') as HTMLButtonElement | null;
  App.videoSearchResults = App.$('video-search-results') as HTMLDivElement | null;
  App.videoPlatformChips = App.$('video-platform-chips') as HTMLDivElement | null;
  // 视频收藏页签元素
  App.videoTabSearch = App.$('video-tab-search') as HTMLButtonElement | null;
  App.videoTabFavorites = App.$('video-tab-favorites') as HTMLButtonElement | null;
  App.videoPaneSearch = App.$('video-pane-search') as HTMLDivElement | null;
  App.videoPaneFavorites = App.$('video-pane-favorites') as HTMLDivElement | null;
  App.videoFavCategoryInput = App.$('video-fav-category-input') as HTMLInputElement | null;
  App.videoFavCategoryCreate = App.$('video-fav-category-create') as HTMLButtonElement | null;
  App.videoFavCategories = App.$('video-fav-categories') as HTMLDivElement | null;
  App.videoFavList = App.$('video-fav-list') as HTMLDivElement | null;
  // 观看历史页签元素
  App.videoTabHistory = App.$('video-tab-history') as HTMLButtonElement | null;
  App.videoPaneHistory = App.$('video-pane-history') as HTMLDivElement | null;
  App.videoHistoryList = App.$('video-history-list') as HTMLDivElement | null;
  App.videoHistoryClear = App.$('video-history-clear') as HTMLButtonElement | null;
  // 视频源页签元素
  App.videoTabSources = App.$('video-tab-sources') as HTMLButtonElement | null;
  App.videoPaneSources = App.$('video-pane-sources') as HTMLDivElement | null;
  App.videoSrcBuiltin = App.$('video-src-builtin') as HTMLDivElement | null;
  App.videoSrcCustom = App.$('video-src-custom') as HTMLDivElement | null;
  App.videoSrcName = App.$('video-src-name') as HTMLInputElement | null;
  App.videoSrcUrl = App.$('video-src-url') as HTMLInputElement | null;
  App.videoSrcAdd = App.$('video-src-add') as HTMLButtonElement | null;
  // 连播队列元素
  App.videoQueuePanel = App.$('video-queue-panel') as HTMLDivElement | null;
  App.videoQueueList = App.$('video-queue-list') as HTMLDivElement | null;
  App.videoQueueClear = App.$('video-queue-clear') as HTMLButtonElement | null;
  // 工作区
  App.workspaceBtn = App.$('workspace-btn') as HTMLButtonElement | null;
  App.workspaceModal = App.$('workspace-modal') as HTMLDivElement | null;
  App.workspaceModalClose = App.$('workspace-modal-close') as HTMLButtonElement | null;
  App.workspacePathInput = App.$('workspace-path-input') as HTMLInputElement | null;
  App.workspaceBrowseBtn = App.$('workspace-browse-btn') as HTMLButtonElement | null;
  App.workspaceRoots = App.$('workspace-roots') as HTMLDivElement | null;
  App.workspaceCurrentPath = App.$('workspace-current-path') as HTMLDivElement | null;
  App.workspaceSaveBtn = App.$('workspace-save-btn') as HTMLButtonElement | null;
  App.workspaceBrowsePath = App.$('workspace-browse-path') as HTMLDivElement | null;
  App.workspaceUpBtn = App.$('workspace-up-btn') as HTMLButtonElement | null;
  App.workspaceSavedList = App.$('workspace-saved-list') as HTMLDivElement | null;
  /* ---------- 状态 ---------- */
  App.State = {
    IDLE: 'idle',
    THINKING: 'thinking',
    LISTENING: 'listening',
    SPEAKING: 'speaking'
  };
  App.currentState = App.State.IDLE;
  App.isRecording = false;
  App.mediaRecorder = null;
  App.audioChunks = [];
  App.ws = null;
  App.wsHeartbeat = null; // WebSocket 心跳定时器
  App.wsReconnectTimer = null; // 重连定时器
  App.wsConnTimeout = null; // 连接超时定时器
  App.currentAudio = null;
  App.audioCtx = null;
  App.analyser = null;
  App.analyserData = null; // --- 流式播放队列 ---
  App.audioQueue = []; // 待播放的句子 [{seq, text, audio_b64, audio_mime}]
  App.isPlayingQueue = false;
  App.currentReplyText = ''; // 当前回复累积文本
  App.currentReplySeg = ''; // 当前语音分句（头顶气泡逐句显示用）
  App.currentReplySession = null; // 当前回复 session_id（用于判断过期消息）
  App._pendingFullText = null; // audio_end 下发的完整文本（队列播完后再应用）
  App.pendingAIMsgEl = null; // 流式回复占位 DOM
  // --- VAD 自动对话模式 ---
  App.voiceMode = 'press'; // 'press' 按住说话 | 'auto' 自动对话（默认按住说话，不自动聆听）
  App.vadStream = null; // VAD 持续监听的 MediaStream
  App.vadAnalyser = null;
  App.vadData = null;
  App.vadRAF = null;
  App.vadState = 'idle'; // 'idle' | 'recording'
  App.vadSilenceStart = 0;
  App.vadInterruptStart = 0;
  App.vadVoiceStart = 0; // 人声连续确认计时（防噪声单帧误判）
  App.vadRecorder = null;
  App.vadChunks = [];
  App._vadClonedTrack = null;
  App.VAD_THRESHOLD = 0.05; // 普通说话音量阈值（降低让小声也能触发）
  App.VAD_INTERRUPT_THRESHOLD = 0.07; // AI 说话时打断阈值（原 0.09 太高，正常音量打断不了；降到 0.07 让正常说话即可打断）
  App.VAD_SILENCE_MS = 700; // 静音判定（原 1000ms 让短句结尾拖沓；700ms 更利落，PCM 预滚已消除切头风险）
  App.VAD_INTERRUPT_MS = 200; // AI 说话时持续多久高音量判定打断（缩短加快响应）
  App.VAD_MIN_RECORD_MS = 350; // 最短录音时长，避免短噪音触发
  // --- 人声特性检测（VAD 噪音过滤：只有人声才触发聆听中）---
  App.VAD_VOICE_ENABLED = true; // 是否启用语音特性评分（true=严格人声过滤）
  App.VAD_VOICE_SCORE_THRESHOLD = 0.32; // 语音特性评分阈值（原 0.40 太严，正常说话常被误判为噪音而不触发）
  App.VAD_VOICE_CONFIRM_MS = 80; // 人声连续确认时长（音量越大确认越快，最低50ms）
  App.VAD_VOICE_MIN_F0 = 70; // 基频下限 Hz（低于此视为低频噪音/机械声）
  App.VAD_VOICE_MAX_F0 = 480; // 基频上限 Hz（高于此视为高频噪音/口哨）
  App.VAD_HARMONIC_BINS = 3; // 基频倍数谐波检测数（F0×2, ×3, ×4）
  // --- 锁屏模式（防误触） ---
  App.lockMode = false; // 是否处于锁屏模式：锁定操作仅语音对话，角色照常活动
  App.LOCK_KEY = 'dabai.lockMode';
  /* ============================================================
   *  互动强化学习（非游戏模式）
   * ============================================================ */
  App.engagementRLActive = false; // 默认关闭，稍后由 boot 根据配置开启
  App._engagementRL = null;

  /* ============================================================
   *  恋爱养成强化学习系统
   * ============================================================ */
  App.datingSystemActive = false; // 默认关闭，由 boot 或用户开启
  App._datingSystem = null;

  /* ============================================================
   *  Three.js 场景
   * ============================================================ */
});
