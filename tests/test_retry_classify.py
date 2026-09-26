"""retry 分类与退避的回归测试。

修的是两个真实误判（对比 Anthropic SDK 的重试策略时发现）：
错误正文里出现独立的 500/503 这类数字，会被当成 HTTP 状态码 —— 于是
「余额不足，需要 500 积分」这种重试一万次也不会成功的业务错误，
被判成瞬时错误反复退避重试，白等 1s+2s 还掩盖真实原因。

分类规则：异常上的 HTTP 状态码优先，拿不到才退回关键词匹配；
关键词里的数字必须带 HTTP 上下文前缀（"HTTP 503" 算，"500 积分" 不算）。
"""
import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness.core import (  # noqa: E402
    DEFAULT_BACKOFF_CAP,
    RetryableError,
    _status_code_of,
    is_transient_error,
    retry_async,
    retry_decision,
)


class _HttpErr(Exception):
    def __init__(self, code, msg=""):
        self.status_code = code
        super().__init__(msg or f"HTTP {code}")


# ---------- 文本路径：正文里的数字不算状态码 ----------

@pytest.mark.parametrize("msg", [
    "账户余额不足，需要 500 积分",
    "invalid request: max_tokens 500 exceeds limit",
    "额度不足，还剩 503 次调用",
    "余额不足，请充值",
    "model gpt-4o-500 not found",
])
def test_正文数字不被当成状态码(msg):
    assert is_transient_error(Exception(msg)) is False


@pytest.mark.parametrize("msg", [
    "connect to 127.0.0.1:5000 refused",
    "HTTP 503 Service Unavailable",
    "HTTP 429 rate limit",
    "upstream bad gateway",
    "request timed out",
    "connection reset by peer",
])
def test_真瞬时错误仍被识别(msg):
    assert is_transient_error(Exception(msg)) is True


# ---------- 状态码路径 ----------

@pytest.mark.parametrize("code", [400, 401, 403, 404, 422])
def test_4xx默认致命(code):
    assert is_transient_error(_HttpErr(code)) is False


@pytest.mark.parametrize("code", [408, 409, 429])
def test_可重试的4xx(code):
    """409 可重试是刻意对齐 Anthropic SDK 的 _RETRYABLE_4XX（并发冲突值得重试）。"""
    assert is_transient_error(_HttpErr(code)) is True


@pytest.mark.parametrize("code", [500, 502, 503, 504, 529])
def test_5xx可重试(code):
    assert is_transient_error(_HttpErr(code)) is True


def test_状态码优先于正文():
    """正文写着 not found，状态码是 503 —— 以码为准。"""
    assert is_transient_error(_HttpErr(503, "upstream not found")) is True


def test_response上的状态码也能读到():
    class _Resp:
        status_code = 503

    class _Err(Exception):
        response = _Resp()

    assert is_transient_error(_Err("boom")) is True


def test_errno不被误读成状态码():
    """OSError.errno=111（ECONNREFUSED）不是 HTTP 111，不能从 `code`/`errno` 取码。"""
    e = OSError(111, "Connection refused")
    assert e.errno == 111
    assert _status_code_of(e) is None
    assert is_transient_error(e) is True  # 靠 ConnectionError 分支


def test_主动声明通道():
    assert is_transient_error(RetryableError("自定义瞬时失败")) is True


# ---------- 退避行为 ----------

def _run(coro):
    return asyncio.run(coro)


def test_退避带抖动(monkeypatch):
    delays = []

    async def fake_sleep(d):
        delays.append(d)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    calls = {"n": 0}

    async def factory():
        calls["n"] += 1
        if calls["n"] < 3:
            raise _HttpErr(503)
        return "ok"

    assert _run(retry_async(factory, attempts=3, backoff=1.0)) == "ok"
    assert len(delays) == 2
    assert 0.8 <= delays[0] <= 1.2, delays      # 1.0 * [0.8, 1.2)
    assert 1.6 <= delays[1] <= 2.4, delays      # 2.0 * [0.8, 1.2)


def test_致命错误不重试(monkeypatch):
    async def fake_sleep(d):
        raise AssertionError("致命错误不该进 sleep")

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    calls = {"n": 0}

    async def factory():
        calls["n"] += 1
        raise _HttpErr(401, "invalid api key")

    with pytest.raises(_HttpErr):
        _run(retry_async(factory, attempts=3))
    assert calls["n"] == 1


