#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""「连续单发只读」检测的契约：跨轮串行浪费必须可见、可反馈、且不误伤。

背景（2026-09-14）：eff_mergeable 只在**同一轮** pending 里统计同名工具出现 >=2 次，
但模型每 LLM 往返只发 1.36 个工具、pending 通常长度 1 —— 真正烧时间的「跨轮各发一个
只读调用」在旧口径下完全不可见。而并行度两天纹丝不动（每批 1.08 个工具），说明光靠
规则区的「并行优先」提示无效，需要基于刚发生的行为给事实反馈。

契约（六条）：
  1. 只读判定以 harness.tool_sched.READONLY_TOOLS 为唯一权威；未知/写工具一律 False
  2. 只有「本轮恰好 1 个工具且它只读」才累加；多工具、写工具、空轮一律清零
  3. 反馈每满 SINGLE_RO_STREAK_N 轮给一次（不是每轮）——覆盖长串行又不刷屏
  4. 反馈只加给 LLM 那份结果，消费一次即解除（不能跨轮残留）
  5. 名字列表有上限，长串行不会让提示无限膨胀
  6. 调度器不可用时判 False（宁可漏报，也不能在写工具上暗示「可以并行」）
"""
import sys
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

import agent  # noqa: E402


def _mk_agent(streak=0, names=None, armed=False):
    """绕过 __init__ 造实例：这里只测状态机，不碰网络/记忆库/技能。"""
    ag = agent.AIAgent.__new__(agent.AIAgent)
    ag._single_ro_streak = streak
    ag._single_ro_names = list(names or [])
    ag._single_ro_armed = armed
    return ag


# ---------- 契约 1：只读判定 ----------

def test_readonly_uses_scheduler_whitelist():
    assert agent._is_readonly_tool("code_read") is True
    assert agent._is_readonly_tool("code_search") is True
    assert agent._is_readonly_tool("read_lines") is True


def test_writer_and_unknown_are_not_readonly():
    assert agent._is_readonly_tool("shell_run") is False
    assert agent._is_readonly_tool("code_edit") is False
    assert agent._is_readonly_tool("根本没有这个工具") is False


def test_readonly_degrades_to_false_without_scheduler(monkeypatch):
    """契约 6：harness 不可用时判 False，绝不误报可并行。"""
    import builtins
    real_import = builtins.__import__

    def _boom(name, *a, **kw):
        if name == "harness.tool_sched":
            raise ImportError("模拟调度器不可用")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", _boom)
    assert agent._is_readonly_tool("code_read") is False


# ---------- 契约 2：累加与清零 ----------

def test_streak_accumulates_on_single_readonly():
    streak, names, hint = agent._single_ro_note(0, [], ["code_read"])
    assert (streak, names, hint) == (1, ["code_read"], False)
    streak, names, hint = agent._single_ro_note(streak, names, ["code_search"])
    assert (streak, names, hint) == (2, ["code_read", "code_search"], False)


def test_multi_tool_round_resets_streak():
    """一轮发了 3 个工具就是正确行为，不该被计数，更不该给提示。"""
    streak, names, hint = agent._single_ro_note(2, ["code_read"], ["code_read", "code_search"])
    assert (streak, names, hint) == (0, [], False)


def test_single_writer_resets_streak():
    streak, names, hint = agent._single_ro_note(2, ["code_read"], ["code_edit"])
    assert (streak, names, hint) == (0, [], False)


def test_empty_round_resets_streak():
    """纯文本轮（模型收尾）不是串行浪费。"""
    assert agent._single_ro_note(2, ["code_read"], []) == (0, [], False)


# ---------- 契约 3：每满 N 轮反馈一次 ----------

def test_hint_fires_exactly_on_multiples_of_n():
    streak, names, armed = 0, [], False
    fired = []
    for i in range(1, 8):
        streak, names, armed = agent._single_ro_note(streak, names, ["code_read"])
        if armed:
            fired.append(i)
    assert fired == [agent.SINGLE_RO_STREAK_N, agent.SINGLE_RO_STREAK_N * 2]


def test_first_two_rounds_stay_silent():
    """阈值必须真的起作用——第 1、2 轮就给提示等于噪声。"""
    streak, names, armed = agent._single_ro_note(0, [], ["code_read"])
    assert armed is False
    streak, names, armed = agent._single_ro_note(streak, names, ["code_read"])
    assert armed is False


# ---------- 契约 4/5：提示文本与消费 ----------

def test_hint_requires_armed_state():
    assert _mk_agent(armed=False)._single_ro_hint() == ""


def test_hint_names_the_tools_and_is_consumed_once():
    ag = _mk_agent(streak=3, names=["code_read", "code_search", "read_lines"], armed=True)
    hint = ag._single_ro_hint()
    assert "code_read" in hint and "read_lines" in hint
    assert "3" in hint
    assert ag._single_ro_armed is False
    assert ag._single_ro_hint() == ""   # 契约 4：消费一次即解除，不会跨轮残留


def test_names_list_is_bounded():
    """契约 5：连发 20 轮也不能让提示文本无限膨胀。"""
    streak, names, _ = 0, [], False
    for _ in range(20):
        streak, names, _ = agent._single_ro_note(streak, names, ["code_read"])
    assert len(names) <= agent.SINGLE_RO_NAMES_MAX


def test_hint_never_leaks_into_agent_state_after_use():
    """提示是给 LLM 的一次性附加文本，不该反过来改计数（否则会自我强化）。"""
    ag = _mk_agent(streak=6, names=["code_read"], armed=True)
    before = (ag._single_ro_streak, list(ag._single_ro_names))
    ag._single_ro_hint()
    assert (ag._single_ro_streak, ag._single_ro_names) == before


# ---------- 回放工具的只读 shell 判据 ----------
# 踩过的坑：初版把 `2>/dev/null` 当成写重定向，导致 357 次单发 shell_run 里
# 一个「纯读连续段」都找不到（0 段）。摘掉丢弃式重定向后才发现真实值是 1 段。
# 判据必须保守但不能假阴——假阴会让人得出「这里没有浪费」的错误结论。

@pytest.mark.parametrize("cmd,expected", [
    ("ls -la data/ 2>/dev/null", True),
    ("cat a.py", True),
    ("grep -rn x . 2>/dev/null | head", True),
    ("rm -rf /tmp/x", False),
    ("cat a > b", False),
    ("find . -delete", False),
    ("pytest -q", False),
    ("venv/bin/python tools/x.py", False),
    ("echo hi > f", False),
])
def test_read_cmd_judge(cmd, expected):
    from tools.single_ro_replay import is_read_cmd
    assert is_read_cmd(cmd) is expected


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
