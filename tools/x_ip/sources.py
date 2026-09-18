"""热点采集：中文时事为主，全部直连。

实测结论（2026-09-18）：
- 微博 hotSearch 不带 Referer 返回 403，带上就 200
- B站 ranking/v2 返回 code=-352（风控），改用 hotword / popular
- V2EX 直连超时、走 127.0.0.1:7890 返回 200，故单独标 proxy=True
- V2EX hot.json 只有 9 条且多是生活闲聊，latest.json 有 41 条真技术话题
- GitHub trending 无 API，抓 HTML 的 <article class="Box-row"> 块
- 36Kr 热榜是 JS 壳（HTML 里无 window.initialState），不接
- 知乎热榜 401 要 cookie，不接
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# 源权重：微博/百度是中文时事主战场；HN 偏科技，对技术 IP 更对口
WEIGHTS = {"weibo": 1.0, "baidu": 0.92, "hn": 0.85, "60s": 0.7, "bili": 0.6}


@dataclass
class Hot:
    source: str
    rank: int
    title: str
    heat: int = 0
    url: str = ""
    sources: list[str] = field(default_factory=list)
    key: str = ""
    ts: float = field(default_factory=time.time)

    def score(self) -> float:
        return WEIGHTS.get(self.source, 0.5) / (self.rank + 2)


PROXY = os.environ.get("X_IP_PROXY", "http://127.0.0.1:7890")

_openers: dict[bool, urllib.request.OpenerDirector] = {}


def _opener(proxy: bool) -> urllib.request.OpenerDirector:
    if proxy not in _openers:
        handlers = [urllib.request.ProxyHandler({"http": PROXY, "https": PROXY})] if proxy else []
        _openers[proxy] = urllib.request.build_opener(*handlers)
    return _openers[proxy]


def _get(
    url: str, referer: str | None = None, timeout: float = 12.0, proxy: bool = False
) -> bytes:
    headers = {"User-Agent": UA, "Accept": "application/json,text/html,*/*"}
    if referer:
        headers["Referer"] = referer
    req = urllib.request.Request(url, headers=headers)
    with _opener(proxy).open(req, timeout=timeout) as resp:
        return resp.read()


def _get_json(
    url: str, referer: str | None = None, timeout: float = 12.0, proxy: bool = False
) -> dict | list:
    return json.loads(_get(url, referer, timeout, proxy))


_PUNCT = re.compile(r"[\s\u3000#【】\[\]（）()「」“”\"'’‘·、，,。.！!？?~～:：;；|/\-—_]+")


def norm_key(title: str) -> str:
    """跨源去重指纹：微博和百度常推同一条，归一化后合并成一条。"""
    t = _PUNCT.sub("", title or "").lower()
    return hashlib.sha1(t.encode("utf-8")).hexdigest()[:12]


def _clean(s: str) -> str:
    s = (s or "").strip()
    if s.startswith("#") and s.endswith("#"):
        s = s.strip("#").strip()
    return _PUNCT.sub(" ", s).strip()


def weibo(limit: int = 15) -> list[Hot]:
    d = _get_json("https://weibo.com/ajax/side/hotSearch", "https://weibo.com/")
    out = []
    for i, it in enumerate(d.get("data", {}).get("realtime", [])[:limit]):
        title = _clean(it.get("word") or it.get("note") or "")
        if not title:
            continue
        out.append(
            Hot(
                source="weibo",
                rank=int(it.get("realpos") or i + 1),
                title=title,
                heat=int(it.get("num") or 0),
                url="https://s.weibo.com/weibo?q=" + urllib.parse.quote(f"#{title}#"),
            )
        )
    return out


def baidu(limit: int = 15) -> list[Hot]:
    d = _get_json("https://top.baidu.com/api/board?platform=wise&tab=realtime")
    out = []
    for card in d.get("data", {}).get("cards", []):
        for blk in card.get("content", []):
            for it in blk.get("content", []):
                title = _clean(it.get("word") or "")
                if not title:
                    continue
                try:
                    rank = int(it.get("index") or len(out) + 1)
                except (TypeError, ValueError):
                    rank = len(out) + 1
                out.append(
                    Hot(
                        source="baidu",
                        rank=rank,
                        title=title,
                        heat=int(it.get("hotScore") or 0),
                        url=it.get("url") or "",
                    )
                )
                if len(out) >= limit:
                    return out
    return out


def bili_hotword(limit: int = 10) -> list[Hot]:
    d = _get_json(
        "https://s.search.bilibili.com/main/hotword", "https://www.bilibili.com/"
    )
    out = []
    for i, it in enumerate(d.get("list", [])[:limit]):
        title = _clean(it.get("keyword") or it.get("show_name") or "")
        if not title:
            continue
        out.append(
            Hot(
                source="bili",
                rank=i + 1,
                title=title,
                heat=int(it.get("heat_score") or 0),
                url="https://search.bilibili.com/all?keyword="
                + urllib.parse.quote(title),
            )
        )
    return out


def sixty_seconds(limit: int = 10) -> list[Hot]:
    d = _get_json("https://60s.viki.moe/v2/60s", timeout=25.0)
    out = []
    for i, n in enumerate((d.get("data") or {}).get("news", [])[:limit]):
        title = _clean(n)
        if not title:
            continue
        out.append(Hot(source="60s", rank=i + 1, title=title[:120], url=""))
    return out


def v2ex(limit: int = 20) -> list[Hot]:
    """V2EX 最新：中文开发者真实在聊的事，IP 素材密度最高。需代理。"""
    d = _get_json("https://www.v2ex.com/api/topics/latest.json", proxy=True)
    out = []
    for i, it in enumerate(d[: limit * 2]):
        title = _clean(it.get("title") or "")
        if not title or len(title) < 6:
            continue
        out.append(
            Hot(
                source="v2ex",
                rank=i + 1,
                title=title[:120],
                heat=int(it.get("replies") or 0),
                url=it.get("url") or "",
            )
        )
        if len(out) >= limit:
            break
    return out


_GH_BLOCK = re.compile(
    r'<article class="Box-row">.*?<h2 class="h3 lh-condensed">\s*<a[^>]*href="/([^"]+)"',
    re.S,
)
# 分段切割而不是全局 findall，否则描述/星数跨 article 错位
_GH_SEG = re.compile(r'<article class="Box-row">')
# GitHub 给这些 class 加过 tmp- 前缀（如 tmp-pr-4），只锁前两段稳定类名
_GH_DESC = re.compile(r'<p class="col-9 color-fg-muted[^"]*">\s*(.*?)\s*</p>', re.S)
# 星数在 star 图标的 </svg> 之后，不是 href 紧后
_GH_STARS = re.compile(r'/stargazers".*?</svg>\s*([\d,]+)', re.S)


def gh_trending(limit: int = 15) -> list[Hot]:
    """GitHub Trending 无官方 API，抓 HTML。trending 榜本身就是「什么在被关注」。"""
    html = _get("https://github.com/trending?since=daily", timeout=20).decode("utf-8", "ignore")
    starts = [m.start() for m in _GH_SEG.finditer(html)]
    out = []
    for i, s in enumerate(starts[:limit]):
        seg = html[s : starts[i + 1] if i + 1 < len(starts) else len(html)]
        m = _GH_BLOCK.search(seg)
        if not m:
            continue
        repo = m.group(1).strip()
        dm = _GH_DESC.search(seg)
        desc = _clean(re.sub(r"<[^>]+>", " ", dm.group(1))) if dm else ""
        sm = _GH_STARS.search(seg)
        stars = int(sm.group(1).replace(",", "")) if sm else 0
        # repo 名不过 _clean——它会把 owner/repo 里的 / 和 - 抹成空格，丢掉仓库身份
        title = f"GitHub 热榜 {repo}：{desc[:70]}" if desc else f"GitHub 热榜：{repo}"
        out.append(
            Hot(
                source="gh",
                rank=i + 1,
                title=title[:140],
                heat=stars,
                url=f"https://github.com/{repo}",
            )
        )
    return out


def hackernews(limit: int = 12) -> list[Hot]:
    """HN 接口慢（实测 topstories 6.7s），且每条详情要单独请求，只取前 limit 条。"""
    ids = json.loads(_get("https://hacker-news.firebaseio.com/v0/topstories.json", timeout=20))
    out = []
    for i, iid in enumerate(ids[:limit]):
        try:
            it = _get_json(f"https://hacker-news.firebaseio.com/v0/item/{iid}.json", timeout=12)
        except Exception:
            continue
        title = _clean(it.get("title") or "")
        if not title:
            continue
        out.append(
            Hot(
                source="hn",
                rank=i + 1,
                title=title,
                heat=int(it.get("score") or 0),
                url=it.get("url") or f"https://news.ycombinator.com/item?id={iid}",
            )
        )
    return out


SOURCES = {
    "v2ex": v2ex,
    "gh": gh_trending,
    "weibo": weibo,
    "baidu": baidu,
    "bili": bili_hotword,
    "60s": sixty_seconds,
    "hn": hackernews,
}

DEFAULT_SOURCES = ["v2ex", "gh", "weibo", "baidu", "bili", "60s"]


def fetch_all(only: list[str] | None = None, per_source: int = 15) -> tuple[list[Hot], list[str]]:
    names = only or DEFAULT_SOURCES
    items: list[Hot] = []
    errs: list[str] = []
    for name in names:
        fn = SOURCES.get(name)
        if not fn:
            errs.append(f"{name}: 未知源")
            continue
        try:
            items.extend(fn(per_source))
        except Exception as e:  # 单源挂掉不能拖垮整轮采集
            errs.append(f"{name}: {type(e).__name__} {e}")

    merged: dict[str, Hot] = {}
    for it in items:
        it.key = norm_key(it.title)
        if it.key in merged:
            prev = merged[it.key]
            prev.sources = sorted(set(prev.sources + [it.source]))
            prev.heat = max(prev.heat, it.heat)
            if it.source == prev.source:
                prev.rank = min(prev.rank, it.rank)
            elif WEIGHTS.get(it.source, 0) > WEIGHTS.get(prev.source, 0):
                prev.source, prev.rank, prev.url = it.source, it.rank, it.url
        else:
            it.sources = [it.source]
            merged[it.key] = it

    return sorted(merged.values(), key=lambda x: -x.score()), errs


def dump(items: list[Hot], path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump([asdict(i) for i in items], f, ensure_ascii=False, indent=1)


def load(path: str) -> list[Hot]:
    with open(path, encoding="utf-8") as f:
        return [Hot(**d) for d in json.load(f)]
