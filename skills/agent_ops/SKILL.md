# 智能体指挥（agent_ops）

派活、配人、查进展、造工具。触发：复杂/跨系统/多步骤任务、或用户点名外部智能体。

## ★ 默认派活铁律

**默认用 `sub_agent_spawn`（我自己的子智能体）**——自带 LLM+工具、可同时并行多个、不占任务中心名额、不动用户已确认的队列。

`delegate_agent_task`（DSH / Codex / OpenCode）**仅在用户明确点名时才用**（“让 DSH 查”“用 Codex 干”“交给 OpenCode”）。
用户没说用谁 → 一律 sub_agent_spawn。**绝不擅自替用户选外部智能体。**

## 工具

### 一、通用子智能体（★默认派活方式）
- `sub_agent_spawn(task, title?, note?, profile?)` 下发一个后台子智能体（LLM+工具自主执行，做完自动汇报）——**默认就用它**，多任务同轮发多个即并行
- `sub_agents_list(all?)` 查行踪：默认只列在跑的，`all=true` 列最近全部（含已完成/失败/已取消，带 worker_id）
- `sub_agent_status(worker_id)` 看单个的进度日志 + 结果 + 错误（已结束的也能查）
- `sub_agent_cancel(worker_id, reason?)` 收回

### 二、外部智能体（⚠ 仅用户点名时用）
- `delegate_agent_task` 委派给 dsh/codex/opencode——**默认不用**；用户明确点名才调（先产出任务规范：目标/范围/验收/步骤/回滚）
- `list_agent_tasks` 查看任务中心进展

### 三、智能体档案（配置子智能体的「人格 + 能力 + 预算」）
- `agent_profile_list` 列出可用智能体（内置 5 个 + 自定义）
- `agent_profile_show(profile_id)` 看完整配置（系统提示词全文 + 实际可用工具数）
- `agent_profile_create(profile_id, name?, system_prompt?, skills?, tools?, deny_tools?, model?, max_rounds?)` 新建
- `agent_profile_update(profile_id, …)` 改配置（内置也能改，改完对新派出的立即生效）
- `agent_profile_delete(profile_id, confirm=true)` 删自定义档案（内置 5 个不可删）

内置档案：`general` 通用执行者｜`coder` 核心开发｜`tester` 测试工程师｜`researcher` 调查员｜`writer` 文档写手。

一个子智能体 = **系统提示词**（怎么想）× **可用技能/工具**（能做什么）× **模型**（花多少）。档案就是把这三件事
收敛成一份可复用配置，落盘 `agent_profiles.json`。

- `skills` 技能白名单（逗号分隔，`all`=不限）；`code_ops`（读写文件/命令/检索）是**基本生存能力**，
  任何档案都隐式保留——白名单防的是越界，不是把人弄残（连文件都读不了的 worker 只会交一份失败汇报）。
  真要禁写文件，用 `deny_tools` 点名（支持 `code_edit`、`write_*` 通配）。
- `tools` 工具白名单：非空则只留这些名字（最精确）；留空=随技能。
- `model` 可覆盖模型名（同一 base_url/api_key，只换模型）——调查员配便宜快的、核心开发配最强的。

### 四、技能工坊
- `skill_dev_list/read/create/edit/write_file/validate/reload/remove`
- `skill_pull_search/inspect/install`（从 GitHub 检索、核查、安全安装）

## 规则
- ★**默认 `sub_agent_spawn`**；只有用户点名 DSH/Codex/OpenCode 才用 `delegate_agent_task`，绝不擅自选外部
- 点名 DSH 必须用 dsh；所有外部委派先请用户确认
- 画图与音乐绝不委派；委派前先查任务中心，同一任务连续失败两次以上停止自动重试
- 清理只删白名单临时文件，git 已跟踪文件禁删
- 派活前先 `agent_profile_list` 挑人：写实现→`coder`、写测试→`tester`、只读调查→`researcher`、写文档→`writer`

详细文档：references/guide.md
