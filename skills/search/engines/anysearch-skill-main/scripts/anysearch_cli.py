#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""anysearch_cli.py —— 统一搜索 CLI（anysearch 契约兼容，本地实现）

背景
----
原 anysearch-skill-main 引擎脚本丢失（skills/search/engines/ 目录整体不存在），
导致 search_web / search_batch / search_subdomains / search_extract 四个工具
一律返回「引擎脚本缺失」。本脚本按 skills/search/skill.py 期望的 CLI 契约重新实现：

    anysearch_cli.py search <query> [--domain D] [--sub_domain S] [--params P]
                                [--zone cn|intl] [--language L] [--max_results N]
    anysearch_cli.py batch_search [--queries JSON] [--query Q ...] [--max_results N]
    anysearch_cli.py get_sub_domains [--domain D] [--domains D1,D2]
    anysearch_cli.py extract <url>

实现原则（第一性原理）
----------------------
「垂直域搜索」的本质不是换个关键词去通用搜索引擎搜，而是**直连该域的结构化数据源**。
故本实现分两层：
  1) 有公开 API 的域 → 直接查 API（code=GitHub、academic=OpenAlex/Crossref、
     finance=新浪/腾讯行情+东财快讯、news=Bing News RSS）；
  2) 没有 API 的域 → 通用多引擎（web_impl，Bing 主引擎）+ 站点限定降级。
所有可用性结论均来自本机实测，不臆造。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from functools import partial
from html.parser import HTMLParser

_HERE = os.path.dirname(os.path.abspath(__file__))
# scripts → anysearch-skill-main → engines → skills/search
_SKILL_DIR = os.path.abspath(os.path.join(_HERE, os.pardir, os.pardir, os.pardir))
if _SKILL_DIR not in sys.path:
    sys.path.insert(0, _SKILL_DIR)

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None

import web_impl  # noqa: E402  （复用其多引擎链 / 抓取 / 正文提取）

