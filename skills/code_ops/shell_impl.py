"""本机命令行技能 —— 大白直接操作用户电脑（Windows / Linux / macOS）的能力。

设计要点（与 harness 底座对齐）：
- 主 Agent 通过 function calling 直接调用本技能，取代旧"分流 LLM + 批量 steps"链路；
  Agent 逐条看结果再决定下一步，天然具备反思能力；
- 执行经 asyncio.wait_for 超时保护 + 输出截断；调用统计由 harness runtime 监督。
"""
from __future__ import annotations

import asyncio
import difflib
import fnmatch
import json
import os
import re
import subprocess
from typing import Optional   # 注解里用到（文件有 future annotations，运行时本不需要，
                              # 但显式导入让 pyflakes / 类型检查器都干净）

# 跨平台兼容层（项目根 platform_compat.py）：平台差异统一走它，本文件不再判断 os.name
try:
    import platform_compat as pc
except ImportError:  # 技能模块被单独加载时兜底
    import sys as _sys
    _sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))))
    import platform_compat as pc


def _executor():
    from codex_runner import EXECUTOR
    return EXECUTOR


def _decode(b: bytes) -> str:
    for enc in ("utf-8", "gbk"):
        try:
            return b.decode(enc)
        except UnicodeDecodeError:
            continue
    return b.decode("utf-8", errors="replace")


def _run_shell_cmd(cmd: str, timeout: int, argv, cwd) -> str:
    """执行命令：自成进程组，超时杀整棵树并带回超时前的部分输出。

    与 codex_runner.Executor.run_sync 的改进保持同一行为（那边服务网页 /cmd）。
    为什么杀整棵树：shell=True 时直接子进程只是 /bin/sh，curl/编译等孙进程若只杀
    shell 会变孤儿继续占网络与资源——「命令超时了机器还一直慢」的来源。
    """
    kw = pc.spawn_kwargs(new_group=True)
    try:
        if argv:
            p = subprocess.Popen(argv, cwd=cwd, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, **kw)
        else:
            p = subprocess.Popen(cmd, shell=True, cwd=cwd,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kw)
    except Exception as e:
        return f'$ {cmd}\n[异常] {e}'
    try:
        out_b, err_b = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _, note = pc.terminate_tree(p.pid, timeout=15)
        try:
            out_b, err_b = p.communicate(timeout=5)
        except Exception:
            out_b, err_b = b'', b''
        got = (_decode(out_b or b'').strip() + '\n'
               + _decode(err_b or b'').strip()).strip()
        snippet = f'\n[超时前输出] …{got[-500:]}' if got else ''
        return (f'$ {cmd}\n[超时：超过{timeout}秒，已终止整棵进程树（{note}）]'
                f'{snippet}')
    text = _decode(out_b)
    err = _decode(err_b)
    if err.strip():
        text += '\n[stderr]\n' + err
    text = text.strip() or '(无输出)'
    return f'$ {cmd}\n[exit={p.returncode}]\n{text}'


async def shell_run(args: dict) -> str:
    cmd = str(args.get("command") or "").strip()
    if not cmd:
        return "错误：command 不能为空"
    timeout = max(1, min(int(args.get("timeout") or 60), 1200))
    # 普通用户：整条命令进 bwrap 沙箱（系统目录只读 + 只有自己目录可写 + 断网），
    # 工作目录也锁进沙箱；管理员与系统身份保持原行为（见 sandbox.py）。
    argv = None
    cwd = str(args.get("root") or "").strip() or None
    try:
        import sandbox as _sb

        actor = _sb.current()
        if actor is not None and not actor.is_admin:
            work = str(_sb.resolve_path(actor, cwd)) if cwd else str(actor.sandbox)
            argv, cwd = _sb.wrap_shell(actor, cmd, work)
    except Exception as e:  # noqa: BLE001
        return f"沙箱拒绝：{e}"
    try:
        out = await asyncio.wait_for(
            asyncio.to_thread(_run_shell_cmd, cmd, timeout, argv, cwd), timeout=timeout + 10)
    except TimeoutError:
        return f"命令超时（>{timeout}s），已终止：{cmd}"
    except Exception as e:
        return f"执行失败：{e.__class__.__name__}: {e}"
    ok = "[exit=0]" in out
    if len(out) > 4000:
        out = out[:3997] + "..."
    return out if ok else out + "\n（注意：命令返回非零退出码，可能执行失败）"


