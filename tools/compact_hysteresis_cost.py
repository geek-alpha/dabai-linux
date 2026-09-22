#!/usr/bin/env python3
"""离线实测：轮内压缩「压到刚好回预算」vs「一次压到 budget×f」的净账。

问题：_compact_tool_history 超预算时把旧轮结果压到刚好 ≤ budget。下一轮新增一条
结果又超、又压一次——每压一次，被改消息之后的整段前缀就作废一次（从命中价 1/50
升回全价重发）。compact_trace.jsonl 实测：一个会话 30 分钟内触发 29 次压缩。

滞回（hysteresis）就是「一次多压一点，换后续十几轮不再触发」。本工具重放真实
会话，对同一批轮次跑多个比例，输出压缩次数 / 前缀作废量 / 缓存计价成本指数。

用法：
    venv/bin/python tools/compact_hysteresis_cost.py --sessions 6 --rounds 30
    venv/bin/python tools/compact_hysteresis_cost.py --ratios 1.0,0.7,0.5
"""
import argparse
import json
import os
import sys
from pathlib import Path

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import agent as A        # noqa: E402
import memory as M       # noqa: E402
import turn_metrics as TM  # noqa: E402

HIT = TM.HIT_PRICE_RATIO


def _pj(v):
    if isinstance(v, str):
        try:
            return json.loads(v)
        except Exception:
            return None
    return v


def _raw(conn, sid) -> list:
    rows = conn.execute(
        "SELECT role, content, tool_calls, tool_results FROM messages "
        "WHERE session_id=? AND (source != 'auto' OR role = 'assistant') "
        "ORDER BY id", (sid,)).fetchall()
    return [{"role": m["role"], "content": m["content"] or "",
             "tool_calls": _pj(m["tool_calls"]), "tool_results": _pj(m["tool_results"])}
            for m in rows]


def replay_compact(rounds: list, budget: int, hysteresis: float,
                   keep_rounds: int = 1) -> dict:
    """逐轮重放：每轮追加后调一次真实的 _compact_tool_history，按前缀缓存计价。

    计价口径与 spill_pointer_cost.py 一致：本轮与上一轮逐条内容相同的部分按命中价，
    从第一个不同的位置起全部按全价——那正是「改了历史中间 → 其后整段前缀作废」。
    """
    msgs, prev = [], []
    st = {"compact": 0, "inv_chars": 0, "saved_chars": 0, "index": 0.0,
          "tokens": [], "cuts": []}
    for rnd in rounds:
        msgs.extend([dict(m) for m in rnd])
        before = [str(m.get("content") or "") for m in msgs]
        A._compact_tool_history(msgs, budget=budget, keep_rounds=keep_rounds,
                                hysteresis=hysteresis)
        cur = [str(m.get("content") or "") for m in msgs]
        changed = [i for i in range(len(msgs)) if before[i] != cur[i]]
        if changed:
            st["compact"] += 1
            min_k = min(changed)
            # 作废范围 = 最早被改那条到末尾（其后每条都从命中价升全价）
            st["inv_chars"] += sum(len(c) for c in cur[min_k:])
            st["saved_chars"] += sum(len(before[i]) - len(cur[i]) for i in changed)
            st["cuts"].append((len(changed), min_k, len(msgs)))
        t = sum(M.estimate_tokens(c) for c in cur)
        st["tokens"].append(t)
        hit = 0
        for i in range(min(len(prev), len(cur))):
            if prev[i] == cur[i]:
                hit += M.estimate_tokens(cur[i])
            else:
                break
        st["index"] += hit * HIT + (t - hit)
        prev = cur
    return st


def _avg(xs) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def main() -> int:
    ap = argparse.ArgumentParser(description="轮内压缩滞回比例的净账实测")
    ap.add_argument("--sessions", type=int, default=6)
    ap.add_argument("--rounds", type=int, default=30)
    ap.add_argument("--min-msgs", type=int, default=60)
    ap.add_argument("--ratios", default="1.0,0.8,0.65,0.5",
                    help="滞回比例：压到 budget×f。1.0=现状")
    ap.add_argument("--budget", type=int, default=0, help="0=取 settings 实际值")
    args = ap.parse_args()
    ratios = [float(x) for x in args.ratios.split(",") if x.strip()]

    budget = args.budget or A._mid_turn_max_tokens()
    conn = M._get_db()
    sids = [r[0] for r in conn.execute(
        "SELECT session_id, COUNT(*) c FROM messages GROUP BY session_id "
        "HAVING c >= ? ORDER BY MAX(id) DESC LIMIT ?", (args.min_msgs, args.sessions))]
    if not sids:
        print("没有足够长的会话可重放")
        return 1

    print(f"预算 budget={budget} token（settings 实际值）｜"
          f"轮内单条上限={A._single_result_max_tokens()} token｜"
          f"旧轮截断={A._tool_result_retro_cap()} 字符｜保留最新 {A.KEEP_NEWEST_TOOL_ROUNDS} 轮")
    tot = {r: {"n": 0, "inv": 0, "saved": 0, "ix": 0.0, "rounds": 0, "peak": 0}
           for r in ratios}
    for sid in sids:
        recs = _raw(conn, sid)
        rounds = M._group_history_rounds(recs)[-args.rounds:]
        if len(rounds) < 5:
            continue
        print(f"\n会话 {sid[:14]}…  轮 {len(rounds)}")
        for r in ratios:
            st = replay_compact(rounds, budget, r)
            d = tot[r]
            d["n"] += st["compact"]
            d["inv"] += st["inv_chars"]
            d["saved"] += st["saved_chars"]
            d["ix"] += st["index"]
            d["rounds"] += len(st["tokens"])
            d["peak"] = max(d["peak"], max(st["tokens"]) if st["tokens"] else 0)
            print(f"  f={r:<5} 压缩 {st['compact']:>3} 次 | 作废 {st['inv_chars']:>8} 字符 | "
                  f"压掉 {st['saved_chars']:>8} 字符 | 成本指数 {st['index']:>10.0f} | "
                  f"峰值 {max(st['tokens']) if st['tokens'] else 0:>7} tok")

    base = tot[ratios[0]]
    print(f"\n===== 合计（{base['rounds']} 轮 / {len(sids)} 会话）=====")
    print(f"{'滞回':>6} {'压缩次数':>8} {'前缀作废字符':>12} {'压掉字符':>10} "
          f"{'成本指数':>11} {'相对现状':>9} {'单次回本轮数':>12}")
    for r in ratios:
        d = tot[r]
        one = d["inv"] / 4 * (1 - HIT)              # 一次性代价（全价当量）
        per = (d["saved"] / 4) * HIT                # 每轮省下的重发体积
        pay = one / per if per > 0 else float("inf")
        pct = (d["ix"] - base["ix"]) / base["ix"] * 100 if base["ix"] else 0
        print(f"{r:>6} {d['n']:>8} {d['inv']:>12} {d['saved']:>10} "
              f"{d['ix']:>11.0f} {pct:>+8.1f}% {pay:>12.1f}")
    print("\n成本指数 = 命中 token×1/50 + 未命中 token×1（前缀缓存计价）；"
          "单次回本轮数 = 一次性作废代价 ÷ 每轮省下的重发成本")
    return 0


if __name__ == "__main__":
    sys.exit(main())
