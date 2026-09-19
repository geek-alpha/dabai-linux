# 任务与执行（tasks）

长任务/批量任务/TODO/定时/策略复盘，五合一。触发：多步骤长任务/批量任务/定时任务/TODO/查策略/登记复盘。

## 长任务与批量（harness_*）
- `harness_flow_plan` 规划长任务（只规划不执行，产出方案）
- `harness_flow_submit` 提交多步骤长任务后台执行（断点续跑）
- `harness_batch_submit` 提交批量任务（一个工具并行应用到一批参数）
- `harness_task_status` 查任务进度与结果 / `harness_task_list` 列出最近任务
- `harness_task_confirm` 批准/拒绝危险步骤 / `harness_task_cancel` 取消 / `harness_task_retry` 重试

## TODO 与定时（todo_* / sched_*）
- `todo_create/plan/list/get/update/subtask/remind/delete` 任务清单全流程
- `sched_add/list/run_now/toggle/remove` 定时任务

## 规划与进度（plan_* / plan_mode）
- `plan_update` 提交/更新**你自己**的工作步骤清单（整份提交，非增量）——多步任务开工前先建，每推进一步重新提交一次
  - 工具层硬约束（不是靠自觉）：同时只允许 1 个 in_progress；不许 pending 直接跳 completed（必须先经过 in_progress）；最多 20 步
- `plan_show` 看当前清单与进度 / `plan_history` 看历史快照（事后核对有没有「事后批量补完」）/ `plan_clear` 收尾清空
- `plan_mode` Plan Mode 开关：`enter` 进入 / `exit` 退出 / `status` 查看
  - 进入后改动型工具（写文件/改代码/shell 等）在执行前被闸门拒绝，只读探索放行
  - 三阶段：先只读探索消掉能查到的事实 → 再问偏好取舍 → 最后交 `<proposed_plan>`（decision complete）
  - 触发：用户说「先想清楚再动手 / 先出方案 / 先别动手」时主动 enter；方案定了、用户让动手时 exit
  - 默认 2 小时超时自动退出（`ttl_minutes` 可调，0=不超时），不会永久只读
- 分工：plan_* 是我自己的干活步骤，todo_* 是用户的任务清单

## 策略复盘（execution_* / strategy_*）
- `execution_record` 登记执行日志（尤其失败时务必登记卡点）
- `execution_review` 复盘：把失败/卡点聚类提炼成可复用策略，写入策略库
- `strategy_lookup` 执行前按任务类型+目标检索历史策略
- `strategy_feedback` 给策略打效果分（good=true 有效 / false 失效，多次失效降权）

## 规则
- goal 用用户原话；危险步骤加 confirm 确认门；提交即校验，错误当场暴露
- 提交后查 harness_task_status，不重复提交；需求有歧义先问
- 单次能说清的工具调用直接用原工具，不后台化
- 动手前可用 strategy_lookup 查策略；完成后（尤其失败时）execution_record 登记

详细文档：references/steps.md
