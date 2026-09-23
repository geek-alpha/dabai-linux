#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""三条「无数据源」规则接上判据的契约（2026-09-24）。

背景：规则区审计的可量化率卡在 87%，卡点不是「样本不足」而是「根本没有数据源能回答」
——摸清大项目 / 简单任务直接做 / 交付即停三条只能标 UNMEASURED，留删靠人猜。

接法：
  · 摸清大项目   → call_names 一直在盘上，数「摸底轮里调没调 code_map」（现成数据源）
  · 简单任务直接做 → 委派任务文本长度 + 多步骤信号（现成数据源）
  · 交付即停     → 新埋点：只读意图轮里动了手（范围扩张）

契约：
  1. 三条映射不许回退成 none（回退 = 又变回「判不了」，而且没人会注意到）
  2. 三个判据各判 OK / GAP / INSUFFICIENT 三分支，达标线两侧都要有断言
  3. 范围扩张埋点必须落在「只读意图 + 无写指令」的交集上——并集会把「为什么…修掉它」
     这类明确授权的轮判成越界，读数被误报淹没
  4. 埋点接线必须在 tool_round 早退之前（纯文本轮的 message 也要能算只读意图）
  5. 定时调度 / 联邦来电不进「小任务」分母（它们的 task 文本不是人写的委派）
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]


