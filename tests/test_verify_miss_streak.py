#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""「改码轮没同轮验证」运行时检查点的契约。

背景（2026-09-22）：审计 trend 最近 3 天加权，158 个改码轮里只有 48% 同轮跑了验证
（达标线 60%）。规则区「改完就验」这条抽象要求改不动行为——照「多步轮未提清单」的
结论，得在模型刚改完的那一刻给事实反馈。本文件锁住这个检查点的三件套与出口。

契约（七条）：
  1. 判据与审计工具 call_stats 的 edit_rounds / verified_edit_rounds 同源（写工具集合与
     验证工具集合逐字相同）——口径一分叉，提示就会打偏，埋点也和审计对不上账
  2. 只有「调过写工具且一次验证工具都没调」才累加；验过的轮/没改码的轮清零
  3. 提示必须在**同一轮**里出现（改完那一刻），因此挂在每条工具执行分支上，
     且只写进「只给 LLM 的副本」，不动 UI 与记忆库
  4. 一轮只提醒一次；提示不反过来改计数
  5. 轮级标记在每轮入口清零（armed 跨轮残留会让下一轮一开工就喊）
  6. 埋点 verify_hints 必须落进 turn_metrics，否则「提醒有没有效果」无从判断
  7. streak 跨进程落盘：重启后接回来（同 sid 才接）

