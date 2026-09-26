# -*- coding: utf-8 -*-
"""学术检索与全文读取（search 技能的科研引擎）。

第一性原理拆科研链：找得到（多源检索）→ 读得进（PDF 全文）→ 溯得源（DOI/BibTeX）→ 追得上（时效过滤）。
四个源全部免认证、无付费 key：arXiv(Atom) / OpenAlex / PubMed(E-utilities) / Crossref。

多源并行 + RRF 融合（Reciprocal Rank Fusion）：同一篇被多个源命中会累积分数，
自然排在只被单源命中的结果之前——这就是「精度」在无 embedding key 时的可落地版本。
"""
from __future__ import annotations

import io
import os
import re
import sys
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

_SKILL_DIR = os.path.dirname(os.path.abspath(__file__))
if _SKILL_DIR not in sys.path:
    sys.path.insert(0, _SKILL_DIR)

import web_impl  # noqa: E402  复用其代理候选链（直连优先 / 墙外代理优先）

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None

_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
       "Chrome/124.0.0.0 Safari/537.36")
_MAILTO = "dabai@localhost"
_TIMEOUT = 25.0
ALL_SOURCES = ("arxiv", "openalex", "crossref", "pubmed")
_ATOM = "{http://www.w3.org/2005/Atom}"
_ARXIV_NS = "{http://arxiv.org/schemas/atom}"


def _get(url: str, params: dict | None = None, timeout: float = _TIMEOUT,
         accept: str | None = None, raw: bool = False, headers: dict | None = None):
    """带 UA 的 GET；直连失败自动换本地代理。raw=True 返回 bytes（PDF 下载用）。

    headers 用于需要鉴权的源（GitHub 必须带 Authorization，未认证实测 403）。
    """
    if requests is None:
        raise RuntimeError("requests 未安装")
    headers = {"User-Agent": _UA, **(headers or {})}
    if accept:
        headers["Accept"] = accept
    last = None
    for proxies in web_impl._proxy_candidates(url):
        try:
            r = requests.get(url, params=params, headers=headers,
                             timeout=(min(6.0, float(timeout)), float(timeout)),
                             proxies=proxies)
            if 200 <= r.status_code < 300:
                return r.content if raw else r
            last = RuntimeError(f"HTTP {r.status_code}")
        except Exception as e:  # noqa: BLE001
            last = e
    raise last if last else RuntimeError("请求失败")


def _clean(s) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip()


def _days_ago(days) -> str:
    return (date.today() - timedelta(days=int(days))).isoformat()


def _as_int(v, default: int) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _norm_key(p: dict) -> str:
    """跨源去重键：DOI 优先，其次规范化标题（去掉标点与大小写差异）。"""
    doi = _clean(p.get("doi")).lower()
    doi = re.sub(r"^https?://(dx\.)?doi\.org/", "", doi)
    if doi:
        return "doi:" + doi
    t = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", _clean(p.get("title")).lower())
    return ("t:" + t[:80]) if t else ""


def _authors_str(authors, n: int = 3) -> str:
    names = [a for a in (authors or []) if a]
    if not names:
        return ""
    head = ", ".join(names[:n])
    return head + (" 等" if len(names) > n else "")


# ---------------------------------------------------------------- arXiv
def _arxiv(query: str, limit: int, days=None, sort: str = "relevance") -> list:
    params = {
        "search_query": f"all:{query}",
        "start": 0,
        "max_results": max(1, min(_as_int(limit, 10), 50)),
        "sortBy": "submittedDate" if sort == "latest" else "relevance",
        "sortOrder": "descending",
    }
    root = ET.fromstring(_get("https://export.arxiv.org/api/query", params=params).content)
    cutoff = _days_ago(days) if days else ""
    out = []
    for e in root.findall(f"{_ATOM}entry"):
        published = _clean(e.findtext(f"{_ATOM}published"))[:10]
        if cutoff and published and published < cutoff:
            continue
        aid = _clean(e.findtext(f"{_ATOM}id"))
        arxid = aid.rsplit("/", 1)[-1]
        if not arxid:
            continue
        out.append({
            "title": _clean(e.findtext(f"{_ATOM}title")),
            "abstract": _clean(e.findtext(f"{_ATOM}summary"))[:400],
            "authors": [_clean(a.findtext(f"{_ATOM}name")) for a in e.findall(f"{_ATOM}author")],
            "year": published[:4],
            "date": published,
            "doi": _clean(e.findtext(f"{_ARXIV_NS}doi")),
            "url": aid,
            "pdf": f"https://arxiv.org/pdf/{arxid}",
            "arxiv_id": arxid,
            "cited": None,
            "venue": _clean(e.findtext(f"{_ARXIV_NS}journal_ref")) or "arXiv",
            "source": "arxiv",
        })
    return out