async def find_file(args: dict) -> str:
    name = str(args.get("name") or "").strip()
    if len(name) < 2:
        return "错误：name 关键词太短"
    stem = os.path.splitext(name)[0].replace(" ", "").lower()
    ext = os.path.splitext(name)[1].lower()
    roots = pc.search_roots()
    skip = {"windows", "program files", "program files (x86)",
            "$recycle.bin", "appdata", "system volume information",
            "node_modules", ".git", "__pycache__"}
    if pc.IS_POSIX:
        skip |= {"proc", "sys", "snap", ".cache", "lost+found"}
    hits, seen = [], set()

    def _run_search():
        for root, max_depth in roots:
            try:
                for dirpath, dirnames, filenames in os.walk(root):
                    rel = os.path.relpath(dirpath, root)
                    depth = 0 if rel == "." else rel.count(os.sep) + 1
                    if depth >= max_depth or os.path.basename(dirpath).lower() in skip:
                        dirnames[:] = []
                        continue
                    for fn in filenames:
                        low = fn.replace(" ", "").lower()
                        score = difflib.SequenceMatcher(
                            None, stem, os.path.splitext(low)[0]).ratio()
                        if stem in low or score >= 0.6:
                            fullp = os.path.join(dirpath, fn)
                            if fullp not in seen:
                                seen.add(fullp)
                                same_ext = 1 if (not ext or fn.lower().endswith(ext)) else 0
                                hits.append((score + same_ext * 0.2, fullp))
                    if len(hits) >= 30:
                        break
            except (PermissionError, OSError):
                continue
            if len(hits) >= 30:
                break

    await asyncio.to_thread(_run_search)
    if not hits:
        return (f"未找到与「{name}」匹配的文件（已搜索桌面/下载/视频/音乐/文档/"
                f"项目目录及本机主要目录浅层）。可以换关键词再试，或告诉用户手动确认位置。")
    hits.sort(key=lambda x: -x[0])
    top = [p for _s, p in hits[:8]]
    best = top[0]
    lines = [f"找到 {len(hits)} 个匹配（按相似度排序）："]
    lines += [f"- {p}" for p in top]
    lines.append(f'\n最匹配："{best}"——后续步骤请使用这个真实完整路径。')
    return "\n".join(lines)


async def search_text(args: dict) -> str:
    """按关键词搜索文件内容（类 grep）：优先 ripgrep，缺失时回退 findstr。

    只读操作；结果返回 文件:行号:内容，限制条数避免刷屏。
    """
    query = str(args.get("query") or "").strip()
    if not query:
        return "错误：query 不能为空"
    root = str(args.get("root") or os.getcwd()).strip().strip('"')
    if not os.path.isdir(root):
        return f"错误：目录不存在：{root}"
    globs = [g.strip() for g in str(args.get("glob") or "").split(",") if g.strip()]
    max_results = max(5, min(int(args.get("max_results") or 40), 200))
    timeout = max(5, min(int(args.get("timeout") or 30), 120))
    exe = _executor()

    # 1) ripgrep：快、尊重编码与 .gitignore
    try:
        def _rg():
            cmd = ["rg", "-n", "--no-heading", "-i", "-S", "--color", "never"]
            for g in globs:
                cmd += ["-g", g]
            cmd += ["--", query, root]
            return subprocess.run(cmd, capture_output=True, timeout=timeout,
                                  text=True, encoding="utf-8", errors="replace")

        p = await asyncio.wait_for(asyncio.to_thread(_rg), timeout=timeout + 10)
        if p.returncode in (0, 1):  # 0=有命中 1=无命中；其他 returncode 视为 rg 自身报错
            lines = [ln for ln in p.stdout.splitlines() if ln.strip()]
            total = len(lines)
            if not total:
                return f"未找到包含「{query}」的内容（{root}）"
            shown = lines[:max_results]
            body = "\n".join(shown)
            tail = f"\n…（共 {total} 处，已截断，可加 glob 缩小范围）" if total > max_results else ""
            return f"匹配 {total} 处（{root}）：\n{body}{tail}"
        # returncode>1 → rg 报错，落到 findstr 兜底
    except FileNotFoundError:
        pass  # rg 未安装 → findstr
    except subprocess.TimeoutExpired:
        return f"搜索超时（>{timeout}s），已终止：{query}"
    except Exception as e:
        return f"搜索失败：{e.__class__.__name__}: {e}"

    # 2) 系统自带工具兜底：Windows 用 findstr，POSIX 用 grep（忽略规则不如 rg，尽力而为）
    if pc.IS_WINDOWS:
        pattern = query.replace('"', '""')
        cmd2 = f'findstr /s /n /i /c:"{pattern}" "{root}\\*.*"'
        tool = "findstr"
    else:
        pattern = query.replace("\\", "\\\\").replace('"', '\\"')
        cmd2 = (f'grep -rnI -i --exclude-dir=.git --exclude-dir=node_modules '
                f'--exclude-dir=.venv -- "{pattern}" "{root}"')
        tool = "grep"
    try:
        out = await asyncio.wait_for(
            asyncio.to_thread(exe.run_sync, cmd2, timeout), timeout=timeout + 10)
    except TimeoutError:
        return f"搜索超时（>{timeout}s），已终止：{query}"
    except Exception as e:
        return f"搜索失败：{e.__class__.__name__}: {e}"
    lines = [ln for ln in out.splitlines()
             if ln.strip() and "[exit=" not in ln and not ln.startswith("$ ")]
    total = len(lines)
    if not total:
        return f"未找到包含「{query}」的内容（{root}）"
    shown = lines[:max_results]
    tail = f"\n…（共 {total} 处，已截断）" if total > max_results else ""
    return f"匹配 {total} 处（{root}，{tool}）：\n" + "\n".join(shown) + tail


