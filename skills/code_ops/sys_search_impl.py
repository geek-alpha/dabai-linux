# -*- coding: utf-8 -*-
"""通用系统文件搜索（合并自原 sys_search 技能）—— 大白的基础能力：全盘找文件，不再只靠猜路径。

- sys_find：按 名称/扩展名/目录/大小/修改时间/文件或目录 多条件全盘查找；
- sys_recent：最近修改的文件（按时间倒序）；
- sys_locate：定位 PATH 里的可执行程序。
所有搜索都带时间预算与结果上限，绝不卡死；跳过系统目录/缓存等噪音（Windows 的 Windows/AppData、POSIX 的 /proc、/sys 等）。
"""
from __future__ import annotations

import asyncio
import fnmatch
import os
import shutil
import subprocess
import time

_SKIP_DIRS = {
    "windows", "program files", "program files (x86)", "appdata",
    "$recycle.bin", "system volume information", "recovery", "perflogs",
    "msocache", "node_modules", ".git", "__pycache__", ".venv", "venv",
    "dist", "build", ".ruff_cache", ".idea", ".vscode", "intel", "amd",
    "nvidia", "temp", "tmp", "cache", "caches",
    # POSIX 系统/挂载噪音（避免扫 /proc、/sys 卡住）
    "proc", "sys", "dev", "lost+found", "snap", ".snap", ".cache",
}
_DEFAULT_MAX = 50
_DEFAULT_BUDGET = 25.0


def _drives() -> list:
    """枚举扫描根（跨平台）：Windows 为固定盘符；POSIX 为用户家目录 + 常见挂载点。"""
    if os.name != "nt":
        roots = [os.path.expanduser("~")]
        for mnt in ("/mnt", "/media", "/run/media"):
            try:
                with os.scandir(mnt) as it:
                    roots += [e.path for e in it if e.is_dir(follow_symlinks=False)]
            except OSError:
                continue
        return roots
    try:
        return [d.rstrip("\\/") + "\\" for d in os.listdrives()]
    except AttributeError:
        return [f"{c}:\\" for c in "CDEFGH" if os.path.exists(f"{c}:\\")]


def _norm_ext(ext: str) -> str:
    e = str(ext or "").strip().lower()
    if not e:
        return ""
    if e.startswith("*."):
        e = e[1:]
    if e.startswith("."):
        e = e[1:]
    return e


def _scan_roots(dirs: list, include_sys: bool) -> list:
    """确定要扫描的根目录（默认只扫用户目录/盘符一级；include_sys 才进系统目录）。

    base 本身必须进扫描根：否则直接躺在主目录/盘符根下的文件（如 ~/dahei）永远搜不到。
    子目录照旧加入（过滤噪音）；重复由 _walk_collect 的 visited 集合去重，不会扫两遍。
    """
    if dirs:
        return [d for d in dirs if os.path.isdir(d)]
    roots = []
    for base in _drives():
        if include_sys:
            roots.append(base)
            continue
        if os.path.isdir(base):
            roots.append(base)   # 关键修复：主目录/盘符本身可被搜到
        try:
            with os.scandir(base) as it:
                for entry in it:
                    try:
                        if not entry.is_dir(follow_symlinks=False):
                            continue
                        if entry.name.lower() in _SKIP_DIRS:
                            continue
                        if os.name != "nt" and entry.name.startswith("."):
                            continue   # POSIX：点开头为配置目录，不是用户资料
                        roots.append(entry.path)
                    except OSError:
                        continue
        except OSError:
            continue
    return roots


