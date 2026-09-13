#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""长跑引擎 → 任务中心视图的端到端测试。

背景（2026-09-13）：长跑引擎是 systemd 拉起的独立进程，既不在 orchestrator
注册表、也不在 Harness TaskSystem 里，任务中心完全看不见它 —— 用户只能靠
`runner.py --status` 手动问。这里固化「合成条目」的契约：
  1. /api/tasks 必须出现 longrun 条目，且带前端渲染所需字段（agent/steps/...）；
  2. /api/tasks/longrun-engine 详情能拿到完整轮次台账；
  3. 合成过程只读 —— 不写任何文件、不启停任何服务。
"""
import json
import os
import sys
import time
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

from tools.longrun import status_view as sv  # noqa: E402


# ---------- 合成视图：契约与只读性 ----------

def test_snapshot_has_fields_frontend_needs():
    s = sv.snapshot(full=False)
    for k in ("id", "kind", "channel", "title", "status", "steps",
              "logs_tail", "logs_count", "agent", "created_at", "updated_at"):
        assert k in s, f"任务中心渲染需要的字段缺失：{k}"
    assert s["id"] == "longrun-engine"
    assert s["kind"] == "longrun"
    assert s["status"] in ("running", "cancelled", "error")
    assert s["agent"]["name"], "agent.name 为空 → 卡片会显示成裸 channel 名"
    assert isinstance(s["created_at"], int) and s["created_at"] > 0


def test_snapshot_full_carries_round_ledger():
    s = sv.snapshot(full=True)
    assert "brief" in s and "logs" in s
    # 详情页要能回答「它在干嘛」：轮次台账 + 上一轮产出
    assert any("轮" in x for x in s["steps"]) or s["steps"] == []


def test_snapshot_is_read_only(tmp_path, monkeypatch):
    """只读契约：合成快照不许写文件（任务中心每几秒轮询一次）。"""
    monkeypatch.setattr(sv, "RUN_DIR", tmp_path)
    monkeypatch.setattr(sv, "STATE", tmp_path / "state.json")
    monkeypatch.setattr(sv, "JOURNAL", tmp_path / "journal.jsonl")
    monkeypatch.setattr(sv, "HEARTBEAT", tmp_path / "heartbeat")
    monkeypatch.setattr(sv, "STOP", tmp_path / "STOP")
    monkeypatch.setattr(sv, "LOCK", tmp_path / "runner.lock")
    before = set(os.listdir(tmp_path))
    s = sv.snapshot(full=True)
    assert set(os.listdir(tmp_path)) == before, "快照过程写了文件，违反只读契约"
    assert s["status"] == "cancelled"      # 空目录 = 没跑过
    assert s["extra"]["cycle"] == 0


def test_stale_heartbeat_is_flagged(tmp_path, monkeypatch):
    """心跳滞后要显式告警 —— 这正是「不知道它在干嘛」的核心场景。"""
    monkeypatch.setattr(sv, "RUN_DIR", tmp_path)
    monkeypatch.setattr(sv, "STATE", tmp_path / "state.json")
    monkeypatch.setattr(sv, "JOURNAL", tmp_path / "journal.jsonl")
    monkeypatch.setattr(sv, "HEARTBEAT", tmp_path / "heartbeat")
    monkeypatch.setattr(sv, "STOP", tmp_path / "STOP")
    monkeypatch.setattr(sv, "LOCK", tmp_path / "runner.lock")
    (tmp_path / "state.json").write_text(json.dumps({"cycle": 3, "last_goal": "g"}))
    (tmp_path / "heartbeat").write_text(str(int(time.time())))
    (tmp_path / "runner.lock").write_text(str(os.getpid()))   # 自己当 runner：活着
    monkeypatch.setattr(sv, "_proc_alive", lambda pid: pid == os.getpid())
    old = time.time() - (sv.STALE_AFTER + 60)
    os.utime(tmp_path / "heartbeat", (old, old))
    s = sv.snapshot(full=True)
    assert s["status"] == "running"
    assert any("滞后" in x for x in s["logs"]), f"滞后未告警：{s['logs']}"


def test_service_action_rejects_unknown():
    ok, msg = sv.service_action("drop-database")
    assert ok is False and "未知动作" in msg


# ---------- HTTP 层：任务中心真的能看见它 ----------

@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient
    import server
    return TestClient(server.app)


def test_api_tasks_lists_longrun(client):
    r = client.get("/api/tasks")
    assert r.status_code == 200
    tasks = r.json()["tasks"]
    hits = [t for t in tasks if t.get("id") == "longrun-engine"]
    assert len(hits) == 1, f"任务中心列表里没有且仅有一条长跑条目：{[t.get('id') for t in tasks]}"
    t = hits[0]
    assert t["kind"] == "longrun" and t["agent"]["name"]


def test_api_task_detail_longrun(client):
    r = client.get("/api/tasks/longrun-engine")
    assert r.status_code == 200
    task = r.json()["task"]
    assert task["id"] == "longrun-engine"
    assert "logs" in task and "brief" in task


# ---------- 轮次 trace：一轮到底干了什么 ----------

from tools.longrun import runner as rn  # noqa: E402

STUB_CLI = '''#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""假 worker：按 --json 契约吐事件流，用来验证 trace 落盘，不烧 token。"""
import json
import sys