async def list_files(args: dict) -> str:
    """列出目录结构与文件清单（只读，优先 rg 尊重 .gitignore，限制条数）。"""
    root = str(args.get("root") or os.getcwd()).strip().strip('"')
    if not os.path.isdir(root):
        return f"错误：目录不存在：{root}"
    try:
        depth = max(0, min(int(args.get("depth") or 2), 6))
    except Exception:
        depth = 2
    globs = [g.strip() for g in str(args.get("glob") or "").split(",") if g.strip()]
    max_entries = max(10, min(int(args.get("max_entries") or 120), 500))
    timeout = max(5, min(int(args.get("timeout") or 20), 60))

    rels: list[str] = []
    used_rg = True
    try:
        def _rg_files():
            cmd = ["rg", "--files", "--glob", "!.git"]
            for g in globs:
                cmd += ["-g", g]
            cmd.append(root)
            return subprocess.run(cmd, capture_output=True, timeout=timeout,
                                  text=True, encoding="utf-8", errors="replace")

        p = await asyncio.wait_for(asyncio.to_thread(_rg_files), timeout=timeout + 10)
        if p.returncode == 0:
            rels = [os.path.relpath(x, root).replace("\\", "/")
                    for x in p.stdout.splitlines() if x.strip()]
            rels = [r for r in rels if not r.startswith("..")]
    except (FileNotFoundError, subprocess.TimeoutExpired):
        used_rg = False
    except Exception:
        used_rg = False

    if not used_rg:
        SKIP = {".git", "node_modules", "__pycache__", ".venv", "dist", "build",
                ".ruff_cache", ".idea", ".vscode"}

        def _walk():
            out = []
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = sorted(d for d in dirnames if d not in SKIP)
                rel = os.path.relpath(dirpath, root).replace("\\", "/")
                d = 0 if rel == "." else rel.count("/") + 1
                if d > depth:
                    dirnames[:] = []
                    continue
                for fn in filenames:
                    if globs and not any(fnmatch.fnmatch(fn, g) for g in globs):
                        continue
                    out.append(os.path.join(rel, fn) if rel != "." else fn)
            return out

        try:
            rels = await asyncio.to_thread(_walk)
        except Exception as e:
            return f"扫描失败：{e.__class__.__name__}: {e}"

    rels = sorted(rels)
    filtered = [r for r in rels if r.count("/") <= depth]
    total = len(filtered)
    shown = filtered[:max_entries]
    lines = [f"共 {total} 个文件（{root}，depth≤{depth}）："]
    if total > max_entries:
        from collections import Counter
        cnt = Counter(r.split("/")[0] for r in filtered)
        top = "，".join(f"{k}({v})" for k, v in cnt.most_common(12))
        lines.append(f"（未列全，显示前 {max_entries} 个）一级目录分布：{top}")
    lines += shown
    if total > max_entries:
        lines.append(f"…共 {total} 个已截断；可缩小 root / depth 或用 glob 过滤")
    return "\n".join(lines)


async def read_lines(args: dict) -> str:
    """按行区间读取文件（只读，避免整读大文件；自动识别 UTF-8/GBK）。"""
    path = str(args.get("path") or "").strip().strip('"')
    if not path:
        return "错误：path 不能为空（可用 find_file / search_text 先定位真实路径）"
    if not os.path.isfile(path):
        return f"错误：文件不存在：{path}"
    try:
        start = max(1, int(args.get("start") or 1))
        max_lines = max(1, min(int(args.get("max_lines") or 100), 1000))
    except Exception:
        start, max_lines = 1, 100
    end = start + max_lines - 1
    try:
        with open(path, "rb") as f:
            if b"\x00" in f.read(2048):
                return f"错误：看起来是二进制文件，不适合按行读取：{path}"
    except Exception as e:
        return f"读取失败：{e.__class__.__name__}: {e}"

    def _read():
        for enc in ("utf-8", "gbk", "latin-1"):
            try:
                got = []
                with open(path, encoding=enc, errors="strict") as f:
                    for i in range(1, end + 1):
                        ln = f.readline()
                        if not ln:
                            break
                        if i >= start:
                            got.append(ln)
                return got, enc
            except UnicodeDecodeError:
                continue
        return [], "utf-8"

    got, enc = await asyncio.to_thread(_read)
    if not got:
        return f"文件「{path}」在第 {start} 行之后没有内容"

    def _has_more():
        try:
            with open(path, encoding=enc, errors="replace") as f:
                for _ in range(end + 1):
                    if not f.readline():
                        return False
                return bool(f.readline())
        except Exception:
            return False

    has_more = await asyncio.to_thread(_has_more)
    out = []
    for i, ln in enumerate(got, start=start):
        text = ln.rstrip("\r\n")
        if len(text) > 240:
            text = text[:237] + "..."
        out.append(f"{i:>6}│ {text}")
    head = f"{path}（编码 {enc}，行 {start}-{start + len(got) - 1}）"
    if has_more:
        head += "，后面还有内容"
    return head + "\n" + "\n".join(out)


def _git_root(root: str) -> str:
    """返回给定目录所在的 git 仓库根；不是仓库则返回空串。"""
    try:
        r = subprocess.run(["git", "-C", root, "rev-parse", "--show-toplevel"],
                           capture_output=True, timeout=15, text=True,
                           encoding="utf-8", errors="replace")
        if r.returncode == 0:
            return r.stdout.strip()
    except Exception:
        pass
    return ""


