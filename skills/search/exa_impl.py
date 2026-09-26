# -*- coding: utf-8 -*-
"""Exa 语义检索（search 技能的 exa 引擎）。

原生 HTTP 实现，不依赖 exa-skills 的脚本（本机 skills/search/engines/ 下只有 anysearch）。
第一性原理：这套工具过去不可用的唯一原因是「脚本不存在 + key 不存在」——
脚本那一层只是搬运工，删掉它，外部依赖就只剩一个 key。

端点与鉴权实测（2026-09-26，用假 key 探针）：
    POST https://api.exa.ai/search        → 401 INVALID_API_KEY（鉴权层可达）
    POST https://api.exa.ai/answer        → 401
    POST https://api.exa.ai/findSimilar   → 400（端点存在，先校验 body 再鉴权）
鉴权：Authorization: Bearer <key>（x-api-key 同样被接受）。
直连可达，无需代理；仍走 web_impl._proxy_candidates 兜底，网络抖动时自动换代理重试。
"""
from __future__ import annotations

import os
import re
import sys

_SKILL_DIR = os.path.dirname(os.path.abspath(__file__))
if _SKILL_DIR not in sys.path:
    sys.path.insert(0, _SKILL_DIR)

import keys_impl  # noqa: E402
import web_impl  # noqa: E402

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None

_API = "https://api.exa.ai"
_KEY_ENV = "EXA_API_KEY"
_TIMEOUT = 30.0
_KEY_URL = "https://dashboard.exa.ai/api-keys"


# ---------------------------------------------------------------- HTTP
def _clean(s) -> str:
    return re.sub(r"\s+", " ", str(s if s is not None else "")).strip()


def _as_int(v, default: int) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _post(path: str, payload: dict, timeout: float = _TIMEOUT):
    """返回 (data, err)。data 为解析后的 JSON；err 为人类可读的失败说明。"""
    if requests is None:  # pragma: no cover
        return None, "requests 未安装，无法访问 Exa API。"
    key = keys_impl.find_key(_KEY_ENV)
    if not key:
        return None, keys_impl.missing_key_msg(_KEY_ENV, _KEY_URL, "Exa")
    url = _API + path
    headers = {"Authorization": f"Bearer {key}", "x-api-key": key,
               "Content-Type": "application/json"}
    last = ""
    for proxies in web_impl._proxy_candidates(url):
        try:
            r = requests.post(url, json=payload, headers=headers,
                              timeout=(min(8.0, timeout), timeout), proxies=proxies)
        except Exception as e:  # noqa: BLE001  网络类错误换下一条代理链
            last = f"{type(e).__name__}: {e}"
            continue
        if 200 <= r.status_code < 300:
            try:
                return r.json(), ""
            except ValueError:
                return None, f"Exa 返回非 JSON（HTTP {r.status_code}）：{r.text[:200]}"
        if r.status_code in (401, 403):
            return None, (f"Exa 鉴权失败（HTTP {r.status_code}）：key 无效或已过期。\n"
                          f"当前 key 来源：{keys_impl.key_source(_KEY_ENV)}。换新 key：{_KEY_URL}")
        if r.status_code == 402:
            return None, (f"Exa 要求付费（HTTP 402）：免费额度用尽或该端点需付费套餐。\n"
                          f"响应：{r.text[:200]}")
        if r.status_code == 429:
            return None, "Exa 限流（HTTP 429）：降低 num 或稍后重试。"
        if 400 <= r.status_code < 500:
            # 请求本身的问题，换代理重试没有意义
            return None, f"Exa 拒绝请求（HTTP {r.status_code}）：{r.text[:220]}"
        last = f"HTTP {r.status_code}: {r.text[:200]}"
    return None, f"Exa 请求失败（所有代理链都试过）：{last}"


# ---------------------------------------------------------------- 输出格式化
def _fmt_results(items: list, n: int) -> str:
    if not items:
        return "（无结果）"
    lines = []
    for i, it in enumerate(items[:n], 1):
        lines.append(f"{i}. {_clean(it.get('title')) or '(无标题)'}")
        meta = []
        if it.get("publishedDate"):
            meta.append(str(it["publishedDate"])[:10])
        if it.get("author"):
            meta.append(_clean(it["author"])[:60])
        if it.get("score") is not None:
            meta.append(f"score {it['score']}")
        if meta:
            lines.append("   " + " · ".join(meta))
        if it.get("url"):
            lines.append(f"   {it['url']}")
        snippet = it.get("summary") or it.get("text") or it.get("highlights")
        if isinstance(snippet, (list, tuple)):
            snippet = " ".join(str(x) for x in snippet)
        if isinstance(snippet, dict):
            snippet = " ".join(f"{k}: {v}" for k, v in snippet.items())
        snippet = _clean(snippet)
        if snippet:
            lines.append(f"   {snippet[:320]}")
    return "\n".join(lines)


def _cost_note(data: dict) -> str:
    total = (data.get("costDollars") or {}).get("total")
    return f"（花费 ${total}）" if total else ""


# ---------------------------------------------------------------- 工具
def exa_search(args: dict) -> str:
    query = _clean(args.get("query"))
    if not query:
        return "请提供查询（描述想找的页面，而不是关键词堆砌）。"
    num = max(1, min(_as_int(args.get("num"), 8), 25))
    payload = {"query": query, "numResults": num, "type": "auto"}
    cat = _clean(args.get("category"))
    if cat:
        payload["category"] = cat
    doms = _clean(args.get("include_domains"))
    if doms:
        payload["includeDomains"] = [d for d in re.split(r"[,\s]+", doms) if d]
    if args.get("text"):
        payload["contents"] = {"text": {"maxCharacters": 1200}}
    data, err = _post("/search", payload)
    if err:
        return err
    items = data.get("results") or []
    return (f"Exa 语义搜索「{query}」：{len(items)} 条{_cost_note(data)}\n"
            + _fmt_results(items, num))


def exa_answer(args: dict) -> str:
    q = _clean(args.get("question") or args.get("query"))
    if not q:
        return "请提供要回答的问题（question 参数）。"
    data, err = _post("/answer", {"query": q, "text": True})
    if err:
        return err
    out = [f"Exa 问答「{q}」{_cost_note(data)}", _clean(data.get("answer")) or "(无答案)"]
    cites = data.get("citations") or []
    if cites:
        out.append("引用：")
        for i, c in enumerate(cites, 1):
            if isinstance(c, dict):
                out.append(f"  [{i}] {_clean(c.get('title'))} {c.get('url') or ''}".rstrip())
            else:
                out.append(f"  [{i}] {c}")
    return "\n".join(out)


def exa_similar(args: dict) -> str:
    url = _clean(args.get("url"))
    if not url:
        return "请提供参考页面 URL。"
    num = max(1, min(_as_int(args.get("num"), 8), 25))
    data, err = _post("/findSimilar", {"url": url, "numResults": num})
    if err:
        return err
    items = data.get("results") or []
    return (f"Exa 相似页（参考 {url}）：{len(items)} 条{_cost_note(data)}\n"
            + _fmt_results(items, num))
