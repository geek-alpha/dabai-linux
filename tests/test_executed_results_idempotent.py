#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""工具级幂等恢复（executed_results）回归契约。

背景（对标 Anthropic lib/tools/_beta_session_runner.py 的 _reconcile）：
大白断点是回合级——落盘点 b（工具执行前，pending_tools=本轮全部待执行工具）
与落盘点 c（工具执行完清空）之间的崩溃窗口里，工具可能已执行成功但磁盘
checkpoint 仍把它列为「待执行」→ 断点续跑会重放 → 写操作（code_edit /
send_message / sched_add）执行两次。

修复：checkpoint 增加 executed_results（tool_call_id -> {result, success}），
工具执行成功即立即落盘；resume 时已答工具直接回灌结果、绝不二次执行——
等价于 Anthropic 的 _answered 集合（result post 成功即 answered）。

三条契约：
1. checkpoint 结构必须带 executed_results，且 save→load round-trip 不丢。
2. 执行成功路径必须「立即持久化」（工具执行后马上落盘，窗口缩到单工具执行中）。
3. resume 重放必须按 executed_results 过滤（已答不重放）。
"""
import ast
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parents[1]

import agent  # noqa: E402


# ---------- 契约 1：checkpoint round-trip 持久化 ----------

def test_executed_results_roundtrip(tmp_path, monkeypatch):
    """executed_results 随 checkpoint 落盘并可完整读回（含 result/success）。"""
    monkeypatch.setattr(agent, "TURN_CKPT_DIR", tmp_path)
    cp = {
        "version": 1,
        "turn_id": "t_roundtrip",
        "user_id": "u1",
        "updated_at": 1234567890.0,
        "pending_tools": [{"id": "call_1", "name": "code_edit"}],
        "executed_results": {
            "call_1": {"result": "文件已修改", "success": True},
        },
    }
    agent.save_turn_checkpoint("u1", cp)
    loaded = agent.load_turn_checkpoint("u1")
    assert loaded is not None
    er = loaded.get("executed_results") or {}
    assert "call_1" in er, "executed_results 必须随 checkpoint 持久化"
    assert er["call_1"]["result"] == "文件已修改"
    assert er["call_1"]["success"] is True


def test_executed_results_absent_on_old_ckpt(tmp_path, monkeypatch):
    """旧版 checkpoint 没有 executed_results 时读回为空 dict——不崩、全部重放（旧行为）。"""
    monkeypatch.setattr(agent, "TURN_CKPT_DIR", tmp_path)
    cp = {
        "version": 1,
        "turn_id": "t_old",
        "user_id": "u2",
        "updated_at": 1234567890.0,
        "pending_tools": [],
    }
    agent.save_turn_checkpoint("u2", cp)
    loaded = agent.load_turn_checkpoint("u2")
    assert (loaded.get("executed_results") or {}) == {}


# ---------- 契约 2/3：静态契约（防重构删掉幂等机制） ----------

def _agent_tree():
    return ast.parse((BASE / "agent.py").read_text(encoding="utf-8", errors="replace"))


def _src_contains(*needles):
    src = (BASE / "agent.py").read_text(encoding="utf-8", errors="replace")
    missing = [n for n in needles if n not in src]
    assert not missing, f"agent.py 缺失关键片段: {missing}"


def test_ckpt_struct_carries_executed_results():
    """checkpoint 结构必须带 executed_results 字段（_save_round_ckpt 的 cp dict）。"""
    _src_contains('"executed_results": executed_results,')


def test_success_persists_immediately():
    """工具执行成功必须立即持久化：两条路径（原生/文本）都有 executed_results 写入。"""
    src = (BASE / "agent.py").read_text(encoding="utf-8", errors="replace")
    # 原生协议：执行结果循环里写 executed_results（按 tc["id"]）
    assert 'executed_results[str(tc["id"])]' in src
    # 文本协议：按 text_{tool_round} 写
    assert 'executed_results[f"text_{tool_round}"]' in src
    # 写后立即落盘（窗口缩到单工具执行中）
    assert src.count("_save_round_ckpt") >= 5, (
        "工具执行后必须立即落盘（b 点 2 处 + 执行成功 2 处 + c 点 1 处）")


def test_resume_filters_answered():
    """resume 重放必须按 executed_results 过滤：已答工具不二次执行。"""
    _src_contains("resume_answered: list = []")
    _src_contains("if _pid in executed_results:")
    _src_contains("if _tpid in executed_results:")


def test_answered_merged_back_into_round():
    """已答工具的结果必须回灌本轮（并入 tool_call_results，走正常消息回填路径）。"""
    _src_contains("for _ra in resume_answered:")


def test_ckpt_cleared_after_round():
    """本轮工具全部执行完后 executed_results 必须清空（防止断点文件无限膨胀）。"""
    _src_contains("executed_results = {}")
