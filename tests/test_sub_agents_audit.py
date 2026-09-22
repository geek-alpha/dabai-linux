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


# ---------- 用户端输入通道（自我迭代轮的批判人格指引） ----------
# 判据：指引必须以独立 user 消息进入上下文（与主人原话同级），
# 不是拼进任务书文本里当背景 —— 后者模型可以绕过。

def test_user_inputs_appended_as_separate_user_messages():
    msgs = [{"role": "system", "content": "S"}, {"role": "user", "content": "任务书"}]
    n = SA._inject_user_inputs(msgs, {"user_inputs": ["【用户端输入】照做", "第二条"]})
    assert n == 2
    assert [m["role"] for m in msgs] == ["system", "user", "user", "user"]
    assert msgs[1]["content"] == "任务书"        # 任务书原样保留，不被改写
    assert msgs[2]["content"].startswith("【用户端输入】")


def test_user_inputs_absent_noop():
    msgs = [{"role": "user", "content": "任务书"}]
    assert SA._inject_user_inputs(msgs, {}) == 0
    assert SA._inject_user_inputs(msgs, None) == 0
    assert SA._inject_user_inputs(msgs, {"user_inputs": ["", "   "]}) == 0
    assert len(msgs) == 1


def test_user_inputs_non_dict_extra_tolerated():
    """extra 被别处塞成字符串时不许炸掉整轮任务。"""
    msgs = []
    assert SA._inject_user_inputs(msgs, "user_inputs") == 0
    assert msgs == []


def _mk_manager(monkeypatch, replies):
    """造一个不碰网络/档案的 manager，replies 依次作为每轮 LLM 的正文。"""
    import sub_agents as mod

    mgr = mod.SubAgentManager.__new__(mod.SubAgentManager)
    monkeypatch.setattr(mgr, "_get_client", lambda prof=None: (None, "m"))
    monkeypatch.setattr(mgr, "_tool_defs", lambda prof=None: [])

    async def _noop(*a, **k):
        return None
    monkeypatch.setattr(mgr, "_mirror_log", _noop)

    seen = []

    async def fake_llm(client, model, messages, tools):
        import types
        seen.append([m["role"] for m in messages])
        text = replies[min(len(seen) - 1, len(replies) - 1)]
        msg = types.SimpleNamespace(content=text, tool_calls=[])
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg)])

    monkeypatch.setattr(mgr, "_llm_call", fake_llm)
    return mod, mgr, seen


def test_empty_final_content_retries_once(monkeypatch):
    """模型只回思考不回正文 → 补一次要结论的重试，而不是把整轮记成「没有结论」。"""
    import asyncio
    mod, mgr, seen = _mk_manager(monkeypatch, ["", "结论：干完了"])
    worker = mod.SubAgent("任务", "标题", None)
    out = asyncio.run(mgr._run_loop(worker))
    assert out == "结论：干完了"
    assert len(seen) == 2
    assert seen[1][-1] == "user"


def test_empty_final_content_retry_is_bounded(monkeypatch):
    """持续空回只补一次，不能无限重试。"""
    import asyncio
    mod, mgr, seen = _mk_manager(monkeypatch, [""])
    worker = mod.SubAgent("任务", "标题", None)
    out = asyncio.run(mgr._run_loop(worker))
    assert out == "（子智能体没有给出结论）"
    assert len(seen) == 2
