"""中途问用户（request_user_input）—— 让「等用户回答」变成一次真实的工具调用。

handler 是 async 的：harness 的技能分发对协程直接 await（harness/skills.py:517），
所以这里能真的挂起等答案，而不是让模型下轮重试。

真正的等待与配对逻辑在根目录 ask_user.py（前端回填走 /api/bridge/confirm）。
"""
from __future__ import annotations


async def request_user_input(args: dict) -> str:
    import ask_user

    a = args or {}
    return await ask_user.ask(
        question=a.get("question", ""),
        options=a.get("options"),
        timeout=a.get("timeout_seconds"),
    )


HANDLERS = {"request_user_input": request_user_input}
