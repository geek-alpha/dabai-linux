# DeepSeek Harness 学习清单

**对象**：`/home/wxf/oss-work/deepseek-harness`（DeepSeek AI 官方开源 agent harness，`dsh`）
**实测规模**（2026-09-22 盘）：2000 个代码文件 / 97.4 万行；541 篇 md；254 个 scripts，其中 60 个是 `verify-*` 门禁；根 `AGENTS.md` 1892 词 / 30 条规则
**一句话总纲**：代码本身不神，狠的是**约束工程**——把「应该」变成「不过就红」。

每条格式：机制 → 证据（`文件:行号`）→ 为什么 → 我方对应 → 动作。
动作优先级：**P0 立刻做 / P1 该做 / P2 储备**。

---

## 一、约束工程：软要求一律脚本化

### L1 文档字数预算 + 硬门禁（P0）
- **机制**：`scripts/doc-budgets.manifest.json` 给常设文档设词数上限，`pnpm run verify-doc-budgets` 拒绝超额文件，也拒绝清单里缺失的文件。超标时的处理顺序写死：**① 搬迁到该管的层 → ② 压缩 → ③ 才提上限，且提额必须在 PR 里说明理由**（"天花板太低是预算 bug"）。
- **证据**：`docs/AGENTS.md:50`、`docs/AGENTS.md:52-56`、`docs/AGENTS.md:58`；`scripts/verify-doc-budgets.ts:1-7`（文件头注释即契约：缺失/非法上限都失败，`--list` 报当前用量）
- **为什么**：这正面回答了我们悬而未决的问题——**规则区 2519 字符 / 14 条，凭什么删哪条**。答案是给预算 + 给用量，而不是靠印象裁。且天花板"只降不升"（保留 ≥5% 余量）让约束是活的。
- **具体数值**（可直接抄）：根 `AGENTS.md` ≤1950 / `architecture.md` ≤2400 / 子树 `AGENTS.md` ≤600 / `testing.md` ≤1300 / `defensive-patterns.md` ≤550
- **实测校准**：根 `AGENTS.md` 现在 1892 词，正好卡在 ≥5% 余量的边缘——预算不是摆设，是被用满的。
- **我方对应**：规则区全靠提示词注入 + 自觉，没有门禁。
- **动作**：写 `tools/rule_budget.py`——规则区设字符上限、每条挂命中计数（`--list` 报用量）、超额先搬迁再压缩；跑在 gate 里。
- **✅ 已交付（2026-09-22）**：`tools/rule_budget.py` + `tools/rule_budgets.json` + `tests/test_rule_budget.py`（14 用例）。实测立起**合计口径 4971 字符 / 28 块**（= agent_rules 段 1432 + 行为准则段 3539）——此前 status.py 只数后者（raw 3584 / 17 条）、prompt_rules_audit 只数前者（1432 / 11 条），两段区间不重叠却同名「规则区」。status.py 已改为只展示、口径唯一来源在 rule_budget，`SECOND_SOURCE` 判据防第二事实源复活。自证：target 降到 4000 → RED + 退出码 1 → 还原 → YELLOW + 退出码 0（manifest diff 无残留）。
- **✅ 已交付·第二层（2026-09-22）**：把审计也扩到全口径——`prompt_rules_audit.py` 的 RULE_MAP 补 17 条行为准则段条目（3539 字符此前从未进审计流程：不是判不出，是连入口都没有），锚点常量收到审计侧一处、rule_budget 复用（`SECOND_SOURCE` 改成扫 `tools/*.py` 的模块级定义）。实测进审计流程 4913/4971（99%，余下是块间空白和「工具详细用法」尾巴行）；**可量化率 944/4971（19%）**——准则段全无埋点，删哪条只能人工判断。顺带修正 `DUPLICATED_RULE`：原判据统计跨块标识符，把 `delegate_agent_task` 误报成重复（两处说的是不同的事），改成比 20 字符以上自然语言片段逐字重复后，5 处命中剩 1 处真重复。

