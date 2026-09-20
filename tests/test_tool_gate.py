#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""工具级确认闸门的契约测试。

对标 Anthropic _route_tool_event 的 evaluated_permission 闸门（fail-closed）：
- 只读工具零确认放行（绝不打断自由跑）
- 高危工具/危险参数按「动作签名」挂确认卡，等用户 allow/deny 裁决
- 用户允许过的签名 → 白名单（重调即放行）；拒绝过的 → 黑名单（重调直接拒）
- 评估器故障 → fail-closed：写操作不误跑；agent 侧评估异常 → 放行（闸门不当故障源）
"""
import asyncio
import sys
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

import tool_gate  # noqa: E402
from tool_gate import evaluate, signature  # noqa: E402

import agent  # noqa: E402


# ---------- evaluate 判定规则 ----------

def test_只读工具零确认():
    v, _, _ = evaluate("code_read", {"files": "a.py"}, {}, {}, {})
    assert v == "allow"


def test_常规写操作信任边界内放行():
    v, _, _ = evaluate("code_edit", {"file": "a.py", "old": "x", "new": "y"}, {}, {}, {})
    assert v == "allow"


@pytest.mark.parametrize("tool", [
    "todo_delete", "sched_remove", "wt_discard",
    "delegate_agent_task", "harness_flow_submit", "sched_add",
])
def test_高危工具名_ask(tool):
    v, reason, _ = evaluate(tool, {}, {}, {}, {})
    assert v == "ask"
    assert reason


def test_shell危险命令_ask():
    v, _, _ = evaluate("shell_run", {"command": "rm -rf /tmp/x"}, {}, {}, {})
    assert v == "ask"
    v2, _, _ = evaluate("shell_run", {"command": "git push -f origin main"}, {}, {}, {})
    assert v2 == "ask"
    v3, _, _ = evaluate("shell_run", {"command": "mkfs.ext4 /dev/sdb1"}, {}, {}, {})
    assert v3 == "ask"


def test_shell安全命令_放行():
    v, _, _ = evaluate("shell_run", {"command": "pytest tests -x -q"}, {}, {}, {})
    assert v == "allow"


def test_创建文件带覆盖参数_ask():
    v, _, _ = evaluate("code_create_file", {"path": "x.py", "overwrite": True}, {}, {}, {})
    assert v == "ask"


def test_创建文件不带覆盖_放行():
    v, _, _ = evaluate("code_create_file", {"path": "x.py", "content": "a"}, {}, {}, {})
    assert v == "allow"


def test_黑名单命中_deny():
    sig = signature("shell_run", {"command": "rm -rf /x"})
    v, reason, _ = evaluate("shell_run", {"command": "rm -rf /x"}, {}, {sig: 1}, {})
    assert v == "deny"
    assert "拒绝" in reason


def test_白名单命中_直接放行():
    sig = signature("todo_delete", {"task_id": "t1"})
    v, _, _ = evaluate("todo_delete", {"task_id": "t1"}, {sig: 1}, {}, {})
    assert v == "allow"


def test_待决确认_不重复弹卡():
    sig = signature("todo_delete", {"task_id": "t1"})
    v, reason, _ = evaluate("todo_delete", {"task_id": "t1"}, {}, {}, {sig: "rid1"})
    assert v == "ask"
    assert "等待你决定" in reason


def test_优先级_黑名单压倒一切():
    sig = signature("code_read", {"files": "a.py"})
    # 只读工具被用户拒过 → deny（denied 优先级高于只读放行）
    v, _, _ = evaluate("code_read", {"files": "a.py"}, {}, {sig: 1}, {})
    assert v == "deny"


def test_签名_参数顺序无关():
    a = signature("shell_run", {"command": "ls", "timeout": 5})
    b = signature("shell_run", {"timeout": 5, "command": "ls"})
    assert a == b


def test_签名_不同参数不同签名():
    a = signature("code_edit", {"file": "a.py"})
    b = signature("code_edit", {"file": "b.py"})
    assert a != b


def test_fail_closed_评估器故障不误跑(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("broken")

    monkeypatch.setattr(tool_gate, "_is_readonly", boom)
    v, _, _ = evaluate("code_edit", {"file": "a.py"}, {}, {}, {})
    assert v == "ask"


# ---------- agent 闸门行为（轻量假实例测方法，不 initialize） ----------

class _FakeAgent(agent.AIAgent):
    """只带闸门状态的轻量替身：继承真实类拿方法表，不跑 __init__。"""

    def __init__(self):
        self._gate_allowed = {}
        self._gate_denied = {}
        self._gate_pending = {}


def _gate(fake, tool, args):
    return asyncio.run(agent.AIAgent._gate_request(fake, tool, args))


def test_ask_拦截回填并弹卡(monkeypatch):
    calls = []

    async def fake_broadcast(ev):
        calls.append(ev)

    monkeypatch.setattr(agent, "_gate_broadcast", fake_broadcast)
    fake = _FakeAgent()
    out = _gate(fake, "todo_delete", {"task_id": "t1"})
    assert "确认" in out and "todo_delete" in out
    assert fake._gate_pending  # 已登记待决确认
    assert len(calls) == 1 and calls[0]["type"] == "bridge_confirm"
    assert calls[0]["request_id"].startswith("tool_gate:")


def test_同签名重调_不重复弹卡(monkeypatch):
    calls = []

    async def fake_broadcast(ev):
        calls.append(ev)

    monkeypatch.setattr(agent, "_gate_broadcast", fake_broadcast)
    fake = _FakeAgent()
    _gate(fake, "todo_delete", {"task_id": "t1"})
    out2 = _gate(fake, "todo_delete", {"task_id": "t1"})
    assert "确认" in out2
    assert len(calls) == 1  # 同一待决签名只弹一次卡，不刷屏


def test_用户允许后_重调即放行():
    fake = _FakeAgent()
    _gate(fake, "todo_delete", {"task_id": "t1"})
    rid = next(iter(fake._gate_pending.values()))
    assert agent.AIAgent.resolve_gate(fake, rid, True) is True
    assert _gate(fake, "todo_delete", {"task_id": "t1"}) is None  # 放行执行


def test_用户拒绝后_重调直接拒绝():
    fake = _FakeAgent()
    _gate(fake, "todo_delete", {"task_id": "t1"})
    rid = next(iter(fake._gate_pending.values()))
    agent.AIAgent.resolve_gate(fake, rid, False)
    out = _gate(fake, "todo_delete", {"task_id": "t1"})
    assert "拒绝" in out
    assert "不会再发起" in out


def test_resolve_gate_未知rid_返回False():
    fake = _FakeAgent()
    assert agent.AIAgent.resolve_gate(fake, "tool_gate:nope", True) is False


def test_评估器异常_闸门放行不阻塞(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("broken")

    monkeypatch.setattr(tool_gate, "evaluate", boom)
    fake = _FakeAgent()
    assert _gate(fake, "code_edit", {"file": "a.py"}) is None  # 故障 → 放行


def test_广播钩子未注册_弹卡不炸():
    fake = _FakeAgent()
    out = _gate(fake, "wt_discard", {"confirm": True})
    assert "确认" in out  # 无广播函数也不抛异常
