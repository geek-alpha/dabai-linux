# -*- coding: utf-8 -*-
"""联网搜索与网页深挖 —— 大白的基础能力。

- web_search：多引擎（DuckDuckGo 主 + Bing 兜底），直连失败自动走本地代理；
- read_web：可读文本 / 链接清单 / 标题大纲 / 表格 / 原始 HTML 五种挖法，
  复杂或 JS 渲染页面自动用无头 Chrome 抓取真实 DOM，支持站内关键词定位。
"""
from __future__ import annotations

import asyncio
import html as html_mod
import os
import glob
import re
import shutil
import subprocess
import tempfile
import time
from html.parser import HTMLParser
from urllib.parse import unquote, urlsplit

try:
    import requests
    _HAS_REQUESTS = True
except ImportError:  # pragma: no cover
    requests = None
    _HAS_REQUESTS = False

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/125.0 Safari/537.36")
# 直连优先；本地代理（127.0.0.1:7890）只在确实监听时才作为兜底尝试。
# 原实现无条件把代理排进候选链：代理没起时每次失败都要再白等一轮 connect 超时。
_PROXY_URL = "http://127.0.0.1:7890"

# 直连必失败的墙外域名（实测：直连 000 / DNS 投毒）。命中则代理优先，
# 不再先白等一轮直连超时——这是搜索慢的主因。
_PROXY_FIRST_HOSTS = (
    # bing.com 也在内：国内 IP 直连会被降级成「主关键词」宽泛结果
    # （实测搜 python asyncio tutorial 只给 python.org 首页），走代理才是真实排序。
    "bing.com",
    "brave.com",
    "google.com", "gstatic.com", "googleapis.com", "googlevideo.com",
    "youtube.com", "ytimg.com", "duckduckgo.com", "wikipedia.org",
    "wikimedia.org", "arxiv.org", "huggingface.co", "hf.co",
    "githubusercontent.com", "twitter.com", "x.com", "openai.com",
    "anthropic.com", "reddit.com", "medium.com", "semanticscholar.org",
    "sciencedirect.com", "springer.com", "nature.com", "quora.com",
    "telegram.org", "discord.com",
)


def _needs_proxy(url: str) -> bool:
    """目标域名是否属于墙外（后缀精确匹配，避免 x.com 误伤 v2x.com 这类）。"""
    try:
        host = (urlsplit(url).hostname or "").lower()
    except Exception:  # noqa: BLE001
        return False
    return bool(host) and any(host == h or host.endswith("." + h)
                              for h in _PROXY_FIRST_HOSTS)


def _proxy_alive(host: str = "127.0.0.1", port: int = 7890, timeout: float = 0.4) -> bool:
    """探测本地代理端口是否真的在监听（0.4s 连不上即视为不可用）。"""
    import socket
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _proxy_candidates(url: str = "") -> list:
    """返回本次请求的代理候选链。

    墙外域名代理优先且不兜底直连（直连必失败，兜底只会白等一轮 6s 连接超时）；
    其余直连优先、代理兜底。代理未监听时一律直连，不引入额外超时。
    """
    if not _proxy_alive():
        return [None]
    proxy = {"http": _PROXY_URL, "https": _PROXY_URL}
    if url and _needs_proxy(url):
        return [proxy]
    return [None, proxy]
