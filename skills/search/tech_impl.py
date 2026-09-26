# -*- coding: utf-8 -*-
"""极客技术信息源聚合：HN / StackExchange / dev.to / Wikipedia / GitHub / HuggingFace / 论文库 / tavily。

为什么单独一层：通用网页搜索给的是「最像问题」的页面，而极客要的四类信息住在四个不同的地方——
踩坑答案在 StackExchange、真实工程评价在 HN 讨论、一手元数据在注册表 API（PyPI/npm/crates/HF）、
前沿在论文库。分开查会漏，合起来查才有交叉印证。

权威度分层（tier）显式写进每一条结果：
  A = 一手/官方（注册表 API、论文原文、官方文档）
  B = 同行评审或高信誉社区（StackExchange 答案、Wikipedia）
  C = 社区讨论与个人博客（HN、dev.to、GitHub 描述）
tavily 返回任意网页，tier 逐条按域名判（_tier_of_url）：docs.*/官方域名 → A，百科与评审站 → B。
把 C 当 A 用是 agent 最常犯的错，所以分层出现在输出里，不藏在提示词里。

2026-09-26 本机实测（详见 SKILL.md 可用性表）：
  可用：HN Algolia、StackExchange、dev.to、Wikipedia、HuggingFace、PyPI、npm、crates.io、
        DockerHub、OpenRouter、GitHub（带 GITHUB_TOKEN）
  不可用：Reddit（403）、Lobsters（bot 挑战页）、Semantic Scholar（429 限流）、
          paperswithcode.com（站点已关停，API 返回 HTML）
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import academic_impl  # noqa: E402
from academic_impl import _as_int, _clean, _get, _rrf  # noqa: E402
from keys_impl import find_key  # noqa: E402

_T = 12.0

# 源名 → 权威度。用于跨源合并后取「最高权威」而不是「第一个命中的」。
_TIER_RANK = {"A": 0, "B": 1, "C": 2}
_SRC_TIER = {
    "arXiv": "A", "OpenAlex": "A", "Crossref": "A", "PubMed": "A",
    "PyPI": "A", "npm": "A", "crates.io": "A", "DockerHub": "A",
    "HuggingFace": "A", "OpenRouter": "A",
    "StackOverflow": "B", "StackExchange": "B", "Wikipedia": "B",
    "Hacker News": "C", "dev.to": "C", "GitHub": "C", "官方文档": "A",
}

# tavily 返回的是任意网页，没有固定 tier，只能按域名判。判据是「离一手有多近」：
# 官方文档/规范/注册表页是一手（A），百科与同行评审站是可信二手（B），其余归社区（C）。
_TIER_A_PREFIX = ("docs.", "developer.", "developers.", "api.", "learn.", "support.", "help.")
_TIER_A_SUFFIX = (
    "readthedocs.io", "gitbook.io", "python.org", "rust-lang.org", "golang.org",
    "nodejs.org", "kernel.org", "mozilla.org", "w3.org", "ietf.org", "rfc-editor.org",
    "pypi.org", "npmjs.com", "crates.io", "hub.docker.com", "huggingface.co",
    "kubernetes.io", "docker.com", "postgresql.org", "redis.io", "sqlite.org",
    "openai.com", "anthropic.com", "apache.org", "gnu.org", "debian.org",
    "ubuntu.com", "archlinux.org", "microsoft.com", "apple.com",
)
_TIER_B_SUFFIX = (
    "wikipedia.org", "arxiv.org", "stackoverflow.com", "stackexchange.com",
    "nature.com", "science.org", "acm.org", "ieee.org", "springer.com",
    "sciencedirect.com", "doi.org", "openalex.org", "pubmed.ncbi.nlm.nih.gov",
    "lwn.net",
)

# 域名与 query 词比对时的停用词："the" 会命中 theverge.com，"dev" 会命中 dev.to，
# 把它们误抬成「官方」比漏判更贵——A 会直接影响 agent 的信与不信。
_HOST_STOP = {
    "the", "and", "for", "with", "how", "why", "what", "not", "are", "you",
    "use", "using", "vs", "dev", "com", "org", "net", "www", "api", "app",
    "get", "set", "new", "old", "top", "best", "all", "one", "two", "doc",
}

# homepage 指向这些站时不算「项目官方站」：它们是托管平台，域太宽，
# 拿来当 include_domains 会把整个平台的页面都放进来（readthedocs.io 会命中所有项目文档）。
_HOSTING_SITES = {
    "github.com", "github.io", "gitlab.com", "bitbucket.org", "gitee.com",
    "readthedocs.io", "netlify.app", "vercel.app", "pages.dev", "web.app",
    "sourceforge.net", "notion.site", "medium.com", "yuque.com",
}

# 文档索引里「怎么用」这类入口页的路径特征：搜一个项目名时想要的往往是它们。
_DOC_ENTRY = (
    "introduction", "overview", "quickstart", "quick-start", "get-started", "getting-started",
    "start", "install", "index", "concept", "architecture", "readme", "setup", "usage",
)
# llms.txt 的条目形如 `- [Introduction](https://docs.mem0.ai/introduction) [Both]: Use when ...`
_LLMS_LINK = re.compile(r"^\s*[-*]\s*\[([^\]]+)\]\((https?://[^)]+)\)\s*(.*)$")


# 用户输入别名 → 内部源短名（内部键统一用短名，人类可读名只出现在输出里）
_SRC_ALIAS = {
    "hn": "hn", "hackernews": "hn", "hacker news": "hn",
    "so": "so", "se": "so", "stackoverflow": "so", "stackexchange": "so",
    "devto": "devto", "dev.to": "devto", "dev": "devto",
    "wiki": "wiki", "wikipedia": "wiki",
    "gh": "gh", "github": "gh",
    "hf": "hf", "huggingface": "hf",
    "academic": "academic", "paper": "academic", "papers": "academic", "arxiv": "academic",
    "tavily": "tavily", "tvly": "tavily",
    "official": "official", "docs": "official", "llms": "official", "llmstxt": "official",
}
# auto 不含 devto：dev.to 官方 API 只支持 tag 检索，匹配的是标签不是内容，混进默认结果会稀释精度
# （实测搜 "rust async runtime" 会带出无关的 WebAssembly 文章）。需要教程类文章时显式 sources="devto"。
# auto 含 tavily：免 key 的五个源拿不到官方文档/厂商博客/榜单这类灰色文献，
# 实测搜「mem0」有论文和 HN 讨论，但拿不到 docs.mem0.ai。tavily 补这一层（basic 1 credit/次）。
_AUTO_SOURCES = ["hn", "so", "gh", "wiki", "academic", "tavily", "official"]

_TRACK_PARAM = re.compile(r"^(utm_|fbclid|gclid|mc_|_hs|spm|ref$|ref_|from$|share_|si$)", re.I)


def _url_key(url: str) -> str:
    """URL 归并键：同一篇内容被多个源引用时收成一条。

    只剥追踪参数——把整个查询串剥掉会让 ?id=123 型页面（每条 HN 评论一个 URL）全撞成一个键。
    """
    u = _clean(url)
    if not u:
        return ""
    u = re.sub(r"^https?://", "", u, flags=re.I)
    u = re.sub(r"^www\.", "", u, flags=re.I)
    head, sep, tail = u.partition("?")
    if sep:
        keep = [kv for kv in tail.split("&") if kv and not _TRACK_PARAM.match(kv.split("=")[0])]
        u = head + (("?" + "&".join(keep)) if keep else "")
    return u.rstrip("/").lower()


_DATE_RE = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})")


def _norm_date(s: str) -> str:
    """日期归一：Crossref 实测返回 '2026-7-1' 这种非零填充格式，统一成 YYYY-MM-DD。"""
    m = _DATE_RE.match(_clean(s))
    if not m:
        return _clean(s)[:10]
    return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"


def _age_str(d: str) -> str:
    """'2021-03-10' → '5年前'。信息时效性对极客检索是硬指标，所以每条都带。"""
    d = _clean(d)[:10]
    if not d:
        return ""
    try:
        dt = datetime.strptime(d, "%Y-%m-%d").date()
    except ValueError:
        return ""
    days = (date.today() - dt).days
    if days < 0:
        return "未来日期"
    if days == 0:
        return "今天"
    if days < 30:
        return f"{days}天前"
    if days < 365:
        return f"{days // 30}个月前"
    return f"{days // 365}年前"


def _strip_html(s: str) -> str:
    s = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", str(s or ""), flags=re.S | re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    for a, b in (("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"'), ("&#39;", "'"),
                 ("&amp;", "&"), ("&nbsp;", " "), ("&#x2F;", "/")):
        s = s.replace(a, b)
    return _clean(s)


def _lang_of(query: str, lang: str = "") -> str:
    """Wikipedia/摘要类源的语言：显式 lang 优先，否则按是否含中日韩字符猜。"""
    if lang:
        return lang
    return "zh" if re.search(r"[\u4e00-\u9fff]", query) else "en"


def _item(title, url, snippet, dt, source, key=None, tier=None, **meta) -> dict:
    """统一条目。

    doi 槽位塞归并键：_rrf 的 _norm_key 优先用 doi。论文带真 DOI 就用真 DOI（同一篇的
    arXiv 版与出版社版合并），其余用 URL 归并键（HN 帖子指向的文章与 dev.to 同篇文章合并）。
    合并后 sources 累加、权威度取最高——这就是交叉印证。
    """
    meta = {k: v for k, v in meta.items() if v not in (None, "", 0, [], {})}
    return {
        "title": _clean(title),
        "url": _clean(url),
        "snippet": _clean(snippet)[:400],
        "date": _norm_date(dt),
        "source": source,
        "tier": tier or _SRC_TIER.get(source) or _SRC_TIER.get(source.split("/")[0], "C"),
        "meta": meta,
        "doi": _clean(key) or _url_key(url),
    }


def _json_get(url, params=None, timeout=_T, headers=None):
    r = _get(url, params=params, timeout=timeout, headers=headers)
    return r.json()


def _since_ts(days) -> int:
    return int((datetime.now(timezone.utc) - timedelta(days=int(days))).timestamp())


def _days_ago_str(days) -> str:
    return (date.today() - timedelta(days=int(days))).isoformat()


# ---------------------------------------------------------------- 各源实现
# 每个源返回统一 item 列表；异常由 _gather 捕获并如实回报——一个源挂掉不能让整次检索看起来「没结果」。
def _src_hn(query, limit, days, sort):
    api = "https://hn.algolia.com/api/v1/search"
    if sort == "latest":
        api += "_by_date"
    params = {"query": query, "tags": "story", "hitsPerPage": limit}
    if days:
        params["numericFilters"] = f"created_at_i>{_since_ts(days)}"
    hits = (_json_get(api, params).get("hits") or [])
    out = []
    for h in hits:
        oid = _clean(h.get("objectID"))
        discuss = f"https://news.ycombinator.com/item?id={oid}" if oid else ""
        out.append(_item(
            h.get("title"), h.get("url") or discuss, h.get("story_text"),
            (h.get("created_at") or "")[:10], "Hacker News",
            points=h.get("points"), comments=h.get("num_comments"),
            discuss=discuss if h.get("url") else "",
        ))
    return out


def _src_so(query, limit, days, sort, site="stackoverflow"):
    params = {
        "order": "desc",
        "sort": "creation" if sort == "latest" else "relevance",
        "q": query, "site": site, "pagesize": limit, "filter": "withbody",
    }
    if days:
        params["fromdate"] = _since_ts(days)
    d = _json_get("https://api.stackexchange.com/2.3/search/advanced", params)
    name = "StackOverflow" if site == "stackoverflow" else f"StackExchange/{site}"
    out = []
    for it in (d.get("items") or []):
        ts = it.get("creation_date")
        dt = datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d") if ts else ""
        out.append(_item(
            it.get("title"), it.get("link"), _strip_html(it.get("body")), dt, name,
            score=it.get("score"), answers=it.get("answer_count"),
            solved="已解决" if it.get("is_answered") else "",
            tags=",".join((it.get("tags") or [])[:4]),
        ))
    return out


def _src_devto(query, limit, days, sort):
    """dev.to 官方 API 只支持 tag 检索（没有关键词搜索接口），所以取 query 首词当 tag。"""
    tag = re.sub(r"[^a-z0-9+#.-]", "", _clean(re.split(r"[\s,]+", query)[0]).lower())
    if not tag:
        return []
    params = {"tag": tag, "per_page": limit}
    if sort != "latest":
        params["top"] = max(30, int(days or 365))
    arts = _json_get("https://dev.to/api/articles", params)
    out = []
    for a in (arts or []):
        out.append(_item(
            a.get("title"), a.get("url"), a.get("description"),
            (a.get("published_at") or "")[:10], "dev.to",
            reactions=a.get("positive_reactions_count"), comments=a.get("comments_count"),
            tag=tag, minutes=a.get("reading_time_minutes"),
        ))
    return out


def _src_wiki(query, limit, days, sort, lang=""):
    lg = _lang_of(query, lang)
    base = f"https://{lg}.wikipedia.org"
    d = _json_get(f"{base}/w/api.php", {
        "action": "query", "list": "search", "srsearch": query,
        "format": "json", "srlimit": max(1, min(int(limit), 5)),
    })
    titles = [x.get("title") for x in ((d.get("query") or {}).get("search") or []) if x.get("title")]

    def summary(t):
        try:
            return t, _json_get(f"{base}/api/rest_v1/page/summary/" + urllib.parse.quote(t.replace(" ", "_")))
        except Exception:  # noqa: BLE001
            return t, {}

    out = []
    with ThreadPoolExecutor(max_workers=max(1, min(4, len(titles)))) as ex:
        for t, s in ex.map(summary, titles):
            if s.get("type") == "disambiguation" or not s.get("extract"):
                continue
            out.append(_item(
                s.get("title") or t,
                ((s.get("content_urls") or {}).get("desktop") or {}).get("page")
                or f"{base}/wiki/{urllib.parse.quote(t.replace(' ', '_'))}",
                s.get("extract"), (s.get("timestamp") or "")[:10], "Wikipedia",
                desc=s.get("description"),
            ))
    return out


def _src_github(query, limit, days, sort):
    token = find_key("GITHUB_TOKEN")
    if not token:
        raise RuntimeError("缺 GITHUB_TOKEN —— GitHub 搜索必须认证（未认证 60 次/小时且本机出口 IP 被共享，实测 403）")
    q = query + (f" created:>{_days_ago_str(days)}" if days else "")
    d = _json_get("https://api.github.com/search/repositories", {
        "q": q, "per_page": limit,
        "sort": "updated" if sort == "latest" else "stars", "order": "desc",
    }, headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"})
    out = []
    for it in (d.get("items") or []):
        out.append(_item(
            it.get("full_name"), it.get("html_url"), it.get("description"),
            (it.get("pushed_at") or "")[:10], "GitHub",
            stars=it.get("stargazers_count"), forks=it.get("forks_count"),
            lang=it.get("language"), issues=it.get("open_issues_count"),
            license=(it.get("license") or {}).get("spdx_id"),
            homepage=it.get("homepage"),
        ))
    return out


def _src_hf(query, limit, days, sort, kind="model"):
    path = "datasets" if kind == "dataset" else "models"
    params = {"search": query, "limit": limit, "sort": "downloads", "direction": -1}
    d = _json_get(f"https://huggingface.co/api/{path}", params)
    out = []
    for it in (d or []):
        mid = _clean(it.get("id") or it.get("modelId"))
        if not mid:
            continue
        out.append(_item(
            mid, f"https://huggingface.co/{'datasets/' if kind == 'dataset' else ''}{mid}",
            it.get("pipeline_tag") or ",".join((it.get("tags") or [])[:6]),
            (it.get("lastModified") or it.get("createdAt") or "")[:10], "HuggingFace",
            downloads=it.get("downloads"), likes=it.get("likes"), kind=kind,
        ))
    return out


def _paper_item(p: dict, source: str) -> dict:
    """论文条目：tier A（原文/一手元数据）。带 DOI 就用真 DOI 归并，同一篇的
    arXiv 版与出版社版会收成一条。"""
    meta = {}
    if p.get("venue"):
        meta["venue"] = p["venue"]
    if p.get("cited") is not None:
        meta["cited"] = p["cited"]
    if p.get("pdf"):
        meta["pdf"] = p["pdf"]
    if p.get("authors"):
        meta["authors"] = "、".join([a for a in p["authors"][:3] if a])
    return _item(p.get("title"), p.get("url") or p.get("pdf"), p.get("abstract"),
                 p.get("date") or p.get("year"), source,
                 key=p.get("doi") or p.get("arxiv_id"), **meta)


def _src_academic(query, limit, days, sort):
    """论文库四源并发。复用 academic_impl 的解析器，但在这里并发——
    串行四源是极客检索里最容易变成瓶颈的一段。"""
    funcs = [("arXiv", academic_impl._arxiv), ("OpenAlex", academic_impl._openalex),
             ("Crossref", academic_impl._crossref), ("PubMed", academic_impl._pubmed)]
    out, errors = [], []
    with ThreadPoolExecutor(max_workers=4) as ex:
        futs = {ex.submit(fn, query, limit, days, sort): name for name, fn in funcs}
        for fu in as_completed(futs):
            try:
                for p in (fu.result() or []):
                    out.append(_paper_item(p, futs[fu]))
            except Exception as e:  # noqa: BLE001
                errors.append(f"{futs[fu]}: {type(e).__name__}")
    if errors and not out:
        raise RuntimeError("; ".join(errors))
    return out


def _host_matches_query(host: str, query: str) -> bool:
    """查询对象自己拥有的域名就是官方——搜 mem0 时 mem0.ai 是官方。

    白名单永远穷举不完厂商域名，这条规则能推广。停用词表是必要的：query 里的 "the"
    会命中 theverge.com 这类媒体站，把它误抬成 A。
    """
    labels = [re.sub(r"[^a-z0-9]", "", x) for x in host.split(".")]
    words = [w for w in re.findall(r"[a-z0-9]{3,}", query.lower()) if w not in _HOST_STOP]
    for w in words:
        for lab in labels:
            if len(lab) >= 3 and (w in lab or lab in w):
                return True
    return False


def _entity_terms(query: str) -> list:
    """query 里含数字的词：mem0 / gpt4 / k8s / http2 / llama3。

    它们在自然语言里几乎不出现，命中即强相关。用它做相关性闸门：搜「mem0 memory layer」
    时能滤掉 Wikipedia 的 Stream X-Machine 和 OpenAlex 的神经科学论文——那些只是词面
    撞上了 memory/layer，而 RRF 只看排名不看相关度，会把它们抬进前几名。
    不含数字词的 query 不做闸门（「rust async runtime」这种无从判断哪个词是实体）。
    """
    return [w for w in re.findall(r"[a-z0-9][a-z0-9._-]*", query.lower()) if re.search(r"\d", w)]


def _has_entity(it: dict, ents: list) -> bool:
    # 去分隔符后再比：gpt-4 与 gpt4、mem0 与 mem-0 都能撞上。
    hay = re.sub(r"[^a-z0-9]", "", f"{it.get('title', '')}{it.get('snippet', '')}{it.get('url', '')}".lower())
    return any(re.sub(r"[^a-z0-9]", "", e) in hay for e in ents)


def _tier_of_url(url: str, query: str = "") -> str:
    host = (urllib.parse.urlsplit(url or "").hostname or "").lower()
    if not host:
        return "C"
    if host.startswith(_TIER_A_PREFIX) or host.endswith(_TIER_A_SUFFIX):
        return "A"
    if host.endswith(_TIER_B_SUFFIX):
        return "B"
    if query and _host_matches_query(host, query):
        return "A"
    return "C"


# tavily 源单次检索只跑一次，_gather 同步等待，读时已写完——不需要锁。
_TAVILY_CREDITS: list = []


def _reg_domain(host: str) -> str:
    return ".".join(host.split(".")[-2:]) if host.count(".") >= 1 else host


def _official_domain(items, query):
    """第一轮结果里属于「查询对象自己的域名」的那个（搜 mem0 → mem0.ai）。

    拿它当第二轮的 include_domains：实测 include_domains=['mem0.ai'] 直接返回
    docs.mem0.ai 六条，而裸的「{query} documentation」要碰运气。
    """
    for it in items:
        host = (urllib.parse.urlsplit(it.get("url") or "").hostname or "").lower()
        if _host_matches_query(host, query):
            return _reg_domain(host)
    return ""


def _official_from_github(items, query):
    """从 GitHub 仓库的 homepage 字段推官方域——比再赌一轮 tavily 便宜且确定。

    实测这个字段靠谱：mem0ai/mem0 → https://mem0.ai，langchain-ai/langchain →
    https://docs.langchain.com/langchain/（直接就是 docs 域）。两个坑：① 不是每个仓库
    都有（microsoft/markitdown 为空）；② 结果里混着同名的其它项目（搜 mem0 时 stars
    最多的第一条是 thedotmack/claude-mem，homepage 指向 claude-mem.ai）。所以只在域名
    跟 query 对得上时才采信，拿不准就返回空，让 tavily 退回「第一轮碰运气」。
    """
    for it in items:
        host = (urllib.parse.urlsplit((it.get("meta") or {}).get("homepage") or "").hostname or "").lower()
        host = re.sub(r"^www\.", "", host)
        # 比注册域：a.github.io 的注册域是 github.io，托管站整类排除
        if not host or _reg_domain(host) in _HOSTING_SITES:
            continue
        if _host_matches_query(host, query):
            return _reg_domain(host)
    return ""


def _llms_entries(text: str) -> list:
    """解析 llms.txt：`- [Title](url) [Tag]: description` 形式的 markdown 链接。

    顺带记住每条所属的 `## 分区`：文档站的索引按重要性排，Getting Started 里的条目
    比 Community & Support 里的更值得推给用户。
    """
    out, section, idx = [], "", 0
    for ln in (text or "").splitlines():
        if ln.startswith("## "):
            section, idx = ln[3:].strip(), idx + 1
            continue
        m = _LLMS_LINK.match(ln)
        if not m:
            continue
        desc = re.sub(r"^\[[^\]]{1,12}\]\s*:?\s*", "", _clean(m.group(3)))
        out.append({"title": _clean(m.group(1)), "url": _clean(m.group(2)),
                    "desc": desc, "section": section, "sidx": idx})
    return out


def _src_official(official, query, limit):
    """官方文档索引（llms.txt）：拿到官方域后直接抓，不赌搜索引擎的召回。

    实测 llms.txt 已是文档站的通用约定：docs.mem0.ai 44KB / docs.langchain.com 24KB /
    docs.anthropic.com 69KB / docs.ollama.com 4.6KB 都是 200，且是纯 markdown 目录。
    比 tavily 的限定轮便宜（1 次 GET vs 1 credit）且确定——拿到的是文档站自己的目录，
    不是「搜索引擎觉得像文档的页面」（同一 query 两次 tavily 分别给 docs 页和
    terms/cli/openclaw，非确定性）。

    没有官方域（query 不是项目名）返回空；有官方域但站点没提供 llms.txt 时报错，
    让用户知道这条路走不通（docs.vllm.ai 实测 404）。
    """
    if not official:
        return []
    text, tried = "", []
    for host in (f"docs.{official}", official):
        try:
            text = _get(f"https://{host}/llms.txt", timeout=15).content.decode("utf-8", "replace")
            break
        except Exception as e:  # noqa: BLE001
            tried.append(f"{host}: {type(e).__name__} {str(e)[:40]}")
    if not text:
        raise RuntimeError("llms.txt 不可用：" + ("; ".join(tried) or "无候选"))
    words = [w for w in re.findall(r"[a-z0-9]{3,}", query.lower()) if w not in _HOST_STOP]
    scored = []
    for e in _llms_entries(text):
        hay = (e["title"] + " " + e["url"]).lower()
        s = 2 * sum(1 for w in words if w in hay)
        s += max(0, 3 - (e["sidx"] - 1) // 3)
        if any(k in urllib.parse.urlsplit(e["url"]).path.lower() for k in _DOC_ENTRY):
            s += 3
        scored.append((s, e))
    scored.sort(key=lambda x: -x[0])
    return [_item(e["title"], e["url"], e["desc"], "", "官方文档", tier="A",
                  section=e["section"] or None) for _s, e in scored[:limit]]


def _src_tavily(query, limit, days, sort, official=""):
    """tavily 源：补官方文档/厂商博客/榜单这类灰色文献——免 key 的五个源都拿不到。

    official 是上层从 GitHub 仓库 homepage 推出来的官方域（确定线索）：给了它就直接
    限定第二轮，不再赌第一轮 tavily 恰好返回官方站。

    权威度逐条按域名判（_tier_of_url），因为 tavily 返回的是任意网页，没有固定 tier。

    短 query（≤ 3 词，典型是项目名/技术名）额外补一次 `{query} documentation`：
    实测搜「mem0 memory layer」时 tavily 无论 basic 还是 advanced 都只给厂商博客和新闻，
    而搜「mem0 documentation」直接命中 docs.mem0.ai 四条——差别在查询词，不在深度。
    代价是多 1 credit，换来的是「问一个库怎么用」时官方文档直接进结果。
    """
    import tavily_impl  # 懒加载：tavily 是可选源，没配 key 时不该拖进 requests 依赖

    queries = [query]
    if len(query.split()) <= 3:
        queries.append(f"{query} documentation")
    lists: dict = {}
    for idx, q in enumerate(queries):
        payload = {"query": q, "max_results": max(limit, 8), "search_depth": "basic"}
        if days:
            for lim, tr in ((1, "day"), (7, "week"), (31, "month"), (365, "year")):
                if int(days) <= lim:
                    payload["time_range"] = tr
                    break
        if idx == 1 and lists:
            dom = official or _official_domain(next(iter(lists.values())), query)
            if dom:
                payload["include_domains"] = [dom]
        data, err = tavily_impl._request("POST", "/search", payload)
        if err:
            if lists:
                break  # 第一轮已有结果，补充查询失败不值得把整源判死
            raise RuntimeError(err.splitlines()[0][:80])
        credits = (data.get("usage") or {}).get("credits")
        if credits:
            _TAVILY_CREDITS.append(credits)
        lists[q] = [_item(
            it.get("title"), _clean(it.get("url")),
            it.get("content") or it.get("raw_content"), it.get("published_date"),
            "Tavily", tier=_tier_of_url(_clean(it.get("url")), query),
            rel=it.get("score"), official=1 if payload.get("include_domains") else None,
        ) for it in (data.get("results") or [])]
        # 源内先按权威度排，再按 tavily 自己的相关度。tavily 的 score 衡量「网页像不像问题」，
        # 不衡量「对极客有多值」——实测搜「mem0 memory layer」时它把 24M 融资新闻排第 1、
        # 官方文档排第 10，直接进 RRF 会把融资新闻抬到全局第一。
        lists[q].sort(key=lambda x: (_TIER_RANK.get(x.get("tier") or "C", 2),
                                     -(x.get("meta", {}).get("rel") or 0)))
    if len(lists) == 1:
        return next(iter(lists.values()))
    # 两个查询各自从 rank 1 起算，走同一套 RRF——tavily 的 score 是查询内归一化的，
    # 跨查询不可比（实测 docs.mem0.ai/introduction 在 documentation 查询里排第 2，
    # score 0.77 低于另一查询里任何一条的 0.9，按 score 混排会把它压到末尾）。
    # 限定官方域那一轮整体 ×1.6：它的每一条都来自官方域，是「官方文档」这个意图的直接命中。
    ranked = [(it, sc * (1.6 if (it.get("meta") or {}).get("official") else 1.0))
              for it, _srcs, sc in _rrf(lists)]
    ranked.sort(key=lambda x: -x[1])
    return [it for it, _sc in ranked]



def _run_source(name, query, limit, days, sort, lang="", site="stackoverflow", official=""):
    if name == "hn":
        return _src_hn(query, limit, days, sort)
    if name == "so":
        return _src_so(query, limit, days, sort, site=site)
    if name == "devto":
        return _src_devto(query, limit, days, sort)
    if name == "wiki":
        return _src_wiki(query, limit, days, sort, lang=lang)
    if name == "gh":
        return _src_github(query, limit, days, sort)
    if name == "hf":
        return _src_hf(query, limit, days, sort)
    if name == "academic":
        return _src_academic(query, limit, days, sort)
    if name == "tavily":
        return _src_tavily(query, limit, days, sort, official=official)
    if name == "official":
        return _src_official(official, query, limit)
    return []


def _gather(names, query, limit, days, sort, lang="", site="stackoverflow", official=""):
    """并发打全部源。返回 (items, ok列表, 错误列表)。一个源挂掉不影响其他源。"""
    items, ok, errs = [], [], []
    if not names:
        return items, ok, errs
    with ThreadPoolExecutor(max_workers=max(1, min(8, len(names)))) as ex:
        futs = {ex.submit(_run_source, n, query, limit, days, sort, lang, site, official=official): n for n in names}
        for fu in as_completed(futs):
            name = futs[fu]
            try:
                got = fu.result() or []
                items += got
                ok.append(f"{name}({len(got)})")
            except Exception as e:  # noqa: BLE001
                errs.append(f"{name}: {type(e).__name__} {str(e)[:70]}")
    return items, ok, errs


_META_CN = {
    "points": "分", "comments": "评", "score": "赞", "answers": "答", "reactions": "赞",
    "rel": "相关度",
    "stars": "star", "forks": "fork", "downloads": "下载", "likes": "赞", "cited": "被引",
    "issues": "issue", "minutes": "分钟", "solved": "", "tags": "tags", "lang": "语言",
    "license": "许可", "venue": "期刊", "authors": "作者", "desc": "", "kind": "",
    "recent": "近期下载", "official": "", "homepage": "官网", "section": "分区",
}


def _meta_str(meta: dict) -> str:
    parts = []
    for k, v in (meta or {}).items():
        if k in ("pdf", "discuss", "official"):
            continue
        label = _META_CN.get(k, k)
        parts.append(f"{label} {v}" if label else str(v))
    return " · ".join(parts)


def _fmt_tech(pairs, show, query, ok, errs, note=""):
    lines = [f"极客检索「{query}」· 命中 {len(pairs)} 条（融合源：{'、'.join(ok) or '无'}）", ""]
    for i, (it, srcs, _score) in enumerate(pairs[:show], 1):
        tier = it.get("tier", "C")
        nsrc = f" ✓{len(set(srcs))}源" if len(set(srcs)) > 1 else ""
        lines.append(f"{i}. [{tier}] {it.get('title') or '(无标题)'}{nsrc}")
        head = [it.get("source") or ""]
        if it.get("date"):
            head.append(f"{it['date']}（{_age_str(it['date'])}）")
        m = _meta_str(it.get("meta") or {})
        if m:
            head.append(m)
        lines.append("   " + " · ".join([h for h in head if h]))
        if it.get("url"):
            lines.append(f"   {it['url']}")
        if it.get("snippet"):
            lines.append(f"   {it['snippet'][:260]}")
        disc = (it.get("meta") or {}).get("discuss")
        if disc:
            lines.append(f"   讨论 {disc}")
        lines.append("")
    if note:
        lines.append(note)
    if errs:
        lines.append("⚠ 未命中的源：" + "；".join(errs))
    if any(it.get("tier") == "C" for it, _s, _c in pairs[:show]):
        lines.append("来源标记：[A] 一手/官方 · [B] 同行评审或高信誉问答 · [C] 社区讨论——C 当线索用，别当结论。")
    return "\n".join(lines).rstrip()


def search_tech(args: dict) -> str:
    query = _clean(args.get("query"))
    if not query:
        return "请提供检索关键词（query）。"
    src_arg = _clean(args.get("sources")).lower() or "auto"
    limit = max(1, min(_as_int(args.get("limit"), 3), 10))
    show = max(1, min(_as_int(args.get("max_results"), 10), 30))
    days = _as_int(args.get("days"), 0) or None
    sort = _clean(args.get("sort")).lower() or "relevance"
    lang = _clean(args.get("lang"))
    site = _clean(args.get("site")) or "stackoverflow"

    names, unknown = [], []
    if src_arg in ("auto", "all", ""):
        names = list(_AUTO_SOURCES) + (["hf"] if src_arg == "all" else [])
    else:
        for raw in re.split(r"[,，\s]+", src_arg):
            key = raw.strip().lower()
            if not key:
                continue
            if key in ("auto", "all"):
                names = list(_AUTO_SOURCES)
                continue
            if key in _SRC_ALIAS:
                names.append(_SRC_ALIAS[key])
            else:
                unknown.append(key)
    seen, uniq = set(), []
    for n in names:
        if n not in seen:
            seen.add(n)
            uniq.append(n)
    names = uniq

    if not names:
        return ("没有可用的信息源。可用值：hn / so / devto / wiki / gh / hf / academic / tavily / official，"
                "或 auto（默认 hn+so+gh+wiki+academic+tavily+official）。")

    _TAVILY_CREDITS.clear()
    # gh 先跑一轮：它的 homepage 字段直指官方站，把它当线索喂给 tavily 的限定轮，
    # 比「赌第一轮 tavily 恰好返回官方站」便宜且确定。gh 结果照样并入，不重复请求。
    pre_items, pre_errs, official = [], [], ""
    if ("tavily" in names or "official" in names) and "gh" in names:
        names = [n for n in names if n != "gh"]
        try:
            pre_items = _src_github(query, limit, days, sort) or []
            official = _official_from_github(pre_items, query)
        except Exception as e:  # noqa: BLE001
            pre_errs.append(f"gh: {type(e).__name__} {str(e)[:70]}")
    items, ok, errs = _gather(names, query, limit, days, sort, lang=lang, site=site, official=official)
    if pre_items:
        items += pre_items
        ok.insert(0, f"gh({len(pre_items)})")
    errs = pre_errs + errs
    notes = []
    if official:
        notes.append(f"（官方域 {official} 取自 GitHub 仓库 homepage）")
    if unknown:
        notes.append(f"（忽略无法识别的源：{','.join(unknown)}）")
    if "tavily" in names and not find_key("TAVILY_API_KEY"):
        notes.append("（tavily 未配置 TAVILY_API_KEY：官方文档/厂商博客类结果缺失）")
    if _TAVILY_CREDITS:
        notes.append(f"（tavily 消耗 {sum(_TAVILY_CREDITS)} credits）")
    ents = _entity_terms(query)
    if ents:
        kept = [it for it in items if _has_entity(it, ents)]
        if kept and len(kept) < len(items):
            notes.append(f"（按实体词 {'/'.join(ents)} 滤掉 {len(items) - len(kept)} 条词面撞车的）")
            items = kept
    note = " ".join(notes)
    if not items:
        tip = ("；".join(errs) if errs else "各源都返回空")
        return (f"极客检索「{query}」无结果（{'、'.join(names)}）：{tip}\n"
                "换更具体的词，或用 sources 指定单个源排查；通用网页兜底用 search_web / search_tavily。")

    by_src: dict = {}
    for it in items:
        by_src.setdefault(it.get("source") or "?", []).append(it)
    pairs = _rrf(by_src)
    for it, srcs, _s in pairs:
        # 条目自身的 tier 也参与：tavily 命中按域名判出的 A 不能被源名基线盖回 C。
        ranks = [_TIER_RANK.get(it.get("tier") or "C", 2)]
        ranks += [_TIER_RANK.get(_SRC_TIER.get(x) or _SRC_TIER.get(x.split("/")[0], "C"), 2)
                  for x in srcs]
        it["tier"] = "ABC"[min(ranks)]
    # RRF 分相同时一手优先。同分的条目很多（每个源的第一名都是 1/61），
    # 不指定次序的话谁排前面取决于并发完成顺序——那是掷骰子，不是排序。
    pairs.sort(key=lambda x: (-round(x[2], 6), _TIER_RANK.get(x[0].get("tier") or "C", 2)))
    return _fmt_tech(pairs, show, query, ok, errs, note)


# ---------------------------------------------------------------- 注册表精确查询（tier A：一手元数据）
# 存在的理由：写依赖前查真实版本号，别凭记忆写——模型幻觉版本号是最常见也最贵的错。
def _reg_pypi(name):
    d = _json_get(f"https://pypi.org/pypi/{urllib.parse.quote(name)}/json")
    info = d.get("info") or {}
    ver = _clean(info.get("version"))
    files = ((d.get("releases") or {}).get(ver) or [])
    urls = info.get("project_urls") or {}
    return {
        "kind": "PyPI", "name": info.get("name") or name, "version": ver,
        "date": _clean(files[0].get("upload_time"))[:10] if files else "",
        "desc": info.get("summary"),
        "extra": f"Python {info.get('requires_python')}" if info.get("requires_python") else "",
        "license": info.get("license_expression") or _clean(info.get("license"))[:40],
        "repo": urls.get("Source") or urls.get("Repository") or urls.get("Homepage") or info.get("home_page"),
        "url": info.get("package_url") or f"https://pypi.org/project/{name}/",
    }


def _reg_npm(name):
    pkg = _json_get(f"https://registry.npmjs.org/{urllib.parse.quote(name)}/latest")
    dl = {}
    try:
        dl = _json_get(f"https://api.npmjs.org/downloads/point/last-month/{urllib.parse.quote(name)}")
    except Exception:  # noqa: BLE001
        pass
    repo = pkg.get("repository")
    if isinstance(repo, dict):
        repo = repo.get("url")
    return {
        "kind": "npm", "name": pkg.get("name") or name, "version": _clean(pkg.get("version")),
        "desc": pkg.get("description"), "license": _clean(pkg.get("license"))[:40],
        "repo": _clean(repo).replace("git+", "").replace(".git", ""),
        "extra": f"月下载 {dl.get('downloads'):,}" if dl.get("downloads") else "",
        "url": f"https://www.npmjs.com/package/{name}",
    }


def _reg_crates(name):
    d = _json_get(f"https://crates.io/api/v1/crates/{urllib.parse.quote(name)}")
    c = d.get("crate") or {}
    vers = d.get("versions") or []
    lic = _clean((vers[0] or {}).get("license")) if vers else ""
    dl = c.get("downloads")
    return {
        "kind": "crates.io", "name": c.get("name") or name,
        "version": c.get("max_stable_version") or c.get("max_version"),
        "date": _clean(c.get("updated_at"))[:10], "desc": c.get("description"),
        "extra": f"总下载 {dl:,}" if dl else "", "license": lic,
        "repo": c.get("repository") or c.get("homepage"),
        "url": f"https://crates.io/crates/{name}",
    }


def _reg_docker(name):
    full = name if "/" in name else f"library/{name}"
    d = _json_get(f"https://hub.docker.com/v2/repositories/{full}/")
    tags = []
    try:
        t = _json_get(f"https://hub.docker.com/v2/repositories/{full}/tags/",
                      {"page_size": 5, "ordering": "last_updated"})
        tags = [x.get("name") for x in (t.get("results") or []) if x.get("name")]
    except Exception:  # noqa: BLE001
        pass
    stars, pulls = d.get("star_count"), d.get("pull_count")
    extra = " · ".join([x for x in (
        f"star {stars:,}" if stars else "", f"拉取 {pulls:,}" if pulls else "",
        "最近 tag " + ",".join(tags) if tags else "") if x])
    return {
        "kind": "DockerHub", "name": d.get("name") or full, "desc": d.get("description"),
        "date": _clean(d.get("last_updated"))[:10], "extra": extra,
        "license": "官方镜像" if d.get("is_official") else "",
        "url": f"https://hub.docker.com/r/{full}",
    }


def _reg_hf(name, kind="model"):
    path = "datasets" if kind == "dataset" else "models"
    d = _json_get(f"https://huggingface.co/api/{path}/{name}")
    dl, likes = d.get("downloads"), d.get("likes")
    return {
        "kind": f"HuggingFace {kind}", "name": d.get("id") or name,
        "date": _clean(d.get("lastModified"))[:10],
        "desc": d.get("pipeline_tag") or ",".join((d.get("tags") or [])[:6]),
        "extra": " · ".join([x for x in (
            f"月下载 {dl:,}" if dl else "", f"likes {likes:,}" if likes else "") if x]),
        "url": f"https://huggingface.co/{'datasets/' if kind == 'dataset' else ''}{name}",
    }


def _reg_llm(query):
    d = _json_get("https://openrouter.ai/api/v1/models")
    q = query.lower()
    hits = [m for m in (d.get("data") or [])
            if q in str(m.get("id", "")).lower() or q in str(m.get("name", "")).lower()]
    hits.sort(key=lambda m: -(m.get("context_length") or 0))

    def price(x):
        try:
            v = float(x)
        except (TypeError, ValueError):
            return ""
        return "动态定价" if v < 0 else f"${v * 1e6:.2f}/M"

    out = []
    for m in hits[:8]:
        pr = m.get("pricing") or {}
        ctx = m.get("context_length")
        out.append({
            "kind": "LLM（OpenRouter）", "name": m.get("id"),
            "desc": _clean(m.get("description"))[:200],
            "extra": " · ".join([x for x in (
                f"上下文 {ctx:,}" if ctx else "",
                f"输入 {price(pr.get('prompt'))}" if price(pr.get("prompt")) else "",
                f"输出 {price(pr.get('completion'))}" if price(pr.get("completion")) else "") if x]),
            "url": f"https://openrouter.ai/{m.get('id')}",
        })
    return out


_REG_KINDS = {
    "pypi": [("PyPI", _reg_pypi)],
    "pip": [("PyPI", _reg_pypi)],
    "python": [("PyPI", _reg_pypi)],
    "npm": [("npm", _reg_npm)],
    "node": [("npm", _reg_npm)],
    "crates": [("crates.io", _reg_crates)],
    "cargo": [("crates.io", _reg_crates)],
    "rust": [("crates.io", _reg_crates)],
    "docker": [("DockerHub", _reg_docker)],
    "image": [("DockerHub", _reg_docker)],
    "hf": [("HuggingFace model", lambda n: _reg_hf(n, "model")),
           ("HuggingFace dataset", lambda n: _reg_hf(n, "dataset"))],
    "model": [("HuggingFace model", lambda n: _reg_hf(n, "model"))],
    "dataset": [("HuggingFace dataset", lambda n: _reg_hf(n, "dataset"))],
    "llm": [("LLM（OpenRouter）", _reg_llm)],
}


def _fmt_reg(rows, name, errs):
    lines = [f"注册表查询「{name}」· 命中 {len(rows)} 项（一手元数据 · tier A）", ""]
    for r in rows:
        ver = f" {r['version']}" if r.get("version") else ""
        lines.append(f"[{r['kind']}] {r.get('name') or name}{ver}")
        head = []
        if r.get("date"):
            head.append(f"{r['date']}（{_age_str(r['date'])}）")
        if r.get("extra"):
            head.append(r["extra"])
        if r.get("license"):
            head.append(r["license"])
        if head:
            lines.append("   " + " · ".join(head))
        if r.get("desc"):
            lines.append(f"   {r['desc']}")
        if r.get("repo"):
            lines.append(f"   源码 {r['repo']}")
        if r.get("url"):
            lines.append(f"   {r['url']}")
        lines.append("")
    if errs:
        lines.append("⚠ " + "；".join(errs))
    return "\n".join(lines).rstrip()


def registry_info(args: dict) -> str:
    name = _clean(args.get("name") or args.get("query"))
    if not name:
        return "请提供包名 / 镜像名 / 模型名（name）。"
    kind = _clean(args.get("kind")).lower() or "auto"
    if kind in ("auto", "all", ""):
        jobs = [("PyPI", _reg_pypi), ("npm", _reg_npm), ("crates.io", _reg_crates)]
        jobs.append(("DockerHub", _reg_docker))
        if "/" in name:
            jobs.append(("HuggingFace model", lambda n: _reg_hf(n, "model")))
        else:
            jobs.append(("HuggingFace model", lambda n: _reg_hf(n, "model")))
            jobs.append(("HuggingFace dataset", lambda n: _reg_hf(n, "dataset")))
    elif kind in _REG_KINDS:
        jobs = _REG_KINDS[kind]
    else:
        return (f"无法识别的 kind「{kind}」。可用值：auto / pypi / npm / crates / docker / "
                "hf / model / dataset / llm。")

    rows, errs = [], []
    with ThreadPoolExecutor(max_workers=max(1, min(8, len(jobs)))) as ex:
        futs = {ex.submit(fn, name): label for label, fn in jobs}
        for fu in as_completed(futs):
            label = futs[fu]
            try:
                got = fu.result()
                rows += got if isinstance(got, list) else [got]
            except Exception as e:  # noqa: BLE001
                msg = str(e)
                if "404" in msg:
                    continue  # 该注册表没有这个包，属正常未命中，不当错误报
                errs.append(f"{label}: {type(e).__name__} {msg[:60]}")

    if not rows:
        tip = "；".join(errs) if errs else "各注册表都查不到这个名字"
        return (f"注册表查询「{name}」无命中：{tip}\n"
                "确认名字拼写，或用 kind 指定注册表（pypi/npm/crates/docker/hf/llm）。")
    return _fmt_reg(rows, name, errs)
