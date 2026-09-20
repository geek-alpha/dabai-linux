#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一键发布大白新版本（本地把既有步骤串起来，发布实现仍只有 CI 一个）。

用法：
    python deploy/release/publish.py --check                   # 只做前置检查，不动任何东西
    python deploy/release/publish.py -m "改了啥"                # 升 patch 版并发布（最常用）
    python deploy/release/publish.py --bump minor -m "改了啥"
    python deploy/release/publish.py -m "改了啥" --commit-all   # 连未提交改动一起提
    python deploy/release/publish.py --dry-run -m "改了啥"      # 走到打包+测试，不提交不推送
    python deploy/release/publish.py --tag-only                 # VERSION 已升好，只补推 tag
    python deploy/release/publish.py -m "改了啥" --no-tests     # 紧急跳过测试（会明确警告）

七步，任何一步不过就停，不留半成品：
    ① 前置闸门   工作区 / 分支 / 是否落后远端 / VERSION 与最新 tag 是否对齐 / token
    ② 打包回验   build_release.py --bump（内含解包回验：sha256 全对、包内无受保护路径）
    ③ 跑测试     默认全量，不过就不发
    ④ 提交       VERSION（或 --commit-all 时的全部改动）并 push main
    ⑤ 打 tag     推 tag —— 这一步触发 CI
    ⑥ 盯落地     watch_release.py 轮询到 release 资产齐全才返回 0
    ⑦ 通知联邦   给地址簿里其它实例留言「新版可拉」（只在 ⑥ 成功后才发）

为什么推了 tag 还不等于发布：
    真正的 release 由 CI 的 publish 作业创建，而它挂在 environment: release 的
    required reviewers 上 —— 管理员必须在网页上点 Approve。所以本脚本跑到第 ⑥ 步
    会停在「等你批准」，不会自己把版本发出去。本地没有、也不该有直接上传资产的能力。

为什么不做成 shell 脚本：
    要用 git 的 GIT_CONFIG_* 环境变量注入 token，避免 token（或它的 base64）出现在
    命令行里被同机其它进程看见。safe-push.sh 用的是 -c 参数，这里更进一步。
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
RELEASE_DIR = Path(__file__).resolve().parent

# 同目录工具：代理探测（直连 GitHub 常见 60KB/s，走本机代理是 MB/s 量级）
if str(RELEASE_DIR) not in sys.path:
    sys.path.insert(0, str(RELEASE_DIR))
import netproxy  # noqa: E402
VERSION_FILE = ROOT / "VERSION"
DIST_DIR = RELEASE_DIR / "dist"
RELEASE_LOG = ROOT / "data" / "release_log.jsonl"

# 与 deploy/release/update.py 的 DEFAULT_CONFIG["REPO"] 同源；
# tests/test_release_publish.py 断言两者一致，改一处漏另一处会被测出来。
REPO_DEFAULT = "geek-alpha/dabai-linux"
BRANCH_DEFAULT = "main"


class Fail(Exception):
    """闸门不过。main 捕获后统一打印并退出 1。"""


def _p(msg: str = "") -> None:
    print(msg, flush=True)


def ok(msg: str) -> None:
    print(f"  \u2713 {msg}", flush=True)


def warn(msg: str) -> None:
    print(f"  \u26a0 {msg}", flush=True)


def die(msg: str) -> None:
    raise Fail(msg)


def _indent(text: str, prefix: str = "    ") -> None:
    for line in text.splitlines():
        print(prefix + line, flush=True)


def _python() -> str:
    """优先用仓库自带 venv —— 打包脚本和测试都依赖它的依赖。"""
    candidate = ROOT / "venv" / "bin" / "python"
    if candidate.exists():
        return str(candidate)
    return sys.executable


def _run(cmd: list, timeout: int | None = None, env: dict | None = None):
    # 子进程统一带代理：git push 和 CI 轮询都要出网，直连时是国内最慢的一段
    e = dict(os.environ if env is None else env)
    e.update(netproxy.proxy_env())
    return subprocess.run(
        cmd, cwd=str(ROOT), capture_output=True, text=True,
        timeout=timeout, env=e,
    )


