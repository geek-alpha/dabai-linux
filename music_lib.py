# -*- coding: utf-8 -*-
"""在线音乐共享库 —— 酷我 / 网易云聚合搜索、直链解析、用户歌单存储。

被 skills/music/skill.py（Agent 工具）与 server.py（前端 REST API）共用，
保证缓存在单进程内只此一份。
"""
from __future__ import annotations

import ast
import html
import json
import re
import threading
import time
import uuid
from pathlib import Path

import requests

BASE_DIR = Path(__file__).resolve().parent
PLAYLISTS_FILE = BASE_DIR / 'music_playlists.json'

UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36')

_KW_HEADERS = {'Referer': 'http://www.kuwo.cn/', 'User-Agent': UA}
_U_ESCAPE = re.compile(r'\\u([0-9a-fA-F]{4})')
_NT_BASE = 'https://music.163.com'
_NT_HDR = {'Referer': 'https://music.163.com/', 'Origin': 'https://music.163.com'}

_BOARDS = [
    ('3778678', '云音乐热歌榜'),
    ('3779629', '云音乐新歌榜'),
    ('19723756', '云音乐飙升榜'),
    ('2884035', '云音乐原创榜'),
]

_HTTP = requests.Session()
_HTTP.headers.update({'User-Agent': UA})


def _get(url, **kw):
    kw.setdefault('timeout', (5, 15))
    return _HTTP.get(url, **kw)


def _post_json(url, data=None, headers=None):
    r = _HTTP.post(url, data=data, headers=headers or {})
    r.encoding = 'utf-8'
    return r.json()


def _clean(v):
    """清理酷我 r.s 返回文本：`\\u0026` 字面量转义 + HTML 实体"""
    s = _U_ESCAPE.sub(lambda m: chr(int(m.group(1), 16)), str(v or ''))
    return html.unescape(s)


def _to_sec(v):
    """'269' / '4:12' / 269 → 秒"""
    if v in (None, ''):
        return 0
    s = str(v).strip()
    try:
        if ':' in s:
            m, sec = s.split(':')[:2]
            return int(m) * 60 + int(float(sec))
        return int(float(s))
    except Exception:
        return 0


class _TTLCache:
    """极简线程安全 TTL 缓存"""

    def __init__(self, ttl: float):
        self._ttl = ttl
        self._data: dict[str, tuple[float, object]] = {}
        self._lock = threading.Lock()

    def get(self, key):
        with self._lock:
            hit = self._data.get(str(key))
            if hit and time.time() < hit[0]:
                return hit[1]
            if hit:
                del self._data[str(key)]
        return None

    def set(self, key, value):
        with self._lock:
            self._data[str(key)] = (time.time() + self._ttl, value)


_url_cache = _TTLCache(900)          # 直链缓存 15 分钟
_search_cache = _TTLCache(600)       # 搜索缓存 10 分钟

# 最近一次搜索/歌单结果缓存（序号 -> 歌曲），让 AI 可以直接说"播放第2首"
_last_results: dict[int, dict] = {}
_last_lock = threading.RLock()

# ---------------- 酷我 ----------------


def kw_cover(sid):
    return (f'http://artistpicserver.kuwo.cn/pic.web?corp=kuwo&type=rid_pic'
            f'&pictype=500&size=500&rid={sid}')