async def git_status(args: dict) -> str:
    """查看 git 工作区状态（只读）：分支 + 变更文件清单。"""
    root = str(args.get("root") or os.getcwd()).strip().strip('"')
    if not os.path.isdir(root):
        return f"错误：目录不存在：{root}"
    repo = _git_root(root)
    if not repo:
        return f"错误：{root} 不在任何 git 仓库内"
    timeout = max(5, min(int(args.get("timeout") or 30), 60))
    max_lines = max(10, min(int(args.get("max_lines") or 300), 1000))

    def _run(cmd):
        return subprocess.run(cmd, capture_output=True, timeout=timeout,
                              text=True, encoding="utf-8", errors="replace")

    branch = (await asyncio.to_thread(
        _run, ["git", "-C", repo, "branch", "--show-current"])).stdout.strip() or "(detached)"
    st = await asyncio.to_thread(_run, ["git", "-C", repo, "status", "--short"])
    if st.returncode != 0:
        return f"git status 失败：{st.stderr.strip()[:300]}"
    lines = [ln for ln in st.stdout.splitlines() if ln.strip()]
    total = len(lines)
    shown = lines[:max_lines]
    head = f"分支：{branch} | 变更 {total} 项（{repo}）"
    body = "\n".join(shown)
    tail = f"\n…共 {total} 项已截断" if total > max_lines else ""
    return head + ("\n" + body if body else "（工作区干净）") + tail


async def git_diff(args: dict) -> str:
    """查看 git 改动差异（只读）：默认未暂存；staged=true 看暂存区；可指定单文件。"""
    root = str(args.get("root") or os.getcwd()).strip().strip('"')
    if not os.path.isdir(root):
        return f"错误：目录不存在：{root}"
    repo = _git_root(root)
    if not repo:
        return f"错误：{root} 不在任何 git 仓库内"
    path = str(args.get("path") or "").strip().strip('"')
    staged = bool(args.get("staged"))
    max_lines = max(10, min(int(args.get("max_lines") or 400), 2000))
    timeout = max(5, min(int(args.get("timeout") or 30), 60))
    scope = "--cached" if staged else None

    def _run(cmd):
        return subprocess.run(cmd, capture_output=True, timeout=timeout,
                              text=True, encoding="utf-8", errors="replace")

    base = ["git", "-C", repo, "diff"]
    if scope:
        base.append(scope)
    if path:
        base += ["--", path]
    stat = await asyncio.to_thread(_run, base + ["--stat"])
    diff = await asyncio.to_thread(_run, base)
    if diff.returncode != 0:
        return f"git diff 失败：{diff.stderr.strip()[:300]}"
    parts = []
    if stat.stdout.strip():
        parts.append(stat.stdout.strip())
    body_lines = [ln for ln in diff.stdout.splitlines() if ln.strip()]
    total = len(body_lines)
    shown = body_lines[:max_lines]
    if shown:
        parts.append("\n".join(shown))
    if total > max_lines:
        parts.append(f"…diff 共 {total} 行，已截断；可指定 path 或减小范围")
    return "\n".join(parts) if parts else "（无差异）"


async def system_check(args: dict) -> str:
    """系统只读体检：进程 / 端口 / 磁盘。适合排查服务、推流、端口占用。"""
    what = str(args.get("what") or "all").strip().lower()
    keyword = str(args.get("keyword") or "").strip().lower()
    port = str(args.get("port") or "").strip()
    timeout = max(5, min(int(args.get("timeout") or 25), 60))
    max_lines = max(10, min(int(args.get("max_lines") or 40), 200))
    root = str(args.get("root") or os.getcwd()).strip().strip('"')
    parts = []

    def _run(cmd):
        return subprocess.run(cmd, capture_output=True, timeout=timeout,
                              text=True, encoding="utf-8", errors="replace")

    if what in ("all", "process", "proc"):
        parsed = []
        if pc.IS_WINDOWS:
            p = await asyncio.to_thread(_run, ["tasklist", "/FO", "CSV", "/NH"])
            rows = [r for r in p.stdout.splitlines() if r.strip()]
            for r in rows:
                m = re.match(r'^"([^"]+)","(\d+)","([^"]*)","(\d+)","([\d,]+) K?"', r)
                if m:
                    name, pid, sess, sessn, mem = m.groups()
                    if keyword and keyword not in name.lower():
                        continue
                    parsed.append(f"{pid}  {name}  内存 {mem} K")
        else:
            for proc in await asyncio.to_thread(pc.list_processes):
                name = str(proc.get("name") or "")
                cmdline = str(proc.get("cmdline") or "")
                if keyword and keyword not in f"{name} {cmdline}".lower():
                    continue
                parsed.append(f"{proc.get('pid')}  {name}  {cmdline[:100]}".rstrip())
        if keyword and not parsed:
            parts.append(f"进程（关键词 {keyword}）：未找到")
        else:
            total = len(parsed)
            shown = parsed[:max_lines]
            head = f"进程（{total} 个" + (f"，关键词 {keyword}" if keyword else "") + "）："
            parts.append(head + "\n" + "\n".join(shown) + (f"\n…共 {total} 个已截断" if total > max_lines else ""))

    if what in ("all", "port", "net"):
        parsed = []
        if pc.IS_WINDOWS:
            p = await asyncio.to_thread(_run, ["netstat", "-ano", "-n"])
            rows = [r for r in p.stdout.splitlines() if "LISTENING" in r]
            for r in rows:
                tok = r.split()
                if len(tok) >= 5:
                    proto, local, foreign, state, pid = tok[0], tok[1], tok[2], tok[3], tok[4]
                    lp = local.rsplit(":", 1)[-1]
                    if port and lp != port:
                        continue
                    parsed.append(f"{proto}  {local}  → {foreign}  PID {pid}")
        else:
            for r in await asyncio.to_thread(pc.list_listening_ports):
                tok = r.split()
                if len(tok) < 4:
                    continue
                local = tok[3]
                lp = local.rsplit(":", 1)[-1]
                if port and lp != port:
                    continue
                m = re.search(r"pid=(\d+)", r)
                parsed.append(f"{tok[0]}  {local}  PID {m.group(1) if m else '-'}")
        if port and not parsed:
            parts.append(f"端口 {port}：无 LISTENING")
        else:
            total = len(parsed)
            shown = parsed[:max_lines]
            head = f"监听端口（{total} 个" + (f"，端口 {port}" if port else "") + "）："
            parts.append(head + "\n" + "\n".join(shown) + (f"\n…共 {total} 个已截断" if total > max_lines else ""))

    if what in ("all", "disk"):
        target = os.path.abspath(root)
        du = await asyncio.to_thread(pc.disk_free, target)
        if du:
            total_b, free_b = du
            parts.append(f"磁盘 {target}：剩余 {free_b / 2**30:.1f} GB / 共 {total_b / 2**30:.1f} GB")
        else:
            parts.append(f"磁盘检查失败：无法读取 {target} 的磁盘用量")

    if not parts:
        parts.append(f"未知检查项：{what}（可用 all/process/port/disk）")
    return "\n".join(parts)


