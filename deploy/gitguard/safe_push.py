#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""安全发布闸门（跨平台）—— 把本仓库推到 GitHub 之前，强制过三道密钥检查。

为什么有这个闸门：2025-06 ~ 2026-09 期间，Windows 侧的 github_auto_push 工具把多个
项目的 settings.json（含明文 API key）自动建仓推送到了公开仓库，泄漏 14 个月。
本脚本是那条路径的替代品：先验证、再推送；token 只在运行时注入，不落盘。

与 safe-push.sh 的关系：同一套判据的 Python 实现。bash 版在 Linux / Git Bash 上能跑，
但 Windows 上不保证有 bash，而大白本身必然有 Python。safe-push.sh 现在是本脚本的
薄包装 —— 两个平台跑同一份逻辑，不会各自漂移。

用法：
    python3 deploy/gitguard/safe_push.py geek-alpha/dabai-linux
    python3 deploy/gitguard/safe_push.py geek-alpha/dabai-linux --public
    python3 deploy/gitguard/safe_push.py geek-alpha/dabai-linux --dry-run
    python3 deploy/gitguard/safe_push.py geek-alpha/dabai-linux --proxy http://127.0.0.1:7890

token 来源（按优先级）：
    1) 环境变量 GITHUB_TOKEN
    2) 平台默认 secrets.env（Linux /etc/dabai/secrets.env；Windows %APPDATA%\\dabai）
    3) ~/.config/dabai/secrets.env
注入方式：git -c http.https://github.com/.extraheader=Authorization: Basic ...
    token 不进命令行参数、不进 remote URL、不进 .git/config。
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
SELF = "deploy/gitguard/safe_push.py"

# 独立复核（不依赖自家扫描器）用的形态。三条分开：真私钥的头独占一行，而代码里把它
# 当字符串处理时头嵌在表达式中间（Cryptodome 的 startswith(b'-----BEGIN OPENSSH
# PRIVATE KEY') 就是后者）。混成一条，vendored 库会把闸门常年卡红。
PATTERNS = (
    r"sk-[A-Za-z0-9]{20,}|ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|AKIA[0-9A-Z]{16}",
    r"^[[:space:]]*-----BEGIN [A-Z ]*PRIVATE KEY-----[[:space:]]*$",
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*[A-Za-z0-9+/]{64,}",
)
# 自身含上面的正则字面量，复核时必须排除，否则闸门永远卡在自己身上
EXCLUDE = {"deploy/gitguard/secretscan.py", "deploy/gitguard/safe-push.sh", SELF}

# 硬雷文件：这些一旦被 git 跟踪就是真泄漏，不看内容
HARD_FILES = (
    "settings.json", "codex_config.json", "stt_config.json", "tts_config.json",
    "cards.json", "character_cards.json", "nodes.json", "key.pem", "cert.pem",
    "data/mixamo_cookies.json",
)

SCAN_TIMEOUT = 900


def out(msg: str = "") -> None:
    print(msg, flush=True)


def ok(msg: str) -> None:
    out(f"  ✓ {msg}")


def die(msg: str) -> None:
    out()
    out(f"✗ 中止：{msg}")
    sys.exit(1)


def git(*args: str, timeout: int = 300) -> tuple[int, str]:
    """跑一条 git 命令，返回（退出码, 合并输出）。"""
    try:
        r = subprocess.run(["git", *args], cwd=str(ROOT), capture_output=True,
                           timeout=timeout, encoding="utf-8", errors="replace")
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except FileNotFoundError:
        die("找不到 git —— 先把 git 装好并放进 PATH")
        return 127, ""
    except subprocess.TimeoutExpired:
        return 124, ""


def secrets_env_candidates() -> list[Path]:
    """token 的持久化位置，按平台给默认值，外加用户级兜底。"""
    cands: list[Path] = []
    if os.name == "nt":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        cands.append(Path(base) / "dabai" / "secrets.env")
    else:
        cands.append(Path("/etc/dabai/secrets.env"))
    cands.append(Path.home() / ".config" / "dabai" / "secrets.env")
    return cands


def find_token() -> tuple[str, str]:
    """返回（token, 来源描述）。找不到返回（"", ""）。"""
    tok = os.environ.get("GITHUB_TOKEN", "").strip()
    if tok:
        return tok, "环境变量 GITHUB_TOKEN"
    for p in secrets_env_candidates():
        try:
            text = p.read_text(encoding="utf-8-sig")
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line.startswith("GITHUB_TOKEN="):
                continue
            val = line.split("=", 1)[1].strip()
            if len(val) >= 2 and val[0] == val[-1] and val[0] in "'\"":
                val = val[1:-1]
            if val:
                return val, str(p)
    return "", ""