def _find_chrome() -> str:
    """定位可用的 Chrome/Chromium（跨平台）：环境变量 DABAI_CHROME → 常见路径 → PATH。"""
    env = (os.environ.get("DABAI_CHROME") or "").strip().strip('"')
    if env and os.path.isfile(env):
        return env
    cands = []
    if os.name == "nt":
        cands += [
            r"D:\AI\Chrome141_AllNew_2025.10.3\App\chrome.exe",
            os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
            os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
            os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
            os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"),
        ]
        cands += sorted(glob.glob(r"C:\Program Files*\Google\Chrome\Application\chrome.exe"))
    else:
        for name in ("google-chrome", "google-chrome-stable", "chromium",
                     "chromium-browser", "msedge", "chrome"):
            p = shutil.which(name)
            if p:
                cands.append(p)
        cands += ["/usr/bin/google-chrome", "/usr/bin/chromium", "/snap/bin/chromium",
                  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"]
    for c in cands:
        if c and os.path.isfile(c):
            return c
    return ""


# 无头 Chrome：渲染 JS 页面后导出真实 DOM（找不到时该能力自动降级为 requests 抓取）
_CHROME = _find_chrome()
_CHROME_PROFILE = os.path.join(tempfile.gettempdir(), "dabai_chrome_profile")
_SKIP_TAGS = ("script", "style", "noscript", "svg", "head", "template", "iframe")


def _session_get(url: str, timeout: float = 20.0):
    """带 UA 的 GET，直连失败自动换本地代理重试。"""
    if not _HAS_REQUESTS:
        raise RuntimeError("requests 未安装")
    last = None
    # connect 超时压到 6s：被墙的直连会在 TCP/TLS 阶段挂死，不压短就是白等。
    tmo = (min(6.0, float(timeout)), float(timeout))
    for proxies in _proxy_candidates(url):
        try:
            r = requests.get(url, headers={"User-Agent": _UA},
                             timeout=tmo, proxies=proxies)
            # 2xx 而非仅 200：DDG 对爬虫正常返回 202，只认 200 会把它误判为失败。
            if 200 <= r.status_code < 300:
                return r
            last = RuntimeError(f"HTTP {r.status_code}")
        except Exception as e:
            last = e
    raise last


def _chrome_dom(url: str, timeout: float = 30.0) -> str:
    """无头 Chrome 渲染页面后导出 DOM（JS 渲染/复杂页面的深挖手段）。"""
    if not os.path.isfile(_CHROME):
        return ""
    try:
        os.makedirs(_CHROME_PROFILE, exist_ok=True)
        # 注意：便携版 Chrome 必须带 --portable 才会尊重 --user-data-dir，
        # 否则会转发给正在运行的浏览器实例并静默退出；系统安装的 Chrome 不带该参数。
        cmd = [_CHROME]
        if "Chrome141_AllNew" in _CHROME:
            cmd.append("--portable")
        cmd += ["--headless=new", "--disable-gpu", "--no-sandbox", "--no-first-run",
                "--no-default-browser-check", "--disable-extensions",
                "--disable-dev-shm-usage", "--virtual-time-budget=8000"]
        # 墙外页面必须让浏览器自己也走代理，否则 Chrome 直连照样拿不到 DOM。
        if _proxy_alive() and _needs_proxy(url):
            cmd.append(f"--proxy-server={_PROXY_URL}")
        cmd += ["--dump-dom", f"--user-data-dir={_CHROME_PROFILE}", url]
        p = subprocess.run(cmd, capture_output=True, timeout=timeout)
        return p.stdout.decode("utf-8", "replace")
    except Exception:
        return ""


def _decode(resp) -> str:
    try:
        return resp.content.decode(resp.encoding or "utf-8", errors="replace")
    except Exception:
        return resp.text


class _TextParser(HTMLParser):
    """把 HTML 抽成段落文本（跳过 script/style/head 等噪音）。"""

    def __init__(self):
        super().__init__()
        self._skip = 0
        self._pending = None
        self.lines = []

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP_TAGS:
            self._skip += 1
            return
        if self._skip:
            return
        if tag in ("p", "div", "li", "br", "section", "article", "tr", "pre",
                   "h1", "h2", "h3", "h4", "h5", "h6", "blockquote"):
            self._flush()

    def handle_endtag(self, tag):
        if tag in _SKIP_TAGS:
            if self._skip:
                self._skip -= 1
            return
        if self._skip:
            return
        if tag in ("p", "div", "li", "br", "section", "article", "tr", "pre",
                   "h1", "h2", "h3", "h4", "h5", "h6", "blockquote"):
            self._flush()

    def handle_data(self, data):
        if self._skip:
            return
        t = data.strip()
        if t:
            self._pending = (self._pending + " " + t) if self._pending else t

    def _flush(self):
        if self._pending:
            self.lines.append(self._pending)
            self._pending = None


def _to_text(html_text: str, max_chars: int = 6000) -> str:
    p = _TextParser()
    try:
        p.feed(html_text)
    except Exception:
        pass
    body = "\n".join(p.lines)
    # 表格也以可读形式附在正文后（复杂结构化数据不被丢掉）
    tables = _extract_tables(html_text)
    if tables:
        body += "\n\n【表格】\n" + "\n\n".join(tables)
    return re.sub(r"\n{3,}", "\n\n", body)[:max_chars]


def _extract_tables(html_text: str, max_rows: int = 15, max_tables: int = 5) -> list:
    out = []
    for tm in re.finditer(r"<table[^>]*>(.*?)</table>", html_text, re.S | re.I):
        rows = []
        for rm in re.finditer(r"<tr[^>]*>(.*?)</tr>", tm.group(1), re.S | re.I):
            cells = [re.sub(r"<[^>]+>", " ", c).strip()
                     for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>",
                                         rm.group(1), re.S | re.I)]
            if cells:
                rows.append(" | ".join(cells))
        if rows:
            out.append("\n".join(rows[:max_rows]))
        if len(out) >= max_tables:
            break
    return out


