#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""reasoning_content 回传缺失（thinking 渠道 400）的自愈契约。

背景（2026-09-13）：真实对话偶发整轮挂掉，报错原文
  Error code: 400 - {'error': {'message': 'The `reasoning_content` in the thinking
  mode must be passed back to the API.', 'type': 'invalid_request_error', ...}}

取证过程（tools/reasoning_echo_400_probe.py，三次迭代）：
  1. 手写 assistant(tool_calls) 删 reasoning            → 200
  2. 真实工具轮 assistant 删 reasoning（流式/非流式）    → 200
  3. 同一请求里有的 assistant 带、有的不带（混合）        → 200
即**当前上游是宽松的，单点复现不出来**；而报错来自同一渠道的严格上游
（该渠道 /models 只暴露 deepseek-flash 与 deepseek-v4-pro，非官方模型名，
属中转；此前同渠道还回过 403「Deposit required to unlock premium models」，
说明上游会随余额/路由切换）。结论：不能靠复现，得让客户端在结构上永远不缺字段。

agent.py 的结构性缺口：只有 4785/4811 两处在 round_reasoning 非空时挂这个字段，
而历史注入(3837/3843)、孤立 tool 修复(825 合成 assistant)、断点续跑/记忆恢复
产出的 assistant 天生没有它 —— 混进请求就整轮 400。

修法（两段式，正常路径零改动）：
  · 踩到时自愈：补空串重试 → 仍被拒则去掉 thinking 参数重试 → 记住；
  · 记住之后每轮在唯一出口（_retry_create）前置补齐，不再多付一次重试。
"""
import asyncio
import sys
import types
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

import agent  # noqa: E402

# 真实报错原文（data/hist_view_state.json 里那条 assistant 消息）
REAL_400 = (
    "Error code: 400 - {'error': {'message': 'The `reasoning_content` in the "
    "thinking mode must be passed back to the API.', 'type': "
    "'invalid_request_error', 'param': None, 'code': 'invalid_request_error'}}"
)
BALANCE_403 = ("Error code: 403 - {'error': {'code': 'access_denied', 'message': "
               "'Access restricted. Deposit required to unlock premium models.'}}")
TOOL_ID_400 = ("Error code: 400 - {'error': {'message': \"missing field tool_call_id\"}}")
CTX_400 = ("Error code: 400 - {'error': {'message': 'This model\\'s maximum context "
           "length is 65536 tokens. However, you requested 70000 tokens.'}}")


class _FakeCompletions:
    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    async def create(self, **kw):
        self.calls.append(kw)
        r = self.results.pop(0) if self.results else "OK"
        if isinstance(r, Exception):
            raise r
        return r


class _FakeRuntime:
    async def supervise_llm(self, kind, fn):
        return await fn()


def _mk_agent(results=()):
    a = agent.AIAgent.__new__(agent.AIAgent)   # 绕过 __init__：单测只要这几个字段
    a._client = types.SimpleNamespace(chat=types.SimpleNamespace(
        completions=_FakeCompletions(results)))
    a._reasoning_echo_required = False
    return a


# ---------------- _ensure_reasoning_echo ----------------

def test_patch_fills_missing_only():
    msgs = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "a"},                              # 缺 → 补空串
        {"role": "assistant", "content": "b", "reasoning_content": "真思考"},  # 有 → 原样
        {"role": "tool", "tool_call_id": "t", "content": "r"},              # 非 assistant
        {"role": "system", "content": "s"},
    ]
    out = agent._ensure_reasoning_echo(msgs)
    assert out[1]["reasoning_content"] == ""
    assert out[2]["reasoning_content"] == "真思考"      # 真实思考绝不被空串覆盖
    assert "reasoning_content" not in out[3]
    assert "reasoning_content" not in out[4]
    assert "reasoning_content" not in msgs[1]           # 不改原对象


def test_patch_is_idempotent_and_keeps_empty_list():
    once = agent._ensure_reasoning_echo([{"role": "assistant", "content": "a"}])
    twice = agent._ensure_reasoning_echo(once)
    assert twice == once
    assert agent._ensure_reasoning_echo([]) == []


# ---------------- _is_reasoning_echo_error ----------------

def test_detects_real_error():
    assert agent._is_reasoning_echo_error(Exception(REAL_400)) is True


@pytest.mark.parametrize("text,label", [
    (BALANCE_403, "余额不足 403"),
    (TOOL_ID_400, "tool_call_id 缺失"),
    (CTX_400, "上下文超长"),
    ("Connection error.", "网络错误"),
])
def test_does_not_swallow_other_errors(text, label):
    assert agent._is_reasoning_echo_error(Exception(text)) is False, label


# ---------------- 自愈路径 ----------------

def test_selfheal_patches_then_remembers():
    err = Exception(REAL_400)
    a = _mk_agent([err, "OK"])
    out = asyncio.run(a._create_with_reason_fallback(
        model="m", messages=[{"role": "assistant", "content": "x"}]))
    assert out == "OK"
    assert a._reasoning_echo_required is True            # 记住，后续前置补齐
    assert a._client.chat.completions.calls[1]["messages"][0]["reasoning_content"] == ""


def test_selfheal_drops_thinking_when_patch_rejected():
    a = _mk_agent([Exception(REAL_400), Exception(REAL_400), "OK"])
    out = asyncio.run(a._create_with_reason_fallback(
        model="m", messages=[{"role": "assistant", "content": "x"}],
        extra_body={"reasoning_effort": "high", "thinking_budget": 2000}))
    assert out == "OK"
    third = a._client.chat.completions.calls[2]
    assert "reasoning_effort" not in third["extra_body"]   # 退出 thinking 模式
    assert "thinking_budget" not in third["extra_body"]


def test_selfheal_reraises_unrelated_error():
    a = _mk_agent([Exception(BALANCE_403)])
    with pytest.raises(Exception, match="Deposit required"):
        asyncio.run(a._create_with_reason_fallback(
            model="m", messages=[{"role": "assistant", "content": "x"}]))
    assert a._reasoning_echo_required is False
    assert len(a._client.chat.completions.calls) == 1      # 不重试无关错误


# ---------------- 唯一出口的前置补齐 ----------------

def test_retry_create_prepatches_after_remembered(monkeypatch):
    a = _mk_agent()
    a._reasoning_echo_required = True
    seen = {}

    async def fake_create(**kw):
        seen.update(kw)
        return "OK"

    a._create_with_reason_fallback = fake_create
    monkeypatch.setattr(agent, "_get_runtime", lambda: _FakeRuntime())
    out = asyncio.run(a._retry_create(
        messages=[{"role": "user", "content": "u"},
                  {"role": "assistant", "content": "a"},
                  {"role": "assistant", "content": "b", "reasoning_content": "r"}]))
    assert out == "OK"
    msgs = seen["messages"]
    assert msgs[1]["reasoning_content"] == ""
    assert msgs[2]["reasoning_content"] == "r"


def test_retry_create_untouched_before_first_hit(monkeypatch):
    a = _mk_agent()
    seen = {}

    async def fake_create(**kw):
        seen.update(kw)
        return "OK"

    a._create_with_reason_fallback = fake_create
    monkeypatch.setattr(agent, "_get_runtime", lambda: _FakeRuntime())
    asyncio.run(a._retry_create(messages=[{"role": "assistant", "content": "a"}]))
    # 没踩过坑就不动请求体：零侵入，避免给不需要该字段的渠道添未知字段
    assert "reasoning_content" not in seen["messages"][0]
