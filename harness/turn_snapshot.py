"""轮级文件快照：一轮里被改过的文件，可以整轮退回。

为什么需要：`.bak-<时间戳>` 只解决「单个文件、刚改坏、撤上一次」，且散落在源文件旁；
`code_create_file` / `code_append` 连备份都没有。多文件重构方向做错时，没有任何
一处能回答「这一轮到底动了哪些文件、怎么一次全部还原」。

做法：写工具执行**前**，把目标文件的原内容复制进 `data/turn_snapshots/<turn_id>/`，
同一文件本轮只存第一次——第一次捕获到的就是轮前状态。undo 按清单整轮还原。

边界（别当成万能的）：
- 只覆盖参数里带得出路径的写工具（code_edit / code_append / code_create_file /
  code_patch）。shell_run、wt_run 这类「命令里能改任何东西」的工具覆盖不到，它们
  改了什么快照不知道；capture 会把这类调用记进 manifest 的 blind 列表，undo 报告
  里明确提示「本轮还有这些工具动过磁盘，不在还原范围」。
- 快照存的是轮前内容副本，不是版本控制。撤回之后再改，快照不会跟着更新。
- 任何异常都不向上抛：快照失败绝不能让工具执行失败。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import threading
import time
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("harness.turn_snapshot")

BASE_DIR = Path(__file__).resolve().parent.parent
SNAP_DIR = BASE_DIR / "data" / "turn_snapshots"

KEEP_TURNS = 20                      # 保留最近多少轮（按目录 mtime 淘汰）
MAX_FILE_BYTES = 4 * 1024 * 1024     # 单文件超过这个大小不快照（记 skipped）
MAX_FILES_PER_TURN = 200             # 单轮最多快照多少文件，防爆盘

# 会改文件的工具 → 路径参数键（按优先级）。只列白名单：宁可漏，也不能把只读调用
# 算进来——误捕获会让 undo 报出一堆「其实没改」的文件，报告就没人看了。
_WRITE_TOOLS: Dict[str, Tuple[str, ...]] = {
    "code_edit": ("file", "files"),
    "code_append": ("path",),
    "code_create_file": ("path",),
}

# 「黑盒写盘」工具：命令/委派类，改了哪些文件从参数里看不出来，只有这些才值得点名。
_BLIND_WATCH = frozenset({"shell_run", "wt_run"})
_BLIND_HINTS = ("delegate", "spawn", "agent_task", "codex", "opencode", "dsh")

_turn_var: ContextVar[str] = ContextVar("turn_snapshot_turn", default="")
_lock = threading.Lock()


# ---------------- 轮标识 ----------------

def set_turn(turn_id: str) -> None:
    """由 agent 在每轮对话开始时调用，后续工具执行都归到这个轮里。"""
    _turn_var.set(str(turn_id or ""))


def current_turn() -> str:
    return _turn_var.get() or ""


def _enabled() -> bool:
    """开关：settings.json -> agent.turn_snapshot（默认开启）。"""
    try:
        fp = BASE_DIR / "settings.json"
        if fp.is_file():
            cfg = json.loads(fp.read_text(encoding="utf-8"))
            return bool((cfg.get("agent") or {}).get("turn_snapshot", True))
    except Exception:
        pass
    return True


def _turn_dir(turn_id: str) -> Path:
    safe = re.sub(r"[^0-9A-Za-z_.-]+", "_", str(turn_id or "")) or "unknown"
    return SNAP_DIR / safe


# ---------------- 路径提取 ----------------

def _split_paths(v: Any) -> List[str]:
    if isinstance(v, (list, tuple)):
        return [str(x).strip() for x in v if str(x).strip()]
    return [p.strip() for p in re.split(r"[,\n]", str(v or "")) if p.strip()]


def _resolve_path(s: str, root: Any) -> Optional[Path]:
    """相对路径按 root（缺省当前工作目录）解析——与 code_ops 的 _norm_root 同口径。"""
    try:
        s = os.path.expanduser(str(s or "").strip())
        if not s:
            return None
        if not os.path.isabs(s):
            base = str(root or "").strip() or os.getcwd()
            s = os.path.join(os.path.expanduser(base), s)
        return Path(s).resolve()
    except Exception:
        return None


_PATCH_PATH_RE = re.compile(r"^(?:---|\+\+\+)\s+(?:[ab]/)?(.+?)(?:\t.*)?$", re.M)


def _patch_paths(patch_text: str) -> List[str]:
    out = []
    for m in _PATCH_PATH_RE.finditer(patch_text or ""):
        p = m.group(1).strip()
        if p and p != "/dev/null":
            out.append(p)
    return out


def _paths_for(tool_name: str, args: Dict[str, Any]) -> List[Path]:
    """这个工具调用会写哪些文件。取不出就返回空（不猜）。"""
    out: List[Path] = []
    keys = _WRITE_TOOLS.get(tool_name)
    if keys:
        for k in keys:
            for s in _split_paths(args.get(k)):
                p = _resolve_path(s, args.get("root"))
                if p:
                    out.append(p)
    elif tool_name == "code_patch":
        for s in _patch_paths(str(args.get("patch") or "")):
            p = _resolve_path(s, args.get("root"))
            if p:
                out.append(p)
    return out


# ---------------- 捕获 ----------------

def _read_manifest(tdir: Path) -> Dict[str, Any]:
    fp = tdir / "manifest.json"
    if not fp.is_file():
        return {"turn_id": tdir.name, "created_at": time.time(), "files": [], "blind": []}
    try:
        data = json.loads(fp.read_text(encoding="utf-8"))
        data.setdefault("files", [])
        data.setdefault("blind", [])
        return data
    except Exception:
        return {"turn_id": tdir.name, "created_at": time.time(), "files": [], "blind": []}


def _write_manifest(tdir: Path, man: Dict[str, Any]) -> None:
    tdir.mkdir(parents=True, exist_ok=True)
    tmp = tdir / "manifest.json.tmp"
    tmp.write_text(json.dumps(man, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(tdir / "manifest.json")


def capture(tool_name: str, arguments: Any) -> List[str]:
    """工具执行**前**调用。返回本轮新捕获的路径（已捕获过的返回空，幂等）。"""
    try:
        if not _enabled():
            return []
        if not isinstance(arguments, dict):
            return []
        # preview 不落盘，没必要占快照
        if arguments.get("preview"):
            return []
        turn = current_turn()
        if not turn:
            return []

        paths = _paths_for(tool_name, arguments)
        if not paths:
            _note_blind(turn, tool_name)
            return []

        tdir = _turn_dir(turn)
        if not tdir.exists():
            prune()          # 新一轮首次落盘时清一次旧快照，不必每次捕获都扫目录
        with _lock:
            man = _read_manifest(tdir)
            known = {f["path"] for f in man["files"]}
            fresh: List[str] = []
            for p in paths:
                sp = str(p)
                if sp in known:
                    continue
                if len(man["files"]) >= MAX_FILES_PER_TURN:
                    break
                rec = _store(tdir, p, tool_name, len(man["files"]) + 1)
                man["files"].append(rec)
                known.add(sp)
                if rec.get("skipped"):
                    logger.warning("快照跳过 %s：%s", sp, rec["skipped"])
                else:
                    fresh.append(sp)
            _write_manifest(tdir, man)
        return fresh
    except Exception as e:
        logger.warning("轮快照捕获失败（不影响工具执行）: %s", e)
        return []


def _store(tdir: Path, p: Path, tool_name: str, seq: int) -> Dict[str, Any]:
    rec: Dict[str, Any] = {
        "path": str(p),
        "existed": p.is_file(),
        "tool": tool_name,
        "at": time.time(),
    }
    if not rec["existed"]:
        return rec      # 原来就没有 → undo 时删掉即可
    try:
        size = p.stat().st_size
    except OSError as e:
        rec["skipped"] = f"stat 失败 {e}"
        return rec
    if size > MAX_FILE_BYTES:
        rec["skipped"] = f"文件 {size} 字节，超过单文件上限 {MAX_FILE_BYTES}"
        return rec
    files_dir = tdir / "files"
    files_dir.mkdir(parents=True, exist_ok=True)
    snap_name = f"{seq:04d}_{re.sub(r'[^0-9A-Za-z_.-]+', '_', p.name)[:80]}"
    try:
        raw = p.read_bytes()
        (files_dir / snap_name).write_bytes(raw)
    except OSError as e:
        rec["skipped"] = f"读取失败 {e}"
        return rec
    rec["snap"] = snap_name
    rec["bytes"] = len(raw)
    rec["sha256"] = hashlib.sha256(raw).hexdigest()
    return rec


def _note_blind(turn: str, tool_name: str) -> None:
    """记下「动过磁盘但快照看不见」的工具调用，undo 报告要如实说出来。

    只认「黑盒写盘」这一类：命令/委派，改了哪些文件从参数里看不出来。不能把所有
    非只读工具都算进来——code_verify 跑个测试、code_undo_turn 自己还原文件、生图
    写张图，都会被误报成「未追踪的写操作」，报告天天喊狼来了就没人看了。
    """
    name = str(tool_name or "")
    if name not in _BLIND_WATCH and not any(h in name for h in _BLIND_HINTS):
        return
    tdir = _turn_dir(turn)
    with _lock:
        man = _read_manifest(tdir)
        if name not in man["blind"]:
            man["blind"].append(name)
            _write_manifest(tdir, man)


# ---------------- 查询与还原 ----------------

def list_turns(limit: int = 20) -> List[Dict[str, Any]]:
    """可用快照轮（新 → 旧）。"""
    if not SNAP_DIR.is_dir():
        return []
    out: List[Dict[str, Any]] = []
    dirs = [d for d in SNAP_DIR.iterdir() if d.is_dir()]
    for d in sorted(dirs, key=lambda x: x.stat().st_mtime, reverse=True):
        man = _read_manifest(d)
        files = man.get("files") or []
        out.append({
            "turn_id": man.get("turn_id") or d.name,
            "created_at": man.get("created_at"),
            "files": len([f for f in files if not f.get("skipped")]),
            "skipped": len([f for f in files if f.get("skipped")]),
            "blind": list(man.get("blind") or []),
            "mtime": d.stat().st_mtime,
        })
        if len(out) >= limit:
            break
    return out


def _latest_turn() -> str:
    turns = list_turns(limit=1)
    return turns[0]["turn_id"] if turns else ""


def undo(turn_id: str = "", paths: Optional[List[str]] = None) -> str:
    """把一轮改过的文件还原成轮前状态。turn_id 留空 = 最近一轮。

    paths 非空时只还原这几个文件（其余留着）——一轮里改对了 3 个、改错了 2 个时用。
    """
    turn = str(turn_id or "").strip() or _latest_turn()
    if not turn:
        return "没有可还原的轮快照（data/turn_snapshots 为空或本轮还没改过文件）。"
    tdir = _turn_dir(turn)
    man = _read_manifest(tdir)
    files = man.get("files") or []
    if not files:
        return f"轮 {turn} 没有文件快照（本轮可能只跑了只读工具）。"

    want = None
    if paths:
        want = {str(_resolve_path(p, None)) for p in paths if str(p).strip()}

    restored, deleted, unchanged, failed, skipped = [], [], [], [], []
    for rec in files:
        p = Path(str(rec.get("path") or ""))
        if want is not None and str(p) not in want:
            continue
        if rec.get("skipped"):
            skipped.append(f"{p}（{rec['skipped']}）")
            continue
        try:
            if rec.get("existed"):
                snap = tdir / "files" / str(rec.get("snap") or "")
                if not snap.is_file():
                    failed.append(f"{p}（快照文件丢失：{rec.get('snap')}）")
                    continue
                raw = snap.read_bytes()
                if p.is_file() and hashlib.sha256(p.read_bytes()).hexdigest() == rec.get("sha256"):
                    unchanged.append(str(p))
                    continue
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(raw)
                restored.append(str(p))
            else:
                if p.exists():
                    shutil.rmtree(p) if p.is_dir() else p.unlink()
                    deleted.append(str(p))
                else:
                    unchanged.append(str(p))
        except OSError as e:
            failed.append(f"{p}（{e}）")

    head = (f"⏪ 轮 {turn} 还原：{len(restored)} 个改回轮前内容、"
            f"{len(deleted)} 个新建的已删除、{len(unchanged)} 个本来就一致")
    body = []
    for x in restored:
        body.append(f"  ↩ {x}")
    for x in deleted:
        body.append(f"  🗑 {x}")
    if skipped:
        body.append(f"  ⚠ 未纳入快照（改不回）：{len(skipped)} 个")
        body += [f"    - {x}" for x in skipped[:5]]
    if failed:
        body.append(f"  ❌ 还原失败：{len(failed)} 个")
        body += [f"    - {x}" for x in failed[:5]]
    blind = list(man.get("blind") or [])
    if blind:
        body.append(f"  ⚠ 本轮还有这些工具动过磁盘、但快照覆盖不到，请自行检查：{', '.join(blind)}")
    return "\n".join([head] + body)


# ---------------- 保留策略 ----------------

def prune(keep: int = KEEP_TURNS) -> int:
    """只保留最近 keep 轮快照，返回删掉的轮数。"""
    try:
        if not SNAP_DIR.is_dir():
            return 0
        dirs = [d for d in SNAP_DIR.iterdir() if d.is_dir()]
        dirs.sort(key=lambda x: x.stat().st_mtime, reverse=True)
        n = 0
        for d in dirs[max(0, int(keep)):]:
            shutil.rmtree(d, ignore_errors=True)
            n += 1
        return n
    except Exception as e:
        logger.warning("轮快照清理失败: %s", e)
        return 0
