# -*- coding: utf-8 -*-
"""在线音乐技能 —— 内置酷我 / 网易云聚合搜索与直链解析（自包含，无外部服务依赖）。

实现逻辑在根目录 music_lib.py（与 server.py 前端 API 共用同一实例/缓存）：
- 搜索：酷我 r.s 老接口（失败回退 kw_token API）+ 网易云 web 搜索，双源并行
- 直链：酷我 antiserver（OGG 优先）；网易云 outer/url 302 解析
- 播放：把解析出的直链交给前端 <audio> 直接播（返回 __screen_command__）
- 歌单/榜单/歌词：网易云承担；酷我歌词走 m.kuwo.cn H5 接口
"""
from __future__ import annotations

import json

import music_lib


def search_music(args: dict) -> str:
    kw = str(args.get('keyword') or args.get('kw') or '').strip()
    if not kw:
        return '缺少 keyword 参数，例如 \'周杰伦\'。'
    limit = max(1, min(int(args.get('limit') or 8), 20))
    try:
        songs = music_lib.search_all(kw, limit)
    except Exception as e:
        return f'搜索失败：{e.__class__.__name__}: {e}'
    if not songs:
        return f'没搜到「{kw}」相关的歌曲，换个关键词试试？'
    music_lib.remember_results(songs[:limit * 2])
    lines = []
    for i, song in enumerate(music_lib._last_results.values(), 1):
        has_stream_hint = not song['vip'] or song['source'] == 'kuwo'
        extra = f" 专辑:{song['album']}" if song.get('album') else ''
        mark = '' if has_stream_hint else ' [VIP]'
        lines.append(f"{i}. 《{song['name']}》-{song['artists']} "
                     f"[{song['source']}] id={song['id']}{extra}{mark}")
    tip = '\n（告诉用户可选哪首，确定后用 music_play 的 index 参数直接播放对应序号）'
    return f'搜索「{kw}」共 {len(lines)} 首：\n' + '\n'.join(lines) + tip


def play_music(args: dict) -> str:
    picked = music_lib.pick_song(args)
    if isinstance(picked, str):
        return picked
    try:
        url = music_lib.resolve(picked['source'], picked['id'])
    except Exception as e:
        return f'解析播放链接失败：{e.__class__.__name__}: {e}'
    if not url:
        label = picked.get('name') or picked['id']
        return (f'《{label}》暂时拿不到播放链接'
                '（可能是 VIP/版权受限或已下架），换一首试试。')
    payload = {'url': url,
               'title': picked.get('name') or picked['id'],
               'artist': picked.get('artists') or '',
               'source': picked['source']}
    try:
        if args.get('volume') is not None:
            payload['volume'] = max(0.0, min(1.0, float(args.get('volume'))))
    except (TypeError, ValueError):
        pass
    payload['message'] = f"已开始播放《{picked.get('name') or picked['id']}》。"
    title = picked.get('name') or picked['id']
    if args.get('watch'):
        # 派子智能体全程负责：播放 + 盯到结束 + 播完自动向主智能体汇报。
        # server 收到 __media_watch__ 后在任务中心登记子智能体，
        # 并把 worker_id 注入到前端屏幕指令里，播完事件由前端回报。
        return json.dumps({
            '__media_watch__': True,
            'kind': 'music',
            'title': title,
            'brief': f'播放《{title}》并全程看护，播完自动向主智能体汇报。',
            'message': f'已派出子智能体负责播放《{title}》，这首歌播完会自动向你（主智能体）汇报。',
            'screen': {'tool': 'play_music', 'args': payload},
        }, ensure_ascii=False)
    return json.dumps({'__screen_command__': True, 'tool': 'play_music', 'args': payload},
                      ensure_ascii=False)


def stop_music(args: dict) -> str:
    return json.dumps({'__screen_command__': True, 'tool': 'stop_music',
                       'args': {'message': '已停止播放。'}},
                      ensure_ascii=False)


def control_music(args: dict) -> str:
    """控制正在播放的音乐：暂停 / 继续 / 切换 / 停止 / 音量。

    返回 __screen_command__ control_music，前端播放器实时执行（暂停不换源，
    可随时继续；音量立即生效）。
    """
    action = str(args.get('action') or '').strip().lower()
    if action not in ('pause', 'resume', 'play', 'toggle', 'stop', 'volume'):
        return ('action 必须是 pause / resume / toggle / stop / volume 之一，'
                '例如 music_control(action="pause") 暂停、'
                'music_control(action="resume") 继续、'
                'music_control(action="volume", value=0.5) 调音量。')
    if action == 'play':
        action = 'resume'
    payload: dict = {'action': action, 'message': '已执行音乐控制。'}
    if action == 'volume':
        try:
            v = float(args.get('value'))
        except (TypeError, ValueError):
            return 'volume 需要带 value（0.0~1.0），如 {"action":"volume","value":0.5}。'
        if not (0.0 <= v <= 1.0):
            return 'volume 需要带 value（0.0~1.0），如 {"action":"volume","value":0.5}。'
        payload['value'] = v
    return json.dumps({'__screen_command__': True, 'tool': 'control_music',
                       'args': payload}, ensure_ascii=False)


