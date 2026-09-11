"""界面模式技能 —— 切换应用模式 / 屏幕 Toast（返回 __screen_command__）。"""
from __future__ import annotations

import json


def switch_mode(args: dict) -> str:
    return json.dumps({"__screen_command__": True, "tool": "switch_app_mode", "args": {
        "mode": args.get("mode"),
    }}, ensure_ascii=False)


def show_toast(args: dict) -> str:
    return json.dumps({"__screen_command__": True, "tool": "show_screen_toast", "args": {
        "message": args.get("message"),
    }}, ensure_ascii=False)


HANDLERS = {
    "switch_app_mode": switch_mode,
    "show_screen_toast": show_toast,
}
