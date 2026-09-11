"""声音技能 —— 切换音色/语速/合成引擎（返回 __screen_command__ 由前端执行）。"""
from __future__ import annotations

import json


def switch_tts(args: dict) -> str:
    return json.dumps({
        "__screen_command__": True,
        "tool": "switch_tts_settings",
        "args": {
            "voice": args.get("voice"),
            "rate": args.get("rate"),
            "engine": args.get("engine"),
        },
    }, ensure_ascii=False)


HANDLERS = {
    "switch_tts_settings": switch_tts,
}
