# 大白 Harness —— 稳定的运行时 + 技能 / 插件扩展框架

「大白」已经从单一的陪聊程序升级为**稳定的智能体运行时**：Agent 本体的
全部执行链路（LLM 调用、工具执行、记忆提取、游戏决策）都运行在
**harness 监督运行时（AgentRuntime）** 的控制之下，长任务与批量任务由
**harness 任务系统（TaskSystem）** 统一执行（持久化断点续跑），并通过
**技能（Skill）** 与 **插件（Plugin）** 两套机制持续扩展能力，无需改核心代码。

- **完全监督**：每一次 LLM 调用与工具执行都经过 harness —— 瞬时错误自动退避重试、
  整体超时保护、连续失败自动熔断（冷却后半开探测恢复）、全量调用计量与 token 记账；
- **长任务 / 批量任务**：多步骤 DAG 流程与批量 map 后台执行，步骤级重试退避、
  流程级看门狗、失败策略（abort/continue）、**服务重启断点续跑（已成功步骤不重算）**、
  批内并发受控、部分失败容忍；
- **队列统摄**：每队列独立 worker 池、任务优先级、启动限速、暂停/恢复；
  全部任务/队列在管理台与 REST API 统一查看、取消、重试；
- **高效**：同一轮多个工具调用自动**并行执行**；一次对话轮就是一个
  RunSpan，在途/最近运行、耗时、轮数、工具数全程可观测；
- **稳定**：工具路由分层、失败的技能/插件/工具被隔离熔断、绝不影响主流程；
  监督层自身永不成为故障源（harness 不可用时自动退化为原有裸调用行为）；
