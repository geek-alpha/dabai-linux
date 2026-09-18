# -*- coding: utf-8 -*-
"""mcp 技能实现 —— 按需连接 MCP server，不用即断。

连接两种：远程 url（Streamable HTTP）或本地 command（子进程）。

元工具只有 4 个（servers/connect/call/disconnect），server 自己的工具不占常驻 schema：
先 connect 拿清单，再 call 调。这是「技能当闸门、MCP 当后端」的落地方式。
"""
from __future__ import annotations

import json
import os
import shlex
import sys

_SKILL_DIR = os.path.dirname(os.path.abspath(__file__))
if _SKILL_DIR not in sys.path:
    sys.path.insert(0, _SKILL_DIR)

import mcp_client as mc  # noqa: E402

_MAX_TOOLS_SHOWN = 40


def _as_list(v):
    """args 容错：模型常把数组传成字符串（'a b' 或 JSON 串）。"""
    if v is None or v == "":
        return []
    if isinstance(v, (list, tuple)):
        return [str(x) for x in v]
    s = str(v).strip()
    if s.startswith("["):
        try:
            return [str(x) for x in json.loads(s)]
        except Exception:
            pass
    try:
        return shlex.split(s)
    except Exception:
        return s.split()


def _as_dict(v):
    if v is None or v == "":
        return {}
    if isinstance(v, dict):
        return v
    try:
        obj = json.loads(str(v))
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def _fmt_tools(tools: list) -> str:
    lines = []
    for t in tools[:_MAX_TOOLS_SHOWN]:
        name = t.get("name", "?")
        desc = (t.get("description") or "").strip().replace("\n", " ")
        if len(desc) > 110:
            desc = desc[:110] + "…"
        schema = t.get("inputSchema") or {}
        props = schema.get("properties") or {}
        req = schema.get("required") or []
        arg_txt = ", ".join(
            f"{k}{'*' if k in req else ''}" for k in props
        ) or "无参数"
        lines.append(f"- {name}({arg_txt})：{desc}")
    if len(tools) > _MAX_TOOLS_SHOWN:
        lines.append(f"…（共 {len(tools)} 个，只列前 {_MAX_TOOLS_SHOWN} 个）")
    return "\n".join(lines) if lines else "（该 server 没有暴露工具）"


# ---------- 工具实现 ----------

def do_servers(args: dict) -> str:
    specs = mc.load_specs()
    live = {s.name: s for s in mc.running()}
    if not specs and not live:
        return ("没有已配置的 MCP server。用 mcp_connect 传 url 或 command 现场接一个，"
                "例如：mcp_connect(server=\"fs\", command=\"npx\", "
                "args=[\"-y\",\"@modelcontextprotocol/server-filesystem\",\"/tmp\"])；"
                "远程的：mcp_connect(server=\"xxx\", url=\"https://host/mcp\", "
                "headers={\"Authorization\":\"Bearer ...\"})")
    lines = []
    for name in sorted(set(specs) | set(live)):
        spec = specs.get(name) or {}
        cmd = (f"URL {spec['url']}" if spec.get("url") else
               " ".join([str(spec.get("command", "?"))] + [str(a) for a in (spec.get("args") or [])]))
        if name in live:
            srv = live[name]
            n = len(srv.tools) if srv.tools else "未拉取"
            lines.append(f"- {name} [运行中] {srv.describe()} 工具 {n} 个 | {cmd}")
        else:
            lines.append(f"- {name} [未连接] | {cmd}")
    st = mc.resource_state()
    out = ("MCP server 清单：\n" + "\n".join(lines) +
           f"\n资源：{mc._fmt_state(st)}（温度上限 {st['temp_limit']:.0f}°C / "
           f"可用内存下限 {st['mem_floor_mb']}MB / 并发上限 {st['max_live']} 个）")
    stale = mc.stale_servers()
    if stale:
        names = "、".join(f"{s['name']}(pgid {s['pgid']})" for s in stale)
        out += (f"\n⚠ {len(stale)} 个孤儿进程组：{names}——技能模块重载后遗留，工具已管不到。"
                f"调 mcp_disconnect(server=\"all\") 清理。")
    return out