def kw_search(kw, limit):
    """酷我搜索：r.s 老接口优先，失败回退 kw_token API。"""
    songs = []
    try:
        params = {'all': kw, 'ft': 'music', 'client': 'kt', 'itemset': 'web_2013',
                  'pn': 0, 'rn': limit, 'rformat': 'json', 'encoding': 'utf8'}
        r = _get('http://search.kuwo.cn/r.s', params=params, headers=_KW_HEADERS)
        text = r.text.strip()
        try:
            data = json.loads(text, strict=False)
        except Exception:
            data = ast.literal_eval(text)  # 老接口实际是单引号 Python 字面量
        for it in (data.get('abslist') or [])[:limit]:
            sid = str(it.get('MUSICRID') or it.get('DC_TARGETID') or '')
            sid = sid.replace('MUSIC_', '').strip()
            if not sid:
                continue
            short = it.get('web_albumpic_short') or ''
            cover = ('http://img1.kwcdn.kuwo.cn/star/albumcover/' + short) if short else kw_cover(sid)
            songs.append({'source': 'kuwo', 'id': sid,
                          'name': _clean(it.get('SONGNAME')) or '未知歌曲',
                          'artists': _clean(it.get('ARTIST')).replace('&', '/'),
                          'album': _clean(it.get('ALBUM')),
                          'duration': _to_sec(it.get('DURATION')),
                          'vip': False, 'cover': cover})
    except Exception:
        songs = []
    if songs:
        return songs
    # 回退 API（需 csrf token cookie）
    token = uuid.uuid4().hex
    r = _get('http://www.kuwo.cn/api/v1/search/music',
             params={'key': kw, 'pn': 1, 'rn': limit, 'httpsStatus': 1},
             headers={**_KW_HEADERS, 'csrf': token, 'Cookie': f'kw_token={token}'})
    r.encoding = 'utf-8'
    for it in ((r.json().get('data') or {}).get('list') or []):
        sid = str(it.get('rid') or '')
        if not sid:
            continue
        songs.append({'source': 'kuwo', 'id': sid,
                      'name': it.get('name') or '未知歌曲',
                      'artists': it.get('artist') or '',
                      'album': it.get('album') or '',
                      'duration': _to_sec(it.get('duration') or it.get('songTimeMinutes')),
                      'vip': False, 'cover': it.get('pic') or kw_cover(sid)})
    return songs


def kw_resolve(sid):
    """酷我直链：antiserver 多候选，OGG 优先。"""
    for tmpl in (
            'http://antiserver.kuwo.cn/anti.s?type=convert_url3&rid=MUSIC_{rid}&format=ogg&response=url',
            'http://antiserver.kuwo.cn/anti.s?type=convert_url&rid=MUSIC_{rid}&format=ogg&response=url',
            'http://antiserver.kuwo.cn/anti.s?type=convert_url3&rid=MUSIC_{rid}&format=mp3&response=url',
            'http://antiserver.kuwo.cn/anti.s?type=convert_url3&rid=MUSIC_{rid}&format=aac&response=url',
            'http://antiserver.kuwo.cn/anti.s?type=convert_url&rid=MUSIC_{rid}&format=mp3&response=url'):
        try:
            r = _get(tmpl.format(rid=sid), headers=_KW_HEADERS)
            t = r.text.strip().strip('"').strip("'")
            if t.startswith('http'):
                return t
        except Exception:
            continue
    return None


def kw_lyric(sid):
    try:
        r = _get(f'http://m.kuwo.cn/newh5/singlesonginfo?musicId={sid}',
                 headers={'Referer': 'http://m.kuwo.cn/', 'User-Agent': UA})
        d = (r.json().get('data') or {})
        return {'lyric': d.get('lyrics') or d.get('lyric') or '', 'tlyric': ''}
    except Exception:
        return {'lyric': '', 'tlyric': ''}

# ---------------- 网易云 ----------------


def nt_search(kw, page, limit, stype=1):
    """网易云 web 搜索。stype: 1=单曲 1000=歌单"""
    data = _post_json(_NT_BASE + '/api/search/get/web',
                      data={'s': kw, 'type': stype, 'offset': (page - 1) * limit,
                            'limit': limit, 'total': 'true'},
                      headers=_NT_HDR)
    return data.get('result') or {}


def nt_song_from_search(it):
    album = it.get('album') or {}
    return {'source': 'netease', 'id': str(it.get('id')),
            'name': it.get('name') or '',
            'artists': '/'.join(a.get('name', '') for a in it.get('artists') or []),
            'album': album.get('name') or '',
            'duration': round((it.get('duration') or 0) / 1000),
            'vip': it.get('fee') in (1, 4),
            'cover': album.get('picUrl') or ''}


