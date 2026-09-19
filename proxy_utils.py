"""fq 代理自动拉起与大白云端 API 代理路由。

在线视频（YouTube）早已实现「端口没监听就自动拉起 Clash」，
本模块把同一套逻辑抽成共享实现，供 LLM 聊天 / TTS / STT / 模型列表 /
画图等所有云端出口复用（共享"已尝试拉起"标记，避免重复启动）。

settings.json 顶层配置 llm_proxy：
  - "" / "off"           直连（默认，不代理）
  - "auto"               自动走 fq 本地代理（Clash 7890），未监听时自动拉起
  - 其它字符串           自定义代理地址，如 http://127.0.0.1:7890 或
                          socks5://127.0.0.1:1080（socks 需要 httpx[socks]）
本地地址（Ollama 等）永远直连，不套代理。
"""
from __future__ import annotations

import logging
import socket
import subprocess
import threading
import time
from urllib.parse import urlparse

logger = logging.getLogger("proxy")

FQ_ROOT = r"D:\AI\Chrome141_AllNew_2025.10.3"
FQ_START_CMD = ["fq.cmd", "start", "clash", "-NoChrome", "-NoElevate"]
FQ_PROXY = "http://127.0.0.1:7890"

_FQ_LOCK = threading.Lock()
_FQ_STARTED = False  # 本次进程内已尝试过启动（失败也置位，避免反复拉起）


def port_listening(port: int) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(1)
    try:
        return s.connect_ex(("127.0.0.1", port)) == 0
    finally:
        s.close()


def ensure_fq_proxy(proxy: str = FQ_PROXY) -> str:
    """确保本地 fq 代理可用：端口未监听时自动拉起 Clash 并等待就绪。
    返回代理地址；启动失败返回原地址（让上层请求自然失败并记日志）。"""
    global _FQ_STARTED
    port = int(proxy.rsplit(":", 1)[1])
    if port_listening(port):
        return proxy
    if _FQ_STARTED:
        return proxy  # 本次进程已尝试过且失败，不再重复拉起
    with _FQ_LOCK:
        if _FQ_STARTED:
            return proxy
        _FQ_STARTED = True
        try:
            # Windows 上 .cmd 必须经 cmd.exe 解释执行（shell=True），
            # shell=False 直接 CreateProcess 会报 WinError 2 找不到文件
            subprocess.run(FQ_START_CMD, cwd=FQ_ROOT, shell=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            for _ in range(20):
                if port_listening(port):
                    logger.info("fq proxy auto-started: %s", proxy)
                    return proxy
                time.sleep(1)
            logger.warning("fq proxy auto-start timeout: %s not listening", proxy)
        except Exception as exc:
            logger.warning("fq proxy auto-start failed: %s", exc)
    return proxy


def is_local_url(url: str) -> bool:
    """本地/内网地址（Ollama、局域网模型服务等）不该套代理。"""
    try:
        host = (urlparse(url or "").hostname or "").lower()
    except Exception:
        return False
    if host in ("localhost", "127.0.0.1", "0.0.0.0", "::1"):
        return True
    if host.startswith("192.168.") or host.startswith("10."):
        return True
    if host.startswith("172."):
        try:
            if 16 <= int(host.split(".")[1]) <= 31:
                return True
        except (ValueError, IndexError):
            pass
    return False


def resolve_llm_proxy(cfg: dict | None):
    """settings.json 的 llm_proxy → 代理地址或 None。

    - "auto"：走 fq 本地代理（Clash 7890，未监听自动拉起，同在线视频）
    - 其它非空字符串：当作自定义代理地址原样返回
    - 空 / off：返回 None（直连）
    """
    val = str((cfg or {}).get("llm_proxy") or "").strip()
    if not val or val.lower() in ("off", "false", "none", "direct", "0"):
        return None
    if val.lower() == "auto":
        return ensure_fq_proxy(FQ_PROXY)
    return val


def requests_proxies(proxy) -> dict:
    """requests 用的 proxies 参数（代理为 None 时返回空 dict）。"""
    return {"http": proxy, "https": proxy} if proxy else {}