def do_connect(args: dict) -> str:
    name = str(args.get("server") or "").strip()
    if not name:
        return "缺少 server 参数（给这个连接起个短名字，如 'fs'）。"
    spec = {}
    if args.get("url"):
        spec = {"url": str(args["url"]).strip()}
        if args.get("headers"):
            spec["headers"] = _as_dict(args["headers"])
    elif args.get("command"):
        spec = {
            "command": str(args["command"]).strip(),
            "args": _as_list(args.get("args")),
        }
        if args.get("env"):
            spec["env"] = _as_dict(args["env"])
        if args.get("cwd"):
            spec["cwd"] = str(args["cwd"])
    timeout = float(args.get("timeout") or mc.DEFAULT_TIMEOUT)
    allow_heavy = str(args.get("allow_heavy", "")).lower() in ("1", "true", "yes", "是")
    try:
        srv = mc.connect(name, spec or None, timeout=timeout, allow_heavy=allow_heavy)
        tools = srv.list_tools(timeout=timeout)
    except mc.MCPError as e:
        return f"连接失败：{e}"
    except Exception as e:
        return f"连接失败：{e.__class__.__name__}: {e}"
    if spec:
        mc.save_spec(name, spec)  # 连上了才落盘：被闸门拦下的配置不该留在 servers.json 里
    head = (f"已连接 {name}：{srv.describe()}"
            f"（协议 {(srv.info or {}).get('protocolVersion', '?')}），"
            f"{len(tools)} 个工具。用 mcp_call(server=\"{name}\", tool=\"...\", arguments={{...}}) 调用。\n")
    return head + _fmt_tools(tools)


def do_call(args: dict) -> str:
    name = str(args.get("server") or "").strip()
    tool = str(args.get("tool") or "").strip()
    if not name or not tool:
        return "需要 server 和 tool 两个参数。"
    timeout = float(args.get("timeout") or mc.DEFAULT_TIMEOUT)
    try:
        mc.resource_guard("call", name)  # 已连接的也要拦：跑着跑着温度冲上去同样危险
        srv = mc.connect(name, None, timeout=timeout)  # 未连接则自动连
        return srv.call_tool(tool, _as_dict(args.get("arguments")), timeout=timeout)
    except mc.MCPError as e:
        return f"调用失败：{e}"
    except Exception as e:
        return f"调用失败：{e.__class__.__name__}: {e}"


def do_disconnect(args: dict) -> str:
    name = str(args.get("server") or "all").strip() or "all"
    if name in ("all", "*"):
        live = mc.running()
        mc.stop_all()
        killed = mc.reap_stale()  # 重载遗留的孤儿：字典里没有，得靠 pid 文件对账
        parts = []
        if live:
            parts.append(f"{len(live)} 个进程已杀")
        if killed:
            parts.append(f"另清 {len(killed)} 个孤儿（{'、'.join(k['name'] for k in killed)}）")
        return ("已断开全部 MCP server（" + "，".join(parts) + "）。" if parts
                else "没有运行中的 MCP server。")
    return f"已断开 {name}。" if mc.stop(name) else f"{name} 本来就没在运行。"


HANDLERS = {
    "mcp_servers": do_servers,
    "mcp_connect": do_connect,
    "mcp_call": do_call,
    "mcp_disconnect": do_disconnect,
}

PROMPT = (
    "【技能 MCP】接第三方 MCP server：mcp_servers 看清单/运行状态 → "
    "mcp_connect(server, url|command, args) 连接并拿工具清单 → "
    "mcp_call(server, tool, arguments) 调用 → mcp_disconnect(server=\"all\") 断开。"
    "远程 server 传 url（Streamable HTTP）+ 需要时 headers 带 key；本地 server 传 command/args 拉子进程。"
    "server 的工具不进常驻工具表，必须先 connect 看清单再 call。"
    "【资源闸门】连接与调用前会查温度和可用内存，超线直接拒绝（这是硬拦截，不是建议）；"
    "命令里带浏览器内核（chromium/playwright 等）需显式 allow_heavy=true 才放行——"
    "这台是 1GB 的 Pi 3，浏览器会把它烧到硬关机。用完立即 mcp_disconnect，别常驻。"
)
