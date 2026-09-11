# -*- coding: utf-8 -*-
"""在线视频技能 —— 能力内建（自包含，无外部服务依赖）。

核心实现在同目录 video_lib.py（B站/AcFun 聚合搜索、yt-dlp 直链解析、
流代理/ffmpeg 合流、播放队列），与 server.py 的 /api/video_hub/* 端点
共享同一模块实例。播放经 __screen_command__ play_video 交给直播大屏，
前端用相对路径取流（手机/局域网同源可用）。

阻塞调用（yt-dlp/网络）全部走 asyncio.to_thread，不卡服务事件循环。
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

_SKILL_DIR = Path(__file__).resolve().parent
if str(_SKILL_DIR) not in sys.path:
    sys.path.insert(0, str(_SKILL_DIR))
import video_lib

_last_results = []  # 最近一次搜索结果（供 index 序号点播）


# ---------- 结果整理 ----------

def _fmt_duration(sec):
    try:
        sec = int(sec)
    except (TypeError, ValueError):
        return ""
    if sec <= 0:
        return ""
    h, m, s = sec // 3600, sec % 3600 // 60, sec % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _screen_args(entry: dict, message: str) -> dict:
    """把 lib 的 entry 转成前端 play_video 屏幕命令参数。

    流地址用主服务原生端点的相对路径（/api/video_hub/...）：
    手机/局域网访问时同源可用，不受混合内容策略影响。
    """
    st = entry.get("stream") or {}
    url = fallback = ""
    if st.get("mode") == "direct":
        url = f"/api/video_hub/proxy?k={st.get('key')}"
    elif st.get("mode") == "relay":
        url = f"/api/video_hub/relay/{st.get('key')}"
        fallback = f"/api/video_hub/relay/{st.get('key')}?t=1"  # 转码兜底（源编码不兼容时）
    return {
        "url": url,
        "fallback_url": fallback,
        "title": entry.get("title") or "",
        "uploader": entry.get("uploader") or "",
        "platform": entry.get("platform") or "",
        "height": st.get("height") or 0,
        "mode": st.get("mode") or "",
        "hub": "",
        "webpage_url": entry.get("webpage_url") or "",
        "duration": entry.get("duration"),
        "message": message,
    }


def _pick_target(args: dict):
    """index / query / url 三选一 → (url, query, err)，err 非空表示参数有误。"""
    idx = args.get("index")
    if idx is not None:
        try:
            idx = int(idx)
        except (TypeError, ValueError):
            return None, None, "index 需要是整数序号。"
        if not (1 <= idx <= len(_last_results)):
            return None, None, (f"序号 {idx} 不在最近一次搜索结果里（现有 {len(_last_results)} 条），"
                                "先 video_search 拿到列表。")
        return _last_results[idx - 1].get("webpage_url"), None, ""
    query = str(args.get("query") or "").strip()
    url = str(args.get("url") or "").strip()
    if query or url:
        return (url or None), (query or None), ""
    return None, None, "需要提供 index（搜索结果序号）、query（关键词）或 url（视频链接）之一。"


# ---------- 工具实现（async：阻塞调用走 to_thread） ----------

async def search_video(args: dict) -> str:
    kw = str(args.get("keyword") or args.get("query") or "").strip()
    if not kw:
        return "缺少 keyword 参数，例如 '蓝色星球 纪录片'。"
    if kw.startswith("http"):
        return "这是链接，直接用 video_play(url=...) 播放即可。"
    limit = max(1, min(int(args.get("limit") or 8), 16))
    platform = args.get("platform") or "all"
    sort = args.get("sort") or "relevance"
    try:
        results = await asyncio.to_thread(
            video_lib.search_videos, kw, platform, limit, sort)
    except ValueError as e:
        return f"搜索失败：{e}"
    except Exception as e:
        return f"搜索失败：{e.__class__.__name__}: {e}"
    if not results:
        return f"没搜到「{kw}」相关的视频，换个关键词试试？"
    _last_results[:] = results
    lines = []
    for i, r in enumerate(results, 1):
        dur = _fmt_duration(r.get("duration"))
        views = r.get("view_count")
        extra = []
        if r.get("uploader"):
            extra.append(f"UP:{r['uploader']}")
        if dur:
            extra.append(dur)
        if isinstance(views, (int, float)) and views > 0:
            extra.append(f"{int(views)//10000}万播放" if views >= 10000 else f"{int(views)}播放")
        src = "B站" if "bili" in str(r.get("platform") or "") else str(r.get("platform") or "")
        lines.append(f"{i}. 《{r.get('title') or r.get('webpage_url')}》[{src}]"
                     + (" " + " · ".join(extra) if extra else ""))
    tip = ("\n（把列表报给用户挑，确定后用 video_play(index) 在大屏播放；"
           "也可直接 video_play(query=...) 关键词点播。）")
    return f"搜索「{kw}」共 {len(results)} 条：\n" + "\n".join(lines) + tip


async def play_video(args: dict) -> str:
    url, query, err = _pick_target(args)
    if err:
        return err
    try:
        if url:
            entry = await asyncio.to_thread(video_lib.resolve_and_play, url)
        else:
            entry = await asyncio.to_thread(
                video_lib.play_by_query, query,
                args.get("platform") or "all", args.get("sort") or "relevance")
    except LookupError:
        return f"没搜到「{query}」相关的视频，换个关键词试试。"
    except Exception as e:
        return f"点播失败：{e.__class__.__name__}: {e}"
    if not (entry.get("stream") or {}).get("key"):
        return f"《{entry.get('title')}》解析不出可播放的流，换一个视频试试。"
    title = entry.get("title") or "未知视频"
    msg = f"已在大屏播放《{title}》。"
    payload = _screen_args(entry, msg)
    alt = entry.get("alternatives") or []
    if alt:
        payload["alternatives"] = [str(a) for a in alt[:3]]
    # 注意：必须返回纯 JSON（server 靠 json.loads 识别屏幕命令），
    # 备选片名放进 args，绝不能拼接在 JSON 字符串之后
    if args.get("watch"):
        # 派子智能体全程负责：播放 + 盯到结束 + 播完自动向主智能体汇报。
        # server 收到 __media_watch__ 后在任务中心登记子智能体，并把
        # worker_id 注入屏幕指令；前端播完回报到 /api/video_hub/api/ended。
        return json.dumps({
            "__media_watch__": True,
            "kind": "video",
            "title": title,
            "brief": f"播放《{title}》并全程看护，播完自动向主智能体汇报。",
            "message": f"已派出子智能体负责播放《{title}》，视频播完会自动向你（主智能体）汇报。",
            "screen": {"tool": "play_video", "args": payload},
        }, ensure_ascii=False)
    return json.dumps({"__screen_command__": True, "tool": "play_video", "args": payload},
                      ensure_ascii=False)


async def queue_video(args: dict) -> str:
    url, query, err = _pick_target(args)
    if err:
        return err
    try:
        entry, pos = await asyncio.to_thread(
            video_lib.queue_add, url, query,
            args.get("platform") or "all", args.get("sort") or "relevance")
    except LookupError:
        return f"没搜到「{query}」相关的视频，换个关键词试试。"
    except ValueError as e:
        return str(e)
    except Exception as e:
        return f"入队失败：{e.__class__.__name__}: {e}"
    note = ""
    if pos == 1 and not video_lib.public_state().get("now"):
        note = "（当前没有在播的视频，可直接 video_play 让它立刻开播。）"
    return f"《{entry.get('title')}》已加入连播队列第 {pos} 位，当前视频播完后自动接上。{note}"


async def control_video(args: dict) -> str:
    action = str(args.get("action") or "").strip()
    value = args.get("value")
    if action == "seek" and (not isinstance(value, (int, float)) or value < 0):
        return "seek 需要带 value（秒数），如 {'action':'seek','value':120}。"
    if action == "volume" and (not isinstance(value, (int, float)) or not (0 <= value <= 1)):
        return "volume 需要带 value（0.0~1.0），如 {'action':'volume','value':0.5}。"

    if action == "next":
        try:
            data = await asyncio.to_thread(video_lib.control, "next")
        except Exception as e:
            return f"切下一部失败：{e}"
        entry = (data or {}).get("next")
        if entry and (entry.get("stream") or {}).get("key"):
            payload = _screen_args(entry, f"连播下一部《{entry.get('title')}》。")
            return json.dumps({"__screen_command__": True, "tool": "play_video",
                               "args": payload}, ensure_ascii=False)
        return json.dumps({"__screen_command__": True, "tool": "control_video",
                           "args": {"action": "stop",
                                    "message": "队列已播完，大屏回到待机。"}},
                          ensure_ascii=False)

    try:
        await asyncio.to_thread(video_lib.control, action, value)
    except ValueError as e:
        return f"未知 action：{e}"
    except Exception as e:
        return f"控制失败：{e.__class__.__name__}: {e}"

    if action == "stop":
        return json.dumps({"__screen_command__": True, "tool": "control_video",
                           "args": {"action": "stop", "message": "已停止视频播放。"}},
                          ensure_ascii=False)
    if action == "pause":
        msg = "已暂停，video_control(resume) 可继续。"
    elif action == "resume":
        msg = "继续播放。"
    elif action == "mute":
        msg = "已静音。" if value in (None, 1, True) else "已取消静音。"
    elif action == "seek":
        msg = f"已拖动到第 {int(value)} 秒。"
    else:  # volume
        msg = f"音量已调到 {round(float(value) * 100)}%。"
    return json.dumps({"__screen_command__": True, "tool": "control_video",
                       "args": {"action": action, "value": value, "message": msg}},
                      ensure_ascii=False)


async def video_status(args: dict) -> str:
    st = await asyncio.to_thread(video_lib.public_state)
    now = st.get("now")
    if not now:
        q = st.get("queue") or []
        ffmpeg = "✅" if st.get("ffmpeg_available") else "❌（高清合流不可用）"
        return f"当前没有在播视频（队列 {len(q)} 部，ffmpeg {ffmpeg}）。用 video_play 点播，或 video_search 先搜。"
    pl = st.get("player") or {}
    pos = _fmt_duration(pl.get("position"))
    dur = _fmt_duration(pl.get("duration"))
    prog = f" {pos}/{dur}" if pos else ""
    state = "已暂停" if pl.get("paused") else "播放中"
    vol = pl.get("volume")
    vol_txt = f"，音量 {round(float(vol)*100)}%" if isinstance(vol, (int, float)) else ""
    q = st.get("queue") or []
    lines = [f"▶ {state}：{now.get('title')}{prog}{vol_txt}"]
    if q:
        lines.append(f"连播队列（{len(q)} 部）：" + "、".join(
            f"{i+1}.{(e.get('title') or '')[:20]}" for i, e in enumerate(q[:5])))
    return "\n".join(lines)


HANDLERS = {
    "video_search": search_video,
    "video_play": play_video,
    "video_queue": queue_video,
    "video_control": control_video,
    "video_status": video_status,
}
