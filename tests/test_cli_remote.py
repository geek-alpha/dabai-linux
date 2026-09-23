# -*- coding: utf-8 -*-
"""CLI 接入模式：让「外部调用」与「网页输入框」真的是同一条路。

服务端与前端的行为用源码守卫锁住——它们一被删，网页端就会静默地不再显示
外部注入的消息，而这种失效没有任何报错，只会让人觉得「功能没生效」。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import dabai_cli as cli  # noqa: E402


def _args(**kw) -> argparse.Namespace:
    base = dict(user=None, namespace="", json=False, local=False, remote=False, port=0)
    base.update(kw)
    return argparse.Namespace(**base)


def test_pick_mode_defaults_to_auto():
    assert cli._pick_mode(_args()) == "auto"


def test_json_and_namespace_stay_local():
    """长跑引擎按 --json 的事件格式解析，接进服务会拿到另一种格式；
    --namespace 要的是独立会话，接进服务会污染网页那条对话线。"""
    assert cli._pick_mode(_args(json=True)) == "local"
    assert cli._pick_mode(_args(namespace="longrun")) == "local"


def test_explicit_flags_win():
    assert cli._pick_mode(_args(local=True, json=True)) == "local"
    assert cli._pick_mode(_args(remote=True)) == "remote"


def test_resolve_uid_remote_follows_web_identity():
    """接入模式必须用网页那个 uid：否则事件广播不到网页，用的也不是同一个 agent。"""
    assert cli._resolve_uid(_args(), "remote") == cli._unified_uid()
    assert cli._resolve_uid(_args(), "local") == "cli"
    assert cli._resolve_uid(_args(user="alice"), "remote") == "alice"


def test_unified_uid_is_admin():
    """统一身份不在用户表里 → 系统身份 = 管理员（与网页局域网直连同口径）。"""
    import sandbox

    assert sandbox.actor_for(cli._unified_uid()).is_admin


def test_arg_hint_accepts_dict_and_json_string():
    """两种事件源的参数类型不同（本地=JSON 串，接入=dict），摘要得一样。"""
    assert cli._arg_hint({"path": "a/b.py"}) == "a/b.py"
    assert cli._arg_hint('{"path": "a/b.py"}') == "a/b.py"
    assert cli._arg_hint("") == ""
    assert cli._arg_hint("not json") == "not json"


def test_printer_renders_ws_events(capsys):
    p = cli._TurnPrinter()
    p.handle_ws({"type": "thinking", "session_id": "s1"})
    p.handle_ws({"type": "tool_call_start", "tool_name": "read_file",
                 "arguments": {"path": "x.py"}})
    p.handle_ws({"type": "tool_call_result", "tool_name": "read_file",
                 "result": "ok", "success": True})
    p.handle_ws({"type": "stream_text", "text": "你好"})
    p.finish()
    out = capsys.readouterr().out
    assert "read_file" in out and "x.py" in out
    assert "你好" in out


def test_server_broadcasts_external_user_message():
    """源码守卫：external 注入要回显给网页端，headless 连接不许抢汇报通道。"""
    src = (BASE / "server.py").read_text("utf-8")
    assert "_broadcast_user_message(" in src
    assert 'msg.get("external") is True' in src
    assert "_front_conn" in src
    assert 'msg.get("headless") is True' in src


def test_frontend_handles_user_message():
    """服务端推了、前端没人渲染，等于没做。"""
    net = (BASE / "web" / "js" / "network" / "09_websocket.ts").read_text("utf-8")
    assert "case 'user_message':" in net
    assert "App.addUserMsg(msg.text || '', false, msg.atts)" in net
    proto = (BASE / "web" / "js" / "types" / "ws-protocol.ts").read_text("utf-8")
    assert "'user_message'" in proto
