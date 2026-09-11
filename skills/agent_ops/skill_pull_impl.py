"""全网拉取技能（skill_pull）—— 按任务需求从全网检索、筛选、下载、校验并安装成熟的 Agent Skill。

三步工作流（与 skill.json 中三个工具一一对应）：
1. skill_pull_search   —— 关键词 → GitHub API 检索候选技能仓库（按 star/活跃度/结构嫌疑排序）
2. skill_pull_inspect  —— 深入核查单个仓库：结构、许可证、文件树、可疑文件
3. skill_pull_install  —— 下载 tarball → 安全解压 → 结构校验 + 静态安全审查 → 注册进 skills/
                          → 触发热重载 → 输出使用说明

安全边界（本技能自身也遵守）：
- 只访问官方域：api.github.com / codeload.github.com（仓库内脚本引用的外部 URL 只警告不跟随）
- 绝不执行下载的代码：下载、解压、审查、复制均为纯静态操作
- 危险代码模式命中即拒绝安装（命令执行 / 动态 eval / 反序列化 / 混淆 / 网络下载执行 / 路径穿越）
- 体积与文件类型限制；技能名白名单；重复安装默认拒绝（force 才覆盖）
- 允许配置 GITHUB_TOKEN（环境变量或 data/github_token.txt）以提升 GitHub API 配额

实现说明：仅用 Python 标准库；HANDLERS 表注册三个工具；所有函数返回可读文本。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import ssl
import tarfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

try:                                    # requests 可用则优先（Windows+OpenSSL 3.x 下 CA 加载更稳）
    import requests as _requests
    _HAS_REQUESTS = True
except Exception:
    _HAS_REQUESTS = False


class _HttpStatusError(Exception):
    """统一的 HTTP 状态异常（requests 与 urllib 两通道共用）。"""

    def __init__(self, code: int, body: str = ""):
        self.code = code
        self.body = body
        super().__init__(f"HTTP {code}")

BASE_DIR = Path(__file__).resolve().parent.parent.parent          # 大白根目录
SKILLS_DIR = BASE_DIR / "skills"                                  # 技能注册目录
CACHE_DIR = BASE_DIR / "data" / "skill_pull_cache"                # 下载缓存（不入库）
GH_API = "https://api.github.com"
GH_CODELOAD = "https://codeload.github.com"
UA = "Mozilla/5.0 (dabai/skill_pull; +https://github.com/)"
RELOAD_PORTS = (8000, 8900, 7860)                                 # 本地服务候选端口

MAX_FILE_BYTES = 1024 * 1024          # 单个文件上限 1MB
MAX_TOTAL_BYTES = 16 * 1024 * 1024    # 解压总上限 16MB
MIN_STARS_DEFAULT = 50                # 成熟度默认门槛
NAME_RE = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")

# 常见中文任务词的英文增强（提升 GitHub 检索命中率）
_QUERY_HINTS = {
    "网页抓取": "web scraping", "抓取": "web scraping", "爬虫": "web scraping crawler",
    "数据分析": "data analysis",
    "翻译": "translation", "写作": "writing", "文案": "copywriting",
    "图片": "image", "绘图": "image generation", "pdf": "pdf",
    "表格": "excel spreadsheet", "excel": "excel",
    "邮件": "email", "会议": "meeting", "摘要": "summarization",
    "记忆": "memory", "搜索": "search web", "浏览器": "browser automation",
    "面试": "interview", "代码": "coding", "编程": "coding",
    "sql": "sql database", "数据库": "database sql", "api": "api",
    "视频": "video", "语音": "speech voice", "音频": "audio",
    "ppt": "slides presentation", "文档": "document", "简历": "resume",
    "调研": "research", "规划": "planning", "决策": "decision making",
}


# ---------------- 基础请求 ----------------

def _ssl_ctx():
    """构建可用的 SSL 上下文：优先用系统(certifi)CA，Windows 下回退到系统证书库，零依赖。"""
    try:
        import certifi  # 若环境已装 certifi 则直接用
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        pass
    try:
        ctx = ssl.create_default_context()
        ctx.check_hostname = True
        ctx.verify_mode = ssl.CERT_REQUIRED
        enum = getattr(ssl, "enum_certificates", None)   # Windows 专有
        if enum is not None:
            loaded = 0
            for _bytes, _enc, _trust in enum("ROOT"):
                try:
                    ctx.load_verify_locations(cadata=_bytes)
                    loaded += 1
                except Exception:
                    pass
            if loaded > 0:
                return ctx
    except Exception:
        pass
    return ssl.create_default_context()  # 最后兜底：默认 CA 束


def _token() -> str:
    tok = (os.environ.get("GITHUB_TOKEN") or "").strip()
    if tok:
        return tok
    pf = BASE_DIR / "data" / "github_token.txt"
    try:
        if pf.exists():
            tok = pf.read_text(encoding="utf-8").strip()
    except Exception:
        pass
    return tok


def _request(url: str, timeout: float = 20.0, headers: dict | None = None,
             data: bytes | None = None) -> bytes:
    h = {"User-Agent": UA}
    if headers:
        h.update(headers)
    tok = _token()
    if tok and data is None:
        h["Authorization"] = "Bearer " + tok
        h["Accept"] = "application/vnd.github+json"
    if _HAS_REQUESTS:
        try:
            if data is not None:
                resp = _requests.post(url, headers=h, data=data, timeout=timeout)
            else:
                resp = _requests.get(url, headers=h, timeout=timeout, allow_redirects=True)
            if resp.status_code >= 400:
                raise _HttpStatusError(resp.status_code, resp.text[:500])
            return resp.content
        except _HttpStatusError:
            raise
        except Exception:
            pass   # requests 渠道整体失败时回退 urllib（极少见）
    req = urllib.request.Request(url, headers=h, data=data)
    with urllib.request.urlopen(req, timeout=timeout, context=_ssl_ctx()) as resp:
        return resp.read()


def _gh_json(url: str, params: dict | None = None):
    u = url
    if params:
        u += ("&" if "?" in u else "?") + urllib.parse.urlencode(params)
    try:
        data = _request(u)
    except (_HttpStatusError, urllib.error.HTTPError) as e:
        if e.code == 403:
            body = ""
            try:
                body = (e.body if isinstance(e, _HttpStatusError)
                        else e.read().decode("utf-8", "replace"))
            except Exception:
                pass
            if "rate limit" in body.lower():
                return {"__error__": "GitHub API 触发速率限制。未配置 GITHUB_TOKEN 时约 60 次/小时；"
                                     "可将 token 写入环境变量 GITHUB_TOKEN 或 data/github_token.txt 提升到 5000 次/小时。"}
            return {"__error__": f"GitHub API 拒绝访问（HTTP 403）：{body[:200]}"}
        if e.code == 404:
            return {"__error__": "仓库不存在或无权访问（HTTP 404）。请确认仓库完整名 owner/name。"}
        return {"__error__": f"GitHub API 错误：HTTP {e.code}"}
    except Exception as e:
        return {"__error__": f"网络请求失败：{e.__class__.__name__}: {e}"}
    try:
        return json.loads(data.decode("utf-8"))
    except Exception as e:
        return {"__error__": f"响应解析失败：{e}"}


# ---------------- 工具1：搜索 ----------------

def _skill_like_hint(full: str, desc: str) -> bool:
    blob = (full + " " + desc).lower()
    return bool(
        re.search(r"skill", blob)
        and re.search(r"(agent|claude|gpt|llm|assistant|workflow|harness|copilot|prompt)", blob)
    )


def _enrich_query(query: str) -> str:
    """中文/简语任务描述 → 增强查询词。

    GitHub 对「中英混排」查询命中率很低（实测中文句+英文词混合 → total=0），
    因此中文需求命中映射表时，直接用英文词搜索；无映射才保留原文。
    """
    q = query.strip()
    lowered = q.lower()
    if re.search(r"[\u4e00-\u9fff]", q):
        for zh, en in _QUERY_HINTS.items():
            if zh in q:
                enq = en
                if "skill" not in enq.lower() and "agent" not in enq.lower():
                    enq += " agent skill"
                return enq
        return q
    if "skill" not in lowered and "agent" not in lowered:
        return f"{q} agent skill".strip()
    return q


def _search_github_request(query: str, min_stars: int) -> list:
    q = _enrich_query(query)
    if "skill" not in q.lower() and "agent" not in q.lower():
        q += " agent skill"
    params = {"q": q, "sort": "stars", "order": "desc", "per_page": "20"}
    data = _gh_json(GH_API + "/search/repositories", params)
    if isinstance(data, dict) and data.get("__error__"):
        return [data]
    items = (data or {}).get("items", []) if isinstance(data, dict) else []
    rows = []
    for it in items:
        full = (it.get("full_name") or "").strip()
        if not full:
            continue
        stars = int(it.get("stargazers_count") or 0)
        if stars < min_stars:
            continue
        lic = ((it.get("license") or {}) or {}).get("spdx_id") or ""
        rows.append({
            "repo": full,
            "stars": stars,
            "forks": int(it.get("forks_count") or 0),
            "lang": it.get("language") or "",
            "updated": (it.get("pushed_at") or "")[:10],
            "license": lic,
            "desc": (it.get("description") or "").strip(),
            "url": it.get("html_url") or "",
            "skill_like": _skill_like_hint(full, it.get("description") or ""),
        })
    return rows


def skill_pull_search(args: dict) -> str:
    query = str(args.get("query") or "").strip()
    if not query:
        return "请提供任务描述或搜索关键词（query 参数），如 'web scraping 网页抓取'。"
    try:
        min_stars = max(0, int(args.get("min_stars") or MIN_STARS_DEFAULT))
    except (TypeError, ValueError):
        min_stars = MIN_STARS_DEFAULT
    try:
        max_results = min(20, max(1, int(args.get("max_results") or 8)))
    except (TypeError, ValueError):
        max_results = 8

    rows = _search_github_request(query, min_stars)
    if rows and isinstance(rows[0], dict) and rows[0].get("__error__"):
        return rows[0]["__error__"]
    if not rows:
        return (f"未找到符合条件的技能仓库（关键词 '{query}'，最低 {min_stars} star）。"
                "建议：改用英文关键词（如 'web scraping'）、降低 min_stars，或更换描述说法后再试。")
    rows = rows[:max_results]
    lines = [f"为「{query}」找到 {len(rows)} 个候选技能仓库（按 star 降序）：", ""]
    for i, r in enumerate(rows, 1):
        flag = "✓ 像 skill 仓库" if r["skill_like"] else "· 待核查"
        lines.append(
            f"{i}. {r['repo']}  ⭐{r['stars']}  ⑂{r['forks']}  "
            f"更新 {r['updated']}  {r['lang'] or ''}  "
            f"许可证 {r['license'] or '未知'}  [{flag}]"
        )
        if r["desc"]:
            lines.append(f"   简介：{r['desc'][:110]}")
    lines.append("")
    lines.append("下一步：对候选执行 skill_pull_inspect(\"owner/name\") 核实结构，"
                 "或用 skill_pull_install(\"owner/name\") 直接安装。")
    return "\n".join(lines)


# ---------------- 仓库元数据 ----------------

def _repo_meta(repo: str) -> dict:
    data = _gh_json(f"{GH_API}/repos/{urllib.parse.quote(repo, safe='/')}")
    if isinstance(data, dict) and data.get("__error__"):
        return data
    if not isinstance(data, dict):
        return {"__error__": "GitHub 返回了异常数据。"}
    lic = ((data.get("license") or {}) or {}).get("spdx_id") or ""
    return {
        "repo": repo,
        "default_branch": data.get("default_branch") or "main",
        "stars": data.get("stargazers_count") or 0,
        "forks": data.get("forks_count") or 0,
        "open_issues": data.get("open_issues_count") or 0,
        "lang": data.get("language") or "",
        "size_kb": data.get("size") or 0,
        "pushed_at": (data.get("pushed_at") or "")[:10],
        "created_at": (data.get("created_at") or "")[:10],
        "license": lic,
        "archived": bool(data.get("archived")),
        "desc": (data.get("description") or "").strip(),
        "topics": (data.get("topics") or [])[:10],
    }


def _file_tree(repo: str, branch: str):
    data = _gh_json(f"{GH_API}/repos/{urllib.parse.quote(repo, safe='/')}/git/trees/{urllib.parse.quote(branch)}",
                    {"recursive": "1"})
    if isinstance(data, dict) and data.get("__error__"):
        return data
    if not isinstance(data, dict) or data.get("truncated"):
        return {"__warning__": "文件树过大被截断，仅显示部分结果。"}
    out = []
    for it in data.get("tree", []):
        if it.get("type") == "blob":
            out.append({"path": it.get("path") or "", "size": int(it.get("size") or 0)})
        elif it.get("type") == "tree":
            out.append({"path": (it.get("path") or "") + "/", "size": 0})
    return out


# ---------------- 工具2：检查 ----------------

_SUSPICIOUS_EXTS = {".exe", ".dll", ".bin", ".so", ".jar", ".bat", ".ps1", ".scr", ".msi", ".vbs"}
_CODE_EXTS = {".py", ".js", ".ts", ".mjs", ".cjs", ".sh", ".rb", ".php", ".pl", ".lua"}


def skill_pull_inspect(args: dict) -> str:
    repo = str(args.get("repo") or "").strip()
    if not repo or "/" not in repo:
        return "请提供 GitHub 仓库完整名 owner/name，如 'anthropics/skills'。"
    meta = _repo_meta(repo)
    if meta.get("__error__"):
        return meta["__error__"]
    branch = meta["default_branch"]
    tree = _file_tree(repo, branch)
    warn = ""
    if isinstance(tree, dict):
        warn = "（" + (tree.get("__error__") or tree.get("__warning__") or "") + "）"
        paths = []
    else:
        paths = [p["path"] for p in tree]

    base = ""
    if paths:
        first_dir = next((p for p in paths if p.endswith("/")), "")
        if first_dir:
            base = first_dir.rstrip("/")
        else:
            fb = next((p for p in paths if not p.endswith("/")), "")
            if "/" in fb:
                base = fb.split("/")[0]
    rel = [p[len(base) + 1:] if base and p.startswith(base + "/") else p for p in paths]
    rel = [p for p in rel if p]
    names = set()
    for p in rel:
        parts = p.split("/")
        for i in range(len(parts)):
            names.add("/".join(parts[: i + 1]))
    top_items = sorted(n for n in names if n.count("/") == 0)

    has_skill_md = any(p.lower().endswith("skill.md") for p in rel)
    has_skill_json = any(p.lower().endswith("skill.json") for p in rel)
    has_agents = any(p.lower().endswith("agents.md") for p in rel)
    has_readme = any(p.lower().startswith("readme") for p in rel)
    md_count = sum(1 for p in rel if p.lower().endswith(".md"))
    code_files = [p for p in rel if p.rsplit(".", 1)[-1].lower() in _CODE_EXTS]
    susp = [p for p in rel if p.rsplit(".", 1)[-1].lower() in _SUSPICIOUS_EXTS]

    lines = [
        f"仓库：{repo}（{meta['desc'][:90]}）", "",
        f"成熟度：⭐ {meta['stars']} / ⑂ {meta['forks']} / issues {meta['open_issues']} / "
        f"{meta['lang'] or '未知语言'} / 创建 {meta['created_at']} / 最近推送 {meta['pushed_at']}"
        f"{' / ⚠ 已归档' if meta['archived'] else ''} / 许可证 {meta['license'] or '未标注'}",
        f"默认分支：{branch}；体积约 {meta['size_kb'] / 1024:.1f} MB；主题标签：{', '.join(meta['topics']) or '无'}",
        "",
        "结构判定：",
        f"  · SKILL.md 说明书：{'✔ 有' if has_skill_md else '✘ 无'}",
        f"  · skill.json 清单：{'✔ 有' if has_skill_json else '✘ 无'}",
        f"  · AGENTS.md：{'✔ 有' if has_agents else '无'}  · README：{'✔ 有' if has_readme else '无'}",
        f"  · Markdown 文档 {md_count} 个；代码文件 {len(code_files)} 个；"
        f"可疑二进制/脚本 {len(susp)} 个{'：' + '、'.join(susp[:6]) if susp else ''}",
    ]
    verdict = ("像标准 Skill 仓库，可安装" if (has_skill_md or has_skill_json) else
               ("包含说明书/清单但不标准，可安装但需留意" if (has_agents or has_readme) else
                "未发现标准 Skill 结构（没有 SKILL.md / skill.json），不建议按技能安装"))
    lines.append(f"  ▶ 结论：{verdict}{warn}")
    lines.append("")
    lines.append(f"顶层内容：{', '.join(top_items[:24]) or '（空）'}")
    if code_files[:12]:
        lines.append(f"代码文件示例：{', '.join(code_files[:12])}")
    lines.append("下一步：确认无误后用 skill_pull_install 安装。")
    return "\n".join(lines)


# ---------------- 下载与安全解压 ----------------

def _download_tarball(repo: str, branch: str) -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    fname = re.sub(r"[^A-Za-z0-9_.-]", "_", repo) + "_" + branch + ".tar.gz"
    dest = CACHE_DIR / fname
    url = f"{GH_CODELOAD}/{repo}/tar.gz/refs/heads/{urllib.parse.quote(branch)}"
    data = _request(url, timeout=90)
    dest.write_bytes(data)
    return dest


def _safe_extract(tarball: Path, dest: Path) -> list:
    """安全解压：粉碎路径穿越（../、绝对路径、盘符），返回顶层目录名列表。"""
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True, exist_ok=True)
    top = []
    total = 0
    with tarfile.open(tarball, "r:gz") as tf:
        for m in tf.getmembers():
            raw = m.name.replace("\\", "/")
            if m.isdir() and "/" not in raw and not top:
                top.append(raw.rstrip("/"))
            if ".." in raw.split("/") or raw.startswith("/") or re.match(r"^[A-Za-z]:", raw):
                raise ValueError(f"解压路径异常，已中止：{raw}")
            total += m.size
            if total > MAX_TOTAL_BYTES:
                raise ValueError(f"解压总大小超过上限（{MAX_TOTAL_BYTES // (1024 * 1024)}MB），已中止")
            if m.isdir():
                continue
            if m.size > MAX_FILE_BYTES:
                raise ValueError(f"单文件过大（>{MAX_FILE_BYTES // 1024}KB），已中止：{m.name}")
            target = dest / raw
            target.parent.mkdir(parents=True, exist_ok=True)
            f = tf.extractfile(m)
            if f is None:
                continue
            target.write_bytes(f.read())
    return top


# ---------------- 静态安全审查 ----------------

_SECURITY_RULES = [
    ("BLOCK", "执行任意命令(系统 shell)",
     re.compile(r"os\.system\s*\(|subprocess\.(?:run|Popen|call|check_output|check_call)\s*\([^)]*shell\s*=\s*True|"
                r"subprocess\.(?:run|Popen|call)\s*\(\s*['\"]")),
    ("BLOCK", "动态执行代码(eval/exec)",
     re.compile(r"(?<!\w)(?:eval|exec)\s*\(|__import__\s*\(|compile\s*\([^)]*['\"]exec")),
    ("BLOCK", "危险反序列化(pickle)",
     re.compile(r"pickle\.(?:loads?|load)\s*\(")),
    ("BLOCK", "base64 大块混淆(>1KB)",
     re.compile(r"(?:base64|b64decode)[^\n]{0,120}decode\s*\(\s*['\"][^'\"]{1024,}['\"]")),
    ("BLOCK", "下载并执行模式",
     re.compile(r"(?:requests\.get\s*\(\s*['\"](?:http|https)[^'\"]+['\"]\)[^\n]{0,300}?"
                r"(?:os\.startfile|CreateProcess|open\s*\([^)]*\.(?:py|exe|bat|ps1|sh)['\"][^)]*['\"]w)|"
                r"urllib\.request\.urlretrieve\s*\([^)]*['\"](?:http|https)[^'\"]+['\"]\))")),
    ("BLOCK", "进程注入/系统底层调用",
     re.compile(r"ctypes\.|windll|CreateRemoteThread|WriteProcessMemory|VirtualAllocEx")),
    ("WARN", "网络套接字/端口操作",
     re.compile(r"socket\.socket\s*\(")),
    ("BLOCK", "路径穿越写入",
     re.compile(r"['\"][^'\"]*\.\.(?:/|\\)[^'\"]*['\"]\s*[,)]")),
    ("WARN", "调用子进程(受限)",
     re.compile(r"subprocess\.(?:run|Popen|call|check_output)\s*\(")),
    ("WARN", "删除文件/目录操作",
     re.compile(r"shutil\.rmtree\s*\(|os\.remove\s*\(|os\.unlink\s*\(")),
    ("WARN", "读取系统环境变量/密钥",
     re.compile(r"os\.environ|getenv\s*\(")),
    ("WARN", "远程网络请求(requests/urllib)",
     re.compile(r"requests\.(?:get|post|put|delete|head)|urllib\.request")),
    ("WARN", "加密/密钥痕迹",
     re.compile(r"Fernet|pycryptodome|Crypto\.|cryptography\.|sshpass")),
]


def _security_scan(root: Path) -> tuple:
    """扫描可执行文本文件中的危险模式。返回 (是否放行, 报告行列表)。"""
    report = []
    bad = 0
    scanned = 0
    text_exts = {".md", ".txt", ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".env",
                 ".gitignore", ".html", ".css", ".xml", ".csv", ".sql", ".ipynb"}
    skip_exts = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".woff", ".woff2", ".ttf",
                 ".mp3", ".wav", ".lock", ".pyc", ".pyd"}
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        if "__pycache__" in p.parts or ".git" in p.parts:
            continue
        if p.suffix in skip_exts:
            continue
        if p.suffix not in _CODE_EXTS and p.suffix not in text_exts:
            if p.suffix in _SUSPICIOUS_EXTS:
                report.append(f"⚠ 发现可疑二进制/脚本文件：{p.relative_to(root)}")
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        scanned += 1
        for level, name, pat in _SECURITY_RULES:
            m = pat.search(text)
            if m:
                ln = text[: m.start()].count("\n") + 1
                mark = "✋ 阻断" if level == "BLOCK" else "⚠ 提示"
                report.append(f"{mark} [{name}] {p.relative_to(root)}:{ln}")
                if level == "BLOCK":
                    bad += 1
                break
    n_warn = len(report) - bad
    report.insert(0, f"静态安全审查：扫描可执行/文本文件 {scanned} 个，"
                     f"命中阻断级 {bad} 条，提示级 {n_warn} 条。")
    return bad == 0, report


# ---------------- 技能发现与清单 ----------------

def _depth(p: Path, base: Path) -> int:
    return len(p.relative_to(base).parts)


def _find_skill_dirs(root: Path, depth: int = 3) -> list:
    """递归寻找含 SKILL.md 或 skill.json 的目录（排除明显噪音目录）。"""
    out = []
    for p in sorted(root.rglob("*")):
        if not p.is_dir():
            continue
        if any(part in {"__pycache__", ".git", "node_modules", "dist", "build", "assets", "images", "img"}
               for part in p.parts):
            continue
        if _depth(p, root) > depth:
            continue
        if (p / "SKILL.md").is_file() or (p / "skill.json").is_file():
            out.append(p)
    return out


def _manifest_of(dir_path: Path):
    mf = dir_path / "skill.json"
    if mf.exists():
        try:
            m = json.loads(mf.read_text(encoding="utf-8"))
            if isinstance(m, dict):
                return m
        except Exception:
            return {"__error__": f"{mf.name} 解析失败"}
    return None


def _skill_name_of(dir_path: Path, repo: str, used: set) -> str:
    """确定技能名：manifest.name 优先；否则目录名清洗；与已用名冲突时加后缀。"""
    name = ""
    m = _manifest_of(dir_path)
    if isinstance(m, dict) and not m.get("__error__"):
        name = str(m.get("name") or "").strip()
    if not name or not NAME_RE.match(name):
        cand = dir_path.name.strip().lower()
        cand = re.sub(r"[^a-zA-Z0-9_-]", "_", cand).strip("_")
        if not cand or cand in {"skill", "skills", "agent", "agents", "src", "main"}:
            cand = re.sub(r"[^a-zA-Z0-9_-]", "_", repo.split("/")[-1]).strip("_").lower()
        name = cand or "skill_pulled"
    base = name
    n = 2
    while name in used and n < 99:
        name = f"{base}-{n}"
        n += 1
    return name


def _auto_manifest(dir_path: Path, name: str, repo: str) -> dict:
    """为只有 SKILL.md 的目录生成最小清单（纯指令型技能）。"""
    dtext = ""
    desc = dir_path / "README.md"
    if desc.exists():
        try:
            first = re.sub(r"[#*<>\[\]]", "", desc.read_text(encoding="utf-8", errors="replace").strip())
            dtext = first.split("\n")[0][:100]
        except Exception:
            dtext = ""
    if not dtext:
        dtext = f"从 {repo} 拉取的 Agent Skill。"
    return {
        "name": name,
        "title": dir_path.name,
        "version": "1.0.0",
        "description": dtext,
        "author": repo.split("/")[0],
        "enabled": True,
        "prompt": f"【技能 {name}】已就绪：使用说明见本目录 SKILL.md；"
                  f"涉及 {dtext[:40]} 等需求时，先调用 skill_help('{name}') 读取说明书再执行。",
        "tools": [],
        "_pulled_from": repo,
    }


# ---------------- 工具3：安装 ----------------

def skill_pull_install(args: dict) -> str:
    repo = str(args.get("repo") or "").strip()
    if not repo or "/" not in repo:
        return "请提供 GitHub 仓库完整名 owner/name，如 'anthropics/skills'。"
    select = str(args.get("select") or "").strip()
    force = bool(args.get("force"))
    meta = _repo_meta(repo)
    if meta.get("__error__"):
        return meta["__error__"]
    branch = meta["default_branch"]

    # 下载 + 安全解压
    try:
        tgz = _download_tarball(repo, branch)
        work = CACHE_DIR / (re.sub(r"[^A-Za-z0-9_.-]", "_", repo) + "_x")
        top = _safe_extract(tgz, work)
    except Exception as e:
        msg = str(e)
        if "解压路径异常" in msg or "过大" in msg or "总大小" in msg:
            return f"✋ 已中止：{msg}"
        return f"下载/解压失败：{e.__class__.__name__}: {e}"
    root = work
    if top:
        cand = work / top[0]
        if cand.is_dir():
            root = cand

    # select 提前解析：只审查并安装选中目录（避免整仓误伤）
    sel = None
    if select:
        sel = (root / select).resolve()
        try:
            sel.relative_to(root)
        except ValueError:
            return "select 路径必须位于仓库内。"
        if not sel.is_dir():
            return f"select 路径 '{select}' 不存在于仓库内。"

    # 结构校验 + 静态安全审查（select 时只扫选中目录）
    scan_root = sel if sel is not None else root
    ok, rev = _security_scan(scan_root)
    lines = list(rev)
    lines.append("")
    if not ok:
        lines += ["✋ 安全审查未通过，已拒绝安装。",
                  "如需人工复核，请在 data/skill_pull_cache/ 下查看解压内容（未执行任何代码）。"]
        return "\n".join(lines)

    # 技能发现
    candidates = [p for p in _find_skill_dirs(scan_root) if p != scan_root]
    root_is_skill = any((scan_root / fn).is_file() for fn in ("SKILL.md", "skill.json"))
    if sel is not None:
        if not ((sel / "SKILL.md").exists() or (sel / "skill.json").exists()):
            return f"select 路径 '{select}' 下未找到技能（需要 SKILL.md 或 skill.json）。"
        candidates = [sel]
    elif root_is_skill and not candidates:
        candidates = [scan_root]
    elif not candidates:
        return ("该仓库未发现标准 Skill 结构（无 SKILL.md / skill.json），已中止安装。"
                "可用 skill_pull_inspect 查看仓库内容，确认是否为 skill 仓库。")

    if len(candidates) > 1:
        lines.append(f"该仓库是技能合集：发现 {len(candidates)} 个技能，将全部安装。"
                     "如需只装一个，可用 select 参数指定相对路径。")

    # 逐个安装注册（幂等：同一来源重装直接跳过）
    used = set()
    if not force:
        for d in SKILLS_DIR.iterdir():
            if d.is_dir():
                used.add(d.name)
    installed = []
    for d in candidates:
        rel_path = d.relative_to(root).as_posix() if d != root else "."
        src_id = f"{meta['repo']}::{rel_path}"
        raw_name = ""
        om = _manifest_of(d)
        if isinstance(om, dict) and not om.get("__error__"):
            raw_name = str(om.get("name") or "").strip()
        if not raw_name or not NAME_RE.match(raw_name):
            cand = d.name.strip().lower()
            cand = re.sub(r"[^a-zA-Z0-9_-]", "_", cand).strip("_")
            raw_name = cand or meta["repo"].split("/")[-1]
        t0 = SKILLS_DIR / raw_name
        if t0.exists() and not force:
            old = _manifest_of(t0)
            old_src = ""
            if isinstance(old, dict):
                old_src = str(old.get("_pulled_from") or "") + "::" + str(old.get("_pulled_path") or "")
            if old_src == src_id:
                lines.append(f"⏭ 技能 {raw_name} 已安装（同一来源），跳过；force=true 可强制重装。")
                continue
        name = _skill_name_of(d, meta["repo"], used)
        used.add(name)
        target = SKILLS_DIR / name
        if target.exists() and not force:
            lines.append(f"⏭ 技能 {name} 已存在（skills/{name}），跳过（force=true 可覆盖）。")
            continue
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
        shutil.copytree(d, target,
                        ignore=shutil.ignore_patterns("__pycache__", ".git", "node_modules", "*.pyc"))
        m = _manifest_of(target)
        if not isinstance(m, dict):
            m = _auto_manifest(target, name, meta["repo"])
        if isinstance(m, dict) and not m.get("__error__"):
            m["_pulled_from"] = meta["repo"]
            m["_pulled_path"] = rel_path
            (target / "skill.json").write_text(
                json.dumps(m, ensure_ascii=False, indent=2), encoding="utf-8")
        installed.append((name, target, m))

    if not installed:
        lines.append("没有新安装任何技能。")
        return "\n".join(lines)

    reload_msg = _trigger_reload()
    lines.append(f"✔ 已安装 {len(installed)} 个技能，位于 skills/ 目录。{reload_msg}")
    lines.append("")
    lines.append("使用说明：")
    for name, target, m in installed:
        tools = m.get("tools") or []
        tool_names = "、".join(t["function"]["name"] for t in tools if isinstance(t, dict)
                               and (t.get("function") or {}).get("name")) or "（无函数工具，指令型）"
        md = target / "SKILL.md"
        preview = ""
        if md.exists():
            try:
                preview = "\n".join(md.read_text(encoding="utf-8", errors="replace").split("\n")[:6])
            except Exception:
                preview = ""
        lines.append(f"── {name}（title: {m.get('title', name)}；工具：{tool_names}）──")
        lines.append(f"    清单：{target / 'skill.json'}；说明书：skill_help('{name}')")
        if preview:
            lines.append("    SKILL.md 摘要：")
            lines.extend("    " + ln for ln in preview.split("\n") if ln.strip())
    lines.append("")
    lines.append("提示：静态审查通过 ≠ 绝对安全。如技能在运行中行为异常，可在 /harness 管理页禁用它。")
    return "\n".join(lines)


def _trigger_reload() -> str:
    """尝试通过本地 REST 触发热重载（多端口兜底）；失败则依赖 hot_reload 守护。"""
    for port in RELOAD_PORTS:
        try:
            _request(f"http://127.0.0.1:{port}/api/harness/reload", timeout=5,
                     headers={"Content-Type": "application/json"}, data=b"{}")
            return f"已通知本地服务（127.0.0.1:{port}）热重载，新技能即时应答。"
        except Exception:
            continue
    return ("未能直连本地服务触发重载；hot_reload 守护会在 1 秒内自动加载新技能，"
            "如仍未生效请在 /harness 管理页点击「重载」。")


# ---------------- 技能注册表 ----------------

HANDLERS = {
    "skill_pull_search": skill_pull_search,
    "skill_pull_inspect": skill_pull_inspect,
    "skill_pull_install": skill_pull_install,
}
