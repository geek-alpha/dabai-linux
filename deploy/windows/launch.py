#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Windows 侧启动器：把 secrets.env 注入环境变量后启动 server.py。

Linux 上这步由 systemd 的 ``EnvironmentFile=`` 完成 —— 服务进程天然带着密钥。
Windows 的计划任务没有等价物（Action 只能给一条命令行，不能挂环境文件），
所以中间放一个启动器，让**交互式启动和服务启动走同一条路径**，
避免「手动跑有密钥、服务跑没密钥」这种最难查的差异。

用法：
    python deploy\\windows\\launch.py            # 启动 server.py
    python deploy\\windows\\launch.py --check    # 只报告会注入哪些变量名（不打印值）

密钥来源：%APPDATA%\\dabai\\secrets.env（由 deploy/secrets/sync_secrets.py 生成，
可用 DABAI_SECRETS_FILE 覆盖）。文件不存在时不报错 —— 没配密钥的大白只是少了
外部 API 能力，不该连启动都起不来。
"""
from __future__ import annotations

import argparse
import os
import re
import runpy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent

# 与 sync_secrets.py 的 NAME_RE 逐字一致：只认大写变量名。
# 同一个文件被两个解析器读出不同结果，是最难查的那类差异。
_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")


def secrets_file() -> Path:
    override = os.environ.get("DABAI_SECRETS_FILE")
    if override:
        return Path(override)
    base = os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming")
    return Path(base) / "dabai" / "secrets.env"


def parse_env_file(text: str) -> dict[str, str]:
    """保守解析 KEY='...' / KEY=...；只认大写变量名，其余原样跳过。

    与 sync_secrets.py 的解析规则保持一致：值一律由同步器单引号包裹，
    这里只剥一层引号，不做 shell 展开（不做展开才不会把 ``$`` 吃成变量）。
    """
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, val = line.partition("=")
        name = name.strip()
        val = val.strip()
        if not _NAME_RE.match(name):
            continue
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "'\"":
            val = val[1:-1]
        out[name] = val
    return out


def load_into_environ() -> tuple[list[str], list[str]]:
    """把密钥读进 os.environ。返回（注入的变量名, 已存在被跳过的变量名）。

    已存在的变量不覆盖：显式 ``set`` 的临时值优先级高于持久化文件，
    这样「临时换一个 key 跑一次」的行为符合直觉。
    """
    path = secrets_file()
    if not path.is_file():
        return [], []
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        print(f"[!] 读不到密钥文件 {path}：{exc}", file=sys.stderr)
        return [], []
    injected, kept = [], []
    for name, value in parse_env_file(text).items():
        if name in os.environ:
            kept.append(name)
            continue
        os.environ[name] = value
        injected.append(name)
    return injected, kept


def main() -> int:
    ap = argparse.ArgumentParser(description="大白 Windows 启动器")
    ap.add_argument("--check", action="store_true",
                    help="只报告会注入哪些变量名（不打印值），不启动")
    ap.add_argument("--entry", default="server.py", help="要启动的入口文件")
    ap.add_argument("rest", nargs="*", help="透传给入口文件的参数")
    args = ap.parse_args()

    injected, kept = load_into_environ()
    path = secrets_file()
    if args.check:
        print(f"密钥文件：{path}（{'存在' if path.is_file() else '不存在'}）")
        print(f"注入 {len(injected)} 个变量：{', '.join(injected) or '(无)'}")
        if kept:
            print(f"已存在被跳过 {len(kept)} 个：{', '.join(kept)}")
        return 0

    entry = ROOT / args.entry
    if not entry.is_file():
        print(f"[X] 找不到入口文件：{entry}", file=sys.stderr)
        return 1

    if injected:
        print(f"[i] 已注入 {len(injected)} 个密钥变量（{', '.join(injected)}）", flush=True)
    elif not path.is_file():
        print(f"[i] 没有密钥文件 {path} —— 缺外部 API 能力，但服务照常启动", flush=True)

    # 原地 exec：不另起进程，Ctrl+C / 退出码 / 信号都直接作用在 server.py 上
    sys.argv = [str(entry)] + args.rest
    os.chdir(ROOT)
    # runpy.run_path 只对「目录 / zip 形式的 sys.path 条目」插 sys.path，对普通 .py
    # 文件不插；而 `python server.py` 会把脚本所在目录放进 sys.path[0]。少了这一步，
    # server.py 里的 `import attach_text` 直接 ModuleNotFoundError —— 且只在走
    # bat / launch.py 时复现，手动 `python server.py` 一切正常，最难查的那类差异。
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    runpy.run_path(str(entry), run_name="__main__")
    return 0


if __name__ == "__main__":
    sys.exit(main())
