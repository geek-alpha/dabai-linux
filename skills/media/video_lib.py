# -*- coding: utf-8 -*-
"""video_lib —— 在线视频核心库（自包含，无外部服务依赖）。

由 media 技能（skills/media/skill.py）与主服务（server.py 的
/api/video_hub/* 端点）共享同一模块实例，能力完全内建：

  * 聚合搜索：B站（yt-dlp bilisearch + buvid cookie 预热）+ AcFun（公开 REST）
  * 直链解析：yt-dlp 提取 + 选流（优先浏览器通吃的 h264/av1 合一 mp4）
  * 流输出：direct 直链代理（Range 可拖进度）/ relay ffmpeg 实时合流（?t=1 转码兜底）
  * 播放状态：当前播放 / 连播队列 / 前端心跳上报

依赖：requests、yt-dlp（主环境）；ffmpeg 可选（relay 高清流需要）。
"""
from __future__ import annotations

import logging
import html
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
import os
from concurrent.futures import ThreadPoolExecutor

import requests
import yt_dlp

log = logging.getLogger("video_lib")


def _iter_ffmpeg_candidates():
    """按 PATH 顺序枚举全部 ffmpeg 可执行文件（shutil.which 只给第一个，不够）。

    不收 .cmd/.bat 包装：relay 的 -headers 参数内嵌真实 CRLF，
    经 cmd.exe %* 二次解析会被拆断甚至注入。"""
    if os.name == "nt":
        exts = [e.lower() for e in os.environ.get("PATHEXT", "").split(";")
                if e.lower() == ".exe"] or [""]
    else:
        exts = [""]          # POSIX：无扩展名，靠可执行位判定
    seen = set()
    for d in os.environ.get("PATH", "").split(os.pathsep):
        if not d:
            continue
        for ext in exts:
            p = os.path.join(d, "ffmpeg" + ext)
            if not os.path.isfile(p):
                continue
            if os.name != "nt" and not os.access(p, os.X_OK):
                continue
            key = p.lower() if os.name == "nt" else p   # Windows 路径不区分大小写，POSIX 区分
            if key not in seen:
                seen.add(key)
                yield p


def _ffmpeg_supports_args(path, pre_args):
    """探测 ffmpeg 是否支持给定前置参数（防 "Unrecognized option" 秒退）。

    IDE 自带的精简 ffmpeg（如 TRAE bin）不含网络协议参数，遇到
    不认识的选项直接报错秒退且 stderr 被 DEVNULL 吞掉——前端只看到
    HTTP 200 + 0 字节，循环"网络波动"。"""
    try:
        r = subprocess.run(
            [path, "-hide_banner", *pre_args, "-i", "data:,", "-f", "null", "-"],
            capture_output=True, timeout=6, stdin=subprocess.DEVNULL)
        err = (r.stderr or b"").decode("utf-8", "replace")
        return "Unrecognized option" not in err and "Option not found" not in err
    except Exception:
        return False


def _ffmpeg_supports_headers(path):
    """探测 ffmpeg 是否支持 -headers（防盗链头，relay 合流必需）。"""
    return _ffmpeg_supports_args(path, ["-headers", "X-Probe: 1"])


def _find_ffmpeg():
    """选 PATH 中第一个支持 -headers 的完整版 ffmpeg；
    都不支持时退回第一个找到的（direct 流不需要 headers）。"""
    fallback = None
    for p in _iter_ffmpeg_candidates():
        if _ffmpeg_supports_headers(p):
            return p
        if fallback is None:
            fallback = p
    return fallback


FFMPEG = _find_ffmpeg()
# 抗瞬断参数（-reconnect 系列，ffmpeg ≥4.0 内置）支持与否，不支持的构建跳过
FFMPEG_RECONNECT = bool(FFMPEG) and _ffmpeg_supports_args(
    FFMPEG, ["-reconnect", "1", "-reconnect_streamed", "1"])

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

BASE_OPTS = {
    "quiet": True,
    "no_warnings": True,
    "noplaylist": True,
    "nocheckcertificate": True,
    "socket_timeout": 15,
    "retries": 2,
    "http_headers": {
        "User-Agent": UA,
        "Referer": "https://www.bilibili.com/",
        "Accept-Language": "zh-CN,zh;q=0.9",
    },
}

CACHE_TTL = 3600  # 直链 / 元数据缓存 1 小时（平台直链一般几小时才过期）

# 内置平台（与 video_sources_lib.BUILTIN_PLATFORMS 一一对应；all 为聚合模式不在此列）
PLATFORMS = [
    {"id": "all", "name": "聚合搜索（全平台）", "searchable": True,
     "hint": "同时搜索哔哩哔哩 + AcFun 并合并结果"},
    {"id": "bilibili", "name": "哔哩哔哩", "searchable": True,
     "hint": "关键词搜索 + 点播（含知识区/公开课/纪录片等教育内容）"},
    {"id": "acfun", "name": "AcFun", "searchable": True,
     "hint": "关键词搜索 + 点播"},
    {"id": "youtube", "name": "YouTube", "searchable": True,
     "hint": "全球最大视频站，需 fq 代理"},
]


def _enabled_platform_ids() -> list:
    """返回当前启用的平台 id（不含 all）。读 video_sources.json 失败时全部启用。"""
    try:
        import video_sources_lib
        return video_sources_lib.enabled_ids()
    except Exception:
        return ["bilibili", "acfun", "youtube"]


def _platform_enabled(pid: str) -> bool:
    return pid in _enabled_platform_ids()

# ---------------------------------------------------------------------------
# 全局状态
# ---------------------------------------------------------------------------
STATE = {"now": None, "queue": [], "report": {}}
STATE_LOCK = threading.Lock()

RESOLVE_CACHE = {}  # webpage_url -> {"entry": {..., "stream"?}, "ts"}
STREAMS = {}        # stream key -> 流信息（direct: 单流 / relay: video+audio）
CACHE_LOCK = threading.Lock()

_ACFUN_CACHE = {}   # query -> (ts, [entry]) 接口不支持翻页，缓存整页结果供本地切片
_ACFUN_LOCK = threading.Lock()


def public_state():
    with STATE_LOCK:
        return {
            "now": STATE["now"],
            "queue": STATE["queue"],
            "player": STATE["report"],
            "ffmpeg_available": bool(FFMPEG),
        }


# ---------------------------------------------------------------------------
# B 站：buvid cookie 预热（写 Netscape cookiefile 喂给 yt-dlp，缓解 412 风控）
# ---------------------------------------------------------------------------
_BILI_COOKIE = {"file": None, "ts": 0}
_BILI_LOCK = threading.Lock()


