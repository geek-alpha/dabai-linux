"""选题层：把「热」和「跟我的 IP 有关」合成一个可排序的分数。

第一性原理：追热点的目的是被推荐给对的人。热度决定曝光上限，契合度决定
关注转化率——两者相乘才有意义，所以纯热点榜不能直接用。

score = 源权重衰减 × 契合度系数 × 跨源共振加成
契合度为 0（且非通用话题）时整体归零：宁可少发，不做无关热点。
"""

from __future__ import annotations

import json
import os
import re
import sys
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    from tools.x_ip import sources
else:
    from . import sources

PERSONA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "persona.json")

# 通用话题：跟任何 IP 都能搭上话，契合度按中位处理，不然会被 pillar 关键词误杀
GENERIC = ["为什么", "如何", "怎么", "专家", "回应", "调查", "通报", "辟谣", "真相", "建议", "提醒"]


def load_persona(path: str = PERSONA_PATH) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _hit(text: str, words: list[str]) -> list[str]:
    t = text.lower()
    return [w for w in words if w.lower() in t]


def fit(title: str, persona: dict) -> tuple[float, list[str], list[str]]:
    """返回 (契合度 0~1, 命中的 pillar 名, 命中的关键词)。"""
    total_hits: list[str] = []
    pillars_hit: list[str] = []
    for name, words in (persona.get("pillars") or {}).items():
        h = _hit(title, words)
        if h:
            pillars_hit.append(name)
            total_hits.extend(h)

    if not total_hits:
        # 通用话题（“如何/回应/通报”）给很低的底分：它不建垂直 IP，只不应直接归零
        return (0.15 if _hit(title, GENERIC) else 0.0), [], []

    # 命中越多越契合，但衰减——3 个词和 5 个词差别没那么大
    base = min(1.0, 0.5 + 0.12 * len(set(total_hits)))
    if len(pillars_hit) > 1:
        base = min(1.0, base + 0.08)  # 跨领域交叉话题通常更好写
    return base, pillars_hit, sorted(set(total_hits))


def is_banned(title: str, persona: dict) -> str:
    h = _hit(title, persona.get("banned_keywords") or [])
    return h[0] if h else ""


def score_one(it: Any, persona: dict) -> dict:
    """给单条热点算最终分，附带可解释的理由（方便人工判断为什么排前面）。

    base 开方、fit 开方——目的是让契合度主导排序。实测旧公式（线性相乘）下，
    微博 #1 的生活类话题（fit=0.35）能压过 V2EX 的真技术帖（fit=0.74），
    因为源权重/名次的差距（3 倍）大于契合度差距（2 倍）。
    """
    ban = is_banned(it.title, persona)
    fitv, pillars_hit, kws = fit(it.title, persona)
    base = it.score()
    resonance = 1.0 + 0.35 * (len(getattr(it, "sources", []) or []) - 1)
    final = 0.0 if ban else (base**0.5) * (fitv**1.8) * resonance
    return {
        "key": it.key,
        "title": it.title,
        "source": it.source,
        "sources": getattr(it, "sources", []),
        "rank": it.rank,
        "heat": it.heat,
        "url": it.url,
        "base": round(base, 4),
        "fit": round(fitv, 3),
        "pillars": pillars_hit,
        "keywords": kws,
        "banned": ban,
        "score": round(final, 5),
    }


def rank(
    items: list[Any],
    persona: dict | None = None,
    drop_banned: bool = True,
    min_score: float | None = None,
) -> list[dict]:
    p = persona or load_persona()
    rows = [score_one(it, p) for it in items]
    if drop_banned:
        rows = [r for r in rows if not r["banned"]]
    thr = p.get("min_score", 0.02) if min_score is None else min_score
    rows = [r for r in rows if r["score"] >= thr]
    return sorted(rows, key=lambda r: -r["score"])


def explain(row: dict) -> str:
    bits = [f"{row['source']}#{row['rank']}"]
    if len(row.get("sources") or []) > 1:
        bits.append("跨源:" + "+".join(row["sources"]))
    if row.get("pillars"):
        bits.append("契合:" + "/".join(row["pillars"]))
    if row.get("keywords"):
        bits.append("词:" + ",".join(row["keywords"][:4]))
    if not row.get("keywords") and row["fit"] >= 0.15:
        bits.append("通用话题")
    return " | ".join(bits)


def from_db(limit: int = 60, db: str | None = None) -> list[dict]:
    """从状态库读回待选热点，转成 rank() 能吃的 Hot 对象。"""
    from . import store

    rows = store.top_topics(limit=limit, statuses=("new",), db=db or store.DEFAULT_DB)
    out = []
    for r in rows:
        h = sources.Hot(
            source=r["source"] or "",
            rank=r["rank"] or 99,
            title=r["title"],
            heat=r["heat"] or 0,
            url=r["url"] or "",
        )
        h.key = r["key"]
        h.sources = (r["sources"] or "").split(",") if r["sources"] else []
        out.append(h)
    return out


def brief(rows: list[dict], limit: int = 12) -> str:
    lines = []
    for i, r in enumerate(rows[:limit], 1):
        lines.append(f"{i:2d}. [{r['score']:.4f}] fit={r['fit']:.2f} {r['title']}")
        lines.append(f"      {explain(r)}")
    return "\n".join(lines)


if __name__ == "__main__":
    items, errs = sources.fetch_all(per_source=12)
    rows = rank(items)
    print(brief(rows, 15))
    if errs:
        print("采集错误:", errs)
