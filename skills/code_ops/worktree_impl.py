"""代码任务隔离（worktree）—— 复杂/重构/大改先在独立 git 工作树里做。

设计要点：
- 工作树统一放在 <仓库父目录>/dabai_worktrees/ 下，分支名 codex/<任务名>；
- 隔离区改动不触发大白核心热重载（不碰主工作区），合并回主分支后由
  hot_reload 正常接管；
- 合并前必须确认工作树无未提交改动；主工作区有未提交改动时合并可能冲突，
  返回错误并保留现场，绝不强推；
- wt_discard 是破坏性操作，必须 confirm=true。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parents[2]     # 大白项目根目录 D:\AI\dabai
_CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

BRANCH_PREFIX = "codex/"
DEFAULT_WT_ROOT = _HERE.parent / "dabai_worktrees"     # D:\AI\dabai_worktrees


def _wt_root() -> Path:
    """隔离区根目录：settings.json -> agent.worktree_dir（默认 <仓库父目录>/dabai_worktrees）。

    每次调用实时读配置，改 settings.json 无需重启即生效；相对路径按 <仓库父目录> 解析。
    """
    try:
        cfg = json.loads((_HERE / "settings.json").read_text(encoding="utf-8"))
        v = str(cfg.get("agent", {}).get("worktree_dir") or "").strip()
        if v:
            p = Path(v).expanduser()
            return p if p.is_absolute() else (_HERE.parent / p)
    except Exception:
        pass
    return DEFAULT_WT_ROOT


def _err(msg: str) -> str:
    return "✘ " + msg


def _slug(name: str) -> str:
    """任务名 → 安全目录名（小写字母数字连字符）。"""
    s = str(name or "").strip().lower()
    s = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "-", s).strip("-")
    return re.sub(r"-+", "-", s)[:48] or "task"


def _git(root: str, args: list, timeout: int = 60):
    cmd = ["git", "-c", "core.quotepath=false", "-c", "color.ui=false"]
    cmd += list(args)
    try:
        r = subprocess.run(cmd, capture_output=True, encoding="utf-8",
                           errors="replace", timeout=timeout, cwd=root,
                           creationflags=_CREATE_NO_WINDOW)
        return r, ""
    except FileNotFoundError:
        return None, "未找到 git 命令，请先安装 Git。"
    except subprocess.TimeoutExpired:
        return None, f"git 命令超时（>{timeout}s）"


def _require_repo(root: str):
    r, err = _git(root, ["rev-parse", "--is-inside-work-tree"])
    if r is None or r.returncode != 0:
        return f"该目录不是 git 仓库（{root}）。{_git_err(r, err)}"
    return None


def _git_err(r, err: str) -> str:
    if err:
        return err
    return (r.stderr or r.stdout or "").strip() or "未知错误"


def _main_repo_of(wt_path: str) -> str | None:
    """由工作树路径反查主仓库根目录。"""
    r, _ = _git(wt_path, ["rev-parse", "--git-common-dir"])
    if r is None or r.returncode != 0:
        return None
    common = Path(r.stdout.strip()).resolve()
    if common.name == ".git" and common.parent.is_dir():
        return str(common.parent)
    if (common / ".git").exists():   # 主仓库 .git 是目录
        return str(common)
    return str(common)


def _branch_of(wt_path: str) -> str | None:
    r, _ = _git(wt_path, ["branch", "--show-current"])
    if r is None or r.returncode != 0:
        return None
    b = r.stdout.strip()
    return b or None


def _dirty_count(wt_path: str) -> int:
    r, _ = _git(wt_path, ["status", "--porcelain"])
    if r is None or r.returncode != 0:
        return -1
    return len([l for l in r.stdout.splitlines() if l.strip()])


def _resolve_wt(root: str, path: str) -> tuple:
    """校验 path 是本仓库登记的工作树，返回 (主仓库根, 工作树绝对路径) 或错误。"""
    root = str(root or "").strip() or os.getcwd()
    main_bad = _require_repo(root)
    if main_bad:
        return None, None, main_bad
    p = Path(path).resolve()
    r, err = _git(root, ["worktree", "list", "--porcelain"])
    if r is None or r.returncode != 0:
        return None, None, f"无法读取工作树清单：{_git_err(r, err)}"
    for line in r.stdout.splitlines():
        if line.startswith("worktree ") and Path(line[9:]).resolve() == p:
            return root, str(p), None
    return None, None, f"{path} 不是本仓库登记的工作树（先 wt_create 或看 wt_list）"


def wt_create(args: dict) -> str:
    root = str(args.get("root") or "").strip() or os.getcwd()
    bad = _require_repo(root)
    if bad:
        return _err(bad)
    name = str(args.get("name") or "").strip()
    if not name:
        return _err("name（任务名）不能为空")
    slug = _slug(name)
    target = _wt_root() / slug
    if target.exists():
        return _err(f"工作树已存在：{target}（换一个任务名，或先 wt_list/wt_merge/wt_discard 处理）")
    branch = BRANCH_PREFIX + slug
    base = str(args.get("base") or "").strip()
    add_args = ["worktree", "add", "-b", branch]
    if base:
        add_args.append(base)
    add_args += [str(target)]
    r, err = _git(root, add_args)
    if r is None or r.returncode != 0:
        return _err(f"创建工作树失败：{_git_err(r, err)}")
    dirty = _dirty_count(root)
    lines = [
        "✔ 已创建隔离工作树：",
        f"  路径：{target}",
        f"  分支：{branch}",
        f"  隔离区：{_wt_root()}（settings.json -> agent.worktree_dir 可改）",
        "  用法：code_ops 各工具传 root=%s 在隔离区改/验；"
        "审查用 wt_status/wt_diff；确认后 wt_merge 合并回主分支。" % target,
    ]
    if dirty > 0:
        lines.append(f"  ⚠ 主工作区有 {dirty} 处未提交改动：合并回主分支时若涉及相同文件可能冲突，"
                     "建议先让用户确认/提交主工作区改动再合并。")
    return "\n".join(lines)


def wt_list(args: dict) -> str:
    root = str(args.get("root") or "").strip() or os.getcwd()
    bad = _require_repo(root)
    if bad:
        return _err(bad)
    r, err = _git(root, ["worktree", "list"])
    if r is None or r.returncode != 0:
        return _err(f"git worktree list 失败：{_git_err(r, err)}")
    lines = ["工作树清单："]
    for raw in r.stdout.splitlines():
        parts = raw.split()
        if not parts:
            continue
        p = parts[0]
        branch = parts[1] if len(parts) > 1 else "?"
        d = _dirty_count(p)
        mark = "（有改动）" if d > 0 else "（干净）"
        lines.append(f"- {p}｜{branch}{mark}")
    return "\n".join(lines)


def wt_status(args: dict) -> str:
    root = str(args.get("root") or "").strip() or os.getcwd()
    path = str(args.get("path") or "").strip()
    main, wt, err = _resolve_wt(root, path)
    if err:
        return _err(err)
    branch = _branch_of(wt) or "?"
    r1, e1 = _git(wt, ["status", "--porcelain=v1", "--branch"])
    r2, e2 = _git(wt, ["diff", "--stat"])
    if r1 is None or r1.returncode != 0:
        return _err(f"git status 失败：{_git_err(r1, e1)}")
    out = [f"工作树状态（{wt}）分支 {branch}："]
    body = r1.stdout.strip()
    if body:
        out.append(body)
    else:
        out.append("（无未提交改动）")
    if r2 and r2.returncode == 0 and r2.stdout.strip():
        out.append("\n改动统计：\n" + r2.stdout.strip())
    out.append("\n下一步：wt_diff 看详细 diff；确认无误 wt_merge 合并；改坏 wt_discard 丢弃。")
    return "\n".join(out)


def wt_diff(args: dict) -> str:
    root = str(args.get("root") or "").strip() or os.getcwd()
    path = str(args.get("path") or "").strip()
    main, wt, err = _resolve_wt(root, path)
    if err:
        return _err(err)
    try:
        max_lines = max(20, min(int(args.get("max_lines") or 400), 5000))
    except ValueError:
        return _err("max_lines 需为数字")
    main_branch = _branch_of(main) or "main"
    parts = []
    r, e = _git(wt, ["diff", "--stat"])
    if r and r.returncode == 0 and r.stdout.strip():
        parts.append("未提交改动统计：\n" + r.stdout.strip())
    r, e = _git(wt, ["diff", main_branch, "--stat"])
    if r and r.returncode == 0 and r.stdout.strip():
        parts.append("相对主分支 %s 的统计：\n%s" % (main_branch, r.stdout.strip()))
    r, e = _git(wt, ["log", "--oneline", main_branch + "..HEAD", "-n", "20"])
    if r and r.returncode == 0 and r.stdout.strip():
        parts.append("隔离分支领先的提交：\n" + r.stdout.strip())
    r, e = _git(wt, ["diff", main_branch])
    if r is None or r.returncode != 0:
        return _err(f"git diff 失败：{_git_err(r, e)}")
    body = r.stdout.strip()
    if body:
        lines = body.splitlines()
        if len(lines) > max_lines:
            body = "\n".join(lines[:max_lines]) + f"\n…（diff 共 {len(lines)} 行，已截断）"
        parts.append("完整 diff：\n" + body)
    if not parts:
        return "工作树与主分支一致，没有差异。"
    return "\n\n".join(parts)


def wt_run(args: dict) -> str:
    root = str(args.get("root") or "").strip() or os.getcwd()
    path = str(args.get("path") or "").strip()
    command = str(args.get("command") or "").strip()
    _m, wt, err = _resolve_wt(root, path)
    if err:
        return _err(err)
    if not command:
        return _err("command 不能为空")
    try:
        timeout = max(5, min(int(args.get("timeout") or 120), 600))
    except ValueError:
        return _err("timeout 需为数字")
    try:
        r = subprocess.run(command, shell=True, capture_output=True, text=True,
                           timeout=timeout, cwd=wt, errors="replace",
                           creationflags=_CREATE_NO_WINDOW)
    except subprocess.TimeoutExpired:
        return f"命令超时（>{timeout}s）：{command}"
    out = ((r.stdout or "") + "\n" + (r.stderr or "")).strip()
    tail = "\n".join(x for x in out.splitlines() if x.strip())[-2500:]
    mark = "✔" if r.returncode == 0 else "✘"
    return f"{mark} exit={r.returncode}：{command}（cwd={wt}）\n输出末尾：\n{tail}"


def wt_merge(args: dict) -> str:
    root = str(args.get("root") or "").strip() or os.getcwd()
    path = str(args.get("path") or "").strip()
    main, wt, err = _resolve_wt(root, path)
    if err:
        return _err(err)
    branch = _branch_of(wt)
    if not branch or not branch.startswith(BRANCH_PREFIX):
        return _err(f"工作树 {wt} 不在 {BRANCH_PREFIX}* 分支上（当前 {branch or '?'}），拒绝合并")
    dirty = _dirty_count(wt)
    if dirty > 0:
        return _err(f"工作树有 {dirty} 处未提交改动，请先在隔离区提交（或用 wt_run 提交）后再合并")
    main_dirty = _dirty_count(main)
    warn = ""
    if main_dirty > 0:
        warn = (f"\n⚠ 主工作区有 {main_dirty} 处未提交改动：合并涉及相同文件时会冲突，"
                "冲突时 git 会拒绝并保留现场，不会覆盖你的改动。")
    r, e = _git(main, ["merge", "--no-ff", branch, "-m",
                       "merge worktree %s" % os.path.basename(wt)])
    if r is None or r.returncode != 0:
        msg = _git_err(r, e)
        if "CONFLICT" in msg or "conflict" in msg:
            return (_err(f"合并冲突：{msg[:500]}")
                    + "\n处理建议：先看冲突文件，解决后在主仓库 git add + git commit 完成合并；"
                      "工作树保留未动。")
        return _err(f"合并失败：{msg}")
    merged = r.stdout.strip()[-800:]
    result = ["✔ 已合并分支 %s 回 %s：%s%s"
              % (branch, os.path.basename(main), merged, warn)]
    if not args.get("keep"):
        rr, ee = _git(main, ["worktree", "remove", wt])
        if rr is not None and rr.returncode == 0:
            result.append(f"✔ 已移除工作树：{wt}")
        else:
            result.append(f"⚠ 工作树移除失败（{_git_err(rr, ee)}），可稍后手动 wt_list 检查")
    if not args.get("keep_branch"):
        rb, _ = _git(main, ["branch", "-d", branch])
        if rb is not None and rb.returncode == 0:
            result.append(f"✔ 已删除已合并分支：{branch}")
    result.append("提示：涉及大白核心（agent.py/server.py 等）的改动合并后，hot_reload 会自动重启生效。")
    return "\n".join(result)


def wt_discard(args: dict) -> str:
    root = str(args.get("root") or "").strip() or os.getcwd()
    path = str(args.get("path") or "").strip()
    if not args.get("confirm"):
        return _err("丢弃工作树是破坏性操作（隔离区所有改动丢失），必须 confirm=true")
    main, wt, err = _resolve_wt(root, path)
    if err:
        return _err(err)
    branch = _branch_of(wt)
    r, e = _git(main, ["worktree", "remove", "--force", wt])
    if r is None or r.returncode != 0:
        return _err(f"移除工作树失败：{_git_err(r, e)}")
    lines = [f"✔ 已丢弃工作树：{wt}"]
    if branch and branch.startswith(BRANCH_PREFIX):
        rb, eb = _git(main, ["branch", "-D", branch])
        if rb is not None and rb.returncode == 0:
            lines.append(f"✔ 已删除分支：{branch}")
        else:
            lines.append(f"⚠ 分支删除失败（{_git_err(rb, eb)}）")
    lines.append("主工作区保持原样，回滚完成。")
    return "\n".join(lines)


HANDLERS = {
    "wt_create": wt_create,
    "wt_list": wt_list,
    "wt_status": wt_status,
    "wt_diff": wt_diff,
    "wt_run": wt_run,
    "wt_merge": wt_merge,
    "wt_discard": wt_discard,
}