def _sig_of(node) -> str:
    """从 AST 节点提取简化签名（只列参数名，不含默认值）。"""
    try:
        a = node.args
        pos = [x.arg for x in a.args]
        if a.vararg:
            pos.append("*" + a.vararg.arg)
        pos += [x.arg for x in a.kwonlyargs]
        if a.kwarg:
            pos.append("**" + a.kwarg.arg)
        return "(" + ", ".join(pos) + ")"
    except Exception:
        return "(...)"


# ---- 多语言符号索引：tree-sitter（真实 AST）优先，缺失时回退正则规则表 ----
_TS_EXT_LANG = {
    ".js": ("javascript", None), ".mjs": ("javascript", None),
    ".cjs": ("javascript", None), ".jsx": ("javascript", None),
    ".ts": ("typescript", None), ".tsx": ("typescript", "tsx"),
    ".c": ("c", None), ".h": ("c", None),
    ".cpp": ("cpp", None), ".cc": ("cpp", None), ".cxx": ("cpp", None),
    ".hpp": ("cpp", None), ".hh": ("cpp", None),
    ".cs": ("c_sharp", None), ".java": ("java", None), ".go": ("go", None),
    ".rs": ("rust", None), ".sh": ("bash", None), ".bash": ("bash", None),
    ".lua": ("lua", None), ".php": ("php", None), ".rb": ("ruby", None),
    ".kt": ("kotlin", None), ".kts": ("kotlin", None),
    ".swift": ("swift", None),
}

_ts_parsers = {}


def _ts_parser(lang: str, variant: Optional[str] = None):
    """按需加载 tree-sitter 语法（懒加载 + 缓存，避免每次调用都 import）。"""
    key = (lang, variant)
    if key not in _ts_parsers:
        import importlib
        mod = importlib.import_module("tree_sitter_" + lang)
        if variant == "tsx":
            fn = getattr(mod, "language_tsx", None) or getattr(mod, "language", None)
        else:
            fn = getattr(mod, "language", None) or getattr(mod, "language_" + lang, None)
        if fn is None:
            raise RuntimeError(f"tree_sitter_{lang} 未导出 language 函数")
        raw = fn()
        from tree_sitter import Language, Parser
        # 兼容两种 API：新版返回 Language 实例，旧版返回 PyCapsule，统一包装
        try:
            lng = raw if isinstance(raw, Language) else Language(raw)
        except TypeError:
            lng = raw
        _ts_parsers[key] = Parser(lng)
    return _ts_parsers[key]


def _node_name(node, src: bytes) -> Optional[str]:
    """取声明节点名：优先 name 字段，缺失时找第一个 identifier 类后代。"""
    n = node.child_by_field_name("name")
    if n is not None:
        return src[n.start_byte:n.end_byte].decode("utf-8", "replace")
    # 兜底只在声明头部找（跳过函数体/结构体体/初始化值，避免抓到 body 里的标识符）
    skip_body = ("body", "block", "compound_statement", "value", "initializer",
                 "field_declaration_list", "class_body", "statement_block",
                 "struct_type", "object_type", "enum_body", "declaration_list",
                 "interface_body", "program")
    stack = [c for c in node.children if c.type not in skip_body]
    while stack:
        c = stack.pop()
        if c.type in ("identifier", "type_identifier", "field_identifier",
                      "property_identifier", "function", "method"):
            return src[c.start_byte:c.end_byte].decode("utf-8", "replace")
        if c.type in skip_body:
            continue
        stack.extend(c.children)
    return None


