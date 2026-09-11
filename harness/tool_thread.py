"""工具专用线程池：与默认 executor（memory DB / TTS / 其它 asyncio.to_thread）隔离。

背景（2026-08-30）：把同步工具处理器放进 `asyncio.to_thread` 后，工具线程与
记忆库写入、TTS、媒体等 111 处 `asyncio.to_thread` 共享同一个进程级默认线程池。
一旦长耗时/超时后无法强杀的僵尸工具线程堆积，默认池被占满，`add_message` 等
核心写入就会排队 → 整个对话"无声卡死"。这里给工具单独一个**有界**线程池：
- 工具僵尸线程最多占满 8 个工具线程，永远不影响记忆库/TTS 等默认池；
- 队列深度可观测，心跳能如实告诉用户"排队等待线程"而不是假装"正在执行"。

线程池按需懒创建，进程退出时随解释器回收（ThreadPoolExecutor 线程为 daemon）。
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import threading
from typing import Any, Callable

_TOOL_POOL = None
_TOOL_POOL_LOCK = threading.Lock()
_ACTIVE = 0          # 正在工具线程里执行的工具数
_QUEUED = 0          # 排队等待空闲工具线程的任务数
_STATS_LOCK = threading.Lock()


def _pool(max_workers: int = 8) -> concurrent.futures.ThreadPoolExecutor:
    global _TOOL_POOL
    if _TOOL_POOL is None:
        with _TOOL_POOL_LOCK:
            if _TOOL_POOL is None:
                _TOOL_POOL = concurrent.futures.ThreadPoolExecutor(
                    max_workers=max_workers,
                    thread_name_prefix="dabai-tool",
                )
    return _TOOL_POOL


def _wrap(fn: Callable, *args):
    """在工具线程内执行的包装：维护 活跃/排队 计数。"""
    global _ACTIVE, _QUEUED
    with _STATS_LOCK:
        _QUEUED -= 1
        _ACTIVE += 1
    try:
        return fn(*args)
    finally:
        with _STATS_LOCK:
            _ACTIVE -= 1


async def run_in_tool_thread(fn: Callable, *args, max_workers: int = 8) -> Any:
    """把同步函数提交到工具专用线程池执行（事件循环永不阻塞）。"""
    global _QUEUED
    with _STATS_LOCK:
        _QUEUED += 1
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        _pool(max_workers), _wrap, fn, *args)


def tool_thread_stats() -> dict:
    """当前工具线程池状态：{active, queued, max_workers}。"""
    with _STATS_LOCK:
        active, queued = _ACTIVE, _QUEUED
    try:
        mw = _TOOL_POOL._max_workers if _TOOL_POOL is not None else 8
    except Exception:
        mw = 8
    return {"active": active, "queued": queued, "max_workers": mw}


def reset_tool_pool() -> None:
    """测试/热重载用：重置线程池。"""
    global _TOOL_POOL, _ACTIVE, _QUEUED
    with _TOOL_POOL_LOCK:
        if _TOOL_POOL is not None:
            _TOOL_POOL.shutdown(wait=False, cancel_futures=True)
            _TOOL_POOL = None
        _ACTIVE = 0
        _QUEUED = 0