def bili_cookiefile():
    """返回 B 站 cookie 文件路径；拿不到返回 None。缓存 6 小时。"""
    with _BILI_LOCK:
        if _BILI_COOKIE["file"] and time.time() - _BILI_COOKIE["ts"] < 6 * 3600:
            return _BILI_COOKIE["file"]
    try:
        s = requests.Session()
        s.headers.update({"User-Agent": UA})
        s.get("https://www.bilibili.com/", timeout=10)
        if "buvid3" not in s.cookies:
            return None
        path = os.path.join(tempfile.gettempdir(), "video_lib_bili_cookies.txt")
        with open(path, "w") as f:
            f.write("# Netscape HTTP Cookie File\n")
            for c in s.cookies:
                dom = c.domain if c.domain.startswith(".") else "." + c.domain.split(".", 1)[-1]
                f.write(f"{dom}\tTRUE\t{c.path}\t{str(bool(c.secure)).upper()}\t0\t{c.name}\t{c.value}\n")
        with _BILI_LOCK:
            _BILI_COOKIE["file"] = path
            _BILI_COOKIE["ts"] = time.time()
        return path
    except Exception as exc:
        log.warning("bili cookie warmup failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# yt-dlp：解析 / 选流
# ---------------------------------------------------------------------------
def _normalize_info(info, fallback_url):
    if info.get("_type") in ("playlist", "multi_video") and info.get("entries"):
        info = info["entries"][0]
    return {
        "id": str(info.get("id") or uuid.uuid4().hex[:8]),
        "title": info.get("title") or fallback_url,
        "uploader": info.get("uploader") or info.get("channel") or "",
        "duration": info.get("duration"),
        "thumbnail": info.get("thumbnail") or next(
            (t.get("url") for t in reversed(info.get("thumbnails") or [])
             if isinstance(t, dict) and t.get("url")), None),
        "view_count": info.get("view_count"),
        "webpage_url": info.get("webpage_url") or fallback_url,
        "platform": (info.get("extractor_key") or "").lower(),
    }


def _codec_rank(vcodec):
    v = (vcodec or "").lower()
    if v.startswith("avc1") or v.startswith("h264"):
        return 4
    if v.startswith("av01"):
        return 3
    if v.startswith("vp09") or v.startswith("vp9"):
        return 2
    if v.startswith("hvc1") or v.startswith("hev"):
        return 1
    return 0


def _http_progressive(f):
    proto = (f.get("protocol") or "").split("+")[0]
    return proto in ("http", "https") and (f.get("ext") or "") in ("mp4", "m4a", "webm")


def _stream_sort_key(f):
    """浏览器能直接解码的编码（h264/av1）优先于 hevc/未知——宁可低清也要能播"""
    rank = _codec_rank(f.get("vcodec"))
    return (1 if rank >= 3 else 0, f.get("height") or 0, rank, f.get("tbr") or 0)


def _pick_streams(formats):
    """优先选音视频合一的 mp4（可 seek、秒开）；
    其次选音视频合一的 HLS（AcFun 等，交 ffmpeg 实时转封装）；
    只有分离流（DASH）时返回最佳视频+音频，交给 ffmpeg 实时合流。"""
    both = [
        f for f in formats
        if _http_progressive(f) and f.get("ext") == "mp4"
        and f.get("vcodec") not in (None, "", "none")
        and f.get("acodec") not in (None, "", "none")
    ]
    if both:
        best = max(both, key=_stream_sort_key)
        return {"mode": "direct", "video": best, "height": best.get("height") or 0}

    # AcFun 等站大量单码率视频 codec 字段为 None（不代表没音轨），未知 codec 保留；
    # 只排除明确 vcodec=="none" 的纯音频 HLS
    hls = [
        f for f in formats
        if (f.get("protocol") or "").split("+")[0].startswith("m3u8")
        and f.get("vcodec") != "none"
    ]
    if hls:
        coded = [f for f in hls
                 if f.get("vcodec") not in (None, "", "none")
                 and f.get("acodec") not in (None, "", "none")]
        # 无「音视频合一」HLS 时（YouTube 的 HLS 全是纯视频变体，acodec=none），
        # 直接选 HLS 必然丢音轨：有 DASH h264 视频 + 独立音频可组时优先组合，
        # 音视频都全（视频 copy + 音频 copy/aac）
        if not coded:
            videos = [f for f in formats if _http_progressive(f)
                      and f.get("vcodec") not in (None, "", "none")]
            h264_videos = [f for f in videos if _codec_rank(f.get("vcodec")) >= 3]
            audios = [f for f in formats if _http_progressive(f)
                      and f.get("vcodec") in (None, "", "none")
                      and f.get("acodec") not in (None, "", "none")]
            if h264_videos and audios:
                # avc1 严格优先于 av01（硬解全覆盖，av1 高分辨率易卡/软解），
                # 没有 avc1 才退 av01
                avc = [f for f in h264_videos if _codec_rank(f.get("vcodec")) >= 4]
                v = max(avc or h264_videos, key=_stream_sort_key)
                a = max(audios, key=lambda f: (f.get("abr") or f.get("tbr") or 0))
                return {"mode": "relay", "video": v, "audio": a,
                        "height": v.get("height") or 0}
        # 优先 h264/av1 的 HLS（浏览器可直接解码，无需转码兜底）
        h264_hls = [f for f in (coded or hls) if _codec_rank(f.get("vcodec")) >= 3]
        if h264_hls:
            best = max(h264_hls, key=lambda f: (f.get("height") or 0, f.get("tbr") or 0))
            return {"mode": "relay", "video": best, "audio": None,
                    "height": best.get("height") or 0, "hls": True}
        # 全是 VP9/HEVC 的 HLS（如 YouTube）：DASH 分离流里通常有 h264 视频轨，
        # 优先用它（视频 copy + 音频转 aac），避免大屏解码不了 VP9 黑屏
        videos = [f for f in formats if _http_progressive(f)
                  and f.get("vcodec") not in (None, "", "none")]
        h264_videos = [f for f in videos if _codec_rank(f.get("vcodec")) >= 3]
        if h264_videos:
            v = max(h264_videos, key=_stream_sort_key)
            audios = [f for f in formats if _http_progressive(f)
                      and f.get("vcodec") in (None, "", "none")
                      and f.get("acodec") not in (None, "", "none")]
            a = max(audios, key=lambda f: (f.get("abr") or f.get("tbr") or 0)) if audios else None
            return {"mode": "relay", "video": v, "audio": a, "height": v.get("height") or 0}
        # 无 h264 可选 → 退回原逻辑（选最高清 HLS，靠 ?t=1 转码兜底）
        best = max(coded or hls, key=lambda f: (f.get("height") or 0, f.get("tbr") or 0))
        return {"mode": "relay", "video": best, "audio": None,
                "height": best.get("height") or 0, "hls": True}

    videos = [f for f in formats if _http_progressive(f)
              and f.get("vcodec") not in (None, "", "none")]
    audios = [f for f in formats if _http_progressive(f)
              and f.get("vcodec") in (None, "", "none")
              and f.get("acodec") not in (None, "", "none")]
    if not videos:
        return None
    v = max(videos, key=_stream_sort_key)
    a = max(audios, key=lambda f: (f.get("abr") or f.get("tbr") or 0)) if audios else None
    return {"mode": "relay", "video": v, "audio": a, "height": v.get("height") or 0}


# ---------------------------------------------------------------------------
# 拉速自检：B 站 DASH 分离流由 ffmpeg 单连接实时合流，CDN 节点质量随机，
# 抽到慢节点（拉速 < 播放码率）时缓冲必然耗尽——表现为播放卡顿。
# 起播前实测拉速，不够就重解析换 CDN 节点，仍不够按画质逐档降级。
# ---------------------------------------------------------------------------
_SPEED_BUDGET_KB = 512   # 测速拉取量（快节点 <1s 拉完）
_SPEED_WINDOW_S = 2.2     # 测速时间窗上限
_SPEED_HEADROOM = 1.6     # 拉速需达到播放码率的倍数余量
# fq 代理链路吞吐逐秒剧烈摆动（对同一 1080p 直链接连实测 127↔1574KB/s，
# 波幅近 12 倍）：突发测速达标不代表撑得过连续数秒的低谷，低于消费速率
# 几秒就会抽干浏览器缓冲。代理源余量加倍——宁可低清，不要反复暂停。
_SPEED_HEADROOM_PROXY = 3.0


def _probe_speed(fmt, headers, proxies=None):
    """实测单条直链拉速（KB/s）。默认模拟 ffmpeg 单连接直连（不走代理），
    与 relay 实际拉流方式保持一致：需代理的源（YouTube）由调用方传入代理。
    只拉头部少量字节、最多数秒；请求失败/超时返回 0。"""
    try:
        # 代理链路抖动大（秒级摆幅可达 12 倍）：默认窗口只采到突发速率，
        # 对代理源加大采样预算再多看一秒，结论更接近持续吞吐
        win = _SPEED_WINDOW_S
        budget = _SPEED_BUDGET_KB
        if proxies:
            win = max(win, 3.0)
            budget = max(budget, 768)
        r = requests.get(fmt["url"], headers=headers or {}, stream=True,
                         timeout=(2, win),
                         proxies=proxies or {"http": None, "https": None})
        if r.status_code >= 400:
            r.close()
            return 0.0
        t0 = time.time()
        total = 0
        for chunk in r.iter_content(65536):
            total += len(chunk)
            if total >= budget * 1024 or time.time() - t0 >= win:
                break
        r.close()
        dt = time.time() - t0
        return (total / 1024.0 / dt) if dt > 0 else 0.0
    except Exception:
        return 0.0


def _relay_candidates(formats):
    """DASH 分离流视频档位按画质从高到低排序；同分辨率内浏览器兼容性
    优先（avc1 > av01 > hevc），同级内低码率在前——供拉速不足时逐档降级。"""
    vids = [f for f in formats
            if _http_progressive(f) and f.get("vcodec") not in (None, "", "none")]
    return sorted(vids, key=lambda f: (-(f.get("height") or 0),
                                        -_codec_rank(f.get("vcodec")),
                                        f.get("tbr") or 0))


def _speed_adapt(picked, formats, headers, retry_round, proxies=None):
    """对首选 relay 流做拉速自检，返回 (够速?, 最终picked)。

    第一轮不够速返回 False（调用方重解析换 CDN 节点再来一次）；
    第二轮仍不够则按画质从高到低逐档实测降级；全都拉不动时选拉速
    最高的一档（比硬卡强）。无码率信息的源（非 B 站）不测速，保持原行为。
    proxies：测速走的代理（YouTube 等源须与 relay ffmpeg 同路径才准）。
    """
    v, a = picked["video"], picked.get("audio")

    # 代理源（YouTube）链路抖动大，安全余量加倍（见常量处注释）
    headroom = _SPEED_HEADROOM_PROXY if proxies else _SPEED_HEADROOM

    def _need(video_fmt):
        tbr = (video_fmt.get("tbr") or 0) + ((a.get("tbr") or 0) if a else 0)
        return tbr / 8.0 * headroom  # kbps → KB/s

    if not (v.get("tbr") or 0):
        return True, picked
    got = _probe_speed(v, headers, proxies)
    need = _need(v)
    if got >= need:
        return True, picked
    log.info("relay stream slow: got %.0fKB/s need %.0fKB/s (round %d)",
             got, need, retry_round)

    if proxies:
        # 代理源：出口固定，重解析换不了节点；用首档实测速率当链路容量，
        # 按「需求 ≤ 容量」直接计算选最高可用档（编码优先 avc1，截 4 档）。
        # 不逐档串行实测——每档最多 ~5s，五档全测能把起播拖到 30s+；
        # 同一代理链路各文件的吞吐基本一致，单次采样足够定案。
        cands = sorted((f for f in _relay_candidates(formats)
                        if f is not v and f.get("tbr")),
                       key=lambda f: (-_codec_rank(f.get("vcodec")),
                                      -(f.get("height") or 0),
                                      f.get("tbr") or 0))[:4]
        for f in cands:
            if got >= _need(f):
                log.info("relay downgrade(computed, link %.0fKB/s): "
                         "%sp %s tbr=%s", got, f.get("height"),
                         f.get("vcodec"), f.get("tbr"))
                return True, {"mode": "relay", "video": f, "audio": a,
                              "height": f.get("height") or 0}
        # 全部超容：退到清单内 ≥360p 的最低码率档兜底（360p 以下大屏没法看）
        floors = [f for f in cands if (f.get("height") or 0) >= 360] or cands
        fmin = min(floors, key=lambda f: f.get("tbr") or 0) if floors else v
        log.warning("proxy link too slow for any tier (%.0fKB/s), floor to "
                    "%sp tbr=%s", got, fmin.get("height"), fmin.get("tbr"))
        return True, {"mode": "relay", "video": fmin, "audio": a,
                      "height": fmin.get("height") or 0}

    if retry_round == 0:
        return False, picked
    # 非代理源维持原语义：round1 重解析换 CDN 节点后仍不够速时，
    # 按画质从高到低逐档实测降级；全都拉不动时选拉速最高的一档
    best, best_speed = None, -1.0
    for f in _relay_candidates(formats):
        if f is v or not (f.get("tbr") or 0):
            continue
        s = _probe_speed(f, headers)
        if s > best_speed:
            best, best_speed = f, s
        if s >= _need(f):
            log.info("relay downgrade: %sp %s tbr=%s (%.0fKB/s)",
                     f.get("height"), f.get("vcodec"), f.get("tbr"), s)
            return True, {"mode": "relay", "video": f, "audio": a,
                          "height": f.get("height") or 0}
    if best is not None:
        log.warning("all relay candidates slow, best %.0fKB/s at %sp tbr=%s",
                    best_speed, best.get("height"), best.get("tbr"))
        return True, {"mode": "relay", "video": best, "audio": a,
                      "height": best.get("height") or 0}
    return True, picked


def _register_stream(item, **extra):
    key = uuid.uuid4().hex[:20]
    item["ts"] = time.time()
    item.update(extra)
    with CACHE_LOCK:
        STREAMS[key] = item
    return key


def resolve(url, need_stream=True, force=False):
    """统一解析入口（yt-dlp，B 站带 buvid cookie 预热）。
    force=True 跳过缓存强制重解析（断流自动恢复用：拿全新直链与流 key）。"""
    now = time.time()
    if not force:
        with CACHE_LOCK:
            hit = RESOLVE_CACHE.get(url)
        if hit and now - hit["ts"] < CACHE_TTL and (not need_stream or hit["entry"].get("stream")):
            return hit["entry"]

    opts = dict(BASE_OPTS, skip_download=True)
    is_yt = bool(re.search(r"(?:^|\.)youtube\.com|youtu\.be", url))
    if is_yt:
        opts["proxy"] = _YT_PROXY  # YouTube 直链解析走 fq 代理
    # YouTube relay 实际经代理拉流（ffmpeg -http_proxy），测速须同路径才准，
    # 否则直连必失败 → 误判慢流 → 降级到错误档位
    yt_probe = {"http": _YT_PROXY, "https": _YT_PROXY} if is_yt else None
    if re.search(r"(?:^|\.)bilibili\.com", url):
        ck = bili_cookiefile()
        if ck:
            opts["cookiefile"] = ck
    picked = None
    headers = {}
    # 两轮解析：首轮选流后实测拉速，慢节点（卡顿根因）时丢弃重解析换
    # CDN 节点再来一次；direct/HLS 无需测速一轮即定。
    for attempt in (0, 1):
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
        entry = _normalize_info(info, url)
        if not need_stream:
            break
        headers = dict(info.get("http_headers") or {})
        headers.setdefault("User-Agent", UA)
        picked = _pick_streams(info.get("formats") or [])
        if not picked:
            raise RuntimeError("no browser-playable format found; try another video")
        if picked["mode"] == "direct" or picked.get("hls"):
            break
        ok, picked = _speed_adapt(picked, info.get("formats") or [], headers,
                                  attempt, yt_probe)
        if ok:
            break
        log.info("re-resolving %s for a faster CDN node", url)
    if need_stream:
        plat = "youtube" if is_yt else ""
        if picked["mode"] == "direct":
            f = picked["video"]
            key = _register_stream({"kind": "direct", "url": f["url"], "headers": headers},
                                   platform=plat)
            entry["stream"] = {"mode": "direct", "key": key, "mime": "video/mp4",
                               "height": picked["height"]}
        else:
            v, a = picked["video"], picked["audio"]
            mime = "video/webm" if (v.get("ext") == "webm") else "video/mp4"
            key = _register_stream({
                "kind": "relay",
                "mime": mime,
                "hls": bool(picked.get("hls")),
                "video": {"url": v["url"], "headers": headers, "ext": v.get("ext")},
                "audio": {"url": a["url"], "headers": headers, "ext": a.get("ext")} if a else None,
            }, platform=plat)
            entry["stream"] = {"mode": "relay", "key": key, "mime": mime,
                               "height": picked["height"], "requires_ffmpeg": True}

    with CACHE_LOCK:
        RESOLVE_CACHE[url] = {"entry": entry, "ts": now}
    return entry


# ---------------------------------------------------------------------------
# 搜索：AcFun REST + B站 yt-dlp
# ---------------------------------------------------------------------------
def acfun_search(query, limit=12, page=1, sort="relevance"):
    """AcFun 站内搜索（公开 REST 接口，无需登录）。

    注意：该接口不支持翻页——pcount/pcursor 参数被忽略，固定返回
    前 30 条（totalNum 可能是总数）。这里用「关键词+排序」缓存 + 本地切片
    模拟分页：同键 2 分钟内复用一次抓取，按 page/limit 切片，
    切完返回空列表（前端据此停止加载）。

    sortType 实测映射：1=综合 / 2=最多播放 / 5=最新发布。"""
    page = max(1, int(page or 1))
    limit = max(1, int(limit or 12))
    sort_type = {"hot": 2, "new": 5}.get(sort, 1)
    now = time.time()
    ckey = (query, sort_type)
    hit = _ACFUN_CACHE.get(ckey)
    if hit and now - hit[0] < 120:
        items = hit[1]
    else:
        r = requests.get(
            "https://www.acfun.cn/rest/pc-direct/search/video",
            params={"keyword": query, "pcount": 30, "pcursor": 0,
                    "resourceType": 2, "site": 0, "sortType": sort_type},
            headers={"User-Agent": UA, "Referer": "https://www.acfun.cn/"},
            timeout=10,
        )
        r.raise_for_status()
        items = []
        for v in r.json().get("videoList") or []:
            if not v.get("id"):
                continue
            title = re.sub(r"</?em>", "", v.get("emTitle") or v.get("title") or "")
            dur = None
            parts = str(v.get("playDuration") or "").split(":")
            if len(parts) in (2, 3) and all(p.isdigit() for p in parts):
                dur = sum(int(p) * 60 ** i for i, p in enumerate(reversed(parts)))
            items.append({
                "id": str(v["id"]),
                "title": title or f"ac{v['id']}",
                "uploader": v.get("userName") or "",
                "duration": dur,
                "thumbnail": v.get("coverUrl"),
                "view_count": v.get("viewCount"),
                "webpage_url": f"https://www.acfun.cn/v/ac{v['id']}",
                "platform": "acfun",
            })
        _ACFUN_CACHE[ckey] = (now, items)
        if len(_ACFUN_CACHE) > 32:
            with _ACFUN_LOCK:
                oldest = sorted(_ACFUN_CACHE.items(), key=lambda kv: kv[1][0])
                for k, _ in oldest[:16]:
                    _ACFUN_CACHE.pop(k, None)
    start = (page - 1) * limit
    return items[start:start + limit]


def _bili_video_search_page(query, limit=12, page=1, order=""):
    """B 站综合搜索 REST 分页（与 yt-dlp bilisearch 同源 API）。

    原 bilisearch 的 limit 是「总条数上限」，无法指定第 N 页；
    这里直接调 x/web-interface/search/type 的 page 参数实现真正的
    按页加载。返回条目自带完整元数据（标题/时长/封面/播放量），
    无需逐条 resolve，比逐条解析快 5-10 倍。

    order：""=综合 / "click"=最多点击 / "pubdate"=最新发布。"""
    page = max(1, int(page or 1))
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Referer": "https://www.bilibili.com/",
                      "Accept-Language": "zh-CN,zh;q=0.9"})
    ck = bili_cookiefile()  # 预热 buvid cookie（缓解 412 风控）
    if ck:
        try:
            for ln in open(ck, encoding="utf-8"):
                ln = ln.strip()
                if not ln or ln.startswith("#"):
                    continue
                parts = ln.split("\t")
                if len(parts) >= 7:
                    s.cookies.set(parts[5], parts[6], domain=".bilibili.com")
        except Exception as exc:
            log.warning("read bili cookie file failed: %s", exc)
    if not s.cookies.get("buvid3"):
        s.cookies.set("buvid3", uuid.uuid4().hex[:16] + "infoc", domain=".bilibili.com")
    params = {"Search_key": query, "keyword": query, "page": page,
              "context": "", "duration": 0, "tids_2": "",
              "__refresh__": "true", "search_type": "video",
              "tids": 0, "highlight": 1}
    if order:
        params["order"] = order
    r = s.get("https://api.bilibili.com/x/web-interface/search/type",
              params=params, timeout=12)
    r.raise_for_status()
    data = r.json()
    if data.get("code") != 0:
        raise RuntimeError(f"bilibili search api code={data.get('code')}: "
                           f"{data.get('message')}")
    out = []
    for v in data.get("data", {}).get("result") or []:
        bvid = v.get("bvid") or ""
        if not bvid:
            m = re.search(r"/video/(BV[0-9A-Za-z]+)", v.get("arcurl") or "")
            if m:
                bvid = m.group(1)
        if not bvid:  # 无视频 id（如特殊内容）不可播放，丢弃
            continue
        title = html.unescape(re.sub(r"</?em\b[^>]*>", "", v.get("title") or "")).strip()
        dur = 0
        parts = str(v.get("duration") or "").split(":")
        if len(parts) in (2, 3) and all(p.isdigit() for p in parts):
            dur = sum(int(p) * 60 ** i for i, p in enumerate(reversed(parts)))
        pic = str(v.get("pic") or "")
        if pic.startswith("//"):
            pic = "https:" + pic
        out.append({
            "id": str(v.get("aid") or bvid),
            "title": title or f"BV{bvid}",
            "uploader": v.get("author") or "",
            "duration": dur or None,
            "thumbnail": pic,
            "view_count": v.get("play") or 0,
            "webpage_url": f"https://www.bilibili.com/video/{bvid}",
            "platform": "bilibili",
        })
        if len(out) >= limit:
            break
    return out


