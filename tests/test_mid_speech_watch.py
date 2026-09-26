#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""「说重点」节奏面（过程话）埋点的契约。

背景（2026-09-25）：这条规则此前只测套话率——「说的是什么」。实测 377 轮里
tool_rounds 中位数 4、最长 115 轮，而工具轮里说没说话没有任何日志：全程沉默
与「一切正常」在数据里长得一样，所以规则里那句「每完成一个阶段……」既没法
判定也没法校准。改法两步：把触发条件换成规则自己写得出的可数事件（连续 3 轮
工具调用没说话），再给 turn_metrics 加 mid_speech_rounds / silent_max 两个字段。

契约（五条）：
  1. 计数块必须在工具分发点之前——放在工具执行之后会被硬停/异常路径跳过，
     沉默最长的那些轮恰好最容易走异常路径
  2. 初始化必须先于计数、且计数行唯一：出现两处 += 说明有一条路径绕过了另一条
  3. 落盘字段与轮内计数同名同源，分母（tool_rounds）就在同一条记录里
  4. 审计聚合不许把老行算进分母：没有字段的行是「没埋点」，不是「没说话」
  5. 规则文本里必须写着那个可数事件，且阈值常量与文案一致（防两处漂移）
"""
import importlib.util
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))


def _load_audit():
    spec = importlib.util.spec_from_file_location(
        "_pra_mid_speech", BASE / "tools" / "prompt_rules_audit.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _agent_lines():
    return (BASE / "agent.py").read_text(encoding="utf-8").splitlines()


# ---------- 契约 1/2/3：agent 侧埋点 ----------

def test_counting_block_sits_before_tool_dispatch():
    lines = _agent_lines()
    init = [i for i, l in enumerate(lines) if l.strip() == "mid_speech_rounds = 0"]
    hook = [i for i, l in enumerate(lines) if l.strip() == "mid_speech_rounds += 1"]
    # 判据必须带缩进：流式解析器里也有一句同名注释（16 空格缩进），
    # 用 strip 后匹配会拿到那个，得出「埋点排在工具分发之后」的假结论。
    dispatch = [i for i, l in enumerate(lines) if l == "            # 处理工具调用"]
    assert len(init) == 1, f"初始化应恰好一处，实际 {len(init)}"
    assert len(hook) == 1, f"计数应恰好一处，实际 {len(hook)}"
    assert dispatch, "找不到工具分发点——判据本身失效了"
    assert init[0] < hook[0] < dispatch[0], (
        "计数块没排在工具分发之前：硬停/异常路径会跳过它，而沉默最长的轮最容易走那些路径")


def test_threshold_constant_is_used_not_just_declared():
    lines = _agent_lines()
    hits = [l for l in lines if "MID_SPEECH_MIN_CHARS" in l]
    assert len(hits) >= 2, f"常量只有定义没有使用（死常量）：{hits}"
    assert any("len(_spoken) >=" in l for l in hits), "判据没按字符数比较——「我来看看」会被算成说了话"


def test_flush_fields_match_the_counters():
    lines = _agent_lines()
    assert any('"mid_speech_rounds": mid_speech_rounds' in l for l in lines), "轮末没落 mid_speech_rounds"
    assert any('"silent_max": silent_max' in l for l in lines), "轮末没落 silent_max"


# ---------- 契约 4：审计聚合 ----------

def test_totals_keeps_old_rows_out_of_the_denominator():
    mod = _load_audit()
    rows = [
        {"ts": 1, "tool_rounds": 10, "tool_calls": 10},          # 老行：埋点之前写的
        {"ts": 2, "tool_rounds": 4, "tool_calls": 4,
         "mid_speech_rounds": 1, "silent_max": 5},
    ]
    a = mod.totals(rows)
    assert a["speech_turns"] == 1
    assert a["speech_tool_rounds"] == 4, "老行的 10 轮被算进了分母：比例会被稀释成假的"
    assert a["mid_speech_rounds"] == 1
    assert a["silent_bad_rounds"] == 1
    assert a["silent_max_worst"] == 5


def test_silent_limit_matches_the_rule_text():
    mod = _load_audit()
    rules, _ = mod.extract_rules()
    text = "".join(rules)
    assert f"连续 {mod.MID_SPEECH_SILENT_LIMIT} 轮" in text, (
        "规则文本里的轮数与判据常量对不上——两处会各自漂移")
    assert "每完成一个阶段" not in text, (
        "不可判定的触发条件回来了：它会被同一句里的「不播报」压过去，中间话又会被省掉")


# ---------- 契约 5：判定三态 ----------

def test_judge_mid_speech_three_states():
    mod = _load_audit()
    few = {"speech_turns": 3, "silent_bad_rounds": 3}
    assert mod.judge("metric", "mid_speech", few, [], "说重点")[0] == "INSUFFICIENT"

    ok = {"speech_turns": 40, "silent_bad_rounds": 0, "mid_speech_rounds": 60,
          "speech_tool_rounds": 100, "silent_max_worst": 1,
          "reply_turns": 40, "reply_boilerplate_rounds": 0}
    tag, obs = mod.judge("metric", "mid_speech", ok, [], "说重点")
    assert tag == "MEASURED_OK" and "60/100" in obs

    gap = dict(ok, silent_bad_rounds=20, silent_max_worst=17,
               reply_boilerplate_rounds=6)
    tag, obs = mod.judge("metric", "mid_speech", gap, [], "说重点")
    assert tag == "MEASURED_GAP"
    assert "17 轮" in obs and "套话 6/40" in obs, "节奏面与套话面要一起报，缺一面就只看半边"


def test_rule_map_points_say_less_at_mid_speech():
    mod = _load_audit()
    m = {r[0]: (r[1], r[2]) for r in mod.RULE_MAP}
    assert m["说重点"] == ("metric", "mid_speech")