_TS_AVOID = {"arrow_function", "lambda", "lambda_expression", "call_expression",
             "assignment", "lexical_declaration"}
_TS_KINDS = ("function", "method", "class", "struct", "interface", "enum",
             "trait", "constructor", "protocol", "macro", "type")


def _ts_symbols(path: str, parser) -> list:
    """用 tree-sitter 语法树收集函数/类/方法/结构体/接口等符号。"""
    with open(path, "rb") as f:
        src = f.read()
    tree = parser.parse(src)
    text = src.decode("utf-8", "replace")
    lines = text.splitlines()
    out: list = []
    seen = set()

    def walk(node):
        t = node.type
        if t in _TS_AVOID:
            for c in node.children:
                walk(c)
            return
        cand = any(k in t.split("_") for k in _TS_KINDS)
        if not cand and t == "variable_declarator":
            val = node.child_by_field_name("value")
            cand = val is not None and val.type in ("arrow_function", "function_expression")
        if cand:
            # 没有 name 字段的普通节点（如 impl/extension 块）不作为符号
            if node.child_by_field_name("name") is None \
                    and not t.endswith(("_declaration", "_definition", "_item", "_specifier")):
                cand = False
        if cand:
            name = _node_name(node, src)
            if name:
                row = node.start_point[0] + 1
                span = node.end_point[0] - node.start_point[0] + 1
                key = (row, name)
                if key not in seen:
                    seen.add(key)
                    snippet = lines[row - 1].strip()[:120] if 0 < row <= len(lines) else ""
                    out.append((row, t.replace("_", " "), name,
                                f"{snippet}  [{span} 行]"))
        for c in node.children:
            walk(c)

    walk(tree.root_node)
    out.sort(key=lambda x: x[0])
    return out


_REGEX_FALLBACK = {
    ".js": [(r"\b(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)", "function"),
            (r"\b(?:export\s+)?class\s+([A-Za-z_$][\w$]*)", "class"),
            (r"\b(?:export\s+)?(?:abstract\s+)?class\s+([A-Za-z_$][\w$]*)", "class")],
    ".ts": [(r"\b(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)", "function"),
            (r"\b(?:export\s+)?class\s+([A-Za-z_$][\w$]*)", "class"),
            (r"\b(?:export\s+)?interface\s+([A-Za-z_$][\w$]*)", "interface"),
            (r"\b(?:export\s+)?enum\s+([A-Za-z_$][\w$]*)", "enum")],
    ".c": [(r"^\s*[\w:\*\s]+\s+([A-Za-z_]\w*)\s*\([^;]*\)\s*(?:const\s*)?\{", "function")],
    ".cpp": [(r"^\s*[\w:<>\*\s&]+\s+([A-Za-z_]\w*)\s*\([^;]*\)\s*(?:const\s*)?(?:noexcept\s*)?\{", "function")],
    ".cs": [(r"^\s*(?:public|private|protected|internal|static|sealed|abstract|partial|readonly|async|virtual|override|new|unsafe|extern)?\s*(?:class|interface|struct|enum|record)\s+(\w+)", "type"),
            (r"^\s*(?:public|private|protected|internal|static|sealed|abstract|partial|async|virtual|override|new|unsafe|extern)?\s*(?:[\w<>\[\],\?]+\s+)?(\w+)\s*\([^;]*\)\s*(?:=>|\{)", "method")],
    ".java": [(r"^\s*(?:public|private|protected|static|final|abstract|synchronized|native|default|strictfp)?\s*(?:class|interface|enum|record|@interface)\s+(\w+)", "type"),
              (r"^\s*(?:public|private|protected|static|final|abstract|synchronized|native|default)?\s*(?:[\w<>\[\],\?]+\s+)?(\w+)\s*\([^;]*\)\s*(?:throws\s+[\w,\s]+)?\{", "method")],
    ".go": [(r"^func\s+(?:\([^)]*\)\s+)?(\w+)", "func"),
            (r"^type\s+(\w+)\s+(?:struct|interface)", "type")],
    ".rs": [(r"^\s*(?:pub\s*\([^)]*\)\s*|pub\s+)?(?:async\s+)?fn\s+(\w+)", "fn"),
            (r"^\s*(?:pub\s*\([^)]*\)\s*|pub\s+)?(?:struct|enum|trait)\s+(\w+)", "type")],
    ".sh": [(r"^\s*([A-Za-z_]\w*)\s*\(\)\s*\{", "function")],
    ".bash": [(r"^\s*([A-Za-z_]\w*)\s*\(\)\s*\{", "function")],
    ".lua": [(r"^\s*function\s+([\w.:]+)", "function")],
    ".php": [(r"^\s*(?:public|private|protected|static|final|abstract)?\s*function\s+(\w+)", "function"),
             (r"^\s*(?:abstract\s+|final\s+)?class\s+(\w+)", "class"),
             (r"^\s*interface\s+(\w+)", "interface")],
    ".rb": [(r"^\s*def\s+([\w!?=]+)", "def"),
            (r"^\s*class\s+(\w+)", "class"),
            (r"^\s*module\s+(\w+)", "module")],
    ".kt": [(r"^\s*(?:private|public|internal|protected|suspend|inline|tailrec|operator|infix|override)?\s*fun\s+(\w+)", "fun"),
            (r"^\s*(?:data\s+|sealed\s+|enum\s+|abstract\s+|open\s+|inner\s+)?(?:class|interface|object)\s+(\w+)", "type")],
    ".swift": [(r"^\s*(?:public|private|internal|fileprivate|open|static|class|override|mutating|nonmutating|async|throws)?\s*fu[cn]\s+(\w+)", "func"),
               (r"^\s*(?:public|private|internal|fileprivate|open|final)?\s*(?:class|struct|enum|protocol)\s+(\w+)", "type")],
}


