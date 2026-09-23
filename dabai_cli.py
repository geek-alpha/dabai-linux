#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""大白 CLI —— 终端里的白头凤。

    dabai                        交互模式
    dabai "改一下 web/style.css"   单次任务
    echo "..." | dabai           管道输入
    dabai -v "..."               显示思维链
    dabai --local "..."          强制本地执行（不接入服务）

两种执行方式：

接入模式（默认，服务在跑时）：把这句话交给正在运行的大白服务，和用户在网页
  输入框里敲下去走同一条路——同一个 Agent 实例、同一份短期记忆、同一个
  session，网页端实时看到「用户气泡 + 工具链 + 流式回复」，终端看到同一份
  事件流。身份默认取 settings.json 的 unified_user_id（= 网页端身份）。
本地模式（服务没跑 / --local / --json / --namespace）：进程内起 AIAgent，
  默认 user_id=cli，与浏览器会话隔离。

--json 与 --namespace 默认走本地：前者要的是稳定的事件格式（长跑引擎按它
解析），后者要的是独立会话——接进服务会污染网页那条对话线。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

_TTY = sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def _c(code: str) -> str:
    return code if _TTY else ""


DIM, BOLD = _c("\033[2m"), _c("\033[1m")
CYAN, GREEN, RED, YELLOW = _c("\033[36m"), _c("\033[32m"), _c("\033[31m"), _c("\033[33m")
RESET = _c("\033[0m")

# 从工具参数里挑一个最能说明「它在干什么」的键
_ARG_KEYS = ("path", "file_path", "file", "command", "query", "pattern",
             "url", "name", "skill", "symbol", "root")


def _arg_hint(arguments) -> str:
    """把工具参数压成一行短摘要，只留一个关键值。

    入参两种来源：本地模式是 agent 事件的 JSON 字符串，接入模式是服务端事件的
dict——两条路都收，摘要格式才不会两样。
    """
    if isinstance(arguments, dict):
        data = arguments
    else:
        raw = str(arguments or "").strip()
        if not raw:
            return ""
        try:
            data = json.loads(raw)
        except Exception:
            return raw.replace("\n", " ")[:70]
    if not isinstance(data, dict):
        return str(data)[:70]
    for key in _ARG_KEYS:
        val = data.get(key)
        if isinstance(val, (str, int, float)) and str(val).strip():
            text = str(val).replace("\n", " ").strip()
            return text if len(text) <= 70 else text[:67] + "..."
    for key, val in data.items():
        if isinstance(val, (str, int, float)) and str(val).strip():
            return f"{key}={str(val)[:60]}"
    return ""


def _result_brief(result: str, success: bool) -> str:
    """工具结果的单行摘要：成功只报体量，失败才给首行原文（错了要看原因）。"""
    text = (result or "").strip()
    if not text:
        return "空"
    if success:
        first = text.split("\n", 1)[0][:60]
        return f"{len(text)}B · {first}" if len(text) > 60 else first
    return text.split("\n", 1)[0][:100]


def _fmt_tokens(n: int) -> str:
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