- **可扩展**：放一份文件进 skills/ 或 plugins/ 目录，重启（或点一下热重载）即获得新能力；
- **可管理**：网页管理台 /harness 与 REST API /api/harness/* 实现热启停、热重载、
  健康状态查看、熔断器复位、任务与队列控制。

---

## 1. 架构总览

```
大白 主 Agent（agent.py AIAgent）—— 唯一智能体底座
 ├── 人设 + function calling + 记忆（所有自然语言请求的唯一决策者）
 ├── LLM 调用（chat/game/decision/character_line/memory/plan 六个渠道）
 │     └── 全部经 _retry_create → harness.runtime.supervise_llm（熔断+重试+超时+计量）
 ├── 工具执行（技能/插件/内置 兜底，已全面 skill 化）
 │     └── 全部经 _supervised_tool → harness.runtime.supervise_tool（熔断+超时+计量）
 │           同一轮多个工具调用 → asyncio.gather 并行执行
 ├── 对话轮（chat_stream）
 │     └── RunSpan 追踪 + UsageEvent → record_usage token 记账（含缓存命中口径）
 └── harness/                        稳定扩展与控制层
      ├── core.py     Harness 门面：工具收集 / 稳定路由 / 健康状态 / 热重载
      ├── runtime.py  Agent 监督运行时：熔断器 / 重试 / 超时 / 计量 / RunSpan / 用量
      ├── tasks.py    任务系统：队列调度 / DAG 流程 / 批量 / 持久化断点续跑
      ├── skills.py   技能注册表（skills/<名称>/）
      ├── plugins.py  插件管理器（plugins/<名称>/）
      └── state.py    启停状态持久化（harness_state.json）

外部智能体 = 普通技能，无特殊通道：
  codex / opencode / DSH → skills/agent_ops（delegate_agent_task，经任务中心确认）
  本机命令行操作        → skills/shell（shell_run / find_file：危险拦截 + 真实路径查找）

已移除：旧"分流 LLM"路由层与全部手动斜杠命令（/ai /cx /cmd /bg /tasks ...）——
分流对每条消息额外消耗一次 LLM 调用且闲聊回复被丢弃（纯浪费），臆造文件路径导致
执行失败、失败即停不反思。现在主 Agent 逐条调用工具并查看结果，天然具备逐步
反思能力；路径查找由 find_file 技能承担；codex / opencode / DSH 仅作为
agent_ops 技能中的委派工具存在，统一由 harness 底座处理。
```

工具执行的路由顺序（agent.py 的 execute_local_tool）：

```
harness 技能 → harness 插件 → fuctions_all_you_need_base 兜底
```

技能/插件提供的工具会**自动合并进 load_local_tools()**，供 function calling 使用；
它们注入的提示词片段会**自动拼进 system prompt**，让模型知道新能力怎么用。

---

## 1.5 Agent 监督运行时（harness/runtime.py）

Agent 初始化时注册进运行时（`register_agent`），此后所有执行受监督：

| 能力 | 说明 |
|------|------|
| LLM 监督 supervise_llm | 熔断 → 瞬时错误重试（默认 3 次 1s→2s）→ 整体超时（默认 180s）→ 按渠道计量（chat/game/decision/character_line/memory） |
| 工具监督 supervise_tool | 熔断 → 超时（默认 30s）→ 按工具计量；失败以错误文案返回，不抛异常打断对话 |
| 熔断器 CircuitBreaker | 连续失败 3 次跳闸，60s 冷却内快速失败；半开态放一次探测，成功即自动恢复 |
| 并行工具执行 | 同一轮的多个相互独立的工具调用经 asyncio.gather 并行执行（harness.runtime.parallel_tools 可关） |
| RunSpan | 每轮 chat_stream 一个运行追踪：在途数、耗时、LLM 轮数、工具次数、成功与否 |
| token 记账 | UsageEvent 统一记入 runtime.record_usage，分渠道累计，管理页可见 |
| 降级安全 | harness 不可用时自动退化为原有裸调用行为；监督代码绝不阻断主流程 |

监督参数可在 settings.json 覆盖（均有默认值）：

```json
"harness": {
  "runtime": {
    "llm_retries": 3, "llm_backoff": 1.0, "llm_timeout": 180,
    "tool_timeout": 30, "breaker_failures": 3, "breaker_cooldown": 60,
    "parallel_tools": true
  }
}
```

运行时观测 API：

| 方法 | 路径 | 说明 |
|------|------|------|
| GET  | /api/harness/runtime | 运行时快照（LLM/工具计量、熔断器、在途/最近运行、token） |
| POST | /api/harness/runtime/reset | 手动复位熔断器，body `{"name":"llm:chat"}` 或 `{"name":"tool:music_search"}` |

（/api/harness/status 的返回中也附带同一份 `runtime` 快照。）

---

## 1.8 任务系统（harness/tasks.py）—— 长任务 / 批量任务 / 队列统摄

对标的业界实践（Celery / Temporal / LangGraph 的核心子集），按大白单体形态裁剪。

### 队列调度

- 每队列独立 worker 池（并发上限），任务优先级（1 最先），启动限速（min_interval）；
- 队列可运行时暂停/恢复（控制 API + 管理页按钮）；
- 失败任务按 2s→4s 指数退避重新入队，耗尽重试进入终态 failed（dead）。

### 长任务（Flow，DAG 流程）

```
harness_flow_submit(name, steps, policy="abort", timeout=3600)
steps 每步：{"id": "...", "deps": [...], "action": "...", ...}
  - {"action":"tool",  "tool":"music_search", "args":{...}}      工具步骤（经 harness 路由 + runtime 监督）
  - {"action":"llm",   "system":"...", "prompt":"{{s1.result}}"} LLM 步骤（plan 渠道，走监督运行时）
  - {"action":"batch", "tool":"...", "items":[...], "concurrency":3}  批内并行
```

- 依赖自动编排（拓扑校验、环拒绝），无依赖分支自动并行；
- `{{步骤id.result}}` 把前步结果注入后步的 prompt/args（长文本自动截断）；
- 步骤级 timeout / max_attempts，流程级整体看门狗；
- 失败策略：`abort`（默认，任一步失败全部中止）/ `continue`（只放弃失败分支）。

### 批量任务（Batch）

`harness_batch_submit(name, tool, items, concurrency=3)`：把一个工具并行 map 到
N 组参数；批内并发信号量受控；单条失败不影响其它条目；结果按序聚合、
失败明细单独给出。

### 规划与防错（对"跑一半出问题"的系统性回答）

用户的三言两语 → 稳定执行，中间有六道防线：

1. **规划器（harness_flow_plan / TaskSystem.plan_flow）**：把用户简短请求交给
   规划 LLM（plan 渠道），先**复述对任务的理解**（目标/范围/交付物），再拆解为
   最小步骤；规划 prompt 自动携带全部可用工具目录（名称+一句话说明），
   规划结果经同一套校验后返回给模型复核，或 autostart 直接开跑。
2. **规划自评（planner critique）**：规划产出后再过一遍批评器——找风险、遗漏、
   更优路径、应加确认门的危险动作；verdict=revise 时给出修订版（修订版必须
   通过同一套校验，否则保留原方案——宁可稳）。默认开启。
3. **提交即校验（fail-fast）**：工具名必须存在于 harness 工具目录；真实笔误
   （编辑距离 ≤2 的唯一最近候选）自动纠正并记录；查无此工具/缺必填参数/参数
   类型不符**当场拒绝**并给出建议——绝不带病开跑。批量任务同样校验。
4. **结果反查（result guard）**：工具"成功返回"但内容是系统级错误文案
   （"技能不存在或未加载"等）→ 自动识别为步骤失败，垃圾结果不流入下游。
5. **确认门（confirm gates）**：危险动作（删除/覆盖/发送/花钱）在步骤上声明
   `confirm:"危险性说明"` → 执行到该步暂停为"等待确认"，经对话工具
   （harness_task_confirm）或管理页/REST 批准后才继续；拒绝则按失败策略收场。
   等待状态持久化，重启续跑后仍需确认；任务重试后危险步骤需重新确认。
6. **全程不失忆**：goal（用户原话）随流程持久化；每个 llm 步骤执行时自动注入
   【任务目标 + 当前进度 + 已完成步骤结果摘要】——流程跑到第 10 步、甚至服务
   重启断点续跑之后，每一步仍然知道自己为什么而做。

### 行为自洽与防钻牛角尖（主对话智能体）

任务系统防的是"跑一半出问题"；主 Agent 的日常对话里同样有长任务——一次对话
可能连续调用几百上千次工具。这部分由 **skills/behavior_strategy** 技能注入
system prompt（无工具、纯规则，随 harness 热重载生效），九条守则：

1. **目标锚定**：动手前与进行中反复对照用户最初的目标，每步先问"这一步是否
   还朝着目标"，跑偏先拉回再继续；
2. **以真实结果为准**：不凭记忆硬撑，关键事实以文件、命令输出、工具返回为
   准，记不清就重新读取核对；
3. **防钻牛角尖**：同一方向连续失败或收益递减时先评估该步是否必需——非必需
   就放掉或换更简单路径，必需就换思路，不无限重试；
4. **及时止损汇报**：被卡住时先穷尽合理自查，仍无解就把卡点、已试方案与可选
   方向清楚告诉用户，不默默反复尝试；
5. **成本意识**：能并行就并行、能复用就复用，不做无意义等待或重复轮询。
6. **顺手修复三分法**：遇到报错或 bug 先分类——挡路的（阻塞当前任务）当场
   修复并告知；自己正在改的文件里的小伤顺手修掉并告知；无关或冒险的（删数据、
   改行为、大改动）不擅自处理，记下并告知用户。
7. **意图优先**：先想清三件事——用户真正要什么、他没想到的坑是什么、做完后
   他下一步要干什么；然后只在任务范围内补上字面请求与真实问题之间的缝隙，
   不靠加戏；
8. **写完必验**：写出来的东西默认不可信，跑一遍才算数——小改动轻量检查，
   大改动完整验证；最后站在用户角度回看"问题真的被解决了吗"；
9. **快而好的节奏**：先打通主干再打磨细节，收益递减就停；用户看不到工具调用与内部思考，
   只能看到输出的文字——所以不播报无信息量的进展（「我来看看」这类），但每完成一个阶段
   或有启发性发现，用一句话说清拿到了什么、意味着什么，而不是只在出结论、卡住或需要
   决策时才开口。

这套守则与任务系统的"全程不失忆（goal 持久化）"、"反思上限（reflection_cap）"
、"LLM 预算（llm_budget）"互补：前者约束主 Agent 的即席执行，后者约束
harness 长任务。

### 理性决策增强（对标一流 agent 的六项差距补齐）

1. **反思看全文**：故障决策器收到失败步骤的 600 字完整错误与失败前部分产出
   （此前仅 200 字摘要）——根因藏在长输出里也能找到。
2. **跨任务经验库**（harness_task_memory.json，自动提炼/去重/保 60 条）：
   工具名常见笔误、彻底失败的用法（工具+参数+错误）、反思救回的成功路径、
   批量大面积失败模式——自动注入规划器与反思器 prompt，踩过的坑不再踩第二遍。
3. **expect 语义校验**：关键步骤可声明 `"expect":"结果应满足什么"`——执行后由
   校验器（150 token 小调用，计入 LLM 预算）复核"答没答到点上"，不满足按步骤
   处理（可重试/触发反思）；校验器故障时放行，宁可漏判不误杀。
4. **结构化澄清**：规划器发现关键信息缺失（对象/范围/偏好）时输出 questions
   而不是猜测；技能层带 autostart 也不开跑，把问题带回模型去问用户。
5. **完成实时推送**：流程/批量到终态即经 WebSocket 广播（type=harness_task），
   前端 toast 即时提示——不用等模型下一次轮询才知道做完了。
6. **重试去重**（测试中揪出的深层 bug）：修复了步骤重试期间协调器重复提交
   同一步骤导致工具被重复执行的缺陷——重试期间父流程状态不再被误重置。

### 失败决策：反思重规划（adaptive reflection）

步骤重试耗尽后**不是立刻放弃**，而是先请故障决策器（plan 渠道）评估：

- `revise`：换工具/换参数/换路径绕过——修订剩余步骤（保留已成功/运行中步骤与
  结果，修订必须通过校验与依赖检查），流程继续；
- `abort` / `continue`：确实无法达成/失败分支可放弃——按决策收场；
- 反思次数受 `reflection_cap`（默认 3）限制，修订无效也计数——杜绝反思循环；
  无 LLM 执行器时自动退回静态 abort/continue 策略。

另有两道防失控闸：**LLM 预算**（单流程 llm 步骤调用总量上限 `llm_budget`，
默认 40，超出即取消后续 llm 步骤并明确失败）；**流程整体看门狗**（timeout）。

### Token 效率（实测）

**前缀缓存命中率优化**（提供方按请求前缀命中 KV 缓存，**命中价 = 非命中的 1/50**）：

| 优化项 | 做法 | 实测效果 |
|--------|------|----------|
| 静态前缀最大化 | 系统提示词只放逐字节稳定内容（人设/称呼/资源清单/技能说明） | 大前缀逐轮稳定 |
| 易变内容后置 | 易变状态（形象/场景/音乐、游戏上下文）、按当前消息检索的召回、最近一次工具执行摘要，全部排在 history **之后**（此前排在 history 之前——每轮一变就作废整段历史，而历史占上下文 93%） | 变化只报废自己 |
| 用户长期记忆稳定排序 | `ORDER BY importance DESC, id DESC`（原按 last_accessed 排序且读取即刷新——顺序每轮重洗，其后全部缓存报废） | 记忆块稳定 |
| 历史滞回窗口（**主路径**） | ctx 打包出来的历史外面再包一层滞回视图（`AIAgent._stable_history_messages`）：平时只把新消息接到尾部；锚点用「role + content 前 80 字符 + tool_calls 条数」抗截断（最新轮不截字符、变旧后被截，拿全文当锚点会把「正常变旧」误判成换会话）；累计超 `HIST_VIEW_MAX_TOKENS`(16k) 才一次性截回一半，切点落在 user 上不拆散 tool 配对 | 修前 `tools/cold_audit.py` 实测：12 轮中 6 轮断点在 `history[0]`（最旧一条滑出 → 其后整段历史每轮作废），可复用仅 2.4k 字符（sys+常驻记忆），作废 51k~324k 字符；修后前缀只追加 |
| 历史滞回窗口（**回退路径**） | `AIAgent._stable_history_view`：视图超 12 轮才截到 8 轮，锚点匹配防串会话 | 仅分层打包不可用（`ctx is None`）时生效 |
| 缓存口径上报 | usage 的 prompt_cache_hit/miss_tokens 全链路采集（chat/game/plan），runtime 记账 | 管理台 Agent 运行时面板实时显示各渠道命中率 |
| 每轮落盘诊断 | data/turn_metrics.jsonl 记 cache_hit/miss、llm_calls、cold_miss、tool_chars、各段字符数（sys/summary/memory/hist/dyn/recall/work）、sys_prompt 指纹 | 命中率改动可验证、Δ 可定位 |

**⚠ 目标函数不是命中率**。计费 = hit×0.02 + miss×1.0，代入 hit = P − miss 得：

    每次调用成本 ≈ 0.02×P + 0.98×Δ      （P = 本次 prompt，Δ = 本次新增）

Δ 的边际成本是 P 的 **49 倍**——未命中几乎按全价计费。而命中率 = 1 − Δ/P 是
混淆指标：把 P 撑大同样能把命中率从 68% 抬到 96%，成本却只增不减。
所以优化方向只有一个：**压小 Δ**（工具结果注入量是 Δ 的大头）。
`turn_metrics.format_summary` 会直接给出「追 99% 的成本 vs 当前稳态成本」的对比。

其余开销控制：任务技能 schema 常驻 ≈1.7k tokens/轮（白名单可关）、规划器 ≤2 步跳过
自评、status 紧凑轮询省 72%、完成汇报队列免去反复轮询、llm_budget=40/反思上限 3/
模板与结果截断等硬上限兜底。plan 渠道 token 与缓存命中率在管理台可查。

### 持久化与断点续跑

- 流程/批量定义与**每个步骤/条目的完成状态实时落盘**（harness_tasks.json，原子写）；
- 服务重启（含热重载自动重启）后自动恢复未完成任务：已成功步骤结果直接复用，
  未完成部分补跑（at-least-once 语义）；
- LLM 步骤的执行器由 Agent 启动时注册，重启窗口内任务系统会等待注册而不是立刻失败。

### 控制与观测

| 方法 | 路径 | 说明 |
|------|------|------|
| GET  | /api/harness/tasks?state=&kind=&limit= | 任务列表 |
| GET  | /api/harness/tasks/{id} | 任务详情（步骤/条目粒度状态与结果） |
| POST | /api/harness/tasks/{id}/cancel | 取消（流程连带取消未完成步骤） |
| POST | /api/harness/tasks/{id}/retry | 重试（保留已成功步骤，只补跑失败部分） |
| POST | /api/harness/tasks/{id}/approve \| /reject | 批准/拒绝等待确认的危险步骤 |
| GET  | /api/harness/queues | 队列状态（worker/排队/运行/暂停） |
| POST | /api/harness/queues/{name}/pause \| /resume | 队列暂停 / 恢复 |

大白模型侧通过 **skills/tasks** 技能提交与操控（harness_flow_submit /
harness_batch_submit / harness_task_status / harness_task_list /
harness_task_cancel / harness_task_retry），管理页「任务与队列」面板可视化。

### 配置（settings.json 的 harness.tasks 段，均有默认值）

```json
"harness": {
  "tasks": {
    "queues": { "default": {"workers": 4}, "batch": {"workers": 6}, "flows": {"workers": 4} },
    "default_timeout": 600, "flow_timeout": 3600, "max_attempts": 2,
    "backoff": 2.0, "batch_concurrency": 3, "item_timeout": 120
  }
}
```

---

## 2. 技能系统（Skill）

一个**技能** = skills/<技能名>/ 目录，包含：

| 文件 | 是否必填 | 作用 |
|------|---------|------|
| skill.json | 必填 | 清单：name/title/version/description/author/enabled/prompt/tools |
| skill.py   | 可选 | 工具实现：TOOLS / PROMPT / HANDLERS 或 execute；生命周期 on_load/on_unload |
| SKILL.md   | 可选 | 技能说明文档 |

### 2.1 纯配置技能（不需要写代码）

只要 skill.json，工具执行可委托已有能力。示例见 skills/weather/。

### 2.2 带实现的技能（推荐）

技能能做的三件事，示例见 skills/filesys/：

1. **注入提示词**：skill.json 的 prompt 或 skill.py 的 PROMPT；
2. **注册工具**：skill.json 的 tools 或 skill.py 的 TOOLS（OpenAI function 格式）；
3. **实现执行**：skill.py 里放一张 HANDLERS 表（工具名 → async/普通函数），或用单一分发器 execute(name, args)。

```python
# skills/my_skill/skill.py
HANDLERS = {
    "my_skill_hello": async def hello(args): ...,
}
# 生命周期钩子（可选）
def on_load(ctx): ...
def on_unload(ctx): ...
```

> 想在管理页看到技能、在对话里被模型调用，工具名建议带前缀（如 filesys_list_dir），
> 避免和其它技能/插件重名冲突。

### 2.3 渐进式披露（Progressive Disclosure）

技能多了以后，把所有说明一次性塞进 system prompt 会膨胀上下文。
「大白」支持**渐进式披露**：每个技能可声明披露级别，按需披露的技能只在
system prompt 里保留一句话摘要，模型需要时用内置 **skill_help** 工具拉取完整说明书。

#### 技能清单里声明披露级别

```json
{
  "name": "filesys",
  "disclosure": "on_demand"   // "full"（默认，全量注入）或 "on_demand"（一句话摘要 + 按需拉取）
}
```

#### 全局开关（默认关闭，保持原有全量行为）

```json
"harness": { "progressive_disclosure": true }
```

开启后：
- **`full` 技能**：完整 prompt 照常注入 system prompt（行为不变）；
- **`on_demand` 技能**：只注入一句话摘要（标题 + 一句话介绍 + 工具名列表），
  完整说明书（SKILL.md 优先，其次 prompt + 工具参数）通过内置工具 `skill_help("技能名")` 按需获取；
- 内置 `skill_help` 工具始终注册；模型调用 `skill_help` 读完说明书后，该技能的
  全部工具会**立即注册为可调用**（同轮后续请求即生效，热重载后也保留），
  真正实现「按需启用」而不是只披露不启用，避免「看得见却用不了」。

> **2026-08-31 会话级技能保持**：`on_demand` 技能的工具注册以**会话**为粒度
> 共享——同一会话内跨轮保持，不再每轮卸载重查（此前每轮清空激活集，模型
> 下一轮调工具会反复收到「未注册，请先 skill_help」错误并重读说明书，
> 实测单轮连续报错 3 次，效率损失明显）。
> 具体机制（`agent._stabilize_tools_for_new_turn`）：
> - 新轮只重建缓存的基础工具（零磁盘 IO）+ 重挂本会话已激活技能的工具；
> - 重挂顺序由 `agent._skill_order`（激活先后）决定，**不能交给 set 的迭代顺序**：
>   同一批技能换个顺序，序列化出的 tools 数组就不同，整条前缀照样全废；
> - 注册总量有上限（`settings.json -> agent.max_active_tools`，默认 **200**），
>   超出按「最久未使用」逐技能淘汰；
> - 新建会话 / 切换角色卡片时调用 `agent.reset_session_skills()` 清空激活集，
>   旧会话的技能不串到新会话。
>
> > **2026-09-01 上限从 48 提到 200**：实测 `turn_metrics.jsonl` 里工具集在
> > **48 ↔ 1** 之间反复跳（16 轮里跳 7 次），每次跳都把整条前缀（含全部历史）
> > 废掉 200k+ 字符。根因不是「激活了什么」，而是**上限正好卡在
> > 1（skill_help）+ 47（code_ops）= 48**：只要再激活任何一个技能，LRU 就会把
> > code_ops 的 47 个工具整段删掉。
> >
> > 算一遍账：删掉 47 个工具 → 一次截断 ≈ 200k 字符全价；留着它们 → 23.5k 字符
> > 每轮按缓存价（≈1/10）计费。**一次截断 ≈ 白留 85 轮**，所以「删」永远比
> > 「留」贵。上限的作用是防工具 schema 无限膨胀，不是省那点常驻开销——
> > 必须远高于实际会用到的技能总量（全部技能合计 66k 字符）。
> > 工具集变化时落盘 `data/tools_trace.jsonl`（reason = activate / evict / reset），
> > 出问题直接指认是谁改的。
> >
> > > **2026-09-01 诊断日志加来源标记**：`data/hist_view_trace.jsonl` /
> > > `data/tools_trace.jsonl` 每行带 `tag` 字段——`run` = 真实运行，
> > > `test` = 自检/探针造的合成样本（`agent_efficiency_check.py` 启动即设
> > > `DABAI_TRACE_TAG=test`）。
> > > 为什么必须有：58 条样本里分不出哪条是现场，导致「滞回在真实路径是否生效」
> > > 一直定不了论。**自检绝不能污染真实观测。**
> > > 看日志用 `python tools/trace_report.py`：默认只统计 `tag=run`，没有就直说。
> > >
> > > > **2026-09-01 干净样本反而暴露真问题**：加 tag 后重跑 `trace_report`，
> > > > 5 条 `tag=run` 里 2 条是 `reset_sid`，**sid 却没变**
> > > > （`05:08:15` 与 `05:09:06` 同为 `73e3be897d53`，`prev` 却是 0）——
> > > > 同一会话被当成新会话重建 = **滞回跨轮根本没生效**。
> > > > 两种可能（视图被清 / 换了实例）现有字段分不出，于是补
> > > > `inst`（实例 id 后 4 位）+ `pid`，并在 `AIAgent.__init__` 落一行
> > > > `instance_new`；`trace_report` 增「实例数」「前缀断裂」两栏。
> > > > 教训：**先把「分辨」做出来，再谈修**——上一轮就是没分辨就下结论才返工。
> > > > 另外观测字段一律 `getattr(self, ..., "")` 容错：
> > > > 单测用 `__new__` 造实例无 `__init__`，trace 绝不能反过来弄挂主路径。
> > > >
> > > > **2026-09-01 摘要块前移是每轮清零缓存的元凶**：断点探针的分布只有三种
> > > > 位置——`brk=1`（摘要块变，白烧 68k~108k 字符）、`brk=3`（history[0] 变，
> > > > 滞回截断，白烧 22k~82k）、`brk≈50`（history 末尾，只废 2k~16k，健康）。
> > > > 对照证据：摘要长度逐轮在 818/41/541 之间跳变，摘要为空的那几轮断点都在
> > > > ~50，有内容的轮次断点全在第 1 条。
> > > > **摘要被当成「静态大前缀」排在 history 之前，而它其实每轮都变**——它一变，
> > > > 其后整段 history 每轮按全价重算。修法：移入易变尾巴区（history 之后），
> > > > 报废范围缩到它自己加尾巴。主路径与旧组装回退路径同步改。
> > > > 教训：**「稳定块」必须由实测认定，不能按名字认定**——叫「长期摘要」不等于
> > > > 每轮一样。分段顺序的唯一判据是「它上一轮到这一轮到底变没变」。
> > > > >
> > > > > **2026-09-01 补上「判定依据」**：`inst` 之后仍有一个洞——日志只记**当前**
> > > > > sid，而 `reset` 判定比的是**上一轮记下的** sid。于是两行 `reset_sid` 的 sid
> > > > > 看着一样，实际判定依据没被记下来，还是要猜。现在 `_trace_hist_view` 多落一个
> > > > > `psid`，取自 `_hist_msgs_sid`（判定用的就是它，不另存一份，避免观测值与
> > > > > 判定值分叉），`trace_report` 据此做「重置归因」：
> > > > > `psid` 空 = 新实例/进程重启（正常）；`psid≠sid` = 会话切换（预期）；
> > > > > `psid=sid` = 同会话却清了视图（真 bug，只有这种要修）。
> > > > > 教训：**记日志要记「判定依据」，不是记「当前值」**——只记结果永远定不了责。
> > > > >
> > > > > **2026-09-01 断点归因排行：先看「哪种原因贵」，再看「哪轮贵」**（新增
> > > > > `tools/break_attrib.py`）。46 轮真实样本，白烧合计 2.9M 字符：
> > > > > `history[N]`（窗口起点滑动）24 轮 57.2%；`sys_prompt` 3 轮 25.9%
> > > > > （平均 249k/次，全是早期的技能激活改首条，已修）；`summary_block` 4 轮
> > > > > 13.8%；`memory_block` 1 轮 3.0%。
> > > > > 教训：**逐轮明细看得见「哪轮贵」，看不见「哪种原因贵」**——而修哪里完全
> > > > > 取决于归因排行。只看单轮数字会一直猜。
> > > > >
> > > > > **2026-09-01 历史视图必须跨进程存活**：视图是进程内状态，重启就丢；
> > > > > 丢了只能从打包结果重建，而打包器每轮按 token 预算「新→旧重挑」、
> > > > > 落点会变 → 新头部与上一进程不同 → 断点回到 `history[0]` → 其后整段
> > > > > 历史白付全价。交叉验证（`break_attrib.py --corr`）：坏轮次（brk≤3）
> > > > > 全都挨着 `instance_new`/`reset_sid`，单轮白烧 68k~324k；视图连续的
> > > > > 轮次断点稳定在 `history[45~53]`，只废 2k~16k——差两个数量级。
> > > > > 修法：`data/hist_view_state.json` 原子落盘（写 .tmp 再 replace），
> > > > > 重启后同 `sid` 直接接回视图（`mode=restore`）；sid 不一致才重建。
> > > > > 自检/探针（`tag=test`）一律关闸，不碰现场状态文件。
> > > > > 教训：**「重启」不等于「换会话」**——把进程生命周期当成会话边界的写法，
> > > > > 每次重启都要重付一遍全价前缀。
> > > > >
> > > > > **2026-09-01 终审：滞回在真实路径生效，无 bug。** `psid` 上线后的现场样本
> > > > > （11 条 run）给出定论：4 条 `reset_sid` 全部落在**实例出生后的首轮**
> > > > > （`instance_new` → 首轮 `reset_sid`（psid 空）→ 之后一律 `append`，
> > > > > 例：05:13:03 出生 → 05:13:36 reset(psid 空) → 05:13:55 append(psid=sid)）。
> > > > > `同会话清视图(真bug)` 计数 **0**。此前看到的「同一 sid 反复 reset_sid」，
> > > > > 真相是**进程重启**（pid 13688 → 19780，`inst` 随之改变）——那几次重启正是
> > > > > 我改 `agent.py` 触发的自动重启，不是滞回本身的问题。
> > > > > 顺带修掉一个假警报：新实例首轮 `reset_sid`（psid 空）本来就没有前缀可断，
> > > > > 却一直被计入「前缀断裂」（4 条里 1 条是这种），现在按 `_real_break` 排除，
> > > > > 并新增「★ 滞回判定」一行给终审结论，不用人肉去数「真bug」那一格。
> > > > > 教训：**诊断工具自己会造假警报**——归因分完类必须给结论，
> > > > > 否则看报告的人得自己当裁判，等于没修。
> > > > >
> > > > > **2026-09-11 「接回来了」不等于「接回有用」**：视图跨进程存盘之后，还剩一个
> > > > > 假成功的口子——`_load_hist_view` 读回了视图，但锚点在新打包结果里找不到时，
> > > > > view 会被整体丢弃（`view = packed`，全量重建），而 mode 仍记 `restore`。
> > > > > 白烧一分不少，报告却显示已修好。
> > > > > 现在 restored 但 anchor<0 → `restore_lost`，并计入前缀断裂；`trace_report`
> > > > > 另给一行「★ 跨进程接回」把 restore 次数直接摆出来。
> > > > > 教训：观测字段必须能分辨「动作做了」和「目的达到了」——只记「读盘成功」，
> > > > > 就是又一次自己骗自己。
> > > > > 另：05:22 那次重启走 `reset_sid` 而非 `restore` 是**预期**——上一进程跑的还是
> > > > > 旧代码、压根没存档，新进程无档可读。修复是否生效，看下一次重启的 `mode`。
> > > > >
> > > > > **2026-09-11 技能激活集也不跨进程存活（与视图同一个病根）**：
> > > > > `_activated_skills` 是纯内存 set，一重启就空 → 工具集从 48 掉回 1。
> > > > > tools 排在请求最前，它一动整条前缀全废。实测 data/turn_metrics.jsonl
> > > > > （12 轮）：工具集在 48↔1 之间跳了 5 次，每次跳变都对应一次重启，
> > > > > 单轮白烧最高 78k 字符；顺带助手还会「忘了自己会用这个技能」。
> > > > > 修法与视图落盘同源：data/skills_state.json 按 sid 原子落盘，重启后同 sid
> > > > > 由 `_restore_skills` 接回，重挂顺序由 `_skill_order` 保证逐字节一致
> > > > > （顺序抖 = 前缀照样废）。
> > > > > 顺带修掉一个更隐蔽的缺口：`_stabilize_tools_for_new_turn` 原来只在
> > > > > `resume is None` 分支里调用，而断点里**不存 tools**——续跑轮不重建，
> > > > > 模型拿到的就是「重启后的空技能集」。现在每轮都重建。
> > > > > 教训：**修一类病，要把这一类里的每个入口都过一遍**——只在「正常轮」
> > > > > 修，续跑轮照样漏。

#### 自定义说明书

按需披露技能的"完整说明书"优先读技能目录下的 **SKILL.md**；没有 SKILL.md 时
回退为 skill.json 的 prompt + 全部工具的参数说明。建议每个 on_demand 技能都写一份 SKILL.md。

示例对比（3 个技能时差异不大；技能越多，渐进的收益越明显）：

```
默认模式：每个技能数百字的完整用法全部进 context
渐进模式：每个 on_demand 技能只剩一行摘要 + skill_help("技能名") 提示
```

---

## 3. 插件系统（Plugin）

一个**插件** = plugins/<插件名>/ 目录，包含 plugin.json（可选）与 plugin.py（必填）。

插件比技能多了**完整的运行时生命周期**（load/unload 钩子、实例持有状态），适合做
需要常驻状态的扩展。两种写法：

### 风格 A —— 继承 Plugin 基类（推荐）

```python
from harness import Plugin

class MyPlugin(Plugin):
    name = "my_plugin"; title = "我的插件"; version = "1.0.0"
    description = "..."
    prompt = "（注入 system prompt 的片段）"

    def on_load(self): ...
    def on_unload(self): ...
    def define_tools(self) -> list: return [/* OpenAI 工具定义 */]
    async def execute_tool(self, name, arguments) -> str: ...