def _regex_symbols(path: str, rules) -> list:
    """正则规则表兜底：按行匹配定义样式（无 tree-sitter 时的降级方案）。"""
    out: list = []
    seen = set()
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except Exception:
        return out
    for i, ln in enumerate(lines, start=1):
        for pat, kind in rules:
            m = re.search(pat, ln)
            if m:
                name = m.group(1).strip()
                key = (i, name)
                if name and key not in seen:
                    seen.add(key)
                    out.append((i, kind, name, ln.strip()[:120]))
                break
    return out


# 大函数的展开阈值：函数超过 _NEST_MIN_LINES 行就列出内部嵌套函数（嵌套函数
# 常是真正的逻辑单元），超过 _SECTION_MIN_LINES 行再附上内部分段注释。
# 没有这两条，模型面对一个 1232 行的函数只能整读——而单次结果上限 16000 字符，
# 整读一个大函数要 8 次调用。列出来之后，模型可以直接「跳到 3123 行看流式创建」。
_NEST_MIN_LINES = 80
_SECTION_MIN_LINES = 300


async def symbols(args: dict) -> str:
    """列出代码文件的符号表（只读）：Python 用标准库 ast；其他语言用 tree-sitter
    真实语法树（JS/TS/C/C++/C#/Java/Go/Rust/Bash/Lua/PHP/Ruby/Kotlin/Swift），
    语法包缺失时自动回退正则规则表。"""
    path = str(args.get("path") or "").strip().strip('"')
    if not path:
        return "错误：path 不能为空（用 find_file / list_files 先定位）"
    if not os.path.isfile(path):
        return f"错误：文件不存在：{path}"
    try:
        max_results = max(5, min(int(args.get("max_results") or 80), 300))
    except Exception:
        max_results = 80
    ext = os.path.splitext(path)[1].lower()

    out: list = []
    engine = "?"
    py_total = None      # .py 路径的顶级符号总数（由 _scan 一并返回）
    if ext == ".py":
        def _scan():
            import ast
            with open(path, encoding="utf-8", errors="replace") as f:
                src = f.read()
            try:
                tree = ast.parse(src)
            except SyntaxError as e:
                lines = src.splitlines()
                snippet = lines[e.lineno - 1].strip() if e.lineno and 0 < e.lineno <= len(lines) else ""
                return None, f"语法错误（第 {e.lineno} 行）：{e.msg} {snippet[:120]}", None
            src_lines = src.split("\n")

            def _span(n):
                return (n.end_lineno or n.lineno) - n.lineno + 1

            def _head(n, kind, name, pad):
                # 行数是判断「这个符号值不值得读」的第一信息：1232 行的函数必须
                # 定点读，30 行的函数可以直接读全。原来只给行号，模型无从判断。
                line = f"{n.lineno:>5}  {pad}{kind} {name}{_sig_of(n)}"
                doc = ast.get_docstring(n)
                if doc:
                    first = doc.strip().splitlines()[0][:60] if doc.strip() else ""
                    if first:
                        line += f"   # {first}"
                return line + f"  [{_span(n)} 行]"

            def _sections(n):
                """函数内的分段注释行（形如 `# ---- 标题 ----`）：大函数的定点读入口。"""
                base = n.col_offset + 4
                found = []
                for i in range(n.lineno + 1, (n.end_lineno or n.lineno) + 1):
                    raw = src_lines[i - 1] if 0 < i <= len(src_lines) else ""
                    st = raw.strip()
                    if not st.startswith("#") or not re.match(r"^#\s*[-=]{3,}", st):
                        continue
                    if len(raw) - len(raw.lstrip()) < base:
                        continue
                    title = re.sub(r"^#[\s\-=]+", "", st).strip(" -=#")[:34]
                    found.append(f"{i} {title}" if title else str(i))
                    if len(found) >= 8:
                        break
                return found

            out2 = []

            def _expand(fn, pad):
                """大函数：追加它的嵌套函数与分段注释。

                这是「理解长文件」的关键一步——嵌套函数常是真正的逻辑单元
                （比如对话主循环里的 _save_round_ckpt / _create_stream），
                不列出来模型就只知道「有个 1232 行的函数」，只能整读。
                """
                if _span(fn) < _NEST_MIN_LINES:
                    return
                # 不能只看 fn.body：嵌套函数可能定义在循环/分支内部（实测：对话主
                # 循环里的 _create_stream 就在 while 体内），只扫顶层语句会漏掉它们——
                # 而它们往往正是最难找、最该被看见的那几个。
                inner = [m for m in ast.walk(fn)
                         if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)) and m is not fn]
                # 只列一层：去掉「嵌套函数里的嵌套函数」，避免大函数输出爆炸
                tops = [m for m in inner
                        if not any(o is not m and o.lineno < m.lineno
                                   and (o.end_lineno or o.lineno) >= (m.end_lineno or m.lineno)
                                   for o in inner)]
                tops.sort(key=lambda x: x.lineno)
                for m in tops:
                    k = "async def" if isinstance(m, ast.AsyncFunctionDef) else "def"
                    out2.append((m.lineno, k, m.name, _head(m, k, m.name, pad)))
                if _span(fn) >= _SECTION_MIN_LINES:
                    secs = _sections(fn)
                    if secs:
                        out2.append((fn.lineno, "section", "",
                                     pad + "↳ 分段（可定点读）：" + " / ".join(secs)))

            tops = [n for n in tree.body
                    if isinstance(n, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))]
            for node in tops[:max_results]:
                kind = ("class" if isinstance(node, ast.ClassDef)
                        else ("async def" if isinstance(node, ast.AsyncFunctionDef) else "def"))
                out2.append((node.lineno, kind, node.name, _head(node, kind, node.name, "")))
                if isinstance(node, ast.ClassDef):
                    for m in node.body:
                        if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)):
                            k2 = "async def" if isinstance(m, ast.AsyncFunctionDef) else "def"
                            out2.append((m.lineno, k2, m.name, _head(m, k2, m.name, "    ")))
                            _expand(m, "          ")
                else:
                    _expand(node, "        ")
            return out2, None, len(tops)

        rows, err, py_total = await asyncio.to_thread(_scan)
        if err:
            return f"{path}：{err}"
        out = rows
        engine = "stdlib ast"
    elif ext in _TS_EXT_LANG:
        try:
            parser = _ts_parser(*_TS_EXT_LANG[ext])
            out = await asyncio.to_thread(_ts_symbols, path, parser)
            engine = "tree-sitter"
        except Exception:
            rules = _REGEX_FALLBACK.get(ext)
            if rules:
                out = await asyncio.to_thread(_regex_symbols, path, rules)
                engine = "regex fallback"
    else:
        rules = _REGEX_FALLBACK.get(ext)
        if rules:
            out = await asyncio.to_thread(_regex_symbols, path, rules)
            engine = "regex fallback"
    if not out:
        return (f"错误：暂不支持该文件类型（{ext}）；"
                "可用 search_text 按关键词搜索，或用 list_files 查看文件")
    # .py 路径已在 _scan 内部按「顶级符号数」截断：展开出来的嵌套函数与分段注释
    # 不占 max_results 额度——它们正是为了「少读代码」才列的，被截掉就白做了。
    total = py_total if py_total is not None else len(out)
    shown = out if py_total is not None else out[:max_results]
    head = f"{path}（{engine}，共 {total} 个符号）："
    body = "\n".join(x[3] for x in shown)
    tail = f"\n…共 {total} 个已截断" if total > max_results else ""
    return head + "\n" + body + tail


