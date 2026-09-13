# -*- coding: utf-8 -*-
"""子智能体审计落盘的存活契约。

背景：注册表在内存里，进程一重启就查不到 worker 干过什么——验收子智能体时只能靠
产物文件 mtime 反推，审计成本高。落盘 data/sub_agents.jsonl 后，重启仍可回查。
这组用例锁住三件事：状态变更真的写盘、重启后能回灌去重、审计坏了不许拖垮主流程。
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import sub_agents as SA


@pytest.fixture
def hist(tmp_path, monkeypatch):
    """把审计文件指向临时路径，别污染真实 data/sub_agents.jsonl。"""
    f = tmp_path / "h.jsonl"
    monkeypatch.setattr(SA, "_HIST_FILE", f)
    return f


def _lines(f):
    return [json.loads(x) for x in f.read_text(encoding="utf-8").splitlines() if x.strip()]


def test_lifecycle_written(hist):
    """spawn → running → done 三次变更都要落盘，末条带最终状态与正文。"""
    w = SA.SubAgent("跑巡店", "巡店", ws=None, profile="shopkeeper")
    SA._hist_record(w, "spawn")
    w.status = SA.ST_RUNNING
    SA._hist_record(w, "status")
    w.result = "第一行结论\n细节"
    w.status = SA.ST_DONE
    SA._hist_record(w, SA.ST_DONE)

    recs = _lines(hist)
    assert [r["event"] for r in recs] == ["spawn", "status", "done"]
    assert recs[-1]["status"] == SA.ST_DONE
    assert recs[-1]["profile"] == "shopkeeper"
    assert "第一行结论" in recs[-1]["result"]


def test_reload_dedup_and_restored_flag(hist):
    """重启后回灌：同一 worker 只留最后一次状态，且 list() 标 restored=True。"""
    w = SA.SubAgent("跑巡店", "巡店", ws=None, profile="shopkeeper")
    for ev, st in (("spawn", SA.ST_QUEUED), ("status", SA.ST_RUNNING), ("done", SA.ST_DONE)):
        w.status = st
        SA._hist_record(w, ev)

    m = SA.SubAgentManager()
    assert len(m._history) == 1
    out = m.list(limit=10)
    assert len(out) == 1
    assert out[0]["restored"] is True
    assert out[0]["status"] == SA.ST_DONE


def test_bad_lines_and_missing_file_dont_raise(hist):
    """坏行跳过、文件不存在返回空——审计绝不能成为故障源。"""
    assert SA.SubAgentManager()._history == []

    hist.write_text('{"id":"x","ts":1}\n这不是json\n{"id":"y","ts":2}\n', encoding="utf-8")
    assert len(SA.SubAgentManager()._history) == 2


def test_trim_keeps_newest(hist, monkeypatch):
    """超限截断只保留最近 _HIST_KEEP 行，且留下的是最新的。"""
    monkeypatch.setattr(SA, "_HIST_MAX_BYTES", 300)
    monkeypatch.setattr(SA, "_HIST_KEEP", 20)
    for i in range(60):
        SA._hist_record(SA.SubAgent("任务%d" % i, "T%d" % i, ws=None), "spawn")

    recs = _lines(hist)
    assert len(recs) == 20
    assert recs[-1]["title"] == "T59"


def test_write_failure_is_swallowed(monkeypatch):
    """写盘失败（如目录不可写）只告警，不抛异常打断执行。"""
    monkeypatch.setattr(SA, "_HIST_FILE", Path("/proc/nonexistent/h.jsonl"))
    SA._hist_record(SA.SubAgent("任务", "T", ws=None), "spawn")   # 不抛即通过