- **✅ 已交付·第三层（2026-09-22）**：**可量化率 19% → 47%**（2348/4971 字符）。做法是补埋点而不是硬接指标：`agent.py` 新增 `_rule_script_names()`（从 shell 命令提取 `.py` 脚本名，只留文件名不留命令原文——94% 的轮都用 shell_run，命令内容此前完全不可见）+ `turn_metrics` 的 `script_names` 字段；审计侧 `call_stats()` 按**轮**粒度算（不是次数求和，否则一轮调 10 次会算成 10 轮）。三个真缺口实测：多步轮 247 中仅 64 轮先提交清单（26%）、改码轮 145 中仅 64 轮同轮验证（44%）、查证类 213/269（79%，零收益候选）。
- **⚠ 未验证的一环**：新埋点要手动重启才生效——`tools/reload_check.py` 原文「core_autorestart：关闭 ← 核心改动不会自动生效」。**「改完代码」不等于「埋点开始产出」**：审计看到 0 次时必须先跑 reload_check 排除未重载，否则会把「安全网没生效」误读成「行为没发生」。

### L2 每个门禁脚本自带契约头注释（P1）
- **机制**：`verify-*` 脚本开头 5~7 行说清：谁在什么条件下会红、缺文件怎么处理、怎么只看用量。
- **证据**：`scripts/verify-doc-budgets.ts:1-7`
- **为什么**：门禁的红必须是**可解释**的，否则第一次变红就被人绕过。
- **我方对应**：`tools/gate_audit.py` 已有骨架（`tests/test_gate_audit.py`）。
- **动作**：给每个门禁脚本补 3 行契约注释（何时红 / 怎么查用量 / 怎么豁免）。

### L3 门禁数量本身就是信号（P2）
- **机制**：254 个脚本里 60 个是 `verify-*`，覆盖文档格式、链接、类型粘贴、归档冻结、术语、仓库引用……
- **证据**：`ls scripts/ | grep -cE '^verify-'` = 60
- **为什么**：规则的可执行率，决定了它是不是装饰。
- **我方对应**：我们的规则绝大多数不可执行。
- **动作**：清点现有 14 条规则，标出「能脚本化 / 只能靠自觉」两栏，先挑 3 条脚本化。

---

## 二、知识管理：每条规则都挂理由，理由有生命周期

### L4 一个事实只有一个家（tier 表要写「不属于这里」）（P1）
- **机制**：13 行 tier 表，每行三列——**Tier / 它的职责 / 不属于它的东西**。配一句分诊口诀：bugs→postmortems；rationale→Agent Notes；procedures→cookbooks；类型定义→subsystems；包契约→README；常设命令→根 `AGENTS.md` + 理由链接。
- **证据**：`docs/AGENTS.md:17`、`docs/AGENTS.md:19-33`、`docs/AGENTS.md:35`
- **为什么**：只说「这里放什么」不够，**列清「不属于这里」才是防重复的关键**。
- **我方对应**：提示词、经验库、长期事业、信条四处都在塞规则，边界模糊。
- **动作**：给我们的四层知识面画同一张三列表。

### L5 slop checklist：8 条可执行的删减判据（P0）
- **机制**：任何文档都能照单自查——重复规则（搜特征短语，只留一个家）、历史外溢、实现状态标注（"implemented!" 会腐烂）、手抄目录、推理过程记录、理由重复放在兄弟方法旁、段落墙、强调膨胀、`implemented/` 笔记里的 spec-speak。
- **证据**：`docs/AGENTS.md:60-72`
- **为什么**：**这是「删哪条」的可操作标准**，比任何"保持简洁"的原则都硬。第 71 行尤其好：*强调遍地都是，就等于什么都没强调*。
- **我方对应**：提示词里大量加粗强调，正是它点名的 emphasis inflation。
- **动作**：把这 8 条搬成 `docs/slop-checklist.md`，每次改提示词/文档前跑一遍。
- **✅ 部分落地（2026-09-22）**：其中 6 条已在 `tools/rule_budget.py` 里可执行——段落墙、强调膨胀、重复规则、历史外溢、推理过程、状态标注。实测首次跑就抓到真问题：**12/28 块带强调标记（43%，上限 25%）**、`delegate_agent_task`/`skill_help` 各出现在 2 块。剩 2 条（手抄目录、spec-speak）要等对应文档类型再落。

