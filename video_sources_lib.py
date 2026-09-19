# -*- coding: utf-8 -*-
"""视频源管理共享库 —— 内置平台启停 + 自定义视频源，本地持久化。

与 video_fav_lib.py / video_history_lib.py 同款架构：数据保存在项目
根目录的 video_sources.json（本地文件，不依赖外部服务），由 server.py
的 /api/video_hub/api/sources* 端点与前端共用。

数据结构：
  {
    "builtin": {"bilibili": true, "acfun": true, "youtube": true},
    "custom": [{"id", "name", "search_url", "enabled", "created"}]
  }

说明：
  * "all"（聚合搜索）是模式不是平台，始终可用，不参与启停；
  * 内置平台可单独禁用 → 搜索页 chips 不显示、聚合搜索/热门也不包含它；
  * 自定义视频源：name + search_url（含 {kw} 占位符，如
    https://www.example.com/search?q={kw}），搜索时后端抓取该页并
    提取视频链接，点播走 yt-dlp 通用解析。
"""
from __future__ import annotations

import json
import re
import threading
import time
import uuid
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
SOURCES_FILE = BASE_DIR / 'video_sources.json'

_save_lock = threading.RLock()

# 内置平台（与 video_lib.PLATFORMS 一一对应；all 为聚合模式不在此列）
BUILTIN_PLATFORMS = [
    {"id": "bilibili", "name": "哔哩哔哩", "hint": "关键词搜索 + 点播（含知识区/公开课/纪录片等）"},
    {"id": "acfun", "name": "AcFun", "hint": "关键词搜索 + 点播"},
    {"id": "youtube", "name": "YouTube", "hint": "全球最大视频站，需 fq 代理"},
]

DEFAULT_BUILTIN = {p["id"]: True for p in BUILTIN_PLATFORMS}


def _load() -> dict:
    try:
        with open(SOURCES_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    builtin = dict(DEFAULT_BUILTIN)
    if isinstance(data.get("builtin"), dict):
        for k, v in data["builtin"].items():
            if k in builtin:
                builtin[k] = bool(v)
    custom = data.get("custom")
    if not isinstance(custom, list):
        custom = []
    return {"builtin": builtin, "custom": custom}


def _save(data: dict) -> None:
    tmp = str(SOURCES_FILE) + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    try:
        Path(tmp).replace(SOURCES_FILE)
    except Exception:
        with open(SOURCES_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)


# ---------------- 查询 ----------------

def list_sources() -> dict:
    """返回全部视频源（内置 + 自定义），带 enabled 状态。"""
    with _save_lock:
        data = _load()
        builtin = []
        for p in BUILTIN_PLATFORMS:
            builtin.append({
                "id": p["id"], "name": p["name"], "hint": p["hint"],
                "enabled": data["builtin"].get(p["id"], True),
            })
        return {"builtin": builtin, "custom": data["custom"]}


def enabled_ids() -> list:
    """返回当前启用的平台 id 列表（不含 all；自定义源只返回启用的）。"""
    with _save_lock:
        data = _load()
        ids = [pid for pid, on in data["builtin"].items() if on]
        ids += [c["id"] for c in data["custom"] if c.get("enabled")]
        return ids


def get_custom(cid: str) -> dict | None:
    with _save_lock:
        data = _load()
        for c in data["custom"]:
            if c["id"] == cid:
                return c
        return None


# ---------------- 内置平台启停 ----------------

def set_builtin(pid: str, enabled: bool) -> bool:
    """启用/禁用内置平台。返回 False = 平台不存在。"""
    if pid not in DEFAULT_BUILTIN:
        return False
    with _save_lock:
        data = _load()
        data["builtin"][pid] = bool(enabled)
        _save(data)
        return True


# ---------------- 自定义视频源 ----------------

def _validate_custom(name: str, search_url: str) -> str | None:
    """校验自定义源，返回错误信息或 None。"""
    name = (name or '').strip()
    search_url = (search_url or '').strip()
    if not name:
        return '名称不能为空'
    if len(name) > 20:
        return '名称最多 20 个字符'
    if not search_url:
        return '搜索链接不能为空'
    if '{kw}' not in search_url:
        return '搜索链接必须包含 {kw} 占位符（如 https://www.example.com/search?q={kw}）'
    if not re.match(r'^https?://', search_url):
        return '搜索链接必须以 http:// 或 https:// 开头'
    return None


def add_custom(name: str, search_url: str) -> dict:
    """新增自定义视频源。返回 {"ok": True, "source": {...}} 或抛 ValueError。"""
    err = _validate_custom(name, search_url)
    if err:
        raise ValueError(err)
    src = {
        "id": "custom_" + uuid.uuid4().hex[:8],
        "name": name.strip(),
        "search_url": search_url.strip(),
        "enabled": True,
        "created": int(time.time()),
    }
    with _save_lock:
        data = _load()
        data["custom"].append(src)
        _save(data)
        return src


def update_custom(cid: str, name: str | None = None,
                  search_url: str | None = None,
                  enabled: bool | None = None) -> dict | None:
    """更新自定义源（字段可部分更新）。返回更新后的源或 None（不存在）。"""
    with _save_lock:
        data = _load()
        for c in data["custom"]:
            if c["id"] != cid:
                continue
            if name is not None:
                name = str(name).strip()
                if not name:
                    raise ValueError('名称不能为空')
                c["name"] = name[:20]
            if search_url is not None:
                search_url = str(search_url).strip()
                err = _validate_custom(c["name"], search_url)
                if err:
                    raise ValueError(err)
                c["search_url"] = search_url
            if enabled is not None:
                c["enabled"] = bool(enabled)
            _save(data)
            return c
        return None


def remove_custom(cid: str) -> bool:
    with _save_lock:
        data = _load()
        before = len(data["custom"])
        data["custom"] = [c for c in data["custom"] if c["id"] != cid]
        if len(data["custom"]) == before:
            return False
        _save(data)
        return True