# ---------------------------------------------------------------- OpenAlex
def _openalex(query: str, limit: int, days=None, sort: str = "relevance") -> list:
    params = {
        "search": query,
        "per-page": max(1, min(_as_int(limit, 10), 50)),
        "mailto": _MAILTO,
        "sort": "publication_date:desc" if sort == "latest" else "relevance_score:desc",
    }
    if days:
        params["filter"] = f"from_publication_date:{_days_ago(days)}"
    data = _get("https://api.openalex.org/works", params=params).json()
    out = []
    for w in (data.get("results") or []):
        loc = w.get("best_oa_location") or w.get("primary_location") or {}
        out.append({
            "title": _clean(w.get("title") or w.get("display_name")),
            "abstract": _reconstruct_abstract(w.get("abstract_inverted_index")),
            "authors": [_clean((a.get("author") or {}).get("display_name"))
                        for a in (w.get("authorships") or [])],
            "year": str(w.get("publication_year") or ""),
            "date": _clean(w.get("publication_date")),
            "doi": _clean(w.get("doi")).replace("https://doi.org/", ""),
            "url": _clean(w.get("doi")) or _clean(w.get("id")),
            "pdf": _clean(loc.get("pdf_url")),
            "cited": w.get("cited_by_count"),
            "venue": _clean(((w.get("primary_location") or {}).get("source") or {}).get("display_name")),
            "source": "openalex",
        })
    return out


def _reconstruct_abstract(inv) -> str:
    """OpenAlex 只给倒排索引（词→位置），还原成可读摘要。"""
    if not isinstance(inv, dict):
        return ""
    pos = {}
    for word, idxs in inv.items():
        for i in (idxs or []):
            pos[i] = word
    return _clean(" ".join(pos[i] for i in sorted(pos)))[:400]


# ---------------------------------------------------------------- PubMed
def _pubmed(query: str, limit: int, days=None, sort: str = "relevance") -> list:
    n = max(1, min(_as_int(limit, 10), 50))
    es = {"db": "pubmed", "term": query, "retmax": n, "retmode": "json",
          "sort": "date" if sort == "latest" else "relevance"}
    if days:
        es.update({"datetype": "pdat", "mindate": _days_ago(days),
                   "maxdate": date.today().isoformat()})
    ids = (_get("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi",
                params=es).json().get("esearchresult") or {}).get("idlist") or []
    if not ids:
        return []
    res = (_get("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi",
                params={"db": "pubmed", "id": ",".join(ids), "retmode": "json"})
           .json().get("result") or {})
    out = []
    for pid in ids:
        it = res.get(pid) or {}
        doi = ""
        for aid in (it.get("articleids") or []):
            if (aid.get("idtype") or "").lower() == "doi":
                doi = _clean(aid.get("value"))
                break
        out.append({
            "title": _clean(it.get("title")),
            "abstract": "",
            "authors": [_clean(a.get("name")) for a in (it.get("authors") or [])],
            "year": _clean(it.get("pubdate"))[:4],
            "date": _clean(it.get("pubdate")),
            "doi": doi,
            "url": f"https://pubmed.ncbi.nlm.nih.gov/{pid}/",
            "pdf": "",
            "pmid": pid,
            "cited": None,
            "venue": _clean(it.get("fulljournalname") or it.get("source")),
            "source": "pubmed",
        })
    return out


# ---------------------------------------------------------------- Crossref
def _crossref(query: str, limit: int, days=None, sort: str = "relevance") -> list:
    params = {
        "query": query,
        "rows": max(1, min(_as_int(limit, 10), 50)),
        "select": ("DOI,title,author,issued,URL,is-referenced-by-count,"
                   "container-title,type,abstract"),
        "mailto": _MAILTO,
        "sort": "published" if sort == "latest" else "relevance",
        "order": "desc",
    }
    if days:
        params["filter"] = f"from-pub-date:{_days_ago(days)}"
    items = ((_get("https://api.crossref.org/works", params=params).json() or {})
             .get("message") or {}).get("items") or []
    out = []
    for it in items:
        issued = ((it.get("issued") or {}).get("date-parts") or [[""]])[0]
        year = str(issued[0] or "")
        out.append({
            "title": _clean((it.get("title") or [""])[0]),
            "abstract": _clean(re.sub(r"<[^>]+>", " ", it.get("abstract") or ""))[:400],
            "authors": [f"{_clean(a.get('given'))} {_clean(a.get('family'))}".strip()
                        for a in (it.get("author") or [])],
            "year": year,
            "date": "-".join(str(x) for x in issued) if year else "",
            "doi": _clean(it.get("DOI")),
            "url": _clean(it.get("URL")) or (f"https://doi.org/{it.get('DOI')}" if it.get("DOI") else ""),
            "pdf": "",
            "cited": it.get("is-referenced-by-count"),
            "venue": _clean((it.get("container-title") or [""])[0]),
            "source": "crossref",
        })
    return out


