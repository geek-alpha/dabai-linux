# -*- coding: utf-8 -*-
"""MCP Streamable HTTP 传输 —— JSON-RPC 2.0 over 单个 HTTP 端点。

stdio 版（mcp_client.MCPServer）管的是子进程；这里没有进程，只有一条会话
（url + headers + session id）。两条传输的共同面只有「握手 / 拉清单 / 调用 /
结果格式化」，所以复用 mcp_client 的常量与格式化函数，本文件只管 HTTP。

规范（MCP 2025-03-26 Streamable HTTP）：
- 所有消息 POST 到同一个 url，Accept 必须同时含 application/json 与 text/event-stream；
- 服务端可回 application/json（单条消息）或 text/event-stream（SSE，data: 行）；
- initialize 响应头若带 Mcp-Session-Id，后续每个请求都要原样带回；
- 通知（无 id）回 202 且无 body，不是错误。
"""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

from mcp_client import CLIENT_INFO, DEFAULT_TIMEOUT, MCPError, format_tool_result

HTTP_PROTOCOL_VERSION = "2025-03-26"
_ACCEPT = "application/json, text/event-stream"


class MCPHttpServer:
    """一个远程 MCP server 的 HTTP 会话（无子进程，stop 只清本地状态）。"""

    def __init__(self, name: str, url: str, headers: dict = None,
                 timeout: float = DEFAULT_TIMEOUT):
        self.name = name
        self.url = str(url).strip()
        self.headers = {str(k): str(v) for k, v in (headers or {}).items()}
        self.timeout = float(timeout or DEFAULT_TIMEOUT)
        self.info = None
        self.tools = []
        self.session_id = None
        self.protocol_version = None
        self._next_id = 0
        self._closed = False
        self._lock = threading.Lock()

    def alive(self) -> bool:
        return not self._closed

    def stderr_tail(self, lines: int = 10) -> str:
        return ""  # 没有子进程；留着是为了跟 stdio 版同形

    def describe(self) -> str:
        sid = f"，session {self.session_id[:8]}…" if self.session_id else ""
        return f"HTTP {self.url}{sid}"

    def start(self, timeout: float = DEFAULT_TIMEOUT) -> dict:
        if self.info:
            return self.info
        result = self._request("initialize", {
            "protocolVersion": HTTP_PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": CLIENT_INFO,
        }, timeout=timeout)
        self.info = result or {}
        self.protocol_version = str(self.info.get("protocolVersion") or HTTP_PROTOCOL_VERSION)
        self._notify("notifications/initialized", timeout=timeout)
        return self.info

    def list_tools(self, timeout: float = DEFAULT_TIMEOUT) -> list:
        if self.tools:
            return self.tools
        result = self._request("tools/list", timeout=timeout) or {}
        self.tools = list(result.get("tools") or [])
        return self.tools

    def call_tool(self, tool: str, arguments=None, timeout: float = DEFAULT_TIMEOUT) -> str:
        result = self._request("tools/call", {
            "name": tool, "arguments": arguments or {},
        }, timeout=timeout) or {}
        return format_tool_result(result)

    def stop(self):
        self._closed = True
        self.tools = []
        self.info = None
        self.session_id = None

    # ---------- HTTP 细节 ----------

    def _request(self, method: str, params=None, timeout: float = DEFAULT_TIMEOUT):
        with self._lock:  # 一条会话串行化，避免 id 竞争与半截响应
            self._next_id += 1
            rid = self._next_id
            reply = self._post({"jsonrpc": "2.0", "id": rid, "method": method,
                                "params": params if params is not None else {}},
                               timeout=timeout, want_id=rid)
        if reply is None:
            raise MCPError(f"{method} 无响应：服务端返回空 body")
        err = reply.get("error")
        if err:
            if isinstance(err, dict):
                raise MCPError(f"{method} 失败：{err.get('message')}（code {err.get('code')}）")
            raise MCPError(f"{method} 失败：{err}")
        return reply.get("result")

    def _notify(self, method: str, params=None, timeout: float = DEFAULT_TIMEOUT):
        self._post({"jsonrpc": "2.0", "method": method, "params": params or {}},
                   timeout=timeout, want_id=None)

    def _post(self, msg: dict, timeout: float = DEFAULT_TIMEOUT, want_id=None):
        headers = {"Content-Type": "application/json", "Accept": _ACCEPT}
        headers.update(self.headers)
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        if self.protocol_version:
            headers["MCP-Protocol-Version"] = self.protocol_version
        body = json.dumps(msg, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(self.url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=float(timeout or self.timeout)) as resp:
                sid = resp.headers.get("Mcp-Session-Id")
                if sid:
                    self.session_id = sid
                if want_id is None:
                    resp.read()
                    return None
                ctype = (resp.headers.get("Content-Type") or "").lower()
                if "text/event-stream" in ctype:
                    return self._read_sse(resp, want_id)
                raw = resp.read().decode("utf-8", "replace").strip()
        except urllib.error.HTTPError as e:
            raise MCPError(self._http_error_text(e)) from None
        except urllib.error.URLError as e:
            raise MCPError(f"连不上 {self.url}：{e.reason}") from None
        except TimeoutError:
            raise MCPError(f"{self.url} 响应超时（{timeout}s）") from None
        if not raw:
            return None
        try:
            return json.loads(raw)
        except Exception:
            raise MCPError(f"{self.url} 返回的不是 JSON：{raw[:200]}") from None

    @staticmethod
    def _read_sse(resp, want_id):
        """从 SSE 流里取出 id 匹配的那条消息；服务端发完即返，不等流关闭。"""
        data_lines = []
        for raw_line in resp:
            line = raw_line.decode("utf-8", "replace").rstrip("\r\n")
            if not line:
                if not data_lines:
                    continue
                payload = "\n".join(data_lines)
                data_lines = []
                try:
                    msg = json.loads(payload)
                except Exception:
                    continue
                if msg.get("id") == want_id:
                    return msg
                continue
            if line.startswith(":"):  # 注释/心跳
                continue
            if line.startswith("data:"):
                data_lines.append(line[5:].lstrip())
        raise MCPError(f"SSE 流已结束，仍没等到 id={want_id} 的响应")

    @staticmethod
    def _http_error_text(e) -> str:
        try:
            body = e.read().decode("utf-8", "replace").strip()
        except Exception:
            body = ""
        hint = ""
        if e.code in (401, 403):
            hint = " —— 该端点要鉴权，把 key 放进 headers，如 {\"Authorization\": \"Bearer xxx\"}"
        elif e.code == 404:
            hint = " —— 路径不对，确认 URL 是否漏了 /mcp 之类的后缀"
        elif e.code in (405, 406):
            hint = " —— 该端点可能只支持旧版 HTTP+SSE（/sse），或要求别的 Accept 头"
        return f"HTTP {e.code} {e.reason}{hint}；响应：{body[:300] or '（空）'}"
