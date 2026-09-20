#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""最小 MCP server（测试夹具）—— 只为验证 mcp 技能的协议层，不依赖网络/第三方包。

实现的 MCP 子集：initialize / notifications/initialized / tools/list / tools/call。
工具：echo(text)、add(a,b)、boom（返回 isError）、hang(seconds)（测超时）、
structured（只给 structuredContent）、both（两种都给，测优先级）、shot（返回真实 PNG 的 image content，测图片回灌）。
"""
from __future__ import annotations

import base64
import json
import struct
import subprocess
import sys
import time
import zlib

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
    {
        "name": "structured",
        "description": "只给 structuredContent，不给 content（规范只 SHOULD 给 text，所以这是合法 server）",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "both",
        "description": "content 与 structuredContent 都有（测优先级）",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "shot",
        "description": "返回一张真实 PNG（image content），测图片回灌",
        "inputSchema": {"type": "object",
                        "properties": {"w": {"type": "number"}, "h": {"type": "number"}}},
    },
]


def _send(obj: dict):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _text(s: str, is_error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": s}], "isError": is_error}


def _png(w: int = 800, h: int = 600) -> bytes:
    """零依赖生成一张真 PNG（渐变彩条）：夹具不该为造图引入第三方包。"""
    row = b"\x00" + bytes([(i * 255) // max(1, w - 1) for i in range(w) for _ in range(3)])
    raw = row * h

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


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
        if name == "structured":
            return {"jsonrpc": "2.0", "id": mid,
                    "result": {"structuredContent": {"temperature": 22.5, "conditions": "Partly cloudy"}}}
        if name == "both":
            return {"jsonrpc": "2.0", "id": mid,
                    "result": {"content": [{"type": "text", "text": "给人看的一句话"}],
                               "structuredContent": {"temperature": 22.5}}}
        if name == "shot":
            raw = _png(int(a.get("w") or 800), int(a.get("h") or 600))
            return {"jsonrpc": "2.0", "id": mid, "result": {"content": [
                {"type": "text", "text": f"已截图 {len(raw)} 字节"},
                {"type": "image", "data": base64.b64encode(raw).decode(),
                 "mimeType": "image/png"},
            ]}}
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
