"""把 agent 工作清单（plan_*）合成成任务中心里的一条只读任务。

为什么走「合成」而不是「镜像任务」：
任务中心的 Task.steps 是 append 式字符串列表（task_orchestrator.py:141/274），
承载不了「同一步骤原地改状态」——硬塞会变成只增不减的假进度。
longrun 已经给出正确先例（tools/longrun/status_view.py）：不注册进 orchestrator，
在 /api/tasks 里只读合成一条，结构化数据放 extra 由前端专门渲染。

数据源是 data/agent_plan.json（plan_impl 写），本模块只读不写。
清单为空时返回 None —— 任务中心不该常驻一条空条目刷屏。
"""
from __future__ import annotations

import os
import sys
import time

TASK_ID = "task-agent-plan"

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

    if done == total:
        status = "done"
    else:
        status = "running"

    if running:
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
    left = total - done
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
