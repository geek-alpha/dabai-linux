"""离线成本模型（零 token）：逐轮重放，比较两套打包参数「每轮发送多少 token + 重建几次」。

为什么需要它：视图是「只追加」的滞回结构，超 HIST_VIEW_MAX_TOKENS 才裁回一半。
放宽最新一轮的上限会让窗口更快顶到天花板 → 裁剪更频繁 → 前缀作废 → 缓存全失效。
只看「实体保留率」会漏掉这笔账，必须把「每轮实际发送量」和「重建次数」一起算。

用法：
    venv/bin/python tools/hist_view_cost.py --sessions 6 --rounds 24
    venv/bin/python tools/hist_view_cost.py --compare 3/500 16/1500 24/1500
"""
import argparse
import json
import os
import sys
import types

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import agent as A   # noqa: E402
import memory as M  # noqa: E402
import turn_metrics as TM  # noqa: E402

# 命中价系数必须与真实计费同源：这里曾写死 0.1（旧假设 1/10），比实测的 1/50 贵 5 倍。
# 系数一变，「多带体积（命中价）换少重建（全价）」的账就反过来，故直接引用单一来源。
HIT_PRICE_RATIO = TM.HIT_PRICE_RATIO


def _cfg():
    with open(os.path.join(BASE, "settings.json"), "r", encoding="utf-8") as f:
        m = (json.load(f) or {}).get("memory") or {}
    return {
        "budget": m.get("short_term_max_tokens", M.SHORT_TERM_MAX_TOKENS),
        "per_round": m.get("short_term_max_chars_per_round", M.SHORT_TERM_MAX_CHARS_PER_ROUND),
        "min_rounds": m.get("short_term_min_rounds", M.SHORT_TERM_MIN_ROUNDS),
        "per_call": m.get("short_term_max_chars_per_tool_call", M.SHORT_TERM_MAX_CHARS_PER_TOOL_CALL),
        "per_tool": m.get("short_term_max_chars_per_tool", M.SHORT_TERM_MAX_CHARS_PER_TOOL),
        "keep_tools": m.get("short_term_keep_last_tools", M.SHORT_TERM_KEEP_LAST_TOOLS),
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
        "SELECT role, content, tool_calls, tool_results FROM messages "
        "WHERE session_id=? AND (source != 'auto' OR role = 'assistant') "
        "ORDER BY id", (sid,)).fetchall()
    return [{"role": m["role"], "content": m["content"] or "",
             "tool_calls": _pj(m["tool_calls"]), "tool_results": _pj(m["tool_results"])}
            for m in rows]


def replay(rounds, cfg, newest_keep, newest_per):
    """逐轮重放，返回每轮 token / 追加重建数 / 缓存计价成本指数。

    成本指数 = Σ(命中前缀 × HIT_PRICE_RATIO + 其余 × 1.0)：命中部分按缓存读价（1/50），
    其余全价（本轮新增 + 重建后作废的旧消息）。只看「每轮均 token」会把
    「重发一遍全价前缀」算成一次普通增量，这就是前一轮估出「每轮只多 3.5k」
    却实测重建翻 6 倍的原因。
    """
    fake = types.SimpleNamespace(memory=types.SimpleNamespace(session_id="cost-%s-%s" % (newest_keep, newest_per)))
    prev = None
    stats = {"tokens": [], "append": 0, "rebuild": 0, "trim": 0, "anchor_lost": 0, "index": 0.0}
    for k in range(1, len(rounds) + 1):
        flat = [m for rnd in rounds[:k] for m in rnd][-200:]
        packed = M._pack_history_records(
            flat, cfg["budget"], cfg["per_round"], min_rounds=cfg["min_rounds"],
            max_chars_per_tool=cfg["per_tool"], max_chars_per_tool_call=cfg["per_call"],
            keep_last_tools=cfg["keep_tools"],
            max_chars_per_tool_newest=newest_per, keep_last_tools_newest=newest_keep)
        before = A._hist_view_tokens(getattr(fake, "_hist_msgs_view", None) or [])
        view = A.AIAgent._stable_history_messages(fake, packed)
        t = A._hist_view_tokens(view)
        stats["tokens"].append(t)
        if before > A.HIST_VIEW_MAX_TOKENS:
            stats["trim"] += 1
        if prev is not None:
            n = 0
            for x, y in zip(prev, view):
                if A._hist_msg_key(x) == A._hist_msg_key(y):
                    n += 1
                else:
                    break
            if n == len(prev) and len(view) >= len(prev):
                stats["append"] += 1
            else:
                stats["rebuild"] += 1
                key = A._hist_msg_key(prev[-1]) if prev else None
                if key is None or not any(A._hist_msg_key(m) == key for m in packed):
                    stats["anchor_lost"] += 1
            hit_tok = A._hist_view_tokens(view[:n])
            stats["index"] += hit_tok * HIT_PRICE_RATIO + (t - hit_tok)
        else:
            stats["index"] += t
        prev = view
    return stats


