# tasks 技能 · 步骤写法 / 失败策略 / 执行保证（详细）

## 步骤（steps）写法

每步 `{id, deps?, action, ...}`，action 四类：

```
{"id":"search", "action":"tool",  "tool":"music_search", "args":{"keyword":"..."}}
{"id":"pick",   "action":"llm",   "system":"选歌助手", "prompt":"从里选一首: {{search.result}}", "deps":["search"]}
{"id":"all",    "action":"batch", "tool":"music_search", "items":[{...},{...}], "concurrency":3}
```

- `deps` 声明依赖（DAG，自动并行无依赖分支）；环依赖会被拒绝。
- `{{步骤id.result}}` 在后续步骤的 prompt / args 里引用前步结果（长文本自动截断）。
- llm 步骤**自动携带**全局上下文：任务目标 + 当前进度 + 已完成步骤结果摘要
  （无需手写，也因此在长流程/重启续跑后不会失忆）。
- 关键产出步骤可加 `"expect":"结果应满足什么"`——执行后自动语义校验，
  答非所问会被拦截重试（比 result_guard 的文案反查更强一级）。
- 步骤级可用 `timeout` / `max_attempts` 覆盖默认值。

## 失败策略 policy

- `abort`（默认）：任一步失败 → 取消其余步骤，流程失败。
- `continue`：只放弃失败分支，独立分支继续执行完。

## 执行保证

- **持久化断点续跑**：流程定义与每步完成状态实时落盘（harness_tasks.json）；
  服务重启 / 热重载后自动恢复，已成功步骤不重算（at-least-once 补跑未完成部分）。
- **重试与退避**：步骤失败按 2s→4s 退避重试（默认 2 次），可按步覆盖。
- **并发受控**：队列 worker 上限 + 批量批内信号量，不会打爆下游。
- **统一监督**：工具步骤经 harness 稳定路由 + runtime 熔断/超时/计量；llm 步骤走
  plan 渠道（用量记入运行时统计）。