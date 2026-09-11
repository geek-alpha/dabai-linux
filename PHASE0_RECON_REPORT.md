# 阶段 0 勘察报告：大白融合改造（Open-LLM-VTuber × airi 模块移植）

> 日期：2026-09-08
> 任务：D:\AI\dabai 保持单一灵魂，新增语音打断、屏幕/剪贴板感知、Minecraft 陪玩三大能力
> 本报告为阶段 0 交付物：只读勘察 + 参考仓库精读，未改动任何运行代码。

## 目录

1. [结论摘要](#1-结论摘要)
2. [项目结构与入口](#2-项目结构与入口)
3. [技能与扩展体系（harness）](#3-技能与扩展体系harness)
4. [已有语音能力现状（ASR/TTS/打断）](#4-已有语音能力现状asrtts打断)
5. [已有感知与游戏现状](#5-已有感知与游戏现状)
6. [Open-LLM-VTuber 精读与可移植点](#6-open-llm-vtuber-精读与可移植点)
7. [airi Minecraft 模块精读与可移植点](#7-airi-minecraft-模块精读与可移植点)
8. [接口分析与差异表](#8-接口分析与差异表)
9. [阶段差距清单与建议技术路线](#9-阶段差距清单与建议技术路线)
10. [性能基线与环境事实](#10-性能基线与环境事实)
11. [风险与未决问题](#11-风险与未决问题)
12. [证据索引](#12-证据索引)

## 1. 结论摘要

### 1.1 大白已具备的能力（与本次任务高度重叠）

- 大白已经是"Open-LLM-VTuber 同类"的 3D VTuber 型语音陪聊项目：FastAPI + WebSocket 双向实时通信、浏览器录音、服务端 STT（SiliconFlow SenseVoiceSmall → faster-whisper 本地兜底）、edge_tts / GPT-SoVITS / API 三引擎 TTS、逐句流式 TTS 分片、Lipsync、VAD 自动对话模式。
- **语音打断已在线上形态**：浏览器本地 VAD（音量 + 人声评分）检测用户开口，播放中 200ms 确认窗口触发 `triggerInterrupt()`（本地立即停播 + WS 通知服务端取消生成任务），并有会话栅栏防迟到音频分片。这是"能量级打断"，不是流式 STT 语义级打断。
- 已有 harness 技能/插件扩展底座 + 大量技能目录；新增模块应**注册为技能/插件**而非侵入核心。
- 已有感知调度器 `perception_dispatcher.py`（EventCategory、EventRule、dispatch）与游戏引擎 `game_engine.py`（moba/寻宝/沙盒/mario/lobby 等场景、自主行为、RL 调度）——新模块应接入这两套既有通道。
- 屏幕截图能力只有遗留的 `screen_shot.py`（pyautogui）且未接入对话；剪贴板与视觉理解**无实现**。

### 1.2 两个参考仓库能提供什么

- **Open-LLM-VTuber**（Python，commit `992309c0`）：可借鉴的是"全链路工厂化接口"（ASR/TTS/VAD 各自 interface + factory + 多实现）、**浏览器持续传 PCM → 后端 VAD 出 utterance 边界 → 控制信号打断**的实时语音协议、以及**多模态输入类型**（ImageSource.SCREEN/CLIPBOARD/UPLOAD、TextSource.CLIPBOARD → OpenAI 兼容 vision content 数组）——正好对应阶段 1 的"流式 STT 语义打断"与阶段 2 的"屏幕/剪贴板 → 视觉模型"接线范式。
- **airi**（TS monorepo，commit `f679616c`）：`integrations/minecraft` 是完整可参考的 Minecraft 智能体（Mineflayer 运行时 + 四层认知架构 + 5s 状态上下文推送）。可移植的是**状态采集与上下文服务模式**（position/health/gameMode/天气/时刻 → 结构化文本 → 每 5s 以 ReplaceSelf 语义推给对话层）、**游戏内聊天进出与过滤**（ChatMessageHandler）、**感知事件规则**。airi 版是"bot 入局自己玩"；大白"陪玩同局"的形态需在阶段 3 前与用户确认（见 §11）。

### 1.3 总体建议

三阶段均按"**既有架构上加独立模块 + 配置开关**"实现，不整套搬参考代码；代码侧优先 Python（技能 + server 接线），前端 TS 仅做最小必要改动。本报告 §9 给出了每阶段的差距清单与具体路线。

## 2. 项目结构与入口

### 2.1 目录真实形态（勘察要点）

根目录 D:\AI\dabai 是一个 Python + TypeScript 混合仓库，git 分支 `main`，领先远端 `origin/auto-20260804-220635` 12 个 commit，**工作区有大量既有未提交改动**（server.py、agent.py、memory.py、skills/* 等，见 `git status`），另有多个 `server.py.bak-*` 备份与超大数据库/日志文件（chat_memory.db-wal 4.1MB 等）。改造必须只做增量，不与这些改动冲突。

| 文件/目录 | 作用（勘察事实） |
|---|---|
| `server.py`（323KB） | 主服务：FastAPI + `/ws` WebSocket 主循环（5311 行起）、STT/TTS、会话/记忆/角色卡/RL/游戏/媒体全部 API 与路由；`if __name__ == "__main__"` 于 6916 行起跑 uvicorn |
| `agent.py`（209KB） | AI Agent 核心：`AIAgent`（1431 行）、`chat_stream`/`_chat_stream_game`、工具执行 `execute_local_tool`（1228 行）、长记忆 ChatMemory |
| `web/` | 前端（TypeScript 模块化，无构建产物在 repo）：`web/js/core/*` 启动/状态/VAD/TTS-Lipsync，`web/js/network/09_websocket.ts` 连接，`web/js/types/ws-protocol.ts` 协议总账 |
| `skills/` | 技能目录：agent_ops / appearance / code_ops / media / search / smell-check-main / tasks，各含 `skill.json`（OpenAI function schema）与 `skill.py` + `*_impl.py` |
| `harness/`、`harness_bridge.py` | 扩展运行时：技能/插件收集、熔断、RunSpan 监督、DSH 桥（`HARNESS.md`） |
| `game_engine.py` / `perception_dispatcher.py` | 既有游戏/感知中枢（详见 §5） |
| `ai_perception_engine.py` / `ai_behavior_engine.py` | 行为/感知策略（历史遗留，被 game_engine 组合） |
| `memory.py`（108KB）+ `chat_memory.db*` | 长期记忆 SQLite + 层级压缩 |
| `dabai.py` / `screen_shot.py` / `dabai.bat` | 遗留 CLI 骨架：`dabai.bat` 执行 `python server.py`；`dabai.py` 引用不存在的 `amazing_agent_dingding/dabai_voice/dabai_ears`（已不可运行，非主入口） |

### 2.2 入口与启动

- 主入口：`python server.py`（见 dabai.bat）。uvicorn 绑定 `SERVER_PORT`（变量定义在 server.py 内，main 于 6916–7024 行）。
- Web 前端开发用 vite（package.json `dev`），但**当前 shell 找不到 node/npm**（见 §10），前端验证需用户侧工具链或另行确认。
- 配置文件：`settings.json`（LLM 供应商/角色/记忆/agent/stt 参数）、`stt_config.json`、`tts_config.json`、`character_cards.json`、`codex_runtime.json`。注意所有文件**均为 UTF-8 编码**，旧日志/README 有 GBK 乱码属显示问题，读取一律 `-Encoding UTF8`。

### 2.3 通信协议（WS 主链路）

- 服务端：`websocket_endpoint`（server.py:5312）收 JSON；上行类型含 `set_user/text/audio/interrupt/anim_state/enter_game_mode/game_state/rl_sync/…`；`_ALWAYS_ALLOW` 与 `_USER_INPUT={"text","audio"}` 决定"谁可打断 AI"（server.py:5340-5353）。
- 用户输入 `text`（server.py:5789 区段）与 `audio`（server.py:5847 区段）都走 `handle_user_message_stream`（server.py:4344）：流式文字 + 并行 TTS worker 逐句推送 `audio_chunk/audio_end`。
- 前端类型总账：`web/js/types/ws-protocol.ts`（ServerMessage/ClientMessage 联合类型，含 `InterruptedMessage`）。

## 3. 技能与扩展体系（harness）

- 技能注册：`skills/<name>/skill.json`（必填 name/title/version/description/enabled/disclosure/prompt/tools），`skill.py` 可选实现（TOOLS/PROMPT/HANDLERS/execute，生命周期 on_load/on_unload）——HARNESS.md 278-291 行有明确 schema 说明；技能/插件工具自动并入 `load_local_tools()`（agent.py:1053）供 function calling。
- 工具执行链：`AIAgent._execute_tool` → `execute_local_tool`（agent.py:1228）→ harness runtime `supervise_tool`（熔断/超时/计量），同一轮工具可并行。
- 服务端热重载与管理 API：`/api/harness/status|reload|skills|plugins`（server.py 6246 起），改技能无需重启进程。
- 既有技能目录能力速览：
  - `media`：视频/音乐/汉化流水线/小游戏/AI 画图（skill.json 内注册大量 function tool）
  - `tasks`：待办/定时/复盘/策略库（可作统一日志与执行规范的落点）
  - `appearance`：界面模式/Toast/声线（返回 `__screen_command__` 标记由服务端转前端动作）
  - `search`/`agent_ops`/`code_ops`：联网搜索、委派子智能体、代码操作
- 特殊返回标记协议：工具可返回 `__screen_command__` / `__media_watch__` 等 JSON 前缀，server.py 在 agent 工具结果里解析并转成前端屏幕指令（server.py:4745 附近、agent.py:89 注释）。
- **结论**：新增"屏幕感知"与"Minecraft 状态工具"最自然的形态是 `skills/<新技能>/skill.json + skill.py`，按既有 registry 注册即可被主 Agent 调用；持续感知事件则复用 PerceptionDispatcher。

## 4. 已有语音能力现状（ASR/TTS/打断）

### 4.1 STT（语音→文字）

- 配置：`stt_config.json`——provider `auto`：API 优先 SiliconFlow `FunAudioLLM/SenseVoiceSmall`（`api_timeout: 6`），失败/空结果降级本地 faster-whisper（`Systran/faster-whisper-medium`, cpu int8，HF 镜像 `hf-mirror.com`）。settings.json `stt` 段还有一档参数（base/int8）。
- 服务端实现：`speech_to_text()`（server.py:1023）+ `speech_to_text_local()`（server.py:965）双路径；`convert_to_wav()`（server.py:772）负责 ffmpeg 转 16k 单声道 wav（可选降噪二遍重试）。
- **形态是"整段文件识别"**：浏览器 VAD 录完一整段（停顿即切）→ 以 `{type:"audio", data: base64, mime_type}` 整段上传（server.py:5847 区段）→ 服务端落临时文件 → ffmpeg → STT → 得到整句文字。不存在"边说边出中间结果"的流式识别。
- 降噪/容错：STT 失败冷却（连续 3 次 / 8s 窗口暂停）、MIME 与字节魔数双检测（防 webm 封包错）。

### 4.2 TTS（文字→语音，流式分片）

- 配置：`tts_config.json`——engine `edge_tts`（zh-CN-XiaoyiNeural, rate +55%），另支持 GPT-SoVITS 本地 7860 与 OpenAI 兼容 API TTS。
- 实现：`generate_tts()`（server.py:672）→ edge/gptsovits/api 三引擎，失败自动回退 edge；`handle_user_message_stream`（server.py:4344）把 LLM 流式输出按标点/60 字切句，`_tts_worker` 并行预生成、按 seq 有序推送 `audio_chunk`（text+audio_b64+mime+thinking），回复结束推 `audio_end`（含 full_text）。
- 播放端：`web/js/core/10_tts_lipsync.ts` 维护 audioQueue，逐段 `<audio>` 播放 + 口型/字幕/气泡；TTS 文本与语音解耦（stream_text 即时显示，audio_chunk 驱动发声与气泡）。
- **TTS 音频在浏览器端播放**（不是服务端扬声器直出），这决定了"打断"主战场在前端播放队列 + 服务端生成任务两个层面。

### 4.3 VAD 与打断（现状能力）

浏览器有"按住说话"与"VAD 自动对话"两种模式（`11_voice_record.ts`、`12_vad_auto.ts`）：

- VAD 常驻 PCM 环形缓冲：说话起点回溯 550ms（打断场景 120ms）防丢开头字；16k 采样上传免重采样（12_vad_auto.ts:117-134）。
- 自动模式状态机：AI SPEAKING 时用"音量 + 200ms 确认窗口"判打断（阈值 `VAD_INTERRUPT_THRESHOLD=0.07`、`VAD_INTERRUPT_MS=200`，01_start.ts:244-246；12_vad_auto.ts:451-463）；AI THINKING 时加人声特性评分防误触发（12_vad_auto.ts:468-485）。
- 打断执行：`App.triggerInterrupt()`（10_tts_lipsync.ts:345 起）＝①本地 `clearAudioQueue()`（pause+销毁 Audio/blob，即时停声）②记 `_interruptedSession` 会话栅栏 ③WS 发 `{type:"interrupt"}`。
- 服务端响应：server.py:5774-5785 处理 `interrupt`——取消全局活跃轮 + `state.active_task.cancel()` + 回执 `interrupted`；前端 `handleInterrupted` 清残余分片、复位状态（10_tts_lipsync.ts:296-310）。
- 受保护纪律：只有用户 text/audio/interrupt 能打断 AI；感知事件/自主行为在 AI 忙/冷却期被丢弃（server.py:5340 注释与 `_kickoff_response` 4926 起的 `allow_interrupt` 逻辑）。

### 4.4 现状评估（对阶段 1 验收的含义）

- "播放中插话 ≤500ms 被打断"在**浏览器端能量检测路径下应已满足或接近**：检测确认 200ms + 触发即本地静音，本地停播不依赖网络。真正的缺口是：
  1. 打断判据是**能量/音量**而非"识别到用户语音内容"（环境音可能误断；用户轻声/远场可能不断）；
  2. 没有**流式 STT**：用户整句说完才转文字，下一轮回复要等整段上传+ffmpeg+STT（数百 ms 到数秒）；
  3. "打断后马上重新聆听/连续对话"的平滑性依赖 VAD 状态机细节，缺可度量埋点。
- 因此阶段 1 的核心不是"从零做打断"，而是**升级判据与链路**（可选项见 §9.1），并补性能埋点用于验收。

## 5. 已有感知与游戏现状

### 5.1 屏幕/剪贴板感知现状

- `screen_shot.py`：pyautogui 截屏存 PNG（工具函数，仅被不可运行的 legacy `dabai.py` 引用）；**未接入任何对话/agent 工具**。
- 全仓库搜索：agent.py/server.py 无 `image_url`/vision 多模态调用实现；只有 agent.py 游戏提示词里"你和用户共享同一个屏幕/你能看到屏幕上的一切"这类**文字设定**（agent.py:3535、game_engine.py:266），并无真实截图管道。
- 剪贴板：无读取实现；Open-LLM-VTuber 的"TextSource.CLIPBOARD"模式可供移植（见 §6.3）。
- LLM 侧已具备条件：settings.json `llm_providers` 里已有"opencode go"供应商（`deepseek-v4-flash-vision-exp`，vision 模型）与多个 OpenAI 兼容供应商，可在 vision 摘要时选用。

### 5.2 感知/游戏/RL 既有中枢

- `perception_dispatcher.py`：`EventCategory`（28 行）/ `EventRule` / `PerceptionDispatcher.dispatch`（190 行）统一路由 `game_state/game_event/game_update/environment_snapshot/ai_behavior_result/game_result/proactive`；含冷落度、全局闸门、保护检查（`_passes_protection` 312 行）、最近历史统计。
- `game_engine.py`：`GameWorld`（快照/事件/`get_ai_perception`）+ `GameEngine`（apply_snapshot/handle_game_event/handle_game_result/handle_exit_game/get_game_context_for_ai/produce_behavior_command…）。现有游戏类型证据：maze/open_field/platformer（GameWorld 构造 98 行附近）、场景 lobby/game_maze/game_sandbox（1051-1077 行）、MOBA/寻宝/沙盒/Mario 引导文本（agent.py:3566-3651）。
- 前端游戏（web/js/game/）：game-mode-manager、game-state-observer、moba-5v5、xiangqi、cyber-corp、unified dating/RL 等——**无 Minecraft**。
- WS 游戏接入点：`enter_game_mode`（server.py:5611 区段）创建 `GameEngine` 并注入描述；`exit_game_mode` 捕获最终上下文并告别；`game_state/game_event/game_update` 等感知事件统一走 `_PERCEPTION_EVENTS` → PerceptionDispatcher（server.py:5348、5738 区段）。
- 外部游戏状态格式：`environment_snapshot` 存在（GameEngine.apply_environment_snapshot 1050 行）——新 Minecraft 状态桥可直接以该消息类型送入，无需发明新协议。

### 5.3 对阶段 2/3 的启示

- 屏幕感知做"按需工具（工具调用/用户指令）"+ 可选"周期性主动快照"，后者的触发走 PerceptionDispatcher 既有保护纪律，避免变成话痨。
- Minecraft 状态接入的两种既有缝：①技能工具（大白主动问/被问时调工具拿状态）；②`environment_snapshot`/`game_update` 周期事件（外部桥推状态，dispatch 决定是否说话）。两条缝都不需要改 WS 用户输入主链路。

## 6. Open-LLM-VTuber 精读与可移植点

> 临时克隆：`%TEMP%\dabai_merge_refs\Open-LLM-VTuber`，commit `992309c0aa19845960228f880013d4685fde93b5`（浅克隆，HEAD 基准）。源码主体在 `src/open_llm_vtuber/`。

### 6.1 架构总览（与大白的对应关系）

| OLV 概念 | 位置 | 大白对应物 |
|---|---|---|
| FastAPI + WebSocket 消息路由 | `websocket_handler.py`（MessageType 35-46 行、处理器字典 86 行起） | server.py `websocket_endpoint` |
| 会话编排/流式回复/中断 | `conversations/conversation_handler.py` | `handle_user_message_stream` + `_kickoff_response` |
| ASR 多引擎工厂 | `asr/asr_factory.py` + `asr/*_asr.py`（faster_whisper/fun_asr/sherpa/azure/groq/openai_whisper/whisper_cpp） | `speech_to_text()` 双路径（可扩展成同款工厂） |
| TTS 多引擎工厂 | `tts/tts_factory.py` + `tts/*`（edge/gpt_sovits/cosyvoice/…共 17 个） | `generate_tts()` 三引擎 |
| VAD | `vad/vad_interface.py` + `silero.py`（Silero VAD，16k，0.032s 帧） | 浏览器 VAD（12_vad_auto.ts）；服务端无 VAD |
| 消息/历史/群组 | `chat_history_manager.py`、`chat_group.py` | memory.py + WS session |
| MCP 工具执行 | `mcpp/*`（tool_manager/tool_executor/mcp_client/server_registry） | harness 工具路由 + `__screen_command__` |
| 前端 | `frontend/`（git submodule，浅克隆未含内容） | `web/`（仓库内完整 TS） |

### 6.2 实时语音与打断协议（阶段 1 主要参考）

OLV 的 WebSocket 上行消息类型（`websocket_handler.py` MessageType）：

- `mic-audio-data`：浏览器持续把**PCM 音频数组**发给服务端累积到 `received_data_buffers[client_uid]`（`_handle_audio_data`）。
- `raw-audio-data`：浏览器持续传**原始音频块**，服务端送 `context.vad_engine.detect_speech(chunk)`（`_handle_raw_audio_data`）：
  - VAD 输出 `<|PAUSE|>` → 服务端回 `{"type":"control","text":"interrupt"}`（**打断信号**）；
  - 输出 `<|RESUME|>` → 继续监听；
  - 输出有效语音块 → 追加缓冲，并回 `{"type":"control","text":"mic-audio-end"}` 触发整段转写。
- `mic-audio-end` / `text-input`：正式触发一轮对话（`_handle_conversation_trigger` → `conversation_handler.handle_conversation_trigger`）。
- `interrupt-signal`：`_handle_interrupt`（websocket_handler.py:369）→ `handle_individual_interrupt` / `handle_group_interrupt`。

打断语义要点（conversation_handler.py）：

- `handle_individual_interrupt`（112 行起）：**取消对话 asyncio task**（`task.cancel()`）→ `agent_engine.handle_interrupt(heard_response)` → 把已听到的部分作为 ai 消息 + `system: "[Interrupted by user]"` 写回历史，让模型"知道被谁打断/听到哪"。
- Agent 侧：`BasicMemoryAgent.handle_interrupt`（basic_memory_agent.py:195）把 `[Interrupted by user]` 按 `interrupt_method`（system/user，配置默认 user）插入消息，后续新消息可自然续上下文。
- TTS 侧：`TTSTaskManager`（tts_manager.py）用 seq 计数 + 缓冲队列保证**并行合成、按序发送**（speak/_process_payload_queue/_process_tts），音频 payload 还附每 20ms RMS 音量数组供前端 Live2D 口型（`utils/stream_audio.py`）。被打断时任务取消即不再发分片。

**移植参考价值**：

1. "打断后让模型知道自己被谁打断/听到了哪一句"——大白现在的 interrupt 只取消不注入上下文，可以在 handle_interrupt 时把已播放文本写入历史（大白 `_pendingFullText/currentReplyText` 已有该信息）。
2. "服务端 VAD + control 信号"协议模式可移植到大白：把浏览器常驻 VAD 改成持续送 16k PCM + 服务端 Silero/能量 VAD 判句，服务端一旦判"用户开口"即发打断；不过这会让打断路径依赖网络 RTT。**建议保留浏览器即时本地停播作为第一道闸**，服务端 VAD/流式 STT 只用来提升判据质量与连续对话（见 §9.1 三个选项）。
3. 前端 OLV 的完整实现随 `frontend/` submodule 未在浅克隆中获得，移植时以协议与后端为准，前端沿用大白现有 12_vad_auto/10_tts_lipsync 改造。

### 6.3 多模态输入（阶段 2 主要参考）

- `agent/input_types.py`：
  - `ImageSource` 枚举：`CAMERA / SCREEN / CLIPBOARD / UPLOAD`——即"截屏/剪贴板图片/摄像头"被设计成第一等输入源；
  - `ImageData{source, data(base64或URL), mime_type}`、`TextData{source(INPUT/CLIPBOARD), content}`、`BatchInput{texts, images, files, metadata}`。
- 前端传入后，`BasicMemoryAgent._to_messages`（basic_memory_agent.py:229-276）把文字与图片组装成 OpenAI 兼容多模态消息：
  - 文本块 `{"type":"text","text":...}`；
  - 剪贴板文字以 `[User shared content from clipboard: ...]` 包一层（`_to_text_prompt` 224-240 行附近）；
  - 图片（`data:image/...` base64）转为 `{"type":"image_url","image_url":{"url":..., "detail":"auto"}}` 数组；无多模态能力的 Agent（Hume 等）显式告警忽略。
- 另在 mcpp/tool_executor.py 有"工具返回多图→文本摘要 `[Tool returned N image(s)]`"的压缩策略。

**移植结论**：大白阶段 2 不需要发明接口——照此模式加两个 harness 工具 `screen_capture`（截屏→base64 data URL）与 `clipboard_read`（pyperclip/浏览器剪贴板→文本），再在 agent 工具结果/上下文注入处按"OpenAI 兼容 `image_url` 消息块"发给 vision 供应商即可；剪贴板文本用 `[用户剪贴板：...]` 包裹注入。

### 6.4 其余参考点（按需取用）

- 配置系统 `config_manager/*`：每类组件独立类型安全配置类——大白用 settings.json 平铺即可，不必照搬。
- MCP 工具层 `mcpp/*`：大白 harness 已有工具路由 + MCP 相关能力（web/tool/mcp 未在本任务范围），不需要移植。
- 主动说话：`conversation_handler` 支持 `ai-speak-signal`（proactive_speak 元数据跳过记忆/历史）——大白 RL/PerceptionDispatcher 已实现同款纪律（`proactive` + `record_history=False`），无新增需求。

## 7. airi Minecraft 模块精读与可移植点

> 临时克隆：`%TEMP%\dabai_merge_refs\airi`，commit `f679616c34f1cf6d282c8d64264242af944b3fed`（浅克隆）。Minecraft 集成主体在 `integrations/minecraft/`（TS，包名 `@proj-airi/minecraft-bot`）。

### 7.1 模块全景

airi Minecraft 是一个**独立常驻 bot 进程**，用 Mineflayer 登录 Minecraft 服务器，跑四层认知（感知→反射→意识→动作），并通过 `@proj-airi/server-sdk` 的 WebSocket 事件缝回连 AIRI（桌面壳）。

目录骨架：

```
integrations/minecraft/
  src/main.ts                       # 入口：装配 config/bot runtime/cognitive/debug/airi client
  src/minecraft-bot-runtime.ts      # bot 生命周期（create/replace/disconnect）
  src/libs/mineflayer/              # mineflayer 封装：core.ts/status.ts/message.ts/ticker.ts/
                                    #   connection-supervisor/plugin-runtime/health/memory/command
  src/cognitive/
    perception/                     # 事件注册 + YAML 规则引擎（attacker/fall/low-health/…）
    reflex/                         # 快速本能（auto-eat/defend/escape-hazard/idle-gaze）
    conscious/                      # brain/planner/chat LLM 决策、map-renderer、任务状态
    action/                         # 动作注册/LLM 规划/任务执行
  src/airi/
    airi-bridge.ts                  # AIRI 事件缝：spark:command / context:update / module:announced / notify
    minecraft-context-service.ts    # 状态快照 → 周期性 context:update 推送（本文重点）
    start-background-client.ts
  src/skills/ + src/plugins/        # 具体技能（采集/合成/战斗/移动/寻路/容器…）与插件（echo/follow/status）
  src/debug/                        # MCP REPL / debug server / web 调试台 / mineflayer-viewer
  .env                              # 配置模板（服务器/BOT_AUTH/BOT_VERSION/OPENAI 键/debug 开关）
```

关键依赖（package.json）：`mineflayer` + `minecraft-data` + 插件矩阵 `mineflayer-armor-manager / auto-eat / collectblock / pathfinder / pvp / tool` + `prismarine-*` + `@modelcontextprotocol/sdk` + `isolated-vm`（动作脚本沙箱）。

### 7.2 状态读取与上下文推送（阶段 3 最直接的移植素材）

`src/airi/minecraft-context-service.ts`：

- 快照字段：`botUsername / serverHost:serverPort / position(x,y,z 一位小数) / health("n/20") / gameMode / otherPlayers / masterUsername`。
- `buildStatusText()` 输出一段**给 LLM 看的替换式状态说明**（含"在线/离线、何时别调用控制工具、服务端、坐标、血量、模式、同服玩家、主人游戏名"）。
- `bindBot()` 后每 **5s**（`STATUS_REFRESH_INTERVAL_MS=5000`）发布一次 `context:update`：`contextId="minecraft:status"`、`lane="minecraft:status"`、`strategy=ReplaceSelf`、`hints=[status, online/offline, 用户名]`；文本不变时不重复发（防刷屏）。
- `MinecraftContextService` 也向 AIRI 暴露"可用/不可用"命令中继状态，离线时明确指示 LLM 不得调用控制工具。

`src/libs/mineflayer/status.ts` 提供更低层、带生命周期的状态类：`position / health / weather(晴/雨/雷) / timeOfDay(晨/午/夜)`，`Status.from(bot)` 只在 ready 后取数，`toOneLiner()` 输出给 LLM 的一行状态。

`src/libs/mineflayer/message.ts`：游戏聊天处理——`ChatMessageHandler` 过滤 bot 自己的消息、`#` 命令前缀，保留 `(username, message)` 会话回调。

`src/libs/mineflayer/ticker.ts`：300ms 主循环（串行 update、超时兜底），供周期刷新/感知轮询复用。

`src/cognitive/perception/`：把 mineflayer 原生事件（受伤、坠落、低血、潜行、被攻击…）注册成 `raw:*` 事件，再经 YAML 规则引擎产出 `signal:*`（如 danger/low-health）——与大白 `perception_dispatcher` 的"事件+规则+触发文本"设计同构。

### 7.3 与 AIRI 的连接面（可对照大白 WS）

- `airi-bridge.ts`：SDK `Client` 订阅 `spark:command`（外部自然语言/动作意图，intent: plan/proposal/action/pause/resume/reroute/context，interrupt: force/soft/false）与 `context:update`（被动上下文只进历史）；本地 `EventBus` 事件（`signal:airi_context`、`signal:airi_command`）唤醒认知循环。
- 事件类型（main.ts `possibleEvents`）：`module:configure / module:announced / spark:command / context:update`。
- 通知：`spark:notify`（id/eventId/kind/href/title/body…）——AIRI 侧展示 bot 告警。

### 7.4 可移植/不可移植判定

**建议移植（模式，非整套）**：

1. `MinecraftContextService` 的"状态快照 → 5s 替换式上下文文本"：大白 game_context 注入同款（坐标/血/模式/同服玩家），间隔可放宽到 10~30s 或按需拉取。
2. `status.ts` 的字段集与 `toOneLiner` 格式（位置/血量/天气/时刻）。
3. `ChatMessageHandler` 的聊天过滤与命令前缀规则（避免 bot 自问自答）。
4. mineflayer 插件矩阵的选型经验（pathfinder/pvp/collectblock/tool/armor/auto-eat）。

**不建议整套移植**：

1. `src/cognitive/conscious/brain.ts`（82KB）+ `js-planner.ts`（55KB）+ prompt 体系：这是给"bot 自主决策/挖矿/战斗"用的认知堆栈，大白有统一灵魂（AIAgent+RL+PerceptionDispatcher），应把"认知"留在 agent 侧，不复制第二大脑。
2. `isolated-vm` 动作脚本沙箱、MCP REPL/debug server（未认证 RCE 面）：大白不需要 bot 自主执行动作链（陪玩形态，见 §11 待确认）。
3. 官方明确 airi Minecraft 已处于废弃路径（README 顶部 Deprecation Notice：将迁移到 Fabric mod 运行时），移植时**只借鉴接口设计**，不建立长期依赖。

### 7.5 如果"同局陪玩"需要大白以 bot 身份入局

则最小闭环是：mineflayer bot 进程（连同一服务器）+ 聊天桥（玩家 → 大白 TTS/气泡，大白回复 → `bot.chat`）+ 状态上下文（7.2）注入 game_context。该形态下 airi 的 `status/chat-message/context-service` 三件套基本够用，cognitive/action 全部不需要。若"同局"是指玩家在客户端玩、大白只看屏幕，则与 mineflayer 无关，改走阶段 2 的屏幕感知 + OCR/视觉理解（详见 §11 未决问题 Q3）。

## 8. 接口分析与差异表

### 8.1 语音链路差异（大白 ↔ Open-LLM-VTuber）

| 接口面 | 大白现状 | OLV（参考） | 差距/移植判定 |
|---|---|---|---|
| 录音上传 | VAD 录整段 → 一次 `audio` 上传 | 持续 `raw-audio-data`/`mic-audio-data` 上传 | 大白可加流式上传通道；整段路径保留作兜底 |
| 语音活动检测 | 浏览器（音量+人声评分） | 服务端 Silero VAD（16k/32ms 帧） | 差距：判据/位置不同；建议"浏览器第一道、服务端第二道"双保险 |
| 打断触发 | 前端 `triggerInterrupt()`（本地停播）+ WS `interrupt` | 后端 `<|PAUSE|>` → `control:interrupt` | 协议可互认；大白本地停播更快，应保留 |
| 打断后的模型上下文 | 仅取消，不回填"听到哪/被谁打断" | task.cancel + `[Interrupted by user]` + heard_response 回写历史 | **可移植改进点** |
| TTS 分片有序推送 | seq + worker 预生成队列（server.py 4344+） | seq + payload_queue 缓冲（TTSTaskManager） | 同构，无移植必要 |
| 口型数据 | audio_chunk 文本 → 前端自算口型 | audio payload 附 20ms RMS 音量数组 | 可选增强，非本任务必需 |

### 8.2 多模态输入差异（大白 ↔ OLV）

| 接口面 | 大白现状 | OLV（参考） | 移植动作 |
|---|---|---|---|
| 屏幕/剪贴板来源枚举 | 无 | ImageSource(SCREEN/CLIPBOARD/…) + ImageData | 新建 harness 工具按同构返回 data-url |
| 剪贴板文本 | 无 | TextSource.CLIPBOARD → 方括号包注 | 直接采用文案约定 |
| 视觉消息块 | 无（纯文本消息） | `image_url` content 数组 + detail:auto | 在 agent/工具的上下文注入点支持图片块，用 vision 供应商摘要后仍可回退纯文本 |
| 摘要压缩 | 工具长结果有压缩 | `[Tool returned N image(s)]` | 抄这条防爆上下文 |

### 8.3 Minecraft 状态接口（大白 ↔ airi）

| 接口面 | airi Minecraft | 大白对应缝 | 移植动作 |
|---|---|---|---|
| 游戏进程接入 | Mineflayer bot 进程（独立登录） | 无（既有游戏是浏览器 2D/3D） | 阶段 3 引入 mineflayer 子进程或外部状态桥（形态待定） |
| 状态快照 | status.ts / context-service（坐标/血/天气/时刻/同服玩家） | `environment_snapshot` / `game_context` 注入 | 新增 `skills/minecraft` 快照工具 + 周期事件源 |
| 上下文发布 | context:update ReplaceSelf 5s | WS `game_update`/`environment_snapshot` → PerceptionDispatcher | 复用既有 _PERCEPTION_EVENTS，无协议新增 |
| 聊天进出 | mineflayer `chat` + ChatMessageHandler 过滤 | WS text/audio 用户通道（语音由大白 TTS 出） | 需要"游戏内消息 ↔ 大白对话"桥（形态待定） |
| 事件规则 | perception YAML 规则（受伤/坠落/低血/潜行） | EventRule + dispatch 触发文本 | 抄规则集，适配 EventRule 配置 |
| 自主动作 | brain/planner/action + 沙箱 | RL/自主行为已存在（ai_action 通道） | 不移植认知堆栈 |

## 9. 阶段差距清单与建议技术路线

### 9.0 通用前提（每个阶段都遵守）

- 每阶段独立分支：`feat/phaseN-*`；settings.json 增加 `modules.<name>.enabled`（默认 false，读取处给默认值避免缺失报错）。
- 涉及 server.py/agent.py 先备份 `.bak-<ts>`，用最小补丁；新增技能尽量自成目录，核心只留注册/接线点。
- 日志统一命名 `modules.*`（logging.getLogger），落点 `logs/modules.log`（logs 目录需建）。

### 9.1 阶段 1 语音打断：差距与三档方案

差距：
1. 打断判据=能量阈值（误断/漏断）；
2. 无流式 STT 中间结果；
3. 打断无上下文回填；
4. 无 ≤500ms 验收的端到端埋点。

建议按验收成本选档（阶段 1 先做 A+B，C 为可选增强）：

- **A. 打断链路加固与埋点（必做）**：在 `triggerInterrupt()`（前端）记录 `t0=performance.now()`，本地 `currentAudio.pause()` 完成记录 `t1`，WS 回执记录 `t2`，输出 `[interrupt-latency] t0→t1→t2` 日志；把本地停播与"开口检测"之间的 VAD 参数做成前端可调（settings/UI）；把服务端 interrupt 分支加"已播文本回填"（把 `state` 里当前 reply 文本作为 heard 部分写历史，模型下次知道被谁打断）。验收：播放 3s+ 的长回复时插话，日志 t1-t0 ≤500ms。
- **B. 开口即断的"流式监听"（关键）**：把 VAD 自动模式从"等整段说完才处理"升级为——浏览器检测到说话即先停播（现有）；随后**开始边录边送**16k PCM 到新增 WS 消息 `audio_stream`（分块）；服务端在消息到达时先做轻量 VAD/能量判据，**不需要识别结果也能第一时间发 `interrupt` 兜底**（防浏览器侧漏判）；整段结束再走现有 `audio` 整段识别出文字回复。这样"开口→停止"总延迟可压到浏览器帧级（≈50-150ms），远低于 500ms 预算。
- **C. 真流式 STT 语义（可选）**：把 faster-whisper/API 接成流式（或分段增量）出中间文本；中间文本仅用于触发更精确的"开始说话/意图确认"，最终文本仍用整段精识别。是否做取决于 B 档能否满足体验（多数情况 B 已足够）。

（注：大白当前浏览器停播不依赖网络，理论上已能 ≤500ms；阶段 1 的目标是把该能力**可复现、可测量、抗误触发**，而不是推翻重做。）

### 9.2 阶段 2 屏幕/剪贴板感知：建议架构

新增技能 `skills/perception/`（skill.json + skill.py）：

1. 工具 1 `screen_capture(mode="active_window|full", max_w=1280, format="png")`：PIL ImageGrab（Windows 全屏/活动窗）+ pyautogui 兜底 → base64 data URL；PNG/JPEG 压缩控制体积。
2. 工具 2 `clipboard_read(max_chars=...)`：pyperclip / PowerShell `Get-Clipboard` → 纯文本；二进制/图片剪贴板转 base64 摘要或提示不可读。
3. 工具 3 `vision_describe(images[], prompt)`：把图片块发给 settings 中 vision 模型（现有 `prov-30914eb1`/`deepseek-v4-flash-vision-exp`，或其它支持 vision 的 OpenAI 兼容供应商），返回文字摘要。
4. 组合工具 `observe_screen()`（供 Agent 一次调用）：截图+剪贴板+可选视觉摘要 → 结构化返回 `{"screen_summary", "clipboard", "ts"}`，并**按 OLV 约定**把剪贴板包成 `[用户剪贴板：...]`、图片以 `image_url` 消息块注入，而不是全塞文本。
5. "主动开聊"策略：加一个低频周期钩子（复用 PerceptionDispatcher `proactive`/PERIODIC 纪律）：模块开启且用户空闲≥N 分钟、屏幕**变化显著**（前后帧哈希差）时才主动 `observe_screen` 并触发一句点评；默认只在用户明确说"看下我屏幕/我屏幕上是什么/帮我看看这个"时按需执行（`on_demand` disclosure，不撑爆上下文）。

隐私与合规：采集仅发生在模块开关开启且被用户触发/满足主动阈值时；截图不写入长期记忆库，视觉摘要可在当轮上下文使用并在轮后丢弃（如需记忆走 memory 的 recall 通道并打 source=screen 标签）。

### 9.3 阶段 3 Minecraft 陪玩：建议架构（待 Q3 确认形态）

参考 airi 只移植"状态/上下文/聊天"三件套：

1. 新增 `skills/minecraft/` 技能：工具 `mc_status()`（在线/坐标/血量/饱和度/模式/维度/天气/时刻/同服玩家）、`mc_chat_send(text)`（若 bot 形态）、`mc_listen_once()`。
2. 服务端接线：`enter_game_mode {game_key:"minecraft"}` 接入现有 GameEngine（把 world.game_type 描述为 minecraft 场景）；周期状态推 `environment_snapshot`/`game_update` 走 PerceptionDispatcher（改 `_PERCEPTION_EVENTS` 白名单即可）。
3. mineflayer 侧：新增独立子进程/worker `tools/minecraft_bridge/`（Node，依赖 mineflayer + pathfinder 等），通过本机 HTTP/WS/stdio 与 server.py 通信，只暴露**状态 + 聊天 + 只读感知**；控制类动作默认禁用（开关 `modules.minecraft.allow_actions`，默认 false）。
4. 连续对话：游戏内玩家消息 → 桥 → 大白以"游戏内消息"为 user 输入触发回复（复用 `_kickoff_response` allow_interrupt=True），大白回复文本转 TTS 同时经 `mc_chat_send` 进游戏；两轮以上对话自然在同一个 WS session/game_context 下连续。
5. 状态上报验收示例：玩家问"我现在状态怎么样" → Agent 调 `mc_status()` → 报坐标/血/模式；若 `modules.minecraft.enabled=false`，工具不存在/返回明确关闭提示，行为与现状一致。

### 9.4 阶段 4 整合回归要点

- 三开关同时开启；回归矩阵：文字对话、按住说话语音、VAD 自动、TTS 播放与打断、换角色/换场景、既有小游戏 enter/exit、技能工具、记忆存取、RL 状态。
- 复测 §10 基线（py_compile/启动/首句/接口延迟），对比偏差 ≤±30%。
- 生成 `logs/modules.log` 汇总启停与异常；出问题按分支回滚（见 TASK_SPEC §5）。

## 10. 性能基线与环境事实

### 10.1 基线测量（阶段 4 复测对象）

测量命令（2026-09-08，本机）：

| 指标 | 命令 | 基线结果 | 说明 |
|---|---|---|---|
| Python 语法/导入级静态检查 | `python -m py_compile server.py agent.py` | exit=0，耗时 208ms | Python 3.13.5 |
| Python 版本 | `python --version` | Python 3.13.5 | |
| 前端 TS 类型检查 | `npm run typecheck` / `node_modules\.bin\tsc --noEmit` | **无法执行**：node/npm 均不在 PATH，常见安装目录无 node.exe | 环境缺口，见 10.2 |
| 主仓库 commit | `git log -1`（main） | `937772c feat: 技能体系大合并…` | 12 commits ahead of origin |
| 参考仓库 commit | `git -C <temp> rev-parse HEAD` | OLV `992309c0…`；airi `f679616c…` | 浅克隆 HEAD |

阶段 4 还应复测的运行时指标（阶段 0 未启动服务以避免改变运行状态）：

- `python server.py` 冷启动到 `/api/info` 可用的秒数；
- WS `set_user → thinking → audio_end` 首句 TTS 端到端延迟；
- WS 播放中打断：`t0(开口) → t1(本地停播)` 毫秒（阶段 1 埋点输出）；
- `logs/modules.log` 行数与异常计数。

### 10.2 工具链环境事实（重要）

- 当前执行 shell（PowerShell）中 `node`、`npm` 均不可用；在 `C:\Program Files\nodejs`、nvm、scoop、用户目录、D:\AI 等常见位置做**有界搜索**均未找到 node.exe。说明项目前端的 TypeScript 构建/类型检查依赖某个未接入此 shell 的工具链（如 IDE 内置 Node 或独立开发机）。
- 影响：TASK_SPEC A1 的 `npm run typecheck` 验收在本机不可直接执行。对策：①python 侧以 `py_compile` 为主门槛；②TS 改动尽量小并以 `web/js/types/ws-protocol.ts` 类型账同步；③阶段 4 前与用户确认 node 接入方式（装 Node / 提供路径 / 用 IDE 检查），未确认前"typecheck 通过"改为"改动文件在无 node 环境下按协议类型账人工核对 + py 侧冒烟"。

## 11. 风险与未决问题

### 11.1 未决问题（需要用户决策后再进入对应阶段）

- **Q1（阶段 1 范围档）**：打断升级做到哪一档？A(链路加固+埋点+上下文回填，必做) / B(开口即断的流式 PCM 监听，推荐) / C(真流式 STT 中间结果，成本最高)。默认按 A+B 规划，C 视验收与成本决定。
- **Q2（阶段 2 视觉供应商）**：用 settings 里现成的 `prov-30914eb1`（opencode zen go, `deepseek-v4-flash-vision-exp`）还是另配硅基流动等多模态供应商？需用户确认该 profile 可用且 key 有效；截图先不落长期记忆是否可接受？
- **Q3（阶段 3 形态，最关键）**：Minecraft "同局"指哪种？(a) 大白以 mineflayer bot 身份登录**同一服务器**（能听到/回游戏内聊天、报 bot 自身状态，需要服务器地址与账号）；(b) 玩家本机玩（含单人），大白通过窗口截图/日志/Mod 桥感知玩家状态并陪聊（不占用游戏账号）。两种路线依赖完全不同，需在阶段 3 开工前确认。**建议优先 (a)**：airi 素材最直接、坐标/血量/聊天都可量化验收；若用户只想在单人本地游戏里被陪聊、无服务器/账号条件，再走 (b)（依赖阶段 2 视觉 + 额外状态桥）。
- **Q4（前端验证）**：node/npm 缺失如何处理（见 §10.2）。

### 11.2 风险登记

| 风险 | 等级 | 缓解 |
|---|---|---|
| 打断链路引入"AI 听到自己声音自打断"（回声） | 高 | 保留浏览器 echoCancellation/AEC + 人声评分过滤；打断场景缩短预滚 120ms；服务端 VAD 只作第二道并带最小持续时长 |
| 截图/剪贴板隐私 | 中 | 模块开关默认关；只读摘要不落库；主动采集有静默期与变化阈值；用户可随时关闭 |
| 大文件核心改动（server.py/agent.py） | 高 | 最小补丁、改动前 .bak 备份、每阶段独立分支、配置默认关可整体回退 |
| git 工作区已有大量未提交改动 | 中 | 不 merge/不回滚他人改动；只新增文件与极小 diff；阶段代码留在分支不强制合 main |
| airi/OLV 仓库演进（airi 明确弃用 mineflayer 路线） | 低 | 只抄接口/设计，不锁定依赖版本，不建立长期耦合 |
| node 工具链缺失导致前端验证不足 | 中 | §10.2 对策；改动前先与用户确认验证方式 |

## 12. 证据索引

### 12.1 大白本体（D:\AI\dabai）

| 证据 | 位置 |
|---|---|
| WS 主循环/消息类型白名单 | server.py:5312、5340-5353 |
| 用户 text / audio 输入分支 | server.py:5789 区段、5847 区段 |
| interrupt 处理 | server.py:5774-5785 |
| 流式回复 + TTS worker + audio_chunk | server.py:4344 起（_gen_tts_audio/_tts_worker/_send_audio_chunk） |
| TTS 引擎 | server.py:672 generate_tts；edge_tts 555、gptsovits 602、api 634 |
| STT 双路径 | server.py:965 local、1023 主入口、772 convert_to_wav |
| AIAgent/工具执行 | agent.py:1431 class、1228 execute_local_tool、1115 get_available_tools |
| harness schema/路由 | HARNESS.md:278-291；agent.py:1053 load_local_tools |
| harness API | server.py:6246 起 |
| 感知派发 | perception_dispatcher.py:190 dispatch、28 EventCategory、312 _passes_protection |
| 游戏引擎 | game_engine.py:30 GameWorld、627 GameEngine、1050 apply_environment_snapshot |
| 前端 VAD/打断参数 | web/js/core/01_start.ts:244-246；12_vad_auto.ts:451-485 |
| 前端播放与触发打断 | web/js/core/10_tts_lipsync.ts:345 triggerInterrupt、296 handleInterrupted |
| 前端录音/协议 | web/js/audio/11_voice_record.ts:61-80；types/ws-protocol.ts（ClientMessage 尾部 audio 消息） |
| 屏幕遗留 | screen_shot.py（整文件）；dabai.py:8 |
| 视觉/剪贴板缺失结论 | 全仓库检索 image_url/vision/clipboard 无实现命中（仅提示词设定 agent.py:3535、game_engine.py:266） |
| settings 内 vision profile | settings.json llm_providers：prov-30914eb1（deepseek-v4-flash-vision-exp） |
| 服务器入口 | server.py:6916-7024 uvicorn.run；dabai.bat `python server.py` |

### 12.2 Open-LLM-VTuber（%TEMP%\dabai_merge_refs\Open-LLM-VTuber）

| 证据 | 位置 |
|---|---|
| WS 消息类型 | src/open_llm_vtuber/websocket_handler.py:35-46（CONVERSATION/CONTROL/DATA）、86 起 handler map |
| 实时 VAD 音频流 | 同文件 `_handle_raw_audio_data`（VAD detect_speech → PAUSE→control interrupt / 语音块缓冲 / mic-audio-end） |
| interrupt 处理 | 同文件:369 `_handle_interrupt`；conversations/conversation_handler.py:112 individual/146 group |
| 打断上下文回填 | conversation_handler.py:112-145；agent/agents/basic_memory_agent.py:195-223 |
| TTS 有序队列 | conversations/tts_manager.py（speak/_process_payload_queue/_process_tts） |
| 音频分片+音量 | utils/stream_audio.py（prepare_audio_payload，20ms RMS） |
| 多模态输入模型 | agent/input_types.py:6-92（ImageSource/TextData/BatchInput） |
| OpenAI 兼容视觉消息 | agent/agents/basic_memory_agent.py:229-276（image_url 数组 + clipboard 文案） |
| VAD 配置（Silero） | vad/silero.py:14-33（16k/32ms/0.4 阈值/3 命中/24 miss） |
| 配置 interrupt_method | config_templates/conf.default.yaml:104-115 |

### 12.3 airi Minecraft（%TEMP%\dabai_merge_refs\airi\integrations\minecraft）

| 证据 | 位置 |
|---|---|
| 状态上下文服务 | src/airi/minecraft-context-service.ts（buildStatusText/bindBot/5s ReplaceSelf） |
| 低层状态 | src/libs/mineflayer/status.ts（position/health/weather/timeOfDay） |
| 游戏聊天过滤 | src/libs/mineflayer/message.ts（ChatMessageHandler） |
| 周期循环 | src/libs/mineflayer/ticker.ts（300ms 串行） |
| AIRI 事件缝 | src/airi/airi-bridge.ts（spark:command/context:update/module:announced/notify） |
| 入口装配 | src/main.ts（mineflayer + 插件矩阵 + cognitive + client） |
| 事件/规则 | src/cognitive/perception/events/index.ts + rules/*.yaml |
| 依赖/脚本 | package.json（mineflayer + 插件矩阵；dev 启动） |
| 配置模板 | .env（BOT_HOSTNAME/BOT_PORT/BOT_USERNAME/BOT_AUTH/BOT_VERSION…） |
| 弃用声明 | integrations/minecraft/README.md 顶部 Deprecation Notice |

---

报告完成。改动状态：仅新增 `TASK_SPEC.md` 与本报告；未修改任何既有代码。
