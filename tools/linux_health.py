#!/usr/bin/env python3
"""定期体检 —— 由 systemd timer（Linux）/ 计划任务（Windows）驱动：写日志，异常时提醒。

为什么用 timer / 计划任务而不是自己 sleep 轮询：
    由系统调度，开机自动拉起、错过的会补跑、进程不常驻（零常驻内存）。
    比「后台线程 while True: sleep(600)」省一个常驻进程。

Linux 上日志写 journald 而不是自己的文件：本机 journald 是 Storage=volatile（内存里），
写日志**不碰 SD 卡**，对树莓派的卡寿命友好；同时自带时间索引与轮转。

两个平台能拿到的指标不一样，这是平台能力差异，不是故障：
    Linux   温度 / PSI / zram / swap —— 全部藏在 /sys、/proc 的文件里，直接读
    Windows 没有这两个文件系统，也没有标准库替代；只取跨平台拿得到的
            磁盘剩余、服务端口、进程存活（要温度得走 WMI，不为此引入依赖）

用法：
    python3 tools/linux_health.py            # 体检一次，输出一行状态
    python3 tools/linux_health.py --alert    # 额外在异常时发通知
    python3 tools/linux_health.py --json     # 输出 JSON（给别的程序消费）
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "skills" / "linux_native"))

IS_WINDOWS = os.name == "nt"


def _state_file() -> Path:
    if IS_WINDOWS:
        base = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
        return base / "dabai" / "health_alert.json"
    return Path.home() / ".cache" / "dabai" / "health_alert.json"


_STATE = _state_file()
# 同一档位在这个时间内不重复提醒（避免每 10 分钟弹一次）
_COOLDOWN_SEC = 3600
_ALERT_LEVELS = ("strained", "critical")


def _load_state() -> dict:
    try:
        return json.loads(_STATE.read_text())
    except Exception:
        return {}


def _save_state(d: dict) -> None:
    try:
        _STATE.parent.mkdir(parents=True, exist_ok=True)
        _STATE.write_text(json.dumps(d))
    except Exception:
        pass


def _notify(title: str, body: str, urgency: str = "normal") -> None:
    """异常时提醒一次。两个平台的通知机制完全不同，失败一律静默。

    体检的主产物是日志，通知只是锦上添花 —— 不能因为通知机制缺失把体检本身搞崩
    （旧版在 Windows 上跑 --alert 会直接 AttributeError: os.getuid）。
    """
    if IS_WINDOWS:
        try:
            subprocess.run(["msg", "*", f"{title} {body}"],
                           timeout=8, capture_output=True)
        except Exception:
            pass
        return
    env = os.environ.copy()
    uid = os.getuid()
    env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{uid}")
    env.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path=/run/user/{uid}/bus")
    try:
        subprocess.run(["notify-send", "-u", urgency, title, body],
                       timeout=8, env=env, capture_output=True)
    except Exception:
        pass


def _should_alert(verdict: str, advice: list[str]) -> bool:
    """只在「变严重」或「超过冷却期」时提醒，避免刷屏。"""
    if verdict not in _ALERT_LEVELS:
        return False
    st = _load_state()
    now = time.time()
    # 档位升级（notice→strained→critical）立刻提醒
    rank = {"healthy": 0, "notice": 1, "strained": 2, "critical": 3, "unknown": 0}
    if rank.get(verdict, 0) > rank.get(st.get("last_verdict", ""), 0):
        return True
    return now - float(st.get("last_ts", 0)) > _COOLDOWN_SEC


def _mark_alert(verdict: str, advice: list[str]) -> None:
    _save_state({"last_ts": time.time(), "last_verdict": verdict,
                 "last_advice": advice[:3], "last_ts_str": time.strftime("%Y-%m-%d %H:%M:%S")})


def _service_port() -> str | None:
    """服务端口：从更新器配置里读（install-windows.ps1 写进去的）。"""
    base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    try:
        for line in (Path(base) / "dabai" / "update.conf").read_text(
                encoding="utf-8-sig").splitlines():
            if line.strip().upper().startswith("PORT="):
                return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return None


def _windows_report() -> dict:
    """Windows 侧的体检指标。

    Linux 把硬件状态暴露成 /sys、/proc 里的文件，senses_impl 靠读文件拿温度、PSI、
    zram —— 这些在 Windows 上不存在。这里只取「跨平台拿得到、且真会出问题」的三项：
    磁盘剩余、服务端口、进程存活。缺温度与内存压力是平台能力差异，不是故障。
    """
    if str(_ROOT) not in sys.path:
        sys.path.insert(0, str(_ROOT))
    import platform_compat as pc

    disk = pc.disk_free(str(_ROOT))
    free_gb = round(disk[1] / 1024 ** 3, 1) if disk else None

    port = _service_port()
    port_ok: bool | None = None
    if port:
        import re as _re
        pat = _re.compile(r"[.:]" + _re.escape(port) + r"\s")
        port_ok = any(pat.search(ln) for ln in pc.list_listening_ports())

    py_procs = [p for p in pc.list_processes()
                if "python" in (p.get("name") or "").lower()]

    advice: list[str] = []
    verdict = "healthy"
    if free_gb is not None and free_gb < 2:
        verdict = "critical"
        advice.append(f"磁盘只剩 {free_gb}GB，更新会失败，先清空间")
    elif free_gb is not None and free_gb < 10:
        verdict = "strained"
        advice.append(f"磁盘剩 {free_gb}GB，偏紧")
    if port_ok is False:
        verdict = "critical"
        advice.append(f"服务端口 {port} 没在听 —— 看门狗会拉起，也可手动 --ensure-running")
    if not py_procs:
        advice.append("没有 python 进程在跑")

    return {
        "verdict": verdict,
        "advice": advice,
        "platform": "windows",
        "storage": {"free_gb": free_gb},
        "service": {"port": port, "listening": port_ok, "python_procs": len(py_procs)},
        "note": "Windows 拿不到温度/PSI/zram（无 /sys、/proc），不是故障",
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--alert", action="store_true", help="异常时发通知")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args()

    if IS_WINDOWS:
        d = _windows_report()
        verdict, advice = d["verdict"], d["advice"]
        if args.json:
            print(json.dumps(d, ensure_ascii=False))
        else:
            st, sv = d["storage"], d["service"]
            print(f"verdict={verdict} disk_free={st.get('free_gb')}GB "
                  f"port={sv.get('port')} listening={sv.get('listening')} "
                  f"python_procs={sv.get('python_procs')} "
                  f"advice={' | '.join(advice) or '-'}")
        if args.alert and _should_alert(verdict, advice):
            _notify(f"大白体检：{verdict}", "；".join(advice),
                    urgency="critical" if verdict == "critical" else "normal")
            _mark_alert(verdict, advice)
            print("ALERT_SENT")
        return 0

    try:
        import senses_impl  # type: ignore
    except Exception as e:  # pragma: no cover - 依赖缺失时给出可诊断提示
        print(f"HEALTH_ERROR 无法加载感官模块：{e}")
        return 0

    d = senses_impl.senses()
    verdict, advice = d["verdict"], d["advice"]
    s, m, c, st, me = d["soc"], d["memory"], d["cpu"], d["storage"], d["self"]

    if args.json:
        print(json.dumps(d, ensure_ascii=False))
        return 0

    # 一行式状态（journald 里方便 grep / 看趋势）
    zr = f"{m.get('zram_ratio')}:1" if m.get("zram_ratio") else "n/a"
    print(
        f"verdict={verdict} temp={s.get('temp_c')}C throttled={s.get('throttled_raw')} "
        f"mem_avail={m.get('available_mb')}MB swap={m.get('swap_used_pct')}% "
        f"zram={zr} load1={c.get('load1')} "
        f"disk_free={st.get('free_gb')}GB self_rss={me.get('vmrss')}MB "
        f"advice={' | '.join(advice)}"
    )

    if args.alert and _should_alert(verdict, advice):
        icon = {"strained": "⚠", "critical": "✗"}.get(verdict, "·")
        _notify(f"{icon} 大白体检：{verdict}",
                "；".join(advice),
                urgency="critical" if verdict == "critical" else "normal")
        _mark_alert(verdict, advice)
        print("ALERT_SENT")
    return 0


if __name__ == "__main__":
    sys.exit(main())
