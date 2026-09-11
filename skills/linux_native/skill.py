"""Linux 原生技能 —— 把大白接进它所在的那台机器。

工具：linux_senses / linux_guard / linux_service / linux_notify / linux_media
      linux_gpio / linux_process / linux_net / linux_storage / linux_audit
实现：senses_impl（只读感知）+ system_impl（服务与桌面操作）
      + gpio_impl（硬件支配）+ sysadmin_impl（进程/网络/存储/审计）
"""
from __future__ import annotations

import os
import sys

_SKILL_DIR = os.path.dirname(os.path.abspath(__file__))
if _SKILL_DIR not in sys.path:
    sys.path.insert(0, _SKILL_DIR)

import gpio_impl  # noqa: E402
import senses_impl  # noqa: E402
import sysadmin_impl  # noqa: E402
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


def _as_bool(v, default: bool = False) -> bool:
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() not in ("false", "0", "no", "")


def _as_int(v):
    """转 int；转不了返回 None（调用方自己报错，不静默用默认值）。"""
    if v is None or str(v).strip() == "":
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


async def _gpio(args: dict) -> str:
    action = (args.get("action") or "list").strip().lower()
    pin = _as_int(args.get("pin"))
    if args.get("pin") not in (None, "") and pin is None:
        return f"✗ pin 必须是数字（BCM 编号），收到 {args.get('pin')!r}"

    if action == "list":
        return gpio_impl.list_pins(only_general=_as_bool(args.get("only_general")))
    if action == "summary":
        return gpio_impl.summary()
    if action == "read":
        if pin is None:
            return "✗ action=read 需要 pin 参数（BCM 编号，如 17）"
        return gpio_impl.read_pin(pin)
    if action == "write":
        if pin is None:
            return "✗ action=write 需要 pin 参数"
        val = _as_int(args.get("value"))
        if val not in (0, 1):
            return f"✗ value 必须是 0 或 1，收到 {args.get('value')!r}"
        return gpio_impl.write_pin(pin, val, release=_as_bool(args.get("release")))
    if action == "blink":
        if pin is None:
            return "✗ action=blink 需要 pin 参数"
        return gpio_impl.blink(
            pin,
            times=_as_int(args.get("times")) or 3,
            interval=args.get("interval") or 0.3,
        )
    if action == "led":
        name = args.get("led") or args.get("name") or ""
        value = args.get("value")
        if not name or value is None or str(value).strip() == "":
            return gpio_impl.led_list()
        return gpio_impl.led_set(str(name), str(value))
    return f"✗ 不支持的 action：{action}（可用：list/summary/read/write/blink/led）"


async def _process(args: dict) -> str:
    action = (args.get("action") or "list").strip().lower()
    pid = _as_int(args.get("pid"))
    if args.get("pid") not in (None, "") and pid is None:
        return f"✗ pid 必须是数字，收到 {args.get('pid')!r}"

    if action == "list":
        return sysadmin_impl.process_list(
            sort_by=args.get("sort_by") or "cpu",
            limit=_as_int(args.get("limit")) or 15,
        )
    if action not in ("detail", "signal", "tune"):
        return f"✗ 不支持的 action：{action}（可用：list/detail/signal/tune）"
    if pid is None:
        return f"✗ action={action} 需要 pid 参数"
    if action == "detail":
        return sysadmin_impl.process_detail(pid)
    if action == "signal":
        return sysadmin_impl.process_signal(pid, args.get("signal") or "TERM")
    return sysadmin_impl.process_tune(
        pid, nice=_as_int(args.get("nice")), affinity=args.get("affinity"),
    )


async def _net(args: dict) -> str:
    action = (args.get("action") or "ports").strip().lower()
    if action == "ports":
        return sysadmin_impl.net_ports(exposure_only=_as_bool(args.get("exposure_only")))
    if action == "conns":
        return sysadmin_impl.net_conns(limit=_as_int(args.get("limit")) or 15)
    if action == "ifaces":
        return sysadmin_impl.net_ifaces()
    if action == "exposure":
        return sysadmin_impl.net_exposure()
    return f"✗ 不支持的 action：{action}（可用：ports/conns/ifaces/exposure）"


async def _storage(args: dict) -> str:
    action = (args.get("action") or "overview").strip().lower()
    path = args.get("path") or "/home/wxf"
    if action == "overview":
        return sysadmin_impl.storage_overview()
    if action == "usage":
        return sysadmin_impl.storage_usage(
            path, depth=_as_int(args.get("depth")) if args.get("depth") is not None else 1,
            limit=_as_int(args.get("limit")) or 12,
        )
    if action == "bigfiles":
        return sysadmin_impl.storage_bigfiles(
            path,
            min_mb=_as_int(args.get("min_mb")) or 50,
            limit=_as_int(args.get("limit")) or 15,
        )
    if action == "clean":
        return sysadmin_impl.storage_clean_candidates()
    return f"✗ 不支持的 action：{action}（可用：overview/usage/bigfiles/clean）"


async def _audit(args: dict) -> str:
    return sysadmin_impl.audit()


HANDLERS = {
    "linux_senses": _senses,
    "linux_guard": _guard,
    "linux_service": _service,
    "linux_notify": _notify,
    "linux_media": _media,
    "linux_gpio": _gpio,
    "linux_process": _process,
    "linux_net": _net,
    "linux_storage": _storage,
    "linux_audit": _audit,
}
