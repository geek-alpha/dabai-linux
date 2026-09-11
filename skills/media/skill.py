# -*- coding: utf-8 -*-
"""媒体娱乐（media）—— 在线影音 + 汉化流水线，四合一。

合并自原 4 个技能：
- video（在线视频：B站/AcFun 聚合搜索、关键词点播、连播队列、大屏播放）
- music（在线音乐：酷我/网易云聚合搜索、播放、歌词、榜单、歌单）
- media_watch（媒体子智能体看护：查看/收回派出去的播放子进程）
- yt-hanhua-pipeline（油管视频汉化流水线：下载/转录/翻译/烧字幕/AI配音/发布B站/垫BGM）

核心实现在同目录：
- video_lib.py（视频核心库，与 server.py /api/video_hub/* 共享同一模块实例）
- music_lib.py（根目录，与 server.py 前端 API 共用）
- media_workers（根目录，子智能体注册表）

工具名全部保持原样（video_* / music_* / media_worker_* / hanhua_*），只归并目录。
"""
from __future__ import annotations

import os
import sys

_SKILL_DIR = os.path.dirname(os.path.abspath(__file__))
if _SKILL_DIR not in sys.path:
    sys.path.insert(0, _SKILL_DIR)

import video_impl  # noqa: E402
import music_impl  # noqa: E402
import media_watch_impl  # noqa: E402
import hanhua_impl  # noqa: E402
import image_gen_impl  # noqa: E402

HANDLERS = {
    "video_search": video_impl.search_video,
    "video_play": video_impl.play_video,
    "video_queue": video_impl.queue_video,
    "video_control": video_impl.control_video,
    "video_status": video_impl.video_status,
    "music_search": music_impl.search_music,
    "music_play": music_impl.play_music,
    "music_stop": music_impl.stop_music,
    "music_control": music_impl.control_music,
    "music_lyric": music_impl.get_lyric,
    "music_billboard": music_impl.hot_songs,
    "music_playlist": music_impl.open_playlist,
    "music_my_playlists": music_impl.my_playlists,
    "music_play_playlist": music_impl.play_playlist,
    "music_status": music_impl.now_playing_hint,
    "media_workers_list": media_watch_impl.media_workers_list,
    "media_worker_status": media_watch_impl.media_worker_status,
    "media_worker_cancel": media_watch_impl.media_worker_cancel,
    "hanhua_status": hanhua_impl.hanhua_status,
    "hanhua_jobs": hanhua_impl.hanhua_jobs,
    "hanhua_log": hanhua_impl.hanhua_log,
    "hanhua_download": hanhua_impl.hanhua_download,
    "hanhua_add": hanhua_impl.hanhua_add,
    "hanhua_run": hanhua_impl.hanhua_run,
    "hanhua_run_stop": hanhua_impl.hanhua_run_stop,
    "hanhua_run_log": hanhua_impl.hanhua_run_log,
    "hanhua_tasks": hanhua_impl.hanhua_tasks,
    "hanhua_pause": hanhua_impl.hanhua_pause,
    "hanhua_config": hanhua_impl.hanhua_config,
    "hanhua_bgm": hanhua_impl.hanhua_bgm,
    "hanhua_remix": hanhua_impl.hanhua_remix,
    "hanhua_doctor": hanhua_impl.hanhua_doctor,
    "image_gen_create": image_gen_impl.create_image,
}
