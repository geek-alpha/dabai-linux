# -*- coding: utf-8 -*-
"""流式静默看门狗 _watchdog_stream 的行为契约测试。

对标 Anthropic _IdleClock 的三个精华：
- 事件驱动：每个 chunk 到达即重置计时（wait_for 天然保证），不是轮询；
- 状态区分：首包（宽容慢思考）与块间隔（严判卡死）两档计时；
- 超时即瞬时错误：_StreamIdleTimeout 必须被 _is_transient_stream_error 认作
  可重建，否则卡死会整轮报废而不是前缀去重续传。
"""
import asyncio

import pytest

from agent import (
    _StreamIdleTimeout,
    _is_transient_stream_error,
    _stream_first_byte_timeout,
    _stream_idle_timeout,
    _watchdog_stream,
)


def _run(coro):
    return asyncio.run(coro)


async def _fast_stream(chunks):
    """正常流：chunk 密集到达，零延迟。"""
    for c in chunks:
        yield c


async def _hang_after(chunks, hang_at):
    """吐完前 hang_at 个 chunk 后永久挂起（模拟提供方静默卡死）。"""
    for i, c in enumerate(chunks, 1):
        yield c
        if i >= hang_at:
            await asyncio.Event().wait()  # 挂起直到被 cancel


async def _hang_first():
    """首包就挂起（模拟慢思考/黑洞连接）。"""
    await asyncio.Event().wait()
    yield "never"


def test_normal_stream_passthrough_no_false_kill():
    """正常流：全部 chunk 透传，一个不杀。"""

    async def _body():
        got = []
        async for c in _watchdog_stream(
                _fast_stream(["a", "b", "c"]),
                first_byte_timeout=60.0, idle_timeout=60.0):
            got.append(c)
        return got

    assert _run(_body()) == ["a", "b", "c"]


def test_steady_state_idle_triggers():
    """稳态静默：前 2 个 chunk 正常，之后挂起 → idle 超时触发。"""

    async def _body():
        with pytest.raises(_StreamIdleTimeout) as ei:
            async for _ in _watchdog_stream(
                    _hang_after(["a", "b"], hang_at=2),
                    first_byte_timeout=60.0, idle_timeout=0.05):
                pass
        return str(ei.value)

    msg = _run(_body())
    assert "steady state" in msg
    assert "timeout" in msg  # 文本兜底依赖这个字样


def test_first_byte_idle_triggers():
    """首包静默：第一个 chunk 就挂起 → first byte 超时（宽容档）。"""

    async def _body():
        with pytest.raises(_StreamIdleTimeout) as ei:
            async for _ in _watchdog_stream(
                    _hang_first(),
                    first_byte_timeout=0.05, idle_timeout=0.05):
                pass
        return str(ei.value)

    assert "first byte" in _run(_body())


def test_natural_end_propagates():
    """流自然结束：StopAsyncIteration 透传，async for 正常退出。"""

    async def _body():
        got = []
        async for c in _watchdog_stream(
                _fast_stream(["x"]),
                first_byte_timeout=60.0, idle_timeout=60.0):
            got.append(c)
        return got

    assert _run(_body()) == ["x"]


def test_timeout_flagged_transient_for_reconnect():
    """超时异常必须是「瞬时错误」→ 流式循环会走前缀去重重建，而不是整轮报废。"""
    err = _StreamIdleTimeout("stream steady state idle timeout after 45s")
    assert _is_transient_stream_error(err) is True


def test_idle_timeout_config_reads_settings(monkeypatch):
    """块间隔超时：从 settings.json 读取（线上已配 300s），钳制在 [5, 600]。"""
    import json
    from agent import load_config

    with open("settings.json", encoding="utf-8") as f:
        _cfg = json.load(f)
    v = float((_cfg.get("agent") or {}).get("stream_idle_timeout", 45) or 45)
    assert _stream_idle_timeout() == v
    assert 5.0 <= _stream_idle_timeout() <= 600.0
    # 无配置时回落到默认 45
    monkeypatch.setattr("agent.load_config", lambda: {})
    assert _stream_idle_timeout() == 45.0


def test_first_byte_timeout_config_reads_settings(monkeypatch):
    """首包超时：默认 180s（settings 未配），钳制在 [10, 900]；无配置回落默认。"""
    assert _stream_first_byte_timeout() == 180.0
    assert 10.0 <= _stream_first_byte_timeout() <= 900.0
    monkeypatch.setattr("agent.load_config", lambda: {})
    assert _stream_first_byte_timeout() == 180.0