这些用例不读真实日志，全部造数据；只有源码级契约读真实 agent.py。
"""
import importlib.util
import sys
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

import agent  # noqa: E402


class _Mem:
    """只带 session_id 的假记忆库：进程重启 ≠ 换会话，靠 sid 判。"""

    def __init__(self, sid):
        self.session_id = sid


def _mk_agent():
    """绕过 __init__ 造实例：这里只测状态机，不碰网络/记忆库/技能。"""
    ag = agent.AIAgent.__new__(agent.AIAgent)
    ag._verify_miss_streak = 0
    ag._verify_miss_sid = None
    ag._verify_round_edit = False
    ag._verify_round_verified = False
    ag._verify_miss_armed = False
    return ag


def _fake_agent(sid):
    ag = _mk_agent()
    ag.memory = _Mem(sid)
    return ag


def _load_audit():
    """tools/ 不是包，按文件路径加载。每次拿干净模块，避免用例互相污染。"""
    path = BASE / "tools" / "prompt_rules_audit.py"
    spec = importlib.util.spec_from_file_location(
        "prompt_rules_audit_verify_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _agent_lines():
    return (BASE / "agent.py").read_text(encoding="utf-8").splitlines()


@pytest.fixture()
def verify_state_path(tmp_path, monkeypatch):
    p = tmp_path / "verify_miss_state.json"
    monkeypatch.setattr(agent, "VERIFY_MISS_STATE_PATH", str(p))
    return p


# ---------- 契约 1：与审计工具同口径 ----------

def test_tool_sets_match_audit():
    """写工具/验证工具两个集合必须与审计工具逐字同源。

    一分叉就会出现「运行时提醒了、审计却不计入改码轮」——埋点看不到效果，
    等于这个检查点白做（清单提示踩过同一个坑）。
    """
    mod = _load_audit()
    assert agent._VERIFY_EDIT_TOOLS == mod._EDIT_TOOLS
    assert agent._VERIFY_TOOLS == mod._VERIFY_TOOLS
    assert agent.VERIFY_MISS_STREAK_N == 2


def test_runtime_judge_and_audit_agree_on_same_rounds():
    """同一批轮：审计的「改码轮/同轮验证轮」与运行时判据必须给同一结论。"""
    mod = _load_audit()
    rows = [
        {"tool_calls": 2, "call_names": ["code_edit"]},                     # 改码未验
        {"tool_calls": 2, "call_names": ["code_edit", "code_test"]},        # 改码已验
        {"tool_calls": 1, "call_names": ["code_read"]},                     # 没改码
        {"tool_calls": 3, "call_names": ["code_create_file", "code_smoke"]},
    ]
    c = mod.call_stats(rows)
    assert (c["edit_rounds"], c["verified_edit_rounds"]) == (3, 2)
    miss = [r for r in rows if agent._verify_miss_note(0, r["call_names"])[0] == 1]
    assert len(miss) == 1 and miss[0]["call_names"] == ["code_edit"]


def test_patch_only_round_matches_the_audit_denominator():
    """只调 code_patch 的轮不触发：审计的改码轮分母里本来就没有 code_patch。

    判据必须跟着审计的分母走——对着一个审计不算改码的轮喊「你没验证」，
    模型判定不适用后，连真改码轮的提示一起忽略。
    """
    assert agent._verify_miss_note(0, ["code_patch"]) == (0, False)


# ---------- 契约 2：累加与清零 ----------

def test_edit_without_verify_accumulates():
    assert agent._verify_miss_note(0, ["code_edit"]) == (1, False)
    assert agent._verify_miss_note(1, ["code_create_file"]) == (2, True)


def test_verified_round_resets_streak():
    """同轮验过了就是正确行为，不该继续计数，更不该给提示。"""
    assert agent._verify_miss_note(5, ["code_edit", "code_test"]) == (0, False)


def test_non_edit_round_resets_streak():
    assert agent._verify_miss_note(5, ["code_read", "shell_run", "search_web"]) == (0, False)


def test_missing_names_is_silent():
    """没有明细时不猜：验证这一类没有次数兜底（几个工具推不出改没改码）。"""
    assert agent._verify_miss_note(3, None) == (0, False)
    assert agent._verify_miss_note(3) == (0, False)


def test_fires_exactly_on_multiples_of_n():
    streak, fired = 0, []
    for i in range(1, 7):
        streak, armed = agent._verify_miss_note(streak, ["code_edit"])
        if armed:
            fired.append(i)
    n = agent.VERIFY_MISS_STREAK_N
    assert fired == [n, n * 2, n * 3]


# ---------- 契约 3/4：同轮注入、一次消费、不改计数 ----------

def test_edit_arms_hint_immediately():
    """核心：提示在改完那一刻就能拿出来，而不是等本轮结束（等轮末就已经晚了）。"""
    ag = _mk_agent()
    ag._reset_verify_watch()
    assert ag._verify_miss_hint() == ""      # 还没改码：不喊
    ag._watch_verify_tool("code_edit")
    hint = ag._verify_miss_hint()
    assert "【验证提示】" in hint
    assert "code_verify" in hint and "同一轮" in hint
    assert ag._verify_miss_hint() == ""      # 一轮只提醒一次，不刷屏


def test_verify_disarms_hint():
    """先验后改：这一段已经验过了，不再喊（防假警）。"""
    ag = _mk_agent()
    ag._reset_verify_watch()
    ag._watch_verify_tool("code_test")
    ag._watch_verify_tool("code_edit")
    assert ag._verify_miss_hint() == ""


def test_verify_after_edit_disarms_too():
    ag = _mk_agent()
    ag._reset_verify_watch()
    ag._watch_verify_tool("code_edit")
    ag._watch_verify_tool("code_smoke")
    assert ag._verify_miss_hint() == ""


def test_hint_carries_cross_round_streak():
    """跨轮计数要出现在提示里——它正是「你已经连着改完不验」这句话的依据。

    什么观测会推翻它：streak 落盘接了回来、提示里却还写 0（那个字段没被读），
    或提示里根本不出现这个数字。
    """
    ag = _mk_agent()
    ag._verify_miss_streak = 2
    ag._reset_verify_watch()
    ag._watch_verify_tool("code_edit")
    assert "连续 2 轮" in ag._verify_miss_hint()


def test_hint_never_leaks_into_counters():
    """提示是给 LLM 的一次性附加文本，不该反过来改计数（否则会自我强化）。"""
    ag = _mk_agent()
    ag._verify_miss_streak = 6
    ag._reset_verify_watch()
    ag._watch_verify_tool("code_edit")
    ag._verify_miss_hint()
    assert ag._verify_miss_streak == 6


# ---------- 契约 5：轮级标记每轮清零 ----------

def test_reset_clears_round_marks():
    ag = _mk_agent()
    ag._watch_verify_tool("code_edit")
    assert ag._verify_miss_armed is True
    ag._reset_verify_watch()
    assert (ag._verify_round_edit, ag._verify_round_verified,
            ag._verify_miss_armed) == (False, False, False)
    assert ag._verify_miss_hint() == ""


def test_reset_is_wired_into_turn_entry():
    """接线契约：每轮入口不重置，上一轮那句提示会跨轮残留到新的一轮。"""
    lines = _agent_lines()
    stab = next(i for i, l in enumerate(lines)
                if "def _stabilize_tools_for_new_turn" in l)
    seg = "\n".join(lines[stab:stab + 40])
    assert "self._reset_verify_watch()" in seg
    assert "self._restore_verify_miss()" in seg


# ---------- 契约 6：埋点与出口 ----------

def test_hint_injected_on_every_tool_branch():
    """提示要挂在**每一条**工具执行分支上，并且只写进「只给 LLM 的副本」。

    与清单提示同一条教训：初版只挂在文本协议分支上，判据/armed/埋点全对，
    提示却永远没有出口（15 个采样轮 plan_hints 全 0）。判据写成「与清单提示的
    注入点一一对应」，谁再漏挂一条，这里立刻红。
    """
    lines = _agent_lines()
    plan_calls = [i for i, l in enumerate(lines) if "self._plan_miss_hint()" in l]
    verify_calls = [i for i, l in enumerate(lines) if "self._verify_miss_hint()" in l]
    assert len(plan_calls) >= 2, f"对照物清单提示只有 {len(plan_calls)} 个注入点"
    assert len(verify_calls) == len(plan_calls), (
        f"验证提示 {len(verify_calls)} 个注入点、清单提示 {len(plan_calls)} 个——"
        "文本协议分支与原生工具调用分支必须都挂上")
    for i in verify_calls:
        ctx = "\n".join(lines[i:i + 5])
        assert "_llm_result" in ctx, f"第 {i + 1} 行的注入点没写进 _llm_result"
        assert "eff_verify_hints += 1" in ctx, f"第 {i + 1} 行的注入点没记埋点"
    native_marker = next(i for i, l in enumerate(lines)
                         if "_merge_hinted: set = set()" in l)
    assert max(verify_calls) > native_marker, "没有任何注入点落在原生工具调用分支里"


def test_watch_registered_before_hint_on_every_branch():
    """登记点必须与 eff_call_names 的记录点一一对应，且排在提示注入之前。

    登记排在注入之后就等于没登记：改完那一刻拿不到 armed，提示只能等到下一轮。
    """
    lines = _agent_lines()
    # 匹配到左括号为止：登记点现在要带参数（shell_run 跑 pytest 与跑 ls 只差在命令
    # 内容里，只看工具名会把真验证记成没验证）。契约是「每个工具分支都挂了登记点」，
    # 不是「调用不带参」。
    watch = [i for i, l in enumerate(lines) if "self._watch_verify_tool(" in l]
    appends = [i for i, l in enumerate(lines) if "eff_call_names.append(tool_name)" in l]
    hint = [i for i, l in enumerate(lines) if "self._verify_miss_hint()" in l]
    assert len(appends) == 2, f"工具分支应恰好 2 条，实际 {len(appends)}"
    assert len(watch) == len(appends), "有工具分支没挂验证登记点"
    for w in watch:
        assert any(h > w for h in hint), "登记必须排在注入之前"


def test_hint_text_defined_once():
    """提示文案只允许出现在 _verify_miss_hint 里——多一份拷贝就多一个走偏的入口。"""
    src = "\n".join(_agent_lines())
    assert src.count("【验证提示】") == 1


def test_streak_update_sits_before_early_return():
    """更新必须排在 tool_round<=0 早退之前。

    早退在 _record_turn_metrics 里，纯文本轮走的就是那条路；把更新写在早退之后，
    「改码轮→纯文本轮→改码轮」会被误算成连续两轮。
    """
    lines = _agent_lines()
    upd = [i for i, l in enumerate(lines) if "= _verify_miss_note(" in l]
    assert len(upd) == 1, f"更新点应唯一，实际 {len(upd)}"
    anchor = next(i for i, l in enumerate(lines)
                  if "_learn_tick(tool_round, eff_tool_calls" in l)
    ret = next(i for i in range(anchor, len(lines))
               if "if tool_round <= 0:" in lines[i])
    assert anchor < upd[0] < ret


def test_counter_recorded_to_metrics():
    lines = _agent_lines()
    assert any('"verify_hints": eff_verify_hints' in l for l in lines)
    assert any("eff_verify_hints = 0" in l for l in lines)
    assert any("eff_verify_hints += 1" in l for l in lines)


def test_audit_reads_the_new_counter():
    """审计必须能读到新埋点，并给出「触发了几次」与「出口是否断了」两种结论。"""
    mod = _load_audit()
    rows = [{"tool_calls": 2, "call_names": ["code_edit"], "verify_hints": 1},
            {"tool_calls": 2, "call_names": ["code_edit"], "verify_hints": 0}]
    c = mod.call_stats(rows)
    assert c["verify_hint_sampled_rounds"] == 2
    assert c["verify_hint_total"] == 1
    assert c["verify_hint_expect"] == 2      # 两轮都「改了码没同轮验证」→ 下界 2
    # 下界成立：两轮各触发一次
    ok = mod.call_stats([{"call_names": ["code_edit"], "verify_hints": 1},
                         {"call_names": ["code_edit"], "verify_hints": 1}])
    assert "已触发 2 次" in mod.verify_hint_note(ok)
    assert "下界 2 次" in mod.verify_hint_note(ok)
    # 低于下界（这里 1 < 2）→ 只能是出口或口径断了，不许读成样本不足
    assert "出口或口径断了" in mod.verify_hint_note(c)
    # 0 触发 + 下界 0 → 样本不足，不许喊查出口
    c_zero = mod.call_stats([{"call_names": ["code_edit", "code_test"],
                              "verify_hints": 0}])
    assert "样本不足" in mod.verify_hint_note(c_zero)
    # 埋点还没上线（行里没这个字段）→ 明说尚无采样
    assert "尚无采样" in mod.verify_hint_note(mod.call_stats([{"tool_calls": 1}]))


# ---------- 契约 7：streak 落盘，重启不清零 ----------

def test_state_file_keeps_only_streak(verify_state_path):
    """盘上只留 streak：armed 是轮级标记，存下去会让重启后的第一轮一开工就喊。"""
    agent._save_verify_miss_state("sid-A", 4)
    assert agent._load_verify_miss_state("sid-A") == (4, False)


def test_state_is_sid_scoped(verify_state_path):
    agent._save_verify_miss_state("sid-A", 2)
    assert agent._load_verify_miss_state("sid-B") == (0, False)


def test_state_expires(verify_state_path, monkeypatch):
    agent._save_verify_miss_state("sid-A", 2)
    monkeypatch.setattr(agent, "VERIFY_MISS_STATE_TTL", 0.0)
    assert agent._load_verify_miss_state("sid-A") == (0, False)


def test_broken_file_degrades_to_zero(verify_state_path):
    verify_state_path.write_text("{半截文件", encoding="utf-8")
    assert agent._load_verify_miss_state("sid-A") == (0, False)


def test_restore_brings_streak_back_after_restart(verify_state_path):
    """核心契约：上一进程攒到的 streak，重启后必须接回来并出现在提示里。

    什么观测会推翻它：盘上明明有 streak=2，接回来后提示里却不写「连续 2 轮」。
    """
    agent._save_verify_miss_state("sid-A", 2)
    ag = _fake_agent("sid-A")
    ag._restore_verify_miss()
    assert ag._verify_miss_streak == 2
    ag._reset_verify_watch()
    ag._watch_verify_tool("code_edit")
    assert "连续 2 轮" in ag._verify_miss_hint()


def test_control_without_restore_means_zero(verify_state_path, monkeypatch):
    """反证：同一份盘上状态，只要「接回来」这一步没做（旧的内存版行为），
    提示里就没有跨轮数字——证明该数字确实来自盘上接回，而不是别处。
    """
    agent._save_verify_miss_state("sid-A", 2)
    monkeypatch.setattr(agent, "_load_verify_miss_state", lambda sid: (0, False))
    ag = _fake_agent("sid-A")
    ag._restore_verify_miss()
    assert ag._verify_miss_streak == 0
    ag._watch_verify_tool("code_edit")
    assert "连续" not in ag._verify_miss_hint()


def test_persist_wired_after_streak_update():
    """接线契约：轮末更新 streak 之后必须落盘，否则这个数活不过一次重启。"""
    lines = _agent_lines()
    upd = next(i for i, l in enumerate(lines) if "= _verify_miss_note(" in l)
    assert any("self._persist_verify_miss()" in l for l in lines[upd:upd + 3]), \
        "轮末没落盘——本次 streak 活不过重启"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))


# ---------- 契约 8：shell 命令级的验证信号（2026-09-22 口径修正） ----------
#
# 背景：call_names 只到工具名一层，而 94% 的轮都在用 shell_run——「改完码跑 pytest」
# 在只看工具名的旧口径里和「改完码 ls 一下」一模一样，都被算成没验证。实测代价
# （data/turn_metrics.jsonl 采样 6 轮）：4 个被判「未验证」的改码轮里每一轮都用
# shell_run 跑过 pytest 或实发探针，18 次提示全是假警；更糟的是提示措辞把模型推向
# code_verify（只做 py_compile），比它本来跑的 pytest 更弱。


def test_shell_pytest_counts_as_verify():
    assert agent._is_verify_call(
        "shell_run", {"command": "venv/bin/python -m pytest tests/ -q"})
    assert agent._is_verify_call(
        "shell_run", {"command": "python -m py_compile agent.py"})
    assert agent._is_verify_call(
        "shell_run", {"command": "venv/bin/python -m compileall tools"})
    assert agent._is_verify_call(
        "shell_run", {"command": "python -m unittest tests.test_x"})


def test_shell_non_verify_commands_are_not_verify():
    """反例：跑台账脚本 / 看目录 / 跑探针都不算——它们是写状态或探索，不是验证。

    宁可漏判（漏了只是读数保守），不可虚高（虚高会让缺口看不见）。
    """
    for cmd in ("ls -la", "cat agent.py",
                "venv/bin/python tools/lesson_add.py 'x'",
                "venv/bin/python tools/long_horizon.py log self-iterate 'x'",
                "venv/bin/python tools/self_iterate.py status",
                "git status"):
        assert not agent._is_verify_call("shell_run", {"command": cmd}), cmd


def test_non_shell_tools_are_not_verify_by_command():
    """非 shell 工具不看参数：code_read 的参数里出现 pytest 也不算验证。"""
    assert not agent._is_verify_call("code_read", {"file": "tests/test_pytest_x.py"})
    assert agent._is_verify_call("code_test", None)     # 工具名本身命中
    assert agent._is_verify_call("code_smoke", {})


def test_verify_call_judge_matches_audit():
    """判据必须与审计工具逐字同源：正则、shell 工具集合、验证工具集合三样都比。

    只比集合不够——shell 命令的识别正则正是这次新加的口径，它一分叉，运行时提示与
    审计读数就会对不上账（一边说验了、一边算没验）。
    """
    mod = _load_audit()
    assert agent._VERIFY_CMD_RE.pattern == mod._VERIFY_CMD_RE.pattern
    assert agent._SHELL_TOOLS == mod._SHELL_TOOLS
    assert agent._VERIFY_TOOLS == mod._VERIFY_TOOLS


def test_shell_pytest_disarms_hint():
    """改完码跑 pytest：提示必须当场撤掉，不能等到下一轮。"""
    ag = _mk_agent()
    ag._reset_verify_watch()
    ag._watch_verify_tool("code_edit")
    assert "【验证提示】" in ag._verify_miss_hint()
    ag._reset_verify_watch()
    ag._watch_verify_tool("code_edit")
    ag._watch_verify_tool(
        "shell_run", {"command": "venv/bin/python -m pytest tests/ -q"})
    assert ag._verify_miss_hint() == ""


def test_shell_hits_are_counted_for_audit():
    """轮级埋点：shell 里跑了几次验证命令要能读出来（审计据此判轮）。"""
    ag = _mk_agent()
    ag._reset_verify_watch()
    ag._watch_verify_tool("shell_run", {"command": "ls"})
    assert ag._verify_shell_hits == 0
    ag._watch_verify_tool("shell_run", {"command": "venv/bin/python -m pytest -q"})
    ag._watch_verify_tool("shell_run", {"command": "python -m py_compile x.py"})
    assert ag._verify_shell_hits == 2


def test_verify_miss_note_respects_verified_flag():
    """跨轮判据要认 shell 验证：只看 call_names 会把「改了码+跑 pytest」记成没验证。

    什么观测会推翻它：把 verified 参数从调用点摘掉，streak 就会在跑过 pytest 的轮上
    继续累加（提示开始假警）。
    """
    assert agent._verify_miss_note(0, ["code_edit", "shell_run"], True) == (0, False)
    assert agent._verify_miss_note(0, ["code_edit", "shell_run"], False) == (1, False)
    assert agent._verify_miss_note(1, ["code_edit"], False) == (2, True)
    # verified 为 None（老调用方）：退回只看工具名，行为与改动前一致
    assert agent._verify_miss_note(0, ["code_edit", "shell_run"], None) == (1, False)
    assert agent._verify_miss_note(0, ["code_edit", "code_test"], None) == (0, False)


def test_audit_round_verified_reads_shell_hits():
    """审计侧轮级判定：老数据没有 verify_shell_hits 字段时退化成旧口径，
    不会把「没埋点」读成「验过了」。"""
    mod = _load_audit()
    assert mod._round_verified({"call_names": ["code_edit"]}) is False
    assert mod._round_verified(
        {"call_names": ["code_edit"], "verify_shell_hits": 0}) is False
    assert mod._round_verified(
        {"call_names": ["code_edit"], "verify_shell_hits": 1}) is True
    assert mod._round_verified({"call_names": ["code_edit", "code_test"]}) is True


def test_shell_hits_wired_into_turn_metrics():
    """接线契约：轮级计数要落进 turn_metrics，否则审计永远读不到（口径改了也白改）。

    踩过同一个坑：埋点加了但没接进 record，审计里恒 0——分不清「没跑验证」与「没记」。
    """
    src = "\n".join(_agent_lines())
    assert 'eff_verify_shell = int(getattr(self, "_verify_shell_hits", 0) or 0)' in src
    assert '"verify_shell_hits": eff_verify_shell,' in src


def test_shell_hits_reset_per_round():
    """轮级标记必须每轮清零，否则上一轮的验证会把下一轮的「没验证」盖掉。"""
    ag = _mk_agent()
    ag._reset_verify_watch()
    ag._watch_verify_tool("shell_run", {"command": "venv/bin/python -m pytest -q"})
    assert ag._verify_shell_hits == 1
    ag._reset_verify_watch()
    assert ag._verify_shell_hits == 0
    assert ag._verify_round_verified is False
