#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""「多步轮未提清单」运行时检查点的契约：抽象规则改不动行为，事实反馈才有埋点能验。

背景（2026-09-22）：规则区的「工作准则」实测 333 轮清单率 26%，逐日 34%→13%→41%
（噪声大），稳定低于达标线 50%；口径排查证明这不是阈值太严——纯只读探索轮只占
多步轮 4%，剔掉也只到 27%。同一天「连续单发只读」的教训已经写明：抽象要求无效，
在模型刚做完时给事实反馈才有效。这里照同一模式做「多步轮未提清单」。

契约（六条）：
  1. 判据与审计工具 call_stats 的 multi_rounds 同源（tool_calls>=3）——口径一分叉，
     提醒会打偏，埋点也和审计对不上账
  2. 只有「工具调用 >= 3 且没调 plan_update」才累加；提过清单/轮次不足/纯文本轮清零
  3. 每满 PLAN_MISS_STREAK_N 轮提醒一次——单轮漏提很常见，每轮都提醒等于噪声
  4. 提醒只加给 LLM 那份结果，消费一次即解除；绝不进 sys_prompt
     （首条一变整条前缀含全部历史白付全价，实测命中只剩 1664 token）
  5. 提示不反过来改计数（否则会自我强化）
  6. 纯文本轮也必须能清零 streak：判据必须在 tool_round 早退之前

这些用例不读真实日志，全部造数据；只有源码级契约读真实 agent.py。
"""
import importlib.util
import sys
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

import agent  # noqa: E402


def _mk_agent(streak=0, armed=False):
    """绕过 __init__ 造实例：这里只测状态机，不碰网络/记忆库/技能。"""
    ag = agent.AIAgent.__new__(agent.AIAgent)
    ag._plan_miss_streak = streak
    ag._plan_miss_armed = armed
    return ag


def _load_audit():
    """tools/ 不是包，按文件路径加载。每次拿干净模块，避免用例互相污染。"""
    path = BASE / "tools" / "prompt_rules_audit.py"
    spec = importlib.util.spec_from_file_location("prompt_rules_audit_plan_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _agent_lines():
    return (BASE / "agent.py").read_text(encoding="utf-8").splitlines()


# ---------- 契约 1：与审计工具同口径 ----------

def test_threshold_matches_audit_multi_rounds():
    """阈值硬编码 3 时必须与审计工具的 multi_rounds 判据一致。

    两边一分叉，就会出现「提醒了却不计入 multi_rounds」——埋点看不到效果，
    等于这个检查点白做。
    """
    assert agent.PLAN_MISS_MIN_CALLS == 3
    mod = _load_audit()
    rows = [{"tool_calls": 3, "call_names": ["code_read"]},
            {"tool_calls": 2, "call_names": ["code_read"]}]
    assert mod.call_stats(rows)["multi_rounds"] == 1


def test_min_calls_boundary_is_inclusive():
    """恰好 3 个工具算多步轮（>=3），2 个不算。"""
    assert agent._plan_miss_note(0, 3, False)[0] == 1
    assert agent._plan_miss_note(0, 2, False) == (0, False)


# ---------- 契约 2：累加与清零 ----------

def test_multi_round_without_plan_accumulates():
    streak, armed = agent._plan_miss_note(0, 5, False)
    assert (streak, armed) == (1, False)
    streak, armed = agent._plan_miss_note(streak, 3, False)
    assert (streak, armed) == (2, True)


def test_plan_update_resets_streak():
    """提过清单就是正确行为，不该继续计数，更不该给提示。"""
    assert agent._plan_miss_note(5, 8, True) == (0, False)


def test_short_round_resets_streak():
    assert agent._plan_miss_note(5, 2, False) == (0, False)


def test_text_round_resets_streak():
    """纯文本轮（模型收尾或纯回答）不是「多步轮漏提清单」。"""
    assert agent._plan_miss_note(5, 0, False) == (0, False)


# ---------- 契约 3：每满 N 轮提醒一次 ----------

def test_hint_fires_exactly_on_multiples_of_n():
    streak, fired = 0, []
    for i in range(1, 7):
        streak, armed = agent._plan_miss_note(streak, 4, False)
        if armed:
            fired.append(i)
    n = agent.PLAN_MISS_STREAK_N
    assert fired == [n, n * 2, n * 3]


def test_first_round_stays_silent():
    """阈值必须真的起作用——第 1 轮就给提示等于噪声。"""
    assert agent._plan_miss_note(0, 9, False)[1] is False


# ---------- 契约 4/5：提示文本与消费 ----------

def test_hint_requires_armed_state():
    assert _mk_agent(armed=False)._plan_miss_hint() == ""


def test_hint_names_the_streak_and_is_consumed_once():
    ag = _mk_agent(streak=3, armed=True)
    hint = ag._plan_miss_hint()
    assert "3" in hint and "plan_update" in hint
    assert ag._plan_miss_armed is False
    assert ag._plan_miss_hint() == ""   # 消费一次即解除，不会跨轮残留


def test_hint_never_leaks_into_counters():
    """提示是给 LLM 的一次性附加文本，不该反过来改计数（否则会自我强化）。"""
    ag = _mk_agent(streak=6, armed=True)
    ag._plan_miss_hint()
    assert ag._plan_miss_streak == 6


def test_hint_only_injected_into_llm_result():
    """提醒只挂在工具结果那份「只给 LLM 的副本」上，绝不进 sys_prompt。

    首条 system 一变，整条前缀（含全部历史）白付一次全价——实测命中只剩
    1664 token ≈ 首条本身。UI 与记忆库存原样结果。
    """
    lines = _agent_lines()
    calls = [i for i, l in enumerate(lines) if "self._plan_miss_hint()" in l]
    assert len(calls) == 1, f"调用点应唯一，实际 {len(calls)}"
    ctx = "\n".join(lines[calls[0]:calls[0] + 3])
    assert "_llm_result" in ctx


def test_hint_text_defined_once():
    """提示文案只允许出现在 _plan_miss_hint 里——多一份拷贝就多一个走偏的入口。"""
    src = "\n".join(_agent_lines())
    assert src.count("【清单提示】") == 1


# ---------- 契约 6：判据位置 ----------

def test_streak_update_sits_before_early_return():
    """更新必须排在 tool_round<=0 早退之前。

    早退在 _record_turn_metrics 里，纯文本轮走的就是那条路；把更新写在早退之后，
    「多步轮→纯文本轮→多步轮」会被误算成连续两轮，提醒打在正常行为上。
    """
    lines = _agent_lines()
    upd = [i for i, l in enumerate(lines) if "= _plan_miss_note(" in l]
    assert len(upd) == 1, f"更新点应唯一，实际 {len(upd)}"
    anchor = next(i for i, l in enumerate(lines)
                  if "_learn_tick(tool_round, eff_tool_calls" in l)
    ret = next(i for i in range(anchor, len(lines))
               if "if tool_round <= 0:" in lines[i])
    assert anchor < upd[0] < ret


def test_hint_counter_is_recorded_to_metrics():
    """埋点字段必须落进 turn_metrics，否则「提醒有没有效果」无从判断。"""
    lines = _agent_lines()
    assert any('"plan_hints": eff_plan_hints' in l for l in lines)
    assert any("eff_plan_hints = 0" in l for l in lines)
    assert any("eff_plan_hints += 1" in l for l in lines)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
