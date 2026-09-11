# 大白融合改造（Open-LLM-VTuber × airi 模块移植）任务规范

> 任务发起：2026-09-08。本文件是唯一任务规范与检查点；改动代码前必须完整阅读，完成后逐条对照验收。

## 1. 目标

「大白」（D:\AI\dabai）保持单一灵魂（现有 WebSocket 主服务 + 3D 角色 + 统一 agent/harness），在其上新增三大能力：
1. 语音打断对话（用户开口 → TTS 立即停，播放中插话 ≤500ms 被打断）；
2. 屏幕/剪贴板感知（截屏+剪贴板 → 视觉理解 → 注入对话上下文）；
3. Minecraft 陪玩（移植 airi 的 Minecraft 模块，同局游戏内连续对话并能报游戏状态）。

同时三模块独立开关 + 统一日志 + 性能基线，全量回归不破坏现有能力。

## 2. 范围与不做

**范围**
- 只改 D:\AI\dabai；克隆的 Open-LLM-VTuber / airi 只放临时目录作参考，不进入主仓库。
- 阶段 0：先勘察真实结构并产出《阶段0 勘察报告》（PHASE0_RECON_REPORT.md），之后才允许改代码。
- 每个功能阶段建独立分支、配置开关（默认关闭），失败可整段回滚到上一阶段。

**明确不做**
- 不重写大白既有架构（server.py WS 主循环 / agent.py / harness / skills 体系保留）。
- 不搬运 Open-LLM-VTuber 或 airi 的整套工程，只移植经验证可行的模块/模式。
- 不替换现有 STT/TTS 供应商与角色人设；不改变 3D 渲染与表情动画体系。
- 不做 Minecraft 服务端/客户端开发，不自动开游戏、不注入外挂；只做"同局感知+陪聊+状态上报"通道。
- 不删除/不动工作区里与本次任务无关的未提交改动（git status 已有大量既有改动，保留原状）。
- 不整读超大文件（>300KB 的 server.py/agent.py 等一律用关键词/区间读）。

## 3. 验收标准（可执行命令或查看单文件验证）

总体（跨阶段，全部完成后执行）：
- A1 服务可启动：`python -m py_compile server.py agent.py` 通过且无新增 import 错误；前端 TS 改动在 node 可用时 `npm run typecheck` 通过；node 不可用时按 PHASE0_RECON_REPORT.md §10.2 的对策执行（协议类型账核对 + 改动最小化，阶段 4 前确认验证方式）。
- A2 三个新模块在 settings.json 有独立开关（`modules.voice_interrupt/screen_aware/minecraft`），默认关闭时对现有行为零影响。
- A3 统一日志：新模块日志统一写 `logs/modules.log`（或沿用统一 logger 命名 `modules.*`），`Get-Content logs/modules.log` 可查三模块启停记录。
- A4 性能基线：改造前先跑一次基线（启动耗时/首句回复耗时/WSS 内存）记录在 PHASE0 报告；阶段 4 复测对比，退化不超过 ±30%。
- A5 回滚：每阶段在独立分支上完成；`git branch` 可看到 `feat/phaseN-*`，开关关掉即回到上一阶段行为。

分阶段验收：
- P0 勘察报告存在且事实可核验：`PHASE0_RECON_REPORT.md` 含入口/技能体系/已有 ASR-TTS/可移植模块清单/接口分析五节，每节给出文件行号证据。
- P1 播放中插话 ≤500ms 被打断：代码含从"开口检测→停止播放→通知服务端取消"链路，前端日志/埋点可测出 ≤500ms；提供断点级说明与实测/模拟证据。
- P2 大白能就屏幕内容主动开聊：截屏+剪贴板工具注册进 harness，视觉理解结果可注入消息上下文；验收演示一轮"看屏幕内容→大白主动评论"（日志含注入的视觉摘要）。
- P3 Minecraft 同局连续对话+报游戏状态：`game_key=minecraft` 可进入状态；连续两轮对话不丢局内上下文；能读取/上报玩家坐标、生命、朝向等状态字段。
- P4 整合回归：三模块同时开启下，文字对话、语音输入、TTS、游戏模式（既有 game_key）回归冒烟通过。