def _extract_links(html_text: str, max_links: int = 40) -> list:
    out = []
    for m in re.finditer(r'<a[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>',
                         html_text, re.S | re.I):
        href = html_mod.unescape(m.group(1))
        text = html_mod.unescape(re.sub(r"<[^>]+>", "", m.group(2))).strip()
        if not text or href.startswith(("javascript:", "#", "mailto:")):
            continue
        item = f"{text[:70]} -> {href}"
        if item not in out:
            out.append(item)
        if len(out) >= max_links:
            break
    return out


def _extract_headings(html_text: str) -> list:
    out = []
    for m in re.finditer(r"<(h[1-6])[^>]*>(.*?)</h\1>", html_text, re.S | re.I):
        lvl = int(m.group(1)[1])
        text = html_mod.unescape(re.sub(r"<[^>]+>", "", m.group(2))).strip()
        if text:
            out.append("  " * (lvl - 1) + "- " + text)
    return out


def _page_title(html_text: str, url: str) -> str:
    m = re.search(r"<title[^>]*>(.*?)</title>", html_text, re.S | re.I)
    return html_mod.unescape(m.group(1)).strip()[:120] if m else url


# ---------- 搜索引擎链（按本机实测可用性排序）----------
# 实测（树莓派 aarch64 / Debian 13 / 国内直连）：
#   DuckDuckGo 全站不可达（html. 与 lite. 均连接超时）；cn.bing.com 正常，0.2s 返回 10 条。
#   故 Bing 置主引擎、DDG 降为兜底；失败原因全程留痕（不再静默吞异常导致「没搜到」误报）。
_BING_PAT = re.compile(
    r'<li[^>]*class="b_algo".*?'
    r'<h2[^>]*>\s*<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>.*?'
    r'<p\b[^>]*>(.*?)</p>', re.S)
_DDG_PAT = re.compile(
    r'<a[^>]*class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>.*?'
    r'<a[^>]*class="result__snippet"[^>]*>(.*?)</a>', re.S)
_DDG_LITE_PAT = re.compile(
    r'<a[^>]*class="result-link"[^>]*href="([^"]+)"[^>]*>(.*?)</a>.*?'
    r'<td[^>]*class="result-snippet"[^>]*>(.*?)</td>', re.S)

