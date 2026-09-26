# -*- coding: utf-8 -*-
"""Tavily 检索 / 抽取 / 深度研究（search 技能的 tavily 引擎）。

原生 HTTP 实现，不依赖 tvly CLI（本机未安装；装 CLI 只是多一层搬运，不是能力）。
端点实测（2026-09-26，假 key 探针）：POST /search、/extract、/research 与 GET /research/{id}
全部返回 401 Unauthorized——链路直达、鉴权层可达，只差一个有效 key。
鉴权：Authorization: Bearer tvly-xxx。

/research 是异步任务：POST 建任务拿 request_id → 轮询 GET /research/{request_id} 直到
status 离开 pending。轮询状态值取自官方 OpenAPI（pending/completed/failed），
另外兼容 succeeded/done/error 等同义写法——上游措辞变一次就整条链断掉，不值当。
"""
from __future__ import annotations

import json
import os
import re
import sys
import time

_SKILL_DIR = os.path.dirname(os.path.abspath(__file__))
if _SKILL_DIR not in sys.path:
    sys.path.insert(0, _SKILL_DIR)

import keys_impl  # noqa: E402
import web_impl  # noqa: E402

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None

_API = "https://api.tavily.com"
_KEY_ENV = "TAVILY_API_KEY"
_KEY_URL = "https://app.tavily.com/home"
_TIMEOUT = 60.0
_POLL_INTERVAL = 4.0

_DONE = {"completed", "complete", "succeeded", "success", "done", "finished"}
_FAIL = {"failed", "failure", "error", "cancelled", "canceled"}


def _clean(s) -> str:
    return re.sub(r"\s+", " ", str(s if s is not None else "")).strip()


def _as_int(v, default: int) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------- HTTP
def _request(method: str, path: str, payload: dict | None = None,
             timeout: float = _TIMEOUT):
    """返回 (data, err)。401/403 直接判定鉴权问题，不再换代理重试。"""
    if requests is None:  # pragma: no cover
        return None, "requests 未安装，无法访问 Tavily API。"
    key = keys_impl.find_key(_KEY_ENV)
    if not key:
        return None, keys_impl.missing_key_msg(_KEY_ENV, _KEY_URL, "Tavily")
    url = _API + path
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    last = ""
    for proxies in web_impl._proxy_candidates(url):
        try:
            r = requests.request(method, url, json=payload, headers=headers,
                                 timeout=(min(8.0, timeout), timeout), proxies=proxies)
        except Exception as e:  # noqa: BLE001
            last = f"{type(e).__name__}: {e}"
            continue
        if 200 <= r.status_code < 300:
            try:
                return r.json(), ""
            except ValueError:
                return None, f"Tavily 返回非 JSON（HTTP {r.status_code}）：{r.text[:200]}"
        if r.status_code in (401, 403):
            return None, (f"Tavily 鉴权失败（HTTP {r.status_code}）：key 无效或已过期。\n"
                          f"当前 key 来源：{keys_impl.key_source(_KEY_ENV)}。换新 key：{_KEY_URL}")
        if r.status_code == 429:
            return None, "Tavily 限流（HTTP 429）：降低 max_results 或稍后重试。"
        if r.status_code == 432 or r.status_code == 433:
            return None, f"Tavily 额度/套餐限制（HTTP {r.status_code}）：{r.text[:200]}"
        if 400 <= r.status_code < 500:
            return None, f"Tavily 拒绝请求（HTTP {r.status_code}）：{r.text[:220]}"
        last = f"HTTP {r.status_code}: {r.text[:200]}"
    return None, f"Tavily 请求失败（所有代理链都试过）：{last}"


# ---------------------------------------------------------------- 输出格式化
def _fmt_search(data: dict, n: int) -> str:
    items = data.get("results") or []
    lines = [f"Tavily 搜索：{len(items)} 条"]
    ans = _clean(data.get("answer"))
    if ans:
        lines.append(f"直接答案：{ans}")
    for i, it in enumerate(items[:n], 1):
        lines.append(f"{i}. {_clean(it.get('title')) or '(无标题)'}")
        meta = []
        if it.get("published_date"):
            meta.append(str(it["published_date"])[:10])
        if it.get("score") is not None:
            meta.append(f"score {it['score']}")
        if meta:
            lines.append("   " + " · ".join(meta))
        if it.get("url"):
            lines.append(f"   {it['url']}")
        body = _clean(it.get("content") or it.get("raw_content"))
        if body:
            lines.append(f"   {body[:320]}")
    credits = (data.get("usage") or {}).get("credits")
    if credits:
        lines.append(f"（消耗 {credits} credits）")
    return "\n".join(lines)