## 4. 实施步骤（每步完成立即验证，不攒到最后）

### 阶段 0 勘察（当前）
1. 核实目录结构/入口/技能体系/已有 ASR-TTS（已验证：入口 server.py; harness + skills/*/skill.json; STT 全段录音→整段识别; TTS edge_tts 分句生成）。✔ 已做初步
2. 写本 TASK_SPEC.md。✔
3. 克隆 Open-LLM-VTuber 与 airi（临时目录，shallow clone）。
4. 精读两仓库：Open-LLM-VTuber 的流式/打断/ASR 架构与 barge-in；airi 的 Minecraft 模块（provider/module/事件/状态读取）。
5. 产出 PHASE0_RECON_REPORT.md：可移植模块清单 + 接口分析（含与大白现有接口的映射与差异）。
6. 自检：报告各节有证据；不动任何代码。

### 阶段 1 语音打断
1. 建分支 `feat/phase1-voice-interrupt`；settings.json 加 `modules.voice_interrupt.enabled`（默认 false）。
2. 在 P0 报告基础上列差距清单：现有为"前端能量 VAD + 全段录音 STT"，需补齐/改造的具体点。
3. 实现（前后端小步改，每步 py_compile / npm run typecheck / WS 冒烟）：
   - 若差距在打断链路延迟 → 收紧前端阈值/增加开口即断分支；
   - 若验收要求"流式 STT 语义" → 引入流式 STT（可选 faster-whisper 分段/供应商流式），边听边断。
4. 自检 P1：模拟/实测"播放中开口→停止"时间戳，产出 ≤500ms 证据。

### 阶段 2 屏幕/剪贴板感知
1. 建分支 `feat/phase2-screen-awareness`；开关 `modules.screen_aware.enabled`（默认 false）。
2. 新增技能 `skills/perception/`（skill.json + skill.py）：工具 `screen_capture`、`clipboard_read`（PyAutoGUI/PIL/pyperclip，视需求）。
3. 视觉理解：复用 settings.llm_providers 里的视觉模型（现成 deepseek-v4-flash-vision-exp），把截图编码送视觉 LLM 得到文字摘要。
4. 注入对话：把摘要注入 agent 消息/系统上下文（技能工具调用路径），配置主动评述触发策略。
5. 自检 P2：演示看屏幕→大白主动开聊；日志含视觉摘要。

### 阶段 3 Minecraft 陪玩
1. 建分支 `feat/phase3-minecraft`；开关 `modules.minecraft.enabled`（默认 false）。
2. 移植 airi Minecraft 模块的状态采集/事件通道到本仓库形态（不整套搬）。
3. 接入 `game_key=minecraft` 到既有 GameEngine / WS enter_game_mode 流程，保持既有游戏不被破坏。
4. 连续对话上下文：局内状态经 memory/上下文注入，保证多轮连续。
5. 自检 P3：进入 minecraft 模式、连续两轮对话、状态上报各验一次。

### 阶段 4 整合回归
1. 建分支 `feat/phase4-integration`；三开关同时打开做全量回归（文字/语音/打断/TTS/既有游戏/技能）。
2. 统一日志与性能复测（对比 P0 基线）。
3. 自检 A1–A5，写验收核验块。

## 5. 风险与回滚

- **风险**：server.py（323KB）/ agent.py（209KB）为核心大文件，且工作区已有大量未提交改动 → 改动前先 `git diff <file> | Measure-Object` 确认面；只做最小补丁；绝不动既有未提交内容。
- **风险**：全段录音链路改动可能破坏现有语音对话 → 配置开关默认关；开关关=旧路径原样。
- **风险**：前端 TS 大改破坏 3D/UI → 每步 npm run typecheck；前端文件同样以补丁小步改。
- **风险**：截图/剪贴板有隐私性 → 仅用户触发或模块开关开启时采集；摘要不进长期记忆库除非显式允许。
- **回滚**：每阶段在独立分支上开发；出问题 `git checkout main` + 关配置开关即回到上一阶段；不合并进 main 直到该阶段验收通过。
- **备份**：涉及 server.py/agent.py/settings.json 的改动先备份副本（.bak-<ts>）再改。
