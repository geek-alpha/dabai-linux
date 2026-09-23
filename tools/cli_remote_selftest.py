#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""端到端验证：外部 CLI 注入 == 用户在网页输入框里敲下这句话。

    venv/bin/python tools/cli_remote_selftest.py

做法是真的开两条 WebSocket：一条假装成网页（普通连接），另一条走 CLI 的
接入模式。断言「网页」那条连接能收到完整的一轮——user_message 回显 →
thinking → stream_text → turn_text_done。少任何一环，用户看到的就不是
「自己发了一句话」，而是「大白突然自言自语」。

退出码：0 通过 / 1 断言失败 / 3 服务还没加载新代码（等重启，不算失败）。
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import aiohttp  # noqa: E402

import dabai_cli as cli  # noqa: E402

PORT = int(os.environ.get("DABAI_PORT", "0") or 0)
UID = cli._unified_uid()
PROMPT = os.environ.get("DABAI_SELFTEST_PROMPT",
                        "只回五个字：接入测试通过。不要调用任何工具。")


def _code_loaded() -> tuple[bool, list[str]]:
    """运行中的进程是否已加载磁盘上的 server.py / dabai_cli.py。

    判据用 hot_reload 自己落盘的「已加载快照」（tools/reload_check.py 同源）。
    不能拿进程启动时间判：自动重启走 os.execv 自替换，PID 和 starttime 都不变。
    """
    import hot_reload as hr

    p = BASE / "data" / "hot_reload_state.json"
    if not p.exists():
        return True, []
    snap = {k: tuple(v) for k, v in (json.loads(p.read_text("utf-8")).get("core") or {}).items()}
    stale = [name for name in ("server.py", "dabai_cli.py")
             if (BASE / name).exists() and snap.get(str(BASE / name)) != hr._file_sig(BASE / name)]
    return (not stale), stale


async def _run(port: int) -> int:
    seen: list[dict] = []
    async with aiohttp.ClientSession() as sess:
        # 假装是网页：普通连接（不带 headless），走真实的 set_user 路径
        async with sess.ws_connect(f"http://127.0.0.1:{port}/ws", heartbeat=20) as page:
            await page.send_json({"type": "set_user", "user_id": UID})
            if not await cli._wait_for_type(page, "user_set", 10.0):
                print("✗ 网页侧没收到 user_set：服务没起来或握手被拒")
                return 1
            printer = cli._TurnPrinter(quiet=True)
            task = asyncio.create_task(cli._remote_turn(port, UID, PROMPT, printer))
            deadline = time.time() + 300
            while time.time() < deadline:
                try:
                    msg = await asyncio.wait_for(page.receive_json(), timeout=5)
                except asyncio.TimeoutError:
                    if task.done():
                        break
                    continue
                except Exception:
                    break
                seen.append(msg)
                if msg.get("type") == "turn_text_done":
                    break
            rc = await task
    return rc if rc else _assert(seen)


def _assert(seen: list[dict]) -> int:
    types = [str(m.get("type") or "") for m in seen]
    um = next((m for m in seen if m.get("type") == "user_message"), None)
    text = "".join(str(m.get("text") or "") for m in seen if m.get("type") == "stream_text")
    checks = [
        ("网页端收到用户气泡回显 user_message", um is not None),
        ("回显内容与注入的一字不差", bool(um) and um.get("text") == PROMPT),
        ("回显带回 origin，发起方能过滤掉自己那条", bool(um) and str(um.get("origin") or "").startswith("cli-")),
        ("网页端进入思考态 thinking", "thinking" in types),
        ("网页端收到流式正文 stream_text", bool(text.strip())),
        ("一轮正常收尾 turn_text_done", "turn_text_done" in types),
    ]
    bad = [name for name, ok in checks if not ok]
    for name, ok in checks:
        print(f"{'✓' if ok else '✗'} {name}")
    if text.strip():
        print(f"  正文：{text.strip()[:80]}")
    print(f"  事件序列：{types[:14]}")
    return 0 if not bad else 1


def main() -> int:
    ok, stale = _code_loaded()
    if not ok:
        print(f"[等重启] 运行中的服务还没加载新代码：{', '.join(stale)}")
        print("         （hot_reload 会把重启推迟到当前对话轮结束——这是设计，不是卡住）")
        return 3
    port = PORT or asyncio.run(cli._pick_server(cli._SERVER_PORTS))
    if not port:
        print(f"✗ 没找到运行中的服务（试过 {cli._SERVER_PORTS}）")
        return 1
    print(f"目标：127.0.0.1:{port} · 身份 {cli._describe_identity(UID)}")
    return asyncio.run(_run(port))


if __name__ == "__main__":
    sys.exit(main())