class _TurnPrinter:
    """一轮对话的终端渲染：正文直出，工具调用压成一行，思维链默认闭嘴。"""

    def __init__(self, verbose: bool = False, quiet: bool = False):
        self.verbose = verbose
        self.quiet = quiet
        self.in_text = False
        self.tool_name = ""
        self.tool_started = 0.0

    def _ensure_break(self) -> None:
        if self.in_text:
            sys.stdout.write("\n")
            self.in_text = False

    def _tool_start(self, name: str, arguments, desc_raw: str = "") -> None:
        """工具开跑：一行说清「谁 + 拿什么参数」，工具说明只在 -v 时附上。"""
        self._ensure_break()
        self.tool_name = name
        self.tool_started = time.time()
        if self.quiet:
            return
        hint = _arg_hint(arguments)
        desc = ""
        if self.verbose:
            desc = f"{DIM}{(desc_raw or '').split('。', 1)[0][:50]}{RESET}"
        line = f"{CYAN}▸ {name}{RESET} {hint}"
        sys.stdout.write((line + f" {desc}" if desc else line) + "\n")
        sys.stdout.flush()

    def _tool_progress(self, note: str) -> None:
        """长任务心跳：只在同一行里滚动，不占屏。"""
        if self.quiet or not note:
            return
        sys.stdout.write(f"\r{DIM}  ⋯ {str(note)[:70]}{RESET}")
        sys.stdout.flush()

    def _tool_result(self, ok: bool, result: str) -> None:
        self._ensure_break()
        took = time.time() - self.tool_started if self.tool_started else 0.0
        mark = f"{GREEN}✓{RESET}" if ok else f"{RED}✗{RESET}"
        brief = _result_brief(result, ok)
        color = "" if ok else RED
        if not self.quiet:
            sys.stdout.write(f"{mark} {DIM}{took:.1f}s{RESET} {color}{brief}{RESET}\n")
            sys.stdout.flush()

    def _usage(self, pin: int, pout: int, rounds: int) -> None:
        self._ensure_break()
        if not self.quiet:
            sys.stdout.write(
                f"{DIM}— {_fmt_tokens(pin)}→{_fmt_tokens(pout)} tok"
                f"{f' · {rounds} 轮' if rounds else ''}{RESET}\n")
            sys.stdout.flush()

    def handle(self, event) -> None:
        from agent import (StreamDelta, ReasoningDelta, ThinkingDelta,
                           ToolCallStart, ToolCallResult, ToolCallProgress,
                           FinalText, UsageEvent)

        if isinstance(event, StreamDelta):
            self.in_text = True
            sys.stdout.write(event.text)
            sys.stdout.flush()
        elif isinstance(event, (ReasoningDelta, ThinkingDelta)):
            if self.verbose and not self.quiet:
                self._ensure_break()
                sys.stdout.write(f"{DIM}{event.text}{RESET}")
                sys.stdout.flush()
        elif isinstance(event, ToolCallStart):
            self._tool_start(event.tool_name, getattr(event, "arguments", ""),
                             getattr(event, "tool_desc", ""))
        elif isinstance(event, ToolCallProgress):
            self._tool_progress(getattr(event, "message", "") or getattr(event, "text", ""))
        elif isinstance(event, ToolCallResult):
            self._tool_result(getattr(event, "success", True), getattr(event, "result", ""))
        elif isinstance(event, FinalText):
            # 工具轮的过程话已经流过了，FinalText 只用于收尾判定
            pass
        elif isinstance(event, UsageEvent):
            self._usage(getattr(event, "prompt_tokens", 0) or 0,
                        getattr(event, "completion_tokens", 0) or 0,
                        getattr(event, "rounds", 0) or 0)

    def finish(self) -> None:
        if self.in_text:
            sys.stdout.write("\n")
            self.in_text = False

    def handle_ws(self, msg: dict) -> None:
        """服务端事件 → 终端渲染（接入模式）。

        与 handle() 同一套排版，只是事件从 WebSocket 来：同一份事件流换个出口，
        网页看到的和终端看到的才是一件事。
        """
        mtype = str(msg.get("type") or "")
        if mtype == "stream_text":
            self.in_text = True
            sys.stdout.write(str(msg.get("text") or ""))
            sys.stdout.flush()
        elif mtype in ("thinking_text", "reasoning"):
            if self.verbose and not self.quiet:
                self._ensure_break()
                sys.stdout.write(f"{DIM}{msg.get('text') or ''}{RESET}")
                sys.stdout.flush()
        elif mtype == "retract_text":
            # 工具轮的过程话被服务端撤回：终端删不掉已经打出来的字，只把状态复位，
            # 免得后面的正文接在过程话屁股后面
            self._ensure_break()
            if self.verbose and not self.quiet:
                sys.stdout.write(f"{DIM}⋯ 过程话已折叠{RESET}\n")
        elif mtype == "tool_call_start":
            self._tool_start(str(msg.get("tool_name") or ""), msg.get("arguments"),
                             str(msg.get("tool_desc") or ""))
        elif mtype == "tool_call_progress":
            self._tool_progress(str(msg.get("message") or ""))
        elif mtype == "tool_call_result":
            self._tool_result(bool(msg.get("success", True)), str(msg.get("result") or ""))
        elif mtype == "usage":
            self._usage(msg.get("prompt_tokens") or 0, msg.get("completion_tokens") or 0,
                        msg.get("rounds") or 0)
        elif mtype == "system_msg":
            self._ensure_break()
            if not self.quiet:
                sys.stdout.write(f"{YELLOW}{msg.get('text') or ''}{RESET}\n")
                sys.stdout.flush()
        elif mtype == "interrupted":
            self._ensure_break()
            sys.stdout.write(f"{YELLOW}已打断{RESET}\n")
            sys.stdout.flush()
        elif mtype == "error":
            self._ensure_break()
            print(f"{RED}✗ {msg.get('message') or ''}{RESET}", file=sys.stderr)


