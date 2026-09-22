#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""「多步轮未提清单」运行时检查点的契约：抽象规则改不动行为，事实反馈才有埋点能验。

背景（2026-09-22）：规则区的「工作准则」实测 333 轮清单率 26%，逐日 34%→13%→41%
（噪声大），稳定低于达标线 50%；口径排查证明这不是阈值太严——纯只读探索轮只占
多步轮 4%，剔掉也只到 27%。同一天「连续单发只读」的教训已经写明：抽象要求无效，
在模型刚做完时给事实反馈才有效。这里照同一模式做「多步轮未提清单」。

契约（六条）：
  1. 判据与审计工具 call_stats 的 multi_rounds 同源（次数 >=3 且「跨 3 类工具或有文件
     改动」）——口径一分叉，提醒会打偏，埋点也和审计对不上账
  2. 只有「多步轮且没调 plan_update」才累加；提过清单/轮次不足/纯文本轮清零
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
    assert agent.PLAN_MISS_MIN_KINDS == 3
    mod = _load_audit()
    assert mod.PLAN_MISS_MIN_CALLS == agent.PLAN_MISS_MIN_CALLS
    assert mod.PLAN_MISS_MIN_KINDS == agent.PLAN_MISS_MIN_KINDS
    assert mod._WRITE_TOOLS == agent._PLAN_WRITE_TOOLS
    rows = [{"tool_calls": 3, "call_names": ["code_read"]},
            {"tool_calls": 3, "call_names": ["code_read", "code_search", "shell_run"]},
            {"tool_calls": 4, "call_names": ["code_read", "code_edit"]},
            {"tool_calls": 2, "call_names": ["code_read", "code_search"]}]
    assert mod.call_stats(rows)["multi_rounds"] == 2


def test_min_calls_boundary_is_inclusive():
    """恰好 3 个工具算多步轮（>=3），2 个不算。"""
    assert agent._plan_miss_note(0, 3, False)[0] == 1
    assert agent._plan_miss_note(0, 2, False) == (0, False)


def test_batch_query_round_is_not_multi():
    """同一种工具重复 3 次（一次并行读 3 个文件）是批量查询，不是多步任务。

    实测这类轮 40 个、0 次列清单：算进来只会让提示对着「我刚读了几个文件」喊
    「你没提清单」，模型判定不适用后，连真多步轮的提示一起忽略。
    """
    assert agent._plan_miss_note(0, 3, False, ["code_read"] * 3) == (0, False)
    assert agent._plan_miss_note(0, 6, False, ["shell_run", "code_read"]) == (0, False)


def test_two_kinds_with_file_edit_is_multi():
    """读+改是典型两环节改造，不该因为工具种类少被漏判。"""
    assert agent._plan_miss_note(0, 4, False, ["code_read", "code_edit"])[0] == 1


def test_missing_call_names_falls_back_to_count():
    """没有明细时退回纯次数判据：宁可多算一轮，不可漏判。"""
    assert agent._plan_miss_note(0, 3, False)[0] == 1


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


def test_hint_injected_on_every_tool_branch():
    """提醒要挂在**每一条**工具执行分支上，并且只写进「只给 LLM 的副本」。

    踩过的坑（2026-09-22）：初版只把 self._plan_miss_hint() 挂在 `if text_tool_call:`
    分支里。那条是文本协议分支，只有模型报「不支持 function calling」后 text_tool_mode
    才会置位——原生工具调用（`else:` 分支）才是常态路径。于是 streak 照常累加、armed
    照常置位、plan_hints 字段照常落盘，提示却永远没有出口：15 个采样轮 plan_hints 全 0，
    同样的窗口里并行提示 35 次（它在两条分支上都挂了）。判据没坏，是出口挂了——
    盯着 streak 阈值调参只会白费一轮。

    判据写成「与 _single_ro_hint() 的注入点一一对应」而不是写死数字：两条分支必须同增
    同减，谁再漏挂一条，这里立刻红。另加一条位置断言，钉死至少一个注入点落在原生分支里。
    """
    lines = _agent_lines()
    plan_calls = [i for i, l in enumerate(lines) if "self._plan_miss_hint()" in l]
    ro_calls = [i for i, l in enumerate(lines) if "self._single_ro_hint()" in l]
    assert len(ro_calls) >= 2, (
        f"对照物 _single_ro_hint() 只有 {len(ro_calls)} 个注入点，判据失去参照")
    assert len(plan_calls) == len(ro_calls), (
        f"清单提示 {len(plan_calls)} 个注入点、并行提示 {len(ro_calls)} 个——"
        "文本协议分支与原生工具调用分支必须都挂上")
    for i in plan_calls:
        ctx = "\n".join(lines[i:i + 5])
        assert "_llm_result" in ctx, f"第 {i + 1} 行的注入点没写进 _llm_result"
        assert "eff_plan_hints += 1" in ctx, f"第 {i + 1} 行的注入点没记埋点"
    # 原生分支独有标记：至少一个注入点必须排在它后面，否则仍是全挂在死分支上。
    native_marker = next(i for i, l in enumerate(lines) if "_merge_hinted: set = set()" in l)
    assert max(plan_calls) > native_marker, "没有任何注入点落在原生工具调用分支里"


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