def _walk_collect(roots: list, want_file: bool, want_dir: bool,
                  pred, max_results: int, budget: float, skip: set):
    """带时间预算的目录树遍历，返回 (hits, timed_out)。hits 元素为 (score, mtime, path, size)。"""
    hits = []
    deadline = time.monotonic() + budget
    timed_out = False
    stack = list(roots)
    visited = set()   # 防重复遍历：roots 里父子目录并存时（base + 一级子目录）只扫一次
    while stack:
        if time.monotonic() >= deadline:
            timed_out = True
            break
        d = stack.pop()
        nd = os.path.normpath(d)
        if nd in visited:
            continue
        visited.add(nd)
        try:
            with os.scandir(d) as it:
                entries = list(it)
        except OSError:
            continue
        for e in entries:
            try:
                if e.is_dir(follow_symlinks=False):
                    if e.name.lower() in skip:
                        continue
                    if want_dir:
                        if pred(e):
                            hits.append(_entry_item(e))
                    stack.append(e.path)
                elif want_file:
                    if pred(e):
                        hits.append(_entry_item(e))
            except OSError:
                continue
            if len(hits) >= max_results * 3:
                break
    hits.sort(key=lambda x: (-x[0], -x[1]))
    return hits[:max_results], timed_out


def _entry_item(e) -> tuple:
    try:
        st = e.stat(follow_symlinks=False)
        return (0, st.st_mtime, e.path, st.st_size)
    except OSError:
        return (0, 0, e.path, 0)


def _fmt_size(n: int) -> str:
    if n >= 2**30:
        return f"{n / 2**30:.1f} GB"
    if n >= 2**20:
        return f"{n / 2**20:.1f} MB"
    if n >= 2**10:
        return f"{n / 2**10:.0f} KB"
    return f"{n} B"


def _fmt_time(ts: float) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))
    except Exception:
        return ""


def _format_hits(hits, label: str, timed_out: bool) -> str:
    if not hits:
        return (f"{label}：未找到匹配" +
                ("（已达到时间预算，仅扫描了部分目录，可加更精确条件或指定 dir 再试）" if timed_out else ""))
    lines = [f"{label}（{len(hits)} 条）："]
    for i, (_score, mt, path, size) in enumerate(hits, 1):
        lines.append(f"{i:>3}. {path}（{_fmt_size(size)}，{_fmt_time(mt)}）")
    if timed_out:
        lines.append("（已达到时间预算，仅扫描了部分目录；可指定 dir 缩小范围）")
    return "\n".join(lines)


async def sys_find(args: dict) -> str:
    """多条件全盘查找文件/目录。"""
    name = str(args.get("name") or "").strip()
    ext = str(args.get("ext") or "").strip()
    dirs = [str(x).strip().strip('"') for x in str(args.get("dir") or "").split(",") if str(x).strip()]
    kind = str(args.get("kind") or "file").strip().lower()
    include_sys = bool(args.get("include_sys"))
    min_mb = float(args.get("min_size_mb") or 0)
    max_mb = float(args.get("max_size_mb") or 0)
    days = float(args.get("newer_than_days") or 0)
    max_results = max(5, min(int(args.get("max_results") or _DEFAULT_MAX), 200))
    budget = max(5.0, min(float(args.get("timeout") or _DEFAULT_BUDGET), 60.0))
    if not name and not ext:
        return "错误：name 和 ext 至少填一个（如 name=报告 或 ext=mp4）"

    exts = [x for x in (_norm_ext(e) for e in ext.split(",")) if x]
    glob_mode = "*" in name or "?" in name
    name_low = name.lower()
    want_file = kind in ("file", "both")
    want_dir = kind in ("dir", "both")
    skip = set(_SKIP_DIRS) if not include_sys else {".git", "__pycache__", "$recycle.bin"}

    def pred(e):
        nl = e.name.lower()
        if glob_mode:
            if not fnmatch.fnmatch(nl, name_low):
                return False
        elif name_low and name_low not in nl:
            return False
        if exts and not any(nl.endswith("." + x) for x in exts):
            return False
        try:
            st = e.stat(follow_symlinks=False)
            if min_mb and st.st_size < min_mb * 2**20:
                return False
            if max_mb and st.st_size > max_mb * 2**20:
                return False
            if days and time.time() - st.st_mtime > days * 86400:
                return False
        except OSError:
            pass
        return True

    roots = await asyncio.to_thread(_scan_roots, dirs, include_sys)
    if not roots:
        return "错误：没有可扫描的目录（dir 不存在？）"
    hits, timed_out = await asyncio.to_thread(
        _walk_collect, roots, want_file, want_dir, pred, max_results, budget, skip)

    # 名字相关度排序分（精确 > 前缀 > 包含），再按修改时间倒序
    if name and not glob_mode:
        for i, (score, mt, path, size) in enumerate(hits):
            base = os.path.basename(path).lower()
            score = 3 if base == name_low else (2 if base.startswith(name_low) else 1)
            hits[i] = (score, mt, path, size)
        hits.sort(key=lambda x: (-x[0], -x[1]))
    cond = []
    if name:
        cond.append(f"名称含「{name}」")
    if ext:
        cond.append(f"类型 .{','.join(exts)}")
    if min_mb or max_mb:
        cond.append(f"大小 {min_mb or 0}-{max_mb or '∞'} MB")
    if days:
        cond.append(f"近 {days:.0f} 天修改")
    return _format_hits(hits, f"查找「{'、'.join(cond) or '全部'}」", timed_out)


