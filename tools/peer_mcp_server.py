#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""联邦 MCP server —— 把整套联邦收成**一个**工具，且默认不挂载。

第一性原理：联邦之前有 9 个常驻工具，等于每轮对话都在提醒「你可以指派别的机器」。
问题不在能力，在**可及性**。MCP 的形态正好治这个：不 connect 就不占工具表，
要用必须先过 mcp 技能这一道；再过管理员凭证（peer_admin）这一道。

两道门的分工：
  · 客户端（skills/mcp/mcp_client.admin_guard）管「能不能挂载」——凭证到 read 级即可。
  · 本文件（服务端）管「这次动作能不能做」——按 action 判 read/talk/task，
    并强制 why（每次使用都得留下必要性，这是「非必要不使用」唯一能落地的地方）。

只读动作也要求 why：理由不是审查，是**摩擦**——一句话的成本换来「不是随手点的」。

调试：echo '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | python tools/peer_mcp_server.py
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import peer_admin  # noqa: E402

# action -> 所需授权级别
ACTION_LEVEL = {
    "list": "read",
    "state": "read",
    "inbox": "read",
    "board": "read",
    "call": "talk",
    "say": "talk",
    "hangup": "talk",
    "social": "talk",     # social 的写类会再抬到 task，见 _level_of
    "task": "task",
}
# social 里会对外产生副作用的 action（发动态/加友/广播/交换名册）
SOCIAL_WRITE = {"post", "comment", "friend_add", "friend_remove", "discover", "announce"}

PEER_TOOL = {
    "name": "peer",
    "description": (
        "跨机大白联邦（默认关闭，需管理员授权后经 mcp 技能连接）。一次只做一件事，action 取值：\n"
        "list=点名谁在线；state=问一台的状态；inbox=读我的留言；board=读联邦黑板（同伴的事实回报）；\n"
        "call=打电话当场等回话（可多轮续接）；say=异步留言；hangup=挂断；\n"
        "task=派活让对面起子智能体真去执行（代价最大，单独要 task 级授权）；\n"
        "social=社会层，配 social_action：status/friends/feed（只读）或 discover/announce/friend_add/friend_remove/post/comment（有副作用）。\n"
        "why 必填：一句话说明为什么这次必要。"
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "action": {"type": "string",
                       "enum": list(ACTION_LEVEL),
                       "description": "这次要做什么，见工具说明"},
            "why": {"type": "string",
                    "description": "为什么这次必要（写进审计，一句话）"},
            "node": {"type": "string", "description": "目标实例名（rpi / aliyun / wsl）"},
            "text": {"type": "string", "description": "要说的话 / 要派的活"},
            "cid": {"type": "string", "description": "通话编号；一般不传，自动续接未挂断的那通"},
            "wait": {"type": "number", "description": "等回话的秒数（默认 90）"},
            "limit": {"type": "number", "description": "条数上限（inbox/feed）"},
            "keep_unread": {"type": "boolean", "description": "inbox：只读不标已读"},
            "item_id": {"type": "string", "description": "social=comment 时评论哪一条"},
            "social_action": {"type": "string",
                              "description": "social 的子动作：status/discover/announce/friends/friend_add/friend_remove/post/feed/comment"},
        },
        "required": ["action", "why"],
    },
}


def _load_peer_skill():
    """按路径加载 skills/peer/skill.py —— 复用它的 HANDLERS，不复制一份逻辑出来漂移。"""
    path = ROOT / "skills" / "peer" / "skill.py"
    spec = importlib.util.spec_from_file_location("dabai_peer_skill", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _level_of(action: str, social_action: str) -> str:
    if action != "social":
        return ACTION_LEVEL[action]
    return "task" if social_action in SOCIAL_WRITE else "read"


def _call_peer(args: dict) -> str:
    action = str(args.get("action") or "").strip().lower()
    why = str(args.get("why") or "").strip()
    if action not in ACTION_LEVEL:
        return f"action 不认：{'/'.join(ACTION_LEVEL)}"
    if not why:
        return "缺 why：一句话说明这次为什么必要（联邦默认关闭，每次使用都留理由）。"

    social_action = str(args.get("social_action") or "").strip().lower()
    level = _level_of(action, social_action)
    ok, msg = peer_admin.check(level)
    if not ok:
        return (f"管理员未授权（需要 {level} 级）：{msg}\n"
                f"授权：python peer_admin.py grant --scope {level} --ttl 600 --why \"…\"")
    peer_admin._audit("use", action=action, social_action=social_action, level=level,
                      why=why, node=str(args.get("node") or ""))

    skill = _load_peer_skill()
    name = {"list": "peer_list", "state": "peer_state", "inbox": "peer_inbox",
            "board": "peer_board", "call": "peer_call", "say": "peer_say",
            "hangup": "peer_hangup", "task": "peer_task", "social": "peer_social"}[action]
    if action == "social":
        args = dict(args)
        args["action"] = social_action
    try:
        return str(skill.HANDLERS[name](args))
    except Exception as e:  # 联邦的异常不该把 MCP 连接打死
        return f"{action} 执行失败：{type(e).__name__}: {e}"


def _send(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _text(s: str, is_error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": s}], "isError": is_error}


def handle(msg: dict):
    method = msg.get("method")
    mid = msg.get("id")
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "dabai-peer", "version": "1.0.0"},
        }}
    if method == "notifications/initialized":
        return None
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": [PEER_TOOL]}}
    if method == "tools/call":
        params = msg.get("params") or {}
        if params.get("name") != "peer":
            return {"jsonrpc": "2.0", "id": mid,
                    "error": {"code": -32602, "message": f"未知工具 {params.get('name')}"}}
        out = _call_peer(params.get("arguments") or {})
        bad = out.startswith("管理员未授权") or out.startswith("缺 why")
        return {"jsonrpc": "2.0", "id": mid, "result": _text(out, is_error=bad)}
    if mid is None:
        return None
    return {"jsonrpc": "2.0", "id": mid,
            "error": {"code": -32601, "message": f"未实现的方法 {method}"}}


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        resp = handle(msg)
        if resp is not None:
            _send(resp)


if __name__ == "__main__":
    main()