def _bilibili_search(query, limit=12, page=1, order=""):
    """B 站关键词搜索：优先 REST 按页加载；失败时 yt-dlp bilisearch 兜底。

    REST 正常时：每页返回该页的 limit 条（元数据已带全，不逐条解析）。
    兜底时：bilisearch 一次取 (page+1)*limit 条再切片（会重复解析，仅在
    REST 异常时走，速度慢但结果可用；兜底路径不支持 order 排序）。"""
    try:
        return _bili_video_search_page(query, limit, page, order)
    except Exception as exc:
        log.warning("bilibili REST search failed, fallback yt-dlp: %s", exc)
    opts = dict(BASE_OPTS, extract_flat=True, skip_download=True)
    ck = bili_cookiefile()
    if ck:
        opts["cookiefile"] = ck
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"bilisearch{(page + 1) * limit + 3}:{query}",
                                download=False)
    raw = [e for e in (info.get("entries") or [])
           if e and (e.get("url") or e.get("webpage_url"))]
    raw = raw[(page - 1) * limit: page * limit]

    def _meta(e):
        url = e.get("url") or e.get("webpage_url")
        if not url:
            return None
        try:
            entry = resolve(url, need_stream=False)
            title = entry.get("title") or ""
            # 完整解析后仍无标题/标题是链接（付费课程页等）才丢弃
            if not title or title.startswith("http"):
                return None
            return entry
        except Exception as exc:
            log.warning("resolve meta failed for %s: %s", url, exc)
            title = e.get("title") or ""
            if not title or title.startswith("http"):
                return None
            return {"id": e.get("id") or uuid.uuid4().hex[:6],
                    "title": title, "webpage_url": url,
                    "platform": "bilibili", "uploader": e.get("uploader") or "",
                    "duration": e.get("duration"), "thumbnail": e.get("thumbnail")}

    with ThreadPoolExecutor(max_workers=6) as ex:
        return [r for r in ex.map(_meta, raw[:limit]) if r]