# ---------------------------------------------------------------- 融合与输出
def _title_sig(p: dict) -> frozenset:
    """标题签名（前 12 个实词）：跨源同篇识别用。arXiv 版没 DOI、出版社版有 DOI，
    单靠 DOI 键抓不住同一篇（实测 Ragas 2023 与 RAGAs 2024 被当成两篇）。"""
    t = re.sub(r"[^a-z0-9\u4e00-\u9fff ]+", " ", _clean(p.get("title")).lower())
    return frozenset([w for w in t.split() if len(w) > 2][:12])
def _jaccard(a: frozenset, b: frozenset) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _date_ok(p: dict, days=None) -> bool:
    """日期闸门：剔除未来日期脏数据（实测 Crossref 有 2114 年条目）与超龄条目。
    日期缺失时放行——不能因为源没给日期就误杀一篇真论文。"""
    d = _clean(p.get("date"))[:10]
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", d):
        y = _clean(p.get("year"))
        if not re.match(r"^\d{4}$", y):
            return True
        d = y + "-01-01"
    if d > (date.today() + timedelta(days=120)).isoformat():
        return False
    return not (days and d < _days_ago(days))


def _rrf(lists: dict, k: int = 60) -> list:
    """Reciprocal Rank Fusion：score = Σ 1/(k+rank)。跨源重复命中累积加权，字段互补。

    先按 DOI/标题归一化键合并，再按标题签名（Jaccard ≥ 0.8）做二次合并，
    把「同一篇的 arXiv 版与出版社版」收成一条。"""
    merged: dict = {}
    for src, items in lists.items():
        for rank, it in enumerate(items, 1):
            key = _norm_key(it)
            if not key:
                continue
            slot = merged.setdefault(key, {"item": dict(it), "score": 0.0, "sources": []})
            slot["score"] += 1.0 / (k + rank)
            slot["sources"].append(src)
            for f in ("doi", "pdf", "cited", "venue", "abstract", "arxiv_id", "pmid", "date"):
                if not slot["item"].get(f) and it.get(f):
                    slot["item"][f] = it[f]
    out: list = []
    for slot in sorted(merged.values(), key=lambda x: -x["score"]):
        sig = _title_sig(slot["item"])
        hit = None
        if len(sig) >= 4:  # 短标题（<4 实词）不做相似合并，避免误并
            for o in out:
                if len(o["sig"]) >= 4 and _jaccard(sig, o["sig"]) >= 0.8:
                    hit = o
                    break
        if hit is None:
            out.append({"item": slot["item"], "score": slot["score"],
                        "sources": list(slot["sources"]), "sig": sig})
            continue
        hit["sources"] = sorted(set(hit["sources"]) | set(slot["sources"]))
        hit["score"] += slot["score"] * 0.5
        for f in ("doi", "pdf", "cited", "venue", "abstract", "arxiv_id", "pmid", "date"):
            if not hit["item"].get(f) and slot["item"].get(f):
                hit["item"][f] = slot["item"][f]
    out.sort(key=lambda x: -x["score"])
    return [(x["item"], x["sources"], x["score"]) for x in out]


def _fmt_papers(pairs: list, max_n: int) -> str:
    lines = []
    for i, (p, srcs, score) in enumerate(pairs[:max_n], 1):
        meta = [x for x in (p.get("year"), p.get("venue")) if x]
        if p.get("cited") is not None:
            meta.append(f"被引 {p['cited']}")
        meta.append("+" .join(srcs))
        lines.append(f"{i}. {p.get('title') or '(无标题)'}")
        lines.append(f"   {' · '.join(meta)}" + (f" · {_authors_str(p.get('authors'))}"
                                                 if p.get("authors") else ""))
        if p.get("doi"):
            lines.append(f"   DOI: {p['doi']}")
        if p.get("pdf"):
            lines.append(f"   PDF: {p['pdf']}")
        elif p.get("url"):
            lines.append(f"   URL: {p['url']}")
        if p.get("abstract"):
            lines.append(f"   摘要: {p['abstract'][:220]}")
    return "\n".join(lines)


