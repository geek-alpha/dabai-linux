#!/usr/bin/env python3
"""启动前依赖自检 —— 缺「启动必需」包就打印清单并以 1 退出。

单一来源：清单与 requirements-core.txt 的启动必需段同源。dabai.sh / dabai.bat /
tools/selfcheck.py 都调这里，避免三处各写一份清单、各自漂 —— 以前 dabai.sh 与
dabai.bat 各内联了 5 个包，于是 uvloop / httptools / python-multipart / websockets
全在盲区：装完判定「齐全」，问题拖到启动时才炸。

用法：
    python3 tools/check_deps.py          # 人类可读；缺启动必需 → 退出 1
    python3 tools/check_deps.py --json   # 机器可读
    python3 tools/check_deps.py --gate   # 连「能力依赖」有缺也退出 1（供 --setup 判定）
"""
from __future__ import annotations

import importlib.util
import json
import sys

# (import 名, requirements 里的包名, 是否 Windows 豁免)
# 启动必需：缺任意一项 server.py 都起不来。
# 依据不是手写清单，是 tools/deps_audit.py 从 server.py 沿 import 图扫出的启动路径依赖。
#   python-multipart：server.py:41 顶层 import 了 FastAPI 的 UploadFile / File，
#                     缺它 FastAPI 在加载期就抛异常（不是运行到上传接口才报）。
#                     代码里没有它的静态 import（FastAPI 内部按名加载），属间接依赖。
# 注：aiohttp / numpy / websockets 曾在这里，但它们不在启动路径上 ——
#     server.py 对它们零引用，按「启动必需」声明是清单虚高。已移到能力依赖。
REQUIRED: list[tuple[str, str, bool]] = [
    ("fastapi", "fastapi", False),
    ("uvicorn", "uvicorn", False),
    ("multipart", "python-multipart", False),
    ("starlette", "starlette", False),
    ("edge_tts", "edge-tts", False),
    ("openai", "openai", False),
    ("requests", "requests", False),
]

# 能力依赖：缺了服务照常启动，但对应功能整块失效或静默降级。
# 不并进 REQUIRED 的理由：会让「只想聊天」的用户被一条视频依赖挡在门外；
# 不干脆不查的理由：缺了什么都不说，用户只看到某个功能莫名其妙全废。
# (import 名, requirements 里的包名, 缺了会失去什么, 是否 Windows 豁免)
CAPABILITY: list[tuple[str, str, str, bool]] = [
    ("websockets", "websockets", "uvicorn 的 WebSocket 实现（缺了网页实时对话连不上）", False),
    ("uvloop", "uvloop", "事件循环加速（缺了退回 asyncio，功能不受影响）", True),
    ("httptools", "httptools", "HTTP 解析加速（缺了退回 h11，功能不受影响）", True),
    ("aiohttp", "aiohttp", "code_ops 远程工作区 / CLI 远程调用", False),
    ("numpy", "numpy", "技能数值计算（android 校验、appearance 姿态调整）与图像工具", False),
    ("httpx", "httpx", "异步 HTTP 客户端", False),
    ("certifi", "certifi", "HTTPS 根证书（缺了部分站点证书校验失败）", False),
    ("PIL", "Pillow", "图片缩略图与图像处理", False),
    ("yt_dlp", "yt-dlp", "视频搜索 / 热门 / 点播", False),
    ("tree_sitter", "tree-sitter", "代码结构感知检索（symbols / code_map）", False),
    ("mss", "mss", "截屏", False),
    ("netifaces", "netifaces", "网卡枚举兜底（本机 IP 展示）", False),
    ("gradio_client", "gradio_client", "Gradio 客户端能力", False),
    ("playwright", "playwright", "浏览器自动化（Mixamo 动作下载 / 网页抓取）", False),
    ("cryptography", "cryptography", "联邦签名（缺了退回 HMAC 并标注降级）", False),
    ("pypdf", "pypdf", "PDF 附件正文提取（attach_text.py:56）", False),
    ("qrcode", "qrcode", "手机配对二维码（server.py:1818）", False),
]


def _exempt(win_exempt: bool) -> bool:
    return win_exempt and sys.platform == "win32"


def _found(mod: str) -> bool:
    try:
        return importlib.util.find_spec(mod) is not None
    except (ImportError, ValueError):
        return False


def missing() -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for mod, pkg, win_exempt in REQUIRED:
        if _exempt(win_exempt):
            continue
        if not _found(mod):
            out.append((mod, pkg))
    return out


def missing_capability() -> list[tuple[str, str, str]]:
    """返回缺失的能力依赖：(import 名, 包名, 会失去什么)。"""
    return [(mod, pkg, what) for mod, pkg, what, ex in CAPABILITY
            if not _exempt(ex) and not _found(mod)]


def main() -> int:
    miss = missing()
    miss_cap = missing_capability()
    # --gate：只要「启动必需」或「能力依赖」有缺就退出 1。
    # 供 dabai.sh/bat 的 --setup 判定用 —— 用默认模式（缺能力依赖退出 0）会让
    # --setup 看到「齐全」直接跳过 pip install，缺的包永远补不上。
    gate = "--gate" in sys.argv
    if "--json" in sys.argv:
        print(json.dumps({
            "missing": [{"module": m, "package": p} for m, p in miss],
            "missing_capability": [{"module": m, "package": p, "loses": w}
                                   for m, p, w in miss_cap],
        }, ensure_ascii=False))
        return 1 if (miss or (gate and miss_cap)) else 0
    if miss:
        print("[X] 缺少启动必需依赖：" + ", ".join(p for _, p in miss))
        print("    安装：python tools/pip_mirror.py -r requirements-core.txt   # 自动挑国内镜像源")
        print("    或一键补齐：dabai.sh --setup  /  dabai.bat --setup")
        return 1
    if miss_cap:
        print("[!] 缺少能力依赖（服务能启动，以下功能不可用或降级）：")
        for _, pkg, what in miss_cap:
            print(f"      - {pkg}：{what}")
        print("    补齐：dabai.sh --setup  /  dabai.bat --setup")
        if gate:
            return 1
    checked = len([1 for _, _, e in REQUIRED if not _exempt(e)])
    if not miss_cap:
        print(f"[ OK ] 依赖齐全（启动必需 {checked} 项 + 能力依赖 {len(CAPABILITY)} 项，"
              f"平台 {sys.platform}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
