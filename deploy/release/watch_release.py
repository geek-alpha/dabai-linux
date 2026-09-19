#!/usr/bin/env python3
"""盯 GitHub Actions 发行版工作流，直到 release 落地或失败。

用法：
  python deploy/release/watch_release.py v1.0.6
       [--repo wangxingfen/dabai-linux] [--root /home/wxf/dabai]
       [--timeout 1800] [--interval 30]

退出码：
  0  release 已落地（tar.gz + sha256 资产齐全）
  1  workflow 失败 / 被取消
  2  超时未落地 / 参数或凭证问题

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
        with urllib.request.urlopen(req, timeout=timeout) as r:
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


def pending_approvals(repo: str, run_id: int, token: str) -> list:
    url = f"https://api.github.com/repos/{repo}/actions/runs/{run_id}/pending_deployments"
    st, d = api_get(url, token)
    if st != 200:
        return []
    return d if isinstance(d, list) else []


def release_assets(repo: str, tag: str, token: str):
    """release 资产名列表；release 不存在返回 None。"""
    st, d = api_get(f"https://api.github.com/repos/{repo}/releases/tags/{tag}", token)
    if st != 200:
        return None
    return [a.get("name") for a in d.get("assets", [])]


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
    ap.add_argument("--repo", default="wangxingfen/dabai-linux")
    ap.add_argument("--root", default="/home/wxf/dabai")
    ap.add_argument("--timeout", type=int, default=1800, help="总超时秒数，默认 1800")
    ap.add_argument("--interval", type=int, default=30, help="轮询间隔秒数，默认 30")
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

    run, err = find_run(args.repo, sha, token)
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
                assets = release_assets(args.repo, args.tag, token)
                if assets is None:
                    print(f"✘ workflow 成功但 release {args.tag} 还没建出来（可能刚完成，稍后再看）")
                    return 1
                missing = [n for n in (f"dabai-{args.tag[1:]}.tar.gz",
                                       f"dabai-{args.tag[1:]}.tar.gz.sha256")
                           if n not in assets]
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