def nt_songs_by_ids(ids):
    out = []
    chunk = [int(x) for x in ids[:100] if str(x).isdigit()]
    if not chunk:
        return out
    c = json.dumps([{'id': x} for x in chunk])
    data = _post_json(_NT_BASE + '/api/v3/song/detail', data={'c': c}, headers=_NT_HDR)
    for it in (data.get('songs') or []):
        al = it.get('al') or {}
        out.append({'source': 'netease', 'id': str(it.get('id')),
                    'name': it.get('name') or '',
                    'artists': '/'.join(a.get('name', '') for a in it.get('ar') or []),
                    'album': al.get('name') or '',
                    'duration': round((it.get('dt') or 0) / 1000),
                    'vip': it.get('fee') in (1, 4),
                    'cover': al.get('picUrl') or ''})
    return out


def nt_resolve(sid):
    """网易云直链：outer/url 302 解析一次拿到 CDN 地址。"""
    outer = f'{_NT_BASE}/song/media/outer/url?id={sid}.mp3'
    try:
        r = _get(outer, allow_redirects=False, headers=_NT_HDR)
    except Exception:
        return None
    if r.status_code in (301, 302, 303, 307, 308):
        loc = r.headers.get('Location') or ''
        if not loc.startswith('http'):
            return None
        # 排除指向 404 页面/非音频内容的无效跳转（VIP、版权受限、已下架常见）
        if '/404' in loc or loc.rstrip('/').endswith(('.html', '.png', '.jpg', '.jpeg')):
            return None
        return loc
    if r.status_code == 200 and 'audio' in (r.headers.get('Content-Type') or ''):
        return outer
    return None


def nt_lyric(sid):
    try:
        d = _get(_NT_BASE + '/api/song/lyric',
                 params={'id': sid, 'lv': 1, 'kv': 1, 'tv': -1},
                 headers=_NT_HDR).json()
        return {'lyric': (d.get('lrc') or {}).get('lyric') or '',
                'tlyric': (d.get('tlyric') or {}).get('lyric') or ''}
    except Exception:
        return {'lyric': '', 'tlyric': ''}


def nt_playlist(pid, limit):
    r = _get(_NT_BASE + '/api/playlist/detail',
             params={'id': pid, 'n': min(limit, 100)}, headers=_NT_HDR)
    d = r.json()
    pl = d.get('playlist') or d.get('result') or {}
    tracks = pl.get('tracks') or []
    songs = [nt_song_from_search(t) for t in tracks[:limit]]
    if not songs:  # 详情里只有 trackIds 时批量补全
        ids = [str(t.get('id')) for t in (pl.get('trackIds') or [])][:limit]
        songs = nt_songs_by_ids(ids)
    return {'name': pl.get('name') or str(pid), 'songs': songs}

# ---------------- 聚合 ----------------


def _https_upgrade(url):
    """把上游返回的 http:// 直链升级为 https://（页面是 HTTPS，混合内容会被浏览器拦截）。"""
    if url and url.startswith('http://'):
        return 'https://' + url[len('http://'):]
    return url


def resolve(source, sid):
    """统一直链解析（带 TTL 缓存）。失败返回 None。"""
    key = f'{source}/{sid}'
    hit = _url_cache.get(key)
    if hit is not None:
        return hit
    url = kw_resolve(sid) if source == 'kuwo' else (
        nt_resolve(sid) if source == 'netease' else None)
    url = _https_upgrade(url)
    _url_cache.set(key, url)  # None 也缓存，避免反复打失效接口
    return url


