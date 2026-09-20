#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""最新一轮放宽上限的回归用例：_pack_history_records 的 *_newest 参数契约。

背景（2026-09-20）：memory.py 的文档写着「最新一轮整体保留，不受单轮字符上限截断」，
但 _pack_one_round 里的 _limit_round_tools 对所有轮一视同仁 —— 实测（tools/
compaction_recall.py，24 会话）每轮工具交互中位 17 次、87% 的轮超过 3 次，最新一轮
只剩 10.0% 的 token、11.1% 的事实实体，下一轮看不见自己刚读过什么，只能重读。

契约（改这里等于改契约）：
  1. 最新一轮走 *_newest 参数，历史轮走原参数；
  2. *_newest = 0 表示继承原参数（向后兼容，老调用方行为不变）；
  3. 丢弃工具交互必须成对，不留悬空 tool_calls（提供方会 400）；
  4. 放宽上限不等于无上限：token 预算仍然管住最新一轮。
"""
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

import memory as M  # noqa: E402


def _tool_round(n_tools, text_len=20):
    """造一轮：user + n 次 (assistant tool_calls + tool 结果)。"""
    rnd = [{"role": "user", "content": "帮我查一下"}]
    for i in range(n_tools):
        rnd.append({"role": "assistant", "content": "",
                    "tool_calls": [{"id": "c%d" % i, "type": "function",
                                    "function": {"name": "read", "arguments": "{}"}}]})
        rnd.append({"role": "tool", "tool_call_id": "c%d" % i, "content": "x" * text_len})
    rnd.append({"role": "assistant", "content": "查完了"})
    return rnd


def _old_round(n_tools=1):
    return [{"role": "user", "content": "上一轮"}] + _tool_round(n_tools)[1:]


def _count_tools(msgs):
    return sum(1 for m in msgs if m.get("role") == "tool")


def _dangling(msgs):
    """返回悬空引用数：有 tool_calls 但没有配对 tool 响应的 assistant 消息。"""
    ids = {m.get("tool_call_id") for m in msgs if m.get("role") == "tool"}
    bad = 0
    for m in msgs:
        for tc in (m.get("tool_calls") or []):
            if tc.get("id") not in ids:
                bad += 1
    return bad


def test_newest_round_keeps_more_tools_than_old_round():
    """最新一轮按 *_newest 放宽，历史轮仍按原上限 —— 这是本次改动的全部意义。"""
    recs = _old_round(2) + _tool_round(12)
    packed = M._pack_history_records(
        recs, 8000, 1200, min_rounds=0,
        max_chars_per_tool=500, keep_last_tools=3,
        keep_last_tools_newest=10, max_chars_per_tool_newest=500)
    tools = _count_tools(packed)
    # 历史轮 2 条全留（未超 3），最新轮留 10 条（12 → 10）
    assert tools == 12, "历史 2 + 最新 10，实际 %d" % tools
    assert _dangling(packed) == 0, "丢弃工具交互时必须成对，不能留悬空 tool_calls"


def test_newest_zero_inherits_old_limit():
    """*_newest = 0 = 继承原参数：老调用方（不传新参数）行为必须一字不变。"""
    recs = _old_round(2) + _tool_round(12)
    a = M._pack_history_records(
        recs, 8000, 1200, min_rounds=0,
        max_chars_per_tool=500, keep_last_tools=3)
    b = M._pack_history_records(
        recs, 8000, 1200, min_rounds=0,
        max_chars_per_tool=500, keep_last_tools=3,
        keep_last_tools_newest=0, max_chars_per_tool_newest=0)
    assert _count_tools(a) == 5, "默认：历史 2 + 最新 3"
    assert [m.get("content") for m in a] == [m.get("content") for m in b]


def test_newest_per_tool_zero_inherits_old_per_tool():
    """max_chars_per_tool_newest = 0 时单条工具结果仍按历史轮上限截断（不放宽）。"""
    recs = [{"role": "user", "content": "查"}]
    recs += [{"role": "assistant", "content": "", "tool_calls": [{"id": "c0"}]},
             {"role": "tool", "tool_call_id": "c0", "content": "y" * 3000}]
    packed = M._pack_history_records(
        recs, 8000, 1200, min_rounds=0,
        max_chars_per_tool=500, keep_last_tools=3,
        max_chars_per_tool_newest=0, keep_last_tools_newest=10)
    tool_msg = [m for m in packed if m.get("role") == "tool"][0]
    assert len(tool_msg["content"]) <= 520, "未放宽时不该超过 500 字（含省略号）"


def test_newest_per_tool_widening_works():
    """显式放宽单条上限时生效 —— 参数不是死代码。"""
    recs = [{"role": "user", "content": "查"}]
    recs += [{"role": "assistant", "content": "", "tool_calls": [{"id": "c0"}]},
             {"role": "tool", "tool_call_id": "c0", "content": "y" * 3000}]
    packed = M._pack_history_records(
        recs, 8000, 1200, min_rounds=0,
        max_chars_per_tool=500, keep_last_tools=3,
        max_chars_per_tool_newest=1500, keep_last_tools_newest=10)
    tool_msg = [m for m in packed if m.get("role") == "tool"][0]
    assert 1500 <= len(tool_msg["content"]) <= 1520


def _tok(msgs):
    return sum(M._estimate_record_tokens(m) for m in msgs)


def test_newest_round_still_bounded_by_budget():
    """放宽条数不等于无上限：预算必须仍然管住最新一轮，否则窗口会被单轮撑爆。"""
    recs = _old_round(1) + _tool_round(60, text_len=2000)
    packed = M._pack_history_records(
        recs, 2000, 1200, min_rounds=0,
        max_chars_per_tool=500, keep_last_tools=3,
        keep_last_tools_newest=60, max_chars_per_tool_newest=1500)
    assert _tok(packed) <= 2000 * 2, "预算 2000，实际 %d" % _tok(packed)
