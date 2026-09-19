"""代码工程（code_ops）—— 大白自带的顶级编程能力：批量检索 / 分析 / 修改代码。

设计要点：
- 纯标准库实现，无第三方依赖；所有路径默认限制在 root（当前工作目录）内，
  分析其它项目时显式传 root；
- 检索/分析默认跳过 node_modules/.git/__pycache__ 等噪音目录
  （include_noise=true 可包含）；
- code_edit 采用「唯一锚点」精准替换：锚点出现 0 次或多于 1 次时只报告、不擅改，
  避免误伤；修改前自动留 .bak-<时间戳> 备份，修改后返回 diff 预览；
- git 感知：code_git_status/diff/log/blame 让大白知道改了什么、谁改的，
  配合 code_review 在交付前自审自己的改动；
- code_patch 支持统一的补丁式编辑（多文件、严格上下文校验、可预览）；
- code_test 跑完整测试套件，验证不再停留在语法级；
- code_smoke 冒烟关卡：语法 + import（模块能加载）+ 可选冒烟命令，
  改库/模块后必须跑，缺依赖/循环导入/顶层报错当场暴露；
- 安全边界：允许修改大白核心（harness/*.py 与项目根目录 *.py）；
  修改这类文件会触发整进程自动重启生效，改完务必 code_verify + code_smoke 验证；
- 所有返回均为可读文本，输出统一截断防刷屏。
"""
from __future__ import annotations

import ast
import difflib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import warnings
from pathlib import Path

# 合并自原 shell 技能：本机命令行/文件查找/系统体检等 10 个工具
# 合并自原 sys_search 技能：全盘文件搜索等 3 个工具
# 合并自原 worktree 技能：git 隔离工作树等 7 个工具
_SKILL_DIR = os.path.dirname(os.path.abspath(__file__))
if _SKILL_DIR not in sys.path:
    sys.path.insert(0, _SKILL_DIR)
import shell_impl  # noqa: E402
import sys_search_impl  # noqa: E402
import worktree_impl  # noqa: E402

_MAX_OUT = 30000
_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

NOISE_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build",
    ".idea", ".vscode", ".ruff_cache", ".pytest_cache", ".mypy_cache",
    "codex_logs", "audio_cache", "undefined", ".trae-html-share-packages",
    ".next", ".nuxt", "coverage",
}

CODE_EXTS = {
    ".py", ".js", ".ts", ".jsx", ".tsx", ".mjs", ".cjs", ".vue", ".svelte",
    ".html", ".css", ".scss", ".less", ".json", ".jsonc", ".java", ".kt",
    ".go", ".rs", ".c", ".h", ".cpp", ".hpp", ".cc", ".cs", ".rb", ".php",
    ".swift", ".sh", ".ps1", ".bat", ".cmd", ".md", ".yaml", ".yml", ".toml",
    ".ini", ".cfg", ".sql", ".xml", ".gradle",
}

DEF_PATTERNS = {
    ".py": [
        r"^\s*(?:async\s+)?def\s+([A-Za-z_]\w*)",
        r"^\s*class\s+([A-Za-z_]\w*)",
    ],
    ".js": [
        r"^\s*(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_$]\w*)",
        r"^\s*(?:export\s+)?class\s+([A-Za-z_$]\w*)",
        r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$]\w*)\s*=",
        r"^\s*(?:export\s+)?(?:interface|type)\s+([A-Za-z_$]\w*)",
    ],
    ".ts": [
        r"^\s*(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_$]\w*)",
        r"^\s*(?:export\s+)?class\s+([A-Za-z_$]\w*)",
        r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$]\w*)\s*=",
        r"^\s*(?:export\s+)?(?:interface|type)\s+([A-Za-z_$]\w*)",
        r"^\s*(?:export\s+)?abstract\s+class\s+([A-Za-z_$]\w*)",
    ],
    ".go": [
        r"^func\s+(?:\([^)]*\)\s*)?([A-Za-z_]\w*)",
        r"^type\s+([A-Za-z_]\w*)\s+(?:struct|interface)",
    ],
    ".java": [
        r"^\s*(?:(?:public|private|protected|static|final|abstract|synchronized|native)\s+)*(?:class|interface|enum)\s+([A-Za-z_]\w*)",
        r"^\s*(?:(?:public|private|protected|static|final|abstract|synchronized|native)\s+)*[\w<>\[\]?,\s]+\s+([A-Za-z_]\w*)\s*\(",
    ],
    ".kt": [
        r"^\s*(?:data\s+|sealed\s+|enum\s+|abstract\s+)?(?:class|interface|object)\s+([A-Za-z_]\w*)",
        r"^\s*(?:suspend\s+)?fun\s+([A-Za-z_]\w*)",
    ],
    ".rs": [
        r"^\s*(?:pub\s+)?fn\s+([A-Za-z_]\w*)",
        r"^\s*(?:pub\s+)?(?:struct|enum|trait)\s+([A-Za-z_]\w*)",
    ],
    ".rb": [
        r"^\s*def\s+([A-Za-z_]\w*)",
        r"^\s*class\s+([A-Za-z_]\w*)",
    ],
    ".php": [
        r"^\s*function\s+([A-Za-z_]\w*)",
        r"^\s*(?:class|interface|trait)\s+([A-Za-z_]\w*)",
    ],
    ".cs": [
        r"^\s*(?:(?:public|private|protected|internal|static|sealed|abstract|partial|readonly)\s+)*(?:class|interface|struct|enum|record)\s+([A-Za-z_]\w*)",
        r"^\s*(?:(?:public|private|protected|internal|static|virtual|override|async|partial)\s+)*[\w<>\[\],\s]+\s+([A-Za-z_]\w*)\s*\(",
    ],
    ".swift": [
        r"^\s*(?:func|class|struct|enum|protocol)\s+([A-Za-z_]\w*)",
    ],
    ".sh": [
        r"^\s*([A-Za-z_]\w*)\s*\(\)\s*\{?",
    ],
}

_JS_EXTS = {".js", ".ts", ".jsx", ".tsx", ".mjs", ".cjs", ".vue", ".svelte"}


# ---------- 通用工具 ----------

def _norm_root(root=None):
    base = str(root or "").strip() or os.getcwd()
    return Path(os.path.expanduser(base)).resolve()


def _within(root: Path, p: Path) -> bool:
    """路径是否位于 root 内。

    2026-08-30 放开：此前所有文件操作限制在 root（默认项目根目录）内，
    模型想改/建/删其它位置的文件会被「越界路径」拒绝。现在放行为全盘可操作，
    由模型按用户意图显式传 root/path；本函数保留签名但不再拦截。
    """
    return True


def _resolve(root: Path, p: str) -> Path:
    p = os.path.expanduser(str(p or "").strip())
    if not p:
        raise ValueError("路径不能为空")
    abs_p = p if os.path.isabs(p) else str(root / p)
    return Path(abs_p).resolve()


def _read_text(path: Path):
    raw = path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw.decode("utf-8-sig"), "utf-8-sig"
    for enc in ("utf-8", "gb18030", "gbk", "latin-1"):
        try:
            return raw.decode(enc), enc
        except (UnicodeDecodeError, ValueError):
            continue
    return raw.decode("utf-8", errors="replace"), "utf-8"


def _is_binary(path: Path) -> bool:
    try:
        with open(path, "rb") as f:
            return b"\x00" in f.read(8192)
    except OSError:
        return True


def _split_list(s):
    if not s:
        return []
    return [x.strip() for x in re.split(r"[\n,;]+", str(s)) if x.strip()]


def _exts_of(exts_str=None):
    if not exts_str:
        return set(CODE_EXTS)
    out = set()
    for e in _split_list(exts_str):
        e = e.strip().lower()
        if not e.startswith("."):
            e = "." + e
        out.add(e)
    return out


def _norm_paths(paths):
    """兼容字符串（逗号/换行分隔）或列表两种传法。"""
    if not paths:
        return []
    if isinstance(paths, (list, tuple)):
        out = []
        for p in paths:
            out.extend(x for x in re.split(r"[\n,;]+", str(p)) if x.strip())
        return out
    return _split_list(paths)


def _trim(text: str, limit: int = _MAX_OUT) -> str:
    text = str(text or "")
    if len(text) > limit:
        return text[:limit] + f"\n…（输出已截断，剩余 {len(text) - limit} 字符未显示）"
    return text


# TODO 标记的识别口径：**大写 + 单词边界**。
# 为什么不能用 re.I：`todo_create`、`TodoService`、skill.json 里的工具名都会命中，
# 结果「TODO 热点」榜首永远是「实现待办功能本身」的那个文件（实测踩过：
# todo_impl.py 以 48 处霸榜）。大写 + \b 才对应「作者手写的待办注释」这个意图。
_TODO_RE = re.compile(r"\b(?:TODO|FIXME|HACK|XXX)\b")

# TODO 只统计代码文件：markdown/配置里的 TODO 不是「代码里的待办」，而 SKILL.md、
# skill.json 恰恰会为了说明这个功能而写出 TODO 字样，结果文档自己霸榜——
# 自指噪声必须掐掉（实测：未掐掉时 SKILL.md / skill.json 占了三席）。
_TODO_SKIP_EXTS = {".md", ".json", ".yml", ".yaml", ".html", ".css", ".txt", ".toml"}


def _count_todos(text: str) -> int:
    return len(_TODO_RE.findall(text or ""))


def _has_todo(line: str) -> bool:
    return bool(_TODO_RE.search(line or ""))


def _parse_py(text: str, fp=None):
    """安全解析 Python 源码：语法错返回 None，并屏蔽被扫文件的 SyntaxWarning。

    为什么要屏蔽 warning：批量扫描（code_map 会读几百个文件）时撞上 `\\.` 这类
    无效转义，Python 会把警告打到 stderr——那不是我们的输出，却会混进工具结果。
    传 filename 保证真需要时仍能定位到具体文件。
    """
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", SyntaxWarning)
            return ast.parse(text, filename=str(fp) if fp else "<source>")
    except (SyntaxError, ValueError):
        return None


def _iter_files(root, exts=None, include_noise=False, max_depth=None,
                paths=None, limit=0, skip_binary=True):
    root = Path(root).resolve()
    if exts is None:
        exts = set(CODE_EXTS)
    starts = [_resolve(root, p) for p in _norm_paths(paths)] if paths else [root]
    seen, count = set(), 0
    for start in starts:
        if not start.exists():
            continue
        if start.is_file():
            if start.suffix.lower() in exts:
                yield start
                count += 1
            continue
        for dirpath, dirnames, filenames in os.walk(start):
            if max_depth is not None:
                rel = os.path.relpath(dirpath, start)
                depth = 0 if rel == "." else rel.count(os.sep) + 1
                if depth >= max_depth:
                    dirnames[:] = []
                    continue
            if not include_noise:
                dirnames[:] = [d for d in dirnames if d not in NOISE_DIRS]
            for fn in sorted(filenames):
                fp = Path(dirpath) / fn
                try:
                    if fp.suffix.lower() not in exts:
                        continue
                    if skip_binary and _is_binary(fp):
                        continue
                except OSError:
                    continue
                if fp in seen:
                    continue
                seen.add(fp)
                yield fp
                count += 1
                if limit and count >= limit:
                    return


def _rel(root: Path, fp: Path) -> str:
    try:
        return os.path.relpath(str(fp), str(root))
    except ValueError:
        return str(fp)  # 跨盘符（C: ↔ D:）：relpath 无意义，直接返回绝对路径


def _inherit_indent(anchor_line: str, new: str) -> str:
    """insert 模式自动继承锚点行缩进（对标 IDE 自动缩进）。

    规则：新内容里顶格且非空的行，自动补上锚点行的前导空白；
    已有缩进的行保持不动（尊重用户显式给的缩进）。
    """
    m = re.match(r"^[ \t]*", anchor_line)
    indent = m.group(0) if m else ""
    if not indent:
        return new
    out = []
    for ln in new.split("\n"):
        if ln.strip() and not ln[:1].isspace():
            out.append(indent + ln)
        else:
            out.append(ln)
    return "\n".join(out)


def _near_miss_hint(lines: list, anchor: str, max_hits: int = 3) -> str:
    """锚点没找到时，给出最相近的真实行（行号+内容），帮模型快速修正锚点。"""
    target = (anchor or "").strip()
    if not target or not lines:
        return ""
    scored = []
    for i, line in enumerate(lines):
        ls = line.strip()
        if not ls:
            continue
        ratio = difflib.SequenceMatcher(None, target, ls).ratio()
        if ratio >= 0.55:
            scored.append((ratio, i + 1, ls[:100]))
    scored.sort(key=lambda x: -x[0])
    if not scored:
        return ""
    hits = scored[:max_hits]
    return ("最相近的现有内容（供修正锚点）：\n"
            + "\n".join(f"  第 {ln} 行（相似 {r:.0%}）：{text}"
                        for r, ln, text in hits))