def emit(d):
    sys.stdout.write(json.dumps(d, ensure_ascii=False) + "\\n")
    sys.stdout.flush()


emit({"type": "StreamDelta", "text": "亲爱的，结论先给：这一步做完了。"})
emit({"type": "ToolCallStart", "tool_name": "code_read",
      "arguments": json.dumps({"path": "agent.py"}, ensure_ascii=False)})
emit({"type": "ToolCallResult", "success": True, "result": "ok"})
emit({"type": "UsageEvent", "prompt_tokens": 1234, "completion_tokens": 56, "rounds": 3})
'''


def _isolate_runner(tmp_path, monkeypatch):
    """把 runner 的磁盘路径全挪进 tmp_path —— 测试绝不碰真引擎的运行数据。"""
    run_dir = tmp_path / "data" / "longrun"
    stub = tmp_path / "stub_cli.py"
    stub.write_text(STUB_CLI, encoding="utf-8")
    monkeypatch.setattr(rn, "BASE", tmp_path)
    monkeypatch.setattr(rn, "RUN_DIR", run_dir)
    monkeypatch.setattr(rn, "TRACES", run_dir / "traces")
    monkeypatch.setattr(rn, "JOURNAL", run_dir / "journal.jsonl")
    monkeypatch.setattr(rn, "STATE", run_dir / "state.json")
    monkeypatch.setattr(rn, "STOP", run_dir / "STOP")
    monkeypatch.setattr(rn, "HEARTBEAT", run_dir / "heartbeat")
    monkeypatch.setattr(rn, "LEDGER", tmp_path / "long_horizon.json")
    monkeypatch.setattr(rn, "CLI", stub)
    monkeypatch.setattr(rn, "PY", Path(sys.executable))
    monkeypatch.setattr(rn, "CALL_TIMEOUT", 60)
    return run_dir


def test_one_cycle_writes_drillable_trace(tmp_path, monkeypatch):
    """一轮跑完必须留下可下钻的 trace：prompt 摘要 + 工具调用序列 + 退出码。"""
    run_dir = _isolate_runner(tmp_path, monkeypatch)
    (tmp_path / "long_horizon.json").write_text(json.dumps({
        "projects": [{"id": "demo", "title": "演示目标", "stage": "active",
                      "next": "读一遍 runner.py", "progress": 0, "log": []}],
    }, ensure_ascii=False), encoding="utf-8")

    state = rn.load_state()
    assert rn.one_cycle(state) == "run"

    trace = run_dir / "traces" / "1.jsonl"
    assert trace.exists(), "本轮没有留下 trace 文件"
    events = [json.loads(x) for x in trace.read_text(encoding="utf-8").splitlines()]
    kinds = [e["type"] for e in events]
    assert kinds[0] == "prompt", f"trace 首行应是 prompt 摘要：{kinds[:3]}"
    assert events[0]["chars"] > 0 and events[0]["head"]
    assert "start" in kinds and kinds[-1] == "exit"
    assert kinds.count("ToolCallStart") == 1 and kinds.count("ToolCallResult") == 1
    last = events[-1]
    assert last["exit"] == 0 and last["goal"] == "demo"
    assert [t["name"] for t in last["tools"]] == ["code_read"]
    assert last["usage"]["in"] == 1234 and last["usage"]["rounds"] == 3

    entry = rn.read_journal(10)[-1]
    assert entry["trace"].endswith("traces/1.jsonl")
    assert entry["tools"] == ["code_read"] and entry["usage"]["out"] == 56
    assert "结论先给" in entry["out_tail"], "journal 的 out_tail 应仍是正文，不是 JSON 事件"


def test_parse_events_survives_non_json_noise():
    """worker 混进非 JSON 行（崩栈/告警）不能把整轮解析搞崩。"""
    events, text = rn.parse_events(
        '{"type":"StreamDelta","text":"甲"}\n'
        'Traceback (most recent call last):\n'
        '{"type":"ToolCallStart","tool_name":"x","arguments":"{}"}\n'
        '{"type":"StreamDelta","text":"乙"}\n')
    assert [e["type"] for e in events] == ["StreamDelta", "ToolCallStart", "StreamDelta"]
    assert text.startswith("甲乙") and "Traceback" in text


def test_snapshot_rounds_are_drillable(tmp_path, monkeypatch):
    """详情页要能点进单轮：rounds 带轮次号，trace 存在性可判。"""
    run_dir = tmp_path
    monkeypatch.setattr(sv, "RUN_DIR", run_dir)
    monkeypatch.setattr(sv, "STATE", run_dir / "state.json")
    monkeypatch.setattr(sv, "JOURNAL", run_dir / "journal.jsonl")
    monkeypatch.setattr(sv, "TRACES", run_dir / "traces")
    monkeypatch.setattr(sv, "HEARTBEAT", run_dir / "heartbeat")
    monkeypatch.setattr(sv, "STOP", run_dir / "STOP")
    monkeypatch.setattr(sv, "LOCK", run_dir / "runner.lock")
    (run_dir / "state.json").write_text(json.dumps({"cycle": 2, "last_goal": "demo"}))
    (run_dir / "journal.jsonl").write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in [
        {"t": 1, "kind": "run", "cycle": 1, "goal": "demo", "exit": 0, "dur": 12.0,
         "progressed": False, "tools": ["code_read"], "tool_count": 1,
         "usage": {"in": 10, "out": 2, "rounds": 1}, "reason": "台账没变（上一轮没落盘接力棒）"},
        {"t": 2, "kind": "run", "cycle": 2, "goal": "demo", "exit": 0, "dur": 30.0,
         "progressed": True, "tools": [], "tool_count": 0, "usage": {}},
    ]) + "\n", encoding="utf-8")
    (run_dir / "traces").mkdir()
    (run_dir / "traces" / "2.jsonl").write_text(
        json.dumps({"type": "prompt", "cycle": 2, "goal": "demo", "chars": 9}) + "\n"
        + json.dumps({"type": "exit", "cycle": 2, "exit": 0, "progressed": True}) + "\n",
        encoding="utf-8")

    s = sv.snapshot(full=True)
    rounds = s["extra"]["rounds"]
    assert [r["cycle"] for r in rounds] == [1, 2]
    assert rounds[0]["trace"] is False and rounds[1]["trace"] is True
    assert rounds[1]["tools"] == [] and rounds[0]["tools"] == ["code_read"]
    assert s["extra"]["trace_cycles"] == [2]

    d = sv.read_trace(2)
    assert d["ok"] and d["lines"] == 2 and d["events"][0]["type"] == "prompt"
    missing = sv.read_trace(99)
    assert missing["ok"] is False and missing["events"] == []


def test_api_longrun_trace_endpoint(client):
    """下钻接口：轮次存在给事件流，不存在也返回 200 + ok=False（不是 404）。"""
    r = client.get("/api/longrun/trace/999999")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False and body["events"] == [] and "trace" in body["error"]


# ---------- 进行中轮次：journal 还没落盘时的实时视图 ----------

def _patch_paths(monkeypatch, run_dir: Path) -> None:
    for name, rel in (("RUN_DIR", ""), ("STATE", "state.json"), ("JOURNAL", "journal.jsonl"),
                      ("TRACES", "traces"), ("HEARTBEAT", "heartbeat"),
                      ("STOP", "STOP"), ("LOCK", "runner.lock")):
        monkeypatch.setattr(sv, name, (run_dir / rel) if rel else run_dir)


def _fake_running_round(run_dir: Path, cycle: int = 7, age: float = 5.0) -> None:
    """造一个「正在跑」的现场：心跳新鲜、trace 还在写、journal 里没有这一轮。"""
    run_dir.mkdir(parents=True, exist_ok=True)
    now = time.time()
    (run_dir / "state.json").write_text(json.dumps({"cycle": cycle - 1, "last_goal": "demo"}))
    (run_dir / "journal.jsonl").write_text(
        json.dumps({"t": now - 600, "kind": "run", "cycle": cycle - 1, "goal": "demo",
                    "exit": 0, "dur": 12.0, "progressed": True}) + "\n", encoding="utf-8")
    (run_dir / "traces").mkdir(exist_ok=True)
    events = [{"type": "prompt", "t": now - 120, "cycle": cycle, "goal": "biz-negotiate", "chars": 9},
              {"type": "ToolCallStart", "tool_name": "code_read", "arguments": {"file": "a.py"}},
              {"type": "ToolCallStart", "tool_name": "shell_run", "arguments": {"command": "ls -la"}}]
    (run_dir / "traces" / f"{cycle}.jsonl").write_text(
        "\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")
    hb = run_dir / "heartbeat"
    hb.write_text("x")
    os.utime(hb, (now - age, now - age))


def test_live_cycle_reports_running_round(tmp_path, monkeypatch):
    """一轮跑 20 分钟还没落盘时，任务中心必须看得见它 —— 否则那一栏僵在旧时间戳上。"""
    _patch_paths(monkeypatch, tmp_path)
    _fake_running_round(tmp_path, cycle=7)

    lv = sv.live_cycle()
    assert lv and lv["cycle"] == 7
    assert lv["goal"] == "biz-negotiate"
    assert lv["calls"] == 2
    assert lv["last"]["name"] == "shell_run" and "ls -la" in lv["last"]["hint"]


def test_live_cycle_skips_landed_round(tmp_path, monkeypatch):
    """已落盘的轮次不算进行中：刚收工那一秒不该报成「正在跑」。"""
    _patch_paths(monkeypatch, tmp_path)
    _fake_running_round(tmp_path, cycle=7)
    with open(tmp_path / "journal.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps({"t": time.time(), "kind": "run", "cycle": 7, "goal": "biz-negotiate",
                            "exit": 0, "dur": 60.0, "progressed": False}) + "\n")

    assert sv.live_cycle() == {}


def test_live_cycle_blank_when_heartbeat_stale(tmp_path, monkeypatch):
    """心跳停更（进程死了/卡死）时不该硬说在跑。"""
    _patch_paths(monkeypatch, tmp_path)
    _fake_running_round(tmp_path, cycle=7, age=400.0)

    assert sv.live_cycle() == {}


def test_snapshot_title_and_steps_carry_live_round(tmp_path, monkeypatch):
    """列表卡片：标题带「进行中（已跑 X分Y秒）」，首条步骤是实时进度。"""
    _patch_paths(monkeypatch, tmp_path)
    _fake_running_round(tmp_path, cycle=7)
    (tmp_path / "runner.lock").write_text(str(os.getpid()))
    monkeypatch.setattr(sv, "_proc_alive", lambda pid: pid == os.getpid())
    monkeypatch.setattr(sv, "unit_state", lambda refresh=False: "active")

    s = sv.snapshot(full=False)
    assert "第 7 轮进行中" in s["title"] and "已跑" in s["title"]
    assert s["steps"][0].startswith("▶ 第 7 轮进行中")
    assert (s["extra"].get("live") or {}).get("cycle") == 7