def _auth_env(token: str) -> dict:
    """把 token 塞进环境变量而不是命令行。

    GIT_CONFIG_COUNT/KEY_0/VALUE_0 是 git 官方的「用环境变量传配置」通道，
    效果等同 `git -c key=value`，但不进 argv —— 同机任何用户都能读别人的
    命令行（/proc/<pid>/cmdline），而环境变量只有同 uid 读得到。
    """
    enc = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    env = dict(os.environ)
    env["GIT_CONFIG_COUNT"] = "1"
    env["GIT_CONFIG_KEY_0"] = "http.https://github.com/.extraheader"
    env["GIT_CONFIG_VALUE_0"] = f"Authorization: Basic {enc}"
    env["GIT_TERMINAL_PROMPT"] = "0"   # 缺 token 时直接失败，不要卡在交互式输入
    return env


def git(*args, token: str = "", check: bool = False):
    env = _auth_env(token) if token else None
    p = _run(["git", *args], env=env)
    if check and p.returncode != 0:
        die(f"git {' '.join(args)} 失败：\n    {p.stderr.strip() or p.stdout.strip()}")
    return p


def _token_paths() -> tuple:
    return (Path("/etc/dabai/secrets.env"), Path.home() / ".config" / "dabai" / "secrets.env")


def read_token(paths=None) -> str:
    """按序找 GITHUB_TOKEN：环境变量 → /etc/dabai/secrets.env → ~/.config/dabai/secrets.env。

    顺序与 deploy/gitguard/safe-push.sh 一致 —— 两处都改才算改，别只改一边。
    paths 只为测试注入，正常调用不传。
    """
    tok = os.environ.get("GITHUB_TOKEN", "").strip()
    if tok:
        return tok
    for path in (_token_paths() if paths is None else paths):
        try:
            if not path.is_file():
                continue
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.startswith("GITHUB_TOKEN="):
                    val = line.split("=", 1)[1].strip().strip('"').strip("'")
                    if val:
                        return val
        except (OSError, UnicodeDecodeError):
            continue
    return ""


def parse_version(text: str) -> tuple:
    """'1.0.10' → (1, 0, 10)。非数字段按 0 处理，别在这里抛异常。"""
    parts = []
    for chunk in str(text).strip().lstrip("v").split("."):
        num = "".join(ch for ch in chunk if ch.isdigit())
        parts.append(int(num) if num else 0)
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts[:3])


def latest_tag() -> str:
    p = git("tag", "--sort=-v:refname", "--list", "v*")
    tags = [t.strip() for t in p.stdout.splitlines() if t.strip()]
    return tags[0] if tags else ""