def _event_json(event) -> dict:
    """事件 → JSON 可序列化 dict（--json 模式给脚本消费）。"""
    d = {"type": type(event).__name__}
    for key in ("text", "tool_name", "arguments", "tool_desc", "result",
                "success", "message", "prompt_tokens", "completion_tokens",
                "total_tokens", "rounds"):
        if hasattr(event, key):
            val = getattr(event, key)
            if isinstance(val, str) and len(val) > 2000:
                val = val[:2000] + "…"
            d[key] = val
    return d


async def run_turn(agent, message: str, printer: _TurnPrinter, json_mode: bool = False) -> None:
    async for event in agent.chat_stream(message):
        if json_mode:
            print(json.dumps(_event_json(event), ensure_ascii=False), flush=True)
        else:
            printer.handle(event)
    if not json_mode:
        printer.finish()


BANNER = f"""{BOLD}白头凤 CLI{RESET} {DIM}· Battle Phoenix · 终端模式
{_c('')}{DIM}输入任务回车执行；/exit 退出，Ctrl+C 打断当前轮{RESET}"""


# ---------- 接入模式：把一轮交给运行中的服务 ----------

# 8001 = nginx 前置 TLS 终结时的回源端口（HTTP），8000 = 直连端口。CLI 只连本机。
_SERVER_PORTS = (8001, 8000)
# 一轮里最长允许的静默：工具轮有心跳（tool_call_progress / turn_status），静默超过
# 这个量级基本是连接断了或服务卡死，没必要无限等
_IDLE_TIMEOUT = float(os.environ.get("DABAI_CLI_IDLE", "900"))


def _unified_uid() -> str:
    """与网页端同一个身份：settings.json -> agent.unified_user_id。"""
    try:
        data = json.loads((BASE_DIR / "settings.json").read_text("utf-8"))
        uid = str((data.get("agent") or {}).get("unified_user_id") or "").strip()
        return uid or "default"
    except Exception:
        return "default"


def _resolve_uid(args, mode: str) -> str:
    """会话身份：接入模式跟网页同一个 uid，本地模式保持 cli（与浏览器隔离）。"""
    if args.user:
        return str(args.user)
    return _unified_uid() if mode == "remote" else "cli"


def _pick_mode(args) -> str:
    """执行方式：local / remote / auto。

    --json 与 --namespace 默认本地：前者是给脚本消费的稳定事件格式（长跑引擎按它
    解析），后者要的是独立会话——接进服务会污染网页那条对话线。
    """
    if args.local:
        return "local"
    if args.remote:
        return "remote"
    if args.json or args.namespace:
        return "local"
    return "auto"


def _describe_identity(uid: str) -> str:
    """这个身份在工具层是什么权限——直接问 sandbox 的判据，不另写一套。"""
    try:
        import sandbox as _sb
        a = _sb.actor_for(uid)
        if a.is_admin:
            return f"user={uid} · 管理员（工具不受沙箱限制）"
        return f"user={uid} · 普通用户（沙箱 {a.sandbox}）"
    except Exception:
        return f"user={uid}"


async def _pick_server(ports) -> int:
    """探到正在跑的大白服务返回端口，没有则 0。"""
    import aiohttp

    timeout = aiohttp.ClientTimeout(total=1.5)
    async with aiohttp.ClientSession(timeout=timeout) as sess:
        for port in ports:
            try:
                async with sess.get(f"http://127.0.0.1:{port}/api/frontend-build") as r:
                    if r.status == 200:
                        return port
            except Exception:
                continue
    return 0


