"""离线压缩质量评测（零 token）：度量窗口内的实体截断损失。

为什么是零 token：Hermes 的 evals/compaction 让模型对「压缩前 vs 压缩后」各答一遍
再判分，一题两次调用、每次 16k 视图上限 —— 百题就是百万级 input token。
但「丢没丢」不必问模型：把事实性实体（路径、文件:行号、数字、命令、标识符、
结论词）抽成集合，压缩前后求差集就能算保留率，纯文本比对。

★ 基线必须是「同一轮的原文」，不是整个会话的历史。
   实测踩过：拿 174 万 token 的全会话当分母，窗口按设计只留 8000 token，
   保留率算出 1%~2% —— 那是在量「窗口本来就不该装下全部历史」这个设计，
   不是在量缺陷。真正会丢东西的是窗口**内部**的三处截断：
     · per_round=1200 字符截断 user/assistant 正文；
     · per_tool=500 字符截断工具结果；
     · keep_last_tools=3 只留最近 3 次工具交互；
   以及「旧轮整轮跳过」（预算放不下就整轮不要）。

两级损失分开量，因为修法不同：
  A. 轮内截断：轮还在，内容被砍 → 调上限或改摘要式压缩；
  B. 整轮跳过：轮没了 → 调预算/保底轮数，或接受（越旧越该丢）。
"""
import argparse
import json
import os
import re
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import agent as A   # noqa: E402
import memory as M  # noqa: E402

# 实体 = 「下一轮还要用得上、丢了就要重新查一遍」的事实。
PATTERNS = [
    ("文件行", re.compile(r"[\w./\-]+\.(?:py|ts|tsx|js|json|md|sh|db|log|yaml|yml|toml|css|html|vrm|mp3|wav)(?::\d+(?:-\d+)?)?")),
    ("路径", re.compile(r"(?:/[\w.\-\u4e00-\u9fff]+){2,}")),
    ("URL", re.compile(r"https?://[^\s，。）)】]+")),
    ("命令", re.compile(r"(?:venv/bin/python3?|python3?|git|npm|npx|pnpm|curl|rg|grep|systemctl|docker|pytest|ffmpeg|adb|pm2)\s+[^\n，。；)）]{2,60}")),
    ("数字", re.compile(r"(?<![\w.])\d{3,}(?:\.\d+)?(?![\w])")),
    ("标识符", re.compile(r"(?<![\w.])(?:[a-z][a-z0-9]*_[a-z0-9_]{3,}|[a-z]+[A-Z][A-Za-z0-9]{2,})(?![\w])")),
    ("结论词", re.compile(r"已做|别重开|根因|结论|失败|超时|回滚|上线|收口|未完成|待办|坑")),
]

_WS = re.compile(r"\s+")


def entities(text: str) -> dict:
    out = {}
    for cls, pat in PATTERNS:
        got = set()
        for m in pat.findall(text or ""):
            s = _WS.sub("", m) if cls != "命令" else _WS.sub(" ", m).strip()
            if cls in ("标识符", "命令", "结论词"):
                s = s.lower()
            got.add(s)
        if got:
            out[cls] = got
    return out


def _msgs_text(msgs) -> str:
    parts = []
    for m in msgs or []:
        parts.append(m.get("content") or "")
        for col in ("tool_calls", "tool_results"):
            v = m.get(col)
            if v:
                parts.append(v if isinstance(v, str) else json.dumps(v, ensure_ascii=False))
    return "\n".join(parts)


def _rate(keep: dict, base: dict):
    per, hit, tot = {}, 0, 0
    for cls, s in base.items():
        h = len(s & keep.get(cls, set()))
        per[cls] = (h, len(s))
        hit += h
        tot += len(s)
    return per, hit, tot