def preflight(args, token: str) -> dict:
    """只读检查。任一不过就 die，不做任何写入。"""
    info = {}
    _p("① 前置闸门")

    branch = git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    if branch != args.branch:
        die(f"当前分支 {branch}，发布必须在 {args.branch} 上做 —— "
            f"否则 tag 指向一个不在 {args.branch} 的提交，别人拉到的不是这版")
    ok(f"分支 {branch}")
    info["branch"] = branch

    dirty = [l for l in git("status", "--porcelain").stdout.splitlines() if l.strip()]
    if dirty and not args.commit_all:
        die("有未提交改动（前 20 条）—— 先提交，或加 --commit-all 把当前改动一起提交：\n    "
            + "\n    ".join(dirty[:20]))
    ok("工作区干净" if not dirty else f"有 {len(dirty)} 项未提交改动（--commit-all 会一起提交）")
    info["dirty"] = dirty

    git("fetch", "origin", args.branch, "--tags", token=token, check=True)
    behind = int(git("rev-list", "--count", f"HEAD..origin/{args.branch}").stdout.strip() or 0)
    ahead = int(git("rev-list", "--count", f"origin/{args.branch}..HEAD").stdout.strip() or 0)
    if behind:
        die(f"落后 origin/{args.branch} {behind} 个提交 —— 先 git pull --rebase，"
            f"否则这版是拿旧代码打的")
    ok(f"与 origin/{args.branch} 同步（本地领先 {ahead} 个待发布提交）")
    info["ahead"] = ahead

    ver = VERSION_FILE.read_text(encoding="utf-8").strip()
    tag = latest_tag()
    if tag and parse_version(ver) < parse_version(tag):
        die(f"VERSION {ver} 低于最新 tag {tag} —— 版本号被改回去了？先查清再发")
    if args.tag_only:
        if parse_version(ver) <= parse_version(tag):
            die(f"--tag-only 要求 VERSION({ver}) 高于最新 tag({tag or '（无）'})，"
                f"现在发不出去新东西")
        ok(f"--tag-only：直接补发 VERSION {ver}，不升号不打包")
    elif tag and parse_version(ver) != parse_version(tag):
        die(f"VERSION {ver} 与最新 tag {tag} 不一致 —— 上次发布可能没走完。\n"
            f"    回到已发布状态：git checkout -- VERSION\n"
            f"    或补发当前版本：--tag-only")
    else:
        ok(f"VERSION {ver} 与最新 tag {tag or '（无）'} 对齐")
    info["version"] = ver
    info["latest_tag"] = tag
    return info


def do_build(part: str) -> str:
    _p(f"② 打包回验（--bump {part}）")
    p = _run([_python(), str(RELEASE_DIR / "build_release.py"),
              "--bump", part, "--out", str(DIST_DIR)])
    _indent((p.stdout or p.stderr).strip())
    if p.returncode != 0:
        git("checkout", "--", "VERSION")
        die("打包/回验失败，已把 VERSION 恢复原值")
    ver = VERSION_FILE.read_text(encoding="utf-8").strip()
    ok(f"打包通过，版本 {ver}")
    return ver


def do_tests(timeout: int) -> None:
    _p("③ 测试（全量）")
    t0 = time.time()
    try:
        p = _run([_python(), "-m", "pytest", "-q"], timeout=timeout)
    except subprocess.TimeoutExpired:
        die(f"测试超过 {timeout}s 没跑完 —— 加 --tests-timeout 放宽，或用 --no-tests 明确跳过")
    lines = [l for l in (p.stdout or "").splitlines() if l.strip()]
    _indent("\n".join(lines[-6:]))
    if p.returncode != 0:
        die("测试未通过 —— 不发布。失败用例看上面")
    ok(f"测试全绿（{time.time() - t0:.0f}s）")


def do_commit(ver: str, note: str, commit_all: bool, dirty: list) -> None:
    _p("④ 提交")
    if commit_all and dirty:
        git("add", "-A", check=True)
        ok(f"暂存 {len(dirty)} 项改动 + VERSION")
    else:
        git("add", "VERSION", check=True)
    msg = f"v{ver}：{note}" if note else f"v{ver}"
    p = git("commit", "-m", msg)
    if p.returncode != 0:
        die(f"提交失败：\n    {p.stderr.strip() or p.stdout.strip()}")
    ok(f"commit：{msg}")


def do_push(branch: str, ver: str, token: str) -> None:
    _p("⑤ 推送 main 与 tag（触发 CI）")
    p = git("push", "origin", branch, token=token)
    if p.returncode != 0:
        die(f"推 main 失败：\n    {p.stderr.strip()}\n"
            f"    （提交已落在本地，修好后重跑本脚本会走 --tag-only 分支）")
    ok(f"main → origin/{branch}")

    tag = f"v{ver}"
    if git("rev-parse", "--verify", f"refs/tags/{tag}").returncode == 0:
        warn(f"tag {tag} 已存在（重发同一版本），不重复创建")
    else:
        git("tag", "-a", tag, "-m", f"大白 {tag}", check=True)
    p = git("push", "origin", tag, token=token)
    if p.returncode != 0:
        die(f"推 tag 失败：\n    {p.stderr.strip()}\n"
            f"    （tag 已在本地；删掉重来：git tag -d {tag}）")
    ok(f"tag {tag} → origin")


