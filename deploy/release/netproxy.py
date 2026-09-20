#!/usr/bin/env python3
"""本机代理探测 —— 发版链路里唯一一份。

为什么需要：直连 GitHub 在国内常见 60KB/s 量级（26MB 的包要 6 分钟，还常在
中途超时断掉）；本机若跑着 sing-box/clash 一类代理，同一个包 7 秒下完。
代理开着、链路却不知道它存在 —— 这是慢的全部原因。

不猜、只探：环境变量优先，其次探本机常用端口，探不通返回 None（没装代理的
机器行为完全不变）。结果进程内缓存，不写盘。

update.py 里另有一份内嵌副本 —— 它被投递到各机、跑在仓库之外，不能依赖本
文件（否则就成了「用待更新的代码去校验更新是否安全」）。改这里的端口表时
那份也要改。
"""
from __future__ import annotations

import os
import socket
import urllib.request
from typing import Optional

PROXY_PORTS = (7890, 7891, 10809, 1080, 10808, 20171)
PROXY_ENV_VARS = ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy")
PROBE_TIMEOUT = 0.3

_cached: Optional[str] = None
_probed = False


def reset() -> None:
    """清掉探测缓存（测试用；正常流程不需要）。"""
    global _cached, _probed
    _cached, _probed = None, False


def detect_proxy() -> Optional[str]:
    """返回可用的代理 URL，没有就 None。"""
    global _cached, _probed
    if _probed:
        return _cached
    _probed = True
    for var in PROXY_ENV_VARS:
        val = os.environ.get(var, "").strip()
        if val:
            _cached = val
            return _cached
    for port in PROXY_PORTS:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=PROBE_TIMEOUT):
                _cached = f"http://127.0.0.1:{port}"
                return _cached
        except OSError:
            continue
    return None


def opener():
    """带代理的 opener；没代理时显式直连 —— 不被环境里残留的坏变量带跑。"""
    proxy = detect_proxy()
    mapping = {"http": proxy, "https": proxy} if proxy else {}
    return urllib.request.build_opener(urllib.request.ProxyHandler(mapping))


def proxy_env() -> dict:
    """给子进程（git / curl）用的代理环境变量。"""
    proxy = detect_proxy()
    if not proxy:
        return {}
    return {
        "HTTPS_PROXY": proxy, "HTTP_PROXY": proxy,
        "https_proxy": proxy, "http_proxy": proxy,
    }


def describe() -> str:
    proxy = detect_proxy()
    return f"走代理 {proxy}" if proxy else "直连（未探测到本机代理）"
