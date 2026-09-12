#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""最小 MCP server（测试夹具）—— 只为验证 mcp 技能的协议层，不依赖网络/第三方包。

实现的 MCP 子集：initialize / notifications/initialized / tools/list / tools/call。
工具：echo(text)、add(a,b)、boom（返回 isError）、hang(seconds)（测超时）。
"""
from __future__ import annotations

import json
import subprocess
import sys
import time

TOOLS = [
    {
        "name": "echo",
        "description": "原样回显 text",
        "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}},
                        "required": ["text"]},
    },
    {
        "name": "add",
        "description": "两个数相加",
        "inputSchema": {"type": "object",
                        "properties": {"a": {"type": "number"}, "b": {"type": "number"}},
                        "required": ["a", "b"]},
    },
    {
        "name": "boom",
        "description": "总是失败（测 isError 路径）",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "hang",
        "description": "睡 N 秒（测客户端超时）",
        "inputSchema": {"type": "object", "properties": {"seconds": {"type": "number"}}},
    },
]


def _send(obj: dict):
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
            "serverInfo": {"name": "dabai-test-server", "version": "0.1.0"},
        }}
    if method == "notifications/initialized":
        return None
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}}
    if method == "tools/call":
        params = msg.get("params") or {}
        name = params.get("name")
        a = params.get("arguments") or {}
        if name == "echo":
            return {"jsonrpc": "2.0", "id": mid, "result": _text(f"echo: {a.get('text', '')}")}
        if name == "add":
            return {"jsonrpc": "2.0", "id": mid,
                    "result": _text(str(float(a.get("a", 0)) + float(a.get("b", 0))))}
        if name == "boom":
            return {"jsonrpc": "2.0", "id": mid, "result": _text("故意失败", is_error=True)}
        if name == "hang":
            time.sleep(float(a.get("seconds", 5)))
            return {"jsonrpc": "2.0", "id": mid, "result": _text("睡醒了")}
        return {"jsonrpc": "2.0", "id": mid,
                "error": {"code": -32602, "message": f"未知工具 {name}"}}
    if mid is None:
        return None
    return {"jsonrpc": "2.0", "id": mid,
            "error": {"code": -32601, "message": f"未实现的方法 {method}"}}


def main():
    if "--spawn-child" in sys.argv:
        # 模拟 npx 那类包装器：fork 一个长命孙进程，验证客户端杀的是整棵进程树
        subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)",
                          "dabai-mcp-child"])
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception:
            continue
        resp = handle(msg)
        if resp is not None:
            _send(resp)


if __name__ == "__main__":
    main()
