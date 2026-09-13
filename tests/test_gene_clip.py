#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""注入段截断（agent.py:_clip）的边界测试。

为什么不测注入段整体：_harness_lessons_block 的结果是「经验库里恰好写了什么」的函数，
断言会随库内容漂移。_clip 是纯函数，句子边界才是要锁的东西。
"""
import importlib.util
import sys
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def clip():
    spec = importlib.util.spec_from_file_location("agent_clip_under_test", BASE / "agent.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["agent_clip_under_test"] = mod
    spec.loader.exec_module(mod)
    return mod._clip


def test_short_text_untouched(clip):
    assert clip("短句。", 120) == "短句。"


def test_exact_length_untouched(clip):
    s = "。" * 120
    assert clip(s, 120) == s


def test_cuts_at_punctuation_inside_window(clip):
    s = "a" * 100 + "。" + "b" * 50
    r = clip(s, 120)
    assert r.endswith("。")
    assert len(r) <= 120


def test_looks_ahead_when_punctuation_too_early(clip):
    """n 内最后标点早于 tol → 宁可超长也要句子完整（这是相对硬切的核心差别）。"""
    s = "短。" + "填" * 118 + "。" + "尾"
    r = clip(s, 120)
    assert r.endswith("。")
    assert 120 < len(r) <= 180


def test_no_punctuation_falls_back_to_ellipsis(clip):
    r = clip("x" * 400, 120)
    assert r.endswith("…")
    assert len(r) == 121


def test_result_never_exceeds_1_5n(clip):
    for s in ("字" * 500, "。" + "字" * 500, "字" * 119 + "。" + "字" * 500):
        assert len(clip(s, 120)) <= 180


def test_empty_and_none_safe(clip):
    assert clip("", 120) == ""
    assert clip(None, 120) == "None"