### L6 Agent Note：决策记录的生命周期是路径编码的（P1）
- **机制**：`{lifecycle}/{class}/yyyy-mm-dd-topic.md`。四态：`proposed/` `implemented/` `rejected/` `archived/`。class 是**封闭集合**（feature / bug-fix / simplification / architecture / process / testing），加一个 class 要同时改 canonical set 和文档。`refactor` 被**故意排除**——与 `simplification` 重叠，判据是「可观察行为是否改变」。
- **证据**：`.agents/notes/README.md:9-15`、`.agents/notes/README.md:23-34`
- **实测分布**：proposed 30 / implemented 424 / rejected 14 / archived 635
- **为什么**：状态写进路径，就不需要维护一个会腐烂的状态字段。**635 条归档 ≠ 垃圾堆，是冻结的历史**。
- **我方对应**：经验库（lesson）只有增删，没有状态机。
- **动作**：给 lesson 加 `status`（active / superseded / rejected），拒绝类的要写清「它阻止了什么诱人的错误」。

### L7 rejected 的保留判据 = 只在它能阻止一个诱人的错误时（P1）
- **机制**：被否决的提案，「**只在它的理由能阻止一个诱人的、有意义的错误时才保留，否则把完整三件套一起删掉**」。
- **证据**：`.agents/notes/README.md:14`
- **为什么**：这是极罕见的、敢说「删掉历史」的工程文化——**历史的价值用未来用途衡量，不用沉没成本衡量**。
- **我方对应**：我们从不删教训，只增不减。
- **动作**：lesson 库加同样的删除判据，定期清理"读过但不会再影响决策"的条目。

### L8 `## Alternatives considered` 强制（P1）
- **机制**：每份决策记录**必须**写被击败的备选方案及它为什么输。理由原话：*没有记录被击败的方案，就会招来反复重审*。备选只能**记录**，不能事后编造（早期文件用固定 HTML 注释占位，gate 只对 pre-format 文件放行）。
- **证据**：`.agents/notes/README.md:109-117`
- **为什么**：我们反复重议同一个决定，根因就是没人写下「当初为什么不选 B」。
- **动作**：写进 lesson 模板——凡是决策类教训，必须带一行「被否掉的方案 + 为什么」。

### L9 归档即冻结，且由脚本守着（P2）
- **机制**：`archived/` 一经封存永久冻结，**明令不得当作当前行为的权威**；`verify-archived-agent-notes` 校验封闭类树、完整中英+sidecar 三件套、归档元数据、sidecar 哈希、append-only 冻结清单。归档时只允许改三处（加 `Archived:` 行、重录 sidecar、修链接）。
- **证据**：`.agents/notes/README.md:38-42`
- **为什么**：把「过时的正确」和「当前的正确」物理隔离，避免 agent 拿旧文档当真。
- **我方对应**：我们的 PHASE0_RECON_REPORT.md 之类历史报告，现在仍可能被当成现状读。
- **动作**：给历史类文档加 `Archived:` 头 + 顶部一行「非当前行为权威」。

### L10 状态与目录交叉校验，格式由 gate 强制（P2）
- **机制**：前三行固定为 `# Agent Note: <title>` / 空行 / `Status: <status>`；status 三形态之一且**必须与所在文件夹一致**（gate 交叉校验）。body 骨架也分状态：`implemented/` 里出现 `## Proposal` / `## Plan` / `## Migration plan` / `## Acceptance criteria` 直接判红——因为那是 spec-speak。
- **证据**：`.agents/notes/README.md:60-68`、`.agents/notes/README.md:76-101`、`.agents/notes/README.md:119-121`
- **为什么**：格式统一让 gate 能查，gate 能查让格式不会腐烂。
- **动作**：lesson 库固定头三行，加校验脚本。

### L11 不建中心索引（P2）
- **机制**：**明令不得添加 `INDEX.md`**，理由是专门有一份 Agent Note 承载；检索靠目录 + 仓库搜索。
- **证据**：`.agents/notes/README.md:19`
- **为什么**：中心索引是必然腐烂的第二事实源。
- **我方对应**：我们有 MEMORY_HIERARCHY.md 这类索引。
- **动作**：索引只允许做「指向检索入口」，不允许做「内容摘要」。

---

## 三、测试哲学：让绿色有意义

