"""外观形象技能 —— 查看/切换 3D 角色模型与背景场景。

查询类工具直接扫描目录返回列表；切换类工具返回 __screen_command__ JSON，
由 server 层拦截后转发前端执行（与原来的本地工具行为完全一致）。
"""
from __future__ import annotations

import json
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent.parent

ALLOWED_MODELS = {".glb", ".gltf", ".vrm"}
ALLOWED_BGS = {".glb", ".gltf"}


def _screen(tool: str, args: dict) -> str:
    return json.dumps({"__screen_command__": True, "tool": tool, "args": args},
                      ensure_ascii=False)


def available_models(args: dict) -> str:
    dir_ = BASE_DIR / "models"
    names = []
    if dir_.is_dir():
        for f in sorted(dir_.iterdir()):
            if f.is_file() and f.suffix.lower() in ALLOWED_MODELS:
                names.append(f.name)
    if not names:
        return "暂无可用角色模型，请先上传模型文件。"
    return "可用角色模型列表：\n" + "\n".join(f"  - {n}" for n in names)


def available_backgrounds(args: dict) -> str:
    dir_ = BASE_DIR / "backgrounds"
    items = ["  - default (默认星空背景)"]
    if dir_.is_dir():
        for f in sorted(dir_.iterdir()):
            if f.is_file() and f.suffix.lower() in ALLOWED_BGS:
                items.append(f"  - {f.name}")
    return "可用背景场景列表：\n" + "\n".join(items)


def switch_model(args: dict) -> str:
    return _screen("switch_character_model", {"model_name": args.get("model_name")})


def switch_bg(args: dict) -> str:
    return _screen("switch_background_scene", {"bg_name": args.get("bg_name")})


HANDLERS = {
    "get_available_models": available_models,
    "get_available_backgrounds": available_backgrounds,
    "switch_character_model": switch_model,
    "switch_background_scene": switch_bg,
}
