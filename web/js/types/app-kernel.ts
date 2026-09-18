/* ============================================================
 * App 内核类型 —— 阶段 1（类型地基）
 * ------------------------------------------------------------
 * App 是贯穿全部模块的内核对象（web/app.ts 逐个调用各模块的
 * init(App) 挂载属性）。本文件是它的类型总账：
 *
 *   核心区（CORE 下方）      —— 手工维护，类型精确
 *   生成区（GENERATED 下方） —— tools/gen-app-kernel.mjs 扫描
 *     web 目录全部 JS 文件的 App 属性用法自动生成，类型暂为 any，
 *     各模块迁移 .ts 时逐步精化并上移到核心区
 *
 * 全量使用清单（读写计数 + 文件来源）见同目录 app-inventory.json。
 * ============================================================ */

import type * as ThreeNS from 'three';
import type { GLTF, GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';
import type { VRMLoaderPlugin, VRMUtils } from '@pixiv/three-vrm';
import type {
  AudioChunkMessage,
  BridgeStatusMessage,
  InterruptedMessage,
  ScreenCommandArgs,
  ScreenCommandMessage,
  ServerMessage,
  SessionSummary,
  UsageMessage,
} from './ws-protocol.js';

/** 角色状态机取值 */
export interface AppKernelState {
  IDLE: 'idle';
  THINKING: 'thinking';
  LISTENING: 'listening';
  SPEAKING: 'speaking';
}

/** 性能分级：'high'=桌面60fps | 'default'=移动30fps | 'low'=低功耗20fps */
export type PerfTier = 'high' | 'default' | 'low';

/** TTS 引擎取值 */
export type TTSEngine = 'edge_tts' | 'gpt_sovits';

/** /api/tts/config 的配置结构 */
export interface TTSConfig {
  engine?: TTSEngine;
  edge_voice?: string;
  edge_rate?: string;
  gptsovits_url?: string;
  gptsovits_ref_audio?: string;
  gptsovits_character?: string;
}

/** /api/models 列表项 */
export interface ModelInfo {
  url: string;
  name: string;
  type: 'glb' | 'gltf' | 'vrm' | string;
  size: number;
}

/** 麦克风采集约束（getUserMedia audio） */
export interface MicConstraints {
  echoCancellation: boolean;
  noiseSuppression: boolean;
  autoGainControl: boolean;
  channelCount: number;
  sampleRate: { ideal: number };
  sampleSize: { ideal: number };
}

/** 语音对话模式：按住说话 / 自动对话 / 唤醒词待机 */
export type VoiceMode = 'press' | 'auto';

/** TTS 流式音频队列分片（10_tts_lipsync 消费） */
export interface AudioQueueItem {
  seq: number;
  text?: string | null;
  audio_b64?: string | null;
  audio_mime?: string | null;
  thinking?: boolean;
  end?: boolean;
  /** 所属回复会话：气泡据此做「同会话序号单调」守卫，丢弃乱序/过期分句 */
  session_id?: string | null;
}

/** /api/backgrounds 列表项 */
export interface BackgroundInfo {
  url: string;
  name: string;
  type: string;
  size: number;
  is_default?: boolean;
}

/** Token 用量统计（localStorage 持久化累计） */
export interface TokenStats {
  /** 累计实际输入（prompt_tokens 逐轮累加，不再覆盖） */
  context: number;
  /** 累计实际输出（completion_tokens 逐轮累加） */
  completion: number;
  /** 累计总 tokens（输入+输出） */
  total: number;
  /** 累计轮数 */
  rounds: number;
  /** 累计回复条数 */
  msgs: number;
  /** 累计缓存命中 tokens */
  cache_hit: number;
  /** 累计缓存未命中 tokens */
  cache_miss: number;
}

/** 在线音乐歌曲（/api/music/* 返回项） */
export interface MusicSong {
  source: string;
  id: string;
  name: string;
  artists?: string;
  album?: string;
  vip?: boolean;
}

/** BGM/音乐播放器运行状态快照（20_bgm_player 提供，UI 据此实时同步按钮/标题） */
export interface BGMState {
  name: string | null;
  playing: boolean;
  paused: boolean;
  stopped: boolean;
  volume: number;
  currentTime: number;
  duration: number;
}

/** 在线视频结果（/api/video_hub/api/search 返回项） */
export interface VideoItem {
  title: string;
  webpage_url: string;
  platform?: string;
  uploader?: string;
  duration?: number | null;
  view_count?: number;
  thumbnail?: string;
}

/** 大屏播放器运行状态（视频面板 UI 轮询，30_task_big_screen 提供） */
export interface VideoBoardState {
  active: boolean;
  title: string;
  mode: string;        // 'direct' | 'relay'
  phase: string;       // loading | playing | recovering | dead | ended
  paused: boolean;
  ended: boolean;
  dead: boolean;
  recovering: boolean;
  ready: boolean;
  currentTime: number;
  duration: number;
  seekable: boolean;   // 时长已知 → 可拖动进度（direct 走 Range；relay 走服务端 ?ss= 点播模拟）
  volume?: number;     // 当前音量 0..1（静音时为 0）
  muted?: boolean;     // 是否静音
  webpage_url?: string;   // 当前播放视频原页链接（np 面板收藏当前视频用）
  uploader?: string;
  platform?: string;
}

/** 表情动作引擎：动作通道值（数值=单次脉冲，{amp,loops}=振荡） */
export interface MotionChannelValue {
  amp?: number;
  loops?: number;
  blendIn?: number;
  blendOut?: number;
}

/** 骨骼通道：x/y/z 各轴可为数值或振荡 */
export interface BoneChannel {
  x?: number | MotionChannelValue;
  y?: number | MotionChannelValue;
  z?: number | MotionChannelValue;
}

/** 动作定义：dur 必填，骨骼通道 + 情绪/眼神/嘴型/眨眼 */
export interface MotionDef {
  dur: number;
  hold?: number;
  emotion?: string;
  emotionIntensity?: number;
  gaze?: string;
  mouth?: number;
  suppressBlink?: number;
  wink?: number;
  [bone: string]: number | BoneChannel | string | undefined;
}

/** 骨骼旋转偏移（motionOffsets 等） */
export interface BoneOffset {
  x: number;
  y: number;
  z: number;
}

/** PAD 情绪模型：愉悦 / 唤醒 / 支配，各 ∈ [-1, 1] */
export interface PADState {
  pleasure: number;
  arousal: number;
  dominance: number;
}

/** 情绪 → 动作参数（由 PAD 计算，驱动动作系统） */
export interface EmotionParams {
  amplitude: number;        // 动作幅度系数 0.75~1.5
  speed: number;            // 动作速度系数 0.75~1.4
  postureExpansion: number; // 姿态扩张度 0~1（dominance）
  gestureFrequency: number; // 手势频率 0~1（arousal）
  actionBias: Record<string, number>; // 各动作大类权重（pose/walk/turn/dance）
  microPool: string[];      // 情绪倾向的微动作池
  dominantEmotion: string;  // 当前主导情绪标签
}

/** Mixamo 动作片段注册项 */
export interface MixamoClipInfo {
  name: string;
  clip: any;                // THREE.AnimationClip
  emotion?: string;
  loop?: boolean;
  description?: string;
}

/** 动作库单个动作定义 */
export interface AnimEntry {
  name: string;
  file: string;
  emotion?: string;
  loop?: boolean;
  description?: string;
}

/** 动作库分类 */
export interface AnimCategory {
  label: string;
  description?: string;
  animations: AnimEntry[];
}

/** 动作库配置 */
export interface AnimLibraryConfig {
  version: string;
  description?: string;
  baseUrl: string;
  autoLoadOnBoot: boolean;
  categories: Record<string, AnimCategory>;
  emotionMap: Record<string, string[]>;
}

/** VR HUD 命中区按钮（画布像素坐标） */
export interface VrHudButton {
  id: string;
  x: number;
  y: number;
  w: number;
  h: number;
}

/** VR HUD ✕ 触发后的高亮反馈 */
export interface VrHudFlash {
  id: string;
  until: number;
}

/** VR 世界内迷你状态面板（vr-hud 挂载） */
export interface VrHud {
  active: boolean;
  mesh: ThreeNS.Mesh | null;
  tex: ThreeNS.CanvasTexture | null;
  cv: HTMLCanvasElement | null;
  ctx: CanvasRenderingContext2D | null;
  buttons: VrHudButton[];
  flash: VrHudFlash | null;
  dirty: boolean;
  _state: string;
  _ray: ThreeNS.Raycaster;
  _v3: ThreeNS.Vector3;
  _q: ThreeNS.Quaternion;
  /** 视频遥控面板展开中（大白影院控制：播放/进度/音量/片单） */
  videoOpen?: boolean;
  /** 片单（收藏夹）视图展开中 */
  listOpen?: boolean;
  show: () => void;
  hide: () => void;
  markDirty: () => void;
  update: (dt: number) => void;
  hitTest: (origin: ThreeNS.Vector3, dir: ThreeNS.Vector3) => string | null;
  trigger: (id: string) => void;
}

/** 任务树节点状态 */
export type TaskStatus = 'pending' | 'queued' | 'confirming' | 'running' | 'done' | 'error' | 'cancelled';

/** 任务树节点（可任意嵌套） */
export interface TaskTreeNode {
  title?: string;
  status?: TaskStatus | string;
  progress?: number;
  desc?: string;
  open?: boolean;
  children?: TaskTreeNode[];
}

/** 任务树卡片数据（addTaskTreeMsg 入参） */
export interface TaskTreeData {
  title?: string;
  description?: string;
  nodes: TaskTreeNode[];
}

/** 智能体目录项（任务中心 agentOf 返回） */
export interface AgentInfo {
  name: string;
  icon: string;
  color: string;
  desc: string;
}

/** /api/tasks 列表项（任务中心） */
export interface TaskItem {
  id: string;
  title?: string;
  status?: string;
  channel?: string;
  kind?: string;
  brief?: string;
  steps?: string[];
  logs?: string[];
  result?: any;
  error?: string;
  agent?: Partial<AgentInfo>;
  updated_at?: number;
  progress?: number;
}

/** task_event 增量事件（09_websocket 推送） */
export interface TaskEvent {
  id: string;
  channel?: string;
  title?: string;
  status?: string;
  brief?: string;
  step?: string;
  log?: string;
  logs?: string[];
  result?: any;
  error?: any;
  event?: string;
}

/** localStorage 持久化的场景状态（window._restoredScene 同构） */
export interface ScenePersistState {
  camZoom?: number;
  camOffsetX?: number;
  camOffsetY?: number;
  camOffsetZ?: number;
  xrMode?: 'off' | 'webxr';
  moveMode?: boolean;
  backgroundAutoRotate?: boolean;
  charPos?: { x: number; z: number };
  charScale?: number;
  bgPos?: { x: number; z: number };
  bgScale?: number;
}

export interface AppKernel {
  /* @@CORE@@ —— 手工维护区：已迁移 .ts 的模块所用属性，类型精确 */

  /* ---------- 依赖命名空间（app-state 初始化即存在，必填） ---------- */
  THREE: typeof ThreeNS;
  GLTFLoader: typeof GLTFLoader;
  VRMLoaderPlugin: typeof VRMLoaderPlugin;
  VRMUtils: typeof VRMUtils;

  /* ---------- DOM 快捷引用（01_start 挂载，元素类型对照 index.html） ---------- */
  $: (id: string) => HTMLElement | null;
  canvas: HTMLCanvasElement | null;
  statusBadge: HTMLDivElement | null;
  subtitle: HTMLDivElement | null;
  messagesEl: HTMLDivElement | null;
  scrollHint: HTMLDivElement | null;
  textInput: HTMLTextAreaElement | null;
  sendBtn: HTMLButtonElement | null;
  voiceBtn: HTMLButtonElement | null;
  toastEl: HTMLDivElement | null;
  toastTimer: number | null;
  resetCamBtn: HTMLButtonElement | null;
  fullscreenBtn: HTMLButtonElement | null;
  chatToggle: HTMLButtonElement | null;
  dropHint: HTMLDivElement | null;
  modelLoading: HTMLDivElement | null;
  modelLoadingText: HTMLDivElement | null;
  /* 背景选择弹窗 */
  bgBtn: HTMLButtonElement | null;
  bgModal: HTMLDivElement | null;
  bgModalClose: HTMLButtonElement | null;
  bgListEl: HTMLDivElement | null;
  bgFileInput: HTMLInputElement | null;
  /* 移动模式 / 第一人称探索 */
  moveBtn: HTMLButtonElement | null;
  fpvBtn: HTMLButtonElement | null;
  fpvCrosshair: HTMLElement | null; // 运行时动态创建
  fpvJoystick: HTMLElement | null; // 运行时动态创建
  fpvJoystickThumb: HTMLElement | null; // 运行时动态创建
  fpvExitBtn: HTMLButtonElement | null;
  /* 角色卡片 */
  roleCardBtn: HTMLButtonElement | null;
  roleCardModal: HTMLDivElement | null;
  roleCardModalClose: HTMLButtonElement | null;
  roleCardList: HTMLDivElement | null;
  roleCardCreateBtn: HTMLButtonElement | null;
  roleCardEditModal: HTMLDivElement | null;
  roleCardEditClose: HTMLButtonElement | null;
  rcName: HTMLInputElement | null;
  rcRoleName: HTMLInputElement | null;
  rcUserName: HTMLInputElement | null;
  rcModelSelect: HTMLSelectElement | null;
  rcModelUploadBtn: HTMLButtonElement | null;
  rcModelFileInput: HTMLInputElement | null;
  // 模型供应商（全局资源）弹窗
  llmProviderBtn: HTMLButtonElement | null;
  providerModal: HTMLDivElement | null;
  providerModalClose: HTMLButtonElement | null;
  providerModalActive: HTMLSpanElement | null;
  providerList: HTMLDivElement | null;
  providerCreateBtn: HTMLButtonElement | null;
  providerEditModal: HTMLDivElement | null;
  providerEditClose: HTMLButtonElement | null;
  providerEditTitle: HTMLElement | null;
  providerName: HTMLInputElement | null;
  providerKind: HTMLSelectElement | null;
  providerBaseUrl: HTMLInputElement | null;
  providerApiKey: HTMLInputElement | null;
  providerDefaultModel: HTMLInputElement | null;
  providerVision: HTMLSelectElement | null;
  providerTestBtn: HTMLButtonElement | null;
  providerTestResult: HTMLSpanElement | null;
  providerModels: HTMLSelectElement | null;
  providerSaveBtn: HTMLButtonElement | null;
  providerDeleteBtn: HTMLButtonElement | null;
  // 云端 API 网络代理（LLM / TTS / STT / 画图）
  llmProxyMode: HTMLSelectElement | null;
  llmProxyUrl: HTMLInputElement | null;
  llmProxySaveBtn: HTMLButtonElement | null;
  llmProxyStatus: HTMLDivElement | null;
  // 角色卡片 TTS：API 供应商（应用配置全部在卡片）
  rcTtsApiPanel: HTMLDivElement | null;
  rcTtsApiUrl: HTMLInputElement | null;
  rcTtsApiKey: HTMLInputElement | null;
  rcTtsApiModel: HTMLInputElement | null;
  rcTtsApiVoice: HTMLInputElement | null;
  // 角色卡片内：供应商 + 模型
  rcLlmProviderSelect: HTMLSelectElement | null;
  rcLlmManageBtn: HTMLButtonElement | null;
  rcLlmModel: HTMLSelectElement | null;
  rcLlmRefreshBtn: HTMLButtonElement | null;
  rcLlmTip: HTMLDivElement | null;
  rcLlmTemperature: HTMLInputElement | null;
  rcLlmTempVal: HTMLSpanElement | null;
  rcLlmVision: HTMLSelectElement | null;
  rcLlmVisionTip: HTMLDivElement | null;
  rcTtsTabs: HTMLDivElement | null;
  rcTtsEdgePanel: HTMLDivElement | null;
  rcTtsGsoPanel: HTMLDivElement | null;
  rcVoiceSelect: HTMLSelectElement | null;
  rcRateRange: HTMLInputElement | null;
  rcRateVal: HTMLLabelElement | null;
  rcGsoUrl: HTMLInputElement | null;
  rcGsoRef: HTMLInputElement | null;
  rcGsoChar: HTMLInputElement | null;
  /* STT 独立设置 */
  rcSttTabs: HTMLDivElement | null;
  rcSttCloudPanel: HTMLDivElement | null;
  rcSttApiUrl: HTMLInputElement | null;
  rcSttApiKey: HTMLInputElement | null;
  rcSttModel: HTMLInputElement | null;
  rcSttProvider: string;
  rcSttSaveBtn: HTMLButtonElement | null;
  rcSttTip: HTMLSpanElement | null;
  rcSystemPrompt: HTMLTextAreaElement | null;
  rcApplyBtn: HTMLButtonElement | null;
  rcDeleteBtn: HTMLButtonElement | null;
  rcToolsEnabled: HTMLInputElement | null;
  rcToolsField: HTMLDivElement | null;
  rcToolsList: HTMLDivElement | null;
  rcAnimEnabled: HTMLInputElement | null;
  rcAnimField: HTMLDivElement | null;
  rcAnimList: HTMLDivElement | null;
  /* 相机设置弹窗 */
  camSettingsBtn: HTMLButtonElement | null;
  camSettingsModal: HTMLDivElement | null;
  camSettingsModalClose: HTMLButtonElement | null;
  camHeightRange: HTMLInputElement | null;
  camHeightVal: HTMLLabelElement | null;
  camDistanceRange: HTMLInputElement | null;
  camDistanceVal: HTMLLabelElement | null;
  camTiltRange: HTMLInputElement | null;
  camTiltVal: HTMLLabelElement | null;
  camSettingsSaveBtn: HTMLButtonElement | null;
  /* 在线音乐 */
  musicBtn: HTMLButtonElement | null;
  musicModal: HTMLDivElement | null;
  musicModalClose: HTMLButtonElement | null;
  musicTabSearch: HTMLButtonElement | null;
  musicTabPlaylists: HTMLButtonElement | null;
  musicTabBoards: HTMLButtonElement | null;
  musicPaneSearch: HTMLDivElement | null;
  musicPanePlaylists: HTMLDivElement | null;
  musicPaneBoards: HTMLDivElement | null;
  musicBoardsEl: HTMLDivElement | null;
  musicSearchInput: HTMLInputElement | null;
  musicSearchBtn: HTMLButtonElement | null;
  musicSearchResults: HTMLDivElement | null;
  musicPlaylistName: HTMLInputElement | null;
  musicPlaylistCreate: HTMLButtonElement | null;
  musicPlaylistsEl: HTMLDivElement | null;
  /* 音乐「正在播放」控制条元素（01_start 绑定） */
  musicNowPlaying: HTMLDivElement | null;
  musicNpTitle: HTMLSpanElement | null;
  musicNpState: HTMLSpanElement | null;
  musicNpToggle: HTMLButtonElement | null;
  musicNpStop: HTMLButtonElement | null;
  musicNpVol: HTMLInputElement | null;
  musicNpVolLabel: HTMLSpanElement | null;
  musicNpTrack: HTMLDivElement | null;
  musicNpFill: HTMLDivElement | null;
  musicNpKnob: HTMLDivElement | null;
  musicNpTime: HTMLSpanElement | null;
  /* 在线音乐方法（28_music_ui 挂载） */
  _musicSearchDone: boolean;
  openMusicModal: () => void;
  closeMusicModal: () => void;
  switchMusicTab: (tab: 'search' | 'playlists' | 'boards') => void;
  musicSearch: () => Promise<void>;
  renderMusicSearchResults: (songs: MusicSong[]) => void;
  playMusicSong: (song: MusicSong) => Promise<void>;
  createMusicPlaylist: () => Promise<void>;
  refreshMusicPlaylists: () => Promise<void>;
  renderMusicPlaylists: (list: any[]) => Promise<void>;
  addSongToPlaylistUI: (song: MusicSong) => Promise<void>;
  removeSongFromPlaylist: (pid: string, songId: string) => Promise<void>;
  playPlaylistDetail: (pl: any) => void;
  _musicBoards?: any[] | null;
  loadMusicBoards: () => Promise<void>;
  renderMusicBoards: (boards: any[]) => void;
  loadMusicBoardSongs: (board: any) => Promise<void>;

  /* 在线视频元素（01_start 绑定） */
  videoBtn: HTMLButtonElement | null;
  videoModal: HTMLDivElement | null;
  videoModalClose: HTMLButtonElement | null;
  videoSearchInput: HTMLInputElement | null;
  videoSearchBtn: HTMLButtonElement | null;
  videoSearchResults: HTMLDivElement | null;
  videoPlatformChips: HTMLDivElement | null;
  /* 视频收藏页签元素（01_start 绑定） */
  videoTabSearch: HTMLButtonElement | null;
  videoTabFavorites: HTMLButtonElement | null;
  videoPaneSearch: HTMLDivElement | null;
  videoPaneFavorites: HTMLDivElement | null;
  videoFavCategoryInput: HTMLInputElement | null;
  videoFavCategoryCreate: HTMLButtonElement | null;
  videoFavCategories: HTMLDivElement | null;
  videoFavList: HTMLDivElement | null;
  /* 观看历史页签元素（01_start 绑定） */
  videoTabHistory: HTMLButtonElement | null;
  videoPaneHistory: HTMLDivElement | null;
  videoHistoryList: HTMLDivElement | null;
  videoHistoryClear: HTMLButtonElement | null;
  /* 视频源页签元素（01_start 绑定） */
  videoTabSources: HTMLButtonElement | null;
  videoPaneSources: HTMLDivElement | null;
  videoSrcBuiltin: HTMLDivElement | null;
  videoSrcCustom: HTMLDivElement | null;
  videoSrcName: HTMLInputElement | null;
  videoSrcUrl: HTMLInputElement | null;
  videoSrcAdd: HTMLButtonElement | null;
  /* 连播队列元素（01_start 绑定） */
  videoQueuePanel: HTMLDivElement | null;
  videoQueueList: HTMLDivElement | null;
  videoQueueClear: HTMLButtonElement | null;
  /* 在线视频方法（29_video_ui 挂载） */
  openVideoModal: () => void;
  closeVideoModal: () => void;
  videoSearch: () => Promise<void>;
  renderVideoSearchResults: (videos: VideoItem[]) => void;
  playVideoItem: (v: VideoItem, opts?: { auto?: boolean }) => Promise<boolean>;
  /* 队列为空时按最近搜索结果顺序取下一部（大屏自动连播兜底，29_video_ui 挂载） */
  videoNextFromSearch: (endedUrl?: string) => VideoItem | null;
  switchVideoTab: (tab: 'search' | 'favorites' | 'history' | 'sources') => void;
  refreshVideoFavorites: () => Promise<void>;
  renderVideoFavorites: (data: any) => void;
  /* 观看历史方法（29_video_ui 挂载） */
  refreshVideoHistory: () => Promise<void>;
  renderVideoHistory: (data: any) => void;
  recordVideoHistory: (v: VideoItem) => Promise<void>;
  clearVideoHistory: () => Promise<void>;
  /* 视频源管理方法（29_video_ui 挂载） */
  refreshVideoSources: () => Promise<void>;
  renderVideoSources: (data: any) => void;
  toggleVideoSource: (id: string, enabled: boolean) => Promise<void>;
  addVideoSource: () => Promise<void>;
  removeVideoSource: (id: string) => Promise<void>;
  createVideoCategory: () => Promise<void>;
  renameVideoCategory: (cid: string) => Promise<void>;
  deleteVideoCategory: (cid: string) => Promise<void>;
  addVideoFavorite: (v: VideoItem, categoryId?: string | null) => Promise<string | null>;
  removeVideoFavorite: (fid: string) => Promise<void>;
  moveVideoFavorite: (fid: string, ev?: Event) => Promise<void>;
  isVideoFavorited: (webpageUrl: string) => string | null;
  _videoFavCache: Map<string, string>;
  _videoFavFilter: string | null;
  /* 连播队列方法（29_video_ui 挂载） */
  videoQueueSync: () => Promise<void>;
  renderVideoQueue: (queue: any[]) => void;
  videoQueueAdd: (v: VideoItem) => Promise<void>;
  videoQueueRemove: (i: number) => Promise<void>;
  videoQueueClearAll: () => Promise<void>;

  /* 工作区元素（01_start 绑定） */
  workspaceBtn: HTMLButtonElement | null;
  workspaceModal: HTMLDivElement | null;
  workspaceModalClose: HTMLButtonElement | null;
  workspacePathInput: HTMLInputElement | null;
  workspaceBrowseBtn: HTMLButtonElement | null;
  workspaceRoots: HTMLDivElement | null;
  workspaceCurrentPath: HTMLDivElement | null;
  workspaceSaveBtn: HTMLButtonElement | null;
  workspaceBrowsePath: HTMLDivElement | null;
  workspaceUpBtn: HTMLButtonElement | null;
  workspaceSavedList: HTMLDivElement | null;
  /* 工作区方法（32_workspace_ui 挂载） */
  openWorkspaceModal: () => void;
  closeWorkspaceModal: () => void;
  refreshWorkspace: () => Promise<void>;
  loadWorkspaceDirs: () => Promise<void>;
  workspaceGoUp: () => void;
  saveWorkspace: () => Promise<void>;
  loadSavedWorkspaces: () => Promise<void>;
  activateSavedWorkspace: (path: string) => Promise<void>;
  saveWorkspaceToSaved: () => Promise<void>;

  /* ---------- 状态机 ---------- */
  State: AppKernelState;
  currentState: AppKernelState[keyof AppKernelState];
  setState: (state: AppKernelState[keyof AppKernelState]) => void;

  /* ---------- 录音 / 音频基础（01_start 初始化） ---------- */
  isRecording: boolean;
  mediaRecorder: MediaRecorder | null;
  audioChunks: Blob[];
  currentAudio: HTMLAudioElement | null;
  audioCtx: AudioContext | null;
  analyser: AnalyserNode | null;
  analyserData: Uint8Array<ArrayBuffer> | null;
  isPlayingQueue: boolean;
  _pendingFullText: string | null;
  pendingAIMsgEl: HTMLElement | null;

  /* ---------- VAD 自动对话（01_start 初始化的常量与状态） ---------- */
  vadAnalyser: AnalyserNode | null;
  vadData: Uint8Array<ArrayBuffer> | null;
  vadRAF: number | null;
  vadSilenceStart: number;
  vadInterruptStart: number;
  vadVoiceStart: number;
  vadRecorder: MediaRecorder | null;
  vadChunks: Blob[];
  _vadClonedTrack: MediaStreamTrack | null;
  VAD_THRESHOLD: number;
  VAD_INTERRUPT_THRESHOLD: number;
  VAD_SILENCE_MS: number;
  VAD_INTERRUPT_MS: number;
  VAD_MIN_RECORD_MS: number;
  VAD_VOICE_ENABLED: boolean;
  VAD_VOICE_SCORE_THRESHOLD: number;
  VAD_VOICE_CONFIRM_MS: number;
  VAD_VOICE_MIN_F0: number;
  VAD_VOICE_MAX_F0: number;
  VAD_HARMONIC_BINS: number;

  /* ---------- 锁屏 / RL 系统开关 ---------- */
  LOCK_KEY: string;
  engagementRLActive: boolean;
  datingSystemActive: boolean;

  /* ---------- Three.js 场景 ---------- */
  scene: ThreeNS.Scene | null;
  camera: ThreeNS.PerspectiveCamera | null;
  renderer: ThreeNS.WebGLRenderer | null;
  modelGroup: ThreeNS.Group | null;
  currentAvatar: any;
  backgroundGroup: ThreeNS.Group | null;
  DEFAULT_CAM_POS: ThreeNS.Vector3 | null;
  targetCamPos: ThreeNS.Vector3 | null;
  MIN_ZOOM: number;
  MAX_ZOOM: number;
  camZoom: number;
  /* ---------- 隐私护栏（非管理员相机限制，02_three_scene 初始化） ---------- */
  IS_ADMIN: boolean;
  USER_ORBIT_PITCH_LIMIT: number;
  USER_MIN_CAM_DISTANCE: number;
  USER_MIN_CAM_HORIZ: number;
  USER_MIN_CAM_HEIGHT: number;
  USER_MIN_VIEW_HEIGHT: number;
  USER_FPV_MIN_HEIGHT: number;
  USER_FPV_MIN_DISTANCE: number;
  userMinZoom: () => number;
  userMinCamDistance: () => number;
  userMinCamHeight: () => number;
  clampCameraForUser: (p: ThreeNS.Vector3) => void;
  camOffsetX: number;
  camOffsetY: number;
  camOffsetZ: number;
  cameraHeight: number;
  cameraTiltDeg: number;
  cameraDistance: number;
  xrMode: 'off' | 'webxr';
  moveMode: boolean;
  backgroundAutoRotate: boolean;
  setMoveMode: (on: boolean) => void;

  /* ---------- 移动模式 / 选中交互（05_move_mode 挂载；raycaster 等由 02 初始化） ---------- */
  raycaster: ThreeNS.Raycaster;
  pointerNdc: ThreeNS.Vector2;
  dragPlane: ThreeNS.Plane;
  dragHitPoint: ThreeNS.Vector3;
  dragOffsetX: number;
  dragOffsetZ: number;
  proceduralChar: ThreeNS.Object3D | null; // 已弃用
  selectedTarget: ThreeNS.Object3D | null;
  selectionHelper: ThreeNS.BoxHelper | null;
  MIN_TARGET_SCALE: number;
  MAX_TARGET_SCALE: number;
  scaleSelectedTarget: (factor: number) => void;
  getSelectableTargets: () => ThreeNS.Object3D[];
  findTopLevelSelected: (obj: ThreeNS.Object3D | null) => ThreeNS.Object3D | null;
  selectTarget: (obj: ThreeNS.Object3D | null) => void;
  clearSelection: () => void;
  updatePointerNdc: (e: { clientX: number; clientY: number }) => void;
  onMovePointerDown: (e: PointerEvent) => void;
  onMovePointerMove: (e: PointerEvent) => void;
  sendAIAction: (message: string, userDriven?: boolean) => void;

  /* ---------- 性能分级 / 自适应渲染（08_state_switch 挂载） ---------- */
  perfTier: PerfTier;
  _renderFrameSkip: number;
  _renderFrameCount: number;
  _vadFrameSkip: number;
  _vadFrameCount: number;
  detectPerfTier: () => void;
  setPerfTier: (tier: PerfTier) => void;
  cyclePerfTier: () => void;
  shouldRenderFrame: () => boolean;
  /** 打字轻载（08_state_switch）：输入框聚焦时把帧率压到 20fps，不停帧 */
  _typingLite: boolean;
  setTypingLite: (on: boolean) => void;
  shouldVADFrame: () => boolean;
  _adaptiveDPR: boolean;
  _fpsAccum: number;
  _fpsCount: number;
  _lastFpsCheck: number;
  _dprAdjustAt: number;
  adaptiveFrame: (dt: number) => void;
  resetAdaptiveDPR: () => void;
  _targetDPR: number;
  _useAA: boolean;
  _starCount: number;
  _nativeDPR: () => number;
  _baseFrameSkip: number;
  _envLevel: number;
  _applyEnvLevel: () => void;
  starField: ThreeNS.Points | null;
  memoryTick: () => void;
  prepareForGame: () => void;
  prepareForLobby: () => void;
  onResize: () => void;
  smoothMouth: number;

  /* ---------- 锁屏 / 沉浸模式（08_state_switch 挂载） ---------- */
  enterLockMode: () => void;
  exitLockMode: () => void;
  immerseMode: boolean;
  _immersePressTimer: number | null;
  toggleImmerseMode: () => void;
  initImmerseLongPress: () => void;
  vrShake: { leftRight: number; upDown: number } | null;
  /* ---------- 08 消费、他模块挂载 ---------- */
  fpvMode: boolean;
  exitFPV: () => void;
  exitXrMode: () => void;
  _flushPendingAIActions: () => void;

  /* ---------- 第一人称探索（06_fpv_mode 挂载；状态常量由 02_three_scene 初始化） ---------- */
  FPV_HEIGHT: number;
  FPV_MOVE_SPEED: number;
  FPV_LOOK_SENSITIVITY: number;
  FPV_PITCH_LIMIT: number;
  fpvPos: ThreeNS.Vector3;
  fpvYaw: number;
  fpvPitch: number;
  fpvSavedAutoRotate: boolean;
  fpvKeys: Record<string, boolean>;
  fpvMoveVec: { x: number; y: number };
  fpvMovePointerId: number | null;
  fpvLookPointerId: number | null;
  fpvLookLastX: number;
  fpvLookLastY: number;
  fpvMoveOrigin: { x: number; y: number };
  fpvJustExited: boolean;
  dragOrbitYaw: number;
  dragOrbitPitch: number;
  toggleFPV: () => void;
  showFloatingJoystick: (x: number, y: number) => void;
  updateFloatingJoystick: (dx: number, dy: number) => void;
  hideFloatingJoystick: () => void;
  onFPVKeyDown: (e: KeyboardEvent) => void;
  onFPVKeyUp: (e: KeyboardEvent) => void;
  updateFPVCamera: (dt: number) => void;

  /* ---------- 背景场景加载（04_bg_load 挂载） ---------- */
  BG_TARGET_SIZE: number;
  gltfLoader: GLTFLoader | null;
  parts: { glow?: ThreeNS.Object3D; contactShadow?: ThreeNS.Object3D; [k: string]: any };
  applyBackground: (gltf: GLTF, url: string, name: string) => void;
  disposeBackground: () => void;
  showModelLoading: (text?: string) => void;
  hideModelLoading: () => void;
  _isBooting: boolean;
  refreshBgListSelection: (activeName: string | null) => void;

  /* ---------- 模型加载（03_model_load_gltf_vrm 挂载） ---------- */
  vrm: any;
  modelType: 'vrm' | 'gltf' | null;
  vrmBones: Record<string, any>;
  headBone: any;
  morphTargets: { mesh: any; index: number; name: string }[];
  _modelGroupBaseY: number;
  applyLoadedModel: (gltf: GLTF, url: string, name?: string) => Promise<void>;
  disposeModel: () => void;

  /* ---------- 称呼设置（23_name_settings；DOM 已并入角色卡片，引用可能不存在） ---------- */
  nameBtn?: HTMLButtonElement | null;
  nameModal?: HTMLDivElement | null;
  nameModalClose?: HTMLButtonElement | null;
  nameSaveBtn?: HTMLButtonElement | null;
  userNameInput?: HTMLInputElement | null;
  initNameConfig: () => void;
  openNameModal: () => void;
  saveUserName: () => Promise<void>;

  /* ---------- 启动 / RL 系统编排（19_boot 挂载） ---------- */
  updateCameraSettingsUI: () => void;
  initThree: () => void;
  bindEvents: () => void;
  /* ---------- 事件绑定（17_events 挂载） ---------- */
  openCamSettingsModal: () => void;
  closeCamSettingsModal: () => void;
  updateVRShakeNotify: () => void;
  initRoleCards: () => void;
  lastUserActivityTime: number;
  roleCardActiveId: string | null;
  restoreActiveRoleCard: () => Promise<any>;
  smoothRotY: number;
  initEngagementRL: () => void;
  initExpressionRL: () => void;
  toggleExpressionRL: () => boolean;
  setGameModeExpressionRL: (inGame: boolean) => void;
  initDatingSystem: () => void;
  toggleDatingMode: () => boolean;

  /* ---------- 场景状态持久化 ---------- */
  SCENE_KEY: string;
  CAM_SETTINGS_KEY: string;
  _saveTimer: number | null;
  _camSettingsSaveTimer: number | null;
  saveSceneState: () => void;
  debouncedSaveScene: () => void;
  restoreSceneState: () => void;
  loadCameraSettings: () => void;
  saveCameraSettings: () => void;
  resetAvatarToOrigin: () => void;
  resetViewState: () => void;
  applySavedPositions: () => void;
  _bgCenterX: number;
  _bgCenterZ: number;
  _findFloorY: (x: number, z: number) => number;
  _smoothTeleport: {
    x0: number; y0: number; z0: number;
    x1: number; y1: number; z1: number;
    t: number; dur: number;
  } | null;

  /* ---------- 待机漫步 ---------- */
  idleWalkTarget: any;
  idleWalkProgress: number;
  walkPath: any[];
  walkSegmentIndex: number;
  currentAction: any;
  nextActionTimer: number;

  /* ---------- 表情动作引擎（08_expression_engine 挂载） ---------- */
  MOTION_PRIORITY: { idle: number; auto: number; rl: number; user: number };
  MOTION_MAX_RAD: number;
  MOTION_SPEED: number;
  MOTION_SMOOTH: number;
  MOTION_LIBRARY: Record<string, MotionDef>;
  IDLE_MICRO_POOL: string[];
  EMOTION_EXPR: Record<string, Record<string, number>>;
  EMOTION_MOUTH: Record<string, number>;
  motionOffsets: Record<string, BoneOffset> | null;
  motionQueue: { motion: string | MotionDef; hold?: number }[];
  _motionActive: boolean;
  _motionName: string;
  _motionDef: MotionDef | null;
  _motionElapsed: number;
  _motionPriority: number;
  _motionHoldLeft: number;
  _motionItemHold: number;
  _lastLiveOffsets: Record<string, BoneOffset> | null;
  _motionSmooth: Record<string, BoneOffset> | null;
  _motionCtx: Record<string, any>;
  _motionOnDone: (() => void) | null;
  _motionKeepExpr: boolean;
  _motionOffsetsPool: Record<string, BoneOffset>;
  _gazeTarget: { x: number; y: number; weight: number; until: number };
  _gazeCur: { x: number; y: number };
  _gazeSideSign: number;
  _eyeBones: { left: any; right: any } | null;
  emotionOverlay: {
    emotion: string;
    until: number;
    fadeMs: number;
    targets: Record<string, number>;
    mouth: number;
  } | null;
  emotionMouth: number;
  _blinkSuppressUntil: number;
  _idleMicroTimer: number;
  _idleMicroInterval: number;
  setGaze: (target: string, weight?: number, duration?: number) => void;
  clearGaze: () => void;
  getGazeOffsets: () => { x: number; y: number };
  suppressBlink: (seconds?: number) => void;
  blinkSuppressed: () => boolean;
  setEmotionOverlay: (emotion: string, intensity?: number, duration?: number) => void;
  clearEmotionOverlay: () => void;
  getEmotionOverlayTargets: () => Record<string, number> | null;
  emotionOverlayActive: () => boolean;
  _ensureEyeBones: () => { left: any; right: any } | null;
  playMotion: (name: string, opts?: any) => void;
  playMotionSequence: (seq: any[], opts?: any) => void;
  interruptMotions: () => void;
  _startNextMotion: () => void;
  _pickIdleMicro: () => string;
  updateMotionSystem: (dt: number) => void;
  motionSystemActive: () => boolean;
  motionName: () => string;

  /* ---------- WebSocket 连接 ---------- */
  ws: WebSocket | null;
  wsHeartbeat: number | null;
  wsReconnectTimer: number | null;
  wsConnTimeout: number | null;
  connectWS: () => void;
  handleWSMessage: (msg: ServerMessage) => void;
  sendText: (text: string, attachments?: any[]) => void;
  sendAudioBase64: (b64: string, mimeType?: string, wakeCheck?: boolean) => void;

  /* ---------- RL 统一调度 ---------- */
  rlHeartbeat: number | null;
  _rlLastDispatchTime: number;
  _rlStatusEl: HTMLElement | null;
  sendRLSync: (wantDecision?: boolean) => void;
  startRLHeartbeat: () => void;
  _engagementRL: any;
  _datingSystem: any;
  _expressionRL: any;
  aiAutonomyController: any;
  aiAutonomy?: any;

  /* ---------- 回复 / 音频队列（10_tts_lipsync 挂载） ---------- */
  currentReplySession: string | null;
  _interruptedSession: string | null;
  currentReplyText: string;
  currentReplySeg: string;
  _streamTextOn: boolean;
  audioQueue: AudioQueueItem[];
  currentAudioSource: MediaElementAudioSourceNode | null;
  ensureAudioCtx: () => void;
  playNextAudio: () => void;
  handleAudioChunk: (msg: AudioChunkMessage) => void;
  handleStreamText: (text: string) => void;
  handleRetractText: (length: number) => void;
  mdToHtml?: (src: string) => string;
  handleAudioEnd: (msg: AudioChunkMessage) => void;
  handleInterrupted: (msg?: InterruptedMessage) => void;
  clearAudioQueue: () => void;
  vadResumeAfterSpeak: () => void;
  /** 无音频分片的连续跳过层数（>32 时让出事件循环，防深递归栈溢出） */
  _ttsSkipDepth?: number;

  /* ---------- 聊天 UI（13_messages 挂载） ---------- */
  addSystemMsg: (text: string) => void;
  addUserMsg: (text: string, isVoice?: boolean, attachments?: any[]) => void;
  addAIMsg: (text: string, isVoice?: boolean) => void;
  showToast: (msg: string) => void;
  showSubtitle: (text: string) => void;
  showTyping: () => void;
  removeTyping: () => void;
  /* 回合气泡：正文分段 + 内联工具块（推理只作底部「思考中」实时指示，不进正文） */
  _turnMsgEl: HTMLElement | null;
  beginTurnBubble: (sessionId?: string | null) => HTMLElement | null;
  ensureTurnSeg: () => HTMLElement | null;
  sealTurnSeg: () => void;
  appendTurnThinking: (text: string) => void;
  handleReasoning: (text: string, sessionId?: string | null) => void;
  clearReasoningLine: () => void;
  /* 回合状态行 + 刷新接管：网络波动重连提示 & 后台对话轮进度快照重建 */
  setTurnStatus: (text: string) => void;
  clearTurnStatus: () => void;
  takeoverTurnInProgress: (msg: any) => void;
  /* 长时间无响应看门狗：工具/思考期间无实时事件时的卡死提示 + 一键中断 */
  _lastTurnActivity: number;
  _toolRunningSince: number;
  /* 本轮已进入工具执行（tool_call_start 置真，回合结束/被取消置假）：
     语音在此状态下不打断本轮，只排队 */
  _turnInTools: boolean;
  noteTurnActivity: () => void;
  clearStuckHint: () => void;
  maybeWarnStuck: () => void;
  finishTurn: (interrupted?: boolean) => void;
  turnTextContainer: () => HTMLElement | null;
  setTurnStreamText: (text: string) => void;
  renderTurnText: (text: string) => void;
  handleUsageMessage: (msg: UsageMessage) => void;
  scrollToBottom: (force?: boolean) => void;
  _trimMessages: () => void;
  notifyFullscreenChat: () => void;
  isFullscreen: boolean;
  extractMediaUrls: (text: string) => string[];
  renderMsgMedia: (el: HTMLElement | null, text: string, asUser?: boolean) => void;
  openMediaViewer: (url: string) => void;
  isNearBottom: () => boolean;
  _newMsgCount: number;
  bumpNewMsg: (el?: HTMLElement | null) => void;
  updateScrollHint: () => void;
  _forceScrolling: boolean;
  _forceScrollTimer: number | null;
  ensureCopyBtn: (el: Node) => void;
  updateChatHeadCount: () => void;
  fmtTokens: (n: number) => string;
  _tokenStats: TokenStats;
  _lastUsage: UsageMessage | null;
  attachMsgTokenBadge: (el: HTMLElement) => void;
  updateTokenMeter: () => void;
  chatFullscreen: boolean;
  chatHeightLevel: number;
  setChatFullscreen: (on: boolean) => void;
  /* 全屏透明（◍）：同样铺满屏幕，但面板半透明且**不静默** —— 3D 场景继续渲染，
   * 角色透过面板看得见。与 chatFullscreen 互斥。 */
  chatGhost: boolean;
  setChatGhost: (on: boolean) => void;
  cycleChatHeight: () => void;
  closeChatPanel: () => void;
  /* 聊天全屏静默总闸（00_quiet 挂载）：全屏时停掉非聊天框的一切渲染 */
  chatQuiet: boolean;
  setQuiet: (on: boolean) => void;
  onQuiet: (fn: (on: boolean) => void) => void;
  syncQuietLoop: () => void;
  quietSnapshot: () => { quiet: boolean; loopStopped: boolean; hooks: number };

  /* ---------- 语音 / VAD（11_voice_record + 12_vad_auto 挂载） ---------- */
  voiceMode: VoiceMode;
  micStream: MediaStream | null;
  pickRecorderMime: () => string;
  MIC_CONSTRAINTS: MicConstraints;
  _acquireMicStream: () => Promise<{ stream: MediaStream; fresh: boolean }>;
  startRecording: () => Promise<void>;
  stopRecording: (cancel?: boolean) => void;
  vadStream: MediaStream | null;
  vadState: 'idle' | 'recording';
  vadLoop: () => void;
  startVADMode: () => Promise<boolean>;
  stopVADMode: () => void;
  vadIsVoice: () => number;
  vadIsHumanVoice: () => boolean;
  vadResetVoiceEma: () => void;
  vadGetConfirmMs: (vol: number) => number;
  /* ---------- 神经 VAD（12b_silero_vad 挂载） ---------- */
  sileroVadInit: () => void;
  sileroVadReady: () => boolean;
  sileroVadProb: () => number;
  sileroVadIsVoice: () => boolean | null;
  sileroVadPush: (samples: Float32Array, rate: number) => void;
  sileroVadReset: () => void;
  vadGetSilenceMs: () => number;
  vadGetVolume: () => number;
  startVADRecording: () => void;
  stopVADRecording: () => void;
  _micStreamReleaseTimer: number | null;
  triggerInterrupt: (force?: boolean) => void;
  setVoiceMode: (mode: VoiceMode) => void;
  lockMode: boolean;
  toggleLockMode: () => void;

  /* ---------- 背景管理 UI（16_bg_ui 挂载） ---------- */
  openBgModal: () => void;
  closeBgModal: () => void;
  refreshBackgroundList: () => Promise<void>;
  renderBackgroundList: (items: BackgroundInfo[]) => void;
  uploadBackgroundFile: (file: File) => Promise<void>;

  /* ---------- 工作流工具链卡片 ---------- */
  toolChainBeginTurn?: () => void;
  /** toolDesc：工具自己的说明（后端随 tool_call_start 推送的 function.description），
   *  供中央字幕显示。前端**不**维护「工具名 → 人话」映射表：工具是动态扩展的，
   *  映射表必然过期，新工具会落进兜底分支、变得不伦不类 */
  toolChainStart?: (toolName: string, args: any, toolDesc?: string) => void;
  toolChainResult?: (toolName: string, result: any, success: boolean) => void;
  /** 工具执行心跳。⚠ elapsed 的单位是【秒】且为整数（后端 int() 截断过），
   *  前端消费时必须 ×1000 转毫秒——曾因直接当毫秒用，秒表每 5 秒被打回 0.0s */
  toolChainProgress?: (toolName: string, elapsed: number, message?: string) => void;
  codexLinkTask?: (toolName: string, taskId: string) => void;
  toolChainAbort?: () => void;
  toolChainEndTurn?: () => void;
  toolChainReset?: () => void;
  addToolCallMsg: (toolName: string, args: any, status?: any) => void;
  addToolCallResult: (toolName: string, result: any, success: boolean) => void;

  /* ---------- 游戏化反馈层（34_game_fx 挂载） ---------- */
  /** 音效开关（HUD 上的小喇叭） */
  gameMuted?: boolean;
  /** 工具调用的游戏化钩子：评级 / 连击 / 暴击 / 经验 / 成就 / 自动折叠 */
  game?: {
    onToolStart: (el: HTMLElement) => void;
    onToolResult: (el: HTMLElement, success: boolean, costMs: number) => void;
    onRoundStart: () => void;
    onRoundEnd: () => void;
    snapshot: () => Record<string, any>;
    reset: () => void;
  };

  /* ---------- 街机氛围层（35_arcade_fx 挂载） ---------- */
  arcade?: {
    /** 氛围层根节点（#arcade-fx） */
    root: HTMLElement;
    /** 从屏幕中心扩散一圈冲击波（暴击 / 升级时由 34_game_fx 调用） */
    pulse: (kind?: 'crit' | 'levelup' | 'tool') => void;
    /** 手动刷新状态色（不依赖 MutationObserver 时用） */
    refresh: () => void;
  };

  /* ---------- 底部栏高度同步（36_layout_sync 挂载） ---------- */
  /** 重新测量输入栏高度并写入 --controls-h（一般由 ResizeObserver 自动调用） */
  syncBottomBar?: () => void;

  /* ---------- 流式节奏引擎（37_stream_pulse 挂载） ---------- */
  /** 把 AI 生成过程拆成「速率 / 节拍 / 呼吸」三个可感知量，写进 --sp-* 变量 */
  stream?: {
    /** 喂入一段增量文本（13_messages 的 setTurnStreamText 调用） */
    feed: (text: string) => void;
    /** 新一轮输出开始 */
    begin: () => void;
    /** 输出结束，清空节奏变量 */
    end: () => void;
    /** 当前速率（字符/秒） */
    readonly rate: number;
    /** 本轮累计节拍数 */
    readonly beats: number;
    /** 手动触发一次节拍脉冲 */
    pulse: () => void;
  };

  /* ---------- 直播舱（38_live_room 挂载） ---------- */
  /** 可变奖励掉落 / 算力热度 / 弹幕 */
  live?: {
    /** 手动掷一次掉落（调试 / 单测用），返回稀有度或 null */
    roll: () => 'rare' | 'epic' | 'legend' | null;
    /** 手动掉落一次（调试 / 单测用）：确定性造出礼物，才能验证堆叠逻辑 */
    drop: (rarity: 'rare' | 'epic' | 'legend') => void;
    /** 手动加热度 */
    heat: (n: number) => void;
    /** 快照：当前热度、历史最高、礼物数、保底计数 */
    snapshot: () => {
      heat: number;
      best: number;
      gifts: number;
      legends: number;
      pity: { sinceDrop: number; sinceEpic: number; sinceLegend: number };
    };
    /** 清空本场与历史数据 */
    reset: () => void;
  };

  /* ---------- 施法态（39_cast_fx 挂载） ---------- */
  /** 工具执行期间的高能态：中央大字幕 / 法阵 / 光柱 / 秒表 / 档位 / 连发 */
  cast?: {
    /** 手动进入施法（调试 / 单测用）。toolDesc = 工具自己的说明，缺省则显示工具名 */
    begin: (toolName: string, toolDesc?: string) => void;
    /** 手动结束施法 */
    end: (ok: boolean) => void;
    /** 当前是否有工具在跑 */
    readonly active: number;
    /** 已用时长 ms */
    readonly elapsed: number;
    /** 当前档位 0=施法 1=深度施法 2=超载 */
    readonly tier: number;
    /** 当前连发数（1 = 没连上）。短期多次调用工具时递增 */
    readonly combo: number;
    /** 工具自己的说明 → 屏幕显示的一行字（暴露给测试，避免测试抄一份规则） */
    describe: (toolDesc: string, toolName?: string) => string;
    /** 强制归零（调试 / 自动化测试用） */
    reset: () => void;
  };

  /* ---------- 本轮对话时钟（40_turn_clock 挂载） ---------- */
  /** 「一轮对话用了多久」的唯一真相来源：开始记一个单调时钟，结束减一下。
   *  此前热度 HUD 从页面加载算、施法秒表从单个工具算，都不是「本轮」。 */
  turnClock?: {
    /** 新一轮开始（无条件重置） */
    start: () => void;
    /** 本轮结束（重复调用不覆盖最终值） */
    stop: () => void;
    /** 当前已耗时 ms（运行中实时算，结束后定格） */
    readonly elapsed: number;
    /** 是否正在计时 */
    readonly running: boolean;
    /** 第几轮 */
    readonly turn: number;
    /** 上一轮最终耗时 ms */
    readonly last: number;
    /** 格式化：<60s 走 0.1 秒精度，>=60s 走 m:ss */
    fmt: (ms?: number) => string;
    /** 状态变化回调：开始/结束时立刻触发，供 HUD 即时刷新（不必等 tick） */
    onChange: ((running: boolean) => void) | null;
    /** 快照（调试 / 单测用） */
    snapshot: () => { running: boolean; elapsed: number; last: number; turn: number };
  };

  /* ---------- 全息舞台（41_holo_stage 挂载） ----------
   * 空间感（全息投影 + 舞台灯光）+ 成果确认（分级欢呼）。
   * 核心约定：cheerLevel 决定「给多少」，而「给不给」永远是给 ——
   * 任何成功至少 1 级反馈，失败也有一次鼓励，不存在「什么都没发生」。 */
  holo?: {
    /** 全息投影层根节点（#holo-fx） */
    root: HTMLElement;
    /** 舞台灯光层（#light-fx） */
    light: HTMLElement;
    /** 手动触发一次欢呼（level 1~5，<=0 不做事） */
    celebrate: (level: number, opts?: { combo?: number }) => void;
    /** 按输入自动分级并欢呼，返回实际级数（0 = 走了鼓励分支） */
    cheer: (input: {
      success: boolean;
      combo?: number;
      crit?: boolean;
      levelUp?: boolean;
      rarity?: 'rare' | 'epic' | 'legend' | null;
      grade?: 'S' | 'A' | 'B' | 'C';
    }) => number;
    /** 失败鼓励：不欢呼，但给一次「被看见」的呼吸 */
    encourage: () => void;
    /** 屏幕震动 1~3 */
    shake: (power?: number) => void;
    /** 频闪一次（最多 3 次闪烁，光敏安全） */
    strobe: () => void;
    /** 彩带（5 级庆祝） */
    confetti: () => void;
    /** 纯函数暴露：测试用它，避免测试自己抄一份分级规则 */
    level: (input: {
      success: boolean;
      combo?: number;
      crit?: boolean;
      levelUp?: boolean;
      rarity?: 'rare' | 'epic' | 'legend' | null;
      grade?: 'S' | 'A' | 'B' | 'C';
    }) => number;
    notes: (level: number, combo?: number) => number[];
    tiers: { en: string; cn: string }[];
    /** 累计欢呼次数 */
    readonly count: number;
    /** 是否处于低配降档（环境氛围减负，成果反馈不变；由掉帧看门狗自动决定） */
    readonly lite: boolean;
    /** 手动切换低配（调试 / 测试用） */
    setLite: (on: boolean) => void;
    /** 帧率快照：{ lite, 最近一窗统计, 采样窗数 } */
    perf: () => { lite: boolean; stats: { n: number; avg: number; p95: number; jankPct: number; spike: boolean }; windows: number };
    reset: () => void;
  };

  /* ---------- 会话管理 ---------- */
  sessionModalEl: HTMLElement | null;
  sessionListEl: HTMLElement | null;
  sessionBtn: HTMLElement | null;
  sessionModalClose: HTMLElement | null;
  newSessionBtn: HTMLElement | null;
  sessionSearchInput: HTMLInputElement | null;
  sessionArchiveToggle: HTMLElement | null;
  _sessionSearchTimer: number | undefined;
  _sessionShowArchived: boolean;
  initSessionUI: () => void;
  renderSessionList: (sessions?: SessionSummary[] | null) => void;
  renderSessionListFromState: () => void;
  requestSessionList: () => void;

  /* ---------- DSH 桥接 ---------- */
  harnessRequestId: string | null;
  harnessStatus?: string;
  _harnessPollTimer: number | null;
  _harnessPolling: boolean;
  showHarnessConfirm: (requestId: string, task?: string) => void;
  harnessPoll: () => void;
  updateHarnessStatus: (msg: BridgeStatusMessage) => void;
  harnessApprove: (approve: boolean) => void;
  harnessClose: () => void;
  onBridgeSay: (text: string) => void;
  dshCardExists: (requestId: string) => boolean;
  notifyTaskDeclined: () => void;

  /* ---------- 任务中心 ---------- */
  handleTaskEvent?: (event: TaskEvent) => void;
  taskBoardOnEvent?: (event: any) => void;
  dshCardOnEvent?: (event: TaskEvent) => void;
  addTaskTreeMsg: (data: TaskTreeData, opts?: any) => HTMLElement;
  maybeRenderTaskTree: (msg: any) => boolean;
  copyPlainText: (text: string) => Promise<void>;
  openTaskCenter: () => void;
  closeTaskCenter: () => void;
  toggleTaskCenter: () => void;
  selectTaskCenter: (taskId: string) => void;
  initTaskCenter: () => void;

  /* ---------- 屏幕控制 / 媒体 ---------- */
  handleScreenCommand: (msg: ScreenCommandMessage) => Promise<void>;
  fuzzyMatchFile: (name: string, endpoint: string) => Promise<string | null>;
  loadModelFromUrl: (url: string, name?: string) => Promise<any>;
  loadBackgroundFromUrl: (url: string, name?: string) => Promise<any>;
  useDefaultBackground: () => void;
  switchTTSEngine: (engine: TTSEngine) => void;
  setBGMVolume: (vol: number) => void;
  playMusicTrack: (url: string, name: string) => void;
  stopBGM: () => void;

  /* ---------- BGM 播放器（20_bgm_player 挂载） ---------- */
  playBGM: (url: string, name: string) => void;
  getCurrentBGM: () => string | null;
  isBGMPlaying: () => boolean;
  pauseBGM: () => void;
  resumeBGM: () => void;
  toggleBGM: () => void;
  seekBGM: (seconds: number) => void;
  getBGMState: () => BGMState;
  onBGMStateChange: (cb: (s: BGMState) => void) => void;
  onMusicTrackEnded?: () => void; // 28_music_ui 挂载

  /* ---------- 媒体子智能体看护（server 注入 worker_id，播完回报闭环） ---------- */
  _musicWorkerId?: string | null; // 当前播放音轨对应的看护子智能体
  _videoWorkerId?: string | null; // 当前大屏视频对应的看护子智能体

  /* ---------- 脚步音效（24_footstep_sfx 挂载） ---------- */
  _footstepSFXReady: boolean;
  _footstepUnlocked: boolean;
  footstepMuted: boolean;
  sfxVolume?: number; // 预留：全局 SFX 音量（当前无赋值点，默认 1）
  initFootstepSFX: () => boolean;
  unlockFootstepSFX: () => void;
  setFootstepMuted: (muted: boolean) => void;
  playFootstep: (vol?: number) => void;
  updateFootstepSFX: (phase: number, speedFactor?: number) => void;
  resetFootstepPhase: () => void;

  /* ---------- TTS 设置（18_tts_settings 挂载；tts DOM 引用全项目无赋值，可选访问） ---------- */
  ttsVoicesLoaded: boolean;
  ttsCharsLoaded: boolean;
  currentTTSEngine: TTSEngine;
  savedEdgeVoice: string;
  ttsBtn?: HTMLButtonElement | null;
  ttsModal?: HTMLDivElement | null;
  ttsModalClose?: HTMLButtonElement | null;
  ttsRateRange?: HTMLInputElement | null;
  ttsRateVal?: HTMLElement | null;
  ttsSaveBtn?: HTMLButtonElement | null;
  ttsVoiceSelect?: HTMLSelectElement | null;
  ttsEdgePanel?: HTMLDivElement | null;
  ttsGsoPanel?: HTMLDivElement | null;
  ttsGsoUrl?: HTMLInputElement | null;
  ttsGsoRef?: HTMLInputElement | null;
  ttsGsoChar?: HTMLInputElement | null;
  initTTSConfig: () => Promise<void>;
  applyTTSConfig: (cfg: TTSConfig) => void;
  openTTSModal: () => Promise<void>;
  saveTTSConfig: () => Promise<void>;

  /* ---------- 模型管理 UI（15_model_ui 挂载；modelModal/modelListEl 无赋值点，可选访问） ---------- */
  modelModal?: HTMLElement | null;
  modelListEl?: HTMLElement | null;
  openModelModal: () => void;
  closeModelModal: () => void;
  refreshModelList: () => Promise<void>;
  renderModelList: (models: ModelInfo[]) => void;
  refreshModelListSelection: (activeName: string | null) => void;
  escapeHtml: (s: string) => string;
  uploadModelFile: (file: File) => Promise<void>;
  playPlaylistCmd?: (args: ScreenCommandArgs) => void;
  videoBoardPlay?: (args: ScreenCommandArgs) => void;
  videoBoardControl?: (args: ScreenCommandArgs) => void;
  videoBoardGetState?: () => VideoBoardState | null;

  /* ---------- 用户活跃度 ---------- */
  _lastUserMessageTime: number;
  _lastUserInteractTime: number;
  _sentAvatarName?: string;
  _sentBgName?: string;

  /* ---------- VR HUD（vr-hud 挂载） ---------- */
  vrHud: VrHud;
  updateVrHud: (dt: number) => void;

  /* ---------- 情绪驱动动作系统（emotion_controller / motion_blender / mixamo_retarget 挂载） ---------- */
  pad: PADState;
  padTarget: PADState;
  emotionParams: EmotionParams | null;
  emotionSource: string;
  _emotionHoldUntil: number;
  _emotionFadeMs: number;
  EMOTION_PAD: Record<string, PADState>;
  EMOTION_MICRO_POOL: Record<string, string[]>;
  setEmotion: (emotion: string, intensity?: number, duration?: number, source?: string) => void;
  setPAD: (pleasure: number, arousal: number, dominance: number, duration?: number) => void;
  updateEmotionController: (dt: number) => void;
  getEmotionParams: () => EmotionParams;
  onReplyEmotion: (emotion: string) => void;
  detectReplyEmotion: (text: string) => string | null;
  _replyEmotionDone: boolean;
  updateEmotionBlender: (dt: number) => void;
  measureEmotionPose: (emotion: string) => Promise<any>;
  /* Mixamo 重定向 */
  mixamoClips: Record<string, MixamoClipInfo>;
  mixamoMixer: any;
  _mixamoActiveClip: string | null;
  _mixamoActiveAction: any;
  _mixamoActiveClipLoop: boolean;
  _mixamoActiveClipStart: number;
  _mixamoSwitchTimer: number | null;
  /* 自然化播放参数（09c 挂载）：起始/交叠/尾部收敛 全链路一致 */
  ANIM_PLAY_PARAMS?: { blend: number; start: number; tail: number; stopFade: number };
  /* 尾收回落（单次动作自然收尾,末姿态缓收至静息后再交还程序微动作） */
  _mixamoTailActive?: boolean;
  _mixamoTailName?: string | null;
  _mixamoTailRem?: number;
  _mixamoTailTotal?: number;
  loadMixamoAnimation: (fbxUrl: string, clipName?: string) => Promise<any>;
  loadBakedMixamoClip: (name: string, bakedUrl: string) => Promise<any>;
  playMixamoClip: (name: string, opts?: any) => void;
  stopMixamoClip: (fadeMs?: number) => void;
  updateMixamoMixer: (dt: number) => void;
  /* 原位播放治理：动作不搬动位置，位移仅由带显式速度的行走系统驱动 */
  _mixamoHipsRestPos: { x: number; y: number; z: number } | null;
  captureMixamoHipsRest: () => boolean;
  resetMixamoHips: () => void;
  /* Mixamo 动作库加载器 */
  _animLibraryConfig: AnimLibraryConfig | null;
  _animLibraryLoaded: boolean;
  _animLibraryLoading: boolean;
  _animLibraryStats: { total: number; loaded: number; failed: number };
  _animLibraryGen: number;
  loadAnimLibraryConfig: () => Promise<AnimLibraryConfig | null>;
  loadAnimationLibrary: (lazy?: boolean) => Promise<number>;
  resetAnimationLibrary: () => void;
  /* 动态加载 + LRU 缓存池（09d 挂载） */
  _animCacheMax: number;
  _animLRU: string[];
  _animLoading: Set<string>;
  _animPrefetchTimer: any;
  ensureClipLoaded: (name: string) => Promise<boolean>;
  touchLRU: (name: string) => void;
  destroyClip: (name: string) => void;
  _prefetchNext: () => void;
  /* 动作状态上报（说话时知道自己的当前动作） */
  _currentAnimState: { name: string; category: string; emotion: string } | null;
  _lastAnimAnnounce: number;
  updateAnimState: (name: string) => void;
  clearAnimState: () => void;
  _announceAnimState: () => void;
  playLibraryClip: (name: string, opts?: any) => boolean;
  playEmotionClip: (emotion: string, opts?: any) => string | null;
  playCategoryClip: (category: string, opts?: any) => string | null;
  getCategoryClips: (category: string) => string[];
  getEmotionClips: (emotion: string) => string[];
  getAnimLibraryStats: () => { total: number; loaded: number; failed: number; available: number };
  /* 角色专属动作过滤 */
  _roleAnimationConfig: { enabled: boolean; allowed: string[] } | null;
  setRoleAnimationConfig: (config: { enabled: boolean; allowed: string[] } | null | undefined) => void;
  isAnimAllowed: (name: string) => boolean;
  getAllowedClips: () => any[];
  pickAllowedLoopClip: (preferEmotion?: string) => string | null;
  pickAllowedClipByEmotion: (emotion: string) => string | null;
  /* 统一动作调度（情绪+场景分类 → 在盘动作随机） */
  _lastScheduledClip: string | null;
  LIBRARY_EMOTION_SCENES: Record<string, string[]>;
  LIBRARY_SCENE_HOLD: Record<string, number[]>;
  pickLibraryActionByScene: (scene: string, preferEmotion?: string, opts?: any) => string | null;
  tryStartLibraryAction: (scene?: string, opts?: any) => string | null;
  /* Mixamo 情绪桥接 */
  _mixamoEmotionEnabled: boolean;
  _mixamoEmotionMode: string;
  _mixamoLastEmotion: string;
  _mixamoEmotionCooldown: number;
  enableMixamoEmotion: (enabled: boolean) => void;
  setMixamoEmotionMode: (mode: string) => void;

  /* ============================================================
   * GENERATED —— 生成区（勿手改）：类型 any，待各模块迁移时精化
   * ============================================================ */
  /* ---- js/audio/11_voice_record.js ---- */

  /* ---- js/audio/18_tts_settings.js ---- */

  /* ---- js/audio/20_bgm_player.js ---- */

  /* ---- js/audio/24_footstep_sfx.js ---- */

  /* ---- js/audio/28_music_ui.js ---- */

  /* ---- js/character/04_bg_load.js ---- */

  /* ---- js/character/06_fpv_mode.js ---- */
  enterFPV?: any;

  /* ---- js/character/07_click_interact.js ---- */
  ARM_REST_Z?: any;
  EXPR_EXTERNAL_KEYS?: any;
  EXPR_MOUTH_BLOCK_KEYS?: any;
  _bubbleElapsedT?: any;
  _bubbleLastT?: any;
  _chatBubbleBaseY?: any;
  _chatBubbleH?: any;
  _chatBubbleOpacity?: any;
  _chatBubblePhase?: any;
  _chatBubblePop?: any;
  _chatBubbleShowT?: any;
  _chatBubbleVisible?: any;
  _chatBubbleW?: any;
  _createSpeechBubble?: any;
  _drawChatBubbleCanvas?: any;
  _ensureSingleSpeechBubble?: any;
  _headLookTmpVec?: any;
  _layoutSpeechBubbleSprite?: any;
  _measureBubbleLayout?: any;
  _rawLookTargetTmp?: any;
  _idleWalkRampT?: any;
  _lastGroundedLog?: any;
  _memoryTickAcc?: any;
  _mouthLogTimer?: any;
  _pendingAIActions?: any;
  _playerGroundY?: any;
  _playerIsGrounded?: any;
  _playerVelocityY?: any;
  _raycastGround?: any;
  _sendAIActionNow?: any;
  _speechBubbleCanvas?: any;
  _speechBubbleDrawCanvas?: any;
  _speechBubbleLayouts?: any;
  _speechBubblePending?: any;
  _speechBubbleSeq?: any;
  _speechBubbleSession?: any;
  _speechBubbleTex?: any;
  _speechBubbleText?: any;
  _speechTmpVec?: any;
  addBodyColliders?: any;
  animate?: any;
  animateModel?: any;
  applyClickWobble?: any;
  applyVrmRestPose?: any;
  autoLookTarget?: any;
  blinkDuration?: any;
  blinkPhase?: any;
  blinkTimer?: any;
  blinkType?: any;
  calmSpringBones?: any;
  checkProactiveTrigger?: any;
  computeBodyFaceCam?: any;
  computeExprTargetsByState?: any;
  computeHeadLookAt?: any;
  exitFocusMode?: any;
  exprChangeInterval?: any;
  exprChangeTimer?: any;
  exprNames?: any;
  exprRandomPool?: any;
  exprTargets?: any;
  exprValues?: any;
  findExpression?: any;
  gazeHeadTiltAcc?: any;
  getWorldCenter?: any;
  handleCharacterClick?: any;
  hideChatBubble?: any;
  identifyModelPart?: any;
  initExpressionState?: any;
  lastProactiveTime?: any;
  mutualGaze?: any;
  nextBlinkAt?: any;
  refreshExprNames?: any;
  scheduleNextBlink?: any;
  setVRMExpression?: any;
  showChatBubble?: any;
  smoothRotX?: any;
  smoothWalkFaceOff?: any;
  speechBubble?: any;
  triggerPokeAt?: any;
  updateExpressions?: any;
  updateIdleExpression?: any;
  updatePlayerPhysics?: any;
  updateSmoothTeleport?: any;
  updateSpeechBubble?: any;
  vrmMouthScale?: any;
  wasMutualGaze?: any;

  /* ---- js/character/08_expression_engine.js ---- */

  /* ---- js/codex/28_codex_runner.js ---- */
  addCodexMsg?: any;
  handleCodexMessage?: any;

  /* ---- js/core/01_start.js ---- */

  /* ---- js/core/02_three_scene.js ---- */
  ACTION_GAP_MAX?: any;
  ACTION_GAP_MIN?: any;
  AI_LOBBY_WALK_SPEED?: any;
  AI_LOBBY_WALK_STEP_LENGTH?: any;
  ALL_POSES?: any;
  AUTO_CAM_DELAY?: any;
  ActionType?: any;
  CONV_POSES?: any;
  DANCE_KINDS?: any;
  DANCE_TEMPO?: any;
  DRAG_THRESHOLD?: any;
  MUTUAL_GAZE_WINDOW?: any;
  PINCH_SENSITIVITY?: any;
  POSES?: any;
  POSE_ENTER_TIME?: any;
  POSE_EXIT_TIME?: any;
  POSE_GAP_MAX?: any;
  POSE_GAP_MIN?: any;
  POSE_HOLD_MAX?: any;
  POSE_HOLD_MIN?: any;
  PROACTIVE_COOLDOWN_MS?: any;
  PROACTIVE_SILENCE_MS?: any;
  WALK_PATH_MAX_SEGMENTS?: any;
  WALK_RANGE?: any;
  WALK_SPEED?: any;
  WALK_STEP_LENGTH?: any;
  ZOOM_THRESHOLD?: any;
  _GRAVITY?: any;
  _MAX_FALL_SPEED?: any;
  _PHYSICS_SUBSTEPS?: any;
  _fullWalkAnimActive?: any;
  _fullWalkPhase?: any;
  _fullWalkRampFactor?: any;
  _fullWalkRampT?: any;
  _groundRayDir?: any;
  _groundRayOrigin?: any;
  _groundRaycaster?: any;
  _onAIWalkComplete?: any;
  addActionWobble?: any;
  addStars?: any;
  advanceWalkSegment?: any;
  alignBodyToWalkDirection?: any;
  applyFullBodyWalkAnimation?: any;
  clickRaycaster?: any;
  clickStartPos?: any;
  clickWobble?: any;
  clock?: any;
  computePoseBlend?: any;
  currentPose?: any;
  danceDuration?: any;
  danceElapsed?: any;
  danceKind?: any;
  danceSpinDir?: any;
  danceSpinSpeed?: any;
  danceSpinStartY?: any;
  danceTurn?: any;
  danceTurnPlan?: any;
  dragTotalRot?: any;
  focusPart?: any;
  gazeBoostUntil?: any;
  gyroPitch?: any;
  gyroYaw?: any;
  idleEnergy?: any;
  idleWalkSpeed?: any;
  idleWalkStart?: any;
  isDragging?: any;
  lastInteractionTime?: any;
  lerp?: any;
  pickNextActionType?: any;
  pickRandomPose?: any;
  pickWalkPath?: any;
  pinching?: any;
  poseBlend?: any;
  poseBlendTarget?: any;
  posePhase?: any;
  poseTimer?: any;
  prevPose?: any;
  recordInteraction?: any;
  startDanceAction?: any;
  startPoseAction?: any;
  startTurnAction?: any;
  startWalkAction?: any;
  turnDuration?: any;
  turnElapsed?: any;
  turnProgress?: any;
  turnStartAngle?: any;
  turnTargetAngle?: any;
  updateActionScheduler?: any;
  updateDanceAction?: any;
  updatePoseTimer?: any;
  updateTurnAction?: any;
  updateWalkTimer?: any;
  userRotX?: any;
  userRotY?: any;
  walkFacingAngle?: any;
  walkSegmentsTotal?: any;
  zoomAbsTotal?: any;
  zoomDebounceTimer?: any;
  zoomNet?: any;

  /* ---- js/core/03_model_load_gltf_vrm.js ---- */

  /* ---- js/core/05_move_mode.js ---- */

  /* ---- js/core/08_state_switch.js ---- */

  /* ---- js/core/19_boot.js ---- */

  /* ---- js/rl/engagement-rl-agent.js ---- */
  setBlendShape?: any;

  /* ---- js/rl/unified-dating-system.js ---- */
  currentMode?: any;

  /* ---- js/input/human-trajectory-recorder.js ---- */
  _gameCamAzimuth?: any;

  /* ---- js/ui/14_toast.js ---- */

  /* ---- js/ui/15_model_ui.js ---- */

  /* ---- js/ui/17_events.js ---- */

  /* ---- js/ui/42_attach.js ---- */
  attachPending: any[];
  initAttach: () => void;
  uploadFiles: (files: File[]) => Promise<void>;
  renderAttachStrip: () => void;
  takeAttachments: () => any[];
  clearAttachments: () => void;

  /* ---- js/ui/23_name_settings.js ---- */

  /* ---- js/ui/25_character_cards.js ---- */
  allRcTools?: any;
  applyRoleCard?: any;
  captureCurrentRole?: any;
  closeRoleCardModalIfOpen?: any;
  collectRcTools?: any;
  collectRoleCardForm?: any;
  deleteRoleCard?: any;
  fillRoleCardForm?: any;
  llmGlobalConfig?: any;
  llmProvidersCache?: any;
  loadRcLlmGlobalConfig?: any;
  loadRcLlmModels?: any;
  refreshRcLlmVisionTip?: any;
  loadProvidersCache?: any;
  loadRcModels?: any;
  loadRcSttConfig?: any;
  loadRcTools?: any;
  loadRcVoices?: any;
  openProviderModal?: any;
  closeProviderModal?: any;
  refreshProviderList?: any;
  renderProviderList?: any;
  openProviderEditor?: any;
  saveProvider?: any;
  deleteProvider?: any;
  activateProvider?: any;
  testProvider?: any;
  loadLlmProxyConfig?: any;
  saveLlmProxy?: any;
  providerNameById?: any;
  renderRcProviderOptions?: any;
  _editingProviderId?: any;
  openRoleCardEditor?: any;
  openRoleCardModal?: any;
  rcEditingId?: any;
  rcLlmDefaultTemp?: any;
  rcLlmProviderId?: any;
  _rcPresetModel?: string;
  rcSttLoaded?: any;
  rcToolsLoaded?: any;
  rcVoicesLoaded?: any;
  rcAnimsLoaded?: any;
  allRcAnims?: any;
  loadRcAnims?: any;
  renderRcAnims?: any;
  collectRcAnims?: any;
  updateRcAnimCount?: any;
  refreshRoleCardList?: any;
  renderRcTools?: any;
  renderRoleCardList?: any;
  saveRcSttConfig?: any;
  saveRoleCard?: any;
  switchRcLlmProvider?: any;
  switchRcSttProvider?: any;
  switchRcTTSEngine?: any;

  /* ---- js/ui/30_task_big_screen.js ---- */
  taskBoardAddMedia?: any;
  taskBoardOnInteraction?: any;
  taskBoardOnNotify?: any;
  taskBoardOnToolChain?: any;
  updateTaskBigScreen?: any;

  /* ---- js/vr/webxr-vr.js ---- */
  _accumHeadShake?: any;
  _aiDrivenWalk?: any;
  _ensureVrShake?: any;
  _enterWebXR?: any;
  _exitWebXR?: any;
  _isMobileDevice?: any;
  _lastXrMode?: any;
  _onControllerConnected?: any;
  _onControllerDisconnected?: any;
  _onControllerSelectStart?: any;
  _onControllerSqueezeStart?: any;
  _onSessionEnd?: any;
  _readStdGamepad?: any;
  _setupXRControllers?: any;
  _stdPadPressed?: any;
  _upVec?: any;
  _vrFixedRotY?: number | null;
  _vrHudSide?: { x: number; z: number } | null;
  _vrScreenWorldObjs?: any;
  _xrCaptureWorld?: any;
  _xrControllers?: any;
  _xrEyeOffY?: any;
  _xrHeadPitchPrev?: any;
  _xrHeadPos?: any;
  _xrHeadPosPrev?: any;
  _xrHeadYawPrev?: any;
  _xrHeightState?: any;
  _xrLookDownTimer?: any;
  _xrLookUpTimer?: any;
  _xrModeAvailable?: any;
  _xrPadClick?: any;
  _xrPauseAutonomy?: any;
  _xrPrevImmersed?: any;
  _xrRaycaster?: any;
  _xrResetView?: any;
  _xrRestoreCamera?: any;
  _xrResumeAutonomy?: any;
  _xrSaveCamera?: any;
  _xrSavedAutonomy?: any;
  _xrSavedCamera?: any;
  _xrSession?: any;
  _xrShakeSmooth?: any;
  _xrShiftWorld?: any;
  _xrSnapBackCamera?: any;
  _xrTmpQuat?: any;
  _xrTmpVecA?: any;
  _xrTmpVecB?: any;
  _xrUndoWorldShift?: any;
  _xrUserOrigin?: any;
  _xrWorldObjs?: any;
  _xrWorldShift?: any;
  cycleXrMode?: any;
  enterXrMode?: any;
  hideVrOverlay?: any;
  showVrOverlay?: any;
  updateXRControllers?: any;
  updateXRFaceUser?: any;
  updateXRHeight?: any;
  vrDecayTimer?: any;
  xrPresenting?: any;
}
