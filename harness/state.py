"""harness 启停状态的持久化存储（harness_state.json）。

技能/插件的 enable/disable 一旦设置，重启后依然生效。
写盘使用「临时文件 + 原子替换」，避免写一半损坏配置。
"""
from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger("harness.state")


class StateStore:
    """轻量 key-value 状态存储：kind(name: bool) + updated_at。"""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._data = self._load()

    def _load(self) -> dict:
        default = {"skills": {}, "plugins": {}, "updated_at": 0}
        if not self.path.exists():
            return default
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return default
            data.setdefault("skills", {})
            data.setdefault("plugins", {})
            if not isinstance(data["skills"], dict) or not isinstance(data["plugins"], dict):
                return default
            return data
        except Exception as e:
            logger.warning("加载 %s 失败（按默认状态继续）: %s", self.path.name, e)
            return default

    def is_enabled(self, kind: str, name: str, default: bool = True) -> bool:
        """读取启停状态；从未设置过时返回 default（清单里的默认值）。"""
        with self._lock:
            store = self._data.get(kind)
            if not isinstance(store, dict):
                return default
            v = store.get(name)
            return bool(v) if isinstance(v, bool) else default

    def set_enabled(self, kind: str, name: str, enabled: bool) -> None:
        """设置启停状态并持久化。"""
        with self._lock:
            store = self._data.setdefault(kind, {})
            store[name] = bool(enabled)
            self._data["updated_at"] = time.time()
            self._save_locked()

    def all_enabled(self, kind: str) -> dict:
        """返回 kind 下全部已记录状态 {name: bool}。"""
        with self._lock:
            store = self._data.get(kind)
            return dict(store) if isinstance(store, dict) else {}

    def _save_locked(self) -> None:
        try:
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2)
            tmp.replace(self.path)
        except Exception as e:
            logger.warning("保存 %s 失败: %s", self.path.name, e)