def api(url: str, token: str, data: dict | None = None,
        method: str | None = None) -> tuple[int, dict]:
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, method=method, headers={
        "Authorization": "Bearer " + token,
        "Accept": "application/vnd.github+json",
        "User-Agent": "dabai-safe-push",
    })
    if body:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw or "{}")
        except ValueError:
            return e.code, {"message": raw[:200]}
    except Exception as e:  # 网络问题
        return 0, {"message": str(e)}


def ensure_repo(target: str, public: bool, token: str) -> None:
    st, d = api("https://api.github.com/repos/" + target, token)
    if st == 200:
        ok(f"仓库已存在（private={d.get('private')}，默认分支={d.get('default_branch')}）")
        if public and d.get("private"):
            out("  ! 你要 public 但现有仓库是 private —— 脚本不改可见性，请手动改")
        return
    if st == 404:
        name = target.split("/", 1)[1]
        st, d = api("https://api.github.com/user/repos", token, {
            "name": name,
            "private": not public,
            "description": "大白 Linux 原生版 —— 树莓派上的 AI 伙伴（已剥离全部密钥）",
            "has_issues": True,
        })
        if st in (200, 201):
            ok(f"已创建 {d.get('full_name')}（private={d.get('private')}）")
            return
        die(f"创建失败 HTTP={st} {d.get('message')}")
    die(f"查询失败 HTTP={st} {d.get('message')}")


def independent_check() -> None:
    """不依赖自家扫描器的独立复核：git grep 三条形态 + 硬雷文件。"""
    hits: list[str] = []
    for pat in PATTERNS:
        # -e 不能省：pattern 以 '-' 开头时 git grep 会把它当选项，错误又被吞掉，
        # 那条规则就成了永远不命中的死规则（静默失效比误报更危险）。
        rc, text = git("grep", "-nIE", "-e", pat, "HEAD", "--", ".", timeout=300)
        if rc not in (0, 1):
            continue
        for line in text.splitlines():
            path = line.split(":", 1)[0]
            if path in EXCLUDE:
                continue
            hits.append(line)
    if hits:
        for line in hits[:10]:
            out(f"    {line}")
        die("HEAD 里发现密钥形态")

    tracked = git("ls-files", timeout=120)[1].splitlines()
    tracked_set = {t.strip() for t in tracked}
    bad = [f for f in HARD_FILES if f in tracked_set]
    if bad:
        die(f"硬雷文件被跟踪：{', '.join(bad)}")
    ok(f"HEAD 无密钥形态（git grep 独立复核）；{len(HARD_FILES)} 类硬雷文件均未被跟踪")


