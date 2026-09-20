#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""max_tokens 截断续补判定的契约测试。

背景（2026-09-20）：对照 Anthropic SDK 的 stop_reason 状态机时发现，大白全库
0 处消费 finish_reason——长回复/长代码写到一半被 max_tokens 掐断（finish_reason
=length）时被当成正常结束交付，用户收到半截回复。修法：对齐 Anthropic 的
「max_tokens → resume」语义，截断时自动续补完整回复。

契约：
  1. 只有 finish_reason=length 才续补（stop / tool_calls / 缺失 一律不续）
  2. 有工具调用的轮不续补：参数截断走 parse_partial_json 的 partial 通道兜底
  3. 续补次数达到上限后放弃（交付半截并明示），防止 provider 永远返回 length
     时死循环烧钱
"""
import sys
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

import agent  # noqa: E402


@pytest.fixture(autouse=True)
def _fixed_limit(monkeypatch):
    """把配置钉死在默认值（limit=3），测试不依赖真实 settings.json。

    只 patch load_config 不 patch _resume_tail_limit 本身——后者有专门的
    解析测试，patch 掉就永远测不到真实逻辑。
    """
    monkeypatch.setattr(agent, "load_config",
                        lambda: {"agent": {"resume_tail_limit": 3}})


# ---------- 契约 1：只有 length 才续补 ----------

def test_length_纯文本_未超限_续补():
    assert agent._should_resume_tail("length", False, None, 0) is True


@pytest.mark.parametrize("fr", ["stop", "tool_calls", "content_filter", "", None])
def test_非length_不续补(fr):
    assert agent._should_resume_tail(fr, False, None, 0) is False


# ---------- 契约 2：有工具调用的轮不续补 ----------

def test_length_原生工具轮_不续补():
    assert agent._should_resume_tail("length", True, None, 0) is False


def test_length_文本工具轮_不续补():
    assert agent._should_resume_tail("length", False, {"name": "x"}, 0) is False


# ---------- 契约 3：续补上限 ----------

@pytest.mark.parametrize("count", [3, 5, 100])
def test_length_超上限_不续补(count):
    assert agent._should_resume_tail("length", False, None, count) is False


def test_length_恰好临界_续补():
    # resume_count < limit（3）：count=2 还能续，count=3 停
    assert agent._should_resume_tail("length", False, None, 2) is True
    assert agent._should_resume_tail("length", False, None, 3) is False


# ---------- 上限解析 ----------

def test_上限_默认3():
    assert agent.MAX_RESUME_TAILS == 3


def test_上限_读取配置(monkeypatch):
    monkeypatch.setattr(agent, "load_config",
                        lambda: {"agent": {"resume_tail_limit": 7}})
    assert agent._resume_tail_limit() == 7


def test_上限_配置0_落到下限1(monkeypatch):
    # 0 会让续补永远不触发（count < 0 恒假），下限 1 保证机制可用
    monkeypatch.setattr(agent, "load_config",
                        lambda: {"agent": {"resume_tail_limit": 0}})
    assert agent._resume_tail_limit() == 1


def test_上限_配置异常_回落默认(monkeypatch):
    monkeypatch.setattr(agent, "load_config", lambda: (_ for _ in ()).throw(RuntimeError))
    assert agent._resume_tail_limit() == 3


# ---------- 契约 4：渠道大写 finish_reason 归一后才能续补 ----------
# 部分 OpenAI 兼容网关转的是 Gemini 原生大写原因（STOP / MAX_TOKENS）。
# 不归一的话 _should_resume_tail 判 "length" 永远为假，截断续补静默失效——
# 半截回复被当正常结束交付，既不报错也不重试，是最难发现的一类回归。

@pytest.mark.parametrize("raw,expect", [
    ("MAX_TOKENS", "length"),        # Gemini 原生大写
    ("max_tokens", "length"),        # Anthropic 风格
    ("Length", "length"),            # 大小写混合
    ("STOP", "stop"),
    ("end", "stop"),
    ("FUNCTION_CALL", "tool_calls"),
    ("stop", "stop"),                # 契约值字节不变
    ("length", "length"),
    ("tool_calls", "tool_calls"),
    ("content_filter", "content_filter"),   # 未知值原样，不硬塞
    ("", ""),                        # 空值原样（调用方保留自己的 or "stop" 默认）
    (None, None),
    (2, 2),                          # 非字符串原样（有网关回 int 原因）
])
def test_finish_reason_归一(raw, expect):
    assert agent._normalize_finish_reason(raw) == expect


def test_大写MAX_TOKENS经归一后触发续补():
    assert agent._should_resume_tail(
        agent._normalize_finish_reason("MAX_TOKENS"), False, None, 0) is True


def test_大写STOP经归一后不续补():
    assert agent._should_resume_tail(
        agent._normalize_finish_reason("STOP"), False, None, 0) is False


def test_不归一就会漏掉截断():
    """变异检验：不归一的大写原因让续补判据失效（回归的根因）。"""
    assert agent._should_resume_tail("MAX_TOKENS", False, None, 0) is False
