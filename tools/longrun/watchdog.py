#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""长跑引擎心跳看门狗（跨平台）—— 进程活着但不再推进（心跳过期）→ 强制重启。

为什么需要它：systemd 的 ``Restart=always``（Windows 侧是计划任务的「失败后重启」）
只能救「进程死了」，救不了「进程卡住」—— 卡在网络 IO 上的循环会永远活着、永远不推进。
心跳由 runner.py 每轮开始时刷新（长活期间每 60s 刷一次），卡住的循环不写心跳，于是被抓出来。

平台差异只体现在「怎么把服务拉起来」：
    Linux    服务 = systemd user unit（systemctl --user start/restart）
    Windows  服务 = 计划任务（schtasks /run）；任务没注册时降级为分离进程直接启动

存活判定两个平台是同一套：**进程在跑 且 心跳新鲜**。比 systemctl is-active 更贴近
「它到底有没有在干活」——单元 active 但心跳停住正是最该抓的那种故障。

用法：
    python3 tools/longrun/watchdog.py             # 检查并自愈一次（给 timer / 计划任务调）
    python3 tools/longrun/watchdog.py --dry-run   # 只报告，不动手

环境变量（沿用旧 watchdog.sh 的名字，便于平滑替换）：
    LONGRUN_RUN_DIR   运行时目录，默认 <仓库>/data/longrun
    LONGRUN_MAX_AGE   心跳过期阈值（秒），默认 1800
    LONGRUN_UNIT      Linux 单元名，默认 dabai-longrun.service
    LONGRUN_TASK      Windows 计划任务名，默认 DabaiLongrun
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import platform_compat as pc  # noqa: E402

RUN_DIR = Path(os.environ.get("LONGRUN_RUN_DIR") or (_ROOT / "data" / "longrun"))
HEARTBEAT = RUN_DIR / "heartbeat"
STOP_FLAG = RUN_DIR / "STOP"
MAX_AGE = int(os.environ.get("LONGRUN_MAX_AGE") or 1800)
UNIT = os.environ.get("LONGRUN_UNIT") or "dabai-longrun.service"
WATCHDOG = os.environ.get("LONGRUN_WD_UNIT") or "dabai-longrun-watchdog.timer"
TASK = os.environ.get("LONGRUN_TASK") or "DabaiLongrun"
WD_TASK = os.environ.get("LONGRUN_WD_TASK") or "DabaiLongrunWatchdog"
RUNNER = _ROOT / "tools" / "longrun" / "runner.py"


def log(msg: str) -> None:
    print(msg, flush=True)


def _run(cmd: list[str], timeout: int = 60) -> tuple[int, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           creationflags=pc.no_window_flags())
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except FileNotFoundError:
        return 127, f"命令不存在：{cmd[0]}"
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, str(exc)


def heartbeat_age() -> int | None:
    """心跳文件年龄（秒）；没有心跳文件返回 None（视为从未推进）。"""
    try:
        return int(time.time() - HEARTBEAT.stat().st_mtime)
    except OSError:
        return None


def runner_pids() -> list[int]:
    """正在跑 --loop 的 runner 进程。用它而不是 systemctl 判定「活着」。"""
    pids = []
    for p in pc.process_tree():
        cmd = p.get("cmdline") or ""
        if "runner.py" in cmd and "--loop" in cmd:
            pids.append(int(p["pid"]))
    return pids


def sweep_orphans(dry: bool = False) -> list[str]:
    """清理「父进程已死」的 agent 子进程。

    runner 被强杀时，它拉起的 agent 子进程会被系统收养（Linux ppid→1）继续跑、继续烧
    token。只清父进程已死的，绝不碰活着的 runner 正在用的子进程 —— 后者正是长跑本身。

    两个平台的「孤儿」判据不同：Linux 上 ppid 变成 1，Windows 上 ppid 仍指向一个
    已经不存在的 pid（Windows 不做 reparent）。
    """
    procs = pc.process_tree()
    alive = {int(p["pid"]) for p in procs}
    notes: list[str] = []
    for p in procs:
        cmd = p.get("cmdline") or ""
        if "dabai_cli" not in cmd or "longrun_" not in cmd:
            continue
        pid = int(p["pid"])
        ppid = int(p.get("ppid") or 0)
        if ppid <= 0:
            continue
        if not (ppid == 1 or ppid not in alive):
            continue
        if dry:
            notes.append(f"[演练] 会清理孤儿 agent 进程 {pid}（父进程 {ppid} 已死）")
            continue
        if pc.kill_pid(pid, force=True):
            notes.append(f"清理孤儿 agent 进程 {pid}（父进程 {ppid} 已死）")
        else:
            notes.append(f"孤儿 agent 进程 {pid} 清理失败（可能已退出）")
    return notes