def _merge_results(groups, sort, limit):
    """聚合各平台结果：交错合并（保留各平台内部次序）。

    各平台在搜索阶段已按所请求的排序（综合/热门/最新）返回各自排好的
    列表，聚合时交错混排保持平台多样性；热门/最新不再按 view_count
    全局重排（部分平台结果无播放量，全局排序会把它压到底部）。"""
    merged = []
    for i in range(max((len(g) for g in groups), default=0)):
        for g in groups:
            if i < len(g):
                merged.append(g[i])
    return merged[:limit]


def _custom_search(src: dict, query: str, limit: int, page: int) -> list:
    """自定义视频源搜索：把 {kw} 换成关键词抓取该页，提取视频链接（yt-dlp 通用解析）。

    页面里优先取 <a href> 指向视频页的链接（title 取链接文本/alt/title），
    拿不到再退回 yt-dlp 对该页做 flat 提取。返回与内置平台同构的 entry 列表。
    """
    import html as _html
    search_url = (src.get("search_url") or "").replace("{kw}", requests.utils.quote(query))
    try:
        r = requests.get(search_url, headers={"User-Agent": UA}, timeout=15)
        r.raise_for_status()
    except Exception as exc:
        log.warning("custom source %s fetch failed: %s", src.get("name"), exc)
        raise RuntimeError(f"自定义源「{src.get('name')}」抓取失败: {exc}")
    ctype = (r.headers.get("content-type") or "").lower()
    if "html" not in ctype:
        # 非 HTML（如 JSON API）：尝试直接解析
        try:
            data = r.json()
        except Exception:
            data = None
        if isinstance(data, dict):
            items = data.get("items") or data.get("results") or data.get("videos") or data.get("data") or []
            if isinstance(items, dict):
                items = items.get("items") or items.get("results") or items.get("videos") or []
            if isinstance(items, list):
                out = []
                for it in items:
                    if not isinstance(it, dict):
                        continue
                    url = it.get("url") or it.get("link") or it.get("webpage_url") or it.get("href") or ""
                    title = it.get("title") or it.get("name") or ""
                    if not url or not title:
                        continue
                    if url.startswith("/"):
                        from urllib.parse import urljoin
                        url = urljoin(search_url, url)
                    out.append({
                        "id": uuid.uuid4().hex[:6],
                        "title": _html.unescape(str(title)),
                        "webpage_url": url,
                        "platform": src["id"],
                        "uploader": str(it.get("uploader") or it.get("author") or ""),
                        "duration": it.get("duration") or 0,
                        "thumbnail": str(it.get("thumbnail") or it.get("thumb") or ""),
                        "view_count": it.get("view_count") or it.get("views") or 0,
                    })
                return out[(page - 1) * limit: page * limit]
        raise RuntimeError(f"自定义源「{src.get('name')}」返回的不是可解析内容")
    # HTML 页面：正则提取视频链接
    import re as _re
    from urllib.parse import urljoin
    text = r.text
    out = []
    seen = set()
    # 常见视频链接形态：/watch?v= /video/ /play/ /detail/ /v/ 等
    for m in _re.finditer(
            r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', text,
            _re.IGNORECASE | _re.DOTALL):
        href, inner = m.group(1), m.group(2)
        if not href or href.startswith(("#", "javascript:", "mailto:")):
            continue
        full = urljoin(search_url, href)
        if full in seen:
            continue
        # 过滤明显不是视频页的链接
        if _re.search(r'\.(css|js|png|jpg|jpeg|gif|svg|ico|xml|json)(\?|$)', full, _re.I):
            continue
        if _re.search(r'/(tag|category|archive|page|search|login|register|about|contact)/', full, _re.I):
            continue
        title = _re.sub(r'<[^>]+>', '', inner)
        title = _html.unescape(title).strip()
        if not title:
            tm = _re.search(r'(?:title|alt)=["\']([^"\']+)["\']', inner, _re.I)
            if tm:
                title = _html.unescape(tm.group(1)).strip()
        if not title or len(title) < 2:
            continue
        seen.add(full)
        out.append({
            "id": uuid.uuid4().hex[:6],
            "title": title,
            "webpage_url": full,
            "platform": src["id"],
            "uploader": "",
            "duration": 0,
            "thumbnail": "",
            "view_count": 0,
        })
        if len(out) >= page * limit:
            break
    if not out:
        # 正则没抓到 → 退回 yt-dlp flat 提取
        try:
            opts = dict(BASE_OPTS, extract_flat=True, skip_download=True)
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(search_url, download=False)
            for e in (info.get("entries") or []) if info else []:
                if not e:
                    continue
                u = e.get("url") or e.get("webpage_url")
                t = e.get("title")
                if not u or not t:
                    continue
                out.append({
                    "id": e.get("id") or uuid.uuid4().hex[:6],
                    "title": t, "webpage_url": u,
                    "platform": src["id"],
                    "uploader": e.get("uploader") or "",
                    "duration": e.get("duration"),
                    "thumbnail": e.get("thumbnail") or "",
                    "view_count": e.get("view_count") or 0,
                })
        except Exception as exc:
            log.warning("custom source %s yt-dlp fallback failed: %s", src.get("name"), exc)
    return out[(page - 1) * limit: page * limit]