def search_paper(args: dict) -> str:
    query = _clean(args.get("query"))
    if not query:
        return "请提供检索主题（query 参数）。"
    src_arg = _clean(args.get("source")).lower() or "auto"
    if src_arg in ("all", ""):
        sources = list(ALL_SOURCES)
    elif src_arg == "auto":
        # auto：三个跨学科源并行（PubMed 只在显式指定或 all 时加入，避免医学库拉低跨领域精度）
        sources = ["arxiv", "openalex", "crossref"]
    else:
        sources = [s.strip() for s in re.split(r"[,\s]+", src_arg) if s.strip() in ALL_SOURCES]
        if not sources:
            return f"未知信息源 {src_arg}，可选：{', '.join(ALL_SOURCES)}（或 auto/all）。"
    limit = _as_int(args.get("limit") or args.get("max_results"), 8)
    per_src = max(limit, 10)
    days = args.get("days")
    sort = _clean(args.get("sort")).lower() or "relevance"
    if sort not in ("relevance", "latest"):
        sort = "relevance"
    fns = {"arxiv": _arxiv, "openalex": _openalex, "crossref": _crossref, "pubmed": _pubmed}
    results: dict = {}
    errors: dict = {}
    with ThreadPoolExecutor(max_workers=len(sources)) as ex:
        futs = {}
        for s in sources:
            # latest 模式只让 arXiv 按时间排（它的 search_query 已限定主题，结果必相关）；
            # OpenAlex/Crossref 按时间排会把未来日期的无关记录顶到最前
            # （实测搜 "large language model agent" 返回锡克教男性气质论文与 2114 年条目）。
            s_sort = "latest" if (sort == "latest" and s == "arxiv") else "relevance"
            futs[s] = ex.submit(fns[s], query, per_src, days, s_sort)
        for s, f in futs.items():
            try:
                results[s] = f.result()
            except Exception as e:  # noqa: BLE001
                errors[s] = f"{type(e).__name__}: {str(e)[:90]}"
    hits = sum(len(v) for v in results.values())
    if not hits:
        tail = ("；".join(f"{k} 失败({v})" for k, v in errors.items())) or "无匹配"
        return f"学术检索「{query}」无结果（{tail}）。可放宽 days 或换 source。"
    pairs = _rrf(results)
    if sort == "latest":
        # 时间排序必须配相关性闸门，否则「新鲜但无关」会盖住「相关」——实测就是这个坑。
        pairs = [x for x in pairs if _date_ok(x[0], days)
                 and web_impl._relevance_ok(query, x[0].get("title", ""),
                                            x[0].get("abstract", ""), 0.34)]
        pairs.sort(key=lambda x: (_clean(x[0].get("date"))[:10] or "0000-00-00"), reverse=True)
        # 未来日期沉底：期刊 online-first / 卷期年份（实测有 2027）不是「最新进展」，
        # 它们会把真正的 arXiv 最新预印本挤下去。稳定排序，两步走。
        _today = date.today().isoformat()
        pairs.sort(key=lambda x: _clean(x[0].get("date"))[:10] > _today)
    else:
        pairs = [x for x in pairs if _date_ok(x[0], days)]
    if not pairs:
        return (f"学术检索「{query}」在 {'近 ' + str(days) + ' 天' if days else '当前条件'}内"
                f"没有通过相关性闸门的结果（命中 {hits} 条，多为无关降级产物）。"
                f"建议放宽 days、去掉 days 或换 sort=relevance。")
    head = (f"学术检索「{query}」｜源: {'+'.join(sources)}"
            f"{f' · 近 {days} 天' if days else ''}"
            f"{' · 按时间' if sort == 'latest' else ' · 按相关度'}"
            f"｜命中 {hits} 条，融合去重后 {len(pairs)} 篇")
    body = _fmt_papers(pairs, limit)
    warn = ("\n（降级源: " + "；".join(f"{k} {v}" for k, v in errors.items()) + "）") if errors else ""
    tip = ("\n提示：read_paper 读全文（传 arXiv ID / DOI / PDF URL），paper_cite 取 BibTeX。")
    return f"{head}\n{body}{warn}{tip}"