def _cfg():
    with open(os.path.join(BASE, "settings.json"), "r", encoding="utf-8") as f:
        m = (json.load(f) or {}).get("memory") or {}
    return {
        "budget": m.get("short_term_max_tokens", M.SHORT_TERM_MAX_TOKENS),
        "per_round": m.get("short_term_max_chars_per_round", M.SHORT_TERM_MAX_CHARS_PER_ROUND),
        "min_rounds": m.get("short_term_min_rounds", M.SHORT_TERM_MIN_ROUNDS),
        "per_tool": m.get("short_term_max_chars_per_tool", M.SHORT_TERM_MAX_CHARS_PER_TOOL),
        "per_call": m.get("short_term_max_chars_per_tool_call", M.SHORT_TERM_MAX_CHARS_PER_TOOL_CALL),
        "keep_tools": m.get("short_term_keep_last_tools", M.SHORT_TERM_KEEP_LAST_TOOLS),
        "per_tool_newest": m.get("short_term_max_chars_per_tool_newest",
                                 M.SHORT_TERM_MAX_CHARS_PER_TOOL_NEWEST),
        "keep_tools_newest": m.get("short_term_keep_last_tools_newest",
                                   M.SHORT_TERM_KEEP_LAST_TOOLS_NEWEST),
    }


def _pj(v):
    if v is None:
        return None
    if isinstance(v, str):
        try:
            return json.loads(v)
        except Exception:
            return None
    return v


def _raw(conn, sid):
    rows = conn.execute(
        "SELECT id, role, content, tool_calls, tool_results FROM messages "
        "WHERE session_id=? AND (source != 'auto' OR role = 'assistant') "
        "ORDER BY id", (sid,)).fetchall()
    return [{"role": m["role"], "content": m["content"] or "",
             "tool_calls": _pj(m["tool_calls"]), "tool_results": _pj(m["tool_results"])}
            for m in rows]


def _age_bucket(k):
    """k=0 是最新一轮（0-based，从新往旧）。"""
    if k == 0:
        return "最新轮"
    if k < 6:
        return "最近2-6轮"
    if k < 12:
        return "第7-12轮"
    return "更旧"


def eval_session(sid, raw, cfg, window):
    rounds = M._group_history_rounds(raw)
    detail = []
    for k in range(min(window, len(rounds))):
        rnd = rounds[len(rounds) - 1 - k]
        base = entities(_msgs_text(rnd))
        if not base:
            continue
        packed, _ = M._pack_one_round(
            rnd, 0 if k == 0 else cfg["per_round"], cfg["budget"], k == 0,
            max_chars_per_tool=(cfg["per_tool_newest"] or cfg["per_tool"]) if k == 0 else cfg["per_tool"],
            max_chars_per_tool_call=cfg["per_call"],
            keep_last_tools=(cfg["keep_tools_newest"] or cfg["keep_tools"]) if k == 0 else cfg["keep_tools"])
        keep = entities(_msgs_text(packed))
        per, hit, tot = _rate(keep, base)
        detail.append({"age": _age_bucket(k), "k": k, "sid": sid,
                       "hit": hit, "tot": tot, "per": per,
                       "lost": {c: sorted(s - keep.get(c, set())) for c, s in base.items()
                                if s - keep.get(c, set())}})

    # 整轮跳过：窗口里到底保住了最近几轮（用 user 正文前缀在 packed 里找）
    packed_all = M._pack_history_records(
        raw, cfg["budget"], cfg["per_round"], min_rounds=cfg["min_rounds"],
        max_chars_per_tool=cfg["per_tool"], max_chars_per_tool_call=cfg["per_call"],
        keep_last_tools=cfg["keep_tools"],
        max_chars_per_tool_newest=cfg["per_tool_newest"],
        keep_last_tools_newest=cfg["keep_tools_newest"])
    have = [_WS.sub("", _msgs_text([m])) for m in packed_all]
    dropped, kept = [], 0
    for k in range(min(12, len(rounds))):
        rnd = rounds[len(rounds) - 1 - k]
        head = next((m["content"] for m in rnd if m["role"] == "user"), "") or ""
        probe = _WS.sub("", head)[:40]
        found = bool(probe) and any(probe in t for t in have)
        if found:
            kept += 1
        else:
            dropped.append({"k": k, "head": head[:60]})
    return detail, dropped, kept, packed_all