_UA = {
    "User-Agent": ("Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}
_TIMEOUT = 15.0


# ---------------------------------------------------------------- 基础工具
def _clean(s) -> str:
    t = re.sub(r"<[^>]+>", "", str(s or ""))
    t = t.replace("&nbsp;", " ").replace("&amp;", "&").replace("&quot;", '"')
    t = t.replace("&lt;", "<").replace("&gt;", ">").replace("&#39;", "'")
    return re.sub(r"\s+", " ", t).strip()


def _get(url: str, params: dict | None = None, headers: dict | None = None,
         timeout: float = _TIMEOUT, referer: str | None = None):
    """带 UA 的 GET；直连失败时自动走本地代理（若在监听）。"""
    if requests is None:
        raise RuntimeError("requests 未安装")
    h = dict(_UA)
    if referer:
        h["Referer"] = referer
    if headers:
        h.update(headers)
    last = None
    for proxies in web_impl._proxy_candidates(url):
        try:
            # connect 超时压到 6s：被墙域名直连会挂到超时，不压短就是白等。
            r = requests.get(url, params=params, headers=h,
                             timeout=(min(6.0, float(timeout)), float(timeout)),
                             proxies=proxies)
            # 2xx 而非仅 200：DDG 等站点对爬虫会返回 202，只认 200 会误判为失败。
            if 200 <= r.status_code < 300:
                return r
            last = RuntimeError(f"HTTP {r.status_code}")
        except Exception as e:  # noqa: BLE001
            last = e
    raise last if last else RuntimeError("请求失败")


def _parse_params(raw: str | None) -> dict:
    """params 支持两种写法：JSON（{"type":"stock","symbol":"AAPL"}）或 key=value,key2=value2。"""
    if not raw:
        return {}
    s = str(raw).strip()
    if not s:
        return {}
    if s.startswith("{"):
        try:
            d = json.loads(s)
            return d if isinstance(d, dict) else {}
        except Exception:  # noqa: BLE001
            return {}
    out = {}
    for part in re.split(r"[,;]", s):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def _items(lines: list, query: str, engine: str, n: int) -> str:
    """统一输出格式：标题 / 链接 / 摘要。"""
    if not lines:
        return f"没有搜到「{query}」的相关结果（引擎：{engine}）。"
    return (f"搜索结果（{query}）｜引擎：{engine}｜共 {len(lines[:n])} 条：\n"
            + "\n".join(lines[:n]))


def _fmt(i: int, title: str, url: str, desc: str = "") -> str:
    title, desc = _clean(title), _clean(desc)
    s = f"{i}. {title}\n   {url}"
    if desc:
        s += f"\n   {desc[:220]}"
    return s


# ---------------------------------------------------------------- general 域
def _h_general(query: str, params: dict, zone: str, lang: str, n: int) -> str:
    """通用域：走 web_impl 多引擎链（Bing 主引擎，实测可用）。"""
    args = {"query": query, "max_results": n}
    out = asyncio.run(web_impl.web_search(args))
    if zone == "intl":
        out = out.replace("bing-cn", "bing-cn(zh)")
    return out


# ---------------------------------------------------------------- code 域
def _h_code(query: str, params: dict, zone: str, lang: str, n: int) -> str:
    """代码域：GitHub 仓库/话题检索（api.github.com，免认证，实测 0.8s）。

    注：GitHub code search API 需认证（实测 401），故本域只做仓库级检索；
    需要看具体代码请用 search_extract 直接读仓库页面。
    """
    sub = (params.get("sub") or params.get("type") or "repo").lower()
    lang_filter = params.get("language") or params.get("lang")
    sort = params.get("sort") or ("stars" if sub == "repo" else "best-match")
    q = query
    if lang_filter:
        q += f" language:{lang_filter}"
    if sub == "topic":
        q = f"topic:{query}"
    try:
        r = _get("https://api.github.com/search/repositories",
                 {"q": q, "per_page": min(n, 30), "sort": sort})
        data = r.json()
    except Exception as e:  # noqa: BLE001
        # 降级：通用搜索 + site:github.com
        return (_h_general(f"{query} site:github.com", params, zone, lang, n)
                + f"\n（GitHub API 不可用：{type(e).__name__} {str(e)[:60]}，已降级为站点限定搜索）")
    lines = []
    for it in (data.get("items") or [])[:n]:
        desc = (f"★{it.get('stargazers_count', 0)} · {it.get('language') or '-'} · "
                f"更新 {str(it.get('pushed_at') or '')[:10]}\n   "
                f"{(it.get('description') or '').strip()}")
        lines.append(_fmt(len(lines) + 1, it.get("full_name") or "", it.get("html_url") or "", desc))
    return _items(lines, query, "GitHub repositories API", n)


# ---------------------------------------------------------------- academic 域
def _h_academic(query: str, params: dict, zone: str, lang: str, n: int) -> str:
    """学术域：OpenAlex（免 key，实测可用）为主，Crossref 兜底。

    注：arXiv API 与 Semantic Scholar 在本机实测不可用（连接超时 / 429 限流），故不作主源。
    """
    sub = (params.get("sub") or "paper").lower()
    try:
        r = _get("https://api.openalex.org/works",
                 {"search": query, "per-page": min(n, 25), "mailto": "dabai@localhost"})
        data = r.json()
        lines = []
        for w in (data.get("results") or [])[:n]:
            title = w.get("title") or w.get("display_name") or ""
            doi = w.get("doi") or ""
            url = doi or (w.get("primary_location") or {}).get("landing_page_url") or \
                f"https://openalex.org/{w.get('id', '').split('/')[-1]}"
            year = w.get("publication_year") or ""
            cited = w.get("cited_by_count", 0)
            authors = ", ".join((a.get("author") or {}).get("display_name", "")
                                for a in (w.get("authorships") or [])[:3])
            lines.append(_fmt(len(lines) + 1, f"{title}（{year}）", url,
                              f"被引 {cited} · {authors}"))
        if lines:
            return _items(lines, query, "OpenAlex", n)
    except Exception:  # noqa: BLE001
        pass
    # 兜底 Crossref
    try:
        r = _get("https://api.crossref.org/works", {"query": query, "rows": min(n, 20)})
        items = ((r.json() or {}).get("message") or {}).get("items") or []
        lines = []
        for it in items[:n]:
            title = (it.get("title") or [""])[0]
            url = it.get("URL") or (f"https://doi.org/{it.get('DOI')}" if it.get("DOI") else "")
            year = ((it.get("issued") or {}).get("date-parts") or [[""]])[0][0]
            auth = ", ".join(f"{a.get('given','')} {a.get('family','')}".strip()
                             for a in (it.get("author") or [])[:3])
            lines.append(_fmt(len(lines) + 1, f"{title}（{year}）", url, auth))
        return _items(lines, query, "Crossref", n)
    except Exception as e:  # noqa: BLE001
        return (_h_general(f"{query} site:arxiv.org OR site:scholar.google.com", params, zone, lang, n)
                + f"\n（学术 API 均不可用：{type(e).__name__} {str(e)[:60]}，已降级为站点限定搜索）")

# ---------------------------------------------------------------- finance 域
def _norm_symbol(sym: str) -> str:
    """行情代码归一化：600519→sh600519，000001→sz000001，AAPL→gb_aapl，00700→rt_hk00700。"""
    s = str(sym or "").strip()
    if not s:
        return ""
    low = s.lower()
    if low.startswith(("sh", "sz", "bj", "gb_", "rt_hk", "hk")):
        return low
    if re.fullmatch(r"\d{6}", s):                     # A 股：6/9 开头沪市，其余深市
        return ("sh" if s[0] in "569" else "sz") + s
    if re.fullmatch(r"\d{1,5}", s):                   # 港股
        return "rt_hk" + s.zfill(5)
    if re.fullmatch(r"[A-Za-z][A-Za-z.\-]{0,5}", s):  # 美股
        return "gb_" + low
    return low


def _quote_sina(symbols: list) -> list:
    """新浪实时行情：hq.sinajs.cn（实测 0.1s，需 Referer 否则 403）。"""
    url = "https://hq.sinajs.cn/list=" + ",".join(symbols)
    r = _get(url, referer="https://finance.sina.com.cn")
    r.encoding = "gbk"
    out = []
    for line in r.text.splitlines():
        m = re.match(r'var hq_str_(\w+)="(.*)";', line.strip())
        if not m:
            continue
        code, body = m.group(1), m.group(2)
        f = body.split(",")
        if len(f) < 4 or not f[0]:
            continue
        try:
            if code.startswith("gb_"):          # 美股：名称,现价,涨跌幅,时间,涨跌额,开,高,低,...
                name, price, chg = f[0], f[1], f[2]
                extra = f"涨跌幅 {chg}% · 时间 {f[3] if len(f) > 3 else '-'}"
            elif code.startswith("rt_hk"):      # 港股：英文名,中文名,开,昨收,高,低,现价,涨跌额,涨跌幅
                name = f[1] if len(f) > 1 and f[1] else f[0]
                price = f[6] if len(f) > 6 else "-"
                chg = f[8] if len(f) > 8 else "-"
                extra = f"涨跌幅 {chg}% · 昨收 {f[3] if len(f) > 3 else '-'}"
            else:                               # A 股：名称,开,昨收,现价,高,低,...
                name, price = f[0], f[3]
                try:
                    prev = float(f[2] or 0)
                    pct = (float(price) - prev) / prev * 100 if prev else 0.0
                    chg = f"{pct:+.2f}"
                except Exception:  # noqa: BLE001
                    chg = "-"
                extra = (f"涨跌幅 {chg}% · 昨收 {f[2]} · 今开 {f[1]} · 最高 {f[4]} · 最低 {f[5]}"
                         if len(f) > 5 else "")
        except Exception:  # noqa: BLE001
            continue
        out.append(f"{name}（{code}）：{price}　{extra}")
    return out


def _h_finance(query: str, params: dict, zone: str, lang: str, n: int) -> str:
    """财经域：quote=实时行情（新浪/腾讯），news=财经快讯（东方财富）。"""
    sub = (params.get("sub") or params.get("type") or "quote").lower()
    if sub in ("quote", "stock", "price"):
        raw = params.get("symbol") or params.get("symbols") or params.get("code") or query
        syms = [_norm_symbol(x) for x in re.split(r"[,\s]+", str(raw)) if x.strip()]
        syms = [s for s in syms if s][:10]
        if not syms:
            return "请提供股票代码（params: symbol=600519 或 symbol=AAPL,00700）。"
        try:
            lines = _quote_sina(syms)
            if lines:
                return (f"实时行情（{'、'.join(syms)}）｜源：新浪财经：\n"
                        + "\n".join(f"{i+1}. {ln}" for i, ln in enumerate(lines)))
        except Exception as e:  # noqa: BLE001
            # 腾讯兜底
            try:
                r = _get("https://qt.gtimg.cn/q=" + ",".join(syms))
                r.encoding = "gbk"
                lines = []
                for line in r.text.splitlines():
                    m = re.match(r'v_(\w+)="(.*)";', line.strip())
                    if m and len(m.group(2).split("~")) > 3:
                        f = m.group(2).split("~")
                        lines.append(f"{f[1]}（{f[2]}）：{f[3]}　涨跌幅 {f[32] if len(f) > 32 else '-'}%")
                if lines:
                    return ("实时行情（腾讯财经兜底）：\n"
                            + "\n".join(f"{i+1}. {ln}" for i, ln in enumerate(lines)))
            except Exception:  # noqa: BLE001
                pass
            return (f"行情获取失败：{type(e).__name__} {str(e)[:80]}\n"
                    f"（新浪/腾讯行情接口均不可用，可换用 search_extract 读行情网页）")
    # news 子域
    try:
        r = _get("https://np-listapi.eastmoney.com/comm/web/getNewsByColumns",
                 {"client": "web", "biz": "web_news_col", "column": "350",
                  "pageSize": min(n, 20), "pageIndex": 1, "req_trace": int(time.time() * 1000)})
        data = r.json()
        arr = ((data.get("data") or {}).get("list")) or []
        lines = []
        for it in arr[:n]:
            title = it.get("title") or it.get("Art_Title") or ""
            url = it.get("url") or it.get("Art_Url") or ""
            tm = it.get("showTime") or it.get("Art_ShowTime") or ""
            lines.append(_fmt(len(lines) + 1, title, url, str(tm)))
        if lines:
            return _items(lines, query or "财经快讯", "东方财富快讯", n)
    except Exception:  # noqa: BLE001
        pass
    return _h_general(f"{query or '财经'} 快讯", params, zone, lang, n)


# ---------------------------------------------------------------- news 域
def _h_news(query: str, params: dict, zone: str, lang: str, n: int) -> str:
    """新闻域：Bing News RSS（实测可用，结构化 XML，比抓 HTML 稳）。"""
    try:
        r = _get("https://www.bing.com/news/search", {"q": query, "format": "RSS"})
        root = ET.fromstring(r.content)
        lines = []
        for it in root.iter("item"):
            title = it.findtext("title") or ""
            link = it.findtext("link") or ""
            desc = it.findtext("description") or ""
            pub = it.findtext("pubDate") or ""
            lines.append(_fmt(len(lines) + 1, title, link, f"{pub} · {_clean(desc)}"))
            if len(lines) >= n:
                break
        if lines:
            return _items(lines, query, "Bing News RSS", n)
    except Exception:  # noqa: BLE001
        pass
    return _h_general(f"{query} 最新消息", params, zone, lang, n)


# ---------------------------------------------------------------- 站点限定降级域
_SITE_HINTS = {
    "legal": "site:pkulaw.com OR site:gov.cn OR site:court.gov.cn",
    "health": "site:msdmanuals.cn OR site:who.int OR site:nhc.gov.cn",
    "security": "site:cve.org OR site:nvd.nist.gov OR site:github.com/advisories",
    "ip": "site:patents.google.com OR site:cnipa.gov.cn",
    "energy": "site:iea.org OR site:nea.gov.cn",
    "environment": "site:mee.gov.cn OR site:unep.org",
    "agriculture": "site:moa.gov.cn OR site:fao.org",
    "travel": "site:mafengwo.cn OR site:tripadvisor.com",
    "film": "site:douban.com OR site:imdb.com",
    "gaming": "site:steamcommunity.com OR site:ign.com OR site:gamersky.com",
    "business": "site:crunchbase.com OR site:sec.gov OR site:qcc.com",
    "resource": "site:github.com OR site:archive.org",
    "social_media": "site:weibo.com OR site:zhihu.com OR site:reddit.com",
}


def _h_site(query: str, params: dict, zone: str, lang: str, n: int, domain: str = "") -> str:
    """无专用 API 的域：通用多引擎 + 权威站点限定，命中更聚焦。"""
    hint = _SITE_HINTS.get(domain, "")
    q = f"{query} {hint}" if hint else query
    out = _h_general(q, params, zone, lang, n)
    if hint:
        out += f"\n（域 {domain}：无公开 API，已按权威站点限定降级检索）"
    return out

# ---------------------------------------------------------------- URL 全文提取（Markdown）
class _MdParser(HTMLParser):
    """HTML → Markdown 的轻量转换器（正文可读性优先，不做完美还原）。"""

    _SKIP = ("script", "style", "noscript", "svg", "head", "template",
             "iframe", "nav", "footer", "form", "header", "aside")

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.md: list = []
        self._skip = 0
        self._pre = 0
        self._href = None

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skip += 1
            return
        if self._skip:
            return
        a = dict(attrs)
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self.md.append("\n\n" + "#" * int(tag[1]) + " ")
        elif tag == "p":
            self.md.append("\n\n")
        elif tag in ("div", "section", "article", "blockquote", "table", "tr", "ul", "ol"):
            self.md.append("\n")
        elif tag == "br":
            self.md.append("\n")
        elif tag == "li":
            self.md.append("\n- ")
        elif tag == "pre":
            self._pre += 1
            self.md.append("\n\n```\n")
        elif tag in ("strong", "b"):
            self.md.append("**")
        elif tag in ("em", "i"):
            self.md.append("*")
        elif tag == "a":
            self._href = a.get("href")
            self.md.append("[")
        elif tag == "img":
            self.md.append(f"![{a.get('alt') or 'image'}]({a.get('src') or ''})")

    def handle_endtag(self, tag):
        if tag in self._SKIP:
            if self._skip:
                self._skip -= 1
            return
        if self._skip:
            return
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6", "p"):
            self.md.append("\n")
        elif tag == "pre":
            self._pre = max(0, self._pre - 1)
            self.md.append("\n```\n")
        elif tag in ("strong", "b"):
            self.md.append("**")
        elif tag in ("em", "i"):
            self.md.append("*")
        elif tag == "a":
            href = self._href or ""
            self.md.append(f"]({href})" if href else "]")
            self._href = None

    def handle_data(self, data):
        if self._skip:
            return
        if self._pre:
            self.md.append(data)
        else:
            t = re.sub(r"\s+", " ", data)
            if t.strip():
                self.md.append(t)


def _to_markdown(html_text: str) -> str:
    p = _MdParser()
    try:
        p.feed(html_text)
    except Exception:  # noqa: BLE001
        pass
    md = "".join(p.md)
    md = re.sub(r"\*\*\s*\*\*", "", md)          # 空粗体
    md = re.sub(r"\[\s*\]\([^)]*\)", "", md)     # 空链接
    md = re.sub(r"[ \t]+\n", "\n", md)
    return re.sub(r"\n{3,}", "\n\n", md).strip()


def _h_extract(url: str) -> str:
    """URL 全文提取 → Markdown。普通抓取内容太少时自动用无头 Chrome 渲染 JS。"""
    html_text, how = "", "直连"
    try:
        r = web_impl._session_get(url, 20.0)
        html_text = web_impl._decode(r)
    except Exception as e:  # noqa: BLE001
        html_text = ""
        how = f"抓取失败（{type(e).__name__}）"
    if len(web_impl._to_text(html_text, 8000)) < 80:
        # 只在拿不到内容或内容极少时才动用无头 Chrome（静态短页面不必付 15s 渲染代价）
        dom = web_impl._chrome_dom(url)
        if dom and len(web_impl._to_text(dom, 8000)) >= 80:
            html_text, how = dom, "无头 Chrome 渲染 JS"
    if not html_text.strip():
        return f"提取失败：{url}（直连与代理均拿不到内容，且无可用无头浏览器）"
    title = web_impl._page_title(html_text, url)
    body = _to_markdown(html_text)
    return f"# {title}\n\n来源：{url}（{how}）\n\n{body}"


# ---------------------------------------------------------------- 域注册表
_DOMAINS = {
    "general": {"subs": {"search": "通用网页搜索"}, "fn": _h_general},
    "code": {
        "subs": {"repo": "GitHub 仓库检索", "topic": "GitHub 话题检索"},
        "fn": _h_code,
        "params": {"sub": "repo|topic（默认 repo）", "language": "语言过滤，如 python",
                   "sort": "stars|forks|updated（默认 stars）"},
    },
    "academic": {
        "subs": {"paper": "论文检索"},
        "fn": _h_academic,
        "params": {"sub": "paper（默认）"},
    },
    "finance": {
        "subs": {"quote": "实时行情", "news": "财经快讯"},
        "fn": _h_finance,
        "params": {"sub": "quote|news（默认 quote）",
                   "symbol": "行情代码，逗号分隔：600519 / AAPL / 00700"},
    },
    "news": {"subs": {"search": "新闻检索（Bing News RSS）"}, "fn": _h_news},
}
for _d, _hint in _SITE_HINTS.items():
    _DOMAINS[_d] = {"subs": {"search": "站点限定检索"}, "fn": partial(_h_site, domain=_d),
                    "note": f"无公开 API，按权威站点限定降级：{_hint}"}


def _dispatch(query: str, domain: str, sub_domain: str, params: dict,
              zone: str, language: str, n: int) -> str:
    d = (domain or "general").strip().lower()
    entry = _DOMAINS.get(d)
    p = dict(params or {})
    if sub_domain:
        p["sub"] = sub_domain
    if not entry:
        return _h_site(query, p, zone, language, n, domain=d)
    return entry["fn"](query, p, zone, language, n)


def _render_subdomains(domains: list) -> str:
    """输出各域的子域与参数 schema（垂直搜索前先调用，拿到参数格式）。"""
    lines = []
    for d in domains:
        e = _DOMAINS.get(d)
        if not e:
            lines.append(f"- {d}：未注册域（搜索时自动按通用域+站点限定降级）")
            continue
        subs = "、".join(f"{k}（{v}）" for k, v in e["subs"].items())
        lines.append(f"- {d}")
        lines.append(f"    sub_domain：{subs}")
        if e.get("params"):
            for k, v in e["params"].items():
                lines.append(f"    params.{k}：{v}")
        if e.get("note"):
            lines.append(f"    说明：{e['note']}")
    head = f"可用垂直域（共 {len(_DOMAINS)} 个）：\n"
    return head + "\n".join(lines)


# ---------------------------------------------------------------- 批量搜索
def _parse_query_items(raw_queries, raw_query) -> list:
    """解析批量查询：支持 JSON 数组（含每项的 domain/sub_domain/params/max_results）或纯串。"""
    items = []
    if raw_queries:
        s = str(raw_queries).strip()
        arr = None
        if s.startswith("["):
            try:
                arr = json.loads(s)
            except Exception:  # noqa: BLE001
                arr = None
        if isinstance(arr, list):
            for it in arr:
                if isinstance(it, dict):
                    q = it.get("query") or it.get("q") or ""
                    if str(q).strip():
                        items.append({
                            "query": str(q).strip(),
                            "domain": it.get("domain") or "",
                            "sub_domain": it.get("sub_domain") or "",
                            "params": _parse_params(it.get("sub_domain_params")
                                                    or it.get("params")) or it.get("params") or {},
                            "max_results": it.get("max_results"),
                        })
                elif it:
                    items.append({"query": str(it).strip()})
        else:
            for x in re.split(r"[\n,;]+", s):
                if x.strip():
                    items.append({"query": x.strip()})
    for x in (raw_query or []):
        if str(x).strip():
            items.append({"query": str(x).strip()})
    return [i for i in items if i.get("query")][:5]


def _run_batch(items: list, default_n: int) -> str:
    from concurrent.futures import ThreadPoolExecutor

    def one(it):
        q = it["query"]
        n = int(it.get("max_results") or default_n)
        t0 = time.time()
        try:
            out = _dispatch(q, it.get("domain") or "", it.get("sub_domain") or "",
                            it.get("params") or {}, "cn", "zh-CN", n)
        except Exception as e:  # noqa: BLE001
            out = f"查询失败：{type(e).__name__} {str(e)[:80]}"
        return q, out, time.time() - t0

    with ThreadPoolExecutor(max_workers=min(5, max(1, len(items)))) as ex:
        results = list(ex.map(one, items))
    parts = []
    for q, out, dt in results:
        parts.append(f"───── 查询：{q}（{dt:.1f}s）─────\n{out}")
    return "\n\n".join(parts)


# ---------------------------------------------------------------- CLI
def main(argv: list | None = None) -> int:
    ap = argparse.ArgumentParser(prog="anysearch_cli", add_help=True)
    sub = ap.add_subparsers(dest="cmd")

    p1 = sub.add_parser("search")
    p1.add_argument("query")
    for f in ("--domain", "--sub_domain", "--params", "--zone", "--language"):
        p1.add_argument(f, default="")
    p1.add_argument("--max_results", type=int, default=5)

    p2 = sub.add_parser("batch_search")
    p2.add_argument("--queries", default="")
    p2.add_argument("--query", action="append", default=[])
    p2.add_argument("--max_results", type=int, default=5)

    p3 = sub.add_parser("get_sub_domains")
    p3.add_argument("--domain", default="")
    p3.add_argument("--domains", default="")

    p4 = sub.add_parser("extract")
    p4.add_argument("url")

    args = ap.parse_args(argv)

    if args.cmd == "search":
        q = str(args.query or "").strip()
        if not q:
            print("错误：query 不能为空")
            return 1
        print(_dispatch(q, args.domain, args.sub_domain, _parse_params(args.params),
                        args.zone or "cn", args.language or "zh-CN",
                        max(1, min(args.max_results, 30))))
        return 0

    if args.cmd == "batch_search":
        items = _parse_query_items(args.queries, args.query)
        if not items:
            print("错误：请提供 --queries（JSON 数组或查询串）或 --query。")
            return 1
        print(_run_batch(items, max(1, min(args.max_results, 30))))
        return 0

    if args.cmd == "get_sub_domains":
        if args.domains:
            ds = [x.strip().lower() for x in re.split(r"[,\s]+", args.domains) if x.strip()]
        elif args.domain:
            ds = [str(args.domain).strip().lower()]
        else:
            ds = list(_DOMAINS.keys())
        print(_render_subdomains(ds[:5] if len(ds) > 5 and args.domains else ds))
        return 0

    if args.cmd == "extract":
        print(_h_extract(str(args.url).strip()))
        return 0

    ap.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
