# -*- coding: utf-8 -*-
"""API key 发现（exa / tavily 共用）。

为什么单独一个模块：两个引擎的 key 查找逻辑一字不差，各抄一份必然分叉——
分叉后「一个能读到 key、另一个读不到」这种 bug 极难发现。

key 来源顺序：环境变量 → 密钥文件。
密钥文件默认 /etc/dabai/secrets.env（deploy/secrets/sync_secrets.py 的派生文件），
其中同步器只维护 MANAGED 标记块，块外是手工区、永不触碰——
所以手工追加一行 EXA_API_KEY=... 是安全的持久落点，不会被下次同步抹掉。
"""
from __future__ import annotations

import os


def secret_files() -> list:
    """密钥文件候选：环境变量指定 → 本机密钥文件 → 用户级密钥文件。"""
    paths = []
    env = (os.environ.get("DABAI_SECRETS_FILE") or "").strip()
    if env:
        paths.append(env)
    paths += ["/etc/dabai/secrets.env",
              os.path.expanduser("~/.config/dabai/secrets.env")]
    return paths


def key_from_file(env_name: str, path: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, val = line.partition("=")
                if k.strip() == env_name:
                    val = val.strip().strip("'\"")
                    if val:
                        return val
    except OSError:
        pass
    return ""


def find_key(env_name: str) -> str:
    v = (os.environ.get(env_name) or "").strip().strip("'\"")
    if v:
        return v
    for p in secret_files():
        v = key_from_file(env_name, p)
        if v:
            return v
    return ""


def key_source(env_name: str) -> str:
    """当前 key 的来源描述（诊断用，不泄露 key 本身）。"""
    if (os.environ.get(env_name) or "").strip().strip("'\""):
        return f"环境变量 {env_name}"
    for p in secret_files():
        if key_from_file(env_name, p):
            return f"密钥文件 {p}"
    return "未配置"


def missing_key_msg(env_name: str, key_url: str, what: str) -> str:
    return (
        f"未配置 {env_name} —— {what} 工具本身已就绪，只差一个 key（不需要任何 CLI/脚本）。\n"
        f"配置方式（任选其一，写入后立即生效，无需重启）：\n"
        f"  1) 临时：export {env_name}=<你的 key>\n"
        f"  2) 持久（推荐）：把下面一行追加到 /etc/dabai/secrets.env 的「手工变量区」\n"
        f"     （同步器只重写 MANAGED 块，块外永不触碰；该文件 root:wxf 0640，需 sudo）：\n"
        f"     {env_name}='<你的 key>'\n"
        f"申请 key：{key_url}"
    )
