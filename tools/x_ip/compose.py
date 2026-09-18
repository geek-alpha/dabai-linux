"""文案层：把「一条热点 + 一个定位」变成几条有立场的推文草稿。

第一性原理：IP 不是靠发得多，是靠每条都带着同一个判断框架。所以这里不做
「新闻摘要」，而是逼模型回答三个问题——这事儿的本质是什么、跟我的读者有什么
关系、我因此改变哪个具体做法。答不出来就别发。

字数按 X 的加权规则算（CJK 字符权重 2，ASCII 权重 1，上限 280），
不按 len() 算——中文 270 字在 X 上等于 540，直接被拒。
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.request
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    from tools.x_ip import topics
else:
    from . import topics

SETTINGS = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "settings.json"
)
STT_CONFIG = os.path.join(os.path.dirname(SETTINGS), "stt_config.json")

# 角度库：给模型明确的输出维度，避免三条草稿说同一句话
ANGLES = [
    ("第一性原理", "拆到这件事的底层机制，问「它到底改变了什么约束」"),
    ("反常识判断", "说出一个和主流反应相反的判断，并给出为什么主流会看错"),
    ("具体经验", "用开发者/独立开发者的具体处境类比，给出一个能立刻做的动作"),
    ("趋势推演", "往前推 12 个月，这件事会变成什么，谁受益谁受损"),
    ("泼冷水", "指出这件事被高估的部分，以及真正被忽略的风险"),
]


def weighted_len(text: str) -> int:
    """X 的 twitter-text 加权长度：CJK 算 2，ASCII 算 1。"""
    w = 0
    for ch in text:
        cp = ord(ch)
        narrow = (
            0 <= cp <= 4351
            or 8192 <= cp <= 8205
            or 8208 <= cp <= 8223
            or 8242 <= cp <= 8247
        )
        w += 1 if narrow else 2
    return w


def _providers() -> list[dict]:
    """LLM 通道链，按顺序试。

    实测（2026-09-18）：settings.json 里的 DeepSeek key 返回 402 Insufficient Balance，
    本地 ollama 11434 未启动。siliconflow 的 key（复用 stt_config.json）chat 可用，
    所以主通道挂了不能直接降到模板——先把备用通道走完。
    """
    out: list[dict] = []

    env_base, env_key = os.environ.get("X_IP_LLM_BASE"), os.environ.get("X_IP_LLM_KEY")
    if env_base and env_key:
        out.append(
            {
                "name": "env",
                "base_url": env_base,
                "api_key": env_key,
                "model": os.environ.get("X_IP_LLM_MODEL", "deepseek-ai/DeepSeek-V3"),
            }
        )

    try:
        with open(SETTINGS, encoding="utf-8") as f:
            s = json.load(f)
        prof = (s.get("llm_profiles") or {}).get(s.get("llm_provider") or "custom") or {}
        base = prof.get("base_url") or s.get("base_url")
        key = prof.get("api_key") or s.get("api_key")
        if base and key:
            out.append(
                {
                    "name": "settings",
                    "base_url": base,
                    "api_key": key,
                    "model": prof.get("model") or s.get("model") or "deepseek-chat",
                }
            )
    except Exception:
        pass

    # siliconflow 兼做 chat 与 STT，已有 key 就不再要第二把钥匙
    try:
        with open(STT_CONFIG, encoding="utf-8") as f:
            stt = json.load(f)
        url = stt.get("api_url") or ""
        key = stt.get("api_key") or ""
        if key and "siliconflow" in url:
            out.append(
                {
                    "name": "siliconflow",
                    "base_url": "https://api.siliconflow.cn/v1",
                    "api_key": key,
                    "model": os.environ.get("X_IP_LLM_MODEL", "deepseek-ai/DeepSeek-V3"),
                }
            )
    except Exception:
        pass

    return out


def _chat_once(p: dict, messages: list[dict], temperature: float, timeout: float) -> str:
    body = json.dumps(
        {"model": p["model"], "messages": messages, "temperature": temperature},
        ensure_ascii=False,
    ).encode("utf-8")
    req = urllib.request.Request(
        p["base_url"].rstrip("/") + "/chat/completions",
        data=body,
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + p["api_key"]},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        d = json.loads(resp.read())
    return d["choices"][0]["message"]["content"]


def call_llm(
    messages: list[dict], temperature: float = 1.0, timeout: float = 180.0
) -> tuple[str, str]:
    """返回 (正文, 通道名)。所有通道都挂才抛异常。"""
    errs = []
    for p in _providers():
        try:
            return _chat_once(p, messages, temperature, timeout), f"{p['name']}:{p['model']}"
        except Exception as e:
            body = ""
            if hasattr(e, "read"):
                try:
                    body = e.read().decode("utf-8", "ignore")[:160]
                except Exception:
                    body = ""
            errs.append(f"{p['name']}({type(e).__name__}{' ' + body if body else ''})")
    raise RuntimeError("所有 LLM 通道都失败：" + " / ".join(errs))


def build_messages(topic: dict, persona: dict, n: int = 3, extra: str = "") -> list[dict]:
    pillars = topic.get("pillars") or []
    angles = ANGLES[: max(n, 1)]
    angle_txt = "\n".join(f"{i + 1}. 【{a}】{d}" for i, (a, d) in enumerate(angles))
    tags = " ".join(persona.get("hashtags") or [])

    sys = (
        f"你在为一个 X（推特）账号写推文，目标不是报道新闻，是建立个人 IP。\n\n"
        f"账号定位：{persona['positioning']}\n"
        f"读者：{persona['audience']}\n\n"
        f"写作要求：\n"
        + "\n".join(f"- {v}" for v in persona.get("voice") or [])
        + "\n\n"
        f"禁止：\n"
        + "\n".join(f"- {v}" for v in persona.get("avoid") or [])
        + f"\n\n硬约束：\n"
        f"- 长度上限：加权 {persona.get('safe_weighted', 262)}（中文汉字算 2、英文数字标点算 1），"
        f"换算过来：纯中文不超过 130 字，中英混排不超过 200 字符。宁短勿长。\n"
        f"- 每条必须给一个明确判断，不能只复述事实\n"
        f"- 结尾用一句真问题收尾，不用「你怎么看？」这种客套话\n"
        f"- 话题标签最多 2 个，候选：{tags}\n"
        f"- 中文输出，不用 emoji 堆砌，不用感叹号连发\n"
        f"- 不要出现「值得注意」「深入探讨」「赋能」这类套话\n"
        f"- 不编造动机、不做阴谋论推测（实测模型会写出「这是招聘工具，刻意留漏洞筛人」这种无依据断言，直接毁可信度）\n"
        f"- 不确定的事就标成推测，不要当事实说\n"
        f"- 保留换行分段，一段一个意思，不要挤成一个密实段落\n"
    )
    user = (
        f"热点：{topic['title']}\n"
        f"来源：{topic.get('source', '')} 榜第 {topic.get('rank', '?')} 名"
        + (f"，命中领域：{'/'.join(pillars)}" if pillars else "")
        + (f"\n链接：{topic['url']}" if topic.get("url") else "")
        + f"\n\n请写 {n} 条推文，每条用一个不同角度：\n{angle_txt}\n\n"
        + (extra + "\n\n" if extra else "")
        + "只输出 JSON 数组，格式：[{\"angle\":\"角度名\",\"text\":\"推文正文\"}]，不要任何解释文字。"
    )
    return [{"role": "system", "content": sys}, {"role": "user", "content": user}]


_JSON_ARR = re.compile(r"\[.*\]", re.S)


def parse_candidates(raw: str) -> list[dict]:
    m = _JSON_ARR.search(raw or "")
    if not m:
        return []
    try:
        arr = json.loads(m.group(0))
    except json.JSONDecodeError:
        return []
    out = []
    for it in arr:
        if isinstance(it, dict) and (it.get("text") or "").strip():
            out.append({"angle": str(it.get("angle") or ""), "text": it["text"].strip()})
    return out


def validate(text: str, persona: dict) -> list[str]:
    """返回问题列表，空列表 = 可发。"""
    probs = []
    wl = weighted_len(text)
    lim = persona.get("safe_weighted", 262)
    if wl > lim:
        probs.append(f"超长：加权 {wl} > {lim}")
    if len(text.strip()) < 25:
        probs.append(f"过短：{len(text.strip())} 字，信息量不足")
    if text.count("#") > 2:
        probs.append(f"话题标签过多：{text.count('#')} 个")
    if text.count("！") + text.count("!") >= 2:
        probs.append("感叹号堆砌")
    for w in ["值得注意", "深入探讨", "赋能", "善用"]:
        if w in text:
            probs.append(f"套话：{w}")
    return probs


def repair(cands: list[dict], persona: dict) -> int:
    """超长/带问题的草稿压回限内。返回修好的条数。

    模型算不准加权字数（实测 284/266 都超了 262），所以不能只靠提示词，
    必须有一道事后的压缩。压缩失败就用硬截断兜底，绝不让超长草稿进发布链路。
    """
    lim = persona.get("safe_weighted", 262)
    fixed = 0
    for c in cands:
        if not c.get("problems"):
            continue
        if weighted_len(c["text"]) > lim:
            try:
                msg = [
                    {
                        "role": "system",
                        "content": (
                            "你是中文推文编辑。把用户给的推文压缩到指定长度内，"
                            "保留最尖锐的那个判断和结尾的问题，删掉铺垫和重复。"
                            "保留原有的换行分段，不要挤成一个密实段落。"
                            "只输出压缩后的正文，不要解释、不要引号。"
                        ),
                    },
                    {
                        "role": "user",
                        "content": (
                            f"压缩到加权 {lim - 10} 以内（中文汉字算 2、英文数字标点算 1）：\n\n"
                            + c["text"]
                        ),
                    },
                ]
                new, _ = call_llm(msg, temperature=0.6, timeout=90.0)
                new = new.strip().strip('"').strip()
                if new and weighted_len(new) < weighted_len(c["text"]):
                    c["text"] = new
            except Exception:
                pass
        if weighted_len(c["text"]) > lim:
            c["text"] = _hard_trim(c["text"], lim)
        c["problems"] = validate(c["text"], persona)
        c["weighted"] = weighted_len(c["text"])
        if not c["problems"]:
            fixed += 1
    return fixed


def _hard_trim(text: str, lim: int) -> str:
    """最后兵底：按行删，尽量保住结尾那句问题。"""
    lines = [ln for ln in text.split("\n") if ln.strip()]
    tail = lines[-1] if lines else ""
    body = lines[:-1]
    while body and weighted_len("\n".join(body + [tail])) > lim:
        body.pop()
    out = "\n".join(body + [tail])
    if weighted_len(out) <= lim:
        return out
    # 连尾句都放不下，硬切尾句
    res = ""
    for ch in tail:
        if weighted_len(res + ch + "…") > lim:
            break
        res += ch
    return res + "…"


def generate(
    topic: dict, persona: dict | None = None, n: int = 3, extra: str = ""
) -> tuple[list[dict], str]:
    """返回 (候选列表, 说明)。候选里带 problems 字段，空 = 干净。"""
    p = persona or topics.load_persona()
    model = ""
    try:
        raw, model = call_llm(build_messages(topic, p, n, extra))
        cands = parse_candidates(raw)
        src = f"llm[{model}]"
    except Exception as e:
        cands = []
        src = f"llm失败({e})"
    if not cands:
        cands = fallback(topic, p)
        src += " → 用模板兜底"
    for c in cands:
        c["problems"] = validate(c["text"], p)
        c["weighted"] = weighted_len(c["text"])
    n_fixed = repair(cands, p)
    if n_fixed:
        src += f" | 压缩修复 {n_fixed} 条"
    return cands, src


def _short_title(title: str, limit: int = 56) -> str:
    """骨架要嵌标题，长标题会把加权字数顶爆（实测 GitHub 描述型标题加权 279 > 262）。"""
    t = title.strip()
    return t if len(t) <= limit else t[: limit - 1] + "…"


def fallback(topic: dict, persona: dict) -> list[dict]:
    """LLM 不可用时的离线兜底：给出「论点骨架」而不是成品，避免发空话。"""
    t = _short_title(topic["title"])
    pillars = "/".join(topic.get("pillars") or []) or "技术"
    return [
        {
            "angle": "第一性原理（骨架·需手写）",
            "text": (
                f"{t}\n\n"
                f"剥掉热闹，这件事真正改变的是哪个约束？\n"
                f"我先说我的判断：它动的是{pillars}这条线上的成本结构，"
                f"不是功能层面的事。\n\n"
                f"你在这条线上最贵的一步是什么？"
            ),
        },
        {
            "angle": "泼冷水（骨架·需手写）",
            "text": (
                f"关于「{_short_title(topic['title'], 34)}」，被高估的部分在哪？\n\n"
                f"我的判断：短期受益的是已经有存量的人，新人拿到的只是入场券。\n\n"
                f"你上一次因为一条热点改变做法，是什么时候？"
            ),
        },
    ]


if __name__ == "__main__":
    from tools.x_ip import sources

    kw = sys.argv[1] if len(sys.argv) > 1 else ""
    items, _ = sources.fetch_all(per_source=15)
    rows = topics.rank(items)
    if kw:
        rows = [r for r in rows if kw in r["title"]]
    if not rows:
        print("没有匹配的选题")
        raise SystemExit(1)
    row = rows[0]
    print(f"选题：{row['title']}\n{topics.explain(row)}\n")
    cands, src = generate(row, n=3)
    print(f"[{src}]\n")
    for i, c in enumerate(cands, 1):
        print(f"--- 候选 {i} · {c['angle']} · 加权 {c['weighted']} ---")
        print(c["text"])
        if c["problems"]:
            print("  ⚠ " + "；".join(c["problems"]))
        print()
