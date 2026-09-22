"""常驻状态段的「按需读全文」入口 —— 与技能的渐进披露同构。

背景（2026-09-22）：长期事业 / 经验库 / 信条 / 联邦来信四段每轮常驻，且都靠截断
压体积——截断只保证「看不全」，不保证「看到该看的」。主 agent 对技能的做法是
摘要常驻 + skill_help 按需拉全文；这里照同一套做：注入段一行摘要，全文经
context_read 工具取。数据源仍是各台账文件，本模块只做读与渲染，不写。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

NAMES = ("long_horizon", "lessons", "conviction", "peer")

_ALIASES = {
    "long_horizon": "long_horizon", "longterm": "long_horizon", "long-term": "long_horizon",
    "长期事业": "long_horizon", "事业": "long_horizon",
    "lessons": "lessons", "lesson": "lessons", "经验库": "lessons", "经验": "lessons",
    "conviction": "conviction", "convictions": "conviction", "信条": "conviction",
    "peer": "peer", "联邦": "peer", "联邦来信": "peer",
}

LESSON_INDEX_CHARS = 100   # 索引每条的主题句长度：索引是用来「认出该读哪条」的
LESSON_INDEX_LIMIT = 60    # 索引一次给多少条（最新在前）；单条全文另取


def _base_dir() -> Path:
    try:
        from harness import get_harness

        return Path(get_harness().base_dir)
    except Exception:
        return Path(__file__).resolve().parent.parent


def _load(fname: str) -> dict:
    try:
        p = _base_dir() / fname
        if not p.exists():
            return {}
        d = json.loads(p.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _clip(s, n: int) -> str:
    s = str(s if s is not None else "")
    return s if len(s) <= n else s[: n - 1] + "…"


def _key(text) -> str:
    """与 tools/lesson_add.py:_key 同构：同一条经验在两处的键必须一致。"""
    return hashlib.sha1(str(text).encode("utf-8")).hexdigest()[:12]


def normalize(name: str) -> str:
    """别名/中文名 → 规范名；不认识返回空串。"""
    return _ALIASES.get(str(name or "").strip().lower(), "")


def _long_horizon(key: str = "") -> str:
    d = _load("long_horizon.json")
    ps = d.get("projects") or []
    qs = [q for q in (d.get("questions") or []) if q.get("status") == "open"]
    if not ps and not qs:
        return "长期事业台账为空。"
    if key:
        p = next((x for x in ps if str(x.get("id")) == key), None)
        if p is None:
            ids = ", ".join(str(x.get("id")) for x in ps) or "（无）"
            return f"没有这个项目：{key}。可用 id：{ids}"
        out = [f"{p.get('title')}（{p.get('id')}）[{p.get('stage')}] {p.get('progress', 0)}%"]
        for label, k in (("为什么", "why"), ("价值", "value"), ("验收", "done_when"),
                         ("下一步", "next"), ("卡在主人", "block_why")):
            if p.get(k):
                out.append(f"  {label}：{p[k]}")
        for e in (p.get("log") or []):
            ev = f"（{e.get('ev')}）" if e.get("ev") else ""
            out.append(f"    {e.get('t', '')} {e.get('what', '')}{ev}")
        return "\n".join(out)
    out = [f"长期事业（{len(ps)} 项，全量未截断）："]
    for p in ps:
        mark = "⏳" if p.get("owner_block") else ("▶" if p.get("stage") == "active" else "⏸")
        out.append(f"{mark} [{p.get('progress', 0):>3}%] {p.get('title')} ({p.get('id')})")
        if p.get("next"):
            out.append(f"     下一步：{p['next']}")
        lg = p.get("log") or []
        if lg:
            out.append(f"     最近：{lg[0].get('t', '')} {_clip(lg[0].get('what', ''), 80)}")
    if qs:
        out.append("悬而未决：")
        out.extend(f"  {i}. {q.get('text')}" for i, q in enumerate(qs, 1))
    out.append('取单个项目全文：context_read("long_horizon", "<id>")')
    return "\n".join(out)


def _lessons(key: str = "") -> str:
    ls = [str(x) for x in (_load("harness_task_memory.json").get("lessons") or [])]
    if not ls:
        return "经验库为空。"
    if key:
        if key.isdigit() and 1 <= int(key) <= len(ls):
            t = ls[int(key) - 1]
        else:
            hit = [t for t in ls if _key(t).startswith(key)]
            if len(hit) != 1:
                return (f"匹配 {len(hit)} 条，换个更长的 hash 前缀"
                        if hit else f"没有匹配：{key}（可用序号 1~{len(ls)} 或 hash 前缀）")
            t = hit[0]
        return f"[{_key(t)}]\n{t}"
    out = [f"经验库共 {len(ls)} 条（最新在前，每条只给主题句）："]
    for i, t in enumerate(ls[:LESSON_INDEX_LIMIT], 1):
        out.append(f"{i:>3}. [{_key(t)}] {_clip(t, LESSON_INDEX_CHARS)}")
    if len(ls) > LESSON_INDEX_LIMIT:
        out.append(f"…（还有 {len(ls) - LESSON_INDEX_LIMIT} 条）")
    out.append('取单条全文：context_read("lessons", "<序号|hash前缀>")')
    return "\n".join(out)


def _conviction(key: str = "") -> str:
    d = _load("conviction.json")
    cs = d.get("convictions") or []
    vs = d.get("vetoes") or []
    if not cs and not vs:
        return "主体性台账为空。"
    out = [f"信条（{len(cs)} 条，可被挑战，过不了 status.py challenge 就降级或删）："]
    for c in cs:
        out.append(f"- {c.get('text', '')}")
        if c.get("why"):
            out.append(f"    依据：{c['why']}")
    if vs:
        out.append(f"拒绝过（{len(vs)} 条，主体性的直接证据）：")
        for v in vs:
            out.append(f"- {v.get('claim', '')}")
            if v.get("why"):
                out.append(f"    理由：{v['why']}")
    return "\n".join(out)


def _peer(key: str = "") -> str:
    try:
        import peer_mesh as pm

        s = pm.unread_summary(preview=20)
    except Exception as e:
        return f"联邦收件箱读取失败：{e.__class__.__name__}: {e}"
    n = int(s.get("count") or 0)
    out = [f"联邦未读 {n} 条（正文已到本机，读信动作需管理员授权）。"]
    for m in s.get("items") or []:
        out.append(f"- [{m.get('from', '?')}] {m.get('text', '')}")
    out.append('读全文：先 python peer_admin.py grant --scope read --ttl 600 --why "…"，'
               '再用 mcp 调 peer(action=inbox)。没授权就别动它。')
    return "\n".join(out)


_READERS = {
    "long_horizon": _long_horizon,
    "lessons": _lessons,
    "conviction": _conviction,
    "peer": _peer,
}


def read(name: str, key: str = "") -> str:
    """按需读全文。name 支持别名；不认识的名字回退为总览 + 可用清单（不抛异常）。"""
    canon = normalize(name)
    if not canon:
        head = brief()
        tip = (f"不认识的状态段：{name}。可用：{'、'.join(NAMES)}"
               "（long_horizon 可带项目 id，lessons 可带序号或 hash 前缀）")
        return f"{head}\n{tip}" if head else tip
    try:
        return _READERS[canon](str(key or "").strip())
    except Exception as e:
        return f"读取 {canon} 失败：{e.__class__.__name__}: {e}"


def brief() -> str:
    """注入段的摘要：只报「有多少、最该看哪一条」，全文交给 context_read。

    刻意不带各段的截断正文——那些截断既占每轮成本，又给不出可据以行动的完整判据。
    """
    parts = []
    try:
        d = _load("long_horizon.json")
        act = [p for p in (d.get("projects") or []) if p.get("stage") == "active"]
        if act:
            top = max(act, key=lambda x: int(x.get("progress") or 0))
            line = f"长期事业 {len(act)} 项进行中，最靠前：{top.get('title')} [{top.get('progress', 0)}%]"
            if top.get("next"):
                line += f"，接力棒：{_clip(top['next'], 60)}"
            parts.append(line)
    except Exception:
        pass
    try:
        n = len(_load("harness_task_memory.json").get("lessons") or [])
        if n:
            parts.append(f"经验库 {n} 条")
    except Exception:
        pass
    try:
        d = _load("conviction.json")
        n = len(d.get("convictions") or [])
        if n:
            parts.append(f"信条 {n} 条")
    except Exception:
        pass
    try:
        import peer_mesh as pm

        n = int((pm.unread_summary(preview=0) or {}).get("count") or 0)
        if n:
            parts.append(f"联邦未读 {n} 条")
    except Exception:
        pass
    if not parts:
        return ""
    return ("【跨会话状态·摘要】" + "；".join(parts) + "。"
            '要读全文调 context_read(name)：name=long_horizon/lessons/conviction/peer，'
            "可带 key 取单条（如 context_read(\"long_horizon\", \"self-iterate\")）。")
