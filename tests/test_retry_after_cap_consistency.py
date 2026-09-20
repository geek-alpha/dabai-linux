# -*- coding: utf-8 -*-
"""同一个 Retry-After 打到四个退避出口，都必须被封住、都不叠抖动。

背景（2026-09-20）：四个出口此前各测各的（core/tasks/stream 单点都覆盖过），
但没有一条测试横向断言「同一条 Retry-After: 3600 打到哪儿都封住」。
封顶值分两档：LLM 重连循环 20s（agent.py:3676 显式 cap=20.0），其余 30s
（core.DEFAULT_BACKOFF_CAP）。谁动了某处的 cap 参数，单点测试都不会红。

四个出口：
  1. harness/core.py:200  retry_async            cap=30（默认）
  2. agent.py:3675        _retry_create 重连循环  cap=20（显式）
  3. agent.py:1116        _stream_retry_delay    cap=30（默认）
  4. harness/tasks.py:992 任务级 _leaf_failed     cap=30（默认）
"""
import asyncio
import pathlib
import sys
from pathlib import Path

import httpx
from openai import APIStatusError

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

import agent  # noqa: E402
from agent import _stream_retry_delay  # noqa: E402
from harness.core import DEFAULT_BACKOFF_CAP, retry_async  # noqa: E402
from harness.tasks import Task, TaskSystem  # noqa: E402

HUGE = "3600"                 # 服务端说等一小时
CAP_LLM_RECONNECT = 20.0      # agent.py:3676 的显式 cap，比默认更严


def _api_error(headers, status=429):
    """真实 openai 异常对象（带 httpx.Response），不是自造替身。"""
    resp = httpx.Response(
        status, headers=headers,
        request=httpx.Request("POST", "https://api.example/v1/chat/completions"))
    return APIStatusError("429 Too Many Requests", response=resp, body=None)


# ---------------- 出口 1：harness/core.py retry_async ----------------

def test_出口1_retry_async_封顶(monkeypatch):
    delays = []

    async def fake_sleep(d):
        delays.append(d)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    calls = {"n": 0}

    async def factory():
        calls["n"] += 1
        if calls["n"] < 2:
            raise _api_error({"retry-after": HUGE})
        return "ok"

    assert asyncio.run(retry_async(factory, attempts=3, backoff=1.0)) == "ok"
    assert delays == [DEFAULT_BACKOFF_CAP], delays


# ---------------- 出口 2：agent.py 重连循环（此前无端到端覆盖） ----------------

def _run_llm_reconnect(monkeypatch, headers, fail_times=1):
    """真跑 Agent._retry_create 的重连循环，收集它睡过的时长。

    两个隔离动作：
    - _get_runtime 置空 → 不走 runtime.supervise_llm，落到 agent.py:3659 的内层；
    - 内层 retry_async 换成直通桩 → 收集到的只有外层重连（agent.py:3675）的值，
      否则内层的 30s 会和外层的 20s 混在一起，分不清是谁封的。
    """
    delays = []

    async def fake_sleep(d):
        delays.append(d)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(agent, "_get_runtime", lambda: None)

    async def passthrough(factory, **kw):
        return await factory()

    monkeypatch.setattr("harness.core.retry_async", passthrough)

    a = agent.AIAgent.__new__(agent.AIAgent)   # 绕过 __init__：只碰用到的字段
    a._reasoning_echo_required = False
    state = {"n": 0}

    async def flaky(**kw):
        state["n"] += 1
        if state["n"] <= fail_times:
            raise _api_error(headers)
        return "OK"

    a._create_with_reason_fallback = flaky
    out = asyncio.run(a._retry_create(messages=[{"role": "user", "content": "u"}]))
    assert out == "OK", out
    return delays


def test_出口2_llm重连封顶(monkeypatch):
    """服务端说 3600s，重连循环按自己的 20s 封顶——不能真等一小时。"""
    delays = _run_llm_reconnect(monkeypatch, {"retry-after": HUGE})
    assert delays == [CAP_LLM_RECONNECT], delays


def test_出口2_llm重连不叠抖动(monkeypatch):
    """听服务端时不叠抖动：抖动可能让它比 cap 更晚，取值必须恒定。"""
    got = set()
    for _ in range(3):
        got |= set(_run_llm_reconnect(monkeypatch, {"retry-after": HUGE}))
    assert got == {CAP_LLM_RECONNECT}, got


# ---------------- 出口 3：agent.py 流式中途重建 ----------------

def test_出口3_流式中途封顶():
    assert _stream_retry_delay(1, _api_error({"retry-after": HUGE})) == DEFAULT_BACKOFF_CAP


def test_出口3_流式中途不叠抖动():
    err = _api_error({"retry-after": HUGE})
    assert {_stream_retry_delay(1, err) for _ in range(20)} == {DEFAULT_BACKOFF_CAP}


# ---------------- 出口 4：harness/tasks.py 任务级 ----------------

class _FakeHarness:
    def __init__(self, base):
        self.base_dir = pathlib.Path(base)


def test_出口4_任务级封顶(tmp_path):
    ts = TaskSystem(_FakeHarness(tmp_path))
    delays = []
    ts._enqueue = lambda t, delay=0.0: delays.append(delay)

    async def boom(system, prompt, max_tokens=800):
        raise _api_error({"retry-after": HUGE})

    ts.set_llm_executor(boom)
    t = Task("t1", "演示", "task", {"kind": "llm", "prompt": "hi"}, max_attempts=3)
    asyncio.run(ts._leaf_body(t))
    assert delays == [DEFAULT_BACKOFF_CAP], delays


# ---------------- 横向：同一条头，四个出口都被封住 ----------------

def test_同一条头四出口都被封住(monkeypatch, tmp_path):
    """一条 Retry-After: 3600 打进四个出口，没有一个漏到 3600。"""
    seen = {}

    delays = []

    async def fake_sleep(d):
        delays.append(d)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    calls = {"n": 0}

    async def factory():
        calls["n"] += 1
        if calls["n"] < 2:
            raise _api_error({"retry-after": HUGE})
        return "ok"

    asyncio.run(retry_async(factory, attempts=3, backoff=1.0))
    seen["core.retry_async"] = delays[0]

    seen["agent 重连"] = _run_llm_reconnect(monkeypatch, {"retry-after": HUGE})[0]
    seen["流式中途"] = _stream_retry_delay(1, _api_error({"retry-after": HUGE}))

    ts = TaskSystem(_FakeHarness(tmp_path))
    task_delays = []
    ts._enqueue = lambda t, delay=0.0: task_delays.append(delay)

    async def boom(system, prompt, max_tokens=800):
        raise _api_error({"retry-after": HUGE})

    ts.set_llm_executor(boom)
    asyncio.run(ts._leaf_body(
        Task("t1", "演示", "task", {"kind": "llm", "prompt": "hi"}, max_attempts=3)))
    seen["任务级"] = task_delays[0]

    assert set(seen) == {"core.retry_async", "agent 重连", "流式中途", "任务级"}
    for name, d in seen.items():
        assert d <= DEFAULT_BACKOFF_CAP, f"{name} 漏到 {d}s"
        assert d < 3600, f"{name} 没被封顶：{d}"
    # LLM 重连那档更严：20s，不是 30s
    assert seen["agent 重连"] == CAP_LLM_RECONNECT, seen
