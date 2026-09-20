#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""确认卡跨实例落定的契约测试。

线上真实结构：卡片由会话实例（server._shared_agents[user_id]）弹出，
确认请求由 HTTP 端点处理。端点只拿得到 rid，若自己去 get_agent() 拿实例，
拿到的是另一个对象 → 永远「确认请求不存在或已过期」（f4e6c29 起的真故障）。

契约：
- 弹卡即登记持有者，按 rid 能反查到持卡实例；
- 解决后注销；陈旧卡片（实例已重置）返回 False 且不留残渣；
- 多实例共存不串台：按 rid 只动自己那张卡；
- /api/bridge/confirm 的 tool_gate 分支走按 rid 反查，不再自己取实例。
"""
import asyncio
import sys
from pathlib import Path

import pytest
from fastapi import HTTPException

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

import agent  # noqa: E402
import gate_audit  # noqa: E402
import tool_gate  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(gate_audit, "AUDIT_FILE", tmp_path / "gate_audit.json")
    monkeypatch.setattr(gate_audit, "_ENTRIES", [])
    monkeypatch.setattr(gate_audit, "_LOADED", False)
    monkeypatch.setattr(tool_gate, "_SETTINGS_PATH", tmp_path / "settings.json")
    monkeypatch.setattr(agent, "_GATE_OWNERS", {})
    return tmp_path


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


def _card(fake, tool="todo_delete", args=None):
    """弹一张卡并返回 rid。"""
    _gate(fake, tool, args if args is not None else {"task_id": "t1"})
    return next(iter(fake._gate_pending.values()))


# ---------- 登记表本身 ----------

def test_弹卡即登记持有者():
    a = _FakeAgent()
    rid = _card(a)
    assert agent._GATE_OWNERS[rid] is a


def test_按rid落定到持卡实例():
    a = _FakeAgent()
    rid = _card(a)
    assert agent.resolve_gate_anywhere(rid, True) is True
    assert a._gate_pending == {}          # 卡片已收尾
    assert len(a._gate_allowed) == 1      # 放行写进了持卡实例
    assert rid not in agent._GATE_OWNERS  # 解决即注销


def test_拒绝也落定到持卡实例():
    a = _FakeAgent()
    rid = _card(a)
    assert agent.resolve_gate_anywhere(rid, False) is True
    assert len(a._gate_denied) == 1
    assert a._gate_allowed == {}


def test_未知rid返回False():
    assert agent.resolve_gate_anywhere("tool_gate:不存在", True) is False


def test_解决后不能重复落定():
    a = _FakeAgent()
    rid = _card(a)
    assert agent.resolve_gate_anywhere(rid, True) is True
    assert agent.resolve_gate_anywhere(rid, True) is False


def test_陈旧卡片不留残渣():
    """实例被重置（重启）后卡片已失效：返回 False，登记表清干净。"""
    a = _FakeAgent()
    rid = _card(a)
    a._gate_pending.clear()
    assert agent.resolve_gate_anywhere(rid, True) is False
    assert rid not in agent._GATE_OWNERS


def test_实例抛异常时不炸且不留残渣(monkeypatch):
    a = _FakeAgent()
    rid = _card(a)

    def boom(*args, **kwargs):
        raise RuntimeError("instance dead")

    monkeypatch.setattr(a, "resolve_gate", boom)
    assert agent.resolve_gate_anywhere(rid, True) is False
    assert rid not in agent._GATE_OWNERS


def test_多实例不串台():
    a, b = _FakeAgent(), _FakeAgent()
    rid_a = _card(a, args={"task_id": "a"})
    rid_b = _card(b, args={"task_id": "b"})
    assert agent.resolve_gate_anywhere(rid_a, True) is True
    assert a._gate_allowed and b._gate_allowed == {}
    assert rid_b in b._gate_pending.values()      # b 那张卡还没被动
    assert agent._GATE_OWNERS.get(rid_b) is b


def test_登记表上限清空防堆积():
    a = _FakeAgent()
    for i in range(agent._GATE_OWNERS_MAX):
        agent._register_gate_owner(f"tool_gate:{i}", a)
    agent._register_gate_owner("tool_gate:last", a)
    assert len(agent._GATE_OWNERS) == 1
    assert agent._GATE_OWNERS["tool_gate:last"] is a


# ---------- 端点契约 ----------

def _confirm(request_id, approve=True, always=False):
    import server
    return asyncio.run(server.harness_bridge_confirm(
        {"request_id": request_id, "approve": approve, "always": always}))


def test_端点按rid反查_允许返回running(monkeypatch):
    import server
    seen = {}

    def fake_resolve(rid, approve, always=False):
        seen.update(rid=rid, approve=approve, always=always)
        return True

    monkeypatch.setattr(agent, "resolve_gate_anywhere", fake_resolve)
    out = _confirm("tool_gate:abc", approve=True)
    assert out["ok"] is True and out["status"] == "running"
    assert seen == {"rid": "tool_gate:abc", "approve": True, "always": False}


def test_端点拒绝返回cancelled(monkeypatch):
    monkeypatch.setattr(agent, "resolve_gate_anywhere", lambda *a, **k: True)
    out = _confirm("tool_gate:abc", approve=False)
    assert out["status"] == "cancelled"


def test_端点未命中返回404(monkeypatch):
    monkeypatch.setattr(agent, "resolve_gate_anywhere", lambda *a, **k: False)
    with pytest.raises(HTTPException) as ei:
        _confirm("tool_gate:过期了")
    assert ei.value.status_code == 404


def test_端点走真实登记表_端到端不落空():
    """不 mock：真弹卡 → 真端点 → 真落定。这就是线上那条路径。"""
    a = _FakeAgent()
    rid = _card(a)
    out = _confirm(rid, approve=False)
    assert out["status"] == "cancelled"
    assert len(a._gate_denied) == 1
    assert gate_audit.list_audit()[0]["decision"] == "deny"


def test_端点不再自己取实例():
    """防回归：端点若又改成 get_agent()，跨实例会再次 404。"""
    import server
    src = Path(server.__file__).read_text(encoding="utf-8")
    i = src.index('if request_id.startswith("tool_gate:")')
    block = src[i:i + 700]
    code = "\n".join(ln for ln in block.splitlines() if not ln.strip().startswith("#"))
    assert "resolve_gate_anywhere" in code
    assert "get_agent" not in code
