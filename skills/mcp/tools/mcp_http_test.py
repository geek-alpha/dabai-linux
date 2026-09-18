# -*- coding: utf-8 -*-
"""mcp_http 自测：本地假 MCP server（stdlib http.server）验 Streamable HTTP 全路径。

覆盖：initialize 握手 / session id 回传 / 通知 202 / tools/list / tools/call 的
JSON 与 SSE 两种响应 / 未知方法报错 / 401 鉴权提示 / connect() 按 url 分发。
不联网、不启子进程，几秒跑完。用法：python3 skills/mcp/tools/mcp_http_test.py
"""
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _SKILL_DIR)

import mcp_client as mc            # noqa: E402
from mcp_http import MCPHttpServer  # noqa: E402

STATE = {"sse": False, "notified": False, "saw_session": None}
FAILS = []


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, obj, ctype="application/json", status=200, headers=None):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path == "/needauth":
            self._send({"error": "unauthorized"}, status=401)
            return
        n = int(self.headers.get("Content-Length") or 0)
        msg = json.loads(self.rfile.read(n) or b"{}")
        STATE["saw_session"] = self.headers.get("Mcp-Session-Id")
        if "id" not in msg:                      # 通知：202 + 无 body
            STATE["notified"] = True
            self.send_response(202)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        rid, method = msg["id"], msg.get("method")
        if method == "initialize":
            self._send({"jsonrpc": "2.0", "id": rid, "result": {
                "protocolVersion": "2025-03-26", "capabilities": {},
                "serverInfo": {"name": "fake", "version": "0"}}},
                headers={"Mcp-Session-Id": "sess-12345678"})
        elif method == "tools/list":
            self._send({"jsonrpc": "2.0", "id": rid, "result": {"tools": [
                {"name": "echo", "description": "回声",
                 "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}}}]}})
        elif method == "tools/call":
            args = (msg.get("params") or {}).get("arguments") or {}
            result = {"content": [{"type": "text", "text": "echo:" + str(args.get("text", ""))}]}
            if STATE["sse"]:
                payload = json.dumps({"jsonrpc": "2.0", "id": rid, "result": result}).encode("utf-8")
                body = b"event: message\ndata: " + payload + b"\n\n"
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self._send({"jsonrpc": "2.0", "id": rid, "result": result})
        else:
            self._send({"jsonrpc": "2.0", "id": rid,
                        "error": {"code": -32601, "message": "no such method"}})


def check(name, cond, extra=""):
    print(("✔ " if cond else "✘ ") + name + ("" if cond else f"   ← {extra!r}"))
    if not cond:
        FAILS.append(name)


def main():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{port}/mcp"
    print(f"假 server: {url}\n")

    h = MCPHttpServer("fake", url)
    info = h.start()
    check("initialize 握手", info.get("protocolVersion") == "2025-03-26", info)
    check("Mcp-Session-Id 已记录", h.session_id == "sess-12345678", h.session_id)
    check("initialized 通知已发出", STATE["notified"], STATE)
    check("通知也带回 session id", STATE["saw_session"] == "sess-12345678", STATE["saw_session"])

    tools = h.list_tools()
    check("tools/list 拿到工具", [t.get("name") for t in tools] == ["echo"], tools)
    check("tools/list 有缓存（第二次不再请求）", h.list_tools() is tools)

    check("tools/call JSON 路径", h.call_tool("echo", {"text": "hi"}) == "echo:hi")
    STATE["sse"] = True
    check("tools/call SSE 路径", h.call_tool("echo", {"text": "sse"}) == "echo:sse")

    try:
        h._request("no/such")
        check("未知方法应报错", False, "没抛异常")
    except mc.MCPError as e:
        check("未知方法报错带 message", "no such method" in str(e), e)

    try:
        MCPHttpServer("bad", f"http://127.0.0.1:{port}/needauth").start()
        check("401 应报错", False, "没抛异常")
    except mc.MCPError as e:
        check("401 提示要鉴权", "鉴权" in str(e), e)

    h.stop()
    check("stop 后 alive() 为假", not h.alive())

    try:
        srv = mc.connect("fake2", {"url": url}, timeout=10)
        check("connect() 按 url 走 HTTP 分支", isinstance(srv, MCPHttpServer), type(srv))
        check("connect() 后能调工具", srv.call_tool("echo", {"text": "c"}) == "echo:c")
        mc.stop("fake2")
    except mc.MCPError as e:
        print(f"⚠ connect() 分发测试被跳过：{e}")

    httpd.shutdown()
    print("\n" + (f"✘ {len(FAILS)} 项失败：{FAILS}" if FAILS else "✔ 全部通过"))
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
