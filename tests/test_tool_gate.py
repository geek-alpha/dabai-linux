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
import gate_audit  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate_gate_audit(tmp_path, monkeypatch):
    """闸门测试会触发审计留痕：隔离到临时文件，别写进线上 data/gate_audit.json。"""
    monkeypatch.setattr(gate_audit, "AUDIT_FILE", tmp_path / "gate_audit.json")
    monkeypatch.setattr(gate_audit, "_ENTRIES", [])
    monkeypatch.setattr(gate_audit, "_LOADED", False)


@pytest.fixture(autouse=True)
def _isolate_ledger_quota(monkeypatch):
    """余额是本机状态，不是闸门契约：余额见底时 heavy_block 把烧钱大户判 deny，
    「高危工具该 ask」就会随机器余额变红。低配额拦截由 test_ledger_quota 专测。"""
    import peer_ledger
    monkeypatch.setattr(peer_ledger, "heavy_block", lambda tool_name: "")


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
        self._gate_card_args = {}
        self._gate_permanent = {}


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
# ---------- 永久白名单（『总是允许』第三选项） ----------

def test_perm_key_高危工具按工具名分桶():
    assert tool_gate.perm_key("todo_delete", {"task_id": "t1"}) == "tool:todo_delete"
    assert tool_gate.perm_key("todo_delete", {"task_id": "t2"}) == "tool:todo_delete"


def test_perm_key_shell按危险模式分桶():
    k1 = tool_gate.perm_key("shell_run", {"command": "rm -rf /tmp/x"})
    k2 = tool_gate.perm_key("shell_run", {"command": "git push -f origin main"})
    assert k1.startswith("shell:") and k2.startswith("shell:")
    assert k1 != k2  # 不同危险模式 → 不同桶，精确放行


def test_perm_key_overwrite参数单独分桶():
    assert tool_gate.perm_key("code_create_file", {"path": "a.py", "overwrite": True}) == \
        "overwrite:code_create_file"


def test_perm_key_普通工具无桶():
    assert tool_gate.perm_key("code_edit", {"file": "a.py"}) == ""


def test_永久白名单命中_直接放行():
    permanent = {"tool:todo_delete": 1}
    v, _, _ = evaluate("todo_delete", {"task_id": "t1"}, {}, {}, {}, permanent)
    assert v == "allow"


def test_永久白名单未命中_仍ask():
    permanent = {"tool:sched_remove": 1}
    v, _, _ = evaluate("todo_delete", {"task_id": "t1"}, {}, {}, {}, permanent)
    assert v == "ask"


def test_永久白名单_拒绝过的签名仍优先_用户可反悔():
    # 用户永久允许了 todo_delete 整类，但这次具体签名被拒绝过 → deny 优先
    denied = {signature("todo_delete", {"task_id": "t1"}): 1}
    permanent = {"tool:todo_delete": 1}
    v, _, _ = evaluate("todo_delete", {"task_id": "t1"}, {}, denied, {}, permanent)
    assert v == "deny"


# ---------- 读写 settings.json ----------

def test_save_load_roundtrip(monkeypatch, tmp_path):
    fake_settings = tmp_path / "settings.json"
    fake_settings.write_text('{"llm_providers": []}', encoding="utf-8")
    monkeypatch.setattr(tool_gate, "_SETTINGS_PATH", fake_settings)

    assert tool_gate.save_permanent("tool:todo_delete", "todo_delete", "删除任务不可逆")
    loaded = tool_gate.load_permanent()
    assert "tool:todo_delete" in loaded
    assert loaded["tool:todo_delete"]["tool"] == "todo_delete"
    # 原有配置保留（读改写，不整体覆盖）
    import json
    cfg = json.loads(fake_settings.read_text(encoding="utf-8"))
    assert "llm_providers" in cfg


def test_load_损坏文件返回空(monkeypatch, tmp_path):
    fake_settings = tmp_path / "settings.json"
    fake_settings.write_text("{broken", encoding="utf-8")
    monkeypatch.setattr(tool_gate, "_SETTINGS_PATH", fake_settings)
    assert tool_gate.load_permanent() == {}


def test_load_无tool_gate段返回空(monkeypatch, tmp_path):
    fake_settings = tmp_path / "settings.json"
    fake_settings.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(tool_gate, "_SETTINGS_PATH", fake_settings)
    assert tool_gate.load_permanent() == {}


# ---------- resolve_gate always 分支（agent 侧） ----------

def test_resolve_gate_always_落盘并永久放行(monkeypatch, tmp_path):
    fake_settings = tmp_path / "settings.json"
    fake_settings.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(tool_gate, "_SETTINGS_PATH", fake_settings)

    fake = _FakeAgent()
    fake._gate_permanent = {}  # 模拟启动时 load_permanent 为空
    _gate(fake, "todo_delete", {"task_id": "t1"})
    rid = next(iter(fake._gate_pending.values()))

    assert agent.AIAgent.resolve_gate(fake, rid, True, always=True) is True
    # 永久白名单已落盘
    assert "tool:todo_delete" in tool_gate.load_permanent()
    assert "tool:todo_delete" in fake._gate_permanent
    # 同类不同参数也不再弹卡（不是签名级放行，是整类放行）
    assert _gate(fake, "todo_delete", {"task_id": "t2"}) is None