```

### 风格 B —— 模块级函数（快速上手）

```python
PLUGIN_NAME = "my_plugin"; PLUGIN_TITLE = "我的插件"; PLUGIN_VERSION = "1.0.0"
PLUGIN_DESCRIPTION = "..."; PLUGIN_PROMPT = "..."
PLUGIN_TOOLS = [/* OpenAI 工具定义 */]
async def execute(name, arguments) -> str: ...
def on_load(ctx): ...
def on_unload(): ...
```

---

## 4. 管理

### 4.1 网页管理台

浏览器打开（大白服务运行中）：**http://<大白地址>/harness**

- 查看健康状态、启停状态、工具清单、最近事件；
- **Agent 运行时面板**：受监督 Agent、LLM 渠道/工具的调用次数与耗时、
  熔断器状态（可一键复位）、token 用量、在途与最近对话轮；
- 对每个技能/插件：「启用」/「禁用」/「热重载」；
- 「热重载全部」一键刷新所有扩展。

### 4.2 REST API

| 方法 | 路径 | 说明 |
|------|------|------|
| GET  | /api/harness/status   | 运行时健康状态（含 Agent 监督运行时快照） |
| GET  | /api/harness/runtime  | Agent 监督运行时快照（计量/熔断/运行/token） |
| POST | /api/harness/runtime/reset | 复位熔断器，body `{"name":"llm:chat" 或 "tool:xxx"}` |
| GET  | /api/harness/skills   | 技能列表 |
| POST | /api/harness/skills/<name>/enable | 启用技能 |
| POST | /api/harness/skills/<name>/disable | 禁用技能 |
| POST | /api/harness/skills/<name>/reload | 热重载技能 |
| GET  | /api/harness/plugins | 插件列表 |
| POST | /api/harness/plugins/<name>/enable | 启用插件 |
| POST | /api/harness/plugins/<name>/disable | 禁用插件 |
| POST | /api/harness/plugins/<name>/reload | 热重载插件 |
| POST | /api/harness/reload | 重载全部 |

```bash
curl -X POST http://localhost:8000/api/harness/plugins/hello_plugin/disable
curl http://localhost:8000/api/harness/status
```

### 4.3 目录配置（可选）

在 settings.json 增加 harness 段可自定义目录：

```json
"harness": {
  "skills_dir": "skills",
  "plugins_dir": "plugins"
}
```

---

## 5. 稳定性设计

- **韧性调用**：Agent 全部 LLM 调用（对话/游戏/决策/台词/记忆）统一经
  harness.runtime.supervise_llm —— 瞬时错误（网络抖动/限流/5xx）自动退避重试
  （默认 3 次，1s→2s）；鉴权、模型不支持等非瞬时错误不重试；另有整体超时保护。
- **熔断隔离**：某个工具或 LLM 渠道连续失败即熔断，冷却期内快速失败并给出
  明确提示，不再拖慢对话轮；半开探测成功后自动恢复，管理页可一键复位。
- **隔离失败**：单个技能/插件加载失败或执行异常，只影响它自己；其它扩展和大白主流程照常运行。
- **同步工具永不阻塞事件循环（2026-08-30）**：技能/插件里的同步处理器（纯 `def`
  handler / execute）统一经 `asyncio.to_thread` 在线程池执行——工具内的
  subprocess/网络/大文件读写不再冻结服务事件循环，心跳、推理指示、超时与
  WebSocket 全部保持响应；`wt_*` 等无 await 的异步包装也改为同步处理器走线程池。
- **状态持久化**：harness_state.json 记录每个技能/插件的启停，重启后依然生效。
- **热重载**：改完技能/插件代码，在管理台点「重载」即可，无需重启大白。
- **监督层不添乱**：harness/runtime 的任何异常都会退化为原有裸调用行为，
  监督本身永远不是故障源。

---

## 6. 内置示例

### 技能
- skills/filesys/ —— 文件助手：在项目目录内安全地列目录、读文本文件；
- skills/weather/ —— 天气助手：wttr.in 免费接口查询任意城市天气；
- skills/image_gen/ —— AI 画图：调用 settings.json 配置的绘图模型，图片存到 /generated/。
- skills/appearance/ —— 外观形象：查看/切换 3D 角色模型与背景场景（on_demand）；
- skills/voice/ —— 声音：切换音色/语速/合成引擎（on_demand）；
- skills/music/ —— 在线音乐：内置酷我/网易云聚合搜索、直链解析、点播/歌词/榜单/歌单（on_demand）；
- skills/interface/ —— 界面模式：切换交互模式、屏幕 Toast（on_demand）；
- skills/game/ —— 一起玩游戏：启动小游戏（on_demand）；
- skills/agent_ops/ —— 智能体指挥：委派 DSH/Codex/OpenCode、查看任务中心（on_demand）；
- skills/tasks/ —— 长任务与批量任务：提交 DAG 流程 / 批量 map 到 harness 任务系统，
  断点续跑、失败重试、并发受控，可查进度/取消/重试（on_demand）；
- skills/shell/ —— 本机命令行：shell_run 执行 Windows 命令（危险拦截+超时保护）、
  find_file 按名字定位真实文件路径——取代旧分流层的本机操作能力（full）。

> 原来的 tools.json 内置工具（屏幕控制/换装换景/换声/Toast/BGM/游戏/委派/任务查询）
> 已全部迁移为上述 on_demand 技能：执行结果（__screen_command__ / __dsh_bridge__ /
> __codex_delegate__ 标记 JSON）与原来完全一致，server 端无需改动。tools.json 已清空。

### 插件
- plugins/hello_plugin/ —— 问候演示插件：hello_world 工具 + on_load/on_unload 生命周期示例。

### 新增技能速查
```bash
# 1) 建目录
mkdir skills/my_skill
# 2) 写 skill.json（必填）
# 3) 写 skill.py（如需要实现）
# 4) 管理台点「重载」，或重启大白
```

> 想了解更多技能的写法与规范，可参考 skills/ 目录下的 SKILL.md、harness/skills.py 头部的作者规范。