def push(target: str, token: str, proxy: str) -> str:
    git("remote", "remove", "origin")
    git("remote", "add", "origin", f"https://github.com/{target}.git")
    rc, branch = git("rev-parse", "--abbrev-ref", "HEAD")
    branch = branch.strip()
    # token 只经 -c 注入：不进 remote URL、不进 .git/config、不进进程参数列表
    enc = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    args = ["-c", f"http.https://github.com/.extraheader=Authorization: Basic {enc}"]
    if proxy:
        args += ["-c", f"http.proxy={proxy}"]
        out(f"  经代理：{proxy}")
    try:
        r = subprocess.run(["git", *args, "push", "-u", "origin", branch],
                           cwd=str(ROOT), capture_output=True, timeout=1800,
                           encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        die("推送超时")
        return branch
    if r.returncode != 0:
        for line in ((r.stdout or "") + (r.stderr or "")).splitlines():
            out(f"    {line}")
        die("推送失败")
    for line in ((r.stdout or "") + (r.stderr or "")).splitlines():
        out(f"    {line}")
    return branch


def post_check(target: str, token: str, sha: str, branch: str) -> int:
    rc = 0
    st, d = api("https://api.github.com/repos/" + target, token)
    if st == 200:
        vis = "public ⚠ 任何人可见" if not d.get("private") else "private"
        ok(f"仓库可见性：{vis}")
        ok(f"体积：{d.get('size')} KB")
    else:
        out(f"  ✗ 查仓库失败 HTTP={st}")
        rc = 1

    st, d = api(f"https://api.github.com/repos/{target}/git/ref/heads/{branch}", token)
    remote_sha = (d.get("object") or {}).get("sha")
    if remote_sha == sha:
        ok(f"远端 {branch} = 本地 HEAD（{sha[:8]}）")
    else:
        out(f"  ✗ 远端 {branch}={str(remote_sha)[:8]} 与本地 {sha[:8]} 不一致")
        rc = 1
    return rc


def main() -> int:
    ap = argparse.ArgumentParser(add_help=True, description="安全发布闸门")
    ap.add_argument("target", nargs="?", help="目标仓库，如 geek-alpha/dabai-linux")
    ap.add_argument("--public", action="store_true", help="建成公开仓库（默认 private）")
    ap.add_argument("--private", action="store_true", help="建成私有仓库（默认）")
    ap.add_argument("--dry-run", action="store_true", help="只预检，不建仓不推送")
    ap.add_argument("--proxy", default=os.environ.get("https_proxy")
                    or os.environ.get("HTTPS_PROXY") or "", help="推送走的 HTTP 代理")
    args = ap.parse_args()

    visibility = "public" if args.public else "private"

    out(f"=== 安全发布闸门（仓库：{ROOT}）===")
    out()

    out("① 工作区必须干净")
    rc, status = git("status", "--porcelain")
    if status.strip():
        for line in git("status", "--short")[1].splitlines()[:20]:
            out(f"    {line}")
        die("有未提交改动 —— 先提交或 stash，否则校验的不是待发布内容")
    ok("干净")

    out()
    out("② 密钥扫描（三道：暂存区 / 跟踪文件 / 全历史）")
    scanner = ROOT / "deploy" / "gitguard" / "secretscan.py"
    for mode in ("--staged", "--tree", "--history"):
        try:
            r = subprocess.run([sys.executable, str(scanner), mode], cwd=str(ROOT),
                               capture_output=True, timeout=SCAN_TIMEOUT,
                               encoding="utf-8", errors="replace")
            ec, text = r.returncode, (r.stdout or "") + (r.stderr or "")
        except subprocess.TimeoutExpired:
            ec, text = 124, ""
        if ec == 0:
            ok(f"{mode} 通过")
            continue
        for line in text.splitlines():
            out(f"    {line}")
        # 超时（124）不是「发现密钥」：全历史扫描本机约 180s，仓库越大越慢，
        # 把「太慢」报成「不安全」会让闸门失去可信度。
        if ec == 124:
            die(f"{mode} 扫描超时（{SCAN_TIMEOUT}s）—— 是闸门太慢，不是发现密钥")
        die(f"{mode} 未通过")

    out()
    out("③ 独立验证（不依赖自家扫描器）")
    independent_check()

    out()
    if not args.target:
        die("未指定目标仓库，例：python3 deploy/gitguard/safe_push.py geek-alpha/dabai-linux")
    out(f"④ 目标：{args.target}（{visibility}）")

    out()
    out("⑤ 取 token")
    token, src = find_token()
    if token:
        ok(f"已取到 token（{token[:4]}***，来源 {src}，不落盘、不进命令行）")
    else:
        out("  ✗ 没找到 GITHUB_TOKEN（按序找：环境变量 → 平台 secrets.env → ~/.config/dabai/secrets.env）")
        out("    系统级持久化： sudo dabai-secrets set GITHUB_TOKEN ghp_xxx")
        out("    Windows： python deploy\\secrets\\sync_secrets.py set GITHUB_TOKEN ghp_xxx")
        if not args.dry_run:
            die("缺少 token，无法建仓/推送")

    if args.dry_run:
        out()
        out("✓ 预检全通过（--dry-run：未建仓、未推送）")
        out(f"  正式推送：python3 deploy/gitguard/safe_push.py {args.target} --{visibility}")
        return 0

    out()
    out("⑥ 确保远程仓库存在")
    ensure_repo(args.target, args.public, token)

    out()
    out("⑦ 推送")
    branch = push(args.target, token, args.proxy)
    sha = git("rev-parse", "HEAD")[1].strip()

    out()
    out("⑧ 推送后自检")
    rc_self = post_check(args.target, token, sha, branch)

    out()
    _, origin = git("remote", "get-url", "origin")
    out(f"  远端 URL 里不含凭据：{'✗ 含凭据' if '@' in origin else '✓'}")

    if rc_self != 0:
        out()
        out("✗ 自检未通过 —— 请人工确认后再对外公布地址")
        return 1

    out()
    out(f"✓ 完成：https://github.com/{args.target}")
    out(f"  可见性：{visibility}")
    out()
    out("  ⚠ Windows 侧的 github_auto_push 若仍把本项目目录列为目标，它会按旧路径再推一遍")
    out("    （并可能把 settings.json 带上去）—— 先改它或停掉，再发布地址。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
