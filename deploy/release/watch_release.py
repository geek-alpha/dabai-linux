#!/usr/bin/env python3
"""盯 GitHub Actions 发行版工作流，直到 release 落地或失败。

用法：
  python deploy/release/watch_release.py v1.0.6
       [--repo geek-alpha/dabai-linux] [--root /home/wxf/dabai]
       [--timeout 1800] [--interval 30]

退出码：
  0  release 已落地（tar.gz + sha256 资产齐全）
  1  workflow 失败 / 被取消 / release 建出来了但资产真缺
  2  超时未落地（含资产同步超出窗口）/ 参数或凭证问题

设计原则：只读观察者。不产生任何发布能力，不违反
「发布只能有一个实现」（deploy/release/README.md）——
真正建 release 的仍是 GitHub Actions 的 publish 步骤。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# 同目录工具：代理探测（直连 GitHub 常见 60KB/s，走本机代理是 MB/s 量级）
sys.path.insert(0, str(Path(__file__).resolve().parent))
import netproxy  # noqa: E402

SECRET_FILES = (
    Path("/etc/dabai/secrets.env"),
    Path.home() / ".config" / "dabai" / "secrets.env",
)


def get_token() -> str:
    tok = os.environ.get("GITHUB_TOKEN", "").strip()
    if tok:
        return tok
    for p in SECRET_FILES:
        if not p.is_file():
            continue
        try:
            for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
                if line.startswith("GITHUB_TOKEN="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
        except OSError:
            continue
    return ""


def api_get(url: str, token: str, timeout: int = 30):
    req = urllib.request.Request(url, headers={
        "Authorization": "Bearer " + token,
        "Accept": "application/vnd.github+json",
        "User-Agent": "dabai-watch-release",
    })
    try:
        with netproxy.opener().open(req, timeout=timeout) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, {}
    except Exception as e:
        return 0, {"message": str(e)}


def tag_commit(root: str, tag: str) -> str:
    """本地仓库里该 tag 指向的 commit sha。找不到返回空串。"""
    r = subprocess.run(
        ["git", "-C", root, "rev-parse", f"{tag}^{{commit}}"],
        capture_output=True, timeout=10)
    if r.returncode != 0:
        return ""
    return r.stdout.decode().strip()


def find_run(repo: str, sha: str, token: str):
    """在 event=push 的 runs 里找 head_sha 匹配的工作流。返回 run dict 或 None。"""
    url = f"https://api.github.com/repos/{repo}/actions/runs?event=push&per_page=50"
    st, d = api_get(url, token)
    if st != 200:
        return None, f"查 workflow 失败 HTTP={st} {d.get('message')}"
    for run in d.get("workflow_runs", []):
        if run.get("head_sha", "") == sha:
            return run, None
    return None, "push 事件里没找到该 commit 对应的 workflow run（可能 workflow 没触发）"


def wait_run(repo: str, sha: str, token: str, window: int = 90, interval: int = 5):
    """窗口内轮询 find_run。

    为什么不能只查一次：push 完立刻查 actions/runs，GitHub 往往还没把这条 run
    登记进去，单次查询会把「还没登记」误报成「workflow 没触发」——v1.0.10 首发
    就这么假失败了一次（重跑同一命令即成功）。
    """
    deadline = time.time() + window
    err = ""
    while True:
        run, err = find_run(repo, sha, token)
        if run:
            return run, None
        if time.time() >= deadline:
            return None, err
        time.sleep(interval)


def pending_approvals(repo: str, run_id: int, token: str) -> list:
    url = f"https://api.github.com/repos/{repo}/actions/runs/{run_id}/pending_deployments"
    st, d = api_get(url, token)
    if st != 200:
        return []
    return d if isinstance(d, list) else []


def release_by_tag(repo: str, tag: str, token: str):
    """release 详情；不存在返回 None。"""
    st, d = api_get(f"https://api.github.com/repos/{repo}/releases/tags/{tag}", token)
    return d if st == 200 else None


def release_assets(repo: str, tag: str, token: str, release_id=None):
    """release 资产名列表；release 不存在返回 None。

    给了 release_id 就走 by-id 端点：by-tags 有缓存延迟，刚建出来的 release
    在它那儿可能还是 404。
    """
    url = (f"https://api.github.com/repos/{repo}/releases/{release_id}"
           if release_id else f"https://api.github.com/repos/{repo}/releases/tags/{tag}")
    st, d = api_get(url, token)
    if st != 200:
        return None
    return [a.get("name") for a in d.get("assets", [])]


def wait_assets(repo: str, tag: str, token: str, names, deadline: float,
                interval: int = 10, sleep=time.sleep, log=print):
    """等 release 资产齐，返回 (release_id, 资产名列表)。

    为什么不能查一次就判死：run 转 completed 与「release 资产查得到」之间有一段
    同步延迟，by-tags 端点尤其明显 —— v1.1.19 首发实测同一时刻 by-id 已见 2 个资产、
    by-tags 还是空，脚本当场报「缺资产」退出 1，几秒后资产其实齐了。
    所以拿到 release id 就改走 by-id 绕开缓存；资产没齐继续等，等到 deadline 为止。
    返回的 release_id 为 None 表示窗口内 release 压根没建出来。
    """
    rel_id = None
    assets: list = []
    while True:
        if rel_id is None:
            rel = release_by_tag(repo, tag, token)
            if rel:
                rel_id = rel.get("id")
                assets = [a.get("name") for a in rel.get("assets", [])]
        else:
            got = release_assets(repo, tag, token, release_id=rel_id)
            if got is not None:
                assets = got
        if all(n in assets for n in names) or time.time() >= deadline:
            return rel_id, assets
        log(f"  … release {tag} 资产未齐（现有 {len(assets)} 个），{interval}s 后重查")
        sleep(interval)


def failed_steps(repo: str, run_id: int, token: str) -> str:
    url = f"https://api.github.com/repos/{repo}/actions/runs/{run_id}/jobs"
    st, d = api_get(url, token)
    if st != 200:
        return "（拿不到失败明细）"
    for job in d.get("jobs", []):
        if job.get("conclusion") not in ("success", None):
            for s in job.get("steps", []):
                if s.get("conclusion") == "failure":
                    return f"{job.get('name')} / {s.get('name')}"
    return "（失败步骤未识别）"


def main() -> int:
    ap = argparse.ArgumentParser(description="盯发行版 workflow 直到 release 落地")
    ap.add_argument("tag", help="要盯的 tag，如 v1.0.6")
    ap.add_argument("--repo", default="geek-alpha/dabai-linux")
    ap.add_argument("--root", default="/home/wxf/dabai")
    ap.add_argument("--timeout", type=int, default=1800, help="总超时秒数，默认 1800")
    ap.add_argument("--interval", type=int, default=30, help="轮询间隔秒数，默认 30")
    ap.add_argument("--assets-window", type=int, default=180,
                    help="run 成功后等 release 资产同步的最长秒数，默认 180")
    args = ap.parse_args()

    token = get_token()
    if not token:
        print("✘ 没找到 GITHUB_TOKEN（环境变量 → /etc/dabai/secrets.env → ~/.config/dabai/secrets.env）")
        return 2

    sha = tag_commit(args.root, args.tag)
    if not sha:
        print(f"✘ 本地仓库找不到 tag {args.tag}（git rev-parse {args.tag}^{{commit}} 失败）")
        return 2
    print(f"盯 {args.tag}（commit {sha[:8]}）@ {args.repo} …")

    run, err = wait_run(args.repo, sha, token)
    if not run:
        print(f"✘ {err}")
        return 2
    run_id = run["id"]
    print(f"  workflow run #{run_id}：{run.get('display_title', '')[:50]}  status={run.get('status')}")

    deadline = time.time() + args.timeout
    while time.time() < deadline:
        st, d = api_get(f"https://api.github.com/repos/{args.repo}/actions/runs/{run_id}", token)
        if st != 200:
            print(f"  ! 查 run 状态失败 HTTP={st}，{args.interval}s 后重试 …")
            time.sleep(args.interval)
            continue

        status = d.get("status")
        conclusion = d.get("conclusion")
        if status == "completed":
            if conclusion == "success":
                names = (f"dabai-{args.tag[1:]}.tar.gz",
                         f"dabai-{args.tag[1:]}.tar.gz.sha256")
                rel_id, assets = wait_assets(
                    args.repo, args.tag, token, names,
                    deadline=min(time.time() + args.assets_window, deadline),
                    interval=min(args.interval, 10), log=print)
                if rel_id is None:
                    print(f"✘ workflow 成功，但 {args.assets_window}s 内 release {args.tag} 仍未建出来")
                    return 2
                missing = [n for n in names if n not in assets]
                if missing:
                    print(f"✘ release 缺资产：{missing}")
                    print(f"   现有：{assets}")
                    return 1
                print(f"✔ release {args.tag} 已落地，资产齐全：")
                for a in sorted(assets):
                    print(f"   · {a}")
                return 0
            print(f"✘ workflow 失败（{conclusion}）：{failed_steps(args.repo, run_id, token)}")
            return 1

        pending = pending_approvals(args.repo, run_id, token)
        if pending:
            print(f"  ⏸ 等待管理员审批（environment: release）——去 Actions 页面点 Approve …")
        else:
            print(f"  … {status}，{args.interval}s 后重查（run #{run_id}）")
        time.sleep(args.interval)

    print(f"✘ 超时（{args.timeout}s）仍未落地。去 https://github.com/{args.repo}/actions 看现场")
    return 2


if __name__ == "__main__":
    sys.exit(main())