async def read_json(args: dict) -> str:
    """读取并校验 JSON 文件（只读，自动识别 UTF-8/GBK，支持点路径取子字段）。"""
    path = str(args.get("path") or "").strip().strip('"')
    if not path:
        return "错误：path 不能为空（用 find_file / list_files 先定位）"
    if not os.path.isfile(path):
        return f"错误：文件不存在：{path}"
    key = str(args.get("key") or "").strip().strip(".")
    try:
        max_chars = max(200, min(int(args.get("max_chars") or 3000), 8000))
    except Exception:
        max_chars = 3000

    def _load():
        for enc in ("utf-8", "gbk", "latin-1"):
            try:
                with open(path, encoding=enc, errors="strict") as f:
                    return json.load(f), enc, None
            except UnicodeDecodeError:
                continue
            except json.JSONDecodeError as e:
                with open(path, encoding=enc, errors="replace") as f:
                    lines = f.read().splitlines()
                snippet = lines[e.lineno - 1].strip()[:150] if e.lineno and 0 < e.lineno <= len(lines) else ""
                return None, enc, f"JSON 解析失败（第 {e.lineno} 行第 {e.colno} 列）：{e.msg}\n附近内容：{snippet}"
        return None, "utf-8", "无法以 UTF-8 / GBK 解码该文件"

    data, enc, err = await asyncio.to_thread(_load)
    if err:
        return f"{path}：{err}"
    cur = data
    if key:
        for part in key.split("."):
            if isinstance(cur, list):
                try:
                    cur = cur[int(part)]
                except Exception:
                    return f"错误：路径 {key} 不存在（「{part}」不是有效数组索引）"
            elif isinstance(cur, dict) and part in cur:
                cur = cur[part]
            else:
                return f"错误：路径 {key} 不存在（找不到「{part}」）"
    text = json.dumps(cur, ensure_ascii=False, indent=2)
    if len(text) > max_chars:
        text = text[:max_chars] + f"\n…（已截断，共 {len(text)} 字符）"
    head = f"{path}（编码 {enc}" + (f"，路径 {key}" if key else "") + "）"
    return head + "\n" + text