def test_resolve_gate_always_拒绝仍只进黑名单(monkeypatch, tmp_path):
    fake_settings = tmp_path / "settings.json"
    fake_settings.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(tool_gate, "_SETTINGS_PATH", fake_settings)

    fake = _FakeAgent()
    fake._gate_permanent = {}
    _gate(fake, "todo_delete", {"task_id": "t1"})
    rid = next(iter(fake._gate_pending.values()))
    agent.AIAgent.resolve_gate(fake, rid, False)
    assert tool_gate.load_permanent() == {}  # 拒绝不落盘


def test_resolve_gate_无弹卡参数_不落盘但允许本次():
    fake = _FakeAgent()  # 无 _gate_card_args（旧实例/异常路径）
    if not hasattr(fake, "_gate_card_args"):
        fake._gate_card_args = {}
    fake._gate_permanent = {}
    sig = signature("todo_delete", {"task_id": "t1"})
    fake._gate_pending[sig] = "tool_gate:xyz"
    assert agent.AIAgent.resolve_gate(fake, "tool_gate:xyz", True, always=True) is True
    assert fake._gate_permanent == {}  # 落盘失败不影响本次放行


def test_弹卡事件带always标记(monkeypatch):
    calls = []

    async def fake_broadcast(ev):
        calls.append(ev)

    monkeypatch.setattr(agent, "_gate_broadcast", fake_broadcast)
    fake = _FakeAgent()
    _gate(fake, "todo_delete", {"task_id": "t1"})
    assert calls[0].get("always_opt") is True  # 前端据此显示『总是允许』按钮
# ---------- 永久白名单清单/撤销（管理面板：/api/bridge/gate-allow） ----------

@pytest.fixture
def perm_settings(tmp_path, monkeypatch):
    """把永久白名单读写隔离到临时文件，绝不碰真实的 settings.json。"""
    monkeypatch.setattr(tool_gate, "_SETTINGS_PATH", tmp_path / "settings.json")
    return tmp_path / "settings.json"


def test_list_空配置返回空列表(perm_settings):
    assert tool_gate.list_permanent() == []


def test_save后_list带描述与时间(perm_settings):
    assert tool_gate.save_permanent("tool:send_message", "send_message", "用户点了总是允许")
    items = tool_gate.list_permanent()
    assert len(items) == 1
    it = items[0]
    assert it["key"] == "tool:send_message"
    assert it["tool"] == "send_message"
    assert "send_message" in it["desc"] and "不再询问" in it["desc"]
    assert it["at"] > 0


def test_list_按加入时间倒序(perm_settings):
    # at 是秒级时间戳，直接用不同 at 构造，验证降序契约
    import json as _json
    perm_settings.write_text(_json.dumps({"tool_gate": {"permanent_allow": {
        "tool:a": {"tool": "a", "reason": "r", "at": 100},
        "tool:b": {"tool": "b", "reason": "r", "at": 200},
    }}}), encoding="utf-8")
    keys = [i["key"] for i in tool_gate.list_permanent()]
    assert keys == ["tool:b", "tool:a"]  # at 大的排前面


def test_remove_撤销成功(perm_settings):
    tool_gate.save_permanent("tool:a", "a", "r")
    assert tool_gate.remove_permanent("tool:a") is True
    assert tool_gate.list_permanent() == []


def test_remove_不存在的key返回False(perm_settings):
    assert tool_gate.remove_permanent("tool:ghost") is False


def test_remove_只删目标保留其它(perm_settings):
    tool_gate.save_permanent("tool:a", "a", "r")
    tool_gate.save_permanent("shell:3", "shell_run", "r")
    assert tool_gate.remove_permanent("tool:a") is True
    assert [i["key"] for i in tool_gate.list_permanent()] == ["shell:3"]


def test_desc_三类键翻译成白话(perm_settings):
    tool_gate.save_permanent("tool:todo_delete", "todo_delete", "r1")
    tool_gate.save_permanent("shell:3", "shell_run", "r2")
    tool_gate.save_permanent("overwrite:code_create_file", "code_create_file", "r3")
    descs = {i["key"]: i["desc"] for i in tool_gate.list_permanent()}
    assert "todo_delete" in descs["tool:todo_delete"] and "不再询问" in descs["tool:todo_delete"]
    assert "第 3 条危险模式" in descs["shell:3"]
    assert "覆盖" in descs["overwrite:code_create_file"]


def test_损坏配置_容错返回(perm_settings):
    perm_settings.write_text("{ not json", encoding="utf-8")
    assert tool_gate.list_permanent() == []
    assert tool_gate.remove_permanent("tool:x") is False


def test_roundtrip_读改写保留其它配置节(perm_settings):
    """save/remove 都是整文件读改写：settings.json 里的其它配置不能丢。"""
    import json as _json
    perm_settings.write_text(_json.dumps({"llm": {"model": "x"}}), encoding="utf-8")
    tool_gate.save_permanent("tool:a", "a", "r")
    cfg1 = _json.loads(perm_settings.read_text(encoding="utf-8"))
    assert cfg1["llm"]["model"] == "x"
    assert list(cfg1["tool_gate"]["permanent_allow"]) == ["tool:a"]
    tool_gate.remove_permanent("tool:a")
    cfg2 = _json.loads(perm_settings.read_text(encoding="utf-8"))
    assert cfg2["llm"]["model"] == "x"
    assert not cfg2["tool_gate"]["permanent_allow"]
