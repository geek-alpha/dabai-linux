#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""任务结局作为因变量：把「本轮到达终态的子任务」接进曝光流水。

背景（2026-09-14）：曝光流水早就有 keys（谁在场）和过程指标（工具轮数/报错/耗时），
但过程量不是结局——一轮调 40 次工具全失败，和一轮调 40 次工具全成功，在 metrics 里
长得一样。真结局只有任务终态（done/error/cancelled），落在 data/sub_agents.jsonl。

本文件锁死四条契约：

  1. 窗口半开：只收 (start, end] 内到达终态的任务，窗口外的一条不收
  2. 去重：同一 id 多行状态事件只算一次（取最后状态）
  3. 窗口首尾相接：上一轮收过的终态事件，下一轮不再重复收
  4. 首轮不从 0 起算：否则历史上所有终态任务会被一次性算进本轮，结局列永久失真
"""
import importlib.util
import json
import sys
import time
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

import agent  # noqa: E402


def _load_gf(tmp_path):
    """加载 tools/gene_fitness.py，把审计/埋点/流水都指到 tmp，不碰真实台账。"""
    spec = importlib.util.spec_from_file_location("gf_outcomes_under_test", BASE / "tools" / "gene_fitness.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.STATS = tmp_path / "stats.json"
    mod.EXPOSURE = tmp_path / "gene_exposure.jsonl"
    mod.SUBAGENT_HIST = tmp_path / "sub_agents.jsonl"
    return mod


def _write_hist(gf, events):
    gf.SUBAGENT_HIST.write_text(
        "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in events), encoding="utf-8")


def _rows(path):
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


# 真实量级的时间基准：秒级 ~1.7e9、毫秒级 ~1.7e12，1e11 是两者的分界。
# 用小数值（如 150.0）当基准会落进「毫秒」区间，测出来的行为跟线上不一样。
T = 1_700_000_000.0


def _ev(i, status, ts):
    """一条状态事件：updated_at 用毫秒，与 sub_agents.py:_hist_record 一致。"""
    return {"id": i, "status": status, "updated_at": int(ts * 1000)}


@pytest.fixture(autouse=True)
def _clean_round(monkeypatch):
    """agent 是进程内单例：缓冲与结局窗口都要在每个用例前后复位，否则互相污染。"""
    agent._gene_round_reset()
    agent._GENE_LAST_TS = 0.0
    yield
    agent._gene_round_reset()
    agent._GENE_LAST_TS = 0.0


def test_outcomes_window_is_half_open(tmp_path):
    gf = _load_gf(tmp_path)
    _write_hist(gf, [_ev("a", "done", T + 100), _ev("b", "done", T + 200), _ev("c", "done", T + 201)])
    assert gf.task_outcomes(T + 100, T + 200) == {"done": 1}, "左端开右端闭：只收 T+200 那条"
    assert gf.task_outcomes(T, T + 100) == {"done": 1}, "右端闭：T+100 那条落在上一轮的右端，不重复计"


def test_outcomes_ignores_non_terminal(tmp_path):
    gf = _load_gf(tmp_path)
    _write_hist(gf, [_ev("a", "queued", T + 150), _ev("b", "running", T + 150), _ev("c", "done", T + 150)])
    assert gf.task_outcomes(T + 100, T + 200) == {"done": 1}, "排队/执行中不是结局"


def test_outcomes_dedupes_by_id(tmp_path):
    gf = _load_gf(tmp_path)
    # 同一 id 的 queued→running→done 三行，只有终态行能进窗口；再造一次重复终态行
    _write_hist(gf, [_ev("a", "done", T + 150), _ev("a", "done", T + 160), _ev("b", "error", T + 170)])
    assert gf.task_outcomes(T + 100, T + 200) == {"done": 1, "error": 1}, "一个任务只能算一次"


def test_outcomes_survives_missing_and_broken_file(tmp_path):
    gf = _load_gf(tmp_path)
    assert gf.task_outcomes(0, 1e12) == {}, "文件不存在不是故障"
    gf.SUBAGENT_HIST.write_text("{坏行\n" + json.dumps(_ev("a", "done", T + 150)) + "\n", encoding="utf-8")
    assert gf.task_outcomes(T + 100, T + 200) == {"done": 1}, "坏行跳过，不拖垮整列"


def test_outcomes_accepts_seconds_too(tmp_path):
    gf = _load_gf(tmp_path)
    _write_hist(gf, [{"id": "a", "status": "done", "updated_at": T + 150}])
    assert gf.task_outcomes(T + 100, T + 200) == {"done": 1}, "秒级时间戳同样要能解析"


def test_flush_attaches_outcomes(tmp_path, monkeypatch):
    gf = _load_gf(tmp_path)
    monkeypatch.setattr(agent, "_gene_mod", lambda: gf)
    now = time.time()
    _write_hist(gf, [_ev("a", "done", now - 0.5)])
    agent._GENE_ROUND_KEYS.append("lesson:x")
    agent._GENE_ROUND_TS = "2026-09-14 10:00:00"
    assert agent._gene_flush_exposure({"tool_rounds": 1, "duration_ms": 5000}) == 1
    row = _rows(gf.EXPOSURE)[-1]
    assert row["metrics"]["task_outcomes"] == {"done": 1}


def test_window_does_not_recount(tmp_path, monkeypatch):
    gf = _load_gf(tmp_path)
    monkeypatch.setattr(agent, "_gene_mod", lambda: gf)
    now = time.time()
    _write_hist(gf, [_ev("a", "done", now - 0.5)])
    agent._GENE_ROUND_KEYS.append("lesson:x")
    agent._gene_flush_exposure({"duration_ms": 5000})
    # 第二轮：没有新的终态事件，同一个任务不能又被算一遍
    agent._GENE_ROUND_KEYS.append("lesson:y")
    agent._gene_flush_exposure({"duration_ms": 5000})
    rows = _rows(gf.EXPOSURE)
    assert rows[0]["metrics"]["task_outcomes"] == {"done": 1}
    assert "task_outcomes" not in rows[1]["metrics"], "上一轮收过的事件不许重复计数"


def test_first_round_does_not_scan_all_history(tmp_path, monkeypatch):
    gf = _load_gf(tmp_path)
    monkeypatch.setattr(agent, "_gene_mod", lambda: gf)
    now = time.time()
    _write_hist(gf, [_ev("old", "error", now - 100000.0)])
    agent._GENE_ROUND_KEYS.append("lesson:x")
    agent._gene_flush_exposure({"duration_ms": 1000})
    row = _rows(gf.EXPOSURE)[-1]
    assert "task_outcomes" not in row["metrics"], "首轮窗口只能覆盖本轮时长，不能从 0 起算"


def test_flush_survives_outcome_failure(tmp_path, monkeypatch):
    gf = _load_gf(tmp_path)
    monkeypatch.setattr(agent, "_gene_mod", lambda: gf)
    monkeypatch.setattr(gf, "task_outcomes", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    agent._GENE_ROUND_KEYS.append("lesson:x")
    assert agent._gene_flush_exposure({"duration_ms": 1000}) == 1, "结局采集失败不能吞掉曝光流水"
    assert _rows(gf.EXPOSURE)[-1]["keys"] == ["lesson:x"]


def _report_lines(gf, capsys):
    gf.report()
    return capsys.readouterr().out


def test_report_flags_unwired_instead_of_fake_green(tmp_path, capsys):
    """空结局列有两种原因，报表必须把它们分开——假绿比报错更难发现。"""
    gf = _load_gf(tmp_path)
    t1, t2 = time.time() - 60, time.time()
    _write_hist(gf, [_ev("a", "done", t2 - 10)])
    gf.EXPOSURE.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in (
        {"ts": t1, "turn": "t1", "keys": ["lesson:x"], "metrics": {"tool_rounds": 1}},
        {"ts": t2, "turn": "t2", "keys": ["lesson:x"], "metrics": {"tool_rounds": 1}},
    )) + "\n", encoding="utf-8")
    out = _report_lines(gf, capsys)
    assert "埋点没接线" in out, "窗口内明明有终态任务，报表不能报「没有任务结束」"


def test_report_says_not_failure_when_window_really_empty(tmp_path, capsys):
    gf = _load_gf(tmp_path)
    t1, t2 = time.time() - 60, time.time()
    gf.EXPOSURE.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in (
        {"ts": t1, "turn": "t1", "keys": ["lesson:x"], "metrics": {"tool_rounds": 1}},
        {"ts": t2, "turn": "t2", "keys": ["lesson:x"], "metrics": {"tool_rounds": 1}},
    )) + "\n", encoding="utf-8")
    out = _report_lines(gf, capsys)
    assert "不是采集失败" in out
    assert "埋点没接线" not in out