def main():
    ap = argparse.ArgumentParser(description="滞回视图成本模型（零 token）")
    ap.add_argument("--sessions", type=int, default=6)
    ap.add_argument("--rounds", type=int, default=24, help="每会话重放最近多少轮")
    ap.add_argument("--min-msgs", type=int, default=60)
    ap.add_argument("--compare", default="3/500,16/1500,24/1500,16/800",
                    help="逗号分隔的 keep/per 组合，逐个跑")
    ap.add_argument("--view-max", type=int, default=0,
                    help="覆盖 HIST_VIEW_MAX_TOKENS（测视图上限对重建频率的影响）")
    args = ap.parse_args()

    if args.view_max:
        A.HIST_VIEW_MAX_TOKENS = args.view_max

    A.HIST_VIEW_STATE_PATH = os.path.join(BASE, "data", ".hist_view_cost_state.json")
    if os.path.exists(A.HIST_VIEW_STATE_PATH):
        os.remove(A.HIST_VIEW_STATE_PATH)

    M._init_db()
    conn = M._get_db()
    rows = conn.execute(
        "SELECT session_id, COUNT(*) c, MAX(id) mx FROM messages "
        "GROUP BY session_id ORDER BY mx DESC LIMIT 200").fetchall()
    sids = [r["session_id"] for r in rows if r["c"] >= args.min_msgs][:args.sessions]
    cfg = _cfg()
    combos = [tuple(int(x) for x in c.split("/")) for c in args.compare.split(",") if c.strip()]
    print("视图上限=%d（超它裁回一半=%d）  budget=%s min_rounds=%s 重放最近 %d 轮\n"
          % (A.HIST_VIEW_MAX_TOKENS, A.HIST_VIEW_MAX_TOKENS // 2, cfg["budget"],
             cfg["min_rounds"], args.rounds))

    all_rounds = {}
    for sid in sids:
        all_rounds[sid] = M._group_history_rounds(_raw(conn, sid))

    print("%-12s %8s %10s %10s %8s %8s %8s %12s" %
          ("keep/per", "轮数", "每轮均tok", "末轮tok", "追加", "重建", "锚点丢", "成本指数"))
    base = None
    for keep, per in combos:
        tt = 0
        n = 0
        app = reb = 0
        idx = 0.0
        lost = 0
        last = []
        for sid, rounds in all_rounds.items():
            sub = rounds[-args.rounds:]
            if len(sub) < 3:
                continue
            st = replay(sub, cfg, keep, per)
            tt += sum(st["tokens"])
            n += len(st["tokens"])
            app += st["append"]
            reb += st["rebuild"]
            lost += st["anchor_lost"]
            idx += st["index"]
            last.append(st["tokens"][-1])
        if base is None:
            base = idx or 1.0
        print("%-12s %8d %10.0f %10.0f %8d %8d %8d %8.0f (%+.0f%%)" %
              ("%d/%d" % (keep, per), n, tt / max(1, n),
               sum(last) / max(1, len(last)), app, reb, lost, idx,
               100.0 * (idx - base) / base))
    print("\n「追加」= 上一轮视图是这一轮的前缀（命中缓存）；「重建」= 前缀作废（全价重发）。")
    print("成本指数 = Σ(命中前缀×%.2f + 其余×1.0)，第一行为基准。" % HIT_PRICE_RATIO)


if __name__ == "__main__":
    main()