def do_watch(ver: str, timeout: int) -> int:
    _p("⑥ 盯 CI 直到 release 落地")
    _p("   publish 作业挂在 environment: release 上，需要管理员在网页点 Approve —— "
       "脚本会停在这里等")
    cmd = [_python(), str(RELEASE_DIR / "watch_release.py"), f"v{ver}",
           "--root", str(ROOT), "--timeout", str(timeout)]
    return subprocess.run(cmd, cwd=str(ROOT)).returncode


def _peer_nodes() -> list:
    """地址簿里的同伴，剔除自己。读不到就返回空 —— 通知不是发布的必要环节。"""
    try:
        nodes = json.loads((ROOT / "data" / "peers.json").read_text(encoding="utf-8"))
        me = json.loads((ROOT / "data" / "node.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    mine = (me or {}).get("node_id", "")
    return [n for n in sorted((nodes or {}).get("nodes", {})) if n != mine]


def do_notify(ver: str) -> dict:
    """给其它实例留言。任何失败都只警告 —— 包已经发出去了，通知失败不能改成发布失败。

    为什么只在 ⑥ 成功后才发：tag 推完不等于 release 建出来了（CI 要管理员 Approve）。
    提前喊「可以拉」会让对面去拉一个还不存在的资产，比不通知更坏。
    """
    _p("⑦ 通知联邦其它实例")
    nodes = _peer_nodes()
    if not nodes:
        warn("地址簿里没有别的实例（data/peers.json），跳过")
        return {"notified": [], "failed": []}

    text = (f"v{ver} 已发布：dabai-{ver}.tar.gz + .sha256 资产齐全，可以拉。"
            f"开了 peer.auto_update 的机器会自己去查新版；否则手工：python deploy/release/update.py")
    sent, failed = [], []
    for n in nodes:
        cmd = [_python(), str(ROOT / "peer_mesh.py"), "say", n, text, "--kind", "release"]
        try:
            p = _run(cmd, timeout=30)
        except subprocess.TimeoutExpired:
            failed.append(n)
            warn(f"{n}：留言超时 30s（对方可能离线，不影响发布）")
            continue
        if p.returncode == 0:
            sent.append(n)
            ok(f"{n}：已留言")
        else:
            failed.append(n)
            last = ((p.stdout or "") + (p.stderr or "")).strip().splitlines()
            warn(f"{n}：留言失败 —— {last[-1] if last else '无输出'}")
    return {"notified": sent, "failed": failed}


def do_log(entry: dict) -> None:
    try:
        RELEASE_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(RELEASE_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as exc:
        warn(f"发布记录没写进去（{exc}）—— 不影响发布本身")


def main() -> int:
    ap = argparse.ArgumentParser(
        description="一键发布大白新版本（本地串步骤，发布实现仍只有 CI 一个）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="例：python deploy/release/publish.py -m \"修了 X 的 Y 问题\"\n"
               "    python deploy/release/publish.py --check",
    )
    ap.add_argument("--bump", choices=["major", "minor", "patch"], default="patch",
                    help="升哪一位版本号（默认 patch）")
    ap.add_argument("-m", "--message", default="", help="这次改了什么（进 commit message）")
    ap.add_argument("--check", action="store_true",
                    help="只做前置闸门就退出（不改工作区、不提交、不推送）")
    ap.add_argument("--dry-run", action="store_true",
                    help="走到打包+测试为止，不提交不推送，VERSION 自动恢复")
    ap.add_argument("--tag-only", action="store_true",
                    help="VERSION 已升好（上次发布没走完），只补推 tag，不升号不打包")
    ap.add_argument("--commit-all", action="store_true",
                    help="把当前未提交改动一起提交（默认拒绝脏工作区）")
    ap.add_argument("--no-tests", action="store_true", help="跳过测试（不推荐）")
    ap.add_argument("--no-watch", action="store_true", help="不盯 CI 落地，推完就返回")
    ap.add_argument("--no-notify", action="store_true", help="不通知联邦其它实例")
    ap.add_argument("--tests-timeout", type=int, default=1800, help="测试超时秒数（默认 1800）")
    ap.add_argument("--watch-timeout", type=int, default=1800, help="盯落地超时秒数（默认 1800）")
    ap.add_argument("--branch", default=BRANCH_DEFAULT, help=f"发布分支（默认 {BRANCH_DEFAULT}）")
    args = ap.parse_args()

    t0 = time.time()
    try:
        _p(f"=== 发布大白（仓库 {ROOT}）===")
        token = read_token()
        if not token:
            die("没找到 GITHUB_TOKEN（按序找：环境变量 → /etc/dabai/secrets.env → "
                "~/.config/dabai/secrets.env）\n"
                "    临时一次：export GITHUB_TOKEN=ghp_xxx")
        ok(f"token 可用（{token[:4]}***，走环境变量注入，不进命令行）")

        info = preflight(args, token)

        if args.check:
            _p()
            _p("✓ 前置闸门全过，可以发布。正式发布：去掉 --check 再加 -m \"改了啥\"")
            return 0

        if args.tag_only:
            ver = info["version"]
        else:
            ver = do_build(args.bump)

        if args.no_tests:
            _p("③ 测试：--no-tests 已跳过 —— 这版没有测试背书")
        else:
            do_tests(args.tests_timeout)

        if args.dry_run:
            if not args.tag_only:
                git("checkout", "--", "VERSION")
            _p()
            _p("✓ dry-run 结束：打包与测试都过了，未提交、未推送"
               + ("（VERSION 已恢复原值）" if not args.tag_only else ""))
            return 0

        if not args.message.strip():
            warn("没给 -m，commit message 只有版本号 —— 建议写清这次改了什么")
        do_commit(ver, args.message.strip(), args.commit_all, info["dirty"])
        do_push(info["branch"], ver, token)

        do_log({
            "version": ver,
            "at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "commit": git("rev-parse", "HEAD").stdout.strip(),
            "pushed_commits": info["ahead"],
            "dirty_files": len(info["dirty"]),
            "tested": not args.no_tests,
            "seconds": round(time.time() - t0, 1),
        })

        rc = 0
        if args.no_watch:
            _p("⑥ 已跳过盯落地（--no-watch）")
        else:
            rc = do_watch(ver, args.watch_timeout)

        note = {"notified": [], "failed": []}
        if rc != 0:
            _p("⑦ 不通知联邦：release 还没落地")
            _p("   （等 CI 跑完/点了 Approve 之后，重跑 watch_release.py 确认，再手工通知）")
        elif args.no_notify:
            _p("⑦ 已跳过联邦通知（--no-notify）")
        else:
            note = do_notify(ver)

        do_log({
            "phase": "outcome",
            "version": ver,
            "at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "watch_rc": rc,
            "notified": note["notified"],
            "notify_failed": note["failed"],
            "seconds": round(time.time() - t0, 1),
        })

        _p()
        if rc == 0:
            _p(f"✓ v{ver} 已发布落地（{time.time() - t0:.0f}s）")
        else:
            _p(f"⚠ v{ver} 的 tag 已推，但盯落地没等到成功（退出码 {rc}）")
            _p("  可能还没人点 Approve，或 CI 挂了 —— 单独重盯：")
            _p(f"  python deploy/release/watch_release.py v{ver}")
        _p(f"  Actions: https://github.com/{REPO_DEFAULT}/actions")
        _p(f"  Release: https://github.com/{REPO_DEFAULT}/releases/tag/v{ver}")
        _p("  其它机器更新：python deploy/release/update.py")
        return rc

    except Fail as exc:
        _p()
        _p(f"✘ 中止：{exc}")
        return 1
    except KeyboardInterrupt:
        _p()
        _p("✘ 被中断（已完成的步骤不回滚；重跑会告诉你从哪继续）")
        return 130


if __name__ == "__main__":
    sys.exit(main())
