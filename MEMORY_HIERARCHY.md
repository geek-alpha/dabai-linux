# 大白分层记忆上下文管理方案
## 0. 当前实现状态（2026-08-25 已落地）
- `memory.py:build_hierarchical_context` 四层打包 + token 预算已实现并由 `agent.py` 调用；
- `record_context_stats` 已接入 `agent.py` 普通对话主循环：每轮对话结束把
  raw/packed 估算与 LLM 返回的真实 prompt 用量写入 `context_stats` 表；
- `settings.json -> memory` 段已加入分层配置（默认值与代码常量一致，可随时覆盖）；
- token 节省按 `context_stats` 表的真实用量核算（真实会话实测平均节省约 70%+）。

## 1. 背景与现状调研

大白当前的上下文由三部分拼装（`agent.py._chat_stream_normal` / `_chat_stream_game`）：

| 层 | 现状实现 | 位置 |
| --- | --- | --- |
| 短期记忆 | 服务端连接级 `history`（最多 200 条→裁 100）+ 记忆库最近 10 条消息；Agent 用滞回窗口保持前缀缓存稳定——主路径 `_stable_history_messages`（消息级，超 16k token 截回一半），回退路径 `_stable_history_view`（满 12 轮裁回 8 轮） | `server.py` / `agent.py:_stable_history_messages` / `agent.py:_stable_history_view` / `memory.py:get_context_messages` |
| 长期摘要 | 每 10 条消息（≥20 条后）触发 LLM 增量摘要，最多注入 3 条、合并限 600 字 | `memory.py:maybe_summarize` / `_generate_summary` |
| 长期关键信息 | LLM/关键词提取用户信息入 `user_memories`，常驻注入 top-2 | `memory.py:extract_and_save_memories` / `get_user_memories` |
| 检索召回 | 2-gram 关键词检索 用户记忆+摘要+最近消息，注入上限 800 字符 | `memory.py:recall_memories` / `build_recall_block` |

调研发现的问题：

1. **预算常量是死代码**：`CONTEXT_TOKEN_BUDGET_RATIO` / `MAX_PROMPT_TOKENS` / `MEMORY_BLOCK_RATIO`
   已定义但从未被引用；`RECALL_MAX_TOKENS` 定义了但 `build_recall_block` 只按字符截断。
2. **上下文可能无限膨胀**：短期窗口只按「轮数」截断（12 轮），单轮超长（实测最长消息 1360 字符）
   不会被压缩；摘要固定 3 条 600 字，不随会话增长降级。
3. **没有量化统计**：`context_stats` 表已建但 `record_context_stats` 从未实现，无法衡量 token 节省。

## 2. 分层方案设计

在保留现有「静态大前缀在前、动态尾巴在后」的缓存友好排布前提下，给每层加 token 预算：

```
messages[0]  system 人设/资源/技能（静态前缀，不变）
messages[1]  system 长期摘要块（最新在前，预算 summary_max_tokens）
messages[2]  system 常驻长期记忆块（按 importance，预算 long_term_max_tokens）
messages[3]  system 易变状态（形象/场景/音乐/媒体/子智能体）
messages[4]  system 相关召回块（按相关性，预算 recall_max_tokens，强制生效）
messages[5..]       短期窗口（最近轮次，预算 short_term_max_tokens，由新到旧截断）
messages[-1] user 当前输入
```

各层预算（可在 `settings.json -> memory` 段覆盖，默认值）：

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `hierarchical_packing` | `true` | 总开关；`false` 完全回退旧行为 |
| `token_budget_ratio` | `0.5` | 提示词预算 = context_window × ratio（`max_prompt_tokens>0` 时优先用固定值） |
| `short_term_max_tokens` | `8000` | 短期窗口预算（由新到旧截断，最旧优先被挤出） |
| `short_term_max_chars_per_round` | `1200` | 单轮 user/assistant 文本最大字符，超出截断加 `…` |
| `short_term_min_rounds` | `6` | 短期窗口保底轮数：最近 N 个用户轮必保留，预算只约束更旧轮 |
| `short_term_max_chars_per_tool` | `500` | 单条工具结果在短期窗口的最大字符（完整结果仍在库中可召回） |
| `summary_max_tokens` | `200` | 长期摘要块预算 |
| `summary_max_chars_per_item` | `120` | 单条摘要最大字符 |
| `long_term_max_tokens` | `150` | 常驻长期记忆块预算 |
| `long_term_top_k` | `2` | 常驻长期记忆条数（保持记忆侧稳定排序） |
| `recall_max_tokens` | `200` | 召回块预算（与 `recall_max_chars` 取更小者，强制生效） |
| `record_stats` | `true` | 每轮写入 `context_stats`（raw/packed/actual 用量） |

### 量化口径

每轮对话写入一行 `context_stats`：

- `raw_tokens`：旧规则（不压缩）下会注入的估算 token —— 完整滞回窗口 +
  全部摘要 + 常驻记忆 + 召回字符上限；
- `packed_tokens`：分层预算实际注入的估算 token；
- `actual_prompt_tokens`：LLM 返回的真实 prompt usage（LLM 调用后回填）；
- `history_rounds` / `summaries` / `memories` / `recall_items`：各层实际条数。

## 3. 改动文件与回滚

- `memory.py`：新增分层打包、预算执行、`record_context_stats`；旧函数保持签名兼容。
- `agent.py`：普通/游戏模式的消息组装改为调用分层打包；回退开关关闭时行为与旧版一致。
- `settings.json`：`memory` 段新增分层配置。
- 节省量化：按 `context_stats` 表记录的真实 prompt 用量核算。

回滚方式：