async def _wait_for_type(ws, want: str, timeout: float) -> bool:
    deadline = time.time() + timeout
    while True:
        left = deadline - time.time()
        if left <= 0:
            return False
        try:
            msg = await asyncio.wait_for(ws.receive_json(), timeout=left)
        except Exception:
            return False
        if str(msg.get("type") or "") == want:
            return True


async def _remote_turn(port: int, uid: str, text: str, printer: _TurnPrinter,
                       json_mode: bool = False) -> int:
    """把一句话交给运行中的服务，并按同一份事件流渲染终端。

    走的就是网页输入框那条路：服务端收到 external 标记后，除了照常跑这一轮，
    还会广播 user_message 让网页端把这句话显示成「用户发的」。
    """
    import aiohttp

    client_id = f"cli-{os.getpid()}-{int(time.time() * 1000) % 1000000}"
    try:
        async with aiohttp.ClientSession() as sess:
            async with sess.ws_connect(f"http://127.0.0.1:{port}/ws",
                                       heartbeat=20, max_msg_size=8 * 1024 * 1024) as ws:
                # headless：这条连接没有界面，不参与「最近活动连接」的竞争
                await ws.send_json({"type": "set_user", "user_id": uid, "headless": True})
                # 必须先等 user_set：没有它服务端不知道这条连接属于谁——广播会发给
                # 所有人，agent 还会拿空 uid 去跑（串进别人的记忆）
                if not await _wait_for_type(ws, "user_set", 10.0):
                    print(f"{RED}✗ 服务没确认身份（set_user 无响应）{RESET}", file=sys.stderr)
                    return 2
                await ws.send_json({"type": "text", "content": text,
                                    "external": True, "client_id": client_id})
                sid = ""
                try:
                    while True:
                        try:
                            msg = await asyncio.wait_for(ws.receive_json(), timeout=_IDLE_TIMEOUT)
                        except asyncio.TimeoutError:
                            print(f"{RED}✗ {_IDLE_TIMEOUT:.0f}s 内没有任何事件，"
                                  f"连接可能已断{RESET}", file=sys.stderr)
                            return 1
                        mtype = str(msg.get("type") or "")
                        if json_mode:
                            print(json.dumps(msg, ensure_ascii=False), flush=True)
                            if mtype == "turn_text_done":
                                return 0
                            continue
                        if mtype == "user_message" and str(msg.get("origin") or "") == client_id:
                            continue  # 自己这句：输入行已经在终端打过了
                        if mtype == "thinking" and not sid:
                            sid = str(msg.get("session_id") or "")
                        printer.handle_ws(msg)
                        if mtype == "turn_text_done" and (not sid or msg.get("session_id") == sid):
                            printer.finish()
                            return 0
                except asyncio.CancelledError:
                    # Ctrl+C：和服务端「停止」是同一个动作，别把一轮任务留在后台空转
                    try:
                        await ws.send_json({"type": "interrupt"})
                    except Exception:
                        pass
                    raise
    except Exception as e:
        print(f"{RED}✗ 接入失败: {type(e).__name__}: {e}{RESET}", file=sys.stderr)
        return 1


class _TurnFailed(Exception):
    """接入模式这一轮失败了（_remote_turn 已经把原因打到 stderr）。"""

    def __init__(self, rc: int):
        super().__init__(f"接入模式执行失败（exit={rc}）")
        self.rc = rc


class _LocalSession:
    """进程内一轮：服务没跑时的退路，与浏览器会话隔离。"""

    def __init__(self, agent):
        self.agent = agent

    async def turn(self, text: str, printer: _TurnPrinter) -> None:
        await run_turn(self.agent, text, printer)


class _RemoteSession:
    """接入模式：一轮交给运行中的服务，与网页输入框同一条路。"""

    def __init__(self, port: int, uid: str, json_mode: bool = False):
        self.port = port
        self.uid = uid
        self.json_mode = json_mode

    async def turn(self, text: str, printer: _TurnPrinter) -> None:
        rc = await _remote_turn(self.port, self.uid, text, printer,
                                json_mode=self.json_mode)
        if rc:
            raise _TurnFailed(rc)


