# project_build —— 从零构建大型项目

> 一句话：把「一句模糊想法」变成「可计算覆盖率的规格矩阵 → 按模块增量实现 → 验收留证 → 反思改进」，
> 并且**流程本身零模型成本**（纯 stdlib 读写核算，不调任何 LLM）。

## 为什么这样设计（第一性原理）

从零构建大项目时，token 花在哪？不是"话多"，而是两件事：

1. **返工**：边写边定接口，写到第 10 个模块发现第 3 个模块的接口要改，前面全废。
   → 解法：**契约先落盘**。`spec.json` 里的模块契约是唯一真相源，实现按契约填，
   模块之间可以并行、可以交给子智能体。
2. **重复注入**：把整份需求反复贴进上下文，每一轮都为同一段文字付费。
   → 解法：**按需注入**。`project_next` 一次只给"当前模块的契约+验收+依赖签名"，
   其余模块只留 `M-002` 这样的 ID。ID 引用三个字符，重述需求三百个字符。

「面面俱到」也不能靠感觉，必须**可计算**：每条功能有 ID（`F-001`），
每个 ID 映射「归属模块 + 验收标准 + 证据」，于是覆盖率随时可算——
`已实现但未验收` 和 `无验收标准` 都会被精确点名。

## 五个工具

| 工具 | 作用 | 关键约束 |
|---|---|---|
| `project_spec_init(idea)` | 想法 → `spec.json`（机读真相源）+ `SPEC.md`（人读视图）+ 目录骨架 | 已有规格默认拒绝覆盖 |
| `project_next()` | 只注入**当前模块**的上下文（默认 6000 字预算，超限自动降级） | 不传 module_id 时自动挑依赖就绪的模块 |
| `project_dispatch(count?, profile?, dry_run?)` | 把多个**无写集冲突**的模块派给配置好的子智能体并行做 | 写集相交/已在执行的模块自动跳过 |
| `project_verify(module_id, evidence)` | 记录验收、更新矩阵；FAIL 时返回结构化反思 | **evidence 必填**——没跑命令不许说完成 |
| `project_matrix()` | 覆盖率核算 + 未验收/无用例/悬空引用点名 | 收尾前必调 |

## 典型流程（每个模块一轮，绝不多花）

```
project_spec_init(idea="做一个XXX：要能A、要能B、界面要有C…")
  ↓ 得到 M-001…M-00N 与 F-001…F-00M
project_next()                       # 只拿到 M-001 的上下文
  ↓ 按契约实现 M-001 的文件
project_verify("M-001", evidence="python -m py_compile core/m001.py → 无输出；…")
  ↓ 通过 → 自动提示下一个模块；失败 → 结构化反思
project_next() → … 循环 … → project_matrix()   # 覆盖率必须 100%
```

## 并行派发（project_dispatch）

模块依赖按**真实契约**而非构建顺序：测试依赖被测的核心（不依赖界面）、文档依赖核心用法
（不依赖测试）→ core 一完成，ui/test/doc 就能同时开工。

```
project_dispatch(count=3, dry_run=true)      # 先预演：哪些模块能并行、各用哪个档案
project_dispatch(count=3)                     # 真正派发 → 子智能体在后台并行做
  ↓ 子智能体各自交回四行回报：改动文件 / 验收命令 / 关键输出 / 未完成项
  跑它们的验收命令 → project_verify(module_id, evidence="…")   # 验收由主智能体统一记录
```

两条硬判据，缺一不可：

1. **写集互不相交**——两个模块改同一文件/目录，同时派出去必然互相覆盖（后写的赢），
   比串行还慢。写集按精确文件路径判（`core/m-001.py` 与 `core/m-002.py` 不算冲突）。
2. **已在执行的模块不再派**（`doing`/`impl` 状态跳过）——同一模块派两次 = 两个 worker 写同一批文件。

派活前用 `agent_profile_list` 看有哪些角色（agent_ops 技能）；按模块类型自动选人：
`core/ui/api/data → coder`、`test → tester`、`doc → writer`。

## 省 token 账（怎么省下来的）

- **流程零成本**：拆需求、算覆盖率、生成上下文全是本地字符串处理，0 次模型调用。
- **上下文按需**：单模块注入 ≤ 6000 字，10 模块规模实测远小于全量规格的 25%。
- **ID 引用**：需求只用 `F-003` 指代，不再重述原文。
- **反思按需**：PASS 时反思函数根本不执行，只有 FAIL 才产出（且只给"失败类型+关键行+下一步"，
  不重述日志全文）。
- **降级而非截断**：超预算先截功能描述 → 再省验收明细 → 最后才硬截断，保证信息密度最高。

## 状态文件

```
<project_dir>/.project_build/spec.json     # 唯一真相源（机读）
<project_dir>/.project_build/current_module.md  # 最近一次 project_next 的上下文（便于续跑）
<project_dir>/SPEC.md                      # 人读视图，可从 spec.json 随时重新生成
```

`spec.json` 结构：`project / goal / modules[] / features[] / log[]`。
模块含 `id/name/kind/dir/deps/provides/files/status/evidence`；
功能含 `id/desc/module/kind/status/acceptance[]/evidence`。
状态取值：`todo → doing → impl → verified/done`（`blocked` 用于显式阻塞）。

## 边界（明确不做）

- 不调用 LLM、不自动写代码——写代码仍由主模型/子智能体按 `project_next` 给的契约完成。
- 不改 harness 核心；不碰前端界面。
- 不替用户做技术选型：模块划分是启发式的（按需求关键词归类），
  不符合理解就直说，改 `spec.json` 即可。
- 不自动验收：子智能体的回报只是"它说做完了"，证据必须由主智能体跑命令复现后才 `project_verify`。
