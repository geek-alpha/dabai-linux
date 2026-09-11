"""Linux 系统集成 —— 让大白能「操作」它所在的那台机器，而不只是跑在上面。

覆盖三层：
1. **服务层**：systemd（系统级 + 用户级双 scope）查状态 / 读日志 / 启停 / 看全系统事件
2. **桌面层**：D-Bus 通知、MPRIS 媒体控制
3. **守护层**：把感知（senses_impl）变成决策（是否放行重活）

实测踩到的坑（都写进注释，别重踩）：
- **大白自己是系统级 unit**（`/etc/systemd/system/myservice.service`），
  只查 `systemctl --user` 会「看不见自己」。必须双 scope 探测。
- `journalctl --user -u <unit>` 在本机**恒返回 "No journal files were found"**：
  用户服务日志落在**系统 journal**，要用 `--user-unit=<unit>`；
  系统级 unit 则直接用 `-u <unit>`。
- `journalctl --since=2h` 解析失败，必须是 `--since=-2h`（相对偏移）。
- 不存在的 unit，`systemctl show` 仍返回默认值（PID 0 / 退出码 0），
  直接渲染会伪装成「服务存在但没跑」——必须查 LoadState 识别 not-found。
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import senses_impl as senses


# ---------------------------------------------------------------- 环境与执行

def _env() -> dict:
    """systemctl / journalctl / gdbus 需要的会话环境，缺了自动补。"""
    env = os.environ.copy()
    uid = os.getuid()
    env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{uid}")
    env.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path=/run/user/{uid}/bus")
    return env


def _run(cmd: list[str], timeout: float = 15.0) -> tuple[int, str, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=_env())
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except subprocess.TimeoutExpired:
        return 124, "", f"命令超时（{timeout}s）：{' '.join(cmd)}"
    except FileNotFoundError:
        return 127, "", f"命令不存在：{cmd[0]}"
    except Exception as e:
        return 1, "", f"{type(e).__name__}: {e}"


# ---------------------------------------------------------------- scope 探测

def _scope(unit: str) -> str:
    """判断 unit 属于系统级还是用户级（'system' / 'user' / 'unknown'）。

    大白自己是系统级 unit，代理是用户级 —— 只认一个 scope 必然瞎一半。
    """
    for scope, pre in (("system", []), ("user", ["--user"])):
        rc, out, _ = _run(["systemctl", *pre, "show", unit, "-p", "LoadState"])
        if rc == 0 and out and "LoadState=not-found" not in out:
            return scope
    return "unknown"


def self_unit() -> str | None:
    """大白当前由哪个 systemd unit 托管（读 /proc/self/cgroup）。

    用于区分「操作别的服务」和「操作自己」——后者会切断当前连接。
    """
    try:
        cg = Path("/proc/self/cgroup").read_text()
    except Exception:
        return None
    for line in cg.splitlines():
        path = line.split(":", 2)[-1]
        parts = [p for p in path.split("/") if p.endswith(".service")]
        if parts:
            return parts[-1]
    return None


def _is_self(unit: str) -> bool:
    return unit == self_unit()


def _norm_unit(unit: str) -> str:
    unit = (unit or "").strip()
    if unit and "." not in unit:
        unit += ".service"
    return unit


# ---------------------------------------------------------------- 服务列表

def service_list(scope: str = "all") -> str:
    """列出服务。user=用户级全部；system=系统级仅运行/失败的；all=两者。"""
    blocks: list[str] = []

    if scope in ("user", "all"):
        rc, out, err = _run(["systemctl", "--user", "list-units", "--type=service",
                             "--all", "--no-legend", "--plain"])
        if rc == 0:
            rows = []
            for line in out.splitlines():
                f = line.split(None, 4)
                if len(f) < 4 or not f[0].endswith(".service"):
                    continue
                rows.append(f"  {_mark(f[3])} {f[0]:<34} {f[2]:<8} {f[3]}")
            blocks.append(f"systemd 用户服务（{len(rows)} 个）\n" + "\n".join(rows))
        else:
            blocks.append(f"✗ 用户服务列表读取失败：{err}")

    if scope in ("system", "all"):
        rc, out, err = _run(["systemctl", "list-units", "--type=service",
                             "--state=running,failed", "--no-legend", "--plain"])
        if rc == 0:
            rows = []
            for line in out.splitlines():
                f = line.split(None, 4)
                if len(f) < 4 or not f[0].endswith(".service"):
                    continue
                rows.append(f"  {_mark(f[3])} {f[0]:<38} {f[2]:<8} {f[3]}")
            if len(rows) > 40:
                rows = rows[:40]
                rows.append("  … 已截断（系统服务较多，只看前 40 个）")
            blocks.append(f"systemd 系统服务（仅运行中/失败，{len(rows)} 个）\n" + "\n".join(rows))
        else:
            blocks.append(f"✗ 系统服务列表读取失败：{err}")

    return "\n\n".join(blocks) if blocks else "✗ 读不到服务列表"


def _mark(sub: str) -> str:
    return {"running": "●", "exited": "○", "dead": "·", "failed": "✗"}.get(sub, "·")


# ---------------------------------------------------------------- 单服务状态

def service_status(unit: str) -> str:
    unit = _norm_unit(unit)
    scope = _scope(unit)
    if scope == "unknown":
        return (f"✗ 没有名为 {unit} 的服务（系统级与用户级都查不到）\n"
                "  用 linux_service(action='list') 看全部服务")

    pre = [] if scope == "system" else ["--user"]
    props = ["ActiveState", "SubState", "MainPID", "ExecMainStatus", "NRestarts",
             "MemoryCurrent", "CPUUsageNSec", "ActiveEnterTimestamp",
             "Description", "TasksCurrent", "UnitFileState", "Restart"]
    rc, out, err = _run(["systemctl", *pre, "show", unit, "-p", ",".join(props)])
    if rc != 0 or not out:
        return f"✗ 查不到服务 {unit}：{err or '未知错误'}"

    d: dict[str, str] = {}
    for line in out.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            d[k] = v

    lines = [f"{unit}  ——  {d.get('Description', '')}",
             f"  作用域   {scope} 级（{'需 sudo 才能控制' if scope == 'system' else '用户可控制'}）"
             f"   开机自启 {d.get('UnitFileState', '?')}",
             f"  状态     {d.get('ActiveState', '?')} / {d.get('SubState', '?')}",
             f"  主进程   PID {d.get('MainPID', '?')}   线程 {d.get('TasksCurrent', '?')}",
             f"  重启     {d.get('NRestarts', '?')} 次（策略 {d.get('Restart', '?')}）"
             f"   退出码 {d.get('ExecMainStatus', '?')}"]
    mem = d.get("MemoryCurrent", "")
    if mem.isdigit() and int(mem) > 0:
        lines.append(f"  内存     {round(int(mem) / 1024 / 1024, 1)}MB")
    else:
        lines.append("  内存     不可测（内核未启用 memory cgroup）")
    cpu = d.get("CPUUsageNSec", "")
    if cpu.isdigit():
        lines.append(f"  CPU 累计 {round(int(cpu) / 1e9, 1)}s")
    if d.get("ActiveEnterTimestamp"):
        lines.append(f"  启动于   {d['ActiveEnterTimestamp']}")
    if _is_self(unit):
        lines.append("  ⚠ 这是大白自己的服务：重启会短暂断开当前连接")
    return "\n".join(lines)


# ---------------------------------------------------------------- 日志

def service_logs(unit: str, lines: int = 40, priority: str = "") -> str:
    unit = _norm_unit(unit)
    lines = max(1, min(int(lines or 40), 500))
    scope = _scope(unit)
    if scope == "unknown":
        return f"✗ 没有名为 {unit} 的服务，无法读日志"

    # 系统级用 -u；用户级必须用 --user-unit（--user -u 在本机恒返回 "No journal files"）
    sel = ["-u", unit] if scope == "system" else [f"--user-unit={unit}"]
    cmd = ["journalctl", *sel, "-n", str(lines), "--no-pager", "--output=short"]
    if priority:
        cmd.append(f"-p{priority}")
    rc, out, err = _run(cmd)
    if rc != 0 or not out.strip():
        return (f"✗ 读不到 {unit} 的日志：{err or '无输出'}\n"
                f"  提示：用户服务日志落在系统 journal，须用 --user-unit=<unit>（--user -u 无效）")
    return f"{unit}（{scope} 级）最近 {lines} 行日志：\n" + "\n".join("  " + l for l in out.splitlines())


def system_events(lines: int = 30, since: str = "2h", priority: str = "err") -> str:
    """全系统近期事件 —— 大白能「看见」机器上发生了什么（不限自己的服务）。"""
    lines = max(1, min(int(lines or 30), 300))
    cmd = ["journalctl", "-n", str(lines), "--no-pager", "--output=short"]
    since = _norm_since(since)
    if since:
        cmd.append(f"--since={since}")
    if priority:
        cmd.append(f"-p{priority}")
    rc, out, err = _run(cmd)
    if rc != 0 or not out.strip():
        return f"✓ {since} 内没有 priority≤{priority} 的事件" + (f"（{err}）" if err else "")
    return (f"近 {since} 内 priority≤{priority} 的系统事件（{len(out.splitlines())} 条）：\n"
            + "\n".join("  " + l for l in out.splitlines()))


# journalctl --since 的时间单位必须显式；裸 '10min'/'2h' 会被当绝对时间解析失败
_SINCE_UNITS = {"s": "s", "sec": "s", "secs": "s", "second": "s", "seconds": "s",
                "m": "min", "min": "min", "mins": "min", "minute": "min", "minutes": "min",
                "h": "h", "hr": "h", "hrs": "h", "hour": "h", "hours": "h",
                "d": "d", "day": "d", "days": "d"}


def _norm_since(since: str) -> str:
    """把 '2h' 规范化成 '-2h'（journalctl 要的是相对偏移或绝对时间）。

    实测：`--since=2h` → "Failed to parse timestamp"，`--since=-2h` → 正常。
    """
    s = (since or "").strip()
    if not s:
        return ""
    if s.startswith("-") or " " in s or ":" in s:
        return s
    m = re.fullmatch(r"(\d+)\s*([A-Za-z]+)", s)
    if m:
        unit = _SINCE_UNITS.get(m.group(2).lower())
        if unit:
            return f"-{m.group(1)}{unit}"
    return s


# ---------------------------------------------------------------- 服务控制

def service_control(action: str, unit: str, confirm: bool = False) -> str:
    unit = _norm_unit(unit)
    if not unit:
        return "✗ 必须指定 unit"
    if action not in ("start", "stop", "restart", "reload"):
        return f"✗ 不支持的操作：{action}"

    scope = _scope(unit)
    if scope == "unknown":
        return f"✗ 没有名为 {unit} 的服务"

    is_self = _is_self(unit)
    # 停掉自己 = 大白彻底下线且不会自动回来（显式 stop 不触发 Restart），必须二次确认
    if action == "stop" and is_self and not confirm:
        return (f"⚠ 拒绝执行：{unit} 正是大白自己，stop 之后**不会自动恢复**"
                "（systemd 只对崩溃/被杀重启，显式 stop 不重启）。\n"
                "  确实要下线请传 confirm=true。")

    pre = [] if scope == "system" else ["--user"]
    rc, out, err = _run(["systemctl", *pre, action, unit], timeout=60)
    if rc != 0:
        if scope == "system" and re.search(r"authentication|Access denied|not authorized", err, re.I):
            return (f"✗ {action} {unit} 需要 root：这是系统级服务，当前无免密 sudo。\n"
                    f"  请让用户执行：sudo systemctl {action} {unit}")
        return f"✗ {action} {unit} 失败：{err or out}"

    tail = f"（{out}）" if out else ""
    extra = "\n  ⚠ 当前连接会短暂中断，服务约 3s 后自动回来" if is_self and action == "restart" else ""
    return f"✓ 已 {action} {unit}{tail}{extra}"


# ---------------------------------------------------------------- 桌面层

def notify(title: str, body: str = "", urgency: str = "normal") -> str:
    """桌面通知 —— 通过 D-Bus 发到用户会话。"""
    if not title:
        return "✗ 需要 title"
    cmd = ["notify-send", "-u", urgency if urgency in ("low", "normal", "critical") else "normal",
           title, body or ""]
    rc, out, err = _run(cmd, timeout=8)
    if rc != 0:
        # 回退到 portal 的 D-Bus 接口
        rc2, _out2, err2 = _run([
            "gdbus", "call", "--session",
            "--dest", "org.freedesktop.portal.Desktop",
            "--object-path", "/org/freedesktop/portal/desktop",
            "--method", "org.freedesktop.portal.Notification.AddNotification",
            "dabai", "{'title': <'%s'>, 'body': <'%s'>}" % (title, body or ""),
        ], timeout=8)
        if rc2 != 0:
            return f"✗ 通知发送失败：{err or err2}"
    return f"✓ 已通知：{title}" + (f" —— {body}" if body else "")


_MPRIS_NS = "org.mpris.MediaPlayer2"
_MEDIA_ACTIONS = ("now", "list", "play", "pause", "next", "prev")


def _mpris_players() -> list[str]:
    rc, out, _ = _run(["gdbus", "call", "--session",
                       "--dest", "org.freedesktop.DBus",
                       "--object-path", "/org/freedesktop/DBus",
                       "--method", "org.freedesktop.DBus.ListNames"])
    if rc != 0:
        return []
    return [n for n in re.findall(r"'([^']+)'", out) if n.startswith(_MPRIS_NS + ".")]


def media(action: str = "now") -> str:
    """MPRIS 媒体 —— 知道用户在听/看什么，并能控制播放。"""
    if action not in _MEDIA_ACTIONS:
        return f"✗ 不支持的操作：{action}（可用 {'/'.join(_MEDIA_ACTIONS)}）"

    players = _mpris_players()
    if not players:
        return "当前没有 MPRIS 播放器（浏览器需开启媒体会话 / 装 MPRIS 扩展才能被看到）"
    if action == "list":
        return "在播的播放器：\n" + "\n".join(f"  · {p}" for p in players)

    name = players[0]
    if action != "now":
        method = {"play": "Play", "pause": "Pause", "next": "Next", "prev": "Previous"}[action]
        rc, _o, err = _run(["gdbus", "call", "--session", "--dest", name,
                            "--object-path", "/org/mpris/MediaPlayer2",
                            "--method", f"org.mpris.MediaPlayer2.Player.{method}"])
        return f"✓ {action} 已发送到 {name}" if rc == 0 else f"✗ {action} 失败：{err}"

    rc, out, err = _run(["gdbus", "call", "--session", "--dest", name,
                         "--object-path", "/org/mpris/MediaPlayer2",
                         "--method", "org.freedesktop.DBus.Properties.GetAll",
                         "org.mpris.MediaPlayer2.Player"])
    if rc != 0:
        return f"✗ 读 {name} 失败：{err}"

    def field(key: str) -> str:
        m = re.search(rf"'{key}':\s*<'(.*?)'>", out)
        return m.group(1) if m else ""

    bits = [field("PlaybackStatus") or "未知"]
    title = field("xesam:title")
    if title:
        artist = ""
        if "'xesam:artist'" in out:
            seg = out.split("'xesam:artist'", 1)[1]
            seg = seg.split("]", 1)[0]
            artist = " / ".join(re.findall(r"'([^']+)'", seg))
        bits.append(f"{artist} - {title}" if artist else title)
    pos = re.search(r"'Position':\s*<(\d+)>", out)
    if pos:
        bits.append(f"{int(pos.group(1)) / 1e6:.0f}s")
    return "  正在播放：" + "   ".join(bits)