async def repl(session, printer: _TurnPrinter) -> None:
    print(BANNER)
    while True:
        try:
            line = await asyncio.to_thread(input, f"{BOLD}你 › {RESET}")
        except (EOFError, KeyboardInterrupt):
            print()
            return
        line = line.strip()
        if not line:
            continue
        if line in ("/exit", "/quit", ":q", "exit", "quit"):
            return
        if line in ("/help", "?"):
            print(f"{DIM}/exit 退出 · Ctrl+C 打断当前轮 · 其余输入直接执行{RESET}")
            continue
        try:
            await session.turn(line, printer)
        except _TurnFailed as e:
            print(f"{RED}✗ {e}{RESET}")
        except asyncio.CancelledError:
            print(f"{YELLOW}已打断{RESET}")
        except KeyboardInterrupt:
            print(f"\n{YELLOW}已打断{RESET}")
        except Exception as e:
            print(f"{RED}✗ {type(e).__name__}: {e}{RESET}")


async def _run(args, text: str) -> int:
    printer = _TurnPrinter(verbose=args.verbose, quiet=args.quiet)
    mode = _pick_mode(args)

    port = 0
    if mode != "local":
        # 显式 --port 就只试它：用户指定了端口，就不该静默改用另一个
        cand = (args.port,) if args.port else _SERVER_PORTS
        port = await _pick_server(cand)
        if not port and mode == "remote":
            print(f"{RED}✗ 没找到运行中的大白服务（试过 {cand}）："
                  f"先 ./dabai.sh 起服务，或加 --local 本地跑{RESET}", file=sys.stderr)
            return 2

    if port:
        session = _RemoteSession(port, _resolve_uid(args, "remote"), json_mode=args.json)
        print(f"{DIM}▸ 已接入大白服务 :{port} · {_describe_identity(session.uid)}{RESET}")
    else:
        from agent import AIAgent

        agent = AIAgent(user_id=_resolve_uid(args, "local"), namespace=args.namespace)
        try:
            await agent.initialize()
        except Exception as e:
            print(f"{RED}初始化失败: {type(e).__name__}: {e}{RESET}", file=sys.stderr)
            return 2
        session = _LocalSession(agent)
        if mode == "auto":
            print(f"{DIM}▸ 服务未运行，本地模式 · user={agent.user_id}{RESET}")

    if text:
        try:
            await session.turn(text, printer)
        except _TurnFailed as e:
            return e.rc
        except KeyboardInterrupt:
            print(f"\n{YELLOW}已打断{RESET}")
            return 130
        return 0

    if not sys.stdin.isatty():
        # 非交互且没给文本：从管道读完全部输入当一次任务
        piped = sys.stdin.read().strip()
        if piped:
            try:
                await session.turn(piped, printer)
            except _TurnFailed as e:
                return e.rc
            return 0

    await repl(session, printer)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(prog="dabai", description="大白 CLI —— 终端里的白头凤")
    ap.add_argument("message", nargs="*", help="单次任务；留空进入交互模式")
    ap.add_argument("-u", "--user", default=None,
                    help="会话身份（默认：接入模式用网页同一个 uid，本地模式用 cli）")
    ap.add_argument("-v", "--verbose", action="store_true", help="显示思维链与过程话")
    ap.add_argument("--namespace", default="",
                    help="记忆命名空间覆盖（如 longrun）：落独立会话，不与角色卡主会话串扰")
    ap.add_argument("-q", "--quiet", action="store_true", help="只输出正文，不显示工具调用")
    ap.add_argument("--json", action="store_true", help="输出原始事件流（JSON Lines）")
    ap.add_argument("--local", action="store_true", help="强制进程内执行（不接入服务）")
    ap.add_argument("--remote", action="store_true", help="强制接入运行中的服务")
    ap.add_argument("--port", type=int, default=0, help="服务端口（默认自动探测）")
    args = ap.parse_args()
    if args.local and args.remote:
        ap.error("--local 与 --remote 互斥")

    text = " ".join(args.message).strip()
    try:
        return asyncio.run(_run(args, text))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