# ---------------------------------------------------------------- 全文读取（PDF）
_ARXIV_ID_RE = re.compile(r"(?:arxiv[:/\s]*)?(\d{4}\.\d{4,5})(v\d+)?", re.I)
_ARXIV_OLD_RE = re.compile(r"arxiv[:/\s]*([a-z-]+(?:\.[A-Z]{2})?/\d{7})", re.I)
_DOI_RE = re.compile(r"10\.\d{4,9}/[^\s\"'<>]+", re.I)


def _norm_ref(ref: str) -> tuple:
    """把 arXiv ID / DOI / PDF URL / 落地页 URL 归一成 (kind, value)。"""
    s = _clean(ref).strip("<>()[]{}，,。;；")
    if s.lower().startswith("http") and s.lower().split("?")[0].endswith(".pdf"):
        return "pdf_url", s
    m = _DOI_RE.search(s)
    if m:
        return "doi", m.group(0).rstrip(".").rstrip("/")
    m = _ARXIV_ID_RE.search(s) or _ARXIV_OLD_RE.search(s)
    if m:
        return "arxiv", m.group(1) + (m.group(2) or "")
    if s.lower().startswith("http"):
        return "url", s
    return "unknown", s


def _pdf_from_doi(doi: str) -> str:
    """DOI → 开放获取 PDF：OpenAlex 优先，Unpaywall 兜底（都免 key）。"""
    try:
        w = _get(f"https://api.openalex.org/works/doi:{doi}", params={"mailto": _MAILTO}).json()
        for loc in (w.get("best_oa_location"), w.get("primary_location")):
            if loc and loc.get("pdf_url"):
                return _clean(loc["pdf_url"])
        oa = w.get("open_access") or {}
        if oa.get("oa_url"):
            return _clean(oa["oa_url"])
    except Exception:  # noqa: BLE001
        pass
    try:
        u = _get(f"https://api.unpaywall.org/v2/{doi}", params={"email": _MAILTO}).json()
        best = u.get("best_oa_location") or {}
        return _clean(best.get("url_for_pdf") or best.get("url"))
    except Exception:  # noqa: BLE001
        return ""


def _parse_pages(spec: str, total: int) -> list:
    """pages 参数：''→前 5 页；'all'→全部（上限 60）；'3'→第 3 页；'2-6'→区间。"""
    spec = _clean(spec).lower()
    if spec in ("all", "*"):
        return list(range(min(total, 60)))
    if not spec:
        return list(range(min(total, 5)))
    m = re.match(r"^(\d+)\s*-\s*(\d+)$", spec)
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        a, b = max(1, a), min(max(b, a), total)
        return list(range(a - 1, b))
    if spec.isdigit():
        i = int(spec)
        return [i - 1] if 1 <= i <= total else []
    return list(range(min(total, 5)))


def _tidy(text: str) -> str:
    """PDF 抽取的碎行处理：压掉 3 行以上空行，去掉纯页码行。"""
    t = re.sub(r"[ \t]+\n", "\n", text or "")
    t = re.sub(r"\n(?:\s*\n){2,}", "\n\n", t)
    return re.sub(r"\n\s*\d{1,3}\s*\n", "\n", t).strip()


