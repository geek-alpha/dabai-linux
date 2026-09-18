# -*- coding: utf-8 -*-
"""服务内模块热重载：把磁盘上的新代码灌进正在跑的服务进程，不重启。

为什么要它：harness 的热重载只覆盖三处 —— 根目录 *.py 与 harness/*.py（改动 = 整进程
重启，会掐断在途对话）、skills/ 与 plugins/（技能/插件自身重载）。tools/ 下的模块不在
扫描范围：server.py 里是函数内 `from tools.longrun.status_view import ...`，sys.modules
命中即返回，服务进程永远跑旧代码，只能靠重启。

实测（2026-09-13）：status_view.py 08:45:23 改完，09:00 /api/tasks 仍返回旧标题
「长跑引擎 · 第 9 轮 · biz-negotiate」，而同刻本地直接调 snapshot() 已经是
「长跑引擎 · 第 10 轮进行中 · longrun-engine（已跑 1分12秒）」。

用法：改完 tools/ 下的代码 → POST /api/harness/skills/hotmod/reload（或重载本技能）
→ targets.json 列出的目录被逐出 sys.modules，下一次函数内 import 读到新代码。

复用 harness._reload.evict_dir_modules：它带「必须是项目根的真子目录」校验（缺 path
字段时会把整个项目的模块从 sys.modules 摘掉，实测误清过 harness.plugins），且会清
__pycache__，避免同秒/同长度编辑时 pyc 校验误判为未变。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger("skill.hotmod")

ROOT = Path(__file__).resolve().parents[2]
TARGETS = Path(__file__).resolve().parent / "targets.json"

PROMPT = (
    "【技能 服务内热重载】改了 tools/ 下的模块源码，服务进程不会重新导入"
    "（只有根目录 *.py 与 harness/*.py 的改动触发整进程重启，会掐断在途对话）。"
    "用 hotmod_reload 把 targets.json 列出的目录从 sys.modules 逐出，下一次 import 即读到新代码。"
)

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "hotmod_reload",
            "description": "把指定目录下的 Python 模块从服务进程 sys.modules 逐出（并清 __pycache__），"
                           "让下一次 import 读到磁盘上的新代码，免重启生效。",
            "parameters": {
                "type": "object",
                "properties": {
                    "dirs": {
                        "type": "string",
                        "description": "逗号分隔的目录（相对项目根，如 tools/longrun）；留空用 targets.json 清单",
                    }
                },
            },
        },
    }
]


def _target_dirs() -> list[str]:
    try:
        data = json.loads(TARGETS.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("hotmod 读 targets.json 失败: %s", e)
        return []
    dirs = data.get("dirs") if isinstance(data, dict) else data
    return [str(d).strip() for d in (dirs or []) if str(d).strip()]


def reload_dirs(dirs: list[str]) -> dict:
    """逐出 dirs 下所有模块的缓存。返回 {目录: 是否执行成功}。"""
    from harness._reload import evict_dir_modules

    out: dict[str, bool] = {}
    for d in dirs:
        p = (ROOT / str(d).strip().lstrip("/")).resolve()
        out[str(d)] = bool(evict_dir_modules(p, ROOT, label="hotmod"))
    return out


def execute(name: str, args: dict):
    if name != "hotmod_reload":
        return {"ok": False, "error": f"未知工具: {name}"}
    raw = str((args or {}).get("dirs") or "").strip()
    dirs = [d.strip() for d in raw.split(",") if d.strip()] or _target_dirs()
    if not dirs:
        return {"ok": False, "error": "没有可刷新的目录（targets.json 为空）"}
    res = reload_dirs(dirs)
    return {"ok": any(res.values()), "dirs": res,
            "hint": "已逐出；下一次调用该模块的函数时会重新 import 磁盘上的新代码"}


def on_load(ctx=None):
    """加载/重载本技能 = 一次刷新：把 targets.json 里的目录从 sys.modules 摘掉。

    这是免重启刷新的触发入口 —— POST /api/harness/skills/hotmod/reload 即可。
    """
    dirs = _target_dirs()
    if not dirs:
        return
    res = reload_dirs(dirs)
    logger.info("hotmod 已刷新模块缓存: %s", res)
