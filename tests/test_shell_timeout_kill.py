# -*- coding: utf-8 -*-
"""shell_run 的硬保证：**设置超时必定断**，且断的是整棵进程树。

为什么要有这组测试（2026-09-22 实测过的三个反例）：
1. `sh -c 'trap "" TERM; sleep 90'` + timeout=3 → 旧实现 8.0s 才返回，消息写
   「已终止整棵进程树」，实测进程返回后仍在跑 —— 因为 terminate_tree 只轮询直接
   子进程（shell=True 下只是最外层 /bin/sh），孙进程继承了 SIG_IGN 就永远活着，
   也永远走不到 SIGKILL 兜底。
2. `(sleep 90 &) ; echo 命令已结束` + timeout=8 → 命令早就跑完，communicate 却要
   等继承管道的孙进程关闭管道，白等满 8 秒。
3. 上层取消（用户点停止）时旧实现完全不杀进程，直接留孤儿。
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "skills" / "code_ops"))

import shell_impl  # noqa: E402

POSIX = os.name != "nt"
_MARK = f"MARKER_SHELLKILL_{os.getpid()}"


def _strays(marker: str = _MARK) -> list:
    r = subprocess.run(["pgrep", "-af", marker], capture_output=True, text=True)
    return [line for line in r.stdout.splitlines() if "pgrep" not in line]


@pytest.fixture(autouse=True)
def _cleanup():
    yield
    subprocess.run(["pkill", "-9", "-f", _MARK], capture_output=True)
    subprocess.run(["pkill", "-9", "-f", "sleep 4242"], capture_output=True)


@pytest.mark.skipif(not POSIX, reason="进程组/SIGKILL 语义仅 POSIX 可测")
def test_timeout_kills_whole_tree_even_if_sigterm_ignored():
    cmd = f"sh -c 'trap \"\" TERM INT; sleep 60 #{_MARK}'"
    t0 = time.time()
    out = asyncio.run(shell_impl.shell_run({"command": cmd, "timeout": 1}))
    elapsed = time.time() - t0
    assert elapsed <= 4.5, f"超时后 {elapsed:.1f}s 才返回（应 ≤ timeout+杀树预算）"
    assert "SIGKILL" in out, f"没有升级到 SIGKILL，说明只发了 SIGTERM：{out}"
    time.sleep(0.3)
    assert not _strays(), "超时返回后进程仍在跑"


@pytest.mark.skipif(not POSIX, reason="POSIX shell 语法")
def test_finished_command_returns_without_waiting_for_pipe_holder():
    """命令已退出就该立刻返回，不为「孙进程占着管道」白等到超时。"""
    t0 = time.time()
    out = asyncio.run(shell_impl.shell_run(
        {"command": "(sleep 4242 &) ; echo 完成", "timeout": 8}))
    elapsed = time.time() - t0
    assert elapsed < 3, f"命令已结束却等了 {elapsed:.1f}s"
    assert "[exit=0]" in out and "完成" in out, out
    assert "后台进程持有输出管道" in out, f"应提示残留后台进程：{out}"


def test_cancel_kills_process():
    async def scenario():
        task = asyncio.create_task(shell_impl.shell_run(
            {"command": f"sh -c 'sleep 60 #{_MARK}'", "timeout": 30}))
        await asyncio.sleep(0.8)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            return True
        return False

    assert asyncio.run(scenario()), "取消应向上抛出 CancelledError"
    time.sleep(0.5)
    assert not _strays(), "取消后进程仍在跑"