def _ast_locate_node(text: str, target: str):
    """AST 结构化定位（对标 ast-grep）：在 .py 源码里找包含 target 的真实代码节点。

    注释/字符串天然不在 AST 里，因此能排除假命中。返回 (start_line, end_line)
    1-based 行号区间；找不到返回 None。
    """
    t = (target or "").strip()
    if not t:
        return None
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return None
    best = None
    for node in ast.walk(tree):
        if not hasattr(node, "lineno"):
            continue
        try:
            seg = ast.get_source_segment(text, node)
        except (TypeError, ValueError, IndentationError):
            continue
        if not seg:
            continue
        if seg.strip() == t:
            return (node.lineno, getattr(node, "end_lineno", node.lineno))
        if t in seg:
            span = (getattr(node, "end_lineno", node.lineno) - node.lineno)
            if best is None or span < (best[1] - best[0]):
                best = (node.lineno, getattr(node, "end_lineno", node.lineno))
    return best


def _fuzzy_locate(text: str, target: str, min_ratio: float = 0.8):
    """模糊容错定位（对标 comby）：target 未逐字符命中时，用 difflib 找最相近的连续行片段。

    返回 (start_line, end_line) 1-based；找不到返回 None。
    """
    lines = text.split("\n")
    t_lines = [l for l in (target or "").strip().split("\n") if l.strip()]
    if not t_lines:
        return None
    first = t_lines[0].strip()
    best_idx, best_ratio = -1, 0.0
    for i, l in enumerate(lines):
        r = difflib.SequenceMatcher(None, first, l.strip()).ratio()
        if r > best_ratio:
            best_ratio, best_idx = r, i
    if best_ratio < min_ratio:
        return None
    start = best_idx
    end = best_idx
    for j in range(1, len(t_lines)):
        if start + j >= len(lines):
            break
        r = difflib.SequenceMatcher(None, t_lines[j].strip(),
                                    lines[start + j].strip()).ratio()
        if r >= min_ratio:
            end = start + j
        else:
            break
    return (start + 1, end + 1)


def _is_core(root: Path, fp: Path) -> bool:
    """大白核心文件识别：harness/*.py 与项目根目录 *.py（仅提示，不再拦截）。"""
    root = Path(root).resolve()
    if not ((root / "codex_runner.py").exists() and (root / "harness").is_dir()):
        return False
    try:
        parts = fp.relative_to(root).parts
    except ValueError:
        return False
    if parts and parts[0] == "harness":
        return True
    return len(parts) == 1 and fp.suffix.lower() == ".py"


# ---------- 1. 批量检索 ----------

# 一次调用最多搜多少个关键词：再多就分两次调用（避免单次输出过长被截断）
_MAX_QUERIES = 8




# ---------- ripgrep 引擎（对标 VS Code / grep.app：Rust 原生扫描） ----------
# 为什么换引擎：2000 文件 / 78 万行用纯 Python 逐行 re 匹配要 ~1s；rg 是 Rust
# 多线程 SIMD 扫描，同量级快 10~50 倍。语法不支持（如 lookahead）或 rg 不可用时
# 自动回退 Python 引擎——两种引擎产出结构一致、结果完全等价。

def _rg_globs(exts, include_noise):
    """扩展名过滤 + 噪音目录排除 → rg 的 -g 参数。"""
    brace = "{" + ",".join(sorted(e.lstrip(".") for e in exts)) + "}"
    globs = [f"*.{brace}"]
    if not include_noise:
        noise = ",".join(sorted(NOISE_DIRS))
        globs += [f"!**/{{{noise}}}/**", f"!{{{noise}}}", "!**/.git/**"]
    return globs


def _rg_hits(root, exts, include_noise, paths, compiled, per_limit,
             case_sensitive, regex_mode):
    """每关键词一个 rg 进程并行扫描；返回 {q: {fp_str: set(行号)}}。

    rg 对 Rust 正则语法不支持（lookahead 等）或超时抛 RuntimeError，
    由调用方回退 Python 引擎——正确性优先，快是第二位的。
    """
    rg = shutil.which("rg")
    if not rg:
        raise RuntimeError("rg 不可用")
    from concurrent.futures import ThreadPoolExecutor
    globs = _rg_globs(exts, include_noise)
    base = [rg, "--json", "--max-count", str(per_limit)]
    if not case_sensitive:
        base.append("-i")
    # include_noise 时连 .gitignore 忽略的目录也搜（对齐 Python 引擎的最大包含语义）；
    # 默认则尊重 .gitignore——VS Code 同款语义，data/ 等非代码目录自动被排除
    if include_noise:
        base += ["--no-ignore", "--hidden", "-g", "!**/.git/**"]
    for g in globs:
        base += ["-g", g]
    starts = [str(s) for s in
              ([_resolve(root, p) for p in _norm_paths(paths)] if paths
               else [Path(root)]) if s.exists()]

    def _one(q, pat):
        # regex 模式用编译前的原始串（Rust 语法接近但不等价，出错即回退）；
        # 字面量模式用原始 q + -F（不能用 re.escape 后的串，双转义会搜不到）。
        pat_src = q if not regex_mode else pat.pattern
        cmd = base + (["-F", "-e", pat_src] if not regex_mode else ["-e", pat_src])
        cmd += starts
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        if r.returncode not in (0, 1):
            raise RuntimeError(f"rg 退出码 {r.returncode}: {r.stderr[:200]}")
        hits = {}
        for raw in r.stdout.splitlines():
            try:
                obj = json.loads(raw)
            except ValueError:
                continue
            if obj.get("type") != "match":
                continue
            d = obj.get("data") or {}
            fp_str = (d.get("path") or {}).get("text", "")
            ln = d.get("line_number")
            if fp_str and ln:
                hits.setdefault(fp_str, set()).add(ln)
        return q, hits

    with ThreadPoolExecutor(max_workers=min(len(compiled), 8)) as ex:
        futs = {ex.submit(_one, q, p): q for q, p in compiled}
        return {futs[f]: f.result()[1] for f in futs}


def _rg_candidate_files(root, exts, include_noise, paths, symbol):
    """rg 粗筛：只返回可能含 symbol 的文件（-l 纯路径，极快）。

    code_locate 用它先过滤再做 AST 精筛——1169 个 .py 全量 parse 是秒级，
    而含某符号的文件通常只有个位数。返回 None 表示 rg 不可用（调用方全量兜底）。
    """
    rg = shutil.which("rg")
    if not rg:
        return None
    globs = _rg_globs(exts, include_noise)
    starts = [str(s) for s in
              ([_resolve(root, p) for p in _norm_paths(paths)] if paths
               else [Path(root)]) if s.exists()]
    cmd = [rg, "-l", "-w", "-F", "-e", symbol]
    if include_noise:
        cmd += ["--no-ignore", "--hidden", "-g", "!**/.git/**"]
    for g in globs:
        cmd += ["-g", g]
    cmd += starts
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
    except (subprocess.TimeoutExpired, OSError):
        return None
    if r.returncode not in (0, 1):
        return None
    return [l for l in r.stdout.splitlines() if l]


def _git_visible_files(root: Path):
    """git 仓库内应被搜索的文件集合（跟踪 + 未跟踪未忽略），返回绝对路径列表。

    语义与 rg 主路径对齐（尊重 .gitignore）：data/ 等被整体忽略的业务工作区
    不进结果。非 git 仓库返回 None → 调用方保持全量遍历兜底。
    """
    try:
        top = subprocess.run(["git", "-C", str(root), "rev-parse", "--show-toplevel"],
                             capture_output=True, text=True, timeout=10)
        if top.returncode != 0:
            return None
        repo = Path(top.stdout.strip())
        r = subprocess.run(
            ["git", "-C", str(repo), "ls-files", "-z",
             "--cached", "--others", "--exclude-standard"],
            capture_output=True, timeout=20)
        if r.returncode != 0:
            return None
        return [str((repo / p).resolve())
                for p in r.stdout.decode("utf-8", "replace").split("\0") if p]
    except (OSError, subprocess.TimeoutExpired):
        return None


def _search_hits_py(root, exts, include_noise, paths, compiled, per_limit):
    """Python 引擎（rg 不可用/语法不支持时的回退）：一次遍历、多模式匹配。"""
    hits = {q: {} for q, _ in compiled}
    totals = {q: 0 for q, _ in compiled}
    for fp in _iter_files(root, exts, include_noise, paths=paths):
        if all(totals[q] >= per_limit for q, _ in compiled):
            break
        try:
            text, _ = _read_text(fp)
        except OSError:
            continue
        lines = text.splitlines()
        for i, line in enumerate(lines, 1):
            for q, pat in compiled:
                if totals[q] >= per_limit:
                    continue
                if pat.search(line):
                    hits[q].setdefault(str(fp), (lines, set()))[1].add(i)
                    totals[q] += 1
    return hits, totals


def _parse_queries(args: dict) -> list:
    """解析检索关键词：queries 数组 / 换行或逗号分隔字符串 / 单个 query。

    为什么要支持批量：一次调用搜 N 个关键词 = 省 N-1 次往返。串行地搜
    「A 在哪定义」「B 在哪引用」是最常见的低效模式，而搜索天然可并行——
    实测一轮里同一个检索工具被拆成 4~5 次调用是常态。
    """
    q = args.get("queries")
    out: list = []
    if isinstance(q, list):
        out = [str(x).strip() for x in q if str(x or "").strip()]
    elif isinstance(q, str):
        out = [x.strip() for x in re.split(r"[\n,，;；]", q) if x.strip()]
    if not out:
        single = str(args.get("query") or "").strip()
        if single:
            out = [single]
    seen, uniq = set(), []
    for x in out:
        if x not in seen:
            seen.add(x)
            uniq.append(x)
    return uniq[:_MAX_QUERIES]


