"""把 agent 工作清单（plan_*）合成成任务中心里的一条只读任务。

为什么走「合成」而不是「镜像任务」：
任务中心的 Task.steps 是 append 式字符串列表（task_orchestrator.py:141/274），
承载不了「同一步骤原地改状态」——硬塞会变成只增不减的假进度。
longrun 已经给出正确先例（tools/longrun/status_view.py）：不注册进 orchestrator，
在 /api/tasks 里只读合成一条，结构化数据放 extra 由前端专门渲染。

数据源由 plan_impl._plan_path() 决定（按用户分目录，回退 skills/tasks/data/agent_plan.json）。
本模块只读，唯一例外是 clear_done()：它转调 plan_impl 的清空，自己不碰文件。
清单为空时返回 None —— 任务中心不该常驻一条空条目刷屏。
"""
from __future__ import annotations

import os
import sys
import time

TASK_ID = "task-agent-plan"

# 清单绑在「当前这件事」上：干完了就该 done，没干完而被搁置就该如实说「已中断」。
# 不这么判，没跑完的清单会永远挂在 running —— 实测踩过：工作全做完并入了仓，
# 任务中心却一直显示「2/5 进行中」，用户以为还在跑。
# 阈值不能短：一轮里跑长任务几十分钟不更新是常态，误报「中断」比不报更糟。
STALE_AFTER = 1800.0


def _stale_after() -> float:
    """阈值可用 DABAI_PLAN_STALE_SEC 覆盖（测试要造陈旧清单，不能真等半小时）。"""
    try:
        return float(os.environ.get("DABAI_PLAN_STALE_SEC") or STALE_AFTER)
    except ValueError:
        return STALE_AFTER

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TASKS_DIR = os.path.join(_ROOT, "skills", "tasks")


def _impl():
    if _TASKS_DIR not in sys.path:
        sys.path.insert(0, _TASKS_DIR)
    import plan_impl
    return plan_impl


def _agent_meta() -> dict:
    return {
        "name": "工作清单",
        "icon": "📋",
        "color": "#f59e0b",
        "desc": "白头凤当前这件事的工作步骤（plan_update 维护）。只读：改状态请直接说，"
                "或让它自己 plan_update。",
    }


def clear_done() -> bool:
    """清单已全部完成时清空数据源，返回是否清了（任务中心「清除已完成」调用）。

    实测踩过：合成条目不在 orchestrator / Harness 注册表里，/api/tasks/clear 够不着它，
    用户按「清除已完成」看到条目消失一秒、下一轮轮询又原样回来 —— 像是卡住了。
    没完成的清单一律不动。
    """
    try:
        return bool(_impl().clear_finished())
    except Exception:
        return False


def snapshot(full: bool = False):
    """合成任务中心条目；没有清单时返回 None。任何异常都不该拖垮任务中心列表。"""
    try:
        return _snapshot(full)
    except Exception:
        return None


def _snapshot(full: bool):
    data = _impl().read_plan()
    plan = data.get('plan') or []
    if not plan:
        return None

    done = sum(1 for s in plan if s.get('status') == 'completed')
    running = [s for s in plan if s.get('status') == 'in_progress']
    total = len(plan)

    updated_at = data.get('updated_at') or 0
    idle_sec = max(0, int(time.time() - updated_at)) if updated_at else 0
    # 只给「有活没干完」的陈旧清单扣中断帽子：全部完成的清单不该被标中断。
    stale = bool(done < total and updated_at and idle_sec > _stale_after())

    if done == total:
        status = "done"
    elif stale:
        status = "stalled"
    else:
        status = "running"

    left = total - done
    if stale:
        title = (f"工作清单 · {done}/{total} · ⏸ 已中断"
                 f"（{idle_sec // 60} 分钟没动，{left} 步未收尾）")
    elif running:
        title = f"工作清单 · {done}/{total} · 进行中：{running[0]['step'][:40]}"
    elif done == total:
        title = f"工作清单 · {total}/{total} 全部完成"
    else:
        title = f"工作清单 · {done}/{total} · ⚠️ 没有进行中的步骤"

    # steps 只放「焦点」摘要（进行中 / 刚完成 / 还剩几步）：全表由前端读
    # extra.plan 渲染成带状态的清单，两处都放全表等于同一屏看两遍同样的东西。
    steps = []
    if running:
        steps.append(f"◐ 进行中：{running[0]['step']}")
    finished = [s for s in plan if s.get('status') == 'completed']
    if finished:
        steps.append(f"● 刚完成：{finished[-1]['step']}")
    if left:
        steps.append(f"○ 还剩 {left} 步")
    if data.get('explanation'):
        steps.append(f"调整说明：{data['explanation']}")

    extra = {
        "plan_view": True,
        "plan": {
            "steps": plan,
            "updated_at": data.get('updated_at') or 0,
            "created_at": data.get('created_at') or 0,
            "explanation": data.get('explanation'),
            "updates": data.get('updates') or 0,
            "done": done,
            "total": total,
            "stale": stale,
            "idle_sec": idle_sec,
        },
    }

    ts = data.get('updated_at') or data.get('created_at') or time.time()
    base = {
        "id": TASK_ID,
        "kind": "plan",
        "channel": "plan",
        "title": title,
        "status": status,
        "steps": steps,
        "result": "",
        "error": "",
        "confirm": False,
        "extra": extra,
        "dsh_session_id": "",
        "agent": _agent_meta(),
        "created_at": int((data.get('created_at') or ts) * 1000),
        "updated_at": int(ts * 1000),
    }
    if full:
        base["brief"] = "白头凤当前这件事的工作步骤（plan_update 维护）。"
        base["logs"] = []
    return base
