#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""输出上限修复层（max_tokens 超渠道上限 400）的回归契约。

背景：_is_retryable_llm_error 对这类错误判 False → 直接 raise，整轮报废。但错误是
「请求本身有病」：本地 tool_max_tokens 允许配到 16384（agent.py:_tool_max_tokens），
渠道上限可能只有 8192，于是每一轮都在同一个参数上撞死，用户看到的是「AI 坏了」。

对齐 Hermes turn_recovery.py:3-7 的 recovery-chain 语义：修好原地 continue，不消耗
重试次数。判据抄 Hermes model_metadata.py:1395-1451 —— 必须先分清「输出上限」
（改小 max_tokens）和「输入超长」（压缩上下文），判反了就是死循环。
"""
import asyncio
import sys
import types
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

import agent  # noqa: E402

# 各渠道真实措辞（照 Hermes model_metadata.py:1334-1346 的 pattern 表）
DASHSCOPE = ("Error code: 400 - {'error': {'message': 'Range of max_tokens should be "
             "[1, 8192]', 'type': 'invalid_request_error'}}")
ANTHROPIC = ("Error code: 400 - {'error': {'message': 'max_tokens: 16384 > 8192, which is "
             "the maximum allowed number of output tokens for claude-sonnet-4-20250514'}}")
AZURE = ("Error code: 400 - max_tokens is too large: 16384. This model supports at most "
         "4096 completion tokens.")
SCALEWAY = "max_completion_tokens is limited to 16384 for glm-5.2"
GENERIC_CAP = "This model exceeds model's maximum output tokens (8192)"
AVAILABLE = "max_tokens: 100000 = available_tokens: 10000"

# 输入超长：修法相反（压缩上下文），绝不能被当成输出上限
CTX_OVERFLOW = ("This model's maximum context length is 65536 tokens. However, you "
                "requested 70000 tokens. Please reduce the length of the messages.")
PROMPT_TOO_LONG = "prompt is too long: 210000 tokens > 200000 maximum"
BALANCE_403 = ("Error code: 403 - {'error': {'code': 'access_denied', 'message': "
               "'Access restricted. Deposit required to unlock premium models.'}}")


class _FakeRuntime:
    async def supervise_llm(self, kind, fn):
        return await fn()


def _mk_agent():
    a = agent.AIAgent.__new__(agent.AIAgent)   # 绕过 __init__：单测只要这几个字段
    a._output_cap = 0
    a._reasoning_echo_required = False
    return a


# ---------------- 解析：各渠道措辞都能抠出上限 ----------------

@pytest.mark.parametrize("text,expect,label", [
    (DASHSCOPE, 8192, "DashScope 区间上界即上限"),
    (ANTHROPIC, 8192, "Anthropic 取右侧天花板"),
    (AZURE, 4096, "Azure supports at most"),
    (SCALEWAY, 16384, "Scaleway limited to"),
    (GENERIC_CAP, 8192, "exceeds model's maximum output tokens (N)"),
    (AVAILABLE, 10000, "Anthropic = available_tokens: N"),
])
def test_解析渠道输出上限(text, expect, label):
    assert agent._parse_output_cap(text) == expect, label


@pytest.mark.parametrize("text,label", [
    (CTX_OVERFLOW, "上下文超长（reduce the length）"),
    (PROMPT_TOO_LONG, "prompt 超长"),
    (BALANCE_403, "余额不足"),
    ("Connection error.", "网络错误"),
    ("", "空文本"),
])
def test_输入超长与无关错误抠不出上限(text, label):
    assert agent._parse_output_cap(text) is None, label


# ---------------- 判定：输出上限 vs 输入超长 ----------------

def test_认得出是输出上限():
    assert agent._is_output_cap_error(AZURE) is True
    assert agent._is_output_cap_error(DASHSCOPE) is True


@pytest.mark.parametrize("text,label", [
    (CTX_OVERFLOW, "输入超长：含 reduce the length"),
    (PROMPT_TOO_LONG, "输入超长：prompt too long"),
    (BALANCE_403, "余额不足"),
    ("Connection error.", "网络错误"),
])
def test_输入超长不被判成输出上限(text, label):
    assert agent._is_output_cap_error(text) is False, label


# ---------------- 参数压缩 ----------------

def test_压缩只改已有的键且不新增():
    kw = {"max_tokens": 16384, "model": "m"}
    assert agent._clamp_output_tokens(kw, 8192) is True
    assert kw["max_tokens"] == 8192
    assert "max_completion_tokens" not in kw


def test_已在上限内不动():
    kw = {"max_tokens": 4096}
    assert agent._clamp_output_tokens(kw, 8192) is False
    assert kw["max_tokens"] == 4096


def test_两个键名都压():
    kw = {"max_tokens": 16384, "max_completion_tokens": 20000}
    assert agent._clamp_output_tokens(kw, 8192) is True
    assert kw["max_tokens"] == 8192
    assert kw["max_completion_tokens"] == 8192


# ---------------- 自愈路径（集成） ----------------

def _run_retry_create(a, monkeypatch, **kw):
    monkeypatch.setattr(agent, "_get_runtime", lambda: _FakeRuntime())
    return asyncio.run(a._retry_create(**kw))


def test_撞到输出上限自动调低并重发(monkeypatch):
    calls = []

    async def fake_create(**kw):
        calls.append(kw)
        if len(calls) == 1:
            raise Exception(DASHSCOPE)
        return "OK"

    a = _mk_agent()
    a._create_with_reason_fallback = fake_create
    out = _run_retry_create(a, monkeypatch, model="m",
                            messages=[{"role": "user", "content": "u"}],
                            max_tokens=16384)
    assert out == "OK"
    assert calls[1]["max_tokens"] == 8192      # 第二次已按错误里的上限压住
    assert a._output_cap == 8192               # 记住，后续轮前置压住


def test_修复不消耗重试也不退避(monkeypatch):
    sleeps = []

    async def fake_sleep(s):
        sleeps.append(s)

    async def fake_create(**kw):
        if not sleeps and len(seen) == 1:
            raise Exception(AZURE)
        return "OK"

    seen = []
    async def counting_create(**kw):
        seen.append(kw)
        if len(seen) == 1:
            raise Exception(AZURE)
        return "OK"

    a = _mk_agent()
    a._create_with_reason_fallback = counting_create
    monkeypatch.setattr(agent.asyncio, "sleep", fake_sleep)
    out = _run_retry_create(a, monkeypatch, model="m",
                            messages=[{"role": "user", "content": "u"}],
                            max_tokens=16384)
    assert out == "OK"
    assert sleeps == []                        # 修复路径不睡退避：不是重试，是修请求
    assert seen[1]["max_tokens"] == 4096


def test_记住之后每轮前置压住(monkeypatch):
    seen = {}

    async def fake_create(**kw):
        seen.update(kw)
        return "OK"

    a = _mk_agent()
    a._output_cap = 8192
    a._create_with_reason_fallback = fake_create
    _run_retry_create(a, monkeypatch, model="m",
                      messages=[{"role": "user", "content": "u"}],
                      max_tokens=16384)
    assert seen["max_tokens"] == 8192          # 不必先失败一次再修


def test_无关错误照旧快速失败(monkeypatch):
    calls = []

    async def fake_create(**kw):
        calls.append(kw)
        raise Exception(BALANCE_403)

    a = _mk_agent()
    a._create_with_reason_fallback = fake_create
    with pytest.raises(Exception, match="Deposit required"):
        _run_retry_create(a, monkeypatch, model="m",
                          messages=[{"role": "user", "content": "u"}],
                          max_tokens=16384)
    assert len(calls) == 1                     # 不重试、不改参数
    assert a._output_cap == 0


def test_每次调用最多修一次不死循环(monkeypatch):
    calls = []

    async def fake_create(**kw):
        calls.append(kw)
        raise Exception(DASHSCOPE)             # 模拟「渠道还有更低隐藏上限」

    a = _mk_agent()
    a._create_with_reason_fallback = fake_create
    with pytest.raises(Exception, match="Range of max_tokens"):
        _run_retry_create(a, monkeypatch, model="m",
                          messages=[{"role": "user", "content": "u"}],
                          max_tokens=16384)
    assert len(calls) == 2                     # 修一次就够，第二次直接抛
    assert calls[1]["max_tokens"] == 8192


def test_输入超长不被改参数(monkeypatch):
    calls = []

    async def fake_create(**kw):
        calls.append(kw)
        raise Exception(CTX_OVERFLOW)

    a = _mk_agent()
    a._create_with_reason_fallback = fake_create
    with pytest.raises(Exception, match="maximum context length"):
        _run_retry_create(a, monkeypatch, model="m",
                          messages=[{"role": "user", "content": "u"}],
                          max_tokens=16384)
    assert len(calls) == 1
    assert a._output_cap == 0                  # 上下文超长不该被记成输出上限


def test_抠不出数字的措辞只修本次不记(monkeypatch):
    calls = []

    async def fake_create(**kw):
        calls.append(kw)
        if len(calls) == 1:
            raise Exception("max_tokens exceeds the configured output limit for this model")
        return "OK"

    a = _mk_agent()
    a._create_with_reason_fallback = fake_create
    out = _run_retry_create(a, monkeypatch, model="m",
                            messages=[{"role": "user", "content": "u"}],
                            max_tokens=16384)
    assert out == "OK"
    assert calls[1]["max_tokens"] == 8192      # 减半探路
    assert a._output_cap == 0                  # 但没明确数字就不记，避免污染后续轮