def main():
    ap = argparse.ArgumentParser(description="压缩质量评测（零 token 实体保留率）")
    ap.add_argument("--sessions", type=int, default=12)
    ap.add_argument("--min-msgs", type=int, default=60)
    ap.add_argument("--window", type=int, default=12, help="每会话评测最近多少轮")
    ap.add_argument("--examples", type=int, default=5)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    M._init_db()
    conn = M._get_db()
    srows = conn.execute(
        "SELECT session_id, COUNT(*) AS c, MAX(id) AS mx FROM messages "
        "GROUP BY session_id ORDER BY mx DESC LIMIT 200").fetchall()
    sids = [r["session_id"] for r in srows if r["c"] >= args.min_msgs][:args.sessions]
    if not sids:
        print("没有满足条件的会话（--min-msgs=%d）。" % args.min_msgs)
        return
    cfg = _cfg()
    print("参数：budget=%s per_round=%s min_rounds=%s per_tool=%s/%s keep_tools=%s/%s 视图上限=%s"
          % (cfg["budget"], cfg["per_round"], cfg["min_rounds"], cfg["per_tool"],
             cfg["per_tool_newest"], cfg["keep_tools"], cfg["keep_tools_newest"],
             A.HIST_VIEW_MAX_TOKENS))
    print("基线=同一轮原文；窗口=%d 轮\n" % args.window)

    all_detail, rows = [], []
    for sid in sids:
        raw = _raw(conn, sid)
        detail, dropped, kept, packed_all = eval_session(sid, raw, cfg, args.window)
        all_detail += detail
        rows.append({"sid": sid, "kept": kept, "dropped": dropped,
                     "tok": A._hist_view_tokens(packed_all)})

    print("%-16s %10s %10s %s" % ("会话", "窗口tok", "保住轮数", "整轮跳过（新→旧序号）"))
    for r in rows:
        print("%-16s %10d %8d/12  %s" % (r["sid"], r["tok"], r["kept"],
              ",".join("第%d轮" % (d["k"] + 1) for d in r["dropped"]) or "无"))

    print("\n【轮内截断】按轮龄分组（轮还在，内容被砍）")
    for age in ("最新轮", "最近2-6轮", "第7-12轮", "更旧"):
        sub = [d for d in all_detail if d["age"] == age]
        if not sub:
            continue
        hit = sum(d["hit"] for d in sub)
        tot = sum(d["tot"] for d in sub)
        print("\n  %s：%d/%d = %.1f%%（%d 轮）" % (age, hit, tot, 100.0 * hit / max(1, tot), len(sub)))
        for cls in [c for c, _ in PATTERNS]:
            h = sum(d["per"].get(cls, (0, 0))[0] for d in sub)
            t = sum(d["per"].get(cls, (0, 0))[1] for d in sub)
            if t:
                print("     %-6s %5d/%-5d %5.1f%%" % (cls, h, t, 100.0 * h / max(1, t)))

    print("\n【最惨的单轮】丢失实体示例：")
    for d in sorted(all_detail, key=lambda d: d["hit"] / max(1, d["tot"]))[:5]:
        ex = []
        for cls, items in d["lost"].items():
            for e in items[:args.examples]:
                ex.append("%s:%s" % (cls, e))
        print("  %s %s 保留 %d/%d = %.1f%%  丢：%s"
              % (d["sid"], d["age"], d["hit"], d["tot"],
                 100.0 * d["hit"] / max(1, d["tot"]), "; ".join(ex[:args.examples])))

    if args.json:
        out = os.path.join(BASE, "data", "compaction_recall.json")
        with open(out, "w", encoding="utf-8") as f:
            json.dump({"ts": __import__("time").time(), "window": args.window,
                       "rounds": all_detail, "sessions": rows}, f, ensure_ascii=False, indent=1)
        print("\n已写 %s" % out)


if __name__ == "__main__":
    main()

