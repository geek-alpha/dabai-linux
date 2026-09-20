"""确认卡审计：允许过什么、拒绝过什么，事后可查。

用户真正要回答的问题不是「闸门拦了多少次」，而是「我什么时候手滑放过了一条危险命令」——
这要求每一次确认决定都留痕（工具、参数、原因、决定、时间），刷新页面还能翻回来。

与永久白名单（settings.json）分开存：白名单是「当前生效的信任」，审计是「历史流水」，
撤销白名单不该抹掉「曾经允许过」这件事。

边界：
  · 上限 50 条，超出丢最旧的；
  · 落盘失败只警告，绝不影响闸门放行判定（审计是旁路，不是判据）；
  · 未知 rid 的 mark 返回 False（弹卡记录丢失时静默忽略，不误伤新卡）。
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("gate_audit")

AUDIT_FILE = Path(__file__).resolve().parent / "data" / "gate_audit.json"
MAX_AUDIT = 50
ARGS_PREVIEW = 200

_LOCK = threading.Lock()
_ENTRIES: List[Dict[str, Any]] = []
_LOADED = False

_DECISION_LABEL = {
    "pending": "⏳ 等你决定",
    "allow": "✅ 允许一次",
    "always": "✓✓ 总是允许（已进永久白名单）",
    "deny": "⛔ 拒绝",
}


def _load() -> None:
    global _LOADED
    if _LOADED:
        return
    _LOADED = True
    try:
        if AUDIT_FILE.exists():
            data = json.loads(AUDIT_FILE.read_text(encoding="utf-8"))
            if isinstance(data, list):
                _ENTRIES.extend([d for d in data if isinstance(d, dict)][-MAX_AUDIT:])
    except Exception as e:
        logger.warning("确认卡审计读取失败: %s", e)


def _save() -> None:
    try:
        AUDIT_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = str(AUDIT_FILE) + ".tmp"
        Path(tmp).write_text(json.dumps(_ENTRIES, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, AUDIT_FILE)
    except Exception as e:
        logger.warning("确认卡审计落盘失败: %s", e)


def _preview(arguments: Any) -> str:
    try:
        if isinstance(arguments, dict):
            raw = json.dumps(arguments, ensure_ascii=False, default=str)
        else:
            raw = str(arguments)
    except Exception:
        raw = str(arguments)
    return raw[:ARGS_PREVIEW]


def record(rid: str, tool: str, arguments: Any, reason: str) -> Dict[str, Any]:
    """登记一次确认卡弹出（决定之前就留痕——弹了卡没人理也该看得见）。"""
    entry: Dict[str, Any] = {
        "id": str(rid or ""),
        "tool": str(tool or ""),
        "args": _preview(arguments),
        "reason": str(reason or "")[:80],
        "decision": "pending",
        "perm_key": "",
        "ts": time.time(),
        "ts_done": 0,
    }
    with _LOCK:
        _load()
        _ENTRIES.append(entry)
        del _ENTRIES[:-MAX_AUDIT]
        _save()
    return entry


def mark(rid: str, decision: str, perm_key: str = "") -> bool:
    """记录用户对确认卡的决定：allow / always / deny。未知 rid 返回 False。"""
    rid = str(rid or "")
    with _LOCK:
        _load()
        for e in reversed(_ENTRIES):
            if e.get("id") == rid:
                e["decision"] = str(decision or "")
                e["perm_key"] = str(perm_key or "")
                e["ts_done"] = time.time()
                _save()
                return True
    return False


def _desc(e: Dict[str, Any]) -> str:
    """翻译成人话，给管理面板的行首说明。"""
    tool = str(e.get("tool") or "未知工具")
    decision = str(e.get("decision") or "")
    label = _DECISION_LABEL.get(decision, decision or "?")
    return f"{label}：{tool}"


def list_audit(limit: int = 20) -> List[Dict[str, Any]]:
    """最近的确认卡记录（新的在前），供管理面板展示。"""
    with _LOCK:
        _load()
        try:
            n = int(limit)
        except (TypeError, ValueError):
            n = 20
        n = max(1, min(n, MAX_AUDIT))
        out: List[Dict[str, Any]] = []
        for e in _ENTRIES[-n:][::-1]:
            item = dict(e)
            item["desc"] = _desc(e)
            out.append(item)
        return out


def clear() -> int:
    """清空审计流水，返回清掉的条数。"""
    with _LOCK:
        _load()
        n = len(_ENTRIES)
        _ENTRIES.clear()
        _save()
    return n
