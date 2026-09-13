#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""大白 CLI —— 终端里的白头凤。

    dabai                        交互模式
    dabai "改一下 web/style.css"   单次任务
    echo "..." | dabai           管道输入
    dabai -v "..."               显示思维链
    dabai --user default "..."   复用浏览器会话的记忆（默认独立为 cli）

与 Web 服务共用同一套 AIAgent：工具、技能、记忆、任务中心全都在。
默认 user_id=cli 是刻意的——独立会话才不会和浏览器里正在跑的那轮抢
同一份 session 落盘（_save_hist_view / 技能状态都按 session_id 写盘）。
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


def _arg_hint(arguments: str) -> str:
    """把工具参数 JSON 压成一行短摘要，只留一个关键值。"""
    raw = (arguments or "").strip()
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
            self._ensure_break()
            self.tool_name = event.tool_name
            self.tool_started = time.time()
            hint = _arg_hint(getattr(event, "arguments", ""))
            if not self.quiet:
                # 工具说明默认不显示：工具名+参数已经说明意图了，长篇 desc 只会淹掉正文
                desc = ""
                if self.verbose:
                    raw = (getattr(event, "tool_desc", "") or "").split("。", 1)[0]
                    desc = f"{DIM}{raw[:50]}{RESET}"
                line = f"{CYAN}▸ {event.tool_name}{RESET} {hint}"
                sys.stdout.write((line + f" {desc}" if desc else line) + "\n")
                sys.stdout.flush()
        elif isinstance(event, ToolCallProgress):
            # 长任务心跳：只在同一行里滚动，不占屏
            if not self.quiet:
                note = getattr(event, "message", "") or getattr(event, "text", "")
                if note:
                    sys.stdout.write(f"\r{DIM}  ⋯ {str(note)[:70]}{RESET}")
                    sys.stdout.flush()
        elif isinstance(event, ToolCallResult):
            self._ensure_break()
            took = time.time() - self.tool_started if self.tool_started else 0.0
            ok = getattr(event, "success", True)
            mark = f"{GREEN}✓{RESET}" if ok else f"{RED}✗{RESET}"
            brief = _result_brief(getattr(event, "result", ""), ok)
            color = "" if ok else RED
            if not self.quiet:
                sys.stdout.write(f"{mark} {DIM}{took:.1f}s{RESET} {color}{brief}{RESET}\n")
                sys.stdout.flush()
        elif isinstance(event, FinalText):
            # 工具轮的过程话已经流过了，FinalText 只用于收尾判定
            pass
        elif isinstance(event, UsageEvent):
            self._ensure_break()
            if not self.quiet:
                pin = getattr(event, "prompt_tokens", 0) or 0
                pout = getattr(event, "completion_tokens", 0) or 0
                rounds = getattr(event, "rounds", 0) or 0
                sys.stdout.write(
                    f"{DIM}— {_fmt_tokens(pin)}→{_fmt_tokens(pout)} tok"
                    f"{f' · {rounds} 轮' if rounds else ''}{RESET}\n")
                sys.stdout.flush()

    def finish(self) -> None:
        if self.in_text:
            sys.stdout.write("\n")
            self.in_text = False


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


async def repl(agent, printer: _TurnPrinter) -> None:
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
            await run_turn(agent, line, printer)
        except asyncio.CancelledError:
            print(f"{YELLOW}已打断{RESET}")
        except KeyboardInterrupt:
            print(f"\n{YELLOW}已打断{RESET}")
        except Exception as e:
            print(f"{RED}✗ {type(e).__name__}: {e}{RESET}")


async def _run(args, text: str) -> int:
    from agent import AIAgent

    agent = AIAgent(user_id=args.user, namespace=args.namespace)
    try:
        await agent.initialize()
    except Exception as e:
        print(f"{RED}初始化失败: {type(e).__name__}: {e}{RESET}", file=sys.stderr)
        return 2

    printer = _TurnPrinter(verbose=args.verbose, quiet=args.quiet)
    if text:
        try:
            await run_turn(agent, text, printer, json_mode=args.json)
        except KeyboardInterrupt:
            print(f"\n{YELLOW}已打断{RESET}")
            return 130
        return 0

    if not sys.stdin.isatty():
        # 非交互且没给文本：从管道读完全部输入当一次任务
        piped = sys.stdin.read().strip()
        if piped:
            await run_turn(agent, piped, printer, json_mode=args.json)
            return 0

    await repl(agent, printer)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(prog="dabai", description="大白 CLI —— 终端里的白头凤")
    ap.add_argument("message", nargs="*", help="单次任务；留空进入交互模式")
    ap.add_argument("-u", "--user", default="cli",
                    help="会话 user_id（默认 cli，与浏览器会话隔离）")
    ap.add_argument("-v", "--verbose", action="store_true", help="显示思维链与过程话")
    ap.add_argument("--namespace", default="",
                    help="记忆命名空间覆盖（如 longrun）：落独立会话，不与角色卡主会话串扰")
    ap.add_argument("-q", "--quiet", action="store_true", help="只输出正文，不显示工具调用")
    ap.add_argument("--json", action="store_true", help="输出原始事件流（JSON Lines）")
    args = ap.parse_args()

    text = " ".join(args.message).strip()
    try:
        return asyncio.run(_run(args, text))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
