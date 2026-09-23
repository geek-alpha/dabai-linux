#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""清单状态回显（_plan_recap）的契约：把账本摊在模型眼前，驱动「逐步更新」。

背景（实测数据，不是感觉）：skills/tasks/data/agent_plan.json 的 history 重建后，
8 份清单里 3 份只提交过 1 次（创建后再没更新），更新过的里出现 [3,5,6] 这种序列
——提清单时前 3 步已经干完了，是事后补记而不是事前规划。

病根在判据口径：_plan_early_hint / _plan_miss_hint 问的都是「**没提过**清单吗」，
提过一次就双双静默（_plan_round_used 置位、streak 清零）。于是「提了但从不更新」
这件事落在所有检测的缝里，直到任务结束都没人提。

契约（五条）：
  1. 清单存在且未全部完成 → 每轮回显一次，含进度与进行中那一步
  2. 清单全完成 / 无清单 / 读不到清单 → 静默（回显不是进度条装饰，是行动提示）
  3. 每轮只回显一次，且轮入口必须重新武装（否则长任务只回显第一轮）
  4. 挂在**每一条**工具执行分支上，且只写进「只给 LLM 的副本」
  5. 埋点落 turn_metrics（plan_recaps）——没埋点就没法证明机制真的生效

这些用例不读真实日志，全部造数据；只有源码级契约读真实 agent.py。
"""
import importlib.util
import sys
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

import agent  # noqa: E402


def _mk_agent():
    """绕过 __init__ 造实例：这里只测回显逻辑，不碰网络/记忆库/技能。"""
    return agent.AIAgent.__new__(agent.AIAgent)


def _agent_lines():
    return (BASE / "agent.py").read_text(encoding="utf-8").splitlines()


def _fake_plan(monkeypatch, plan):
    monkeypatch.setattr(agent, "_load_agent_plan", lambda: {"plan": plan})


# ---------- 契约 1：有活没干完就回显 ----------

def test_recap_shows_progress_and_running_step(monkeypatch):
    _fake_plan(monkeypatch, [
        {"step": "读代码", "status": "completed"},
        {"step": "改代码", "status": "in_progress"},
        {"step": "跑测试", "status": "pending"},
    ])
    r = _mk_agent()._plan_recap()
    assert "【清单】1/3" in r, r
    assert "进行中：改代码" in r, r
    assert "还有 2 步" in r, r


def test_recap_flags_missing_in_progress(monkeypatch):
    """有 pending 却一个 in_progress 都没有——最典型的搁置，必须点出来。"""
    _fake_plan(monkeypatch, [
        {"step": "A", "status": "completed"},
        {"step": "B", "status": "pending"},
    ])
    r = _mk_agent()._plan_recap()
    assert "没有进行中的步骤" in r, r


# ---------- 契约 2：该静默的场景 ----------

def test_recap_silent_when_all_done(monkeypatch):
    """全完成是等清空，不是搁置——回显在这里只会变成噪声。"""
    _fake_plan(monkeypatch, [{"step": "A", "status": "completed"}])
    assert _mk_agent()._plan_recap() == ""


def test_recap_silent_without_plan(monkeypatch):
    """没提过清单的单点小改不该被塞一行进度。"""
    _fake_plan(monkeypatch, [])
    assert _mk_agent()._plan_recap() == ""


def test_recap_silent_on_broken_plan(monkeypatch):
    """plan 字段坏掉时静默退化，绝不能把整轮工具结果搞坏。"""
    monkeypatch.setattr(agent, "_load_agent_plan", lambda: {"plan": None})
    assert _mk_agent()._plan_recap() == ""


def test_load_agent_plan_never_raises():
    """真实路径读不到也必须返回 dict——回显的失败模式只能是「没有它」。"""
    assert isinstance(agent._load_agent_plan(), dict)


# ---------- 契约 3：每轮一次 + 轮入口重新武装 ----------

def test_recap_once_per_round(monkeypatch):
    _fake_plan(monkeypatch, [{"step": "A", "status": "in_progress"}])
    ag = _mk_agent()
    assert ag._plan_recap() != ""
    assert ag._plan_recap() == "", "同一轮里重放等于刷屏"


def test_reset_rearms_recap(monkeypatch):
    """轮入口不清标记的话，长任务只有第一轮看得见账本——机制等于没做。"""
    _fake_plan(monkeypatch, [{"step": "A", "status": "in_progress"}])
    ag = _mk_agent()
    assert ag._plan_recap() != ""
    assert ag._plan_recap() == ""
    ag._reset_plan_watch()
    assert ag._plan_recap() != "", "每轮入口必须重新武装回显"


# ---------- 契约 4：两条工具执行分支都挂 ----------

def test_recap_injected_on_every_tool_branch():
    """回显必须和 _single_ro_hint() 的注入点一一对应。

    踩过的坑同清单提示：只挂文本协议分支时，原生工具调用（常态路径）没有出口——
    计数照常累加、埋点照常落盘，模型却永远看不到那句话。判据写成「与对照物同增同减」
    而不是写死数字，谁再漏挂一条这里立刻红。
    """
    lines = _agent_lines()
    recap = [i for i, l in enumerate(lines) if "self._plan_recap()" in l]
    ro = [i for i, l in enumerate(lines) if "self._single_ro_hint()" in l]
    assert len(ro) >= 2, f"对照物 _single_ro_hint() 只有 {len(ro)} 个注入点，判据失去参照"
    assert len(recap) == len(ro), (
        f"回显 {len(recap)} 个注入点、并行提示 {len(ro)} 个——"
        "文本协议分支与原生工具调用分支必须都挂上")
    for i in recap:
        ctx = "\n".join(lines[i:i + 4])
        assert "_llm_result" in ctx, f"第 {i + 1} 行的注入点没写进 _llm_result"
        assert "eff_plan_recaps += 1" in ctx, f"第 {i + 1} 行的注入点没记埋点"
    native_marker = next(i for i, l in enumerate(lines) if "_merge_hinted: set = set()" in l)
    assert max(recap) > native_marker, "没有任何注入点落在原生工具调用分支里"


def test_recap_text_defined_once():
    """文案只允许出现在 _plan_recap 里——多一份拷贝就多一个走偏的入口。"""
    src = "\n".join(_agent_lines())
    assert src.count("【清单】") == 1


# ---------- 契约 5：埋点可查 ----------

def test_recap_metric_recorded():
    src = "\n".join(_agent_lines())
    assert '"plan_recaps": eff_plan_recaps,' in src, "turn_metrics 里没有回显埋点，机制生效与否无法验证"
    assert "eff_plan_recaps = 0" in src, "埋点计数器没在轮初初始化"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
