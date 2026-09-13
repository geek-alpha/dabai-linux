#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""经验注入的截断口径：保住「规则句」，不是保住「背景句」。

背景（2026-09-14）：教训的写法是「背景 → 实测 → 规则」，而 _clip 取前 120 字符必然
取到背景，规则永远进不来。实测真库 77 条里 47 条（61%）的指令词只出现在第 120 字符
之后——注入的是一条读得懂、但没法据以行动的故事。契约：

  1. 短文本原样返回，不做任何加工
  2. 规则句在 120 字符之后时，结果必须含规则句（本次改动的唯一目的）
  3. 结果长度有界，不能因为「多取一句」把注入预算撑爆
  4. 无规则句 / 单句超长 → 退回 _clip，不炸、不返回空
  5. 真库回归：丢规则的条数必须显著低于旧口径（跑真实文件，不是夹具）
"""
import ast
import json
import re
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parents[1]
WANTED = {"_CLIP_PUNC", "_LESSON_RULE", "_SENT_SPLIT", "_clip", "_clip_lesson"}


def _load():
    """AST 抽出 helper 单独 exec：不 import agent，避免拉起整个进程。"""
    src = (BASE / "agent.py").read_text(encoding="utf-8", errors="replace")
    ns = {"re": re}
    for node in ast.parse(src).body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", "") in WANTED for t in node.targets):
            exec(compile(ast.Module(body=[node], type_ignores=[]), "<agent>", "exec"), ns)
        elif isinstance(node, ast.FunctionDef) and node.name in WANTED:
            exec(compile(ast.Module(body=[node], type_ignores=[]), "<agent>", "exec"), ns)
    missing = WANTED - set(ns)
    assert not missing, f"agent.py 里找不到 {sorted(missing)}"
    return ns


def test_short_text_untouched():
    ns = _load()
    t = "别拿时间戳取模做轮换。"
    assert ns["_clip_lesson"](t) == t


def test_rule_after_120_is_kept():
    """本次改动的唯一目的：第 120 字符之后的规则句必须进结果。"""
    ns = _load()
    t = "背景" * 50 + "。实测数据见下。" + "规则：必须用每轮递增序号取模。"
    out = ns["_clip_lesson"](t)
    assert "规则：必须用每轮递增序号取模。" in out


def test_length_bounded():
    ns = _load()
    t = "背景" * 50 + "。实测见下。" + "规则：" + "细节" * 100 + "。"
    out = ns["_clip_lesson"](t)
    assert len(out) <= 150 * 1.5, f"注入长度失控：{len(out)}"


def test_no_rule_falls_back():
    ns = _load()
    t = "背景" * 100
    assert ns["_clip_lesson"](t) == ns["_clip"](t, 120)


def test_single_long_sentence_survives():
    ns = _load()
    out = ns["_clip_lesson"]("啊" * 400)
    assert out and len(out) <= 400


def test_real_library_regression():
    """跑真实经验库：新口径丢规则的条数必须显著低于旧口径。"""
    ns = _load()
    f = BASE / "harness_task_memory.json"
    if not f.exists():
        pytest.skip("经验库不存在")
    ls = [str(x) for x in (json.loads(f.read_text(encoding="utf-8")).get("lessons") or [])]
    rule = ns["_LESSON_RULE"]
    long_enough = [x for x in ls if len(x) > 120 and rule.search(x[120:])]
    if len(long_enough) < 20:
        pytest.skip(f"真库样本不足（{len(long_enough)} 条），这条回归失去意义")
    lost = [x for x in long_enough if not rule.search(ns["_clip_lesson"](x))]
    assert len(lost) <= len(long_enough) * 0.4, (
        f"新口径丢规则 {len(lost)}/{len(long_enough)} 条，改善不足"
    )
