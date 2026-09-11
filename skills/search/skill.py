# -*- coding: utf-8 -*-
"""统一搜索技能（search）—— 四引擎合一。

合并自 4 个搜索技能：
- anysearch-skill-main（通用/垂直域/批量/URL提取，匿名可用）
- tavily-skills（LLM 优化搜索/提取/爬取/深度研究，需 tvly CLI + TAVILY_API_KEY）
- exa-skills（语义搜索/答案/相似页，需 EXA_API_KEY）
- web（DuckDuckGo+Bing 搜索/读网页/天气/翻墙代理）

工具命名规则：
- search_* 前缀 = 新引擎（anysearch/tavily/exa）
- web_search/read_web/weather_check/fq_ctl/proxy_test = 原 web 技能工具，保持原名
"""
from __future__ import annotations

import glob
import json
import os
import shutil
import subprocess
import sys

_SKILL_DIR = os.path.dirname(os.path.abspath(__file__))
if _SKILL_DIR not in sys.path:
    sys.path.insert(0, _SKILL_DIR)

# ---------- 引擎路径（跨平台探测，不再硬编码盘符） ----------
_ENGINE_DIR = os.path.join(_SKILL_DIR, "engines")


def _engine_roots() -> list:
    """引擎脚本候选根目录：环境变量 → 本技能 engines/ → 历史备份目录（兼容旧布局）。"""
    roots = []
    env = (os.environ.get("DABAI_SEARCH_ENGINES") or "").strip().strip('"')
    if env:
        roots.append(env)
    roots.append(_ENGINE_DIR)
    roots += sorted(glob.glob(os.path.join(os.path.dirname(_SKILL_DIR),
                                           "_merged_backup_*", "search_engines")))
    return roots


def _find_engine(*rel_parts: str) -> str:
    """在候选根目录里探测引擎脚本；都找不到时返回首个候选路径（报错信息里可见）。"""
    rel = os.path.join(*rel_parts)
    roots = _engine_roots()
    for r in roots:
        p = os.path.join(r, rel)
        if os.path.isfile(p):
            return p
    return os.path.join(roots[0], rel) if roots else rel


_ANYSEARCH_CLI = _find_engine("anysearch-skill-main", "scripts", "anysearch_cli.py")
_EXA_WEB_SEARCH = _find_engine("exa-skills", "exa-web-search", "scripts", "web_search.py")
_EXA_CLIENT = _find_engine("exa-skills", "_shared", "exa_client.py")
_EXA_ANSWER = _find_engine("exa-skills", "exa-web-search", "scripts", "exa_client.py")

# ---------- web 引擎（原 web 技能实现） ----------
import web_impl  # noqa: E402
import weather_impl  # noqa: E402
import fq_impl  # noqa: E402


def _run(cmd: list, timeout: int = 120) -> str:
    """跑一条命令，返回 stdout+stderr。"""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout, encoding="utf-8", errors="replace")
        out = (p.stdout or "") + (p.stderr or "")
        return out.strip() or f"(exit={p.returncode})"
    except FileNotFoundError:
        return f"命令不存在: {cmd[0]}"
    except subprocess.TimeoutExpired:
        return f"命令超时（>{timeout}s）: {' '.join(cmd[:3])}..."


def _check_file(path: str) -> str | None:
    if not os.path.isfile(path):
        return (f"引擎脚本缺失: {path}\n"
                f"该引擎脚本需自备：放到 {_ENGINE_DIR}/ 下，"
                f"或用环境变量 DABAI_SEARCH_ENGINES 指向引擎根目录。")
    return None


# ---------- anysearch 引擎 ----------
def _anysearch(args: dict, sub: str, extra: list | None = None) -> str:
    err = _check_file(_ANYSEARCH_CLI)
    if err:
        return err
    cmd = [sys.executable, _ANYSEARCH_CLI, sub]
    if extra:
        cmd += extra
    return _run(cmd)


def search_web(args: dict) -> str:
    query = str(args.get("query") or "").strip()
    if not query:
        return "请提供搜索关键词（query 参数）。"
    err = _check_file(_ANYSEARCH_CLI)
    if err:
        return err
    cmd = [sys.executable, _ANYSEARCH_CLI, "search", query]
    for flag, key in (("--domain", "domain"), ("--sub_domain", "sub_domain"),
                      ("--params", "params"), ("--zone", "zone"),
                      ("--language", "language")):
        v = args.get(key)
        if v:
            cmd += [flag, str(v)]
    mr = args.get("max_results")
    if mr:
        cmd += ["--max_results", str(mr)]
    return _run(cmd)


def search_batch(args: dict) -> str:
    queries = args.get("queries")
    qlist = args.get("query")
    if not queries and not qlist:
        return "请提供 queries（JSON 数组）或 query（多个查询串）。"
    err = _check_file(_ANYSEARCH_CLI)
    if err:
        return err
    cmd = [sys.executable, _ANYSEARCH_CLI, "batch_search"]
    if queries:
        cmd += ["--queries", str(queries)]
    if qlist:
        if isinstance(qlist, str):
            qlist = [qlist]
        for q in qlist:
            cmd += ["--query", str(q)]
    mr = args.get("max_results")
    if mr:
        cmd += ["--max_results", str(mr)]
    return _run(cmd)


