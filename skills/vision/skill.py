# -*- coding: utf-8 -*-
"""看图技能（vision）—— 图片像素直接进模型上下文，不做文字转述。

本工具只做三件事：把图片（URL / 本地路径 / data URL）拿到本地磁盘、判定当前
模型看不看得见图、在结果**开头**留下 [[IMG:绝对路径]] 标记。真正把像素塞进请求体
的是 agent.py 的注入通道（_append_img_messages → 多模态 user 消息）。

为什么不做成外接 MCP server：读图能力判定要读本进程的 settings.json
（harness/vision_probe），注入通道也在本进程内；外接子进程两头都够不着，还得
为一张图多养一个常驻进程（这台 1GB 的 Pi 上不划算）。MCP server 返回的 image
content 最终也是落盘 + [[IMG:]] 标记，与本工具同一条路。
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import socket
import sys
from pathlib import Path
from urllib.parse import urlsplit

_BASE_DIR = Path(__file__).resolve().parents[2]
_IMG_DIR = _BASE_DIR / "data" / "vision_images"
_MAX_BYTES = 8 * 1024 * 1024
# 与 agent._IMG_MAX_PER_RESULT 对齐：agent 侧单次工具结果最多注入 4 张，
# 工具侧先卡住同一数字，模型收到的说明才不会与「注入上限」自相矛盾。
_MAX_IMAGES = 4
_KEEP_FILES = 60
_TIMEOUT = (8, 25)
_PROXY_URL = "http://127.0.0.1:7890"
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/125.0 Safari/537.36")
_DATA_URL_RE = re.compile(r"^data:image/([a-z0-9.+-]+);base64,(.+)$", re.I | re.S)
_EXT_BY_FORMAT = {"JPEG": ".jpg", "PNG": ".png", "GIF": ".gif", "WEBP": ".webp",
                  "BMP": ".bmp", "AVIF": ".avif", "TIFF": ".tiff", "ICO": ".ico"}


if str(_BASE_DIR) not in sys.path:
    sys.path.insert(0, str(_BASE_DIR))


_PRIVATE_SUFFIX = (".local", ".internal", ".localhost", ".lan")


def _is_private_host(host: str) -> bool:
    """本机/内网/链路本地地址。"""
    import ipaddress
    h = str(host or "").strip().strip("[]").lower()
    if not h:
        return True
    if h == "localhost" or h.endswith(_PRIVATE_SUFFIX):
        return True
    try:
        ip = ipaddress.ip_address(h)
    except ValueError:
        return False
    return bool(ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_unspecified)


def _url_guard(url: str) -> str:
    """非管理员不许用本工具访问本机/内网 URL（与 read_web 被禁同一个理由）。"""
    try:
        import sandbox as _sb
        actor = _sb.current()
    except Exception:
        actor = None
    if actor is None or actor.is_admin:
        return ""
    host = urlsplit(url).hostname or ""
    if _is_private_host(host):
        return f"拒绝：{host or '（无主机名）'} 是本机/内网地址，普通用户不能通过本工具访问"
    return ""


def _local_path(s: str) -> Path:
    """本地路径：非管理员按自己沙箱解析并拒绝越界，与其它工具同一道闸门。"""
    p = Path(os.path.expanduser(s))
    try:
        import sandbox as _sb
        actor = _sb.current()
    except Exception:
        actor = None
    if actor is not None and not actor.is_admin:
        return _sb.resolve_path(actor, s)
    if not p.is_absolute():
        p = _BASE_DIR / p
    return p


def _load_cfg() -> dict:
    try:
        return json.loads((_BASE_DIR / "settings.json").read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def _status() -> tuple:
    """(能不能看图, 模型名, 依据)。判定逻辑与 agent 侧注入同源，不另立一套。"""
    try:
        if str(_BASE_DIR) not in sys.path:
            sys.path.insert(0, str(_BASE_DIR))
        from harness.vision_probe import active_model, current_can_see
        cfg = _load_cfg()
        ok, why = current_can_see(cfg)
        return bool(ok), (active_model(cfg) or "(未指定)"), why
    except Exception as e:
        return False, "(未知)", f"读图能力判定失败（{e.__class__.__name__}: {e}）"


def _proxy_alive() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", 7890), timeout=0.4):
            return True
    except OSError:
        return False


def _download(url: str) -> tuple:
    """下载图片字节 → (bytes, 错误)。直连失败且本地代理在监听时走代理重试一次。"""
    try:
        import requests
    except ImportError:
        return b"", "本机没有 requests，无法下载图片"
    tries = [None]
    if _proxy_alive():
        tries.append({"http": _PROXY_URL, "https": _PROXY_URL})
    err = ""
    for proxies in tries:
        try:
            r = requests.get(url, headers={"User-Agent": _UA, "Accept": "image/*,*/*;q=0.8"},
                             timeout=_TIMEOUT, stream=True, proxies=proxies)
            try:
                if r.status_code != 200:
                    err = f"HTTP {r.status_code}"
                    continue
                ctype = (r.headers.get("Content-Type") or "").split(";")[0].strip().lower()
                if ctype and not ctype.startswith("image/"):
                    return b"", f"不是图片（Content-Type: {ctype}）—— 读网页正文请用 read_web"
                buf = bytearray()
                for chunk in r.iter_content(65536):
                    buf += chunk
                    if len(buf) > _MAX_BYTES:
                        return b"", f"超过单图上限 {_MAX_BYTES // 1048576}MB"
            finally:
                r.close()
            if not buf:
                err = "内容为空"
                continue
            return bytes(buf), ""
        except Exception as e:
            err = f"{e.__class__.__name__}: {str(e)[:90]}"
    return b"", err or "下载失败"


def _prune() -> None:
    """超过 _KEEP_FILES 张时按 mtime 删最旧的到一半；清理失败不影响本次落盘。"""
    try:
        files = [p for p in _IMG_DIR.iterdir() if p.is_file()]
        if len(files) <= _KEEP_FILES:
            return
        files.sort(key=lambda p: p.stat().st_mtime)
        for p in files[:len(files) - _KEEP_FILES // 2]:
            try:
                p.unlink()
            except OSError:
                pass
    except Exception:
        pass


def _store(raw: bytes) -> tuple:
    """校验是张真图 → 内容寻址落盘。返回 (路径, 描述, 错误)。"""
    try:
        import io
        from PIL import Image
        im = Image.open(io.BytesIO(raw))
        fmt = str(im.format or "").upper()
        w, h = im.size
    except Exception as e:
        return "", "", f"不是可解析的图片（{e.__class__.__name__}）"
    ext = _EXT_BY_FORMAT.get(fmt)
    if not ext:
        return "", "", f"图片格式 {fmt or '未知'} 不支持（支持 PNG/JPEG/GIF/WEBP/BMP/AVIF）"
    try:
        _IMG_DIR.mkdir(parents=True, exist_ok=True)
        path = _IMG_DIR / (hashlib.sha1(raw).hexdigest()[:16] + ext)
        if not path.exists():
            tmp = path.with_name(path.name + f".tmp{os.getpid()}")
            tmp.write_bytes(raw)
            os.replace(tmp, path)
            _prune()
        return str(path), f"{fmt} {w}×{h}，{len(raw) / 1024:.0f}KB", ""
    except Exception as e:
        return "", "", f"落盘失败（{e.__class__.__name__}）"


def _fetch_one(item: str) -> tuple:
    """一个来源 → (路径, 描述, 错误)。"""
    s = str(item or "").strip().strip('"').strip("'")
    if not s:
        return "", "", "空项"
    if s.lower().startswith("data:image/"):
        m = _DATA_URL_RE.match(s)
        if not m:
            return "", "", "data URL 解析失败（需要 data:image/xxx;base64,<数据>）"
        try:
            raw = base64.b64decode(m.group(2), validate=False)
        except Exception as e:
            return "", "", f"base64 解码失败（{e.__class__.__name__}）"
        return _store(raw)
    if s.lower().startswith(("http://", "https://")):
        deny = _url_guard(s)
        if deny:
            return "", "", deny
        raw, err = _download(s)
        if err:
            return "", "", err
        return _store(raw)
    try:
        p = _local_path(s)
    except Exception as e:
        return "", "", f"沙箱拒绝：{e}"
    if not p.is_file():
        return "", "", f"文件不存在：{p}"
    try:
        raw = p.read_bytes()
    except Exception as e:
        return "", "", f"读取失败（{e.__class__.__name__}）"
    if len(raw) > _MAX_BYTES:
        return "", "", f"超过单图上限 {_MAX_BYTES // 1048576}MB"
    return _store(raw)


def _items(args: dict) -> list:
    """归一化参数：支持数组 / JSON 数组字符串 / 换行分隔 / 单个 url。"""
    v = args.get("images")
    if v is None:
        v = args.get("urls")
    out = []
    if isinstance(v, (list, tuple)):
        out = [str(x) for x in v]
    elif isinstance(v, str):
        s = v.strip()
        if s.startswith("["):
            try:
                arr = json.loads(s)
                out = [str(x) for x in arr] if isinstance(arr, list) else [s]
            except Exception:
                out = [s]
        else:
            out = s.splitlines()
    single = args.get("url") or args.get("image")
    if isinstance(single, str) and single.strip():
        out.append(single)
    return [x for x in (str(i).strip() for i in out) if x]


def see_image(args: dict) -> str:
    items = _items(args or {})
    if not items:
        return ("请提供图片：images 传图片 URL（http/https）、本地路径或 data URL，"
                f"一次最多 {_MAX_IMAGES} 张。")
    ok, model, why = _status()
    if not ok:
        return (f"【读图不可用】当前模型 {model} 看不到图（判定依据：{why}）。"
                f"本工具没有注入任何图片。\n"
                f"要看图请换支持读图的模型（名字含 vision/vl/4o/claude/gemini 等），"
                f"或在配置页把该供应商的「读图能力」改成「支持读图」。")
    marks, lines, fails, seen = [], [], [], set()
    for it in items[:_MAX_IMAGES]:
        path, desc, err = _fetch_one(it)
        if err:
            fails.append(f"{it} → {err}")
            continue
        if path in seen:
            continue
        seen.add(path)
        marks.append(f"[[IMG:{path}]]")
        lines.append(f"{len(marks)}. {it}（{desc}）")
    if not marks:
        return (f"【看图失败】{len(fails)} 张图都没拿到：\n"
                + "\n".join("- " + f for f in fails))
    body = [f"【看图】当前模型 {model} 支持读图（{why}）。{len(marks)} 张图的像素已注入上下文，"
            f"直接说你看到的画面内容，别按文件名或 URL 猜。"]
    body += lines
    if fails:
        body.append("未取到：" + "；".join(fails))
    left = len(items) - _MAX_IMAGES
    if left > 0:
        body.append(f"另有 {left} 张未处理（单次上限 {_MAX_IMAGES} 张，防上下文超支）—— 需要时再单独调用。")
    return "\n".join(marks) + "\n" + "\n".join(body)


HANDLERS = {
    "see_image": see_image,
}