# (引擎名, URL 模板, 解析器, 单次超时秒)
# 前两个并行主力，其余降级兜底。实测（2026-09-11）：
#   bing-rss 是唯一稳定拿到「真实排序」的通道——HTML 页在爬虫模式下会被降级；
#   ddg 质量最好但公共出口 IP 会被反爬挑战页拦截（保留为机会型副引擎）。
def _parse_bing_rss(text: str) -> list:
    """Bing RSS 通道（format=rss）：主引擎。

    HTML 页在爬虫模式下会被降级成「主关键词」宽泛结果（搜 asyncio 教程只返回
    python.org 首页），RSS 通道返回的才是真实排序；且 XML 结构稳定，不受前端改版影响。
    """
    out = []
    for item in re.findall(r"<item>(.*?)</item>", text, re.S):
        ti = re.search(r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", item, re.S)
        li = re.search(r"<link>(.*?)</link>", item, re.S)
        de = re.search(r"<description>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</description>", item, re.S)
        if not (ti and li):
            continue
        url = li.group(1).strip()
        if not url.startswith("http"):
            continue
        out.append((_clean_text(ti.group(1)), url,
                    _clean_text(de.group(1))[:200] if de else ""))
    return out


def _regex_parser(pat, ddg: bool = False):
    """把「正则命中」包装成与 _parse_bing_rss 同签名的解析器。"""
    def _parse(text: str) -> list:
        return _hits_structured(pat.findall(text), ddg=ddg, limit=12)
    return _parse


def _parse_brave(text: str) -> list:
    """Brave Search：能正确理解 sing-box 这类连字符词的免费通道（实测）。

    Bing 把 `sing-box` 里的 `-box` 当排除运算符，搜「sing-box hysteria2 config」
    返回电影《Sing》；Brave 同一条查询直接命中 sing-box 官方文档。
    Svelte SSR 结构：<div class="snippet" data-type="web"> 内 <a href> 带 URL 与标题，
    紧随其后的 generic-snippet 是摘要。
    """
    out = []
    for blk in re.split(r'<div class="snippet[^"]*"[^>]*data-type="web"', text)[1:]:
        m = re.search(r'<a href="(https?://[^"]+)"[^>]*>.*?'
                      r'<div class="title search-snippet-title[^"]*"[^>]*>(.*?)</div>',
                      blk, re.S)
        if not m:
            continue
        url, title = m.group(1), _clean_text(m.group(2))
        dm = re.search(r'<div class="content [^"]*"[^>]*>(.*?)</div>', blk, re.S)
        snip = _clean_text(dm.group(1))[:200] if dm else ""
        if title and url:
            out.append((title, url, snip))
    return out


# 引擎顺序即排序权重（round-robin 合并按此交错，第一个引擎的 0 号结果排第一）。
# 实测：Brave 精准（技术词、连字符、多词全部命中），Bing RSS 只认「第一个词」——
# 搜 sing-box hysteria2 config 返回电影《Sing》，加引号与 site: 限定均无效。
# 所以 Brave 主、Bing 副（中文查询 Bing 分词语义正常，仍有价值）。
_ENGINES = [
    ("brave", "https://search.brave.com/search?q={q}",
     _parse_brave, 12.0),
    ("bing-rss", "https://www.bing.com/search?q={q}&format=rss&count=20",
     _parse_bing_rss, 12.0),
    ("ddg", "https://html.duckduckgo.com/html/?q={q}",
     _regex_parser(_DDG_PAT, ddg=True), 12.0),
    ("bing-cn", "https://cn.bing.com/search?q={q}&setlang=zh-CN",
     _regex_parser(_BING_PAT), 12.0),
]


# 引擎熔断：公共搜索通道会按 IP 限流（Brave/DDG 实测），不熔断的话
# 每次搜索都要白等一轮连接超时。成功一次即解除。
_COOLDOWN = {}
_COOLDOWN_SEC = 300


# Bing 把查询里的连字符当「排除运算符」：搜 sing-box 被理解成「sing 且不含 box」，
# 于是返回电影《Sing》——实测前 3 条全是噪音，官方文档被挤到第 4 位之后。
# 技术词（sing-box / asyncio-timeout / x86-64）必须加引号锁成短语。
_HYPHEN_TOKEN = re.compile(r'(?<![\w"-])([A-Za-z0-9][\w.+]*(?:-[\w.+]+)+)(?![\w"-])')
_STOP = {"the", "a", "an", "of", "and", "or", "for", "to", "in", "on", "vs", "with"}


def _tokens(query: str) -> list:
    """查询分词：去引号、小写、丢停用词与单字符（中文单字保留）。"""
    raw = re.sub(r'["\']', " ", query).lower()
    out = []
    for t in re.split(r"[^\w\u4e00-\u9fff.+]+", raw):
        if not t or t in _STOP:
            continue
        if len(t) > 1 or "\u4e00" <= t[0] <= "\u9fff":
            out.append(t)
    return out


def _shape_query(engine: str, query: str) -> str:
    """按引擎语法微调查询串：Bing 系给含连字符的技术词加引号，其余引擎原样透传。"""
    if not engine.startswith("bing") or '"' in query:
        return query
    return _HYPHEN_TOKEN.sub(lambda m: '"' + m.group(1) + '"', query)


def _relevance_ok(query: str, title: str, snip: str, min_ratio: float = 0.4) -> bool:
    """宽泛引擎的相关性闸门：命中词占比过低 = 降级结果，丢弃。

    实测 Bing RSS 只按首词检索，搜 sing-box hysteria2 config 返回电影《Sing》——
    标题里只有 sing 命中，4 个词命中 1 个，明显是降级产物。Brave 不过闸门（它可信）。
    """
    toks = _tokens(query)
    if len(toks) < 2:  # 单关键词查询无从判别，放行
        return True
    blob = (title + " " + snip).lower()
    return sum(1 for t in toks if t in blob) / len(toks) >= min_ratio


def _as_int(v, default: int) -> int:
    """容错取整：调用方（LLM/上层）可能传 'abc'、None、''，不能让搜索因此崩掉。"""
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _cooled(name: str) -> bool:
    return time.time() < _COOLDOWN.get(name, 0)


def _mark_fail(name: str) -> None:
    _COOLDOWN[name] = time.time() + _COOLDOWN_SEC


def _clean_text(s: str) -> str:
    """去标签 + 反转义 + 压空白（Bing 标题内嵌 <strong> 高亮、摘要带 &ensp;）。"""
    t = html_mod.unescape(re.sub(r"<[^>]+>", "", s or ""))
    return re.sub(r"\s+", " ", t).strip()


def _hits_structured(hits: list, ddg: bool = False, limit: int = 10) -> list:
    """把正则命中规整成 (title, url, snippet) 三元组，按 URL 去重。"""
    out, seen = [], set()
    for href, title, snip in hits:
        title, snip = _clean_text(title), _clean_text(snip)
        if not title or not href:
            continue
        if ddg:
            m = re.search(r"uddg=([^&]+)", href)
            href = unquote(m.group(1)) if m else href
        if href.startswith("//"):
            href = "https:" + href
        if href in seen:
            continue
        seen.add(href)
        out.append((title, href, snip[:200]))
        if len(out) >= limit:
            break
    return out


def _format_hits(hits: list, max_results: int, ddg: bool = False) -> list:
    """把正则命中格式化成结果行（保留：供其它调用方复用）。"""
    return [f"{i}. {t}\n   {u}\n   {s}"
            for i, (t, u, s) in enumerate(
                _hits_structured(hits, ddg=ddg, limit=max_results), 1)]


async def web_search(args: dict) -> str:
    """按关键词搜索网页：主力引擎并行合并去重，失败自动降级到剩余引擎。"""
    query = str(args.get("query") or "").strip()
    if not query:
        return "错误：query 不能为空"
    max_results = max(1, min(_as_int(args.get("max_results"), 5), 10))

    async def run(engine):
        name, tpl, parser, timeout = engine
        if _cooled(name):  # 熔断：近期连续失败，本次直接跳过，不白等连接超时
            return name, [], f"{name}: 熔断中（近期连续失败，5 分钟后自动重试）"
        url = tpl.format(q=requests.utils.quote(_shape_query(name, query)))
        try:
            r = await asyncio.to_thread(_session_get, url, timeout)
        except Exception as e:  # noqa: BLE001
            _mark_fail(name)
            return name, [], f"{name}: {type(e).__name__} {str(e)[:70]}"
        try:
            items = parser(r.text)[:max_results]
        except Exception as e:  # noqa: BLE001
            _mark_fail(name)
            return name, [], f"{name}: 解析异常 {type(e).__name__} {str(e)[:50]}"
        if not items:
            return name, [], f"{name}: 返回页面但无结果（HTTP {r.status_code}, {len(r.text)} 字节）"
        if name.startswith("bing"):  # 宽泛引擎过相关性闸门（全滤掉=该引擎降级，不给噪音）
            items = [it for it in items if _relevance_ok(query, it[0], it[2])]
            if not items:
                return name, [], f"{name}: 结果全为降级噪音（{len(r.text)} 字节页面里无相关性命中）"
        _COOLDOWN.pop(name, None)
        return name, items, ""

    # 主引擎并行：Bing RSS 快而广、Brave 精而准，单引擎必然偏科
    # （实测 Bing 搜「sing-box hysteria2」返回电影《Sing》，Brave 直中官方文档）。
    merged, seen, used, diag = [], set(), [], []
    pools = []
    for name, items, err in await asyncio.gather(*[run(e) for e in _ENGINES[:2]]):
        if err:
            diag.append(err)
            continue
        used.append(name)
        pools.append(items)

    # 交错合并（round-robin）：Bing 广、Brave 准，顺序拼接会让 Bing 的宽泛结果把
    # Brave 的精准命中挤出 max_results（实测搜 sing-box 时前 3 条全是《Sing》电影）。
    for i in range(max((len(p) for p in pools), default=0)):
        for pool in pools:
            if i >= len(pool):
                continue
            title, href, snip = pool[i]
            if href in seen:
                continue
            seen.add(href)
            merged.append((title, href, snip))

    if not merged:  # 主力全挂/全空 → 逐个试剩余引擎
        for engine in _ENGINES[2:]:
            name, items, err = await run(engine)
            if err:
                diag.append(err)
                continue
            used.append(name)
            merged = items
            break

    if not merged:
        return (f"没有搜到「{query}」的相关结果。\n引擎诊断：\n"
                + "\n".join("  - " + d for d in diag)
                + "\n（可换关键词重试，或用 read_web / search_extract 直接读指定 URL）")

    out = [f"{i}. {t}\n   {u}\n   {s}"
           for i, (t, u, s) in enumerate(merged[:max_results], 1)]
    src = "（引擎：" + " + ".join(used) + "）"
    if diag:
        src += "｜降级记录：" + "；".join(diag)
    return f"搜索结果（{query}）{src}：\n" + "\n".join(out)


async def read_web(args: dict) -> str:
    """读网页并深挖：text/links/headings/tables/html 五种模式 + JS 渲染 + 站内定位。"""
    url = str(args.get("url") or "").strip()
    if not url:
        return "错误：url 不能为空"
    if not url.startswith(("http://", "https://")):
        return "错误：url 需要以 http:// 或 https:// 开头"
    mode = str(args.get("mode") or "text").strip().lower()
    keyword = str(args.get("keyword") or "").strip()
    js = str(args.get("js") or "auto").strip().lower()
    max_chars = max(500, min(int(args.get("max_chars") or 3000), 8000))

    # 1) 普通抓取
    html_text = ""
    used_chrome = False
    try:
        r = await asyncio.to_thread(_session_get, url, 20.0)
        html_text = _decode(r)
    except Exception:
        html_text = ""

    # 2) JS 渲染：显式要求，或普通抓取内容太少时，用无头 Chrome 导出真实 DOM
    want_js = js == "true" or (js != "false" and len(_to_text(html_text, 8000)) < 300)
    if want_js:
        dom = await asyncio.to_thread(_chrome_dom, url)
        # 显式 js=true 时只要拿到 DOM 就用；auto 时要求内容确实更丰富才替换
        if dom and (js == "true" or len(_to_text(dom, 8000)) >= 300):
            html_text = dom
            used_chrome = True

    if not html_text.strip():
        return f"读取网页失败：{url}（直连和代理都拿不到内容）"

    title = _page_title(html_text, url)
    head = f"{title}\n来源：{url}" + ("（已用无头 Chrome 渲染 JS）" if used_chrome else "")

    if mode == "links":
        links = _extract_links(html_text)
        return head + "\n\n链接清单（" + str(len(links)) + " 条）：\n" + "\n".join(links) if links \
            else head + "\n\n（页面里没有可读链接）"
    if mode == "headings":
        hs = _extract_headings(html_text)
        return head + "\n\n标题大纲：\n" + "\n".join(hs) if hs else head + "\n\n（没有标题结构）"
    if mode == "tables":
        ts = _extract_tables(html_text)
        return head + "\n\n表格（" + str(len(ts)) + " 张）：\n" + "\n\n".join(ts) if ts \
            else head + "\n\n（页面里没有表格）"
    if mode == "html":
        return head + "\n\n原始 HTML（前 " + str(max_chars) + " 字符）：\n" + html_text[:max_chars]

    # 默认 text：正文 + 表格（keyword 搜索跑在全文上，最后再截断）
    full_body = _to_text(html_text, 100000)
    body = full_body
    if keyword:
        lines = full_body.splitlines()
        hits = [i for i, ln in enumerate(lines) if keyword.lower() in ln.lower()]
        if hits:
            ctx = []
            for i in hits[:10]:
                lo, hi = max(0, i - 2), min(len(lines), i + 3)
                ctx.append("\n".join(lines[lo:hi]))
            body = f"站内找到 {len(hits)} 处「{keyword}」：\n" + "\n---\n".join(ctx)
        else:
            body = f"全文没有找到「{keyword}」（可换关键词或 mode=links 看链接）"
    return head + "\n\n" + body[:max_chars]


HANDLERS = {
    "web_search": web_search,
    "read_web": read_web,
}
