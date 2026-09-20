#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""确认卡审计的契约测试。

要回答的问题不是「闸门拦了多少次」，而是「我什么时候手滑放过了危险命令」——
所以每条确认决定（工具/参数/原因/决定/时间）都必须留痕，且刷新、重启后还在。

契约：
- 弹卡即留痕（pending），决定后原地改写（allow/always/deny），不新增一条；
- always 记下 perm_key：能回答「这条永久白名单是哪次手滑加的」；
- 上限 50 条丢最旧；落盘原子写、目录不存在自动建、损坏配置容错；
- 审计是旁路：record/mark 抛异常绝不影响弹卡与放行判定。
"""
import asyncio
import json
import sys
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

import gate_audit  # noqa: E402
import tool_gate  # noqa: E402
import agent  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """审计与白名单都隔离到临时文件：单测绝不碰线上 data/ 与 settings.json。"""
    audit_file = tmp_path / "gate_audit.json"
    monkeypatch.setattr(gate_audit, "AUDIT_FILE", audit_file)
    monkeypatch.setattr(gate_audit, "_ENTRIES", [])
    monkeypatch.setattr(gate_audit, "_LOADED", False)
    monkeypatch.setattr(tool_gate, "_SETTINGS_PATH", tmp_path / "settings.json")
    return audit_file


# ---------- 流水本身 ----------

def test_record_留痕为待决(_isolate):
    gate_audit.record("tool_gate:abc", "todo_delete", {"task_id": "t1"}, "不可逆操作")
    items = gate_audit.list_audit()
    assert len(items) == 1
    it = items[0]
    assert it["decision"] == "pending"
    assert "等你决定" in it["desc"] and "todo_delete" in it["desc"]
    assert it["args"] == '{"task_id": "t1"}'
    assert it["reason"] == "不可逆操作"
    assert it["ts"] > 0 and it["ts_done"] == 0


def test_mark_允许一次(_isolate):
    gate_audit.record("r1", "shell_run", {"command": "rm -rf /x"}, "危险命令")
    assert gate_audit.mark("r1", "allow") is True
    it = gate_audit.list_audit()[0]
    assert it["decision"] == "allow"
    assert "允许一次" in it["desc"]
    assert it["ts_done"] > 0


def test_mark_总是允许带永久键(_isolate):
    gate_audit.record("r1", "todo_delete", {}, "高危")
    gate_audit.mark("r1", "always", "tool:todo_delete")
    it = gate_audit.list_audit()[0]
    assert it["decision"] == "always"
    assert it["perm_key"] == "tool:todo_delete"   # 能回溯这条白名单是哪次手滑加的
    assert "总是允许" in it["desc"] and "永久白名单" in it["desc"]


def test_mark_拒绝(_isolate):
    gate_audit.record("r1", "wt_discard", {}, "高危")
    gate_audit.mark("r1", "deny")
    assert "拒绝" in gate_audit.list_audit()[0]["desc"]


def test_mark_未知rid返回False(_isolate):
    assert gate_audit.mark("nope", "allow") is False


def test_决定不新增条目_原地改写(_isolate):
    gate_audit.record("r1", "todo_delete", {}, "高危")
    gate_audit.mark("r1", "allow")
    assert len(gate_audit.list_audit()) == 1


def test_上限50_丢最旧(_isolate):
    for i in range(60):
        gate_audit.record(f"r{i}", "todo_delete", {}, "r")
    items = gate_audit.list_audit(50)
    assert len(items) == 50
    assert items[0]["id"] == "r59"    # 新的在前
    assert items[-1]["id"] == "r10"   # 最旧 10 条被丢


def test_倒序_新的在前(_isolate):
    for i in range(3):
        gate_audit.record(f"r{i}", "t", {}, "x")
    assert [i["id"] for i in gate_audit.list_audit()] == ["r2", "r1", "r0"]


def test_limit非法值回退(_isolate):
    gate_audit.record("r1", "t", {}, "x")
    assert len(gate_audit.list_audit("abc")) == 1
    assert len(gate_audit.list_audit(0)) == 1


def test_落盘后重启仍可读回(_isolate):
    gate_audit.record("r1", "todo_delete", {"task_id": "t"}, "高危")
    gate_audit.mark("r1", "allow")
    gate_audit._ENTRIES.clear()   # 模拟进程重启：内存态清空
    gate_audit._LOADED = False
    items = gate_audit.list_audit()
    assert len(items) == 1 and items[0]["decision"] == "allow"


def test_原子写_不留tmp(_isolate):
    gate_audit.record("r1", "t", {}, "x")
    assert _isolate.exists()
    assert not Path(str(_isolate) + ".tmp").exists()


def test_目录不存在时自动建(tmp_path, monkeypatch):
    monkeypatch.setattr(gate_audit, "AUDIT_FILE", tmp_path / "deep" / "gate_audit.json")
    monkeypatch.setattr(gate_audit, "_ENTRIES", [])
    monkeypatch.setattr(gate_audit, "_LOADED", False)
    gate_audit.record("r1", "t", {}, "x")
    assert (tmp_path / "deep" / "gate_audit.json").exists()


def test_损坏配置容错(_isolate):
    _isolate.write_text("{ not json", encoding="utf-8")
    assert gate_audit.list_audit() == []
    assert gate_audit.mark("r1", "allow") is False   # 不炸


def test_参数预览截断(_isolate):
    gate_audit.record("r1", "shell_run", {"command": "x" * 500}, "r")
    assert len(gate_audit.list_audit()[0]["args"]) == gate_audit.ARGS_PREVIEW


def test_非dict参数容错(_isolate):
    gate_audit.record("r1", "shell_run", "raw string", "r")
    assert gate_audit.list_audit()[0]["args"] == "raw string"


def test_clear清空并落盘(_isolate):
    gate_audit.record("r1", "t", {}, "x")
    assert gate_audit.clear() == 1
    assert gate_audit.list_audit() == []
    assert json.loads(_isolate.read_text(encoding="utf-8")) == []


# ---------- 闸门集成（轻量假实例，不跑 initialize） ----------

class _FakeAgent(agent.AIAgent):
    """只带闸门状态的轻量替身：继承真实类拿方法表，不跑 __init__。"""

    def __init__(self):
        self._gate_allowed = {}
        self._gate_denied = {}
        self._gate_pending = {}
        self._gate_card_args = {}
        self._gate_permanent = {}


def _gate(fake, tool, args):
    return asyncio.run(agent.AIAgent._gate_request(fake, tool, args))


def test_弹卡即留痕(_isolate):
    fake = _FakeAgent()
    _gate(fake, "todo_delete", {"task_id": "t1"})
    items = gate_audit.list_audit()
    assert len(items) == 1
    assert items[0]["decision"] == "pending"
    assert items[0]["tool"] == "todo_delete"
    assert items[0]["id"].startswith("tool_gate:")


def test_同签名重调_不重复留痕(_isolate):
    fake = _FakeAgent()
    _gate(fake, "todo_delete", {"task_id": "t1"})
    _gate(fake, "todo_delete", {"task_id": "t1"})
    assert len(gate_audit.list_audit()) == 1


def test_允许后审计变allow并带perm_key(_isolate):
    fake = _FakeAgent()
    _gate(fake, "todo_delete", {"task_id": "t1"})
    rid = next(iter(fake._gate_pending.values()))
    assert agent.AIAgent.resolve_gate(fake, rid, True) is True
    it = gate_audit.list_audit()[0]
    assert it["decision"] == "allow"
    assert it["perm_key"] == "tool:todo_delete"   # 高危工具整桶


def test_拒绝后审计变deny(_isolate):
    fake = _FakeAgent()
    _gate(fake, "todo_delete", {"task_id": "t1"})
    rid = next(iter(fake._gate_pending.values()))
    agent.AIAgent.resolve_gate(fake, rid, False)
    assert gate_audit.list_audit()[0]["decision"] == "deny"


def test_总是允许审计标always(_isolate):
    fake = _FakeAgent()
    _gate(fake, "shell_run", {"command": "rm -rf /tmp/x"})
    rid = next(iter(fake._gate_pending.values()))
    agent.AIAgent.resolve_gate(fake, rid, True, always=True)
    it = gate_audit.list_audit()[0]
    assert it["decision"] == "always"
    assert it["perm_key"].startswith("shell:")


def test_审计写入失败_不影响放行(_isolate, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(gate_audit, "mark", boom)
    fake = _FakeAgent()
    _gate(fake, "todo_delete", {"task_id": "t1"})
    rid = next(iter(fake._gate_pending.values()))
    assert agent.AIAgent.resolve_gate(fake, rid, True) is True   # 审计坏了也照样放行


def test_审计登记失败_弹卡不炸(_isolate, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(gate_audit, "record", boom)
    fake = _FakeAgent()
    out = _gate(fake, "todo_delete", {"task_id": "t1"})
    assert "确认" in out and fake._gate_pending


# ---------- 端点 ----------

def test_端点返回审计清单(_isolate):
    import server
    gate_audit.record("r1", "todo_delete", {"task_id": "t"}, "高危")
    gate_audit.mark("r1", "always", "tool:todo_delete")
    data = asyncio.run(server.gate_audit_list(limit=5))
    assert data["ok"] is True
    assert len(data["items"]) == 1
    assert data["items"][0]["perm_key"] == "tool:todo_delete"


# ---------- 重启清账：点不动的 pending 卡不能继续显示「等你决定」 ----------

def test_清账_未决卡标为失效(_isolate):
    gate_audit.record("tool_gate:a", "shell_run", {"command": "rm -rf /x"}, "不可逆操作")
    assert gate_audit.expire_stale() == 1
    it = gate_audit.list_audit()[0]
    assert it["decision"] == "expired"
    assert it["ts_done"] > 0
    assert "失效" in it["desc"] and "shell_run" in it["desc"]


def test_清账_已决定的记录不动(_isolate):
    gate_audit.record("tool_gate:allow1", "todo_delete", {"task_id": "t1"}, "不可逆")
    gate_audit.record("tool_gate:deny1", "todo_delete", {"task_id": "t2"}, "不可逆")
    gate_audit.record("tool_gate:pend1", "shell_run", {"command": "git push -f"}, "强制推送")
    gate_audit.mark("tool_gate:allow1", "allow")
    gate_audit.mark("tool_gate:deny1", "deny")
    assert gate_audit.expire_stale() == 1
    got = {e["id"]: e["decision"] for e in gate_audit.list_audit()}
    assert got == {"tool_gate:allow1": "allow", "tool_gate:deny1": "deny", "tool_gate:pend1": "expired"}


def test_清账_无未决返回0(_isolate):
    gate_audit.record("tool_gate:a", "shell_run", {}, "x")
    gate_audit.mark("tool_gate:a", "deny")
    assert gate_audit.expire_stale() == 0
    assert gate_audit.expire_stale() == 0


def test_清账_落盘且重启后仍是失效(_isolate):
    """清账必须落盘：否则下次启动读回旧文件，又是「等你决定」。"""
    gate_audit.record("tool_gate:a", "shell_run", {"command": "rm -rf /x"}, "不可逆")
    gate_audit.expire_stale()
    raw = json.loads(_isolate.read_text(encoding="utf-8"))
    assert raw[0]["decision"] == "expired"
    gate_audit._ENTRIES = []
    gate_audit._LOADED = False
    assert gate_audit.list_audit()[0]["decision"] == "expired"


def test_清账_不影响后续新卡(_isolate):
    """清账只扫历史，新弹的卡照旧是 pending——否则刚弹就被误标失效。"""
    gate_audit.record("tool_gate:old", "shell_run", {}, "x")
    gate_audit.expire_stale()
    gate_audit.record("tool_gate:new", "shell_run", {}, "x")
    got = {e["id"]: e["decision"] for e in gate_audit.list_audit()}
    assert got["tool_gate:old"] == "expired" and got["tool_gate:new"] == "pending"