async def sys_recent(args: dict) -> str:
    """最近修改的文件（按修改时间倒序）。"""
    days = max(1, int(args.get("days") or 7))
    dirs = [str(x).strip().strip('"') for x in str(args.get("dir") or "").split(",") if str(x).strip()]
    ext = str(args.get("ext") or "").strip()
    max_results = max(5, min(int(args.get("max_results") or 50), 200))
    budget = max(5.0, min(float(args.get("timeout") or _DEFAULT_BUDGET), 60.0))
    exts = [x for x in (_norm_ext(e) for e in ext.split(",")) if x]
    skip = set(_SKIP_DIRS)
    cutoff = time.time() - days * 86400

    def pred(e):
        nl = e.name.lower()
        if exts and not any(nl.endswith("." + x) for x in exts):
            return False
        try:
            return e.stat(follow_symlinks=False).st_mtime >= cutoff
        except OSError:
            return False

    roots = await asyncio.to_thread(_scan_roots, dirs, False)
    if not roots:
        return "错误：没有可扫描的目录"
    hits, timed_out = await asyncio.to_thread(
        _walk_collect, roots, True, False, pred, max_results, budget, skip)
    return _format_hits(hits, f"近 {days} 天修改的文件", timed_out)


def _which_all(name: str) -> list:
    """枚举 PATH 中所有匹配的可执行文件（跨平台；shutil.which 只给第一个，不够）。

    Windows 按 PATHEXT 补 .exe/.cmd/...；POSIX 校验可执行位，路径区分大小写。"""
    exts = [""]
    if os.name == "nt":
        pathext = os.environ.get("PATHEXT") or ".COM;.EXE;.BAT;.CMD"
        exts += [e for e in pathext.split(";") if e]
    found, seen = [], set()
    for d in os.environ.get("PATH", "").split(os.pathsep):
        if not d:
            continue
        for ext in exts:
            p = os.path.join(d, name + ext)
            if not os.path.isfile(p):
                continue
            if os.name != "nt" and not os.access(p, os.X_OK):
                continue
            key = p.lower() if os.name == "nt" else p
            if key not in seen:
                seen.add(key)
                found.append(p)
    if not found:
        w = shutil.which(name)
        if w:
            found.append(w)
    return found


async def sys_locate(args: dict) -> str:
    """定位 PATH 里的可执行程序（Windows 按 PATHEXT，POSIX 按可执行位）。"""
    name = str(args.get("name") or "").strip()
    if not name:
        return "错误：name 不能为空"
    try:
        lines = await asyncio.to_thread(_which_all, name)
    except Exception as e:
        return f"定位失败：{e.__class__.__name__}: {e}"
    if not lines:
        return f"PATH 中没有找到「{name}」（可用 sys_find name={name} kind=file 全盘找）"
    head = f"「{name}」位于："
    return head + "\n" + "\n".join(f"{i}. {ln}" for i, ln in enumerate(lines[:10], 1))