### L12 守卫必须自证有效（P0）
- **机制**：**A guard only guards if the regression fails it**——写完守卫，**引入那个回归 → 看它变红 → 撤回**。并给出具体写法：对没有 `inject` 的插件，Loader smoke 在默认导出替换命名导出时**仍会绿**，所以要加 `expect('default' in mod).toBe(false)` + `unwrapExports` 往返断言。
- **证据**：`docs/testing.md:40`
- **为什么**：这是最有价值的一条。ACP 事故里 **178 个绿测试 + 100% 行覆盖，产品却完全不能用**——因为测试走的加载路径和真实路径不同。
- **动作**：写进经验库（已落）+ 每次新增测试/门禁，跑一次「变红证明」并在汇报里贴出来。

### L13 验世界，不验自述（P0）
- **机制**：e2e 断言要**外部重跑命令、重读文件**；「对 agent 自己输出的关键词探测会让作弊的 agent 通过」。另外：断言**未触碰的文件字节级一致**；资源在测试里创建、在 `afterEach` 释放（失败/重试/超时也要）；共享 fixture 放 `tests/harness.ts`，**绝不能放另一个 `*.e2e.ts`**（import 一个 spec 会重新注册它的 `describe` 并重复真实 API 调用）。
- **证据**：`docs/testing.md:35`
- **为什么**：我们大量"验证通过"是拿 agent 自己的输出当真。
- **动作**：汇报模板固定要求贴**外部命令的原始输出 + 退出码**。

### L14 只 mock 昂贵或不确定的边界（P1）
- **机制**：只 mock LLM adapter / 网络 / 时钟，**下游全真**。桥接测试保留真实工具注册表和管线，`MockAdapter` 是唯一的 mock。手搓替身只证明"桥能搬字节"，不证明"发布出去的工具行为如断言"。
- **证据**：`docs/testing.md:29`
- **动作**：审查我们测试里 mock 的范围，凡 mock 掉自己核心逻辑的，标为无效测试。

### L15 覆盖率的正确用法（P1）
- **机制**：per-file 100% 是**门禁**，但注释写明：**未覆盖的行往往是该删的死代码，不是该补的测试**；行覆盖必要但永不充分——它证明行跑过，不证明功能如发布般工作。
- **证据**：`docs/testing.md:10`
- **为什么**：直接顶掉「为了覆盖率而补测试」这种自欺。
- **动作**：以后见到未覆盖代码，先问「该删吗」再问「该测吗」。

### L16 只有进程是隔离的（P1）
- **机制**：fork worker 并行跑 spec，但**端口、可预测路径、外部命名空间、继承的子进程都不隔离**；「一个只在单独跑时才通过的 spec，是 spec 的缺陷，不是 runner 不稳定」。
- **证据**：`docs/testing.md:21`
- **为什么**：这是"flaky 测试"最诚实的归因方式——先怪自己。
- **动作**：遇到不稳定测试，先按这条定性，再决定改 spec 还是改 runner。

### L17 解析只在 source 平面（P1）
- **机制**：所有 vitest 配置把裸 workspace import 解析到 `src`，**绝不通过 package `exports` 走构建后的 `lib/`**——陈旧产物会加载**第二份模块单例**。
- **证据**：`docs/testing.md:45`
- **为什么**：**和我们昨天修的 Symbol 身份分裂是同一类病**（两份模块 → 两个 Symbol）。
- **动作**：检查我们有没有"加载了第二份模块"的路径（热重载、子进程、sys.path 混用）。

### L18 真实入口路径 = 已发布产物（P1）
- **机制**：`bin` 必须用**纯 node 跑构建后的 `lib/bin.js`**，才能暴露 tsx 掩盖的失败（settle 竞态、模块解析、被吞掉的加载失败）；并且要断言**配置真的缺失时退出码非零**。
- **证据**：`docs/testing.md:41`
- **动作**：我们的冒烟要跑真实入口（`dabai.sh` / `python -m`），不是 import 一下就算。

### L19 别省真 key 的测试（P2）
- **机制**：**无 key 测试只证明管道通，只有真 key 运行才证明 agent 能用**。最高价值的是启动真实 `dsh` profile、发一条 prompt、然后**检查世界**的 smoke——它抓的正是"单测全绿、产品全坏"这一类。无 key 时自跳过，保证无密 CI 不阻塞。
- **证据**：`docs/testing.md:25`
- **动作**：挑 1 条最关键链路（比如 pdd 客服接口）做成真 key 端到端 smoke。

