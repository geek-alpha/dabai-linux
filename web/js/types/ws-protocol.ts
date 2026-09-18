/* ============================================================
 * WebSocket 协议类型 —— 阶段 1（类型地基）
 * ------------------------------------------------------------
 * 前后端 JSON 消息的类型总账，来源：
 *   - 下行（服务端 → 客户端）：09_websocket.ts handleWSMessage 的全部分支
 *   - 上行（客户端 → 服务端）：各 ws.send 调用点
 *   - audio_chunk 字段经 server.py TTS 流式推送核实
 *
 * 约定：只声明前端实际读写的字段；未尽字段用索引签名兜底，
 * 后续阶段按需收紧。
 * ============================================================ */

/* ============ 下行：服务端 → 客户端 ============ */

/** 聊天历史条目（user_set / session_switched 附带） */
export interface ChatHistoryItem {
  user?: string;
  ai?: string;
}

/** 会话摘要（session_list 附带） */
export interface SessionSummary {
  id: string;
  title?: string;
  is_active?: boolean;
  updated_at?: string | number;
  message_count?: number;
  approx_tokens?: number;
  summary?: string;
  pinned?: boolean | number;
  archived?: boolean | number;
  is_current?: boolean;
}

/** TTS 音频分片 / 结束信号（10_tts_lipsync 消费，含 session 过滤） */
export interface AudioChunkMessage {
  type: 'audio_chunk' | 'audio_end';
  session_id?: string | null;
  seq?: number;
  text?: string;
  audio_b64?: string | null;
  audio_mime?: string | null;
  final?: boolean;
  [k: string]: any;
}

/** LLM 用量事件（usage 徽章，13_messages 消费） */
export interface UsageMessage {
  type: 'usage';
  [k: string]: any;
}

/** 用户/AI 回复被打断 */
export interface InterruptedMessage {
  type: 'interrupted';
  session_id?: string | null;
  [k: string]: any;
}

/** DSH 桥接任务状态推送 / 轮询结果（共用同一形状） */
export interface BridgeStatusMessage {
  type: 'bridge_status';
  request_id?: string;
  status?: 'pending' | 'running' | 'done' | 'error' | 'cancelled' | string;
  reply?: string;
  error?: string;
  [k: string]: any;
}

/** harness 任务系统终态推送（后台长任务完成提示） */
export interface HarnessTaskPush {
  state?: string;
  status?: string;
  name?: string;
  id?: string;
  [k: string]: any;
}

/** AI 屏幕控制指令参数（MCP 工具 → 前端动作） */
export interface ScreenCommandArgs {
  model_name?: string;
  bg_name?: string;
  engine?: string;
  voice?: string;
  rate?: number | string;
  mode?: string;
  message?: string;
  volume?: number;
  title?: string;
  artist?: string;
  url?: string;
  [k: string]: any;
}

export interface ScreenCommandMessage {
  type: 'screen_command';
  tool?: string;
  args?: ScreenCommandArgs;
}

/** RL 统一调度计划（rl_dispatch 附带） */
export interface RlDispatchPlan {
  agent_choice?: 'engagement' | 'ai_agent' | 'game_agent' | string;
  reason?: string;
  mode_name?: string;
  behavior_cmd?: any;
  strategy?: any;
  snapshot_interval?: number;
  interval_mode?: 'game' | 'lobby' | string;
  [k: string]: any;
}

/** RL 状态回显（rl_sync 的响应） */
export interface RlStatusPayload {
  snapshot_interval?: number;
  interval_mode?: 'game' | 'lobby' | string;
  [k: string]: any;
}

