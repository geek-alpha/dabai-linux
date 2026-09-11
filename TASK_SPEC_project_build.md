# TASK_SPEC —— 大白「从零构建大型项目」能力（project_build 技能）

> 分支 `codex/project-build-skill`　工作树 `D:\AI\dabai_worktrees\project-build-skill`
> 状态：已完成（验收核验见文末）

## 1. 目标

让大白具备**从零构建大型项目**的结构化能力：一句模糊想法 →
「可计算覆盖率的规格矩阵 → 按模块增量实现 → 验收留证 → 实时反思改进」，
且流程本身零模型成本（纯 stdlib），上下文按需注入以省 token。

交付物：新技能 `skills/project_build/`（4 个工具 + 常驻纪律 + 自测脚本），
不改 harness 核心，合并回主分支即热重载生效。

## 2. 范围与不做

**做**
- `project_spec_init`：想法 → `spec.json`（唯一真相源）+ `SPEC.md`（人读视图）+ 目录骨架
- `project_next`：只注入当前模块的契约+验收+依赖（默认 6000 字预算，超限三级降级）
- `project_verify`：记录验收证据、更新覆盖矩阵；FAIL 时返回结构化反思
- `project_matrix`：覆盖率核算 + 未验收/无用例/悬空引用点名
- 同阶段功能超 4 条自动切多模块，模块数随项目规模增长（本次实测 10 模块 / 29 功能）

**明确不做**
- 不改 `harness/` 任何核心文件，不动 `codex_runner.py` 的 TASK_SPEC 逻辑
- 不做前端界面（本期纯工具层）
- 不调用任何 LLM（省 token 的关键：流程本身零模型成本）
- 不自动写代码——写代码仍由主模型/子智能体按 `project_next` 给的契约完成

## 3. 验收标准

1. 语法与导入：`py_compile` 全通过；import 冒烟无缺依赖/循环导入。
2. 端到端可用：一句想法初始化 → `spec_init → next → verify → matrix` 全链路不报错，
   `spec.json` 合法可解析，逐模块推进到覆盖率 100%。
3. 矩阵可计算：故意留「已实现未验收」+「无验收标准」各一条，`project_matrix` 精确点名。
4. 省 token 可量化：`project_next` 单次注入 ≤ 全量 spec 字符数的 25%。
5. 反思有触发条件：FAIL 返回结构化反思（类型+关键行+下一步）；PASS 零反思文本；
   空证据被拒绝（没跑命令不许说完成）。
6. 不污染运行中大白：改动全在隔离工作树，主工作区 `git status` 干净。

## 4. 实施步骤

1. 读技能范式（`skills/agent_ops/skill.py`）确认 TOOLS/HANDLERS 写法 ✅
2. 写实现骨架（常量/工具函数）→ 语法校验 ✅
3. 分块补全：需求拆解与模块规划 → 按需注入与反思 → 4 个 handler ✅
4. 写 `skill.py` / `skill.json` / `SKILL.md`（JSON 解析校验通过）✅
5. 端到端自测脚本 `selftest_e2e.py`，10 模块 / 29 功能规模实跑 ✅
6. 自查 diff → 合并 ✅

## 5. 风险与回滚

- 新技能与现有技能重名 → 已确认 `skills/` 下无 `project_build`
- skill.json 格式不合规导致加载失败 → 照抄 agent_ops 字段结构，已过 JSON 解析校验
- 注入文本过长反噬 token → 硬预算 6000 字 + 三级降级（截描述 → 省验收 → 硬截断）
- 回滚：改动全在 `codex/project-build-skill` 分支；出问题 `wt_discard` 一键丢弃，
  或 `git revert` 合并提交；主工作区零影响（合并前已确认干净）

## 【验收核验】
PASS 语法与导入（py_compile 无输出；code_smoke import 通过）
PASS 端到端可用（selftest_e2e.py：10 模块 / 29 功能，逐模块推进到 10/10 完成、覆盖率 100.0%）
PASS 矩阵可计算（F-001 置 impl → 精确点名「已实现未验收」；F-002 清空验收 → 精确点名「无验收标准」；覆盖率降至 96.6%）
PASS 省 token 可量化（project_next 单模块注入 725 字符 vs 全量 spec 8748 字符 = 8.3%，远低于 25% 上限）
PASS 反思有触发条件（FAIL 返回「失败类型：语法错误」+ 关键行 line 42 + 下一步建议；PASS 后全项目 reflection 均为空；空 evidence 被拒绝）
PASS 不污染运行中大白（改动全部在隔离工作树，主工作区 git status 干净）
【验收核验结束】

自测复跑：`python skills/project_build/selftest_e2e.py`（20 项断言，退出码 0）