---

## 四、防御模式：33 行全是真金

`docs/defensive-patterns.md` 全文 33 行 / 550 词封顶 / 7 条模式，每条都是**这里真出过或差点出的缺陷类**，写法统一为「规则 + 它会阻止的复发」。

### L20 正交结果独立上报（P0）
- **机制**：一个结果可以同时是几件事——**进程可能超时且退出 0，因为它捕获了信号**。`timedOut` / `signal` / `exitCode` 各自独立上报，**绝不能把一个标志的报告嵌进另一个的分支里**，否则调用方会把被腰斩的运行读成干净成功。
- **证据**：`docs/defensive-patterns.md:9`
- **为什么**：我们的 shell 工具正是这个形状（超时 + 退出码 + 输出截断），有同样的误读风险。
- **动作**：核对 shell 工具的超时上报路径，确保超时和退出码不互相覆盖。

### L21 dispose 必须到达静止，而非只发出请求（P1）
- **机制**：teardown 只发 kill/abort 就返回会留下孤儿。清理要异步并**await 子进程退出**（kill → await done）；并且**先关闭监听/通知注册表，再 kill**，这样迟到的完成事件保持沉默。
- **证据**：`docs/defensive-patterns.md:21`
- **动作**：审查我们的进程清理（server / 定时任务 / 子智能体）。

### L22 不可信输出不给环境变量和可预测路径（P1）
- **机制**：派生的命令拿**清洗过的 env**（丢掉 `*KEY*` / `*SECRET*` / `*TOKEN*` / `*PASSWORD*`），避免 harness 凭据泄漏到输出、`env` 或溢出文件里；临时/溢出文件用私有 0700 目录 + 随机名 + 独占只属主打开（`'wx'`, `0o600`）——可预测的全局可读路径会招来符号链接竞态和信息披露。
- **证据**：`docs/defensive-patterns.md:29`
- **动作**：检查我们调用外部命令时是否把整个环境（含 cookie/token）传下去。

### L23 异步状态不是同步状态（P1）
- **机制**：`followup()` 没有单条消息的完成或结果；后台任务完成与回合边界竞态；`close()` 对 EOF 和主动释放都触发。**不能把 `agent/status` 或 `whenIdle()` 当成某一条 follow-up 的结果**。且 guard 是双向的：**如果等待的转换永不发生，等待就会挂死**——必须显式处理"无可等待"分支。
- **证据**：`docs/defensive-patterns.md:17`
- **动作**：检查我们的等待逻辑有没有"永不到来的事件"分支。

### L24 其余三条（P2）
- **公共契约两侧都要守**（`:13`）：多种表示形式（抛异常 vs `finish{kind:'error'}`）必须在**返回前归一化**，并在类型定义处写明归一化契约。
- **分发器里包住回调异常**（`:25`）：用户提供的监听器抛异常，不得拒绝它所在的 promise，也不得饿死后面的监听器——dispatch 循环套 try/catch + 日志。
- **删除链接形状的路径**（`:33`）：可能是 symlink/junction 的路径用 `lstat().isSymbolicLink()` 判断后 `unlink`——只删链接、不跟进目标；递归删除保留给确定是真目录的路径。

---

## 五、结构与协作

### L25 事故 → 规则闭环（P1）
- **机制**：`docs/postmortem/` 10 篇，标题格式 = **编号 + 一句话根因**（例：`0001-acp-default-export-drops-inject`）。复盘写完，规则**直接落进对应层的 `AGENTS.md` 并回链这篇复盘**。
- **证据**：`docs/postmortem/0001-acp-default-export-drops-inject.md:5`（`Status: resolved (fix in PR #41)`）；`packages/AGENTS.md:5-6`（两条导出规则，各自链到 postmortem）
- **为什么**：规则不是拍脑袋写的，是**从事故里长出来的**——所以没人会质疑它凭什么存在。
- **我方对应**：我们有教训库，但没有"事故报告"这一层。
- **动作**：重大故障写 `docs/postmortem/NNNN-一句话根因.md`，正文固定「执行摘要 / 时间线 / 根因 / 为什么测试没抓住 / 落地的规则（回链）」。

