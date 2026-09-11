# -*- coding: utf-8 -*-
"""工具调度：依赖感知并行 + 只读结果缓存。

【为什么要这个模块】
agent.py 已经支持「同一轮多个工具并行」（asyncio.gather），但那是**无差别**并行：
把只读的 code_read 和写文件的 code_edit 一视同仁地扔进同一批。这有两个问题：

  1. **危险**：两个写工具若落在同一路径上，会互相覆盖——后写的把先写的成果吃掉。
     这不是理论风险，是真实踩过的坑（同一文件并行编辑，第一处改动凭空消失）。
  2. **浪费**：只读工具被写工具拖住，明明互不相干却要一起等。

而「提速」的另一个大头是**重复读**：agent.py 专门统计了 eff_re_reads（同一轮里
完全相同的工具+参数再次出现），说明模型重读同一文件是常态。每一次重读都是一次
真实的磁盘 IO + 一次可能的慢搜索，而文件内容在这期间根本没变。

【本模块只做两件事，各对应一个问题】
  plan(pending)   → 把待执行工具切成「批内可安全并行」的批次：
                    只读一批并行跑；写工具按资源键分桶，同键必分属不同批。
  ReadCache       → 只读结果缓存：带 path 的按文件 mtime 校验（跨轮安全），
                    不带 path 的只在同一 epoch 内有效（写操作后立刻失效）。

【安全边界（宁可慢，不可错）】
  · 判定不出资源键的写工具 → 独占一批，绝不同批并行。
  · 任何写工具执行后 → 缓存 epoch +1，无 path 的缓存立即全失效。
  · 缓存命中失败（文件被外部改动）→ 视为未命中，照常执行。
  · 本模块任何异常都不向上抛：调度失败就退化成「全部串行」，缓存失败就当没有缓存。
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger("harness.tool_sched")

# ---------- 只读工具：可任意并行，且结果可缓存 ----------
# 判据只有一条：这个工具**不改变磁盘上任何东西**。不满足就不许进这个集合。
READONLY_TOOLS = frozenset({
    # 代码检索 / 阅读
    "code_search", "code_list_files", "code_read", "code_locate",
    "code_analyze", "code_deps", "read_lines", "read_json", "symbols",
    "search_text", "list_files", "find_file",
    # git 只读
    "git_status", "git_diff", "git_log", "git_blame",
    "code_git_status", "code_git_diff", "code_git_log", "code_git_blame",
    # 本机只读
    "sys_find", "sys_recent", "sys_locate", "system_check",
    # 工作区只读
    "workspace_get", "workspace_roots", "workspace_list",
    "workspaces_list", "wt_list", "wt_status", "wt_diff",
})

# ---------- 可缓存的只读工具：纯文件读取，结果只取决于磁盘内容 ----------
# code_search / code_locate 这类不带 path 的也放进来，但它们只在同 epoch 内有效。
CACHEABLE_TOOLS = frozenset({
    "code_read", "read_lines", "read_json", "code_list_files", "list_files",
    "symbols", "code_locate", "code_analyze", "code_deps",
    "search_text", "code_search", "find_file",
    "git_status", "git_diff", "git_log",
})

# 路径类参数的候选键名：不同工具叫法不同，挨个试
_PATH_KEYS = ("path", "file", "target", "root", "file_path", "filepath", "dir")

# 判定不出资源键时使用的「独占」键：带这个键的调用必须单独一批
EXCLUSIVE = ("*",)


def is_readonly(tool_name: str) -> bool:
    """是否只读工具。未知工具一律视为「非只读」——保守优先。"""
    return str(tool_name or "") in READONLY_TOOLS


def _norm_path(p: Any) -> Optional[str]:
    """规范化路径用于比较：绝对化 + 统一大小写（Windows 路径不区分大小写）。"""
    if not p or not isinstance(p, (str, os.PathLike)):
        return None
    try:
        s = str(p).strip()
        if not s:
            return None
        return os.path.normcase(os.path.abspath(s))
    except Exception:
        return None


def resource_key(tool_name: str, args: Any) -> Tuple:
    """写工具的资源键：**同键的调用必须串行**，否则会互相覆盖。

    能判定出目标路径 → 用路径做键（不同文件的写可以并行）。
    判定不出 → 返回 EXCLUSIVE，独占一批（宁可慢，不可错）。
    """
    if not isinstance(args, dict):
        return EXCLUSIVE
    for k in _PATH_KEYS:
        got = _norm_path(args.get(k))
        if got:
            return ("path", got)
    # 能枚举出多个路径的（如 code_patch 的 files）→ 用排序后的路径集合做键
    for k in ("files", "paths"):
        v = args.get(k)
        if isinstance(v, str) and v.strip():
            parts = [_norm_path(x) for x in v.replace("\n", ",").split(",")]
            parts = sorted(x for x in parts if x)
            if parts:
                return ("paths", tuple(parts))
    # 影响面未知（shell 命令可能改任何东西）→ 独占
    return EXCLUSIVE


def plan(pending: Sequence[Tuple[int, str, Any]]) -> List[List[Tuple[int, str, Any]]]:
    """把待执行工具切成批次，批内可安全并行、批间必须串行。

    Args:
        pending: [(下标, 工具名, 参数)] —— 下标用于把结果对回原始顺序。

    Returns:
        批次列表。只读工具永远在第一批（它们彼此无冲突）；
        写工具按资源键分桶，同键必分属不同批次。

    出错时返回 [[每一项单独成批]]，即完全串行——降级但正确。
    """
    try:
        items = list(pending or [])
        if len(items) <= 1:
            return [[it] for it in items]

        readonly = [it for it in items if is_readonly(it[1])]
        writers = [it for it in items if not is_readonly(it[1])]

        batches: List[List[Tuple[int, str, Any]]] = []
        if readonly:
            batches.append(readonly)   # 只读之间无冲突，整批并行

        # 写工具：按资源键分桶，同键不能落在同一批
        buckets: List[Tuple[set, List]] = []
        for it in writers:
            key = resource_key(it[1], it[2])
            for keys, bucket in buckets:
                if key not in keys:
                    keys.add(key)
                    bucket.append(it)
                    break
            else:
                buckets.append(({key}, [it]))

        for keys, bucket in buckets:
            if EXCLUSIVE in keys:
                # 独占键：每个单独一批，不与他人并行
                batches.extend([it] for it in bucket)
            else:
                batches.append(bucket)
        return batches
    except Exception as e:  # 调度只是优化，绝不能成为故障源
        logger.warning("[tool_sched] plan 失败，退化为完全串行: %s", e)
        return [[it] for it in (pending or [])]


def has_write_conflict(pending: Sequence[Tuple[int, str, Any]]) -> bool:
    """这一轮里是否存在「必须串行」的写操作（同键或独占）。

    供调用方快速判断：没有冲突就沿用原有的整批并行，有冲突才走分批路径。
    """
    try:
        writers = [it for it in (pending or []) if not is_readonly(it[1])]
        if len(writers) <= 1:
            return False
        seen = set()
        for it in writers:
            key = resource_key(it[1], it[2])
            if key == EXCLUSIVE or key in seen:
                return True
            seen.add(key)
        return False
    except Exception:
        return True   # 判定不了就当作有冲突（保守）


# ============================================================
#  只读结果缓存
# ============================================================

def _stat_sig(tool_name: str, args: Any) -> Optional[Tuple]:
    """取「结果有效性签名」：文件类工具用 mtime+size，取不到就返回 None。

    签名一致 ⇒ 磁盘内容没变过 ⇒ 缓存结果依然成立。
    """
    if not isinstance(args, dict):
        return None
    sigs = []
    for k in _PATH_KEYS:
        p = args.get(k)
        if not p or not isinstance(p, (str, os.PathLike)):
            continue
        try:
            st = os.stat(str(p))
            sigs.append((_norm_path(p), st.st_mtime_ns, st.st_size))
        except OSError:
            sigs.append((_norm_path(p), None, None))   # 文件不存在也是一种「状态」
    return tuple(sigs) if sigs else None


class ReadCache:
    """只读工具结果缓存。

    两种有效性口径，按工具是否有 path 参数自动选择：
      · 有 path → mtime+size 签名一致即可用（跨轮安全，外部改动会自动失效）
      · 无 path → 只在同一 epoch 内有效（任何写操作都会把 epoch 推进，立即失效）

    Args:
        ttl: 条目存活秒数（默认 90s，够覆盖一轮密集探索，又不至于拿到太旧的东西）
        maxsize: 最多缓存多少条（超出按插入顺序淘汰最早的）
    """

    def __init__(self, ttl: float = 90.0, maxsize: int = 128):
        # 下限 0.1s 而不是 1s：留出可测试的空间（单测用 0.2s 验证过期逻辑）
        self.ttl = max(0.1, float(ttl))
        self.maxsize = max(8, int(maxsize))
        self._data: Dict[str, Tuple[float, int, Optional[Tuple], Any]] = {}
        self._epoch = 0
        self.hits = 0
        self.misses = 0

    # ---------- 内部 ----------
    @staticmethod
    def _key(tool_name: str, args: Any) -> str:
        try:
            return tool_name + "|" + json.dumps(args, sort_keys=True, ensure_ascii=False,
                                                default=str)
        except Exception:
            return ""

    # ---------- 对外 ----------
    @property
    def epoch(self) -> int:
        return self._epoch

    def invalidate(self) -> None:
        """写操作后调用：推进 epoch。

        只推进、不清空——因为两类条目的失效条件不同：
          · 无 path（搜索类）→ epoch 变了即失效（写操作可能改变搜索结果）
          · 有 path（读取类）→ 继续用 mtime 自证。写操作若真改了那个文件，
            mtime 自然变了，会自动失效；没改就说明缓存依然成立，不该白跑一次。
        """
        self._epoch += 1

    def get(self, tool_name: str, args: Any):
        """命中返回 (result, success)，否则 None。"""
        if str(tool_name) not in CACHEABLE_TOOLS:
            return None
        k = self._key(tool_name, args)
        if not k:
            return None
        item = self._data.get(k)
        if item is None:
            self.misses += 1
            return None
        ts, epoch, sig, value = item
        # 过期
        if time.monotonic() - ts > self.ttl:
            self._data.pop(k, None)
            self.misses += 1
            return None
        # 有效性：有签名比签名，无签名比 epoch
        if sig is not None:
            if _stat_sig(tool_name, args) != sig:
                self._data.pop(k, None)
                self.misses += 1
                return None
        elif epoch != self._epoch:
            self._data.pop(k, None)
            self.misses += 1
            return None
        self.hits += 1
        return value

    def put(self, tool_name: str, args: Any, result: Any, success: bool) -> None:
        """写入缓存。只缓存成功的、且结果不为空的小结果（大结果缓存收益低、占内存）。"""
        try:
            if not success or result is None:
                return
            if str(tool_name) not in CACHEABLE_TOOLS:
                return
            text = result if isinstance(result, str) else str(result)
            if not text or len(text) > 200_000:   # 200KB 以上不缓存
                return
            k = self._key(tool_name, args)
            if not k:
                return
            if len(self._data) >= self.maxsize:
                # 按插入顺序淘汰最早的
                oldest = next(iter(self._data), None)
                if oldest:
                    self._data.pop(oldest, None)
            self._data[k] = (time.monotonic(), self._epoch,
                             _stat_sig(tool_name, args), (result, True))
        except Exception as e:
            logger.debug("[tool_sched] 缓存写入失败（忽略）: %s", e)

    def stats(self) -> Dict[str, Any]:
        total = self.hits + self.misses
        return {
            "hits": self.hits,
            "misses": self.misses,
            "hit_rate": round(self.hits / total, 3) if total else None,
            "size": len(self._data),
            "epoch": self._epoch,
        }


# 进程级单例：缓存要跨轮才有价值，每次新建等于没有
_CACHE: Optional[ReadCache] = None


def get_cache() -> ReadCache:
    global _CACHE
    if _CACHE is None:
        _CACHE = ReadCache()
    return _CACHE
