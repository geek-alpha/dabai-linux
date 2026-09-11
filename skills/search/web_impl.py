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
from html.parser import HTMLParser
from urllib.parse import unquote

try:
    import requests
    _HAS_REQUESTS = True
except ImportError:  # pragma: no cover
    requests = None
    _HAS_REQUESTS = False

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/125.0 Safari/537.36")
# 直连优先，失败自动走本地代理 127.0.0.1:7890（被墙/翻墙场景兜底）
_PROXIES = [None, {"http": "http://127.0.0.1:7890", "https": "http://127.0.0.1:7890"}]
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
    for proxies in _PROXIES:
        try:
            r = requests.get(url, headers={"User-Agent": _UA},
                             timeout=timeout, proxies=proxies)
            if r.status_code == 200:
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
                "--disable-dev-shm-usage", "--virtual-time-budget=8000",
                "--dump-dom", f"--user-data-dir={_CHROME_PROFILE}", url]
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


async def web_search(args: dict) -> str:
    """按关键词搜索网页：DuckDuckGo 主引擎，无结果自动换 Bing。"""
    query = str(args.get("query") or "").strip()
    if not query:
        return "错误：query 不能为空"
    max_results = max(1, min(int(args.get("max_results") or 5), 10))
    q = requests.utils.quote(query)

    # 1) DuckDuckGo
    items = []
    try:
        r = await asyncio.to_thread(_session_get, "https://html.duckduckgo.com/html/?q=" + q, 20.0)
        raw = r.text
        items = re.findall(
            r'<a[^>]*class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>.*?'
            r'<a[^>]*class="result__snippet"[^>]*>(.*?)</a>',
            raw, re.S)
    except Exception:
        items = []

    # 2) Bing 兜底
    if not items:
        try:
            r = await asyncio.to_thread(_session_get, "https://www.bing.com/search?q=" + q, 20.0)
            raw = r.text
            items = re.findall(
                r'<li class="b_algo".*?<h2><a href="([^"]+)"[^>]*>(.*?)</a>.*?'
                r'<p[^>]*>(.*?)</p>',
                raw, re.S)
        except Exception:
            items = []

    out = []
    for href, title, snip in items[:max_results]:
        title = html_mod.unescape(re.sub(r"<[^>]+>", "", title)).strip()
        snip = html_mod.unescape(re.sub(r"<[^>]+>", "", snip)).strip()
        m = re.search(r"uddg=([^&]+)", href or "")
        if m:
            href = unquote(m.group(1))
        out.append(f"{len(out) + 1}. {title}\n   {href}\n   {snip[:200]}")
    if not out:
        return f"没有搜到「{query}」的相关结果（可换关键词再试）"
    return f"搜索结果（{query}）：\n" + "\n".join(out)


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
