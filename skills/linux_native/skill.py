"""Linux 原生技能 —— 把大白接进它所在的那台机器。

工具：linux_senses / linux_guard / linux_service / linux_notify / linux_media
实现：senses_impl（只读感知）+ system_impl（服务与桌面操作）
"""
from __future__ import annotations

import os
import sys

_SKILL_DIR = os.path.dirname(os.path.abspath(__file__))
if _SKILL_DIR not in sys.path:
    sys.path.insert(0, _SKILL_DIR)

import senses_impl  # noqa: E402
import system_impl  # noqa: E402

_SERVICE_ACTIONS = {"list", "status", "logs", "events", "start", "stop", "restart", "reload"}


async def _senses(args: dict) -> str:
    full = args.get("full", True)
    if isinstance(full, str):
        full = full.strip().lower() not in ("false", "0", "no")
    return senses_impl.format_report(senses_impl.senses(full=bool(full)))


async def _guard(args: dict) -> str:
    need = (args.get("need") or "normal").strip().lower()
    if need not in ("light", "normal", "heavy"):
        need = "normal"
    ok, reason = senses_impl.thermal_guard(need)
    icon = "✓" if ok else "✗"
    label = {"light": "轻活", "normal": "普通任务", "heavy": "重活"}[need]
    return f"{icon} {label}放行判定：{'可以执行' if ok else '暂不建议'} —— {reason}"


async def _service(args: dict) -> str:
    action = (args.get("action") or "status").strip().lower()
    unit = args.get("unit") or ""
    lines = args.get("lines")
    try:
        lines = int(lines) if lines is not None else None
    except (TypeError, ValueError):
        lines = None

    # 先校验 action —— 否则非法 action 会误报「缺少 unit」，掩盖真正的错误
    if action not in _SERVICE_ACTIONS:
        return f"✗ 不支持的 action：{action}（可用：{', '.join(sorted(_SERVICE_ACTIONS))}）"

    if action == "list":
        scope = (args.get("scope") or "all").strip().lower()
        return system_impl.service_list(scope if scope in ("user", "system", "all") else "all")
    if action == "events":
        return system_impl.system_events(lines=lines or 30, since=args.get("since") or "2h")
    if not unit:
        return f"✗ action={action} 需要 unit 参数（如 dabai.service）"
    if action == "status":
        return system_impl.service_status(unit)
    if action == "logs":
        return system_impl.service_logs(unit, lines=lines or 40)
    return system_impl.service_control(action, unit, confirm=bool(args.get("confirm")))


async def _notify(args: dict) -> str:
    return system_impl.notify(
        args.get("title") or "",
        args.get("body") or "",
        args.get("urgency") or "normal",
    )


async def _media(args: dict) -> str:
    return system_impl.media(args.get("action") or "now")


HANDLERS = {
    "linux_senses": _senses,
    "linux_guard": _guard,
    "linux_service": _service,
    "linux_notify": _notify,
    "linux_media": _media,
}