def search_subdomains(args: dict) -> str:
    domain = args.get("domain")
    domains = args.get("domains")
    if not domain and not domains:
        return "请提供 domain（单个域）或 domains（批量，最多 5 个）。"
    err = _check_file(_ANYSEARCH_CLI)
    if err:
        return err
    cmd = [sys.executable, _ANYSEARCH_CLI, "get_sub_domains"]
    if domains:
        cmd += ["--domains", str(domains)]
    else:
        cmd += ["--domain", str(domain)]
    return _run(cmd)


def search_extract(args: dict) -> str:
    url = str(args.get("url") or "").strip()
    if not url:
        return "请提供要提取的 URL。"
    err = _check_file(_ANYSEARCH_CLI)
    if err:
        return err
    return _run([sys.executable, _ANYSEARCH_CLI, "extract", url])


# ---------- exa 引擎 ----------
def _exa_web_search(args: dict) -> str:
    err = _check_file(_EXA_WEB_SEARCH)
    if err:
        return err
    query = str(args.get("query") or "").strip()
    if not query:
        return "请提供查询（描述想找的页面）。"
    cmd = [sys.executable, _EXA_WEB_SEARCH, query]
    n = args.get("num")
    if n:
        cmd += ["-n", str(n)]
    cat = args.get("category")
    if cat:
        cmd += ["-c", str(cat)]
    return _run(cmd)


def _exa_client(sub: str, args: dict) -> str:
    err = _check_file(_EXA_CLIENT)
    if err:
        return err
    cmd = [sys.executable, _EXA_CLIENT, sub]
    if sub == "contents":
        url = str(args.get("url") or "").strip()
        if not url:
            return "请提供要读取的 URL。"
        cmd += [url, "--text"]
    elif sub == "answer":
        q = str(args.get("question") or args.get("query") or "").strip()
        if not q:
            return "请提供要回答的问题。"
        cmd += [q]
    elif sub == "similar":
        url = str(args.get("url") or "").strip()
        if not url:
            return "请提供参考页面 URL。"
        cmd += [url]
        n = args.get("num")
        if n:
            cmd += ["-n", str(n)]
    return _run(cmd)


def search_exa(args: dict) -> str:
    return _exa_web_search(args)


def search_exa_answer(args: dict) -> str:
    return _exa_client("answer", args)


def search_exa_similar(args: dict) -> str:
    return _exa_client("similar", args)


# ---------- tavily 引擎 ----------
def _tvly(args: list, timeout: int = 180) -> str:
    tvly = shutil.which("tvly")
    if not tvly:
        return ("tvly CLI 未安装。安装：pip install tavily-cli（或 uv tool install tavily-cli），"
                "然后 tvly login --api-key tvly-xxx 配置 TAVILY_API_KEY。")
    return _run([tvly] + args, timeout=timeout)


def search_tavily(args: dict) -> str:
    query = str(args.get("query") or "").strip()
    if not query:
        return "请提供搜索关键词（query 参数）。"
    cmd = ["search", query, "--json"]
    depth = args.get("depth")
    if depth:
        cmd += ["--depth", str(depth)]
    tr = args.get("time_range")
    if tr:
        cmd += ["--time-range", str(tr)]
    dom = args.get("include_domains")
    if dom:
        cmd += ["--include-domains", str(dom)]
    mr = args.get("max_results")
    if mr:
        cmd += ["--max-results", str(mr)]
    return _tvly(cmd)


def search_tavily_extract(args: dict) -> str:
    url = str(args.get("url") or "").strip()
    if not url:
        return "请提供要提取的 URL。"
    return _tvly(["extract", url, "--json"])


def search_tavily_research(args: dict) -> str:
    topic = str(args.get("topic") or "").strip()
    if not topic:
        return "请提供研究主题（topic 参数）。"
    cmd = ["research", topic]
    model = args.get("model")
    if model:
        cmd += ["--model", str(model)]
    return _tvly(cmd, timeout=300)


# ---------- web 引擎（原样转发，web_impl 是 async 协程） ----------
async def web_search(args: dict) -> str:
    return await web_impl.web_search(args)


async def read_web(args: dict) -> str:
    return await web_impl.read_web(args)


def weather_check(args: dict) -> str:
    return weather_impl.check_weather(args)


def fq_ctl(args: dict) -> str:
    return fq_impl.fq_ctl(args)


def proxy_test(args: dict) -> str:
    return fq_impl.proxy_test(args)


HANDLERS = {
    "search_web": search_web,
    "search_batch": search_batch,
    "search_subdomains": search_subdomains,
    "search_extract": search_extract,
    "search_exa": search_exa,
    "search_exa_answer": search_exa_answer,
    "search_exa_similar": search_exa_similar,
    "search_tavily": search_tavily,
    "search_tavily_extract": search_tavily_extract,
    "search_tavily_research": search_tavily_research,
    "web_search": web_search,
    "read_web": read_web,
    "weather_check": weather_check,
    "fq_ctl": fq_ctl,
    "proxy_test": proxy_test,
}
