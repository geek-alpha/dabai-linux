# -*- coding: utf-8 -*-
"""流式中途重建流的等待时长必须听 Retry-After。

背景（2026-09-20）：agent.py 的流式重试分支自己写了一套
`min(2.0 * stream_attempt, 15.0)`，是全部退避调用点里唯一没走 backoff_delay 的。
服务端在 SSE 中途回 429 + Retry-After: 30 时，实测 _retry_after_of 拿得到 30.0，
代码照睡 2.0s——2s 后重撞等于把限流又加重一次。
"""
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import httpx
from openai import APIStatusError

from agent import _stream_retry_delay
from harness.core import DEFAULT_BACKOFF_CAP


def _api_error(headers=None, status=429):
    """真实 openai 异常对象（带 httpx.Response），不是自造替身。"""
    resp = httpx.Response(
        status, headers=headers or {},
        request=httpx.Request("POST", "https://api.example/v1/chat/completions"))
    return APIStatusError("429 Too Many Requests", response=resp, body=None)


def test_中途429听_retry_after():
    """服务端说等 5s 就等 5s——不是原来的 2s。"""
    assert _stream_retry_delay(1, _api_error({"retry-after": "5"})) == 5.0


def test_听服务端时不叠抖动():
    """叠抖动可能比服务端要求的更早重撞。"""
    err = _api_error({"Retry-After": "5"})
    assert {_stream_retry_delay(1, err) for _ in range(20)} == {5.0}


def test_retry_after_仍受封顶约束():
    """服务端给 3600 也不能真睡一小时。"""
    assert _stream_retry_delay(1, _api_error({"retry-after": "3600"})) == DEFAULT_BACKOFF_CAP


def test_HTTP_date形式也认():
    """RFC 7231 的 IMF-fixdate 形式（与 harness 的 _parse_http_date 同源）。"""
    when = datetime.now(timezone.utc) + timedelta(seconds=12)
    hdr = format_datetime(when, usegmt=True)
    d = _stream_retry_delay(1, _api_error({"retry-after": hdr}))
    assert 10.0 <= d <= 12.0, d


def test_没头就退回指数退避():
    """base=2、max_exp=3、叠抖动：attempt 1/2/3 → 2/4/8 上下 20%。"""
    err = _api_error({})
    for attempt, lo, hi in ((1, 1.6, 2.4), (2, 3.2, 4.8), (3, 6.4, 9.6)):
        for _ in range(50):
            d = _stream_retry_delay(attempt, err)
            assert lo <= d <= hi, (attempt, d)


def test_异常没挂response也不能抛():
    """连接重置类异常没有 .response，退回指数退避而不是崩在流式循环里。"""
    d = _stream_retry_delay(1, ConnectionResetError("connection reset by peer"))
    assert 1.6 <= d <= 2.4


def test_不传异常也安全():
    """调用方漏传异常时不能炸。"""
    assert 1.6 <= _stream_retry_delay(1) <= 2.4


def test_垃圾头退回指数退避():
    """认不出的头当没有，别当成 0 秒打热循环。"""
    d = _stream_retry_delay(1, _api_error({"retry-after": "soon"}))
    assert 1.6 <= d <= 2.4


def test_流式重试循环的调用点接上了():
    """接线守卫：纯函数测不到「调用点被改回去」。

    实测过——把 agent.py 里的 `_delay = _stream_retry_delay(stream_attempt, e)`
    换回 `min(2.0 * stream_attempt, 15.0)`，上面 8 条全绿。所以用 AST 盯住调用点
    本身（含参数名），比字符串匹配稳，比端到端跑 chat_stream 便宜。
    """
    import ast
    from pathlib import Path

    import agent as agent_mod

    tree = ast.parse(Path(agent_mod.__file__).read_text(encoding="utf-8"))
    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "_stream_retry_delay"]
    assert len(calls) == 1, f"流式重试循环必须调用 _stream_retry_delay，实际 {len(calls)} 处"
    names = [a.id for a in calls[0].args if isinstance(a, ast.Name)]
    assert names == ["stream_attempt", "e"], names