def get_lyric(args: dict) -> str:
    picked = music_lib.pick_song(args)
    if isinstance(picked, str):
        return picked
    lrc = (music_lib.kw_lyric(picked['id']) if picked['source'] == 'kuwo'
           else music_lib.nt_lyric(picked['id']))
    if not lrc.get('lyric'):
        return '这首歌没有可用歌词。'
    lyric = lrc['lyric']
    if len(lyric) > 3000:
        lyric = lyric[:3000] + '\n…（过长截断）'
    out = f"《{picked.get('name') or ''}》歌词（LRC）：\n{lyric}"
    tl = lrc.get('tlyric') or ''
    if tl:
        out += '\n\n翻译歌词：\n' + tl[:1500]
    return out


def hot_songs(args: dict) -> str:
    lines = ['当前可用榜单：']
    for pid, name in music_lib._BOARDS:
        lines.append(f'- {name}（netease，playlist_id={pid}）')
    tip = '\n（用 music_playlist 的 playlist_id 打开榜单后可点播其中歌曲）'
    return '\n'.join(lines) + tip


def open_playlist(args: dict) -> str:
    """打开网易云在线歌单 / 榜单，返回可点播歌曲列表。"""
    pid = str(args.get('playlist_id') or '').strip()
    if not pid:
        kw = str(args.get('keyword') or '').strip()
        if not kw:
            return '需要提供 playlist_id 或 keyword（歌单搜索词）。'
        try:
            pls = (music_lib.nt_search(kw, 1, 5, stype=1000).get('playlists') or [])
        except Exception as e:
            return f'歌单搜索失败：{e.__class__.__name__}: {e}'
        if not pls:
            return f'没找到与「{kw}」相关的歌单。'
        p0 = pls[0]
        return (f"找到歌单《{p0.get('name') or ''}》（id={p0.get('id')}）。"
                f"如需打开列表请带上 playlist_id={p0.get('id')} 再调用。")
    limit = max(1, min(int(args.get('limit') or 15), 50))
    try:
        pl = music_lib.nt_playlist(pid, limit)
    except Exception as e:
        return f'歌单打开失败：{e.__class__.__name__}: {e}'
    songs = pl.get('songs') or []
    if not songs:
        return f'歌单《{pl["name"]}》没有可播放的歌曲。'
    music_lib.remember_results(songs)
    lines = [f'歌单《{pl["name"]}》歌曲（前 {len(songs)} 首）：']
    for i, s in enumerate(songs, 1):
        lines.append(f"{i}. 《{s['name']}》-{s['artists']}")
    tip = '\n（用 music_play 的 index 参数可直接播放其中某首）'
    return '\n'.join(lines) + tip


def my_playlists(args: dict) -> str:
    """列出用户自己创建的歌单。"""
    pls = music_lib.list_playlists()
    if not pls:
        return '还没有创建过歌单。可以先 music_search 找歌，再让前端/用户创建歌单；或对在线歌单说「加入我的歌单」。'
    lines = ['我的歌单：']
    for p in pls:
        lines.append(f"- 《{p['name']}》（playlist_id={p['id']}，{p['song_count']} 首）")
    tip = '\n（用 music_play_playlist 的 playlist_id 直接播放某个歌单）'
    return '\n'.join(lines) + tip


def play_playlist(args: dict) -> str:
    """播放一个用户自建歌单：返回 __screen_command__ play_playlist，前端按队列顺序播放。"""
    pid = str(args.get('playlist_id') or '').strip()
    if not pid:
        name = str(args.get('playlist_name') or '').strip()
        if not name:
            return '需要提供 playlist_id（用 music_my_playlists 查看）。'
        for p in music_lib.list_playlists():
            if p['name'] == name:
                pid = p['id']
                break
    if not pid:
        return f'没有找到名为「{name}」的歌单，用 music_my_playlists 查看现有歌单。'
    pl = music_lib.get_playlist(pid)
    if not pl:
        return f'歌单 {pid} 不存在（可能已删除）。'
    songs = pl.get('songs') or []
    if not songs:
        return f'歌单《{pl["name"]}》是空的，先加几首歌再播。'
    payload = {'playlist_id': pl['id'], 'title': pl['name'],
               'songs': [{'source': s['source'], 'id': s['id'],
                          'name': s['name'], 'artists': s['artists']} for s in songs],
               'message': f"开始播放歌单《{pl['name']}》共 {len(songs)} 首。"}
    if args.get('watch'):
        return json.dumps({
            '__media_watch__': True,
            'kind': 'playlist',
            'title': pl['name'],
            'brief': f'按顺序播放歌单《{pl["name"]}》（{len(songs)} 首）并全程看护，播完自动向主智能体汇报。',
            'message': f'已派出子智能体负责歌单《{pl["name"]}》，全部播完会自动向你（主智能体）汇报。',
            'screen': {'tool': 'play_playlist', 'args': payload},
        }, ensure_ascii=False)
    return json.dumps({'__screen_command__': True, 'tool': 'play_playlist', 'args': payload},
                      ensure_ascii=False)


def now_playing_hint(args: dict) -> str:
    return ('在线音乐能力内置（酷我+网易云，无需外部服务）。'
            '流程：music_search 搜索 → music_play(index) 播放 → music_stop 停止；'
            'get_lyric 取歌词、music_billboard 看榜单、music_playlist 打开在线歌单、'
            'music_my_playlists 看自建歌单、music_play_playlist 播放自建歌单。')


HANDLERS = {
    'music_search': search_music,
    'music_play': play_music,
    'music_stop': stop_music,
    'music_control': control_music,
    'music_lyric': get_lyric,
    'music_billboard': hot_songs,
    'music_playlist': open_playlist,
    'music_my_playlists': my_playlists,
    'music_play_playlist': play_playlist,
    'music_status': now_playing_hint,
}