def search_videos(query, platform="all", limit=12, sort="relevance", page=1):
    """关键词搜索。platform=all 聚合全部平台并行搜索；
    sort=relevance 综合 / hot 最热门 / new 最新发布（各平台按官方
    排序接口返回，聚合交错混排）。

    page>=1 全平台按页加载：B 站 REST 真分页；
    AcFun 接口固定返回前 30 条，用缓存切片模拟分页（切完为空，
    聚合后由前端按 webpage_url 去重自然"加载完毕"）。"""
    sort = sort if sort in ("hot", "new") else "relevance"
    bili_order = {"hot": "click", "new": "pubdate"}.get(sort, "")
    page = max(1, int(page or 1))
    limit = max(1, int(limit or 12))
    if platform == "all":
        # 聚合搜索只跑「已启用」的平台（video_sources.json 控制，禁用即不显示也不搜索）
        enabled = _enabled_platform_ids()
        jobs = []
        if "bilibili" in enabled:
            jobs.append(("bilibili", lambda: _bilibili_search(query, limit, page, bili_order)))
        if "acfun" in enabled:
            jobs.append(("acfun", lambda: acfun_search(query, 30, page, sort)))
        if "youtube" in enabled:
            jobs.append(("youtube", lambda: youtube_search(query, 30, page, sort)))
        merged = None
        if jobs:
            with ThreadPoolExecutor(max_workers=len(jobs)) as ex:
                futs = {ex.submit(fn): name for name, fn in jobs}
                groups = []
                for fut, name in futs.items():
                    try:
                        groups.append(fut.result())
                    except Exception as exc:
                        log.warning("%s search failed: %s", name, exc)
            groups = [g for g in groups if g]
            if groups:
                merged = _merge_results(groups, sort, limit)
        if not merged:
            raise RuntimeError("all platform searches failed")
    elif platform in ("bilibili", "acfun", "youtube"):
        if not _platform_enabled(platform):
            raise ValueError(f"platform '{platform}' 已被禁用，请在视频源设置里启用后再搜索")
        if platform == "bilibili":
            merged = _merge_results([_bilibili_search(query, limit, page, bili_order)], sort, limit)
        elif platform == "acfun":
            merged = _merge_results([acfun_search(query, limit, page, sort)], sort, limit)
        else:
            merged = _merge_results([youtube_search(query, limit, page, sort)], sort, limit)
    elif platform.startswith("custom_"):
        # 自定义视频源：按 {kw} 模板抓取搜索页，提取视频链接（点播走 yt-dlp 通用解析）
        try:
            import video_sources_lib
            src = video_sources_lib.get_custom(platform)
        except Exception:
            src = None
        if not src or not src.get("enabled"):
            raise ValueError(f"自定义源 '{platform}' 不存在或已被禁用")
        merged = _custom_search(src, query, limit, page)
    else:
        raise ValueError(f"unknown platform '{platform}'; use all / bilibili / acfun / youtube / custom_*")
    schedule_prewarm(merged)
    return merged


def _bili_flat_candidates(query, count=3):
    """B 站 flat 搜索取前 N 条带标题的候选（跳过逐条元数据解析，点播提速）。"""
    opts = dict(BASE_OPTS, extract_flat=True, skip_download=True)
    ck = bili_cookiefile()
    if ck:
        opts["cookiefile"] = ck
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"bilisearch{count + 3}:{query}", download=False)
    out = []
    for e in info.get("entries") or []:
        if not e:
            continue
        u = e.get("url") or e.get("webpage_url")
        t = e.get("title") or ""
        if u and t and not t.startswith("http"):
            out.append(u)
            if len(out) >= count:
                break
    return out


# ---------------------------------------------------------------------------
# 播放 / 队列 / 控制
# ---------------------------------------------------------------------------
def _pick_and_play(url):
    entry = resolve(url, need_stream=True)
    with STATE_LOCK:
        STATE["now"] = entry
    return entry


def resolve_and_play(url, force=False):
    """按链接解析并设为当前播放（skill 的 index/url 点播路径；
    force=True 强制刷新直链，断流恢复用）。
    手动点播会退出"搜索结果连播"模式（清掉 search_ctx）。"""
    entry = resolve(url, need_stream=True, force=force)
    with STATE_LOCK:
        STATE["now"] = entry
        STATE.pop("search_ctx", None)
    return entry


def _set_search_ctx(query, platform, sort, page=1, pool=None):
    """记录关键词点播的搜索结果上下文：队列空时点 next 按此继续连播。

    pool 为轻量搜索条目（无 stream，播放时才解析）；None 表示懒填充
    （B 站 flat 快速路径不额外搜索，首次 next 时再搜第 1 页）。"""
    with STATE_LOCK:
        STATE["search_ctx"] = {
            "query": query, "platform": platform, "sort": sort,
            "page": page, "pool": pool or [], "idx": 0, "done": False,
        }


def play_by_query(query, platform="all", sort="relevance"):
    """关键词点播：搜索取第一条并立即播放，其余结果存入"搜索结果池"，
    队列空时点 next 自动按搜索结果继续连播（池耗尽自动翻页）。
    B 站优先走 flat 快速路径（跳过逐条元数据解析，起播显著提速），
    候选逐个尝试（付费课/失效视频自动跳过），全失败再回退完整搜索。
    返回 entry（成功时含备选片名 alternatives）。"""
    if platform in ("all", "bilibili"):
        try:
            for u in _bili_flat_candidates(query):
                try:
                    entry = _pick_and_play(u)
                    _set_search_ctx(query, platform, sort, page=1, pool=None)
                    return entry
                except Exception as exc:
                    log.warning("flat candidate failed %s: %s", u, exc)
        except Exception as exc:
            log.warning("bili flat pick failed: %s", exc)
    results = search_videos(query, platform if platform != "bilibili" else "bilibili", 5, sort)
    if not results:
        raise LookupError(f"no results for: {query}")
    url = results[0]["webpage_url"]
    alternatives = [r["title"] for r in results[1:4]]
    entry = _pick_and_play(url)
    _set_search_ctx(query, platform, sort, page=1, pool=results[1:])
    if alternatives:
        entry["alternatives"] = alternatives
    return entry


def _pop_next():
    with STATE_LOCK:
        nxt = STATE["queue"].pop(0) if STATE["queue"] else None
    if not nxt:
        # 队列空：有搜索结果上下文就按搜索结果继续连播（池耗尽自动翻页）
        nxt = _next_from_search_ctx()
        if not nxt:
            with STATE_LOCK:
                STATE["now"] = None
            return None
    if not nxt.get("stream"):
        try:
            nxt = resolve(nxt["webpage_url"], need_stream=True)
        except Exception as exc:
            log.warning("queue resolve failed: %s", exc)
            nxt["error"] = str(exc)
    with STATE_LOCK:
        STATE["now"] = nxt
    return nxt