# ---------- 契约 7：streak 落盘，重启不清零 ----------
# 背景（2026-09-22）：streak/armed 是纯内存字段，要连中 2 个多步轮才置位 armed，
# 而 journalctl 实测 120 分钟内 8 次热更新重启——计数永远攢不满，plan_hints 恒 0，
# 审计读数 rules:plan_rate 卡在 33%（达标线 50%）。病根与历史视图/技能激活集一样。

def _fake_agent(sid, streak=0, armed=False, plan_sid=None):
    """绕过 __init__ 造实例，只给 memory.session_id（落盘路由用它）。"""
    ag = agent.AIAgent.__new__(agent.AIAgent)

    class _M:
        pass

    m = _M()
    m.session_id = sid
    ag.memory = m
    ag._plan_miss_streak = streak
    ag._plan_miss_armed = armed
    ag._plan_miss_sid = plan_sid
    return ag


@pytest.fixture
def plan_miss_state_path(tmp_path, monkeypatch):
    """绝不碰真实 data/plan_miss_state.json：合成样本不能污染现场。"""
    p = tmp_path / "plan_miss_state.json"
    monkeypatch.setattr(agent, "PLAN_MISS_STATE_PATH", str(p))
    return p


def test_state_survives_restart(plan_miss_state_path):
    """落盘后能原样读回——这就是「重启不清零」的全部内容。"""
    agent._save_plan_miss_state("sid-A", 2, True)
    assert agent._load_plan_miss_state("sid-A") == (2, True)


def test_state_is_sid_scoped(plan_miss_state_path):
    """别的会话的状态不能串进来（新会话 = 从零起算）。"""
    agent._save_plan_miss_state("sid-A", 2, True)
    assert agent._load_plan_miss_state("sid-B") == (0, False)


def test_state_expires(plan_miss_state_path, monkeypatch):
    """隔太久的状态不能再算「连续 N 轮」。"""
    agent._save_plan_miss_state("sid-A", 2, True)
    monkeypatch.setattr(agent, "PLAN_MISS_STATE_TTL", 0.0)
    assert agent._load_plan_miss_state("sid-A") == (0, False)


def test_broken_file_degrades_to_zero(plan_miss_state_path):
    """读到坏文件一律当零，不能因为观测/持久化把主路径弄挂。"""
    plan_miss_state_path.write_text("{半截文件", encoding="utf-8")
    assert agent._load_plan_miss_state("sid-A") == (0, False)


def test_restore_brings_hint_back_after_restart(plan_miss_state_path):
    """核心契约：上一进程攢到 armed 的状态，重启后必须还能触发提示。

    什么观测会推翻它：把盘上的 armed=True 换成 False 后提示仍然出现（说明
    提示不是由恢复的状态触发的），或恢复后 hint 仍为空（落盘没接上）。
    """
    agent._save_plan_miss_state("sid-A", 2, True)
    ag = _fake_agent("sid-A")
    ag._restore_plan_miss()
    assert (ag._plan_miss_streak, ag._plan_miss_armed) == (2, True)
    assert "【清单提示】" in ag._plan_miss_hint()
    # 消费必须落盘：否则下次重启 armed 又是 True，同一句提示被反复注入
    assert agent._load_plan_miss_state("sid-A")[1] is False


def test_control_without_restore_means_no_hint(plan_miss_state_path, monkeypatch):
    """反证：同一份 armed 状态文件，只要「接回来」这一步没做（旧的内存版行为），
    提示就不存在——证明提示确实来自「从盘上接回」而不是别的路径。
    """
    agent._save_plan_miss_state("sid-A", 2, True)
    monkeypatch.setattr(agent, "_load_plan_miss_state", lambda sid: (0, False))
    ag = _fake_agent("sid-A")
    ag._restore_plan_miss()
    assert (ag._plan_miss_streak, ag._plan_miss_armed) == (0, False)
    assert ag._plan_miss_hint() == ""


def test_wiring_restore_and_persist_sites():
    """接线契约：三个挂点少一个，落盘就等于没写。

    · 每轮入口（_stabilize_tools_for_new_turn）必须调 _restore_plan_miss
    · 轮末 streak 更新后必须落盘（_record_turn_metrics 里 _plan_miss_note 之后）
    · 提示被消费后必须落盘（_plan_miss_hint 里 armed 置 False 之后）
    """
    lines = _agent_lines()
    stab = next(i for i, l in enumerate(lines)
                if "def _stabilize_tools_for_new_turn" in l)
    assert any("self._restore_plan_miss()" in l for l in lines[stab:stab + 30]), \
        "每轮入口没接回 streak——重启仍旧清零"
    upd = next(i for i, l in enumerate(lines) if "= _plan_miss_note(" in l)
    assert any("self._persist_plan_miss()" in l for l in lines[upd:upd + 6]), \
        "轮末没落盘——本次 streak 活不过重启"
    hz = next(i for i, l in enumerate(lines) if "def _plan_miss_hint" in l)
    hed = next(i for i in range(hz, len(lines))
               if "self._plan_miss_armed = False" in lines[i])
    assert any("self._persist_plan_miss()" in l for l in lines[hed:hed + 5]), \
        "提示消费后没落盘——重启后同一句提示会被反复注入"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