### L26 每条规则挂 rationale 链接（P1）
- **机制**：根 `AGENTS.md` 30 条规则，几乎每条结尾都是 `([rationale](...))`，指到承载理由的那份决策记录。规则行本身控制在 1~3 行。
- **证据**：根 `AGENTS.md`（179 行 / 30 条 / 1892 词）
- **为什么**：规则短、理由长，**规则才读得下去，理由才审得动**。
- **动作**：给提示词里的每条规则挂一个理由锚点（lesson id 或文件路径）。

### L27 skills 是可复用工作流，不是提示词（P2）
- **机制**：`.agents/skills/` 12 个 `dsh-*` skill：`dsh-code-review`、`dsh-doc`、`dsh-prose-standard`、`dsh-find-simplifications`、`dsh-pre-push-checks`、`dsh-ci-test-reliability`、`dsh-archive-agent-notes`、`dsh-speed-up-perf`、`dsh-trim-cot-leakage`、`dsh-merging-stacked-prs`、`dsh-translate-docs`、`record-browser-gif`。每个 skill 是**带判据的决策标准**（如 `dsh-find-simplifications` = 找能删的东西），且被文档层反向引用。
- **证据**：`docs/AGENTS.md:3`（文档规范直接指向 `dsh-doc` 和 `dsh-prose-standard`）、`docs/testing.md:21`（CI 可靠性规则归 `dsh-ci-test-reliability` 所有）
- **为什么**：**规则和它的执行器绑在一起**，文档只负责指路。
- **动作**：把我们的高频流程（改码前审查、收工前自检）做成 skill，而不是提示词段落。

### L28 文档也是 i18n 一等公民（P2）
- **机制**：`docs/` 365 篇 md + 265 个 `.i18n.yaml`；中英成对更新由 pairing gate 校验；**机器校验的 header token 保持英文原样**，`.zh.md` 跳过格式 gate 但由配对 gate 查一致性。
- **证据**：`.agents/notes/README.md:123-125`
- **为什么**：双语文档最容易漂移，所以把「结构逐节对应」变成机器可查的约束。
- **动作**：暂不需要（我们单语），但"成对更新由 gate 校验"这个思路可用于提示词与文档的双份规则。

---

## 立刻可做的 TOP 3

1. **`tools/rule_budget.py`**（L1 + L5）——规则区设字符上限、每条挂命中计数、超额先搬迁再压缩；顺带把 slop checklist 8 条做成检查项。这是悬而未决问题「凭什么删哪条」的直接答案。
2. **守卫自证流程**（L12）——新增任何测试/门禁，必须附「引入回归 → 变红 → 撤回」的证据，贴进汇报。
3. **验证汇报模板固定格式**（L13）——只认外部命令原始输出 + 退出码 + 未触碰文件一致性；不认 agent 自述。

## 证据索引（全部为实读文件）

| 文件 | 关键行 | 主题 |
|---|---|---|
| `docs/AGENTS.md` | 17 / 19-33 / 35 | 一个事实只有一个家 |
| `docs/AGENTS.md` | 50 / 52-56 / 58 | 字数预算与红灯三步 |
| `docs/AGENTS.md` | 60-72 | slop checklist |
| `.agents/notes/README.md` | 9-15 / 14 / 19 | 生命周期路径编码、rejected 删除判据、禁索引 |
| `.agents/notes/README.md` | 23-34 / 38-42 | class 封闭集合、归档冻结 |
| `.agents/notes/README.md` | 60-68 / 76-101 / 119-121 | 头部与 body 格式、迁移机械化 |
| `.agents/notes/README.md` | 109-117 | Alternatives considered 强制 |
| `docs/testing.md` | 10 / 21 / 25 / 29 | 覆盖率用法、隔离、真 key、mock 边界 |
| `docs/testing.md` | 35 / 40 / 41 / 45 | 验世界、守卫自证、真实产物、source 平面 |
| `docs/defensive-patterns.md` | 9 / 13 / 17 / 21 / 25 / 29 / 33 | 7 条防御模式 |
| `scripts/verify-doc-budgets.ts` | 1-7 | 门禁脚本契约头注释 |
| `packages/AGENTS.md` | 5-6 | 事故长出的规则 |
| `docs/postmortem/0001-*.md` | 5 | 复盘标题格式与状态行 |