def _next_from_search_ctx():
    """从搜索结果池取下一个轻量条目；池耗尽自动翻页拉下一页。

    返回轻量条目（webpage_url 必有，stream 由调用方补解析）；
    全部失败返回 None（结束连播）。"""
    with STATE_LOCK:
        ctx = STATE.get("search_ctx")
        if not ctx:
            return None
        query, platform, sort, page = ctx["query"], ctx["platform"], ctx["sort"], ctx["page"]
        pool, idx, done = ctx["pool"], ctx["idx"], ctx["done"]
    while True:
        if idx < len(pool):
            item = pool[idx]
            with STATE_LOCK:
                ctx["idx"] = idx + 1
            return item
        if done:
            return None
        # 池耗尽：翻下一页继续搜
        try:
            page += 1
            results = search_videos(query, platform, 5, sort, page=page)
        except Exception as exc:
            log.warning("search ctx next page %d failed: %s", page, exc)
            with STATE_LOCK:
                ctx["done"] = True
            return None
        if not results:
            with STATE_LOCK:
                ctx["done"] = True
            return None
        with STATE_LOCK:
            ctx["page"] = page
            ctx["pool"] = results
            ctx["idx"] = 1  # 下一页第 0 条就是"下一个"
        return results[0]


def pop_next():
    """当前视频播完：取队列下一部作为当前播放；队列空返回 None。"""
    return _pop_next()


def _add_queue(url):
    try:
        entry = resolve(url, need_stream=True)
    except Exception:
        entry = resolve(url, need_stream=False)
    with STATE_LOCK:
        STATE["queue"].append(entry)
    return entry


def queue_add(url=None, query=None, platform="all", sort="relevance"):
    """加入连播队列（query 时搜索取第一条）。返回 (entry, position)。"""
    if not url:
        if not query:
            raise ValueError("need 'query' or 'url'")
        results = search_videos(query, platform, 3, sort)
        if not results:
            raise LookupError(f"no results for: {query}")
        url = results[0]["webpage_url"]
    entry = _add_queue(url)
    with STATE_LOCK:
        STATE.pop("search_ctx", None)  # 手动加队列后 next 优先走队列
        position = len(STATE["queue"])
    return entry, position


def queue_list():
    with STATE_LOCK:
        return list(STATE["queue"])


def queue_remove(i=None, all_flag=False):
    with STATE_LOCK:
        if all_flag:
            STATE["queue"] = []
            return
        if i is not None and 0 <= i < len(STATE["queue"]):
            STATE["queue"].pop(i)


def control(action, value=None):
    """播放控制。next/stop 改变服务端状态；pause/resume/seek/volume/mute
    由前端大屏播放器本地执行（本函数仅校验动作合法性）。"""
    allowed = {"pause", "resume", "seek", "volume", "mute", "stop", "next"}
    if action not in allowed:
        raise ValueError(f"unknown action, allowed: {sorted(allowed)}")
    if action == "next":
        return {"next": _pop_next()}
    if action == "stop":
        with STATE_LOCK:
            STATE["now"] = None
    return {"ok": True}


def report(body):
    """前端播放器心跳（进度/音量），供 status 查看。"""
    with STATE_LOCK:
        STATE["report"] = {**body, "ts": time.time()}


# ---------------------------------------------------------------------------
# 流输出：直连代理 / ffmpeg 合流（供 server.py 端点调用）
# ---------------------------------------------------------------------------
def open_proxy(key, range_header=None):
    """打开 direct 直链流（带防盗链头，透传 Range）。返回 requests.Response（流式）。"""
    with CACHE_LOCK:
        item = STREAMS.get(key)
    if not item or item.get("kind") != "direct":
        raise KeyError("stream expired, re-play via video_play")
    headers = dict(item["headers"])
    if range_header:
        headers["Range"] = range_header
    proxies = {"http": _YT_PROXY, "https": _YT_PROXY} if item.get("platform") == "youtube" else None
    return requests.get(item["url"], headers=headers, proxies=proxies,
                        stream=True, timeout=(5, 30), allow_redirects=True)


def start_relay(key, transcode=False, ss=None):
    """启动 ffmpeg 实时合流（默认 -c copy 转封装，亚秒延迟）。
    ss>0 时在输入端定位到该秒（断流恢复的断点续播）。
    ?t=1 为转码兜底模式：源编码浏览器解不了（如 HEVC）时全转 h264+aac。
    返回 (Popen, mime)。"""
    with CACHE_LOCK:
        item = STREAMS.get(key)
    if not item or item.get("kind") != "relay":
        raise KeyError("stream expired, re-play via video_play")
    if not FFMPEG:
        raise RuntimeError("ffmpeg not installed; required for merging HD streams")

    ss_val = None
    try:
        if ss is not None and float(ss) > 0:
            ss_val = max(0.0, float(ss))
    except (TypeError, ValueError):
        ss_val = None

    cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-nostdin"]

    def add_input(fmt):
        hdr = fmt.get("headers") or {}
        if hdr:
            cmd.extend(["-headers", "".join(f"{k}: {v}\r\n" for k, v in hdr.items())])
        if FFMPEG_RECONNECT:
            # 抗瞬时断流：代理/CDN 秒级抖动掐死输入时允许 ffmpeg 自动重连续读，
            # 而不是整条流直接断掉触发前端 10s 卡死恢复
            cmd.extend(["-reconnect", "1", "-reconnect_streamed", "1",
                        "-reconnect_delay_max", "2"])
        if item.get("platform") == "youtube":
            cmd.extend(["-http_proxy", _ensure_fq_proxy(_YT_PROXY)])  # YouTube 源走 fq 代理
        if ss_val is not None:
            cmd.extend(["-ss", f"{ss_val:.2f}"])
        cmd.extend(["-i", fmt["url"]])

    add_input(item["video"])
    has_audio = bool(item.get("audio"))
    if has_audio:
        add_input(item["audio"])
        cmd.extend(["-map", "0:v:0", "-map", "1:a:0"])
    elif not item.get("hls"):
        # 纯视频单输入只留视频轨；HLS 单输入音视频合一，不加 -map 让 ffmpeg 自选
        cmd.extend(["-map", "0:v:0"])

    if transcode:
        cmd.extend(["-c:v", "libx264", "-preset", "veryfast", "-c:a", "aac",
                    "-b:a", "160k", "-f", "mp4",
                    "-movflags", "empty_moov+frag_keyframe+default_base_moof",
                    "pipe:1"])
    elif item.get("hls"):
        # TS 分片的音频可能是 ADTS AAC / MP3 等，统一转 AAC 规避容器与 bsf 兼容问题
        cmd.extend(["-c:v", "copy", "-c:a", "aac", "-b:a", "160k", "-f", "mp4",
                    "-movflags", "empty_moov+frag_keyframe+default_base_moof",
                    "pipe:1"])
    elif item.get("mime") == "video/webm":
        cmd.extend(["-c", "copy", "-f", "webm", "pipe:1"])
    else:
        # 容器不匹配：视频 mp4(h264) + 音频 webm(opus) 时，-c copy 会把 Opus
        # 硬塞进 mp4 容器，浏览器解不了报"源不兼容"。此时视频保持 copy，
        # 只把音频转成 AAC（CPU 开销小，亚秒延迟不受影响）。
        v_ext = (item.get("video") or {}).get("ext")
        a_ext = (item.get("audio") or {}).get("ext")
        if has_audio and v_ext == "mp4" and a_ext == "webm":
            cmd.extend(["-c:v", "copy", "-c:a", "aac", "-b:a", "160k", "-f", "mp4",
                        "-movflags", "empty_moov+frag_keyframe+default_base_moof",
                        "pipe:1"])
        else:
            cmd.extend(["-c", "copy", "-f", "mp4",
                        "-movflags", "empty_moov+frag_keyframe+default_base_moof",
                        "pipe:1"])

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def _drain():
        # 持续排空 stderr：合流进程长跑时若错误日志写满管道缓冲区，ffmpeg
        # 会阻塞在写 stderr 上、stdout 随之断流（表现为播放卡死）。退出时
        # 保留最后一行错误供诊断（启动秒退=选项不支持/源不可达也能看到原因）。
        tail = ""
        try:
            for line in iter(proc.stderr.readline, b""):
                s = line.decode("utf-8", "replace").strip()
                if s:
                    tail = s
        except Exception:
            pass
        if tail:
            log.warning("relay ffmpeg stderr tail: %s", tail[:200])

    threading.Thread(target=_drain, daemon=True).start()
    return proc, item.get("mime") or "video/mp4"