def search_all(kw, limit=8):
    """双源并行聚合搜索，返回合并后的歌曲列表（酷我在前）。"""
    cache_key = f'{kw}#{limit}'
    cached = _search_cache.get(cache_key)
    if cached is not None:
        return cached
    import concurrent.futures as cf
    results = {}
    with cf.ThreadPoolExecutor(max_workers=2) as ex:
        f_kw = ex.submit(kw_search, kw, limit)
        f_nt = ex.submit(lambda: [
            nt_song_from_search(it) for it in (nt_search(kw, 1, limit).get('songs') or [])])
        for fut, name in ((f_kw, 'kuwo'), (f_nt, 'netease')):
            try:
                results[name] = fut.result(timeout=25)
            except Exception:
                results[name] = []
    merged = results.get('kuwo', []) + results.get('netease', [])
    _search_cache.set(cache_key, merged)
    return merged


def remember_results(songs: list[dict]):
    """把一份歌曲列表按序号缓存进 _last_results（AI 用 index 点播）。"""
    with _last_lock:
        _last_results.clear()
        for i, s in enumerate(songs, 1):
            _last_results[i] = dict(s)


def pick_song(args: dict):
    """从 index / source+song_id 解析出一首歌。错误时返回错误字符串。"""
    idx = args.get('index')
    if idx is not None and _last_results:
        try:
            song = _last_results.get(int(idx))
        except (TypeError, ValueError):
            song = None
        if song:
            return song
        return f'序号 {idx} 不在最近的列表里，请先 music_search 再选择。'
    source = str(args.get('source') or '').strip().lower()
    sid = str(args.get('song_id') or '').strip()
    if source in ('kuwo', 'netease') and sid:
        return {'source': source, 'id': sid,
                'name': str(args.get('title') or ''), 'artists': str(args.get('artist') or '')}
    return '需要提供 index（最近列表的序号），或同时提供 source 与 song_id。'

# ---------------- 用户歌单存储 ----------------


def _load_playlists() -> list[dict]:
    try:
        with open(PLAYLISTS_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _save_playlists(pls: list[dict]) -> None:
    with open(PLAYLISTS_FILE, 'w', encoding='utf-8') as f:
        json.dump(pls, f, ensure_ascii=False, indent=2)


def list_playlists() -> list[dict]:
    pls = _load_playlists()
    return [{'id': p['id'], 'name': p['name'], 'created': p.get('created', 0),
             'song_count': len(p.get('songs') or [])} for p in pls]


def get_playlist(pid: str) -> dict | None:
    for p in _load_playlists():
        if p['id'] == pid:
            return p
    return None


def create_playlist(name: str) -> dict:
    name = str(name).strip()
    if not name:
        raise ValueError('歌单名不能为空')
    pid = uuid.uuid4().hex[:12]
    pls = _load_playlists()
    pl = {'id': pid, 'name': name, 'created': int(time.time()), 'songs': []}
    pls.append(pl)
    _save_playlists(pls)
    return pl


def add_song(pid: str, song: dict) -> dict | None:
    song = {'source': str(song.get('source') or ''),
            'id': str(song.get('id') or ''),
            'name': str(song.get('name') or ''),
            'artists': str(song.get('artists') or '')}
    if not song['source'] or not song['id']:
        raise ValueError('歌曲缺少 source / id')
    pls = _load_playlists()
    for pl in pls:
        if pl['id'] != pid:
            continue
        songs = pl.get('songs')
        if songs is None:
            songs = pl['songs'] = []
        for s in songs:
            if s['source'] == song['source'] and s['id'] == song['id']:
                return pl  # 已存在，幂等
        songs.append(song)
        _save_playlists(pls)
        return pl
    return None


def remove_song(pid: str, song_id: str) -> bool:
    pls = _load_playlists()
    for pl in pls:
        if pl['id'] != pid:
            continue
        songs = pl.get('songs') or []
        before = len(songs)
        pl['songs'] = [s for s in songs if str(s.get('id')) != str(song_id)]
        if len(pl['songs']) == before:
            return False
        _save_playlists(pls)
        return True
    return False


def delete_playlist(pid: str) -> bool:
    pls = _load_playlists()
    new = [p for p in pls if p['id'] != pid]
    if len(new) == len(pls):
        return False
    _save_playlists(new)
    return True