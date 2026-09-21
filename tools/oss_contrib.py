#!/usr/bin/env python3
"""给别人的仓库做贡献：选目标 → fork → 开 PR，一条链路的可复跑工具。

为什么需要它：给上游修 bug 的瓶颈从来不是写代码，而是选错目标——仓库半年没
人合并 PR、issue 描述模糊到无法复现、维护者要的是重构而你能给的是小修。
先把这些用 API 事实查清楚，再动手。

判据（每个 PR 必须能答）：
  1. 上游最近有合并记录吗（repo 子命令看 pushed/最近合并 PR）
  2. 问题能本地复现吗（before/after 工具输出，缺了不提）
  3. 改动面是不是最小可审查的一块

  oss_contrib.py repo   <owner/repo>              仓库健康度（值不值得投入）
  oss_contrib.py issues <owner/repo> [--label L]  开放 issue 概览
  oss_contrib.py fork   <owner/repo> [--dir D]    fork + clone + 配 upstream
  oss_contrib.py pr     <owner/repo> --title T --body-file F
  oss_contrib.py status <owner/repo> <number>     PR 状态 + CI

凭据：环境变量 GITHUB_TOKEN（repo scope）。git 身份与凭据另配（见 README 的
git config --global credential.helper store）。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

API = "https://api.github.com"
DEFAULT_WORK_DIR = Path("/home/wxf/oss-work")


def _token() -> str:
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        sys.exit("缺 GITHUB_TOKEN：export GITHUB_TOKEN=<token>")
    return token


def _api(path: str, method: str = "GET", payload: dict | None = None):
    """一次 GitHub API 调用；失败时把响应体打出来，别让人猜是哪一步挂了。"""
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        API + path,
        data=data,
        method=method,
        headers={
            "Authorization": "Bearer " + _token(),
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        sys.exit(f"HTTP {exc.code} {method} {path}\n{exc.read().decode()[:600]}")


def cmd_repo(args) -> int:
    repo = _api(f"/repos/{args.repo}")
    if repo.get("message"):
        sys.exit(f"{args.repo}: {repo['message']}")
    commits = _api(f"/repos/{args.repo}/commits?per_page=1")
    merged = [p for p in _api(f"/repos/{args.repo}/pulls?state=closed&per_page=20&sort=updated")
              if p.get("merged_at")]

    print(f"{repo['full_name']}  ★{repo['stargazers_count']}  open issues {repo['open_issues_count']}"
          f"  lang {repo['language']}")
    print(f"  last push     : {repo['pushed_at']}")
    if commits and isinstance(commits, list):
        print(f"  last commit   : {commits[0]['commit']['author']['date'][:10]}"
              f"  {commits[0]['commit']['message'].splitlines()[0][:60]}")
    if merged:
        print(f"  last merged PR: {merged[0]['merged_at'][:10]}  #{merged[0]['number']}"
              f"  {merged[0]['title'][:55]}")
    else:
        print("  last merged PR: 最近 20 条已关闭 PR 里没有合并记录 —— 上游可能已停摆")

    days = _days_since(repo["pushed_at"])
    if days is not None:
        verdict = "活跃" if days <= 90 else ("半停摆" if days <= 365 else "停摆")
        print(f"  判断          : 距上次 push {days} 天 → {verdict}")
    return 0


def _days_since(iso: str) -> int | None:
    from datetime import datetime, timezone
    try:
        then = datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None
    return (datetime.now(timezone.utc) - then).days


def cmd_issues(args) -> int:
    query = f"/repos/{args.repo}/issues?state=open&per_page={args.limit}&sort=updated"
    if args.label:
        query += "&labels=" + urllib.parse.quote(args.label)
    items = _api(query)
    if not isinstance(items, list):
        sys.exit(f"{args.repo}: {items.get('message', items)}")

    shown = 0
    for it in items:
        if "pull_request" in it:
            continue
        labels = ",".join(l["name"] for l in it["labels"]) or "-"
        print(f"#{it['number']:<5} c={it['comments']:<2} {it['updated_at'][:10]} [{labels}]"
              f" {it['title'][:70]}")
        shown += 1
    print(f"（{shown} 条开放 issue）")
    return 0


def _git(args: list[str], cwd, check: bool = True):
    proc = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    if check and proc.returncode != 0:
        sys.exit(f"git {' '.join(args)} 失败:\n{proc.stderr.strip()}")
    return proc


def _wait_for_fork(full_name: str, tries: int = 15):
    """GitHub 建 fork 是异步的，clone 前得等它真的存在。"""
    for _ in range(tries):
        info = _api(f"/repos/{full_name}")
        if not info.get("message"):
            return info
        time.sleep(2)
    return None


def cmd_fork(args) -> int:
    work_dir = Path(args.dir)
    name = args.repo.split("/")[1]
    target = work_dir / name
    me = _api("/user")["login"]

    _api(f"/repos/{args.repo}/forks", method="POST")
    if not _wait_for_fork(f"{me}/{name}"):
        sys.exit(f"fork {me}/{name} 迟迟没就绪，稍后重试")

    if target.exists():
        print(f"已存在，跳过 clone: {target}")
    else:
        work_dir.mkdir(parents=True, exist_ok=True)
        _git(["clone", f"https://github.com/{me}/{name}.git", str(target)], cwd=work_dir)

    _git(["remote", "add", "upstream", f"https://github.com/{args.repo}.git"], cwd=target, check=False)
    _git(["fetch", "upstream", "--quiet"], cwd=target, check=False)

    print(f"就绪: {target}")
    print(f"  origin   -> https://github.com/{me}/{name}.git（你的 fork，push 到这）")
    print(f"  upstream -> https://github.com/{args.repo}.git（上游，只读）")
    return 0


def cmd_pr(args) -> int:
    me = _api("/user")["login"]
    cwd = Path(args.dir)
    branch = args.branch or _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=cwd).stdout.strip()
    if branch in ("master", "main", "HEAD"):
        sys.exit(f"当前分支是 {branch}：先 git checkout -b <分支名> 再提 PR，别从主干提")

    body = Path(args.body_file).read_text() if args.body_file else ""
    pr = _api(f"/repos/{args.repo}/pulls", method="POST", payload={
        "title": args.title,
        "head": f"{me}:{branch}",
        "base": args.base,
        "body": body,
    })
    print(f"PR #{pr['number']}: {pr['html_url']}")
    return 0


def cmd_status(args) -> int:
    pr = _api(f"/repos/{args.repo}/pulls/{args.number}")
    print(f"#{pr['number']} {pr['title']}")
    print(f"  state    : {pr['state']}  merged={bool(pr.get('merged_at'))}"
          f"  mergeable={pr.get('mergeable')}")
    print(f"  changed  : {pr['changed_files']} files  +{pr['additions']}/-{pr['deletions']}")

    runs = _api(f"/repos/{args.repo}/commits/{pr['head']['sha']}/check-runs").get("check_runs", [])
    if not runs:
        print("  CI       : 还没有 check run —— fork PR 常要维护者点一下批准才跑")
    for run in runs:
        print(f"  CI       : {run['name']} {run['status']} {run.get('conclusion')}")

    for comment in _api(f"/repos/{args.repo}/issues/{args.number}/comments"):
        print(f"  评论 @{comment['user']['login']} {comment['created_at'][:10]}:"
              f" {comment['body'][:300]}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="给别人的仓库做贡献：选目标 → fork → 开 PR")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("repo", help="仓库健康度：值不值得投入")
    p.add_argument("repo")
    p.set_defaults(func=cmd_repo)

    p = sub.add_parser("issues", help="开放 issue 概览")
    p.add_argument("repo")
    p.add_argument("--label", default=None)
    p.add_argument("--limit", type=int, default=30)
    p.set_defaults(func=cmd_issues)

    p = sub.add_parser("fork", help="fork + clone + 配 upstream")
    p.add_argument("repo")
    p.add_argument("--dir", default=str(DEFAULT_WORK_DIR))
    p.set_defaults(func=cmd_fork)

    p = sub.add_parser("pr", help="从当前分支开 PR")
    p.add_argument("repo")
    p.add_argument("--title", required=True)
    p.add_argument("--body-file", default=None)
    p.add_argument("--base", default="master")
    p.add_argument("--branch", default=None)
    p.add_argument("--dir", default=".")
    p.set_defaults(func=cmd_pr)

    p = sub.add_parser("status", help="PR 状态 + CI + 评论")
    p.add_argument("repo")
    p.add_argument("number", type=int)
    p.set_defaults(func=cmd_status)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