# fq 代理（YouTube 等外网站点）
_FQ_PROXY="http://127.0.0.1:7890"
_FQ_UA="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"

# ---------------------------------------------------------------------------
# fq 代理自动拉起：YouTube 走本地代理（127.0.0.1:7890）。
# 代理未启动时自动拉起 Clash（fq.cmd start clash），避免"点在线视频看不了
# YouTube"——之前代理硬编码但从不检查端口，Clash 没开时请求全部失败。
# 实现与 LLM/TTS 等云端出口共用 proxy_utils（共享"已尝试拉起"标记）。
# ---------------------------------------------------------------------------
from proxy_utils import ensure_fq_proxy as _ensure_fq_proxy  # noqa: E402

def _fq_get(url,timeout=20):
    return requests.get(url,headers={"User-Agent":_FQ_UA},proxies={"http":_FQ_PROXY,"https":_FQ_PROXY},timeout=timeout)

# ---------------------------------------------------------------------------
# YouTube support (fq proxy)：yt-dlp ytsearch 搜索 + 直链解析
# ---------------------------------------------------------------------------
_YT_PROXY = "http://127.0.0.1:7890"


def _yt_flat_thumb(e, vid):
    """flat 搜索条目只有 thumbnails 列表（新版 yt-dlp 不再输出 thumbnail 字段），
    取分辨率最高的一张；列表也缺时用官方 hqdefault 图床兜底（永远可用）。"""
    best = ""
    best_wh = -1
    for t in e.get("thumbnails") or []:
        if not (isinstance(t, dict) and t.get("url")):
            continue
        wh = (t.get("width") or 0) * (t.get("height") or 0)
        if wh > best_wh:
            best_wh = wh
            best = t["url"]
    return best or (f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg" if vid else "")


def youtube_search(query, limit=12, page=1, sort="relevance"):
    """YouTube 站内搜索（yt-dlp ytsearch，走 fq 代理）。

    ytsearch 不支持翻页/排序参数，这里一次取 (page*limit+3) 条再本地切片；
    sort 参数忽略（YouTube 综合排序即最相关）。"""
    page = max(1, int(page or 1))
    limit = max(1, int(limit or 12))
    total = page * limit + 3
    opts = dict(BASE_OPTS, extract_flat=True, skip_download=True, proxy=_YT_PROXY)
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"ytsearch{total}:{query}", download=False)
    out = []
    for e in info.get("entries") or []:
        if not e:
            continue
        vid = e.get("id") or ""
        if not vid:
            continue
        title = e.get("title") or ""
        if not title or title.startswith("http"):
            continue
        out.append({
            "id": vid,
            "title": title,
            "uploader": e.get("uploader") or e.get("channel") or "",
            "duration": e.get("duration"),
            "thumbnail": e.get("thumbnail") or _yt_flat_thumb(e, vid),
            "view_count": e.get("view_count") or 0,
            "webpage_url": f"https://www.youtube.com/watch?v={vid}",
            "platform": "youtube",
        })
        if len(out) >= total:
            break
    start = (page - 1) * limit
    return out[start:start + limit]


# ---------------------------------------------------------------------------
# 热门 / 推荐：B站官方热门榜 + AcFun 全站日榜 + YouTube trending（fq 代理）。
# platform=all 并行抓取后复用 _merge_results 交错合并。
# ---------------------------------------------------------------------------
_HOT_CACHE = {}          # (platform, limit, page) -> (ts, [entry])
_HOT_CACHE_TTL = 300     # 5 分钟缓存，避免每次打开弹窗都打外网
_HOT_CACHE_LOCK = threading.Lock()


def _bilibili_hot(limit=12, page=1):
    """B 站官方热门榜：x/web-interface/popular（复用 buvid cookie 预热防 412）。"""
    page = max(1, int(page or 1))
    limit = max(1, int(limit or 12))
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Referer": "https://www.bilibili.com/",
                      "Accept-Language": "zh-CN,zh;q=0.9"})
    ck = bili_cookiefile()
    if ck:
        try:
            for ln in open(ck, encoding="utf-8"):
                ln = ln.strip()
                if not ln or ln.startswith("#"):
                    continue
                parts = ln.split("\t")
                if len(parts) >= 7:
                    s.cookies.set(parts[5], parts[6], domain=".bilibili.com")
        except Exception as exc:
            log.warning("read bili cookie file failed: %s", exc)
    if not s.cookies.get("buvid3"):
        s.cookies.set("buvid3", uuid.uuid4().hex[:16] + "infoc", domain=".bilibili.com")
    r = s.get("https://api.bilibili.com/x/web-interface/popular",
              params={"ps": min(limit, 20), "pn": page}, timeout=12)
    r.raise_for_status()
    data = r.json()
    if data.get("code") != 0:
        raise RuntimeError(f"bilibili popular api code={data.get('code')}: "
                           f"{data.get('message')}")
    out = []
    for v in data.get("data", {}).get("list") or []:
        bvid = v.get("bvid") or ""
        if not bvid:
            continue
        pic = str(v.get("pic") or "")
        if pic.startswith("//"):
            pic = "https:" + pic
        out.append({
            "id": bvid,
            "title": html.unescape(str(v.get("title") or "")).strip(),
            "uploader": (v.get("owner") or {}).get("name") or "",
            "duration": v.get("duration") or None,
            "thumbnail": pic,
            "view_count": (v.get("stat") or {}).get("view") or 0,
            "webpage_url": f"https://www.bilibili.com/video/{bvid}",
            "platform": "bilibili",
        })
        if len(out) >= limit:
            break
    return out


def acfun_hot(limit=12, page=1):
    """AcFun 首页推荐流（SSR 直出：猴子推荐 + 香蕉榜 + 各频道推荐）。

    抓取 https://www.acfun.cn/ 首页 HTML，解析全部视频卡片（data-mediaid），
    按 id 去重后本地按页切片（约 30 条，可连续下拉 2~3 页）；
    翻页超出首页条数时返回空列表（前端据此停止加载）。
    """
    page = max(1, int(page or 1))
    limit = max(1, int(limit or 12))
    try:
        r = requests.get(
            "https://www.acfun.cn/",
            headers={"User-Agent": UA, "Referer": "https://www.acfun.cn/",
                     "Accept": "application/json, text/plain, */*"},
            timeout=12,
        )
        r.raise_for_status()
        text = r.text
    except Exception as exc:
        log.warning("acfun home fetch failed: %s", exc)
        raise RuntimeError(f"acfun home fetch failed: {exc}")
    out = []
    seen = set()
    # 首页 HTML 里卡片是转义引号（\"），按 data-mediaid 切块逐卡片解析
    blocks = re.split(r'data-mediaid=\\?"(\d+)\\?"', text)
    for i in range(1, len(blocks), 2):
        mid = blocks[i]
        if mid in seen:
            continue
        seen.add(mid)
        b = blocks[i + 1][:3000]
        # 标题：优先 data-title（纯标题）；否则取普通 title 属性第一行
        title = ""
        tm = re.search(r'data-title=\\?"([^"\\]*)', b)
        if tm:
            title = html.unescape(tm.group(1)).strip()
        info = ""
        tm2 = re.search(r'(?<!data-)title=\\?"([^"\\]*)', b)
        if tm2:
            info = html.unescape(tm2.group(1)).strip()
        if not title and info:
            first = re.split(r'[\r\n]+', info)[0].strip()
            if first:
                title = first
        if not title:
            continue
        href = ""
        hm = re.search(r'href=\\?"(/v/ac\d+)\\?"', b)
        if hm:
            href = hm.group(1)
        else:
            hm2 = re.search(r'href="(/v/ac\d+)"', b)
            if hm2:
                href = hm2.group(1)
        if not href:
            continue
        # 封面
        img = ""
        im = re.search(r'<img src=\\?"([^"\\]+)', b)
        if im:
            img = im.group(1)
        # 时长
        dur = 0
        dm = re.search(r'video-time\\?">([^<\\]*)</span>', b)
        if dm:
            parts = str(dm.group(1)).strip().split(":")
            if len(parts) in (2, 3) and all(p.isdigit() for p in parts):
                dur = sum(int(p) * 60 ** i for i, p in enumerate(reversed(parts)))
        # UP 主：优先 monkey-up-name，否则从 title 属性「UP:xxx」提取
        up = ""
        um = re.search(r'monkey-up-name[^>]*href=\\?"/u/(\d+)\\?"[^>]*title=\\?"([^"\\]*)', b)
        if um:
            up = html.unescape(um.group(2)).strip()
        if not up:
            up_m2 = re.search(r'UP[:：]\s*([^\s&#]+)', info)
            if up_m2:
                up = up_m2.group(1).strip()
        # 点击量：优先卡片内「点击:NNNN」，否则从 title 属性提取
        view = 0
        vm = re.search(r'点击[:：](\d+)', b)
        if vm:
            view = int(vm.group(1))
        if not view:
            v_m2 = re.search(r'点击[:：]\s*(\d+)', info)
            if v_m2:
                view = int(v_m2.group(1))
        out.append({
            "id": mid,
            "title": title,
            "uploader": up,
            "duration": dur,
            "thumbnail": img,
            "view_count": view,
            "webpage_url": "https://www.acfun.cn" + href,
            "platform": "acfun",
        })
        if len(out) >= page * limit:
            break
    return out[(page - 1) * limit: page * limit]


