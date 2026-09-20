# -*- coding: utf-8 -*-
"""Retry-After 的边界值与扩展头：对齐 Anthropic SDK 的取值规则。

背景（2026-09-20，改前实测）三处缺口：
  retry-after: 0 / -5 / 已过期 HTTP-date → 全部返回 0.0 → 退避 0 秒 = 热循环
  retry-after-ms: 1500（非标准毫秒头）   → 完全不认，退回指数 ~1s
  x-should-retry: false + 500            → 照样重试（服务端明确说别重试）

权威依据（本机已装的库源码，非记忆）：
  anthropic 1.6.0 / openai 3.14.1 `_base_client.py:821`
      if retry_after is not None and retry_after > 0:   ← 非正值退回指数退避
  `_base_client.py:786` 优先认 retry-after-ms（毫秒比整数秒精确）
  `_base_client.py:839` 优先认 x-should-retry（服务端显式表态）
"""
import builtins

import pytest

from harness.core import _retry_after_of, backoff_delay, is_transient_error


class _Resp:
    def __init__(self, headers):
        self.headers = headers


class _Err(Exception):
    def __init__(self, headers=None, status=429):
        super().__init__(f"HTTP {status}")
        if headers is not None:
            self.response = _Resp(headers)
        self.status_code = status


# ---------- 非正值：退回指数退避，绝不打热循环 ----------

@pytest.mark.parametrize("hdr", ["0", "-5", "-0.001", "0.0", "00"])
def test_非正秒数退回指数退避(hdr):
    """服务端回 0/负数时照睡 0 秒 = 热循环，而热循环正是被限流时最不该做的。"""
    assert _retry_after_of(_Err({"retry-after": hdr})) is None


def test_已过期日期退回指数退避():
    from datetime import datetime, timedelta, timezone
    from email.utils import format_datetime

    hdr = format_datetime(datetime.now(timezone.utc) - timedelta(seconds=60), usegmt=True)
    assert _retry_after_of(_Err({"retry-after": hdr})) is None


@pytest.mark.parametrize("bad", [0.0, -1.0, -999.0, float("nan")])
def test_backoff_delay_拒绝非正retry_after(bad):
    """即使调用方绕过 _retry_after_of 直接传坏值，也不能返回 0 秒。"""
    d = backoff_delay(1, base=1.0, jitter=False, retry_after=bad)
    assert d > 0, (bad, d)
    assert d == 1.0


# ---------- retry-after-ms ----------

def test_毫秒头被认():
    assert _retry_after_of(_Err({"retry-after-ms": "1500"})) == 1.5


def test_毫秒头优先于秒头():
    """同时给两个头时认更精确的毫秒值（Anthropic 的取值顺序）。"""
    assert _retry_after_of(_Err({"retry-after-ms": "1500", "retry-after": "30"})) == 1.5


def test_毫秒头坏了退回秒头():
    e = _Err({"retry-after-ms": "abc", "retry-after": "30"})
    assert _retry_after_of(e) == 30.0


def test_毫秒头也封顶():
    assert _retry_after_of(_Err({"retry-after-ms": "99999999"})) == 30.0


def test_毫秒头为零退回指数退避():
    assert _retry_after_of(_Err({"retry-after-ms": "0"})) is None


# ---------- x-should-retry ----------

def test_服务端说别重试就不重试():
    assert is_transient_error(_Err({"x-should-retry": "false"}, 500)) is False
    assert is_transient_error(_Err({"x-should-retry": "false"}, 429)) is False


def test_服务端说重试就重试():
    assert is_transient_error(_Err({"x-should-retry": "true"}, 400)) is True


def test_头大小写与空白都要认():
    assert is_transient_error(_Err({"X-Should-Retry": " FALSE "}, 500)) is False


@pytest.mark.parametrize("status,expect", [(429, True), (401, False)])
def test_头值无法识别时走常规分类(status, expect):
    assert is_transient_error(_Err({"x-should-retry": "maybe"}, status)) is expect


def test_RetryableError_优先于服务端头():
    """业务侧主动声明值得重试时，不看服务端头。"""
    from harness.core import RetryableError

    e = RetryableError("boom")
    e.response = _Resp({"x-should-retry": "false"})
    assert is_transient_error(e) is True


# ---------- 端到端：出口层不再热循环 ----------

def test_流式出口_非正retry_after也不热循环():
    import agent

    assert agent._stream_retry_delay(1, _Err({"retry-after": "0"})) > 0


def test_流式出口_认毫秒头():
    import agent

    assert agent._stream_retry_delay(1, _Err({"retry-after-ms": "2500"})) == 2.5


# ---------- harness 不可用时的兜底 ----------

def test_harness不可用时兜底封顶对齐(monkeypatch):
    """兜底公式的封顶必须与 DEFAULT_BACKOFF_CAP 一致（原来是 15s）。"""
    import agent

    real_import = builtins.__import__

    def _boom(name, *a, **k):
        if str(name).startswith("harness"):
            raise ImportError("harness 不可用")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", _boom)
    assert agent._stream_retry_delay(1, None) == 2.0
    assert agent._stream_retry_delay(20, None) == 30.0


# ---------- 确定 vs 猜测：文本兜底不许推翻明确表态 ----------

def test_retry_decision_区分确定与猜测():
    from harness.core import retry_decision

    assert retry_decision(_Err({"x-should-retry": "false"}, 500)) is False
    assert retry_decision(_Err({}, 500)) is True
    assert retry_decision(_Err({}, 400)) is False
    assert retry_decision(_Err({}, 429)) is True
    # 无状态码无表态：只能靠文本猜，必须返回 None 而不是 False
    assert retry_decision(RuntimeError("connection reset by peer")) is None


def test_文本兜底不许推翻服务端表态():
    """x-should-retry: false + 消息含 'rate limit' 时，agent 的重连分类器不能重试。

    改前实测：harness 返回 False 后 _is_retryable_llm_error 继续做文本匹配，
    "rate limit" 命中 → True，服务端的明确表态被一句猜测推翻。
    """
    import agent

    e = _Err({"x-should-retry": "false"}, 429)
    e.args = ("Rate limit reached for gpt-4",)
    assert agent._is_retryable_llm_error(e) is False
    assert agent._is_transient_stream_error(e) is False