def _load_audit():
    path = BASE / "tools" / "prompt_rules_audit.py"
    spec = importlib.util.spec_from_file_location("pra_three_sources", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_agent():
    path = BASE / "agent.py"
    spec = importlib.util.spec_from_file_location("agent_three_sources", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------- 1. 映射表不许回退 ----------

def test_three_rules_mapped_to_real_metrics():
    src = (BASE / "tools" / "prompt_rules_audit.py").read_text(encoding="utf-8")
    for rule in ("摸清大项目", "简单任务直接做", "交付即停"):
        assert f'("{rule}", "none", None)' not in src, f"{rule} 回退成无数据源了"
    assert '("摸清大项目", "metric", "code_map_use")' in src
    assert '("简单任务直接做", "delegate", "small_task")' in src
    assert '("交付即停", "metric", "scope_creep")' in src


def test_three_metrics_have_judge_branches():
    """映射到 metric ≠ 判得了：judge 里没分支会落到「映射表未覆盖」的 UNMEASURED 兜底。"""
    mod = _load_audit()
    for metric in ("code_map_use", "scope_creep"):
        _, obs = mod.judge("metric", metric, {}, [], "x", {})
        assert "映射表未覆盖" not in obs
    _, obs = mod.judge("delegate", "small_task", {}, [], "简单任务直接做", {})
    assert "映射表未覆盖" not in obs


# ---------- 2. 范围扩张判据：交集，不是并集 ----------

def test_scope_creep_only_counts_readonly_intent():
    ag = _load_agent()
    assert ag._scope_creep_stats("这个函数是什么", ["code_read"]) == (1, 0)
    # 只读意图 + 动手 = 越界
    assert ag._scope_creep_stats("这个函数是什么", ["code_read", "code_edit"]) == (1, 1)


def test_scope_creep_excludes_explicit_write_requests():
    """一句话里既有只读词又有写指令，授权就是写——这种轮里动手不算越界。"""
    ag = _load_agent()
    for msg in ("为什么报错？修掉它", "看一下这个文件然后改掉", "是什么问题，清理掉"):
        assert ag._scope_creep_stats(msg, ["code_edit"]) == (0, 0), msg


def test_scope_creep_ignores_shell_only_rounds():
    """只读诊断里跑 ls/cat 是常态：shell_run 不算「动手」，否则读数全是误报。"""
    ag = _load_agent()
    assert ag._scope_creep_stats("检查一下日志里的报错", ["shell_run", "code_read"]) == (1, 0)


def test_scope_creep_survives_bad_input():
    ag = _load_agent()
    assert ag._scope_creep_stats(None, None) == (0, 0)
    assert ag._scope_creep_stats("", ["code_edit"]) == (0, 0)


def test_scope_creep_wired_before_early_return():
    """纯文本轮也要能算只读意图：接线必须排在 tool_round 早退之前。"""
    src = (BASE / "agent.py").read_text(encoding="utf-8")
    call = src.index("_scope_creep_stats(message, eff_call_names)")
    assert src.index("if tool_round <= 0:", call) > call
    assert '"scope_creep": eff_scope_creep' in src
    assert '"readonly_rounds": eff_readonly_round' in src


# ---------- 3. judge 三分支 ----------

def test_scope_creep_judge_three_branches():
    mod = _load_audit()
    tag, obs = mod.judge("metric", "scope_creep", {"scope_creep": 1, "readonly_rounds": 5}, [])
    assert tag == "INSUFFICIENT" and "5 轮" in obs
    tag, obs = mod.judge("metric", "scope_creep", {"scope_creep": 0, "readonly_rounds": 100}, [])
    assert tag == "MEASURED_OK"
    tag, obs = mod.judge("metric", "scope_creep", {"scope_creep": 9, "readonly_rounds": 100}, [])
    assert tag == "MEASURED_GAP" and "9.0%" in obs


def test_code_map_use_judge_three_branches():
    mod = _load_audit()
    tag, obs = mod.judge("metric", "code_map_use", {}, [], "摸清大项目",
                         {"survey_rounds": 3, "survey_map_rounds": 3})
    assert tag == "INSUFFICIENT" and "3 轮" in obs
    tag, _ = mod.judge("metric", "code_map_use", {}, [], "摸清大项目",
                       {"survey_rounds": 40, "survey_map_rounds": 20})
    assert tag == "MEASURED_OK"
    tag, obs = mod.judge("metric", "code_map_use", {}, [], "摸清大项目",
                         {"survey_rounds": 40, "survey_map_rounds": 2})
    assert tag == "MEASURED_GAP" and "余 38 轮" in obs


def test_small_task_judge_three_branches():
    mod = _load_audit()
    few = [{"id": f"a{i}", "task": "读一下配置并汇报", "created_at": 1} for i in range(3)]
    tag, obs = mod.judge("delegate", "small_task", {}, few, "简单任务直接做", {})
    assert tag == "INSUFFICIENT" and "样本不足" in obs

    clean = [{"id": f"b{i}", "task": ("把 /home/wxf/dabai 下所有 python 文件的编码统一成 utf-8，"
                                      "先扫描再分批改，每批跑一次测试并汇报结果"), "created_at": 1}
             for i in range(9)]
    tag, _ = mod.judge("delegate", "small_task", {}, clean, "简单任务直接做", {})
    assert tag == "MEASURED_OK"

    dirty = [{"id": f"c{i}", "task": "读一下配置并汇报", "created_at": 1} for i in range(9)]
    tag, obs = mod.judge("delegate", "small_task", {}, dirty, "简单任务直接做", {})
    assert tag == "MEASURED_GAP" and "9 条是小任务" in obs


# ---------- 4. 数据源统计口径 ----------

def test_call_stats_counts_survey_rounds():
    mod = _load_audit()
    rows = [
        {"ts": 1.0, "tool_calls": 4, "call_names": ["code_map", "code_read", "code_search"]},
        {"ts": 2.0, "tool_calls": 4, "call_names": ["code_read", "code_search", "symbols"]},
        # 只跨 2 种 = 不是摸底轮（改一个小地方用不到 3 种）
        {"ts": 3.0, "tool_calls": 2, "call_names": ["code_read", "code_search"]},
    ]
    c = mod.call_stats(rows)
    assert c["survey_rounds"] == 2
    assert c["survey_map_rounds"] == 1


def test_delegation_stats_small_tasks_excludes_scheduled():
    mod = _load_audit()
    subs = [
        {"id": "s1", "task": "查一下天气", "title": "定时·天气", "created_at": 1},
        {"id": "s2", "task": "查一下天气", "title": "联邦来电：x", "created_at": 1},
        {"id": "s3", "task": "查一下天气", "title": "", "created_at": 1},
        {"id": "s4", "task": "重构 auth 模块：先扫描所有调用点，再分批改并逐个跑测试",
         "title": "", "created_at": 1},
    ]
    d = mod.delegation_stats(subs)
    assert d["spec_tasks"] == 2      # 定时/来电不计入
    assert d["small_tasks"] == 1     # 只有 s3 是小任务