/** 服务端 → 客户端消息全集（handleWSMessage 的 switch 分支） */
export type ServerMessage =
  | { type: 'ready' }
  | { type: 'pong' }
  | { type: 'user_set'; user_id?: string; history?: ChatHistoryItem[] }
  | { type: 'thinking'; session_id?: string | null; text?: string; resume?: boolean }
  | { type: 'thinking_text'; session_id?: string; text?: string }
  | { type: 'reasoning'; session_id?: string; text?: string }
  | { type: 'stream_text'; session_id?: string; text?: string }
  | { type: 'retract_text'; session_id?: string; length?: number }
  | { type: 'turn_status'; session_id?: string; text?: string }
  | { type: 'turn_in_progress'; session_id?: string | null; resume?: boolean; full_text?: string; reasoning_tail?: string; tool_events?: any[] }
  | { type: 'listening' }
  | { type: 'system_msg'; text?: string }
  | { type: 'transcript'; text?: string }
  | AudioChunkMessage
  | UsageMessage
  | InterruptedMessage
  | { type: 'tool_call_start'; tool_name?: string; tool_desc?: string; arguments?: any }
  | { type: 'tool_call_result'; tool_name?: string; result?: any; success?: boolean }
  | { type: 'tool_call_progress'; tool_name?: string; elapsed?: number; message?: string }
  | { type: 'codex_start' | 'codex_log' | 'codex_progress' | 'codex_done' | 'codex_error' | 'codex_timeout' | 'codex_terminated' | 'codex_msg'; [k: string]: any }
  | { type: 'session_list'; sessions?: SessionSummary[]; query?: string }
  | { type: 'session_switched'; session_id?: string; history?: ChatHistoryItem[]; summary?: string }
  | { type: 'session_created'; session_id?: string; reason?: string }
  | { type: 'session_deleted'; session_id?: string; next_session_id?: string }
  | { type: 'session_renamed'; session_id?: string; title?: string }
  | { type: 'session_pinned'; session_id?: string; pinned?: boolean }
  | { type: 'session_archived'; session_id?: string; archived?: boolean; active_changed?: boolean }
  | { type: 'error'; message?: string }
  | { type: 'restart_vad'; reason?: string }
  | { type: 'bridge_confirm'; request_id?: string; task?: string }
  | BridgeStatusMessage
  | { type: 'bridge_say'; text?: string }
  | { type: 'task_event'; event?: any }
  | { type: 'media_worker_event'; kind?: string; event?: any; [k: string]: any }
  | { type: 'harness_task'; task?: HarnessTaskPush }
  | { type: 'task_tree'; data?: any; tree?: any; [k: string]: any }
  | ScreenCommandMessage
  | { type: 'ai_behavior_command'; behavior?: string; [k: string]: any }
  | { type: 'rl_dispatch'; data?: RlDispatchPlan }
  | { type: 'rl_status'; data?: RlStatusPayload };

/* ============ 上行：客户端 → 服务端 ============ */

/** rl_sync 携带的统一状态快照（前端 RL 心跳） */
export interface RlSyncPayload {
  affection: number;
  trust: number;
  intimacy: number;
  emotion: number;
  want_decision: boolean;
  event: string;
  seconds_since_user_message: number;
  user_engaged: boolean;
}

/** 客户端 → 服务端消息全集 */
export type ClientMessage =
  | { type: 'set_user'; user_id: string }
  | { type: 'set_avatar'; name: string }
  | { type: 'set_background'; name: string }
  | { type: 'ping' }
  | { type: 'text'; content: string; attachments?: unknown[] }
  | { type: 'list_sessions'; q?: string; include_archived?: boolean }
  | { type: 'search_sessions'; q?: string }
  | { type: 'new_session' }
  | { type: 'switch_session'; session_id: string }
  | { type: 'delete_session'; session_id: string }
  | { type: 'rename_session'; session_id: string; title: string }
  | { type: 'pin_session'; session_id: string; pinned?: boolean }
  | { type: 'archive_session'; session_id: string; archived?: boolean }
| { type: 'rl_sync'; data: RlSyncPayload }
  | { type: 'audio'; data: string; mime_type?: string }
  // 媒体子智能体回报：前端把「播完/停止」事件带回，闭环看护 worker
  | { type: 'music_end'; worker_id?: string; name?: string; final?: boolean }
  | { type: 'music_stop'; worker_id?: string }
  | { type: 'video_stop'; worker_id?: string }
  // 前端动作状态上报：播放/停止动作时同步，供 LLM 说话时知道自己正在做什么
  | { type: 'anim_state'; anim: { name?: string; category?: string; emotion?: string } | null };
