#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""「交付文本」埋点的契约（说重点 / 克制自我纠正 / 授权与自主）。

背景（2026-09-24）：这三条规则讲的是同一份材料——我交付出去的那段文字，而它
此前不进任何日志，审计只能标 none（三条合计 713 字符、占规则区 15.6%）。现在轮末
按黑名单计数落盘：只落计数与命中的词名，不落原文。

契约（六条）：
  1. 空文本不记流水——否则「没说话」会被算成一轮干净交付，把分母稀释成假的
  2. 套话含「不是 X 而是 Y」对比框架：规则点名的修辞，说 A 就直接说 A
  3. 道歉按出现次数累加（一轮说两次就是两次），分子按轮数算
  4. 结尾征询只看尾巴 80 字符：正文中部的问句是在讲清事实，不算把决定权推回用户
  5. 钩子必须挂在 _record_turn_metrics 最前面（tool_round 早退之前）——纯文本轮的
     回复最可能出套话，漏掉它样本就偏（源码级断言）
  6. 审计侧三条规则必须接上新 metric（回退成 none 立刻红）
"""
import importlib.util
import json
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))


def _load_agent():
    spec = importlib.util.spec_from_file_location("_agent_reply", BASE / "agent.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_audit():
    spec = importlib.util.spec_from_file_location(
        "_pra_reply", BASE / "tools" / "prompt_rules_audit.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _agent_lines():
    return (BASE / "agent.py").read_text(encoding="utf-8").splitlines()


# ---------- 契约 1：空文本不记流水 ----------

def test_empty_text_is_not_a_clean_turn():
    mod = _load_agent()
    st = mod._reply_style_stats("   \n  ")
    assert st == {"chars": 0, "boilerplate": 0, "boilerplate_words": [],
                  "apology": 0, "tail_ask": 0}
    assert mod._reply_style_flush("") == 0


# ---------- 契约 2：套话与对比框架 ----------

def test_boilerplate_counts_words_and_contrast_frame():
    mod = _load_agent()
    st = mod._reply_style_stats("亲爱的，重点结论：深入探讨一下这个方案。")
    assert st["boilerplate"] == 2
    assert "重点结论" in st["boilerplate_words"]
    assert mod._reply_style_stats("不是 A，而是 B。")["boilerplate"] == 1


def test_clean_reply_has_zero_boilerplate():
    mod = _load_agent()
    st = mod._reply_style_stats("pytest 1542 passed，无数据源 9 条降到 6 条。")
    assert st["boilerplate"] == 0 and st["apology"] == 0


# ---------- 契约 3：道歉按次数累加 ----------

def test_apology_counts_every_occurrence():
    mod = _load_agent()
    assert mod._reply_style_stats("抱歉，我错了。")["apology"] == 2
    assert mod._reply_style_stats("抱歉，抱歉，是我搞错了。")["apology"] == 3


# ---------- 契约 4：结尾征询只看尾巴 ----------

def test_tail_ask_only_looks_at_the_tail():
    mod = _load_agent()
    # 尾巴里出现征询 → 1
    assert mod._reply_style_stats("证据在下面。\n\n要我接着跑吗")["tail_ask"] == 1
    # 以问号收尾 → 1（把决定权推回用户的典型形状）
    assert mod._reply_style_stats("这个要保留吗？")["tail_ask"] == 1
    # 正文中部问了句、结尾是结论 → 0：那是讲清事实，不是请决策
    mid = "要我确认的话，读数在这里。" + "结论已落盘。" * 12
    assert mod._reply_style_stats(mid)["tail_ask"] == 0


# ---------- 契约 5：只落计数、钩子在早退之前 ----------

def test_flush_writes_counts_only(tmp_path, monkeypatch):
    mod = _load_agent()
    monkeypatch.setattr(mod, "_harness_base", lambda: tmp_path)
    secret = "亲爱的，重点结论在这里，抱歉。"
    assert mod._reply_style_flush(secret, tool_rounds=3) == 1
    lines = (tmp_path / "data" / "reply_style.jsonl").read_text(
        encoding="utf-8").splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert set(rec) == {"ts", "tool_rounds", "chars", "boilerplate",
                        "boilerplate_words", "apology", "tail_ask"}
    assert rec["tool_rounds"] == 3 and rec["boilerplate"] == 1
    # 原文不进盘：判断有没有套话不需要把回复抄进磁盘
    assert secret not in lines[0]


def test_hook_sits_before_the_tool_round_early_exit():
    lines = _agent_lines()
    hook = [i for i, l in enumerate(lines) if "_reply_style_flush(full_text" in l]
    assert len(hook) == 1, f"钩子应恰好一处，实际 {len(hook)}"
    early = [i for i, l in enumerate(lines) if "if tool_round <= 0:" in l]
    assert early, "找不到 tool_round 早退点——判据本身失效了"
    assert hook[0] < early[0], (
        "钩子排在了 tool_round 早退之后：纯文本轮（tool_round=0）的回复最可能出套话，"
        "漏掉它分母就偏了")


# ---------- 契约 6：审计侧接上新 metric ----------

def test_rule_map_points_three_rules_at_reply_metrics():
    mod = _load_audit()
    m = {r[0]: (r[1], r[2]) for r in mod.RULE_MAP}
    # 「说重点」2026-09-25 改指 mid_speech：套话率测不出「全程沉默」，而那是实测的失败模式
    assert m["说重点"] == ("metric", "mid_speech")
    assert m["克制自我纠正"] == ("metric", "reply_apology")
    assert m["授权与自主"] == ("metric", "reply_tail_ask")


def test_reply_stats_counts_rounds_not_hits():
    mod = _load_audit()
    rows = [
        {"chars": 100, "boilerplate": 3, "boilerplate_words": ["善用"],
         "apology": 0, "tail_ask": 1},
        {"chars": 50, "boilerplate": 0, "boilerplate_words": [],
         "apology": 2, "tail_ask": 0},
    ]
    st = mod.reply_stats(rows)
    assert st["reply_turns"] == 2
    assert st["reply_boilerplate_rounds"] == 1      # 轮数，不是 3 次命中
    assert st["reply_boilerplate_hits"] == 3
    assert st["reply_apology_rounds"] == 1
    assert st["reply_tail_ask_rounds"] == 1
    assert "善用×1" in st["reply_words"]


def test_judge_reply_branches():
    mod = _load_audit()
    few = {"reply_turns": 3, "reply_boilerplate_rounds": 0}
    assert mod.judge("metric", "reply_boilerplate", few, [], "说重点")[0] == "INSUFFICIENT"
    ok = {"reply_turns": 40, "reply_boilerplate_rounds": 0, "reply_boilerplate_hits": 0}
    assert mod.judge("metric", "reply_boilerplate", ok, [], "说重点")[0] == "MEASURED_OK"
    gap = {"reply_turns": 40, "reply_boilerplate_rounds": 8,
           "reply_boilerplate_hits": 9, "reply_words": "善用×3"}
    tag, obs = mod.judge("metric", "reply_boilerplate", gap, [], "说重点")
    assert tag == "MEASURED_GAP" and "善用×3" in obs
    assert mod.judge("metric", "reply_apology",
                     {"reply_turns": 40, "reply_apology_rounds": 20},
                     [], "克制自我纠正")[0] == "MEASURED_GAP"
    assert mod.judge("metric", "reply_tail_ask",
                     {"reply_turns": 40, "reply_tail_ask_rounds": 2},
                     [], "授权与自主")[0] == "MEASURED_OK"
