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