def code_search(args: dict) -> str:
    queries = _parse_queries(args)
    if not queries:
        return "错误：query（检索内容）不能为空"
    root = _norm_root(args.get("root"))
    exts = _exts_of(args.get("exts"))
    try:
        context = max(0, min(int(args.get("context") or 0), 10))
        limit = max(1, min(int(args.get("limit") or 100), 500))
    except ValueError:
        return "错误：context/limit 需为数字"
    flags = 0 if args.get("case_sensitive") else re.IGNORECASE
    include_noise = bool(args.get("include_noise"))
    paths = args.get("paths")

    # 编译全部关键词：某个正则写错只跳过它自己，不影响其它关键词（批量时尤其重要，
    # 否则一个笔误让整批检索白做）
    compiled, bad = [], []
    for q in queries:
        pat_src = q if args.get("regex") is not False else re.escape(q)
        try:
            compiled.append((q, re.compile(pat_src, flags)))
        except re.error as e:
            bad.append(f"⚠ 关键词「{q}」正则无效，已跳过 —— {e}")
    if not compiled:
        return "\n".join(bad)

    multi = len(compiled) > 1
    # 多关键词时 limit 均分：否则第一个关键词吃光额度，后面的白搜。
    per_limit = limit if not multi else max(10, limit // len(compiled))

    # 一次遍历、多模式匹配：N 个关键词的磁盘 IO 只做一遍。
    # 这是批量检索的真收益——若只是「合并成一个调用但内部循环 N 遍」，
    # 省下的只是往返时间，磁盘成本反而翻 N 倍。
    # 引擎选择：rg（Rust 多线程扫描）快 10~50 倍；语法不支持/出错回退 Python。
    # 两引擎产出相同的 hits/totals 结构，下面的格式化完全共用。
    hits: dict = {q: {} for q, _ in compiled}
    totals: dict = {q: 0 for q, _ in compiled}
    if shutil.which("rg"):
        try:
            for q, fp_hits in _rg_hits(
                    root, exts, include_noise, paths,
                    [(q, p) for q, p in compiled], per_limit,
                    bool(args.get("case_sensitive")),
                    args.get("regex") is not False).items():
                n = 0
                for fp_str, lns in fp_hits.items():
                    if n >= per_limit:
                        break
                    take = sorted(lns)[:per_limit - n]
                    if not take:
                        continue
                    n += len(take)
                    try:
                        text, _ = _read_text(Path(fp_str))
                    except OSError:
                        continue
                    hits[q][fp_str] = (text.splitlines(), set(take))
                totals[q] = n
        except (RuntimeError, subprocess.TimeoutExpired, OSError):
            hits, totals = _search_hits_py(root, exts, include_noise, paths,
                                           compiled, per_limit)
    else:
        hits, totals = _search_hits_py(root, exts, include_noise, paths,
                                       compiled, per_limit)

    grand = sum(totals.values())
    if grand == 0 and not multi:
        ext_desc = ("全部代码类型" if exts == set(CODE_EXTS)
                    else ", ".join(sorted(exts)))
        return f"未找到匹配（root={root}，扩展名过滤：{ext_desc}）。"

    parts = []
    if multi:
        parts.append(f"✅ 一次检索 {len(compiled)} 个关键词，共 {grand} 处：")
    else:
        parts.append(f"✅ 匹配 {grand} 处：")
    if bad:
        parts += bad
    for q, _pat in compiled:
        if multi:
            parts.append(f"=== 「{q}」（{totals[q]} 处）")
            if not hits[q]:
                parts.append("  （无匹配）")
                continue
        for fp_str, (lines, lns) in hits[q].items():
            rel = _rel(root, Path(fp_str))
            parts.append(f"=== {rel}（{len(lns)} 处）")
            sorted_hits = sorted(lns)
            intervals = []
            for ln in sorted_hits:
                lo, hi = max(1, ln - context), min(len(lines), ln + context)
                if intervals and ln - intervals[-1][1] <= 2 * context + 1:
                    intervals[-1] = (intervals[-1][0], max(intervals[-1][1], hi))
                else:
                    intervals.append((lo, hi))
            for lo, hi in intervals:
                for ln in range(lo, hi + 1):
                    mark = "▶" if ln in lns else " "
                    parts.append(f"  {mark} {ln:>5}│ {lines[ln - 1]}")
    hint = ("\n提示：找函数/类的定义与引用请用 code_locate；读整段代码请用 code_read。"
            if context == 0 else "")
    return _trim("\n".join(parts) + hint)


def code_list_files(args: dict) -> str:
    root = _norm_root(args.get("root"))
    exts = _exts_of(args.get("exts"))
    include_noise = bool(args.get("include_noise"))
    max_depth = args.get("max_depth")
    try:
        max_depth = int(max_depth) if max_depth else None
        limit = max(1, min(int(args.get("limit") or 300), 2000))
    except ValueError:
        return "错误：max_depth/limit 需为数字"
    files = list(_iter_files(
        root, exts, include_noise, max_depth=max_depth,
        paths=args.get("dirs"), limit=limit))
    if not files:
        return "该目录下没有匹配的代码文件。"
    counts = {}
    for fp in files:
        counts[fp.suffix.lower()] = counts.get(fp.suffix.lower(), 0) + 1
    if args.get("summary_only"):
        lines = [f"共 {len(files)} 个代码文件："]
        lines += [f"  {ext or '(无扩展名)'}：{counts[ext]}"
                  for ext in sorted(counts)]
        return _trim("\n".join(lines))
    lines = [f"共 {len(files)} 个代码文件（限制 {limit}）："]
    lines += [f"  {_rel(root, fp)}" for fp in files]
    return _trim("\n".join(lines))


def code_read(args: dict) -> str:
    files_str = args.get("files")
    if not files_str:
        return ("错误：files 不能为空（逗号/换行分隔；"
                "每项可用 路径、路径:行号、路径:起-止）")
    root = _norm_root(args.get("root"))
    try:
        max_lines = max(10, min(int(args.get("max_lines") or 500), 5000))
    except ValueError:
        return "错误：max_lines 需为数字"
    parts, errors = [], []
    for item in _split_list(files_str):
        path_spec, start, end = item, None, None
        m = re.match(r"^(.*?):(\d+)(?:\s*-\s*(\d+))?$", item)
        if m and ":" in item:
            path_spec = m.group(1)
            start = int(m.group(2))
            end = int(m.group(3) or m.group(2))
        try:
            fp = _resolve(root, path_spec)
        except ValueError as e:
            errors.append(str(e))
            continue
        if not _within(root, fp):
            errors.append(f"越界路径（不在 root={root} 内）：{fp}")
            continue
        if not fp.is_file():
            errors.append(f"文件不存在：{path_spec}")
            continue
        try:
            text, enc = _read_text(fp)
        except OSError as e:
            errors.append(f"读取失败 {path_spec}: {e}")
            continue
        lines = text.splitlines()
        total = len(lines)
        if start is None:
            start, end = 1, min(total, max_lines)
        else:
            start = max(1, min(start, total + 1))
            end = min(max(start, end), total)
        parts.append(f"### {_rel(root, fp)}（共 {total} 行，编码 {enc}，显示 {start}-{end}）")
        width = len(str(end))
        parts += [f"{i:>{width}}│ {lines[i - 1]}" for i in range(start, end + 1)]
        if total > end:
            parts.append(f"（还有 {total - end} 行未显示：可用 {_rel(root, fp)}:{end + 1}-{min(total, end + max_lines)} 继续读）")
    body = "\n".join(parts)
    if errors:
        body += "\n\n⚠ 部分条目未读成功：\n" + "\n".join(f"  - {e}" for e in errors)
    return _trim(body)


# ---------- AST 结构感知（对标 ast-grep：按语法树定位，而非纯文本） ----------
# 升级来源：GitHub 高级玩法调研（ast-grep/comby 结构化搜索、aider 最小 diff 策略）。
# Python 用标准库 ast 做真实定义/引用识别，排除注释与字符串里的同名假命中；
# 语法错误时自动回退正则（至少能给出行号）。零第三方依赖。

def _py_sig(node) -> str:
    """函数签名（参数列表），ast.unparse 失败时降级。"""
    try:
        return "(" + ast.unparse(node.args) + ")"
    except Exception:
        return "(...)"


def _py_ast_defs(text: str):
    """用标准库 ast 提取 Python 定义符号表（函数/类，含签名与行号）。
    语法错误返回 None（调用方回退正则）。"""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return None
    out = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if isinstance(node, ast.ClassDef):
                kind, sig = "class", ""
            elif isinstance(node, ast.AsyncFunctionDef):
                kind, sig = "async def", _py_sig(node)
            else:
                kind, sig = "def", _py_sig(node)
            out.append((node.lineno, kind, node.name, sig))
    return out


def _py_ast_refs(text: str, symbol: str):
    """用 ast 提取 symbol 的真实引用位置（Load 上下文，排除定义/赋值/删除）。
    语法错误返回 None。"""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return None
    lines = text.splitlines()
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id == symbol \
                and isinstance(node.ctx, ast.Load):
            ln = node.lineno
            txt = lines[ln - 1].strip()[:90] if 0 < ln <= len(lines) else ""
            out.append((ln, txt))
    return out


# ---------- 2. 代码分析 ----------

def code_locate(args: dict) -> str:
    symbol = str(args.get("symbol") or "").strip()
    if not symbol or not re.match(r"^[A-Za-z_]\w*$", symbol):
        return "错误：symbol 需为合法标识符（字母/数字/下划线，不能以数字开头）"
    root = _norm_root(args.get("root"))
    kind = str(args.get("kind") or "all").lower()
    try:
        limit = max(1, min(int(args.get("limit") or 80), 400))
    except ValueError:
        return "错误：limit 需为数字"
    exts = _exts_of(args.get("exts"))
    paths = args.get("paths")
    word = r"\b" + re.escape(symbol) + r"\b"
    defs, refs = [], []
    # rg 粗筛候选文件（-l 秒级）→ 只对命中文件 AST 精筛；rg 不可用则全量兜底
    cand = _rg_candidate_files(root, exts, False, paths, symbol)
    if cand is not None and not paths:
        # rg 的 -g include 是 whitelist 语义：会把被 gitignore 排除的根级文件
        # 强制包含进来（如 gene_stats.json），与兜底路径的候选集合不一致。
        # 用 git 白名单求交集，保证 rg/兜底两条路径结果完全等价。
        seen = _git_visible_files(root)
        if seen is not None:
            seen_set = set(seen)
            cand = [c for c in cand if str(Path(c).resolve()) in seen_set]
    if cand is not None:
        iter_files = [Path(c) for c in dict.fromkeys(cand)]
    elif paths:
        # 显式限定子目录（可能点名搜 data/ 等被忽略目录）：尊重意图，全量遍历
        iter_files = _iter_files(root, exts, False, paths=paths)
    else:
        # rg 不可用 + 全仓：git 白名单剪枝（与 rg 主路径同语义，跳过被忽略的业务工作区）
        seen = _git_visible_files(root)
        iter_files = ([Path(p) for p in seen if not _is_binary(Path(p))]
                      if seen is not None
                      else _iter_files(root, exts, False, paths=paths))
    for fp in iter_files:
        try:
            text, _ = _read_text(fp)
        except OSError:
            continue
        suffix = fp.suffix.lower()
        if suffix == ".py":
            # 正则粗筛：文件里没有该符号就直接跳过，省掉 AST parse（命中率极低）
            if not re.search(word, text):
                continue
            # AST 结构感知：真实定义/引用，排除注释与字符串里的同名假命中
            ast_defs = _py_ast_defs(text)
            if ast_defs is not None:
                for ln, dkind, name, _sig in ast_defs:
                    if name == symbol:
                        defs.append((fp, ln, f"{dkind} {name}"))
                ast_refs = _py_ast_refs(text, symbol)
                for ln, txt in ast_refs:
                    if not any(dl == ln for _fp, dl, _t in defs if _fp == fp):
                        refs.append((fp, ln, txt))
                continue
            # 语法错误：回退正则（至少能给出行号）
        for i, line in enumerate(text.splitlines(), 1):
            if not re.search(word, line):
                continue
            is_def = False
            for p in DEF_PATTERNS.get(suffix, []):
                m = re.match(p, line)
                if m and m.group(1) == symbol:
                    is_def = True
                    break
            (defs if is_def else refs).append((fp, i, line.strip()))
    defs.sort(key=lambda x: (str(x[0]), x[1]))
    refs.sort(key=lambda x: (str(x[0]), x[1]))
    def_lines = {(fp, i) for fp, i, _ in defs}
    refs_only = [(fp, i, t) for fp, i, t in refs if (fp, i) not in def_lines]
    out = [f"符号 {symbol} 的定位结果："]
    if defs:
        out.append(f"定义（{len(defs)} 处）：")
        out += [f"  {_rel(root, fp)}:{i}  {t[:90]}" for fp, i, t in defs]
    if kind in ("all", "ref") and refs_only:
        shown = refs_only[:limit]
        out.append(f"引用/其余出现（显示 {len(shown)}/{len(refs_only)} 处）：")
        out += [f"  {_rel(root, fp)}:{i}  {t[:90]}" for fp, i, t in shown]
    if kind == "def" and not defs:
        out.append("  未找到定义。")
    if kind in ("all", "ref") and not defs and not refs_only:
        out.append("  项目中未找到该符号。")
    return _trim("\n".join(out))


def _analyze_file(fp: Path, root: Path) -> str:
    try:
        text, enc = _read_text(fp)
    except OSError as e:
        return f"### {_rel(root, fp)}\n读取失败：{e}"
    lines = text.splitlines()
    rel = _rel(root, fp)
    imports, defs, todos = [], [], []
    max_indent = 0
    suffix = fp.suffix.lower()
    ast_complexity = None
    if suffix == ".py":
        # AST 结构感知：真实定义（含签名）+ 圈复杂度，对标 ast-grep outline
        ast_defs = _py_ast_defs(text)
        if ast_defs is not None:
            defs = [(ln, f"{kind} {name}{sig}") for ln, kind, name, sig in ast_defs]
            try:
                tree = ast.parse(text)
                cyc = 1
                for node in ast.walk(tree):
                    if isinstance(node, (ast.If, ast.For, ast.While,
                                        ast.ExceptHandler, ast.With,
                                        ast.Assert, ast.BoolOp)):
                        cyc += 1
                ast_complexity = cyc
            except SyntaxError:
                pass
    for i, line in enumerate(lines, 1):
        s = line.strip()
        if not s:
            continue
        if suffix == ".py":
            m = re.match(r"^\s*(?:import|from)\s+([\w.]+)", line)
            if m:
                imports.append((i, m.group(1)))
        elif suffix in _JS_EXTS:
            for m in re.finditer(r"(?:require\s*\(\s*|from\s+)(['\"])([^'\"]+)\1", line):
                imports.append((i, m.group(2)))
        if ast_complexity is None:
            for p in DEF_PATTERNS.get(suffix, []):
                m = re.match(p, line)
                if m:
                    defs.append((i, m.group(1)))
                    break
        indent = len(line) - len(line.lstrip())
        max_indent = max(max_indent, indent)
        if _has_todo(s):
            todos.append((i, s[:80]))
    out = [f"### {rel}（{len(lines)} 行，编码 {enc}）"]
    if imports:
        shown = "、".join(f"{i}:{imp}" for i, imp in imports[:20])
        out.append(f"import/依赖（{len(imports)} 条，显示前 20）：{shown}")
    if defs:
        shown = "、".join(f"{i}:{name}" for i, name in defs[:30])
        out.append(f"定义（{len(defs)} 个）：{shown}")
    if todos:
        out.append("⚠ TODO/FIXME：")
        out += [f"  {i}: {t}" for i, t in todos[:10]]
    n_funcs = sum(1 for _, name in defs if name)
    blank = sum(1 for ln in lines if not ln.strip())
    avg_len = sum(len(l) for l in lines) // max(len(lines), 1)
    cyc_txt = f"；圈复杂度约 {ast_complexity}" if ast_complexity else ""
    out.append(
        f"结构提示：定义数 {n_funcs}；最大缩进深度约 {max_indent // 4} 层"
        f"（缩进 {max_indent} 空格）；空行占比 {blank / max(len(lines), 1):.0%}；"
        f"平均行长 {avg_len} 字符{cyc_txt}"
    )
    if len(lines) > 800:
        out.append(f"⚠ 文件超过 800 行（{len(lines)}），建议评估拆分。")
    return "\n".join(out)


def _analyze_dir(d: Path, root: Path) -> str:
    files = list(_iter_files(root, set(CODE_EXTS), False, paths=[str(d)]))
    if not files:
        return f"目录 {d} 下没有代码文件。"
    by_ext, loc_of, todos = {}, {}, 0
    for fp in files:
        try:
            text, _ = _read_text(fp)
        except OSError:
            continue
        n = text.count("\n") + 1
        loc_of[fp] = n
        by_ext[fp.suffix.lower()] = by_ext.get(fp.suffix.lower(), 0) + 1
        todos += _count_todos(text)
    sizes = sorted(loc_of.items(), key=lambda kv: kv[1], reverse=True)
    rel = _rel(root, d)
    out = [
        f"目录结构分析：{rel or '.'}",
        f"代码文件 {len(files)} 个，总行数约 {sum(loc_of.values())}，"
        f"TODO/FIXME 标记 {todos} 处",
        "扩展名分布：",
    ]
    out += [f"  {ext or '(无扩展名)'}：{by_ext[ext]}"
            for ext in sorted(by_ext)]
    out.append("最大文件 Top5：")
    out += [f"  {n} 行  {_rel(root, fp)}" for fp, n in sizes[:5]]
    big = [f"  {n} 行  {_rel(root, fp)}" for fp, n in sizes if n > 800]
    if big:
        out.append(f"⚠ 超过 800 行的大文件（{len(big)} 个，建议拆分）：")
        out += big[:10]
    return "\n".join(out)


def code_analyze(args: dict) -> str:
    root = _norm_root(args.get("root"))
    files = _split_list(args.get("files"))
    if files:
        targets = []
        for f in files:
            fp = _resolve(root, f)
            if not _within(root, fp):
                return f"⛔ 越界路径（不在 root={root} 内）：{fp}"
            if fp.is_file():
                targets.append(fp)
            elif fp.is_dir():
                targets.extend(_iter_files(
                    root, set(CODE_EXTS), False, paths=[str(fp)]))
            else:
                return f"文件不存在：{f}"
        if not targets:
            return "指定位置没有可分析的代码文件。"
        return _trim("\n\n".join(_analyze_file(fp, root) for fp in targets))
    d = Path(args.get("dir")).resolve() if args.get("dir") else root
    if not _within(root, d):
        return f"⛔ 越界路径（不在 root={root} 内）：{d}"
    return _trim(_analyze_dir(d, root))


def _py_module_candidates(imp: str, root: Path, from_dir: Path):
    imp = imp.strip()
    if not imp:
        return []
    if imp.startswith("."):
        parts = imp.split(".")
        dots = len(parts) - 1
        mod = ".".join(parts[1:])
        base = from_dir
        for _ in range(dots - 1):
            base = base.parent
    else:
        base = root
        mod = imp
    mod_parts = [p for p in mod.split(".") if p]
    if not mod_parts:
        return []
    parent = base / Path(*mod_parts[:-1]) if len(mod_parts) > 1 else base
    cands = [
        parent / (mod_parts[-1] + ".py"),
        parent / mod_parts[-1] / "__init__.py",
    ]
    return [c for c in cands if c.is_file()]


def _js_resolve(imp: str, from_dir: Path, root: Path):
    imp = imp.strip().strip("'\"")
    if not imp.startswith("."):
        return None
    base = (from_dir / imp).resolve()
    cands = []
    for ext in (".js", ".ts", ".jsx", ".tsx", ".mjs", ".cjs"):
        cands.append(Path(str(base) + ext))
        cands.append(base / f"index{ext}")
    hits = [c for c in cands if c.is_file()]
    return hits[0] if hits else None


def _imports_of(fp: Path, text=None):
    """取一个文件的 import 列表。

    text 可传入以免重复读盘：code_map 要在**一次遍历**里同时算行数、TODO、
    入口证据、复杂函数、依赖图，再各自读一遍文件就是 5 倍 IO。
    """
    if text is None:
        try:
            text, _ = _read_text(fp)
        except OSError:
            return []
    out = []
    suffix = fp.suffix.lower()
    if suffix == ".py":
        for m in re.finditer(r"^\s*(?:import|from)\s+([\w.]+)", text, re.M):
            out.append(m.group(1))
        for m in re.finditer(r"^\s*from\s+(\.[\w.]+)\s+import", text, re.M):
            out.append(m.group(1))
    elif suffix in _JS_EXTS:
        for m in re.finditer(r"(?:require\s*\(\s*|from\s+)(['\"])([^'\"]+)\1", text):
            out.append(m.group(2))
    return out


def _find_cycle(edges: dict):
    visited = set()

    def dfs(u, path):
        if u in path:
            i = path.index(u)
            return path[i:] + [u]
        if u in visited:
            return None
        visited.add(u)
        for v in sorted(edges.get(u, ())):
            if v in edges:
                r = dfs(v, path + [u])
                if r:
                    return r
        return None

    for u in edges:
        r = dfs(u, [])
        if r:
            return r
    return None


def _dep_map_of(root: Path, targets, texts=None) -> dict:
    """构建 {相对路径: [被依赖的相对路径]}。

    抽成独立函数的原因：code_deps（看单个文件的依赖）与 code_map（找枢纽文件）
    需要**完全一致**的依赖图。两份实现迟早漂移，届时「deps 说 A 被依赖、map
    说不是」这种矛盾最难查。
    """
    dep_map = {}
    for fp in targets:
        rel = _rel(root, fp)
        text = (texts or {}).get(str(fp))
        deps = []
        for imp in _imports_of(fp, text):
            if fp.suffix.lower() == ".py":
                resolved = _py_module_candidates(imp, root, fp.parent)
            elif fp.suffix.lower() in _JS_EXTS:
                r = _js_resolve(imp, fp.parent, root)
                resolved = [r] if r else []
            else:
                resolved = []
            deps += [_rel(root, c) for c in resolved]
        dep_map[rel] = sorted(set(deps))
    return dep_map


def _incoming_of(dep_map: dict) -> dict:
    """入度：每个文件被多少个文件依赖。这是「哪个文件是关键」最硬的度量。"""
    inc = {}
    for deps in dep_map.values():
        for d in deps:
            inc.setdefault(d, 0)
            inc[d] += 1
    return inc


def code_deps(args: dict) -> str:
    root = _norm_root(args.get("root"))
    files = _split_list(args.get("files"))
    try:
        limit = max(1, min(int(args.get("limit") or 60), 300))
    except ValueError:
        return "错误：limit 需为数字"
    if files:
        targets = []
        for f in files:
            fp = _resolve(root, f)
            if not _within(root, fp):
                return f"⛔ 越界路径：{fp}"
            if fp.is_file():
                targets.append(fp)
            else:
                return f"文件不存在：{f}"
    else:
        targets = list(_iter_files(root, set(CODE_EXTS) & (
            {".py", ".js", ".ts", ".jsx", ".tsx", ".mjs", ".cjs"}), False))
    if not targets:
        return "没有可分析的代码文件。"
    dep_map = _dep_map_of(root, targets)
    out = [f"依赖分析（共 {len(targets)} 个文件，显示前 {limit} 个）："]
    shown = 0
    for rel in sorted(dep_map):
        if shown >= limit:
            out.append(f"…（还有 {len(dep_map) - shown} 个文件未显示）")
            break
        deps = dep_map[rel]
        if deps:
            out.append(f"  {rel}  →  {', '.join(deps)}")
        else:
            out.append(f"  {rel}  （无项目内依赖）")
        shown += 1
    incoming = _incoming_of(dep_map)
    # 枢纽排名：这是「哪个文件是关键」最硬的度量，也是 code_map 的核心维度。
    # 原来算出来了却只用它筛孤立文件，排名直接丢掉——数据已在手，白扔了。
    hubs = sorted(((n, rel) for rel, n in incoming.items()
                   if n >= 2 and rel in dep_map), key=lambda kv: (-kv[0], kv[1]))[:10]
    if hubs:
        out.append(f"枢纽文件（被依赖最多 = 改动影响面最大，Top {len(hubs)}）：")
        out += [f"  {n} 个文件依赖  {rel}" for n, rel in hubs]
    orphans = [rel for rel in dep_map if rel not in incoming]
    if orphans:
        out.append(f"孤立文件（没有被任何文件引用，{len(orphans)} 个）：")
        out += [f"  {rel}" for rel in orphans[:20]]
    cycle = _find_cycle({rel: set(deps) for rel, deps in dep_map.items()})
    if cycle:
        out.append("⚠ 检测到循环依赖：")
        out.append("  " + " → ".join(cycle))
    else:
        out.append("循环依赖：未检测到。")
    return _trim("\n".join(out))


# ---------- 2b. 项目全貌（把「读什么」从猜测变成排名） ----------

# 入口点的文件名证据：程序通常从这些名字开始
_ENTRY_STEMS = {
    "main", "__main__", "app", "server", "manage", "cli", "index", "run",
    "start", "launch", "entry", "bootstrap", "wsgi", "asgi", "application",
}

# 复杂函数阈值：40 行的函数整读也不贵，1200 行的才要命
_MAP_FUNC_MIN = 60


def _entry_points(root: Path, hits, cap: int = 10):
    """把 package.json 声明与遍历得到的入口证据合并排序。

    为什么值得单独做：读陌生项目从入口顺调用链走一遍，比随机读五个文件
    理解得多。但入口常常不叫 main.py（dabai 的入口就是 agent.py），所以
    必须多证据；package.json 的 main/bin 是 JS 项目里最权威的声明。
    """
    declared = []
    pkg = root / "package.json"
    if pkg.is_file():
        try:
            data = json.loads(pkg.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            data = {}
        cands = []
        if isinstance(data.get("main"), str):
            cands.append(data["main"])
        b = data.get("bin")
        if isinstance(b, str):
            cands.append(b)
        elif isinstance(b, dict):
            cands += [v for v in b.values() if isinstance(v, str)]
        scripts = data.get("scripts")
        if isinstance(scripts, dict):
            for v in scripts.values():
                if isinstance(v, str):
                    cands += re.findall(r"[\w./\\-]+\.(?:js|ts|mjs|cjs)", v)
        for c in cands:
            p = (root / c).resolve()
            if p.is_file():
                declared.append(_rel(root, p))

    out, seen = [], set()
    for rel in declared:
        if rel not in seen:
            seen.add(rel)
            out.append((rel, "package.json 声明"))
    for rel, why in hits:
        if rel not in seen:
            seen.add(rel)
            out.append((rel, why))
    # 有 __main__ 块的最可信，排前面
    out.sort(key=lambda kv: (0 if "__main__" in kv[1] else 1))
    return out[:cap]


def _git_churn(root: Path, commits: int, cap: int = 8):
    """改动热点：近 N 次提交里被改得最频繁的文件。

    为什么看历史而不是只看结构：结构告诉你「什么被依赖」，历史告诉你
    「什么在动」。改得最勤的文件是 bug 高发区，也最该先读懂。
    返回 None 表示不是 git 仓库/git 不可用。
    """
    r, _err = _git_run(root, ["log", f"-n{commits}", "--numstat",
                              "--format=@@%h", "--no-renames"], timeout=90)
    if r is None or r.returncode != 0:
        return None
    counts, adds, dels = {}, {}, {}
    cur = None
    for line in (r.stdout or "").splitlines():
        if line.startswith("@@"):
            cur = line[2:].strip()
            continue
        parts = line.split("\t")
        if len(parts) != 3 or not cur:
            continue
        a, d, path = parts[0].strip(), parts[1].strip(), parts[2].strip()
        if not path:
            continue
        counts[path] = counts.get(path, 0) + 1
        for box, val in ((adds, a), (dels, d)):
            try:
                box[path] = box.get(path, 0) + int(val)
            except ValueError:
                pass          # 二进制文件是 "-"
    rows = []
    for path, n in counts.items():
        if not (root / path).is_file():
            continue          # 已删除/改名的文件不再提示
        rows.append((n, path, adds.get(path, 0), dels.get(path, 0)))
    rows.sort(key=lambda r: (-r[0], r[1]))
    return rows[:cap]


def code_map(args: dict) -> str:
    """项目全貌：一次调用拿到「该读哪几个文件」的排名。

    为什么需要它：摸清一个大项目，原来要散着调 5~6 次工具（list_files →
    analyze → deps → 再凭感觉挑文件读），而「挑」这一步最贵——猜错一个
    1200 行的文件，定点读要烧掉 8 次调用。本工具的唯一目标：把「读什么」
    从猜测变成排名。五个维度各有硬证据，都不靠感觉：
      · 入口点   ：文件名 + `__main__` 块 + package.json（程序从这里开始）
      · 枢纽文件 ：入度（被多少文件 import）—— 改动影响面最硬的度量
      · 复杂函数 ：函数行数 —— 最该被拆、也最该被定点读的地方
      · 改动热点 ：git 提交频次 —— 历史比结构更会说话，改得勤 = bug 高发
      · TODO 热点：作者自己标记的问题

    可用 paths 限定子目录（如 paths="src"），大型仓库里把第三方代码排在外面，
    排名才不会被 vendor 目录淹没。
    """
    root = _norm_root(args.get("root"))
    try:
        top = max(1, min(int(args.get("top") or 8), 20))
        commits = max(10, min(int(args.get("commits") or 300), 2000))
    except ValueError:
        return "错误：top/commits 需为数字"

    files = list(_iter_files(root, set(CODE_EXTS), False,
                             paths=_norm_paths(args.get("paths")), limit=2000))
    if not files:
        return f"目录 {root} 下没有代码文件。"

    # ---- 单次遍历：所有维度共用这一次读取（大项目上读取就是最贵的开销）----
    loc_rel, todo_rel, texts = {}, {}, {}
    entry_hits, funcs, by_ext = [], [], {}
    for fp in files:
        by_ext[fp.suffix.lower()] = by_ext.get(fp.suffix.lower(), 0) + 1
        try:
            text, _ = _read_text(fp)
        except OSError:
            continue
        texts[str(fp)] = text
        rel = _rel(root, fp)
        loc_rel[rel] = text.count("\n") + 1
        n_todo = 0 if fp.suffix.lower() in _TODO_SKIP_EXTS else _count_todos(text)
        if n_todo:
            todo_rel[rel] = n_todo
        why = []
        if fp.stem.lower() in _ENTRY_STEMS:
            why.append("文件名")
        if fp.suffix.lower() == ".py":
            if "__main__" in text and re.search(r"^\s*if\s+__name__\s*==", text, re.M):
                why.append("__main__ 块")
        elif fp.suffix.lower() in _JS_EXTS:
            if "listen(" in text or "createServer(" in text:
                why.append("启动代码")
        if why:
            entry_hits.append((rel, " + ".join(why)))
        if fp.suffix.lower() == ".py":
            tree = _parse_py(text, fp)
            if tree is not None:
                for node in ast.walk(tree):
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        span = (node.end_lineno or node.lineno) - node.lineno + 1
                        if span >= _MAP_FUNC_MIN:
                            funcs.append((span, rel, node.name, node.lineno))

    out = [f"项目全貌：{root}",
           f"代码文件 {len(loc_rel)} 个，约 {sum(loc_rel.values())} 行；"
           "扩展名：" + "、".join(f"{e or '(无)'}×{n}" for e, n in
                                  sorted(by_ext.items(), key=lambda kv: -kv[1])[:6])]

    entries = _entry_points(root, entry_hits, cap=top)
    out.append("")
    out.append("【入口点】读代码从这里开始，顺着调用链走一遍最省")
    if entries:
        out += [f"  {rel}  ← {why}" for rel, why in entries]
    else:
        out.append("  （未识别到明显入口，建议先看文件名与目录结构）")

    dep_map = _dep_map_of(root, files, texts)
    incoming = _incoming_of(dep_map)
    hubs = sorted(((n, rel) for rel, n in incoming.items()
                   if n >= 2 and rel in dep_map), key=lambda kv: (-kv[0], kv[1]))[:top]
    out.append("")
    out.append("【枢纽文件】被 import 最多 = 改动影响面最大，先读懂它们")
    if hubs:
        for i, (n, rel) in enumerate(hubs, 1):
            out.append(f"  {i}. {rel}  被 {n} 个文件依赖，{loc_rel.get(rel, 0)} 行")
    else:
        out.append("  （没有文件被 2 个以上文件依赖，项目可能是扁平脚本集合）")

    funcs.sort(key=lambda r: (-r[0], r[1]))
    out.append("")
    out.append(f"【复杂函数 Top {top}】≥{_MAP_FUNC_MIN} 行：最该定点读，别整读")
    if funcs:
        for i, (span, rel, name, ln) in enumerate(funcs[:top], 1):
            out.append(f"  {i}. {rel}:{ln}  {name}  {span} 行")
    else:
        out.append(f"  （没有超过 {_MAP_FUNC_MIN} 行的函数，代码规模健康）")

    churn = _git_churn(root, commits, top)
    out.append("")
    if churn is None:
        out.append("【改动热点】（不是 git 仓库或 git 不可用，跳过）")
    else:
        out.append(f"【改动热点】近 {commits} 次提交：改得最勤 = bug 高发区")
        if churn:
            for i, (n, path, a, d) in enumerate(churn, 1):
                out.append(f"  {i}. {path}  {n} 次改动  +{a}/-{d}")
        else:
            out.append("  （该范围内没有文件改动记录）")

    todo_rows = sorted(todo_rel.items(), key=lambda kv: (-kv[1], kv[0]))[:top]
    out.append("")
    out.append("【TODO 热点】作者自己标记的待办/隐患")
    if todo_rows:
        out += [f"  {n} 处  {rel}" for rel, n in todo_rows]
    else:
        out.append("  （没有 TODO/FIXME 标记）")

    out.append("")
    out.append("【下一步】对枢纽文件用 symbols 看结构 → 只定点读要改的区间；"
               "入口点用来顺调用链。不要凭感觉挑文件整读。")
    return _trim("\n".join(out))


# ---------- 3. 代码修改 ----------

# ---------- 批量编辑的支撑函数 ----------


BACKUP_KEEP = 1


def _prune_backups(fp: Path, keep: int = BACKUP_KEEP) -> int:
    """同一文件的 .bak-<时间戳> 只留最近 keep 份，返回删掉的份数。

    备份只解决「刚改坏、还没提交，要撤回上一次」这一种情况，一份就够；
    真正的回滚网是 git。留 3 份的代价实测是 143 个散落在 24 个源码目录里的
    全量副本（都 gitignore、git 看不见，扫目录时极易当成源码）。
    只匹配 .bak-<纯数字>，引擎自定义的 .bak-r33 / .pre-apply-* 一律不碰。
    """
    pat = re.compile(re.escape(fp.name) + r"\.bak-(\d+)$")
    cands = []
    try:
        for p in fp.parent.iterdir():
            m = pat.match(p.name)
            if m:
                cands.append((int(m.group(1)), p))
    except OSError:
        return 0
    cands.sort(reverse=True)
    removed = 0
    for _, p in cands[keep:]:
        try:
            p.unlink()
            removed += 1
        except OSError:
            pass
    return removed



def _norm_edit(d: dict) -> dict:
    """把一处编辑的参数收口成统一结构（含常见别名识别）。

    独立出来的原因：edits 数组里每一项都要走同一套解析与别名容错，
    逻辑重复两遍迟早会漂移成两套标准（模型今天写 old、明天写 find，
    两处判断不一致就会出现「单改能用、批改报错」这种最难查的 bug）。
    """
    return {
        "mode": str(d.get("mode") or "replace").lower(),
        "old": str(d.get("old") or d.get("old_text") or d.get("old_string")
                   or d.get("find") or d.get("target") or d.get("anchor") or ""),
        "new": str(d.get("new") or d.get("new_text") or d.get("new_string")
                   or d.get("replacement") or d.get("content") or ""),
        "replace_all": bool(d.get("replace_all")),
        "line_start": d.get("line_start"),
        "line_end": d.get("line_end"),
        "position": str(d.get("position") or "after").lower(),
    }


def _edit_sort_key(e: dict):
    """批量编辑的排序键：行号类排前面（按行号降序），文本类排后面（保持原顺序）。

    这是 edits 数组唯一的正确性关键，也是「从后往前改」那条纪律的实现：
      · 行号类必须**从后往前**应用。先改第 10 行会让第 20 行的行号错位，
        后面所有按行号定位的编辑都会改到错误位置——而且**不会报错**
        （行号依然合法、依然有内容可改），只会静默改错。所以排序不能省。
      · 行号类要整体排在文本类之前。文本替换可能增删行，一旦先做，
        所有行号就全部作废。
      · 文本类之间用稳定排序保持原顺序（模型给的顺序通常有语义）。
    """
    ls = e.get("line_start")
    if ls is None:
        return (1, 0)
    try:
        return (0, -int(ls))
    except (TypeError, ValueError):
        return (1, 0)


def _edit_text_once(norm: str, e: dict, fp: Path, file_s: str):
    """在 norm 上执行一次编辑，返回 (新文本, 说明列表)；新文本为 None 表示这次没改动。

    抽成独立函数的理由：单处编辑与批量编辑必须共用同一套逻辑（AST 结构化定位、
    模糊纠偏、行号模式、自纠错提示）。批量时每次都用**上一次的结果**作为输入，
    所以后面的编辑看到的是最新文本——定位总是基于真实内容重算，不会因为
    前面的改动而失效。
    """
    notes = []
    lines = norm.split("\n")
    mode, old, new = e["mode"], e["old"], e["new"]

    # ---- 行号模式（Claude Code 风格）：line_start/line_end 直接按行号编辑 ----
    if e["line_start"] is not None:
        try:
            ls = int(e["line_start"])
            le = int(e["line_end"]) if e["line_end"] is not None else ls
        except (TypeError, ValueError):
            return None, [f"错误：line_start/line_end 需为数字（{file_s}）"]
        if ls < 1 or le < ls or le > len(lines):
            return None, [f"错误：行号越界（{file_s}，共 {len(lines)} 行，请求 {ls}-{le}）"]
        if mode == "insert":
            idx = le if e["position"] == "after" else ls - 1
            anchor_line = lines[idx] if idx < len(lines) else ""
            ins = _inherit_indent(anchor_line, new).split("\n")
            lines[idx:idx] = ins
        else:
            lines[ls - 1:le] = new.split("\n") if new else []
        return "\n".join(lines), notes

    # ---- insert 模式：锚点行前后插入 ----
    if mode == "insert":
        anchor = old.strip()
        if not anchor:
            return None, ["insert 模式需要 anchor（插入锚点文本）"]
        hit_idx = [i for i, l in enumerate(lines) if anchor in l]
        if not hit_idx:
            # 模糊容错：锚点没逐字符命中时，找最相近的行
            fuzzy = _fuzzy_locate(norm, anchor)
            if fuzzy:
                hit_idx = [fuzzy[0] - 1]
                notes.append(f"（{file_s} 锚点未逐字符命中，已按相似行 {fuzzy[0]} 自动纠偏）")
            else:
                hint = _near_miss_hint(lines, anchor)
                return None, ["未找到锚点文本，未做任何修改。（锚点需与文件内容逐字符一致）"
                              + (("\n" + hint) if hint else "")]
        if len(hit_idx) > 1:
            pos_str = "、".join(str(i + 1) for i in hit_idx[:10])
            return None, [f"⚠ 锚点出现 {len(hit_idx)} 次（第 {pos_str} 行），不唯一，"
                          "未修改。请给出更长/更唯一的锚点。"]
        idx = hit_idx[0]
        ins = _inherit_indent(lines[idx], new).split("\n")
        if e["position"] == "before":
            lines[idx:idx] = ins
        else:
            lines[idx + 1:idx + 1] = ins
        return "\n".join(lines), notes

    # ---- replace 模式：原文替换（含 AST 结构化定位 + 模糊容错）----
    if not old.strip():
        # 自纠错：缺 old 时给出可直接复制的原文片段 + 正确用法，
        # 让模型下一轮一次改对，而不是反复报错翻车
        excerpt = "\n".join(norm.split("\n")[:8])
        return None, [
            "replace 模式需要 old（要被替换的原文），本次未做任何修改。\n"
            "正确做法：先用 code_read 读出目标片段，把要替换的原文"
            "逐字复制进 old 参数（注意缩进/引号/换行，必须与文件一致）；\n"
            "或改用 mode=insert + anchor=锚点行 + position=after/before 插入新内容。\n"
            "文件开头几行供参考：\n" + excerpt
        ]
    count = norm.count(old)
    if count == 0:
        # ① AST 结构化定位（.py）：排除注释/字符串假命中
        ast_span = None
        if fp.suffix.lower() == ".py":
            ast_span = _ast_locate_node(norm, old)
        if ast_span:
            ls, le = ast_span
            lines[ls - 1:le] = new.split("\n") if new else []
            notes.append(f"（{file_s} 文本未逐字符命中，已按 AST 节点 {ls}-{le} 行结构化替换）")
            return "\n".join(lines), notes
        # ② 模糊容错：找最相近的连续行片段
        fuzzy = _fuzzy_locate(norm, old)
        if fuzzy:
            ls, le = fuzzy
            lines[ls - 1:le] = new.split("\n") if new else []
            notes.append(f"（{file_s} 文本未逐字符命中，已按相似行 {ls}-{le} 自动纠偏）")
            return "\n".join(lines), notes
        hint = _near_miss_hint(lines, old)
        return None, ["未找到要替换的原文（出现 0 次），未做任何修改。"
                      "原文需与文件内容逐字符一致（注意缩进/引号/换行）。"
                      + (("\n" + hint) if hint else "")]
    if count > 1 and not e["replace_all"]:
        pos_str = "、".join(str(i + 1) for i in
                            [lines.index(l) + 1 for l in lines if old in l][:10])
        return None, [f"⚠ 原文出现 {count} 次（第 {pos_str} 行），锚点不唯一，未修改。"
                      "请补上更多上下文使锚点唯一，或传 replace_all=true 全部替换。"]
    if e["replace_all"]:
        return norm.replace(old, new), notes
    return norm.replace(old, new, 1), notes


def code_edit(args: dict) -> str:
    """精准修改文件（v3.0 升级：对标 ast-grep / comby / Claude Code）。

    新增能力：
    - AST 结构化定位：.py 文件优先用 ast 找真实代码节点，注释/字符串里的假命中自动排除
    - 模糊容错：锚点差一两个字符时自动纠偏（comby 风格），不再直接失败
    - 行号模式：line_start/line_end 按行号精准编辑（Claude Code 风格）
    - 多文件批量：files 参数一次改多个文件（codemod 风格）
    """
    root = _norm_root(args.get("root"))
    files = args.get("files") or args.get("file")
    if not files:
        return "错误：file（或 files）不能为空"
    file_list = _split_list(files) if isinstance(files, str) else list(files)
    if not file_list:
        return "错误：file（或 files）不能为空"
    mode = str(args.get("mode") or "replace").lower()
    # 参数别名收口（模型常把 old 写成 old_text/old_string/find/target/anchor，
    # 把 new 写成 new_text/new_string/replacement/content——统一识别，不再翻车）
    old = (str(args.get("old") or args.get("old_text") or args.get("old_string")
               or args.get("find") or args.get("target") or args.get("anchor") or ""))
    new = (str(args.get("new") or args.get("new_text") or args.get("new_string")
               or args.get("replacement") or args.get("content") or ""))
    preview = bool(args.get("preview"))
    replace_all = bool(args.get("replace_all"))
    line_start = args.get("line_start")
    line_end = args.get("line_end")
    results = []
    for file_s in file_list:
        try:
            fp = _resolve(root, file_s)
        except ValueError as e:
            results.append(f"错误：{e}")
            continue
        if not _within(root, fp):
            results.append(f"⛔ 越界路径（不在 root={root} 内）：{fp}")
            continue
        if not fp.is_file():
            results.append(f"文件不存在：{fp}")
            continue
        core_note = ("\n⚠ 这是大白核心文件，修改后会触发整进程自动重启生效。"
                     if _is_core(root, fp) else "")
        try:
            text, enc = _read_text(fp)
        except OSError as e:
            results.append(f"读取失败：{e}")
            continue
        norm = text.replace("\r\n", "\n")
        # diff 的基线必须单独留一份：批量编辑会就地推进 norm（每处编辑都用上一次
        # 的结果作为输入），若直接拿 norm 当基线，前后文本相同 → diff 恒为空，
        # 用户看不到究竟改了什么。
        orig_norm = norm
        # ---- 应用编辑：单处（顶层参数）或批量（edits 数组）----
        # 批量编辑的价值：改长文件 N 处 = 原来 N 次调用往返（每次都要等一次
        # LLM 生成），现在 1 次。这是「修改长文件」最大的一笔成本。
        edit_list = args.get("edits")
        # 用 is not None 而不是真值判断：edits=[] 是模型可能给的「空批次」，
        # 必须明确报错。若用真值判断会掉进单处路径，返回「replace 模式需要 old」——
        # 那个提示对模型是误导（它明明传的是 edits）。
        if edit_list is not None:
            if not isinstance(edit_list, list):
                results.append(f"错误：edits 需为数组（{file_s}）")
                continue
            edits = [_norm_edit(d) for d in edit_list if isinstance(d, dict)]
            if not edits:
                results.append(f"错误：edits 为空或没有有效的编辑项（{file_s}）："
                               "每项需为对象，且含 old/new 或 line_start")
                continue
            edits.sort(key=_edit_sort_key)
            failed = False
            for i, e in enumerate(edits, 1):
                new_norm, notes = _edit_text_once(norm, e, fp, file_s)
                if new_norm is None:
                    results.append(f"✘ {file_s}：第 {i}/{len(edits)} 处编辑未应用 → "
                                   + "\n".join(notes))
                    failed = True
                    break
                norm = new_norm
                results += notes
            if failed:
                # 关键：一处失败就整批不写。半途写入会造成「以为全改了、其实只改了前几处」
                # 的假象——这种静默的部分成功，比直接报错难查得多。
                results.append("（已应用的部分也**未写入文件**，避免「只改了一部分」的假象。"
                               "修正失败项后重试，或把 edits 拆成多次调用。）")
                continue
            changed_norm = norm
        else:
            new_norm, notes = _edit_text_once(norm, _norm_edit(args), fp, file_s)
            if new_norm is None:
                results += notes
                continue
            changed_norm = new_norm
            results += notes
        diff = "\n".join(difflib.unified_diff(
            orig_norm.split("\n"), changed_norm.split("\n"),
            fromfile="旧", tofile="新", lineterm=""))
        if len(diff) > 8000:
            diff = diff[:8000] + "\n…（diff 已截断，改动已按锚点完成；需要完整 diff 可查看备份文件）"
        if preview:
            results.append(f"🔍 预览模式（未写入文件）：{_rel(root, fp)}\n" + diff)
            continue
        nl = "\r\n" if "\r\n" in text else "\n"
        out = nl.join(changed_norm.split("\n"))
        if not out.endswith(nl):
            out += nl
        bak = fp.with_name(fp.name + f".bak-{int(time.time())}")
        try:
            bak.write_bytes(fp.read_bytes())
            _prune_backups(fp)
        except OSError as e:
            results.append(f"备份失败（未修改文件）：{e}")
            continue
        try:
            fp.write_bytes(out.encode(enc if enc != "utf-8-sig" else "utf-8-sig"))
        except UnicodeEncodeError:
            fp.write_bytes(out.encode("utf-8"))
            enc = "utf-8"
        except OSError as e:
            results.append(f"写入失败（已留备份 {bak.name}）：{e}")
            continue
        results.append(f"✅ 已修改：{_rel(root, fp)}（编码 {enc}）\n"
                       f"备份：{bak.name}（确认无误后可删除）\n"
                       "diff 预览：\n" + diff + core_note)
    return "\n\n".join(results)


# ---------- 4. git 感知与补丁 ----------

def _git_run(root: Path, args_list, timeout: int = 60):
    cmd = ["git", "-c", "core.quotepath=false", "-c", "color.ui=false"]
    cmd += list(args_list)
    try:
        r = subprocess.run(
            cmd, capture_output=True, encoding="utf-8", errors="replace",
            timeout=timeout, cwd=str(root), creationflags=_CREATE_NO_WINDOW)
        return r, ""
    except FileNotFoundError:
        return None, "未找到 git 命令，请先安装 Git。"
    except subprocess.TimeoutExpired:
        return None, f"git 命令超时（>{timeout}s）已终止"


def _git_err(r, err: str) -> str:
    if err:
        return err
    return (r.stderr or r.stdout or "").strip() or "未知错误"


def _require_git_repo(root: Path):
    r, err = _git_run(root, ["rev-parse", "--is-inside-work-tree"])
    if r is None or r.returncode != 0:
        return f"⚠ 该目录不是 git 仓库（root={root}）。{_git_err(r, err)}"
    return None


def code_git_status(args: dict) -> str:
    root = _norm_root(args.get("root"))
    bad = _require_git_repo(root)
    if bad:
        return bad
    short = args.get("short") is not False
    args_list = ["status", "--porcelain=v1", "--branch"] if short else ["status"]
    r, err = _git_run(root, args_list)
    if r is None or r.returncode != 0:
        return f"git status 失败：{_git_err(r, err)}"
    out = r.stdout.strip()
    if not out:
        return "✅ 工作区干净（无未提交改动）。"
    if not short:
        return _trim(out)
    lines = [l for l in out.splitlines()]
    counts = {"M": 0, "A": 0, "D": 0, "R": 0, "??": 0, "其他": 0}
    for l in lines:
        head = l[:2].strip()
        if head == "??":
            counts["??"] += 1
        elif head in ("M", "A", "D", "R"):
            counts[head] += 1
        else:
            counts["其他"] += 1
    summary = "、".join(f"{k} {v} 个" for k, v in counts.items() if v)
    return _trim(f"git 状态（{summary}）：\n" + out)


def code_git_diff(args: dict) -> str:
    root = _norm_root(args.get("root"))
    bad = _require_git_repo(root)
    if bad:
        return bad
    base = ["--staged"] if args.get("staged") else []
    ref = str(args.get("ref") or "").strip()
    files = _split_list(args.get("files"))
    files_args = ["--"] + files if files else []
    stat = args.get("stat") is not False
    try:
        max_lines = max(10, min(int(args.get("max_lines") or 800), 5000))
    except ValueError:
        return "错误：max_lines 需为数字"
    parts = []
    if stat:
        r1, e1 = _git_run(root, ["diff"] + base + ([ref] if ref else [])
                          + ["--stat"] + files_args)
        if r1 is None or r1.returncode != 0:
            return f"git diff --stat 失败：{_git_err(r1, e1)}"
        if r1.stdout.strip():
            parts.append(r1.stdout.strip())
    r2, e2 = _git_run(root, ["diff"] + base + ([ref] if ref else []) + files_args)
    if r2 is None or r2.returncode != 0:
        return f"git diff 失败：{_git_err(r2, e2)}"
    body = r2.stdout.strip()
    if not body and not parts:
        return "没有差异（改动为空，或改动已被提交）。"
    if body:
        parts.append(body)
    out = "\n\n".join(parts)
    lines = out.splitlines()
    if len(lines) > max_lines:
        out = ("\n".join(lines[:max_lines])
               + f"\n…（diff 已截断，共 {len(lines)} 行；"
                 f"可用 max_lines 调大，或加 files= 只看单个文件）")
    return _trim(out)


def code_git_log(args: dict) -> str:
    root = _norm_root(args.get("root"))
    bad = _require_git_repo(root)
    if bad:
        return bad
    try:
        limit = max(1, min(int(args.get("limit") or 20), 100))
    except ValueError:
        return "错误：limit 需为数字"
    file = str(args.get("file") or "").strip()
    args_list = ["log", "--date=short",
                 "--pretty=format:%h %ad %an %s", "-n", str(limit)]
    if file:
        args_list += ["--", file]
    r, err = _git_run(root, args_list)
    if r is None or r.returncode != 0:
        return f"git log 失败：{_git_err(r, err)}"
    out = r.stdout.strip()
    if not out:
        return "仓库还没有提交记录。"
    return _trim(f"最近 {limit} 条提交：\n" + out)


def code_git_blame(args: dict) -> str:
    root = _norm_root(args.get("root"))
    bad = _require_git_repo(root)
    if bad:
        return bad
    file = str(args.get("file") or "").strip()
    if not file:
        return "错误：file（要 blame 的文件路径）不能为空"
    lines_spec = str(args.get("lines") or args.get("line") or "").strip()
    if not re.match(r"^\d+(-\d+)?$", lines_spec):
        return "错误：lines 需为行号或行区间，如 10 或 10-40"
    r, err = _git_run(root, ["blame", "-L", lines_spec, "--", file])
    if r is None or r.returncode != 0:
        return f"git blame 失败：{_git_err(r, err)}"
    lines = []
    for l in r.stdout.splitlines():
        if len(l) > 130:
            l = l[:130] + "…"
        lines.append(l)
    return _trim(f"{file} 第 {lines_spec} 行归属：\n" + "\n".join(lines))


def _parse_unified_patch(patch_text: str):
    """解析 unified diff，返回 [{"path","old_path","hunks":[{...}]}]。"""
    files, cur, hunk = [], None, None
    for raw in patch_text.splitlines():
        line = raw.rstrip("\r")
        # 跳过 git 元信息行（不会进入任何 hunk，也不该被当成补丁内容）
        if (line.startswith(("index ", "new file mode ", "deleted file mode ",
                             "old mode ", "new mode ", "similarity index ",
                             "dissimilarity index ", "rename from ", "rename to ",
                             "copy from ", "copy to ", "Binary files ",
                             "GIT binary patch", "\\ No newline at end of file"))
                or line == "--"):
            continue
        if line.startswith("diff --git "):
            if cur and cur["hunks"]:
                files.append(cur)
            cur = {"path": None, "old_path": None, "hunks": []}
            hunk = None
        elif line.startswith("--- "):
            if cur is None:
                cur = {"path": None, "old_path": None, "hunks": []}
            p = line[4:]
            if p.startswith("a/"):
                p = p[2:]
            cur["old_path"] = p
            hunk = None
        elif line.startswith("+++ "):
            if cur is None:
                cur = {"path": None, "old_path": None, "hunks": []}
            p = line[4:]
            if p.startswith("b/"):
                p = p[2:]
            cur["path"] = p
            hunk = None
        elif line.startswith("@@"):
            m = re.match(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", line)
            if not m or cur is None:
                continue
            hunk = {
                "old_start": int(m.group(1)),
                "old_count": int(m.group(2) or 1),
                "new_start": int(m.group(3)),
                "new_count": int(m.group(4) or 1),
                "lines": [],
            }
            cur["hunks"].append(hunk)
        elif cur is not None and hunk is not None and line:
            op = line[0]
            if op in "+- ":
                hunk["lines"].append((op, line[1:]))
    if cur and cur["hunks"]:
        files.append(cur)
    return files


def _locate_hunk(lines: list, hunk: dict, window: int = 160,
                 ratio_min: float = 0.82):
    """定位 hunk 在文件中的插入位置。

    先按行号精确匹配；失败后在同一位置的 ±window 行窗口内做模糊匹配
    （stripped 行序列相似度 ≥ ratio_min 才接受，避免误伤）。
    返回 (位置, 是否模糊) 或 (None, False)。
    """
    old_start, old_count = hunk["old_start"], hunk["old_count"]
    exp_old = [t for op, t in hunk["lines"] if op in "- "]
    idx = old_start - 1
    if old_count > 0:
        region = lines[idx:idx + old_count]
        if len(region) >= len(exp_old) and \
                all(a == b for a, b in zip(region, exp_old)):
            return idx, False
    if not exp_old:
        # 纯新增 hunk：无上下文可校验，按行号（越界则追加到末尾）
        return min(idx, len(lines)), False
    n = len(exp_old)
    lo = max(0, idx - window)
    hi = min(len(lines) - n + 1, idx + window + 1)
    if hi <= lo:
        return None, False
    target = [l.strip() for l in exp_old]
    best, best_ratio = lo, 0.0
    for i in range(lo, hi):
        cand = [l.strip() for l in lines[i:i + n]]
        if len(cand) < n:
            continue
        r = difflib.SequenceMatcher(None, target, cand).ratio()
        if r > best_ratio:
            best, best_ratio = i, r
    if best_ratio >= ratio_min:
        return best, True
    return None, False


def _apply_patch_file(fp: Path, old_path: str, hunks: list, is_new: bool):
    """应用补丁到单个文件（严格优先、模糊兜底），返回 (新文本, 说明) 或错误信息。

    返回说明 dict：{"enc": 编码, "fuzzy": 是否用过模糊匹配, "notes": [提示]}。
    """
    if str(fp.name).lower() == "dev/null":
        return None, "不支持删除文件（/dev/null），已跳过。"
    if fp.exists():
        try:
            text, enc = _read_text(fp)
        except OSError as e:
            return None, f"读取失败 {fp.name}: {e}"
        norm = text.replace("\r\n", "\n")
        nl = "\r\n" if "\r\n" in text else "\n"
        had_trailing = norm.endswith("\n")
        lines = norm.split("\n")
        if had_trailing and lines and lines[-1] == "":
            lines.pop()
    else:
        if not is_new:
            return None, f"文件不存在且补丁未标记为新建：{fp.name}"
        lines, had_trailing, enc, nl = [], False, "utf-8", "\n"
    fuzzy_used, notes = False, []
    for hi, hunk in enumerate(reversed(hunks), 1):
        old_count = hunk["old_count"]
        exp_old = [t for op, t in hunk["lines"] if op in "- "]
        pos, fuzzy = _locate_hunk(lines, hunk)
        if pos is None:
            return None, (f"补丁第 {hi} 段上下文不匹配（{fp.name} 期望位置第 "
                          f"{hunk['old_start']} 行起），且附近 {160} 行内无足够相似的"
                          f"内容（期望首行：{exp_old[0]!r}）。请重新生成补丁。")
        new_lines = [t for op, t in hunk["lines"] if op in "+ "]
        if fuzzy:
            fuzzy_used = True
            notes.append(f"第 {hi} 段在偏离原行号的位置（{pos + 1} 行起）模糊匹配后应用")
        if old_count == 0:
            lines[pos:pos] = new_lines
        else:
            lines[pos:pos + old_count] = new_lines
    out = nl.join(lines)
    if had_trailing:
        out += nl
    return out, {"enc": enc, "fuzzy": fuzzy_used, "notes": notes}


def code_patch(args: dict) -> str:
    patch_text = str(args.get("patch") or "")
    if not patch_text.strip():
        return "错误：patch（unified diff 文本）不能为空"
    root = _norm_root(args.get("root"))
    preview = bool(args.get("preview"))
    files = _parse_unified_patch(patch_text)
    if not files:
        return "错误：未能从补丁中解析出任何文件（需要 --- / +++ 和 @@ 段）。"
    results, errors = [], []
    for f in files:
        old_s = (f.get("old_path") or "").strip()
        new_s = (f.get("path") or "").strip()
        # 删除文件：新路径为 /dev/null、旧路径为真实文件 → 执行删除
        deleting = bool(old_s and old_s != "/dev/null" and new_s == "/dev/null")
        path_s = old_s if deleting else (new_s or old_s)
        if not path_s or path_s == "/dev/null":
            errors.append(f"跳过无效路径：{path_s!r}")
            continue
        try:
            fp = _resolve(root, path_s)
        except ValueError as e:
            errors.append(str(e))
            continue
        if deleting:
            # 2026-08-30 放开删除能力：不再跳过 /dev/null（删除文件）
            if not fp.exists():
                errors.append(f"删除目标不存在：{fp}")
                continue
            if preview:
                results.append(f"🔍 预览删除 {_rel(root, fp)}")
                continue
            try:
                fp.unlink()
            except OSError as e:
                errors.append(f"删除失败 {fp}: {e}")
                continue
            results.append(f"🗑 已删除：{_rel(root, fp)}")
            continue
        is_new = not fp.exists()
        new_text, info = _apply_patch_file(fp, old_s, f["hunks"], is_new)
        if new_text is None:
            errors.append(info or "补丁应用失败")
            continue
        enc = info.get("enc", "utf-8")
        fnote = ""
        if info.get("notes"):
            fnote = "\n  ⚠ " + "；".join(info["notes"])
        if preview:
            results.append(f"🔍 预览 {_rel(root, fp)}：{fnote}\n" + new_text)
            continue
        try:
            if not is_new:
                bak = fp.with_name(fp.name + f".bak-{int(time.time())}")
                bak.write_bytes(fp.read_bytes())
                _prune_backups(fp)
                try:
                    fp.write_bytes(new_text.encode(
                        enc if enc != "utf-8-sig" else "utf-8-sig"))
                except UnicodeEncodeError:
                    fp.write_bytes(new_text.encode("utf-8"))
                results.append(f"✅ 已应用：{_rel(root, fp)}（备份 {bak.name}）{fnote}")
            else:
                fp.parent.mkdir(parents=True, exist_ok=True)
                fp.write_bytes(new_text.encode("utf-8"))
                results.append(f"✅ 已创建：{_rel(root, fp)}（{len(new_text)} 字节）{fnote}")
        except OSError as e:
            errors.append(f"写入失败 {_rel(root, fp)}: {e}")
    out = "\n".join(results)
    if errors:
        out += "\n\n⚠ 部分文件未应用：\n" + "\n".join(f"  - {e}" for e in errors)
    if not results:
        return _trim("❌ 补丁未能应用：\n" + "\n".join(f"  - {e}" for e in errors))
    return _trim(out)


# ---------- 5. 测试与自审 ----------

def _project_python(root: Path) -> str:
    """选项目解释器：root/venv → root/.venv → 当前解释器。

    直接用 sys.executable 会绕过项目 venv，让「依赖明明装了却报 ModuleNotFoundError」的
    假阴性混进验证结果——验证一旦不可信，整个闭环就废了，所以必须跟随项目环境。
    不读 DABAI_PYTHON：那是大白自己的启动解释器，跑别人的项目会拿错依赖。
    """
    for venv in ("venv", ".venv"):
        for sub in ("bin/python", "Scripts/python.exe"):
            p = root / venv / sub
            if p.is_file():
                return str(p)
    return sys.executable


def code_test(args: dict) -> str:
    root = _norm_root(args.get("root"))
    try:
        timeout = max(10, min(int(args.get("timeout") or 300), 600))
    except ValueError:
        return "错误：timeout 需为数字"
    files = _split_list(args.get("files")) or _split_list(args.get("paths"))
    pattern = str(args.get("pattern") or "").strip()
    verbose = bool(args.get("verbose"))
    cmd = [_project_python(root), "-m", "pytest", "-q", "--no-header",
           "-p", "no:cacheprovider"]
    if verbose:
        cmd.append("-v")
    if pattern:
        cmd += ["-k", pattern]
    if files:
        cmd += files
    try:
        r = subprocess.run(
            cmd, capture_output=True, encoding="utf-8", errors="replace",
            timeout=timeout, cwd=str(root), creationflags=_CREATE_NO_WINDOW)
    except subprocess.TimeoutExpired:
        return f"⚠ 测试运行超时（>{timeout}s）已终止"
    out, err = r.stdout or "", r.stderr or ""
    if "No module named 'pytest'" in err or "No module named pytest" in err:
        py = _project_python(root)
        return (f"⚠ 项目解释器（{py}）没有安装 pytest。可改用 code_verify(mode=test) "
                f"逐文件运行，或先安装：{py} -m pip install pytest")
    failed = [l.strip() for l in out.splitlines() if l.strip().startswith("FAILED")]
    summary = ""
    for l in reversed(out.splitlines()):
        l = l.strip()
        if "passed" in l or "failed" in l or "error" in l:
            summary = l
            break
    mark = "✔" if r.returncode == 0 else "✘"
    parts = [f"{mark} 测试结果：{summary or f'退出码 {r.returncode}'}"]
    if failed:
        shown = failed[:30]
        parts.append(f"失败用例（{len(failed)} 个，显示前 {len(shown)}）：")
        parts += [f"  {f}" for f in shown]
    tail = (out + "\n" + err).strip()
    if len(tail) > 2000:
        tail = tail[-2000:] + "\n…（输出截断，只显示末尾）"
    parts.append("输出末尾：\n" + tail)
    return _trim("\n".join(parts))


def code_review(args: dict) -> str:
    root = _norm_root(args.get("root"))
    ref = str(args.get("ref") or "").strip()
    name_args = ["diff", "--name-status", "-M"] + ([ref] if ref else ["HEAD"])
    r, err = _git_run(root, name_args)
    if r is None or (r.returncode != 0 and not ref):
        # 无 HEAD（全新仓库）或非仓库：回退到 git status 清单
        r2, err2 = _git_run(root, ["status", "--porcelain=v1"])
        if r2 is None or r2.returncode != 0:
            return f"⚠ 无法读取改动（{_git_err(r or r2, err or err2)}）。" \
                   "请确认该目录是 git 仓库。"
        changed = [(l[:2].strip() or "??", l[3:].strip())
                   for l in r2.stdout.splitlines() if l.strip()]
    else:
        if r.returncode != 0:
            return f"git diff 失败：{_git_err(r, err)}"
        changed = []
        for l in r.stdout.splitlines():
            parts = l.split("\t")
            if len(parts) >= 2:
                changed.append((parts[0], parts[1]))
    if not changed:
        return "改动审查：没有发现改动（工作区与提交一致）。"
    # 补充未跟踪文件（git diff 不显示 ?? 文件）
    r3, _ = _git_run(root, ["status", "--porcelain=v1"])
    if r3 and r3.returncode == 0:
        untracked = [l[3:].strip() for l in r3.stdout.splitlines()
                     if l[:2].strip() == "??"]
        changed += [("??", f) for f in untracked]
    stat_args = ["diff", "--stat"] + ([ref] if ref else ["HEAD"])
    rs, _ = _git_run(root, stat_args)
    out = [f"改动审查（{ref or '未提交改动 vs HEAD'}）：",
           f"变更文件 {len(changed)} 个："]
    out += [f"  {st}  {f}" for st, f in changed[:80]]
    if rs and rs.returncode == 0 and rs.stdout.strip():
        out.append("\n" + rs.stdout.strip())
    checks = []
    for st, f in changed:
        if st.startswith("D"):
            continue
        fp = root / f
        if fp.is_file() and fp.suffix.lower() in (
                ".py", ".json", ".js", ".mjs", ".cjs"):
            checks.append(_syntax_check(fp))
    if checks:
        out.append("\n语法检查：")
        out += checks
    out.append("\n建议：code_git_diff 看详细 diff；code_smoke 做 import 冒烟；"
               "code_test 跑相关测试；确认无误后交给用户提交。")
    return _trim("\n".join(out))


def _syntax_check(fp: Path) -> str:
    suffix = fp.suffix.lower()
    try:
        text, _ = _read_text(fp)
    except OSError as e:
        return f"✘ {fp.name}：读取失败 {e}"
    if suffix == ".py":
        try:
            compile(text, str(fp), "exec")
            return f"✔ {fp.name}：Python 语法正确"
        except SyntaxError as e:
            snippet = (e.text or "").strip()[:60]
            return f"✘ {fp.name}：Python 语法错误 第 {e.lineno} 行 {e.msg}" \
                   + (f"（{snippet}）" if snippet else "")
    if suffix == ".json":
        try:
            json.loads(text)
            return f"✔ {fp.name}：JSON 解析正确"
        except json.JSONDecodeError as e:
            return f"✘ {fp.name}：JSON 解析错误 第 {e.lineno} 行 {e.msg}"
    if suffix in (".js", ".mjs", ".cjs"):
        node = shutil.which("node")
        if not node:
            return f"⚠ {fp.name}：未找到 node，跳过 JS 语法检查"
        try:
            r = subprocess.run(
                [node, "--check", str(fp)], capture_output=True, text=True,
                timeout=60, creationflags=_CREATE_NO_WINDOW)
        except subprocess.TimeoutExpired:
            return f"⚠ {fp.name}：node --check 超时"
        if r.returncode == 0:
            return f"✔ {fp.name}：node --check 通过"
        return f"✘ {fp.name}：node --check 失败\n{(r.stderr or r.stdout).strip()[:500]}"
    return f"✔ {fp.name}：无内置语法检查（{suffix or '无扩展名'}）"


def code_create_file(args: dict) -> str:
    path_s = str(args.get("path") or "").strip()
    content = str(args.get("content") or "")
    if not path_s:
        return "错误：path 不能为空"
    root = _norm_root(args.get("root"))
    try:
        fp = _resolve(root, path_s)
    except ValueError as e:
        return f"错误：{e}"
    if not _within(root, fp):
        return f"⛔ 越界路径（不在 root={root} 内）：{fp}"
    if fp.exists() and not args.get("overwrite"):
        return f"文件已存在：{_rel(root, fp)}（传 overwrite=true 才会覆盖）"
    try:
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_bytes(content.encode("utf-8"))
    except OSError as e:
        return f"创建失败：{e}"
    warn = ""
    if args.get("check_syntax") is not False:
        warn = _syntax_check(fp)
    return (f"✅ 已创建：{_rel(root, fp)}"
            f"（{len(content.encode('utf-8'))} 字节）"
            + (f"\n{warn}" if warn else ""))


def code_append(args: dict) -> str:
    """追加内容到文件末尾 —— 分块写长文件的核心原语。

    为什么需要它（这是「创建长文件」最容易踩的坑）：
    模型要写一个 2000 行的文件，只能在**一次** code_create_file 里把全部内容
    生成完。单次响应有输出上限，长内容必然被截断 —— 结果是工具参数 JSON 不合法
    （调用直接失败）或者文件被写成半截（更糟：看似成功，其实缺一半）。
    分块追加把「一次写 2000 行」变成「写 N 次、每次 200 行」，每块都不会撞上限。

    附带收益：配合「骨架优先」用法（先写导入 + 函数签名 + 空实现，再逐块填内容），
    文件在任何中途时刻都是**语法合法**的，中断也不会留下不可用的一团。
    """
    path_s = str(args.get("path") or "").strip()
    content = str(args.get("content") or "")
    if not path_s:
        return "错误：path 不能为空"
    if not content:
        return "错误：content 不能为空（追加空内容无意义）"
    root = _norm_root(args.get("root"))
    try:
        fp = _resolve(root, path_s)
    except ValueError as e:
        return f"错误：{e}"
    if not _within(root, fp):
        return f"⛔ 越界路径（不在 root={root} 内）：{fp}"

    created = not fp.exists()
    old = ""
    if not created:
        if fp.is_dir():
            return f"错误：{_rel(root, fp)} 是目录"
        try:
            old, _enc = _read_text(fp)
        except OSError as e:
            return f"追加失败（读取原文件）：{e}"

    # 原文件末尾没有换行时必须补一个，否则新内容会和最后一行粘在一起
    sep = "" if (created or not old or old.endswith("\n")) else "\n"
    try:
        fp.parent.mkdir(parents=True, exist_ok=True)
        with open(fp, "a", encoding="utf-8", newline="") as f:
            f.write(sep + content)
    except OSError as e:
        return f"追加失败：{e}"

    total_lines = (old + sep + content).count("\n") + (0 if (old + sep + content).endswith("\n") else 1)
    warn = ""
    if args.get("check_syntax") is not False:
        warn = _syntax_check(fp)
    head = (f"✅ 已追加：{_rel(root, fp)}（+{len(content.encode('utf-8'))} 字节，"
            f"共 {total_lines} 行）")
    if created:
        head += "（文件不存在，已新建）"
    return head + (f"\n{warn}" if warn else "")


def _run_test(fp: Path, root: Path, timeout: int) -> str:
    rel = _rel(root, fp)
    if fp.suffix.lower() != ".py":
        return f"⚠ {rel}：目前只支持运行 .py 测试文件"
    try:
        r = subprocess.run(
            [_project_python(root), str(fp)], capture_output=True, text=True,
            timeout=timeout, cwd=str(root), errors="replace",
            creationflags=_CREATE_NO_WINDOW)
    except subprocess.TimeoutExpired:
        return f"⚠ {rel}：测试运行超时（>{timeout}s）已终止"
    tail = (r.stdout + "\n" + r.stderr).strip()
    if len(tail) > 1500:
        tail = tail[-1500:] + "\n…（输出截断，只显示末尾）"
    mark = "✔" if r.returncode == 0 else "✘"
    return f"{mark} {rel}：python 运行结束，退出码 {r.returncode}\n{tail}"


def _import_smoke(fp: Path, root: Path, timeout: int) -> str:
    """在子进程里以模块方式导入 .py 文件，捕获导入期错误（最常用的冒烟）。

    语法正确但 import 就炸（缺依赖/循环导入/顶层代码报错）是改代码后最常见的坑，
    这一步专门把它暴露出来；不执行 __main__，只验证模块能完整加载。
    """
    rel = _rel(root, fp)
    code = (
        "import importlib.util, sys\n"
        "spec = importlib.util.spec_from_file_location('_smoke', %r)\n"
        "if spec is None or spec.loader is None:\n"
        "    print('LOADER_NONE'); sys.exit(2)\n"
        "m = importlib.util.module_from_spec(spec)\n"
        "try:\n"
        "    spec.loader.exec_module(m)\n"
        "except SystemExit as e:\n"
        "    print('SYSTEM_EXIT', getattr(e, 'code', None)); sys.exit(0)\n"
        "except Exception as e:\n"
        "    print('IMPORT_FAIL', type(e).__name__, str(e)[:400]); sys.exit(1)\n"
        "print('IMPORT_OK')\n" % (str(fp),)
    )
    try:
        r = subprocess.run(
            [_project_python(root), "-c", code], capture_output=True, text=True,
            timeout=timeout, cwd=str(root), errors="replace",
            creationflags=_CREATE_NO_WINDOW)
    except subprocess.TimeoutExpired:
        return f"✘ {rel}：import 冒烟超时（>{timeout}s）"
    out = ((r.stdout or "") + "\n" + (r.stderr or "")).strip()
    if r.returncode == 0 and "IMPORT_OK" in out:
        return f"✔ {rel}：import 冒烟通过"
    tail = "\n".join(out.splitlines()[-8:])[:500]
    return f"✘ {rel}：import 冒烟失败（exit={r.returncode}）\n{tail}"


def _run_smoke_command(command: str, root: Path, timeout: int) -> str:
    try:
        r = subprocess.run(
            command, shell=True, capture_output=True, text=True,
            timeout=timeout, cwd=str(root), errors="replace",
            creationflags=_CREATE_NO_WINDOW)
    except subprocess.TimeoutExpired:
        return f"✘ 冒烟命令超时（>{timeout}s）：{command}"
    out = ((r.stdout or "") + "\n" + (r.stderr or "")).strip()
    tail = "\n".join(x for x in out.splitlines() if x.strip())[-1500:]
    mark = "✔" if r.returncode == 0 else "✘"
    return f"{mark} 冒烟命令 exit={r.returncode}：{command}\n输出末尾：\n{tail}"


def code_smoke(args: dict) -> str:
    """改完代码后的冒烟关卡：语法 + import（模块能加载）+ 可选冒烟命令。

    - syntax：py_compile / JSON 解析 / node --check（不执行代码）
    - import（默认）：只以模块方式导入 .py，暴露缺依赖/循环导入/顶层代码报错；
      语法检查交给 code_verify，避免重复
    - all：语法 + import 一次性全查（单独用 code_smoke 时选它）
    - command：最后再跑一条冒烟命令
    """
    files = _split_list(args.get("files"))
    if not files:
        return "错误：files 不能为空（要冒烟的文件，逗号/换行分隔）"
    root = _norm_root(args.get("root"))
    mode = str(args.get("mode") or "import").lower()
    if mode not in ("syntax", "import", "all"):
        return "错误：mode 只能是 syntax / import / all"
    try:
        timeout = max(5, min(int(args.get("timeout") or 120), 600))
    except ValueError:
        return "错误：timeout 需为数字"
    results, failed = [], 0
    for f in files:
        try:
            fp = _resolve(root, f)
        except ValueError as e:
            results.append(f"✘ {f}：{e}")
            failed += 1
            continue
        if not _within(root, fp):
            results.append(f"✘ {f}：越界路径（不在 root={root} 内）")
            failed += 1
            continue
        if not fp.is_file():
            results.append(f"✘ {f}：文件不存在")
            failed += 1
            continue
        if mode in ("syntax", "all"):
            r = _syntax_check(fp)
            results.append(r)
            if r.startswith("✘"):
                failed += 1
                continue  # 语法不过就先修，不做 import
        if mode in ("import", "all") and fp.suffix.lower() == ".py":
            r = _import_smoke(fp, root, timeout)
            results.append(r)
            if r.startswith("✘"):
                failed += 1
    command = str(args.get("command") or "").strip()
    if command:
        r = _run_smoke_command(command, root, timeout)
        results.append(r)
        if r.startswith("✘"):
            failed += 1
    head = "冒烟结果：" + ("✔ 全部通过" if failed == 0 else f"✘ {failed} 项未通过，先修复再继续")
    return _trim(head + "\n\n" + "\n\n".join(results))


def code_verify(args: dict) -> str:
    files = _split_list(args.get("files"))
    if not files:
        return "错误：files 不能为空"
    root = _norm_root(args.get("root"))
    mode = str(args.get("mode") or "syntax").lower()
    try:
        timeout = max(5, min(int(args.get("timeout") or 120), 600))
    except ValueError:
        return "错误：timeout 需为数字"
    results = []
    for f in files:
        try:
            fp = _resolve(root, f)
        except ValueError as e:
            results.append(f"✘ {f}：{e}")
            continue
        if not _within(root, fp):
            results.append(f"✘ {f}：越界路径（不在 root={root} 内）")
            continue
        if not fp.is_file():
            results.append(f"✘ {f}：文件不存在")
            continue
        if mode in ("syntax", "all"):
            results.append(_syntax_check(fp))
        if mode in ("test", "all"):
            results.append(_run_test(fp, root, timeout))
    return _trim("\n\n".join(results))


def code_undo_turn(args: dict) -> str:
    """把某一轮工具改过的文件还原成轮前状态（轮级快照，见 harness/turn_snapshot.py）。

    为什么按「轮」而不是按文件：一次重构会散着改好几个文件，方向错了要退回时，
    逐个去找 .bak-<时间戳> 既慢又容易漏；新建的文件连 .bak 都没有。轮级快照把
    「这一轮动过哪些文件」变成一条清单，一次全退。undo 幂等：还原过的再调一次
    只会报「本来就一致」，不会又写一遍。

    list_only=true 只看有哪些轮、不还原；paths 只退指定文件（改对了的留着）。
    """
    import datetime

    from harness import turn_snapshot as ts

    if args.get("list_only"):
        try:
            limit = max(1, min(int(args.get("limit") or 10), 50))
        except (TypeError, ValueError):
            limit = 10
        turns = ts.list_turns(limit=limit)
        if not turns:
            return "没有轮快照（data/turn_snapshots 为空，或本轮还没改过文件）。"
        lines = ["可还原的轮快照（新→旧）："]
        for t in turns:
            when = datetime.datetime.fromtimestamp(t["mtime"]).strftime("%m-%d %H:%M:%S")
            extra = f"，{t['skipped']} 个未纳入快照" if t["skipped"] else ""
            blind = f"，另有 {'/'.join(t['blind'])} 动过磁盘但未追踪" if t["blind"] else ""
            lines.append(f"  {t['turn_id']}  {when}  {t['files']} 个文件{extra}{blind}")
        lines.append("传 turn_id 还原某一轮；不传 = 最近一轮。")
        return "\n".join(lines)

    paths = args.get("paths")
    if isinstance(paths, str):
        paths = [p.strip() for p in paths.replace("\n", ",").split(",") if p.strip()]
    return ts.undo(str(args.get("turn_id") or ""), paths or None)


HANDLERS = {
    "code_undo_turn": code_undo_turn,
    "code_search": code_search,
    "code_list_files": code_list_files,
    "code_read": code_read,
    "code_locate": code_locate,
    "code_analyze": code_analyze,
    "code_deps": code_deps,
    "code_map": code_map,
    "code_edit": code_edit,
    "code_create_file": code_create_file,
    "code_append": code_append,
    "code_verify": code_verify,
    "code_smoke": code_smoke,
    "code_git_status": code_git_status,
    "code_git_diff": code_git_diff,
    "code_git_log": code_git_log,
    "code_git_blame": code_git_blame,
    "code_patch": code_patch,
    "code_test": code_test,
    "code_review": code_review,
    # ---- 合并自原 shell 技能（10 个本机命令行工具）----
    "shell_run": shell_impl.shell_run,
    "find_file": shell_impl.find_file,
    "search_text": shell_impl.search_text,
    "list_files": shell_impl.list_files,
    "read_lines": shell_impl.read_lines,
    "git_status": shell_impl.git_status,
    "git_diff": shell_impl.git_diff,
    "system_check": shell_impl.system_check,
    "symbols": shell_impl.symbols,
    "read_json": shell_impl.read_json,
    # ---- 合并自原 sys_search 技能（3 个全盘文件搜索工具）----
    "sys_find": sys_search_impl.sys_find,
    "sys_recent": sys_search_impl.sys_recent,
    "sys_locate": sys_search_impl.sys_locate,
    # ---- 合并自原 worktree 技能（7 个 git 隔离工作树工具）----
    "wt_create": worktree_impl.wt_create,
    "wt_list": worktree_impl.wt_list,
    "wt_status": worktree_impl.wt_status,
    "wt_diff": worktree_impl.wt_diff,
    "wt_run": worktree_impl.wt_run,
    "wt_merge": worktree_impl.wt_merge,
    "wt_discard": worktree_impl.wt_discard,
}