def read_paper(args: dict) -> str:
    ref = _clean(args.get("ref") or args.get("id") or args.get("doi")
                 or args.get("url") or args.get("query"))
    if not ref:
        return "请提供论文标识（arXiv ID / DOI / PDF URL），例如 2312.10997 或 10.1145/3777378。"
    kind, val = _norm_ref(ref)
    if kind == "pdf_url":
        pdf_url = val
    elif kind == "arxiv":
        pdf_url = f"https://arxiv.org/pdf/{val}"
    elif kind == "doi":
        pdf_url = _pdf_from_doi(val)
    elif kind == "url":
        pdf_url = val
    else:
        return f"无法识别标识：{ref}（支持 arXiv ID、DOI、PDF 直链）。"
    if not pdf_url:
        return (f"未找到开放获取 PDF（{kind}: {val}）——该文献可能未 OA。"
                f"可先用 search_paper 找 OA 版本，或 read_web 读落地页。")
    try:
        blob = _get(pdf_url, raw=True, timeout=90, accept="application/pdf")
    except Exception as e:  # noqa: BLE001
        return f"PDF 下载失败（{pdf_url}）：{type(e).__name__} {str(e)[:120]}"
    if not blob[:5].startswith(b"%PDF"):
        return f"该链接返回的不是 PDF（{pdf_url}），可能是落地页——改用 read_web。"
    try:
        from pypdf import PdfReader
    except ImportError:  # pragma: no cover
        return "缺少 pypdf：venv/bin/pip install pypdf（或 pdftotext 转文本后再喂给我）。"
    try:
        reader = PdfReader(io.BytesIO(blob))
        total = len(reader.pages)
    except Exception as e:  # noqa: BLE001
        return f"PDF 解析失败（{len(blob)} 字节）：{type(e).__name__} {str(e)[:120]}"
    idxs = _parse_pages(args.get("pages"), total)
    if not idxs:
        return f"pages 参数越界：该 PDF 共 {total} 页。"
    max_chars = _as_int(args.get("max_chars"), 8000)
    chunks, empty = [], 0
    for i in idxs:
        try:
            t = _tidy(reader.pages[i].extract_text() or "")
        except Exception:  # noqa: BLE001
            t = ""
        if not t:
            empty += 1
        chunks.append(f"----- 第 {i + 1} 页 -----\n{t or '(本页无可抽取文本)'}")
    body = "\n".join(chunks)
    truncated = ""
    if len(body) > max_chars:
        body = body[:max_chars]
        truncated = f"\n…（已截断到 {max_chars} 字符，用 pages 指定页段继续读）"
    head = (f"论文全文｜{kind}: {val}｜共 {total} 页，本次第 "
            f"{idxs[0] + 1}-{idxs[-1] + 1} 页｜{len(blob) // 1024} KB\nPDF: {pdf_url}")
    note = ""
    if empty == len(idxs):
        note = "\n（全页无可抽取文本：可能是扫描版/图片型 PDF，需 OCR，本工具不做）"
    return f"{head}\n{body}{truncated}{note}"


# ---------------------------------------------------------------- 引用（BibTeX）
def _arxiv_bibtex(arxid: str) -> str:
    root = ET.fromstring(_get("https://export.arxiv.org/api/query",
                              params={"id_list": arxid}).content)
    e = root.find(f"{_ATOM}entry")
    if e is None:
        return f"% arXiv 无此条目：{arxid}"
    title = _clean(e.findtext(f"{_ATOM}title"))
    authors = [_clean(a.findtext(f"{_ATOM}name")) for a in e.findall(f"{_ATOM}author")]
    year = _clean(e.findtext(f"{_ATOM}published"))[:4]
    doi = _clean(e.findtext(f"{_ARXIV_NS}doi"))
    prim = e.find(f"{_ARXIV_NS}primary_category")
    prim = _clean(prim.get("term")) if prim is not None else ""
    key = re.sub(r"\W+", "", (authors[0].split()[-1] if authors else "anon")) + year
    fields = [f"  title={{{title}}}", f"  author={{{' and '.join(authors)}}}",
              f"  year={{{year}}}", f"  eprint={{{arxid}}}", "  archivePrefix={arXiv}"]
    if prim:
        fields.append(f"  primaryClass={{{prim}}}")
    if doi:
        fields.append(f"  doi={{{doi}}}")
    fields.append(f"  url={{https://arxiv.org/abs/{arxid}}}")
    return "@misc{" + key + ",\n" + ",\n".join(fields) + "\n}"


def paper_cite(args: dict) -> str:
    refs = args.get("refs") or args.get("ref") or args.get("ids") or args.get("doi")
    if isinstance(refs, str):
        refs = re.split(r"[,\n;]+", refs)
    elif isinstance(refs, dict):
        refs = [refs]
    refs = [_clean(r) for r in (refs or []) if _clean(r)]
    if not refs:
        return "请提供 DOI 或 arXiv ID（可多个，逗号/换行分隔）。"
    out = []
    for r in refs[:10]:
        kind, val = _norm_ref(r)
        try:
            if kind == "doi":
                txt = _get(f"https://api.crossref.org/works/{val}/transform/application/x-bibtex",
                           accept="application/x-bibtex").text
                out.append(txt.strip())
            elif kind == "arxiv":
                out.append(_arxiv_bibtex(val))
            else:
                out.append(f"% 无法识别（需 DOI 或 arXiv ID）：{r}")
        except Exception as e:  # noqa: BLE001
            out.append(f"% {r} 取引用失败：{type(e).__name__} {str(e)[:80]}")
    return "\n\n".join(out)


HANDLERS = {
    "search_paper": search_paper,
    "read_paper": read_paper,
    "paper_cite": paper_cite,
}
