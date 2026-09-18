# -*- coding: utf-8 -*-
"""视频收藏夹共享库 —— 分类 + 收藏视频的本地持久化存储。

与 music_lib.py 的歌单同款架构：数据保存在项目根目录的
video_favorites.json（本地文件，不依赖任何外部服务），由
server.py 的 /api/video_hub/api/favorites* 端点与前端共用。

数据结构：
  {
    "categories": [{"id", "name", "created"}],
    "favorites":  [{"id", "video": {...}, "category_id": str|null, "created"}]
  }
收藏 id 由 webpage_url 哈希而来 → 同一视频天然去重（幂等）。
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
import uuid
from pathlib import Path

import user_store

BASE_DIR = Path(__file__).resolve().parent
FAVS_NAME = 'video_favorites.json'
# 全局文件（uid 为空 = 本机主人 / 无用户上下文），保留常量供外部引用
FAVS_FILE = BASE_DIR / FAVS_NAME

_save_lock = threading.RLock()


def _load(uid=None) -> dict:
    path = user_store.user_file(FAVS_NAME, uid)
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except Exception:
        return {'categories': [], 'favorites': []}
    if not isinstance(data, dict):
        return {'categories': [], 'favorites': []}
    data.setdefault('categories', [])
    data.setdefault('favorites', [])
    return data


def _save(data: dict, uid=None) -> None:
    path = user_store.user_file(FAVS_NAME, uid)
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


def _fav_id(webpage_url: str) -> str:
    return hashlib.sha1(str(webpage_url).encode('utf-8')).hexdigest()[:12]


# ---------------- 查询 ----------------

def list_all(uid=None) -> dict:
    """返回完整收藏数据（categories + favorites）。"""
    with _save_lock:
        return _load(uid)


def get_favorite(fid: str, uid=None) -> dict | None:
    with _save_lock:
        for f in _load(uid)['favorites']:
            if f['id'] == fid:
                return f
    return None


# ---------------- 分类管理 ----------------

def create_category(name: str, uid=None) -> dict:
    name = str(name).strip()
    if not name:
        raise ValueError('分类名不能为空')
    cid = uuid.uuid4().hex[:12]
    cat = {'id': cid, 'name': name, 'created': int(time.time())}
    with _save_lock:
        data = _load(uid)
        for c in data['categories']:
            if c['name'] == name:
                raise ValueError(f'分类《{name}》已存在')
        data['categories'].append(cat)
        _save(data, uid)
    return cat


def rename_category(cid: str, name: str, uid=None) -> dict | None:
    name = str(name).strip()
    if not name:
        raise ValueError('分类名不能为空')
    with _save_lock:
        data = _load(uid)
        for c in data['categories']:
            if c['name'] == name and c['id'] != cid:
                raise ValueError(f'分类《{name}》已存在')
        for c in data['categories']:
            if c['id'] == cid:
                c['name'] = name
                _save(data, uid)
                return c
    return None


def delete_category(cid: str, uid=None) -> bool:
    """删除分类；该分类下的收藏自动归入「未分类」（category_id=None）。"""
    with _save_lock:
        data = _load(uid)
        before = len(data['categories'])
        data['categories'] = [c for c in data['categories'] if c['id'] != cid]
        moved = 0
        for f in data['favorites']:
            if f.get('category_id') == cid:
                f['category_id'] = None
                moved += 1
        if len(data['categories']) == before:
            return False
        _save(data, uid)
        return True


# ---------------- 收藏管理 ----------------

def add_favorite(video: dict, category_id: str | None = None, uid=None) -> dict:
    """收藏一个视频（按 webpage_url 幂等去重，已收藏直接返回现有项）。

    video 用白名单字段保存，丢弃未知字段；category_id 不合法时归入未分类。
    """
    url = str(video.get('webpage_url') or '').strip()
    if not url:
        raise ValueError('视频缺少 webpage_url，无法收藏')
    clean = {
        'title': str(video.get('title') or '未知视频'),
        'webpage_url': url,
        'platform': str(video.get('platform') or ''),
        'uploader': str(video.get('uploader') or ''),
        'duration': video.get('duration') or 0,
        'view_count': video.get('view_count') or 0,
        'thumbnail': str(video.get('thumbnail') or ''),
    }
    fid = _fav_id(url)
    with _save_lock:
        data = _load(uid)
        for f in data['favorites']:
            if f['id'] == fid:
                return {'favorite': f, 'existed': True}
        if category_id and not any(c['id'] == category_id for c in data['categories']):
            category_id = None
        fav = {'id': fid, 'video': clean, 'category_id': category_id,
               'created': int(time.time())}
        data['favorites'].append(fav)
        _save(data, uid)
        return {'favorite': fav, 'existed': False}


def remove_favorite(fid: str, uid=None) -> bool:
    with _save_lock:
        data = _load(uid)
        before = len(data['favorites'])
        data['favorites'] = [f for f in data['favorites'] if f['id'] != fid]
        if len(data['favorites']) == before:
            return False
        _save(data, uid)
        return True


def move_favorite(fid: str, category_id: str | None, uid=None) -> bool:
    """把收藏移动到指定分类；category_id=None 归入未分类。"""
    with _save_lock:
        data = _load(uid)
        ok_cats = {c['id'] for c in data['categories']}
        if category_id is not None and category_id not in ok_cats:
            raise ValueError('分类不存在')
        for f in data['favorites']:
            if f['id'] == fid:
                f['category_id'] = category_id
                _save(data, uid)
                return True
    return False