def _start_detached() -> tuple[bool, str]:
    """兜底：不经计划任务，直接分离启动 runner（Windows 上任务没注册时用）。"""
    if not RUNNER.is_file():
        return False, f"找不到 {RUNNER}"
    try:
        subprocess.Popen(
            [sys.executable, str(RUNNER), "--loop"],
            cwd=str(_ROOT), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            **pc.spawn_kwargs(),
        )
        return True, "已分离启动 runner.py --loop"
    except OSError as exc:
        return False, f"启动失败：{exc}"


def start_service() -> tuple[bool, str]:
    if pc.IS_WINDOWS:
        # 看门狗任务可能是上次 stop 时被禁用的，先恢复——否则它不会再来救场
        _run(["schtasks", "/change", "/tn", WD_TASK, "/enable"])
        rc, out = _run(["schtasks", "/run", "/tn", TASK])
        if rc == 0:
            return True, f"已启动计划任务 {TASK}"
        ok, msg = _start_detached()
        return ok, f"计划任务 {TASK} 启动失败（{out.strip()[:120]}）→ {msg}"
    rc, out = _run(["systemctl", "--user", "start", UNIT])
    if rc == 0:
        return True, f"已启动 {UNIT}"
    # 单元处于 failed 状态时 start 会被拒，先清掉失败态再试一次
    _run(["systemctl", "--user", "reset-failed", UNIT])
    rc, out = _run(["systemctl", "--user", "start", UNIT])
    if rc == 0:
        return True, f"已启动 {UNIT}（先清了 failed 状态）"
    return False, f"启动 {UNIT} 失败：{out.strip()[:200]}"


def restart_service(pids: list[int]) -> tuple[bool, str]:
    if pc.IS_WINDOWS:
        for pid in pids:
            pc.terminate_tree(pid, timeout=15)
        ok, msg = start_service()
        return ok, f"已重启：{msg}"
    rc, out = _run(["systemctl", "--user", "restart", UNIT])
    if rc == 0:
        return True, f"已重启 {UNIT}"
    return False, f"重启 {UNIT} 失败：{out.strip()[:200]}"


def stop_service() -> tuple[bool, str]:
    """停引擎：先断看门狗再停本体——反过来做，看门狗下一次触发就把它拉回来了。"""
    if pc.IS_WINDOWS:
        _run(["schtasks", "/change", "/tn", WD_TASK, "/disable"])
        _run(["schtasks", "/end", "/tn", TASK])
        killed = []
        for pid in runner_pids():
            if pc.terminate_tree(pid, timeout=15)[0]:
                killed.append(str(pid))
        tail = f"（结束 runner pid {', '.join(killed)}）" if killed else ""
        return True, f"已停止计划任务 {TASK}，看门狗任务已禁用{tail}"
    _run(["systemctl", "--user", "stop", WATCHDOG])
    rc, out = _run(["systemctl", "--user", "stop", UNIT])
    return (rc == 0, f"已停止 {UNIT}" if rc == 0 else f"停止 {UNIT} 失败：{out.strip()[:200]}")


def main() -> int:
    ap = argparse.ArgumentParser(description="长跑引擎心跳看门狗")
    ap.add_argument("--dry-run", action="store_true", help="只报告，不动手")
    args = ap.parse_args()
    dry = args.dry_run

    if STOP_FLAG.exists():
        log(f"急停文件存在（{STOP_FLAG}），看门狗不干预")
        return 0

    pids = runner_pids()
    age = heartbeat_age()

    if not pids:
        log(f"runner 未运行 → 拉起")
        for n in sweep_orphans(dry):
            log(n)
        if dry:
            log("[演练] 会拉起 runner")
            return 0
        ok, msg = start_service()
        log(msg)
        return 0 if ok else 1

    if age is None or age > MAX_AGE:
        shown = "无心跳文件" if age is None else f"心跳过期 {age}s"
        log(f"{shown}（阈值 {MAX_AGE}s）→ 重启（pid {', '.join(map(str, pids))}）")
        if dry:
            log("[演练] 会重启并清扫孤儿")
            return 0
        ok, msg = restart_service(pids)
        log(msg)
        for n in sweep_orphans(dry):
            log(n)
        return 0 if ok else 1

    log(f"存活，心跳 {age}s 前（阈值 {MAX_AGE}s），pid {', '.join(map(str, pids))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
