"""长任务与批量任务技能 —— 大白把多步长任务/批量任务交给 harness 任务系统。

工具全部是 harness.tasks（TaskSystem）的薄封装：
- harness_flow_submit / harness_batch_submit  提交（后台执行，立即返回任务ID）
- harness_task_status / harness_task_list     查询（进度到步骤/条目粒度）
- harness_task_cancel / harness_task_retry    控制（取消 / 保留成果重试）

执行特性（由任务系统保证，本技能只做参数透传与结果裁剪）：
- 断点续跑：服务重启自动恢复，已成功步骤不重算；
- 步骤级超时+重试退避，流程级整体看门狗；失败策略 abort / continue；
- 同轮多步骤并行、批量并发受控；工具步骤经 harness 稳定路由 + runtime 监督。

合并自原 3 个技能：
- tasks（harness 任务系统 9 工具）
- todo（TODO 任务清单 9 工具 + 提醒调度线程）
- scheduler（定时任务 5 工具）
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

# 合并自原 todo 技能：TODO 任务清单 9 个工具 + 提醒调度线程
# 合并自原 scheduler 技能：定时任务 5 个工具
_SKILL_DIR = os.path.dirname(os.path.abspath(__file__))
if _SKILL_DIR not in sys.path:
    sys.path.insert(0, _SKILL_DIR)
import todo_impl  # noqa: E402
import sched_impl  # noqa: E402


def on_load(ctx):
    """技能加载时启动 todo 提醒调度线程（幂等）。"""
    try:
        todo_impl.on_load(ctx)
    except Exception as e:  # noqa: BLE001
        print(f'[tasks] todo 提醒调度器启动失败: {e}')


def on_unload(ctx):
    """技能卸载时停止 todo 提醒调度线程。"""
    try:
        todo_impl.on_unload(ctx)
    except Exception as e:  # noqa: BLE001
        print(f'[tasks] todo 提醒调度器停止失败: {e}')


def _ts():
    from harness import get_harness
    ts = get_harness().tasks
    ts.ensure_started()
    return ts


def _ok(data, extra: str = "") -> str:
    return json.dumps({"ok": True, **data}, ensure_ascii=False, default=str) + extra


def _err(e) -> str:
    return json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False)


async def _mirror_to_center(tid: str, title: str, goal: str) -> None:
    """把 TaskSystem 任务镜像到任务中心（前端任务中心实时可见）。

    创建 orchestrator 镜像任务（channel=sub），后台轮询 TaskSystem 状态，
    把步骤里程碑/终态同步过去；任务中心打开即拉全量快照。

    镜像条带 extra.harness_tid：/api/tasks 已直接合并 TaskSystem 的 flow/batch
    任务，两者是同一个任务，列表侧按 harness_tid 去重只留原生条（信息更全），
    否则任务中心会同时出现两条同名任务。TaskSystem 不可用时镜像条仍在，作为兜底。
    """
    try:
        from task_orchestrator import get_orchestrator
        orch = get_orchestrator()
        task = await orch.create(
            kind="flow", title=title or "Flow 流程",
            ws=None, brief=goal or title or "",
            channel="sub",
            extra={"harness_tid": tid},   # 供 /api/tasks 去重（原生条进度/步骤更全）
            auto_run=False,               # 状态由本镜像轮询推进，不擅自置 running
        )
    except Exception:
        return  # 任务中心不可用时静默降级（TaskSystem 本身照常执行）

    async def _poll():
        ts = _ts()
        last_steps: set = set()
        try:
            while True:
                st = ts.status(tid)
                if st is None:
                    await orch.set_error(task, f"任务 {tid} 不存在")
                    return
                state = str(st.get("state") or "pending")
                res = st.get("result")
                steps = (res or {}).get("steps") if isinstance(res, dict) else None
                if isinstance(steps, dict):
                    for sid, s in steps.items():
                        if sid in last_steps:
                            continue
                        sstate = s.get("state")
                        if sstate == "succeeded":
                            last_steps.add(sid)
                            r = str(s.get("result") or "")[:120]
                            await orch.add_step(task, f"✅ {sid}: {r}" if r else f"✅ {sid}")
                        elif sstate == "failed":
                            last_steps.add(sid)
                            await orch.add_step(task, f"❌ {sid}: {str(s.get('error') or '')[:120]}")
                if state in ("succeeded", "failed", "cancelled"):
                    if state == "succeeded":
                        final = ""
                        if isinstance(steps, dict):
                            done = [str(s.get("result")) for s in steps.values()
                                    if s.get("state") == "succeeded" and s.get("result")]
                            if done:
                                final = done[-1][:2000]
                        await orch.set_result(task, final or "流程完成", "done")
                    elif state == "failed":
                        await orch.set_error(task, str(st.get("error") or "流程失败")[:2000])
                    else:
                        await orch.set_status(task, "cancelled")
                    return
                await asyncio.sleep(2)
        except Exception:
            pass

    asyncio.ensure_future(_poll())


async def harness_flow_plan(args: dict) -> str:
    """规划长任务：三言两语 → 理解复述 + 步骤方案（校验/纠错后返回，可选直接开跑）。"""
    try:
        ts = _ts()
        autostart = bool(args.get("autostart"))
        plan = await ts.plan_flow(str(args.get("goal") or ""),
                                  hints=str(args.get("hints") or ""),
                                  critique=True if autostart else None)  # 直跑时强制自评兜底
        if plan.get("needs_clarification"):
            # 信息不足：把问题带回给模型 → 问用户；绝不含糊开跑
            return _ok({"needs_clarification": True,
                        "questions": plan.get("questions") or [],
                        "understanding": plan.get("understanding") or "",
                        "hint": "关键信息缺失——先向用户提出这些问题，答案放入 hints 后重新规划"})
        if plan.get("ok") and autostart:
            tid = ts.submit_flow(
                name=(plan.get("understanding") or "")[:40] or "规划任务",
                goal=plan.get("goal") or "",
                steps=plan.get("steps") or [],
                policy=str(args.get("policy") or "abort"),
            )
            return _ok({"planned": True, "submitted": True, "task_id": tid,
                        "understanding": plan.get("understanding"),
                        "autofixed": plan.get("autofixed"),
                        "hint": "已按规划开始后台执行，稍后用 harness_task_status 查进度。"})
        return json.dumps(plan, ensure_ascii=False, default=str)
    except Exception as e:
        return _err(e)


async def harness_flow_submit(args: dict) -> str:
    try:
        tid = _ts().submit_flow(
            name=str(args.get("name") or "未命名流程"),
            goal=str(args.get("goal") or ""),
            steps=args.get("steps") or [],
            policy=str(args.get("policy") or "abort"),
            timeout=float(args["timeout"]) if args.get("timeout") else None,
        )
        asyncio.ensure_future(_mirror_to_center(
            tid, str(args.get("name") or "未命名流程"),
            str(args.get("goal") or "")))
        return _ok({"task_id": tid,
                    "hint": "已后台执行并挂到任务中心；用 harness_task_status 查进度，不要反复重复提交。"})
    except Exception as e:
        return _err(e)


async def harness_flow_dsl_submit(args: dict) -> str:
    """把一段声明式 Flow 类定义（Python 代码）提交给 TaskSystem 后台执行。

    flow_code 里用 @start / @listen 装饰器定义 Flow 子类（可省略 import，
    已预置 Flow/start/listen），提交后断点续跑/失败反思/LLM 预算全生效。
    """
    try:
        code = str(args.get("flow_code") or "").strip()
        if not code:
            return _err("flow_code 不能为空：需要一段定义 Flow 子类的 Python 代码")
        from harness.flow_dsl import Flow, start, listen
        ns = {"Flow": Flow, "start": start, "listen": listen}
        exec(compile(code, "<flow_dsl>", "exec"), ns)
        flow_cls = next((v for v in ns.values()
                         if isinstance(v, type) and issubclass(v, Flow) and v is not Flow),
                        None)
        if flow_cls is None:
            return _err("代码里没有定义 Flow 子类（class XXX(Flow): ...）")
        flow = flow_cls()
        tid = flow.submit_to_tasks(_ts(),
                                   name=str(args.get("name") or ""),
                                   goal=str(args.get("goal") or ""))
        asyncio.ensure_future(_mirror_to_center(
            tid, str(args.get("name") or flow_cls.__name__),
            str(args.get("goal") or "")))
        return _ok({"task_id": tid, "flow_class": flow_cls.__name__,
                    "hint": "已后台执行并挂到任务中心；用 harness_task_status 查进度，不要反复重复提交。"})
    except Exception as e:
        return _err(e)
async def harness_batch_submit(args: dict) -> str:
    try:
        tid = _ts().submit_batch(
            name=str(args.get("name") or "批量任务"),
            tool=str(args.get("tool") or ""),
            items=args.get("items") or [],
            concurrency=int(args["concurrency"]) if args.get("concurrency") else None,
        )
        return _ok({"task_id": tid,
                    "hint": "已后台并行执行；用 harness_task_status 查进度。"})
    except Exception as e:
        return _err(e)


def _compact_status(st: dict) -> dict:
    """status 瘦身版：轮询场景给模型看的最小信息量（省 token）。

    步骤只留 状态/错误/结果截断120字；流程终态时附最终摘要（400字）。
    detail=true 时返回全量。"""
    out = {k: st.get(k) for k in
           ("id", "name", "kind", "state", "progress", "status", "goal",
            "waiting_confirm", "error")}
    if st.get("kind") == "flow" and isinstance((st.get("result") or {}).get("steps"), dict):
        steps = {}
        for sid, s in st["result"]["steps"].items():
            r = s.get("result")
            steps[sid] = {"state": s.get("state"),
                          "error": (s.get("error") or "")[:80],
                          "result": (r[:120] if isinstance(r, str)
                                     else str(r)[:120]) if s.get("state") == "succeeded" else None}
        out["steps"] = steps
        if st.get("state") == "succeeded" and steps:
            last = sorted(st["result"]["steps"].items())[-1][1].get("result")
            if isinstance(last, str) and last:
                out["final"] = last[:400]
    elif st.get("kind") == "batch":
        r = st.get("result") or {}
        out["batch"] = {"count": r.get("count"), "ok": r.get("ok"),
                        "failed": r.get("failed")}
        if st.get("state") in ("succeeded", "failed") and r.get("failures"):
            out["batch"]["failures"] = {k: str(v)[:100] for k, v in
                                        list(r["failures"].items())[:5]}
    return out


async def harness_task_status(args: dict) -> str:
    try:
        st = _ts().status(str(args.get("task_id") or ""))
        if st is None:
            return _err(f"任务不存在: {args.get('task_id')}")
        if args.get("detail"):
            return _ok(st)
        return _ok(_compact_status(st))
    except Exception as e:
        return _err(e)


async def harness_task_list(args: dict) -> str:
    try:
        ts = _ts()
        jobs = ts.list_tasks(state=args.get("state") or None,
                             kind=None, limit=int(args.get("limit") or 10))
        flows = [j for j in jobs if j["kind"] in ("flow", "batch")]
        out = {"tasks": flows or jobs}
        reports = ts.drain_reports()
        if reports:
            out["completed_since_last_check"] = reports  # 完成汇报：自动带回，免去反复轮询
        return _ok(out)
    except Exception as e:
        return _err(e)


async def harness_task_cancel(args: dict) -> str:
    try:
        ok = _ts().cancel(str(args.get("task_id") or ""))
        return _ok({"cancelled": ok}) if ok else _err("任务不存在或已结束")
    except Exception as e:
        return _err(e)


async def harness_task_confirm(args: dict) -> str:
    """批准/拒绝流程中等待确认的危险步骤（approve=true 批准，false 拒绝）。"""
    try:
        ts = _ts()
        tid = str(args.get("task_id") or "")
        approve = bool(args.get("approve", True))
        ok, msg = (ts.approve_step if approve else ts.reject_step)(
            tid, str(args.get("note") or ""))
        if not ok:
            return _err(msg)
        return _ok({"result": msg,
                    "hint": ("已批准，流程继续执行" if approve else
                             "已拒绝该步骤，流程将按失败策略处理")})
    except Exception as e:
        return _err(e)


async def harness_task_retry(args: dict) -> str:
    try:
        ok = _ts().retry(str(args.get("task_id") or ""))
        return _ok({"retried": ok}) if ok else _err("任务不存在或仍在运行")
    except Exception as e:
        return _err(e)


HANDLERS = {
    "harness_flow_plan": harness_flow_plan,
    "harness_flow_submit": harness_flow_submit,
    "harness_flow_dsl_submit": harness_flow_dsl_submit,
    "harness_batch_submit": harness_batch_submit,
    "harness_task_status": harness_task_status,
    "harness_task_list": harness_task_list,
    "harness_task_cancel": harness_task_cancel,
    "harness_task_retry": harness_task_retry,
    "harness_task_confirm": harness_task_confirm,
    # ---- 合并自原 todo 技能（9 个 TODO 任务清单工具）----
    "todo_breakdown": todo_impl._do_breakdown,
    "todo_create": todo_impl._do_create,
    "todo_plan": todo_impl._do_plan,
    "todo_list": todo_impl._do_list,
    "todo_get": todo_impl._do_get,
    "todo_update": todo_impl._do_update,
    "todo_subtask": todo_impl._do_subtask,
    "todo_remind": todo_impl._do_remind,
    "todo_delete": todo_impl._do_delete,
    # ---- 合并自原 scheduler 技能（5 个定时任务工具）----
    "sched_add": sched_impl.sched_add,
    "sched_list": sched_impl.sched_list,
    "sched_run_now": sched_impl.sched_run_now,
    "sched_toggle": sched_impl.sched_toggle,
    "sched_remove": sched_impl.sched_remove,
}