1. `settings.json -> memory.hierarchical_packing = false`（软回退，不改代码）；
2. 恢复编辑前备份 `memory.py.bak-hierarchical-*` / `agent.py.bak-hierarchical-*` /
   `settings.json.bak-hierarchical-*`（硬回滚）；
3. 数据库只新增行（`context_stats`），不改旧表结构，无需迁移。

## 4. 预期收益

- 长会话（>20 轮）从「全部历史 + 固定摘要」变为「摘要预算 + 最新窗口预算」，
  单轮 prompt token 从随会话线性增长变为有上界；
- 超长消息不再整条塞入，单轮 1360 字符的消息被截到 500 字符；
- 召回/摘要/记忆三层合计默认约 550 token 封顶，历史窗口 1200 token 封顶；
- 量化数据落库，可持续校准估算误差。

## 5. 2026-08-29 工程上下文增强（修复"刚做完就忘"）

诊断结论：旧预算把"工程性 agent"饿死了——`short_term_max_tokens=1200`（约 2 轮）、
`summary_max_tokens=200`（约 1 条 120 字增量摘要）、单轮截 500 字。实测
`context_stats` 显示记忆层仅注入 ~1000 token（实际 prompt 7.5k），而执行日志里
单任务打到 60~107 轮工具调用、大量"重读"——模型每一轮都在丢失上一轮做过什么。

本次改动（均可在 settings.json -> memory 覆盖，`hierarchical_packing=false` 一键回退）：

1. **预算上调**（DeepSeek V4 上下文为 1M/1049K，按 1M 窗口配预算，仍远小于提示词预算）：
   `short_term_max_tokens 1200→3000`、单轮 500→1200 字、`summary_max_tokens
   200→800`、单条 120→600 字、`long_term 150→300 / top_k 2→4`、`recall 200→400`。
2. **摘要改为累计式**：`_llm_summarize` 输出 = 旧摘要 + 新内容的合并版（自包含），
   不再只写增量、"同上"即断链——最新一条摘要即覆盖全会话，上限 600 字。
3. **注入最近真实工具结果**：`get_recent_work_digest()` 每轮把最近一次带工具
   轮次的「目标 + 实际工具 + 截断结果」原样注入（≤1200 字），模型不需要靠
   重读文件回忆自己做过什么。
4. **垃圾记忆过滤**：`_clean_think_markers` 剔除 `</think>` 泄漏，
   `_is_memory_noise` 拦截"无/同上/无无"等空壳回复；已在库的 `无</think>无`
   垃圾条目一并清理。

## 6. 2026-08-29 对话轮断点多槽位化（修复"被打断后无法继续"）

诊断结论：旧断点每用户单文件，用户插话启动新对话轮时 `chat_stream` 先
`clear_turn_checkpoint`，与旧轮 `_mark_ckpt_paused` 竞态——常见结果是断点被清掉，
「继续」只能退化为一句话任务摘要，完整现场丢失。

本次改动（agent.py / server.py）：

1. **断点按 turn_id 多槽位落盘**（`data/turn_checkpoints/<user>__<turn_id>.json`，
   每用户最多 8 个，latest 指针定位最新）；新对话轮不再清空旧断点；
2. **「继续」只认最新已暂停断点**（`load_paused_turn_checkpoint`），
   普通新轮不覆盖它，任务可一直等到被续跑；
3. **断点续跑完成连源槽位一并清理**，防止同一任务被重复续跑；
4. **服务重启只自动恢复崩溃断点**（未标记 paused），用户暂停的断点等『继续』，
   不再重启抢跑；
5. **收尾兜底**：流式循环因 is_cancelled 提前 break 正常收尾时，
   服务端显式 `mark_latest_ckpt_paused`，保证『继续』始终可用；
6. **方案 C 兜底摘要增强**：任务状态摘要附最近真实工具结果，断点丢失时
   也能靠结果摘要续跑。

## 7. 2026-08-31 短期窗口保底轮（修复"前两轮就忘"）

诊断结论：`context_stats` 显示短期窗口实际只注入 `history_rounds=2~3`——不是
历史没存，而是双重截断把最近轮次挤没了：
1. 消息读取只回看最近 60 条，而单轮工具循环可占 60+ 条（实测最新一轮 42 条），
   更早的用户轮在查询阶段就被整批丢出窗口；
2. 工具轮单条结果常达上万字（整轮几千 token），3000 token 预算装不下几轮。
数据库现场：`chat_memory.db` 最近 100 条消息仅构成 6 个轮次，其中一轮
18328 字符（估算 12019 token），打包后只剩 3 个用户轮。

本次改动（均可在 settings.json -> memory 覆盖，`hierarchical_packing=false` 一键回退）：

1. **保底轮**：`short_term_min_rounds=6`，最近 6 个用户轮强制保留（只受单轮
   字符上限约束，不再因 token 预算丢整轮），保证「刚说过的话」稳定进入上下文；
2. **扩大回读窗口**：消息查询从最近 60 条扩大到最近 300 条（LIMIT 400），
   单轮工具循环再长也挤不掉更早的用户轮，保底轮才有数据可保；
3. **工具结果截断**：`short_term_max_chars_per_tool=500`，短期窗口内 tool 消息
   文本截断保留要点（tool_calls/tool_results JSON 结构不动，function-calling
   历史仍有效），完整结果仍在消息库中可经 recall 召回；
4. **预算上调**：`short_term_max_tokens 3000→8000`（1M 上下文窗口，提示词
   预算内仍很宽裕），旧轮能带更多。

量化口径不变：`context_stats.history_rounds` 现在应能稳定看到 ≥6，且
`packed_tokens` 只小幅上升（工具长文被截断后占位有限；实测单轮记忆块
约 9.2k token，相对 1M 上下文可忽略）。