def _fmt_extract(data: dict) -> str:
    ok = data.get("results") or []
    bad = data.get("failed_results") or []
    lines = [f"Tavily 抽取：成功 {len(ok)} 个，失败 {len(bad)} 个"]
    for i, it in enumerate(ok, 1):
        body = it.get("raw_content") or it.get("content") or ""
        lines.append(f"--- [{i}] {it.get('url')} （{len(body)} 字符）")
        lines.append(body[:4000] if body else "(空内容)")
    for it in bad:
        lines.append(f"--- 失败：{it.get('url')} → {it.get('error') or it.get('status')}")
    return "\n".join(lines)


def _report_text(data: dict) -> str:
    """研究报告的正文可能在 output / result / content / answer / report 里，按序取第一个非空。"""
    for k in ("output", "result", "content", "answer", "report", "response"):
        v = data.get(k)
        if isinstance(v, str) and v.strip():
            return v
        if isinstance(v, dict):
            for kk in ("content", "text", "answer", "report"):
                if isinstance(v.get(kk), str) and v[kk].strip():
                    return v[kk]
    return ""


def _fmt_research(data: dict) -> str:
    body = _report_text(data)
    lines = [f"Tavily 深度研究（status={data.get('status')}，request_id={data.get('request_id')}）"]
    lines.append(body.strip() if body else json.dumps(data, ensure_ascii=False)[:2000])
    srcs = data.get("sources") or data.get("citations") or (data.get("results") if isinstance(data.get("results"), list) else None)
    if srcs:
        lines.append("来源：")
        for i, s in enumerate(srcs[:20], 1):
            if isinstance(s, dict):
                lines.append(f"  [{i}] {_clean(s.get('title'))} {s.get('url') or ''}".rstrip())
            else:
                lines.append(f"  [{i}] {s}")
    credits = (data.get("usage") or {}).get("credits")
    if credits:
        lines.append(f"（消耗 {credits} credits）")
    return "\n".join(lines)


# ---------------------------------------------------------------- 工具
def tavily_search(args: dict) -> str:
    query = _clean(args.get("query"))
    if not query:
        return "请提供搜索关键词（query 参数）。"
    payload = {"query": query,
               "max_results": max(1, min(_as_int(args.get("max_results"), 5), 20))}
    depth = _clean(args.get("depth")).lower()
    if depth in ("basic", "advanced", "fast", "ultra-fast"):
        payload["search_depth"] = depth
    for src, dst in (("time_range", "time_range"), ("topic", "topic")):
        v = _clean(args.get(src))
        if v:
            payload[dst] = v
    dom = _clean(args.get("include_domains"))
    if dom:
        payload["include_domains"] = [d for d in re.split(r"[,\s]+", dom) if d]
    if args.get("include_answer"):
        payload["include_answer"] = True
    data, err = _request("POST", "/search", payload)
    if err:
        return err
    return _fmt_search(data, _as_int(args.get("max_results"), 5))


def tavily_extract(args: dict) -> str:
    url = _clean(args.get("url"))
    urls = args.get("urls")
    if isinstance(urls, str):
        urls = [u for u in re.split(r"[,\s]+", urls) if u]
    if not url and not urls:
        return "请提供要提取的 URL（url 或 urls 参数）。"
    payload = {"urls": [url] if url else list(urls)[:20], "format": "markdown"}
    data, err = _request("POST", "/extract", payload)
    if err:
        return err
    return _fmt_extract(data)


def tavily_research(args: dict) -> str:
    """建研究任务并轮询到完成。传 request_id 时改为复查既有任务（不新建）。"""
    rid = _clean(args.get("request_id"))
    timeout = float(max(30, min(_as_int(args.get("timeout"), 180), 600)))
    if not rid:
        topic = _clean(args.get("topic") or args.get("input"))
        if not topic:
            return "请提供研究主题（topic 参数）。"
        payload = {"input": topic, "model": _clean(args.get("model")) or "auto",
                   "stream": False}
        data, err = _request("POST", "/research", payload, timeout=90.0)
        if err:
            return err
        rid = _clean(data.get("request_id") or data.get("id"))
        if not rid:
            return f"Tavily 未返回 request_id：{json.dumps(data, ensure_ascii=False)[:300]}"
        status = _clean(data.get("status")).lower()
        if status in _DONE:
            return _fmt_research(data)
    deadline = time.monotonic() + timeout
    last_status = ""
    while True:
        data, err = _request("GET", f"/research/{rid}")
        if err:
            return err
        last_status = _clean(data.get("status")).lower()
        if last_status in _DONE:
            return _fmt_research(data)
        if last_status in _FAIL:
            return (f"Tavily 研究任务 {rid} 失败（status={last_status}）："
                    f"{json.dumps(data, ensure_ascii=False)[:400]}")
        if time.monotonic() >= deadline:
            return (f"Tavily 研究任务 {rid} 超时（{timeout:.0f}s，当前 status={last_status or '未知'}）。\n"
                    f"任务仍在后台跑：稍后传 request_id={rid} 复查结果即可。")
        time.sleep(_POLL_INTERVAL)