def test_退避封顶(monkeypatch):
    delays = []

    async def fake_sleep(d):
        delays.append(d)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    async def factory():
        raise _HttpErr(503)

    with pytest.raises(_HttpErr):
        _run(retry_async(factory, attempts=12, backoff=10.0))
    assert delays, "应该至少重试过一次"
    assert max(delays) <= DEFAULT_BACKOFF_CAP, delays


def test_尊重RetryAfter(monkeypatch):
    """服务端给了 Retry-After 就照它睡，不再叠加抖动。"""
    delays = []

    async def fake_sleep(d):
        delays.append(d)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    class _Resp:
        headers = {"retry-after": "7"}

    class _RateLimited(Exception):
        status_code = 429
        response = _Resp()

    calls = {"n": 0}

    async def factory():
        calls["n"] += 1
        if calls["n"] < 2:
            raise _RateLimited()
        return "ok"

    assert _run(retry_async(factory, attempts=3, backoff=1.0)) == "ok"
    assert delays == [7.0]


def test_RetryAfter也封顶(monkeypatch):
    """服务端写 Retry-After: 99999 时不能真的睡一天。"""
    delays = []

    async def fake_sleep(d):
        delays.append(d)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    class _Resp:
        headers = {"Retry-After": "99999"}

    class _RateLimited(Exception):
        status_code = 429
        response = _Resp()

    calls = {"n": 0}

    async def factory():
        calls["n"] += 1
        if calls["n"] < 2:
            raise _RateLimited()
        return "ok"

    _run(retry_async(factory, attempts=3, backoff=1.0))
    assert delays == [DEFAULT_BACKOFF_CAP]


def test_尊重RetryAfter的HTTPdate形式(monkeypatch):
    """429 用 HTTP-date 写等待时间（RFC 7231 的另一种合法形式）也要听。

    只认秒数时这里会退回 1s 指数退避——服务端要求等 20s 而我们 1s 后重撞。
    """
    from datetime import datetime, timedelta, timezone
    from email.utils import format_datetime

    delays = []

    async def fake_sleep(d):
        delays.append(d)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    hdr = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=20), usegmt=True)

    class _Resp:
        headers = {"retry-after": hdr}

    class _RateLimited(Exception):
        status_code = 429
        response = _Resp()

    calls = {"n": 0}

    async def factory():
        calls["n"] += 1
        if calls["n"] < 2:
            raise _RateLimited()
        return "ok"

    assert _run(retry_async(factory, attempts=3, backoff=1.0)) == "ok"
    assert delays, "应该重试过一次"
    assert 17.0 <= delays[0] <= 20.0, (hdr, delays)


# ---------- 网关「无说明文字的 400」：上游抽风，可重试 ----------
# 2026-09-26 19:59 实测：opencode zen/go 网关偶发 HTTP 400，body 只有请求里的
# model 名（{"model": "deepseek-v4.1-flash"}）—— 定时任务《掌柜巡店·每日》首轮
# 调用 13 秒即被判死，同一分钟主对话轮报同一个错，而 1 分钟后同形状请求
# （curl 探针 + 主链路）全部 200。旧分类把 400 一律当确定性错误，于是把上游
# 抽风读成「请求有病」，任务整轮白跑、当天不再重试。


class _BodyErr(Exception):
    """openai SDK 异常形状：status_code + 解析后的 body。"""

    def __init__(self, code, body):
        self.status_code = code
        self.body = body
        super().__init__(f"Error code: {code} - {body}")


def test_无说明文字的400判为可重试():
    e = _BodyErr(400, {"model": "deepseek-v4.1-flash"})
    assert retry_decision(e) is True
    assert is_transient_error(e) is True


@pytest.mark.parametrize("body", [
    {"error": {"message": "The `reasoning_content` in the thinking mode must be passed back"}},
    {"message": "max_tokens 500 exceeds limit"},
    {"detail": "prompt is too long"},
    {"model": "deepseek-v4.1-flash", "extra": "x"},   # 多带字段 = 不是那个形状
    {},
])
def test_带说明或形状不符的400仍不重试(body):
    """反例：真·请求有病的 400 重发一万次还是同一个结果，重试只是白等。"""
    e = _BodyErr(400, body)
    assert retry_decision(e) is False
    assert is_transient_error(e) is False


def test_同类形状的非400不受影响():
    assert retry_decision(_BodyErr(401, {"model": "x"})) is False   # 鉴权：不重试
    assert retry_decision(_BodyErr(503, {"model": "x"})) is True    # 5xx：本来就重试