_YT_CONSENT_LOCK = threading.Lock()
_YT_CONSENT = {"file": None}


def _yt_consent_cookiefile():
    """YouTube EU 同意页绕过：写静态 CONSENT/SOCS cookie 文件（无需联网预热）。"""
    with _YT_CONSENT_LOCK:
        if _YT_CONSENT["file"] and os.path.exists(_YT_CONSENT["file"]):
            return _YT_CONSENT["file"]
        path = os.path.join(tempfile.gettempdir(), "video_lib_yt_consent_cookies.txt")
        with open(path, "w") as f:
            f.write("# Netscape HTTP Cookie File\n")
            f.write(".youtube.com\tTRUE\t/\tTRUE\t0\tCONSENT\tYES+cb.20210328-17-p0.en+US+000\n")
            f.write(".youtube.com\tTRUE\t/\tTRUE\t0\tSOCS\tCAI\n")
        _YT_CONSENT["file"] = path
        return path


def youtube_hot(limit=12, page=1):
    """YouTube 热门（yt-dlp trending，走 fq 代理）。

    部分区域无登录会把 /feed/trending 重定向回首页（yt-dlp 直接报错），
    此时回退到带 bp 的 trending 子榜（gaming most popular，yt-dlp 自测可用）。
    """
    page = max(1, int(page or 1))
    limit = max(1, int(limit or 12))
    total = page * limit + 3
    opts = dict(BASE_OPTS, extract_flat=True, skip_download=True, proxy=_YT_PROXY,
                cookiefile=_yt_consent_cookiefile())
    urls = [
        "https://www.youtube.com/feed/trending",
        "https://www.youtube.com/feed/trending?bp=4gIcGhpnYW1pbmdfY29ycHVzX21vc3RfcG9wdWxhcg%3D%3D",
    ]
    entries = []
    last_err = None
    for u in urls:
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(u, download=False)
            entries = info.get("entries") or []
            if entries:
                break
        except Exception as exc:
            last_err = exc
            log.warning("youtube hot failed (%s): %s", u, str(exc)[:120])
    if not entries:
        raise RuntimeError(f"youtube trending failed: {last_err}")
    out = []
    for e in entries:
        if not e:
            continue
        vid = e.get("id") or ""
        if not vid:
            continue
        title = e.get("title") or ""
        if not title or title.startswith("http"):
            continue
        out.append({
            "id": vid,
            "title": title,
            "uploader": e.get("uploader") or e.get("channel") or "",
            "duration": e.get("duration"),
            "thumbnail": e.get("thumbnail") or _yt_flat_thumb(e, vid),
            "view_count": e.get("view_count") or 0,
            "webpage_url": f"https://www.youtube.com/watch?v={vid}",
            "platform": "youtube",
        })
        if len(out) >= total:
            break
    start = (page - 1) * limit
    return out[start:start + limit]


def hot_videos(platform="all", limit=12, page=1):
    """热门 / 推荐聚合：按平台返回官网热门列表，字段与搜索结果一致。

    platform=all 并行抓取各平台热门后复用 _merge_results 交错合并；
    单平台失败只记日志，不拖垮其他平台；全部失败才抛错。
    """
    limit = max(1, min(int(limit or 12), 24))
    page = max(1, int(page or 1))
    if platform not in ("all", "bilibili", "acfun", "youtube"):
        raise ValueError(f"unknown platform '{platform}'; use all / bilibili / acfun / youtube")
    if platform != "all" and not _platform_enabled(platform):
        raise ValueError(f"platform '{platform}' 已被禁用，请在视频源设置里启用后再看热门")
    # 缓存 key 含「启用的平台集合」：关闭/开启视频源后自动失效，避免聚合热门仍带已禁用平台
    ckey = (platform, limit, page, tuple(sorted(_enabled_platform_ids())))
    now = time.time()
    with _HOT_CACHE_LOCK:
        hit = _HOT_CACHE.get(ckey)
        if hit and now - hit[0] < _HOT_CACHE_TTL:
            return hit[1]
    if platform == "all":
        # 聚合热门只跑「已启用」的平台
        enabled = _enabled_platform_ids()
        jobs = []
        if "bilibili" in enabled:
            jobs.append(("bilibili", lambda: _bilibili_hot(limit, page)))
        if "acfun" in enabled:
            jobs.append(("acfun", lambda: acfun_hot(limit, page)))
        if "youtube" in enabled:
            jobs.append(("youtube", lambda: youtube_hot(limit, page)))
        merged = None
        if jobs:
            with ThreadPoolExecutor(max_workers=len(jobs)) as ex:
                futs = {ex.submit(fn): name for name, fn in jobs}
                groups = []
                for fut, name in futs.items():
                    try:
                        groups.append(fut.result())
                    except Exception as exc:
                        log.warning("%s hot fetch failed: %s", name, str(exc)[:120])
            groups = [g for g in groups if g]
            if groups:
                merged = _merge_results(groups, "hot", limit)
        if not merged:
            raise RuntimeError("all platform hot fetch failed")
    elif platform == "bilibili":
        merged = _merge_results([_bilibili_hot(limit, page)], "hot", limit)
    elif platform == "acfun":
        merged = _merge_results([acfun_hot(limit, page)], "hot", limit)
    else:
        merged = _merge_results([youtube_hot(limit, page)], "hot", limit)
    with _HOT_CACHE_LOCK:
        _HOT_CACHE[ckey] = (time.time(), merged)
        if len(_HOT_CACHE) > 32:
            oldest = sorted(_HOT_CACHE.items(), key=lambda kv: kv[1][0])
            for k, _ in oldest[:16]:
                _HOT_CACHE.pop(k, None)
    schedule_prewarm(merged)
    return merged


# ---------------------------------------------------------------------------
# 搜索预热：YouTube 全量解析要走代理多跳（好时段 ~5s，拥堵时更久），
# 是点播延迟的大头。搜索结果返回后后台静默预解析前几条 → 用户点播时
# 直接命中 RESOLVE_CACHE（resolve 同一入口），起播只剩 ffmpeg ~2s。
# ---------------------------------------------------------------------------
_PREWARM_LOCK = threading.Lock()
_PREWARM_DONE = {}      # url -> 上次预热完成时间（失败也算，防止连续搜索反复重试）
_PREWARM_INFLIGHT = set()
_PREWARM_SEM = threading.Semaphore(2)   # 全局并发上限，避免触发 YouTube 风控
_PREWARM_TTL = 2700     # 预热有效期（略短于 RESOLVE_CACHE 的 1h，留出点击余量）
_PREWARM_MAX_DONE = 256


def _prewarm_worker(urls):
    for u in urls:
        now = time.time()
        with _PREWARM_LOCK:
            if u in _PREWARM_INFLIGHT or _PREWARM_DONE.get(u, 0) > now - _PREWARM_TTL:
                continue
            _PREWARM_INFLIGHT.add(u)
        try:
            with _PREWARM_SEM:
                resolve(u, need_stream=True)
            log.info("prewarm ok: %s", u)
        except Exception as exc:
            log.warning("prewarm failed %s: %s", u, str(exc)[:120])
        finally:
            with _PREWARM_LOCK:
                _PREWARM_INFLIGHT.discard(u)
                if len(_PREWARM_DONE) > _PREWARM_MAX_DONE:
                    oldest = sorted(_PREWARM_DONE.items(), key=lambda kv: kv[1])
                    for k, _ in oldest[:_PREWARM_MAX_DONE // 4]:
                        _PREWARM_DONE.pop(k, None)
                _PREWARM_DONE[u] = time.time()


def schedule_prewarm(results, count=3):
    """对搜索结果里的前 count 条 YouTube 视频发起后台预解析（静默失败）。"""
    urls, seen = [], set()
    for r in results or []:
        if r.get("platform") != "youtube":
            continue
        u = r.get("webpage_url")
        if not u or u in seen:
            continue
        seen.add(u)
        urls.append(u)
        if len(urls) >= count:
            break
    if urls:
        threading.Thread(target=_prewarm_worker, args=(urls,), daemon=True).start()
    return len(urls)
