# -*- coding: utf-8 -*-
"""视频观看历史共享库 —— 本地持久化存储。

与 video_fav_lib.py 同款架构：数据保存在项目根目录的
video_history.json（本地文件，不依赖任何外部服务），由
server.py 的 /api/video_hub/api/history* 端点与前端共用。

数据结构：
  {
    "history": [{"id", "video": {...}, "watched_at"}]
  }
历史 id 由 webpage_url 哈希而来 → 同一视频重复观看自动去重并更新时间戳置顶。
最多保留最近 MAX_HISTORY（1000）条，超出自动裁剪最旧记录。
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from pathlib import Path

import user_store

BASE_DIR = Path(__file__).resolve().parent
HIST_NAME = 'video_history.json'
# 全局文件（uid 为空 = 本机主人 / 无用户上下文），保留常量供外部引用
HIST_FILE = BASE_DIR / HIST_NAME

_save_lock = threading.RLock()

MAX_HISTORY = 1000


def _load(uid=None) -> dict:
    path = user_store.user_file(HIST_NAME, uid)
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except Exception:
        return {'history': []}
    if not isinstance(data, dict):
        return {'history': []}
    data.setdefault('history', [])
    return data


def _save(data: dict, uid=None) -> None:
    path = user_store.user_file(HIST_NAME, uid)
    tmp = str(path) + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    try:
        tmp_path = Path(tmp)
        tmp_path.replace(path)  # 原子替换，防写一半损坏
    except Exception:
        # 极端情况（如 Windows 文件占用）：直接覆盖写
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)


def _hist_id(webpage_url: str) -> str:
    return hashlib.sha1(str(webpage_url).encode('utf-8')).hexdigest()[:12]


# ---------------- 查询 ----------------

def list_history(limit: int = 200, uid=None) -> list:
    """按观看时间倒序返回历史（最新在前）。"""
    with _save_lock:
        data = _load(uid)
        items = sorted(data['history'],
                       key=lambda h: h.get('watched_at') or 0, reverse=True)
        if limit and limit > 0:
            items = items[:limit]
        return items


# ---------------- 记录管理 ----------------

def add_history(video: dict, uid=None) -> dict:
    """记录一次观看（按 webpage_url 幂等去重：重复观看更新时间戳并置顶）。

    超出 MAX_HISTORY 自动裁剪最旧记录。video 用白名单字段保存，丢弃未知字段。
    """
    url = str(video.get('webpage_url') or '').strip()
    if not url:
        raise ValueError('视频缺少 webpage_url，无法记录')
    clean = {
        'title': str(video.get('title') or '未知视频'),
        'webpage_url': url,
        'platform': str(video.get('platform') or ''),
        'uploader': str(video.get('uploader') or ''),
        'duration': video.get('duration') or 0,
        'view_count': video.get('view_count') or 0,
        'thumbnail': str(video.get('thumbnail') or ''),
    }
    hid = _hist_id(url)
    now = int(time.time())
    with _save_lock:
        data = _load(uid)
        existed = False
        for h in data['history']:
            if h['id'] == hid:
                h['video'] = clean
                h['watched_at'] = now
                existed = True
                break
        if not existed:
            data['history'].append({'id': hid, 'video': clean, 'watched_at': now})
        # 按时间倒序 + 裁剪到 MAX_HISTORY
        data['history'].sort(key=lambda h: h.get('watched_at') or 0, reverse=True)
        if len(data['history']) > MAX_HISTORY:
            data['history'] = data['history'][:MAX_HISTORY]
        _save(data, uid)
        return {'history': data['history'][0], 'existed': existed}


def remove_history(hid: str, uid=None) -> bool:
    with _save_lock:
        data = _load(uid)
        before = len(data['history'])
        data['history'] = [h for h in data['history'] if h['id'] != hid]
        if len(data['history']) == before:
            return False
        _save(data, uid)
        return True


def clear_history(uid=None) -> bool:
    with _save_lock:
        data = _load(uid)
        if not data['history']:
            return False
        data['history'] = []
        _save(data, uid)
        return True