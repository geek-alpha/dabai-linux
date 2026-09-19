"""plan_mode 工具的 handler（进入/退出/查看 Plan Mode）。

真正的状态与闸门在根目录 plan_mode.py —— 那里被 agent.execute_local_tool
和 harness 两层调用，工具只是入口，不是判据。
"""
from __future__ import annotations

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import plan_mode  # noqa: E402


def _do_plan_mode(args) -> str:
    args = args or {}
    action = str(args.get("action") or "status").strip().lower()
    if action == "enter":
        raw = args.get("ttl_minutes")
        try:
            ttl_min = None if raw is None else float(raw)
        except (TypeError, ValueError):
            ttl_min = None
        return plan_mode.enter(str(args.get("topic") or ""), ttl_min)
    if action in ("exit", "leave", "quit"):
        return plan_mode.leave()
    if action == "status":
        return plan_mode.status_text()
    return (
        f"未知 action：{action}。可用值：enter（进入 Plan Mode，只规划不动手）/ "
        "exit（退出，恢复可执行）/ status（查看当前状态）。"
    )

HANDLERS = {"plan_mode": _do_plan_mode}
