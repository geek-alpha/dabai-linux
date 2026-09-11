#!/usr/bin/env python3
"""定期体检 —— 由 systemd timer 驱动：把系统状态写进 journald，异常时弹桌面通知。

为什么用 systemd timer 而不是自己 sleep 轮询：
    timer 由系统调度，开机自动拉起、错过的会补跑（Persistent=true）、
    进程不常驻（零常驻内存）、日志自动进 journal。
    这是 Linux 原生做法，比「后台线程 while True: sleep(600)」省一个常驻进程。

为什么日志写 journald 而不是自己的文件：
    本机 journald 是 Storage=volatile（内存里），写日志**不碰 SD 卡**，
    对树莓派的卡寿命友好；同时自带时间索引与轮转，不用自己管。

用法：
    python3 tools/linux_health.py            # 体检一次，输出一行状态
    python3 tools/linux_health.py --alert    # 额外在异常时发桌面通知
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

try:
    import senses_impl  # type: ignore
except Exception as e:  # pragma: no cover - 依赖缺失时给出可诊断提示
    print(f"HEALTH_ERROR 无法加载感官模块：{e}")
    sys.exit(0)

_STATE = Path.home() / ".cache" / "dabai" / "health_alert.json"
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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--alert", action="store_true", help="异常时发桌面通知")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args()

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
