#!/usr/bin/env python3
"""离线实测：短期窗口里「工具结果留头留尾」vs「只留一行指针」各花多少 token。

问题：SHORT_TERM_MAX_CHARS_PER_TOOL=500 的截断，是在「正文还在上下文里」的前提下做的
省钱动作。真正的大头是反过来——正文全在盘上（data/tool_spill/），上下文只留一行指针，
需要时再 read_lines 读回来。省的是每轮 token，代价是每次访问多一次 read 往返。

这笔账不能靠估：视图是「只追加」的滞回结构，少带体积会让窗口更慢顶到
HIST_VIEW_MAX_TOKENS → 重建更少 → 前缀作废更少。两个方向都省钱，必须一起算。
所以本工具重放真实会话，对同一批轮次跑 A（现状）/ B（指针）两套，输出：
  每轮平均 token、视图重建次数、缓存计价成本指数、每轮落盘指针条数。

用法：
    venv/bin/python tools/spill_pointer_cost.py --sessions 6 --rounds 24
    venv/bin/python tools/spill_pointer_cost.py --min-chars 500   # 只对超过 N 字的结果改指针
"""
import argparse
import json
import os
import re
import sys
import time
import types
from pathlib import Path

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import agent as A        # noqa: E402
import memory as M       # noqa: E402
import turn_metrics as TM  # noqa: E402

HIT_PRICE_RATIO = TM.HIT_PRICE_RATIO


def _cfg() -> dict:
    with open(os.path.join(BASE, "settings.json"), "r", encoding="utf-8") as f:
        m = (json.load(f) or {}).get("memory") or {}
    return {
        "budget": m.get("short_term_max_tokens", M.SHORT_TERM_MAX_TOKENS),
        "per_round": m.get("short_term_max_chars_per_round", M.SHORT_TERM_MAX_CHARS_PER_ROUND),
        "min_rounds": m.get("short_term_min_rounds", M.SHORT_TERM_MIN_ROUNDS),
        "per_call": m.get("short_term_max_chars_per_tool_call", M.SHORT_TERM_MAX_CHARS_PER_TOOL_CALL),
        "per_tool": m.get("short_term_max_chars_per_tool", M.SHORT_TERM_MAX_CHARS_PER_TOOL),
        "keep_tools": m.get("short_term_keep_last_tools", M.SHORT_TERM_KEEP_LAST_TOOLS),
        "keep_newest": m.get("short_term_keep_last_tools_newest",
                             M.SHORT_TERM_KEEP_LAST_TOOLS_NEWEST),
        "per_tool_newest": m.get("short_term_max_chars_per_tool_newest",
                                 M.SHORT_TERM_MAX_CHARS_PER_TOOL_NEWEST),
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


def _raw(conn, sid) -> list:
    rows = conn.execute(
        "SELECT role, content, tool_calls, tool_results FROM messages "
        "WHERE session_id=? AND (source != 'auto' OR role = 'assistant') "
        "ORDER BY id", (sid,)).fetchall()
    return [{"role": m["role"], "content": m["content"] or "",
             "tool_calls": _pj(m["tool_calls"]), "tool_results": _pj(m["tool_results"])}
            for m in rows]


def _pointerize(records: list, min_chars: int) -> tuple:
    """把超长工具结果正文换成一行指针（正文照旧落盘），返回 (记录, 指针条数)。

    指针文本必须 < per_tool 上限，否则打包时自己又被截断。
    """
    out, n = [], 0
    for m in records:
        if m.get("role") == "tool":
            text = m.get("content") or ""
            if len(text) > min_chars:
                path = M._spill_tool_text(text)
                if path:
                    name = Path(path).name
                    m = {**m, "content": (
                        f"【工具结果原文已落盘 data/tool_spill/{name}（{len(text)}字）"
                        f"· 用 read_lines 读它，别重跑命令】")}
                    n += 1
        out.append(m)
    return out, n


def replay(rounds: list, cfg: dict, min_chars: int = 0,
           hist_only: bool = False) -> dict:
    """逐轮重放。min_chars=0 → A（现状）；>0 → B（超长结果只留指针）。

    hist_only=True → 只把「历史轮」指针化，最新一轮保留正文。理由：最新一轮是下一轮
    唯一必读的上下文，把它换成指针等于把「免费看见结论」改成「花一次 read」。
    """
    fake = types.SimpleNamespace(
        memory=types.SimpleNamespace(session_id="spill-cost-%s" % min_chars))
    prev, st = None, {"tokens": [], "append": 0, "rebuild": 0, "trim": 0,
                      "index": 0.0, "pointers": [], "rounds_kept": []}
    seen_ptr = set()
    for k in range(1, len(rounds) + 1):
        old = [m for rnd in rounds[:k - 1] for m in rnd]
        new = rounds[k - 1]
        if min_chars:
            old, _ = _pointerize(old, min_chars)
            if not hist_only:
                new, _ = _pointerize(new, min_chars)
        flat = (old + new)[-200:]
        if min_chars:
            # 指针条数按「本轮新增」记：flat 每轮都含全部历史，直接取 len 会算成累计值
            now = {m["content"] for m in flat
                   if str(m.get("content", "")).startswith("【工具结果原文已落盘")}
            st["pointers"].append(len(now - seen_ptr))
            seen_ptr |= now
        packed = M._pack_history_records(
            flat, cfg["budget"], cfg["per_round"], min_rounds=cfg["min_rounds"],
            max_chars_per_tool=cfg["per_tool"], max_chars_per_tool_call=cfg["per_call"],
            keep_last_tools=cfg["keep_tools"],
            max_chars_per_tool_newest=cfg["per_tool_newest"],
            keep_last_tools_newest=cfg["keep_newest"])
        before = A._hist_view_tokens(getattr(fake, "_hist_msgs_view", None) or [])
        view = A.AIAgent._stable_history_messages(fake, packed)
        t = A._hist_view_tokens(view)
        st["tokens"].append(t)
        st["rounds_kept"].append(sum(1 for m in view if m.get("role") == "user"))
        if before > A.HIST_VIEW_MAX_TOKENS:
            st["trim"] += 1
        if prev is not None:
            same = 0
            for x, y in zip(prev, view):
                if A._hist_msg_key(x) == A._hist_msg_key(y):
                    same += 1
                else:
                    break
            if same == len(prev) and len(view) >= len(prev):
                st["append"] += 1
            else:
                st["rebuild"] += 1
            hit = A._hist_view_tokens(view[:same])
            st["index"] += hit * HIT_PRICE_RATIO + (t - hit)
        else:
            st["index"] += t
        prev = view
    return st


def _avg(xs) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def readback_audit() -> int:
    """回读率审计：落盘指针被真的读回了几次。

    分子（可实测）：tool_calls 里「读型工具 + 指向 tool_spill/*.txt 的路径」的条数——
    要读回落盘原文只能走 read_lines/code_read，路径必然出现在那个调用的参数里。
    不能用 LIKE '%tool_spill%' 粗筛：写型工具（code_edit/code_append）的参数里
    恰好含这个字符串时会被算成回读（实测把 0 次读回虚报成 47 次）。
    分母（近似）：索引里每天新增的落盘条数（= 指针条数上界；摘要索引与留头尾截断
    共用同一份落盘，不全是 pointer_only 产生的）。比值为 0 = 指针化的代价没真发生。
    """
    read_tools = {"read_lines", "code_read", "read_file", "shell_run", "cat"}
    pat = re.compile(r"tool_spill/[0-9a-f]{10}\.txt")
    conn = M._get_db()
    rows = conn.execute(
        "SELECT id, created_at, tool_calls FROM messages WHERE role='assistant' "
        "AND tool_calls LIKE '%tool_spill%' ORDER BY id").fetchall()
    per_day, samples = {}, []
    for mid, ts, tcs in rows:
        try:
            calls = json.loads(tcs) or []
        except Exception:
            continue
        for t in calls:
            fn = t.get("function") or {}
            if fn.get("name") not in read_tools:
                continue
            args = str(fn.get("arguments") or "")
            if not pat.search(args):
                continue
            d = time.strftime("%Y-%m-%d", time.localtime(float(ts or 0)))
            per_day[d] = per_day.get(d, 0) + 1
            if len(samples) < 3:
                samples.append(f"    id={mid} {fn.get('name')} {args[:90]}")
    print(f"读回事件（历史累计）  {sum(per_day.values())} 次")
    for d in sorted(per_day):
        print(f"    {d}  读回 {per_day[d]} 次")
    for s in samples:
        print(s)
    idx = M._spill_index_path()
    spill_day = {}
    if idx.exists():
        for line in idx.read_text(encoding="utf-8", errors="ignore").splitlines():
            try:
                e = json.loads(line)
            except Exception:
                continue
            d = time.strftime("%Y-%m-%d", time.localtime(e.get("ts", 0)))
            spill_day[d] = spill_day.get(d, 0) + 1
    files = [p for p in M._TOOL_SPILL_DIR.glob("*.txt")] if M._TOOL_SPILL_DIR.exists() else []
    print(f"落盘正文文件（现存）  {len(files)} 个；索引 {sum(spill_day.values())} 条")
    for d in sorted(spill_day)[-7:]:
        print(f"    {d}  新增落盘 {spill_day[d]} 条")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="工具结果：截断 vs 指针 的上下文成本实测")
    ap.add_argument("--sessions", type=int, default=6)
    ap.add_argument("--rounds", type=int, default=24, help="每会话重放最近多少轮")
    ap.add_argument("--min-msgs", type=int, default=60)
    ap.add_argument("--min-chars", type=int, default=M.SHORT_TERM_MAX_CHARS_PER_TOOL,
                    help="超过多少字的工具结果改指针（默认与 per_tool 同量级）")
    ap.add_argument("--hist-only", action="store_true",
                    help="只指针化历史轮，最新一轮保留正文（推荐折中）")
    ap.add_argument("--readback", action="store_true",
                    help="不回放，只审计「指针被读回了几次」（落盘索引 + 库里 tool_calls 实测）")
    ap.add_argument("--unify", action="store_true",
                    help="第三套 C：不指针化，但把最新轮的 per_tool 降到与历史轮一致"
                         "（验证「重建变少」到底是省体积的功劳，还是统一打包规则的功劳）")
    args = ap.parse_args()
    if args.readback:
        return readback_audit()

    cfg = _cfg()
    conn = M._get_db()
    sids = [r[0] for r in conn.execute(
        "SELECT session_id, COUNT(*) c FROM messages GROUP BY session_id "
        "HAVING c >= ? ORDER BY MAX(id) DESC LIMIT ?", (args.min_msgs, args.sessions))]
    if not sids:
        print("没有足够长的会话可重放")
        return 1

    print(f"参数：budget={cfg['budget']} per_round={cfg['per_round']} "
          f"per_tool={cfg['per_tool']} keep_tools={cfg['keep_tools']}/"
          f"{cfg['keep_newest']} 指针阈值={args.min_chars}字 "
          f"模式={'仅历史轮' if args.hist_only else '含最新轮'}")
    tot = {"a_tok": [], "b_tok": [], "a_rb": 0, "b_rb": 0,
           "a_ix": 0.0, "b_ix": 0.0, "ptr": 0, "rounds": 0,
           "a_kept": [], "b_kept": [], "c_tok": [], "c_rb": 0,
           "c_ix": 0.0, "c_kept": []}
    for sid in sids:
        recs = _raw(conn, sid)
        rounds = M._group_history_rounds(recs)[-args.rounds:]
        if len(rounds) < 3:
            continue
        a = replay(rounds, cfg, 0)
        b = replay(rounds, cfg, args.min_chars, hist_only=args.hist_only)
        c = (replay(rounds, {**cfg, "per_tool_newest": cfg["per_tool"]}, 0)
             if args.unify else None)
        tot["a_tok"] += a["tokens"]
        tot["b_tok"] += b["tokens"]
        tot["a_rb"] += a["rebuild"]
        tot["b_rb"] += b["rebuild"]
        tot["a_ix"] += a["index"]
        tot["b_ix"] += b["index"]
        tot["ptr"] += sum(b["pointers"])
        tot["rounds"] += len(a["tokens"])
        tot["a_kept"] += a["rounds_kept"]
        tot["b_kept"] += b["rounds_kept"]
        if c:
            tot["c_tok"] += c["tokens"]
            tot["c_rb"] += c["rebuild"]
            tot["c_ix"] += c["index"]
            tot["c_kept"] += c["rounds_kept"]
        print(f"\n会话 {sid[:14]}…  轮 {len(rounds)}")
        print(f"  A 现状  每轮 {_avg(a['tokens']):7.0f} tok | 重建 {a['rebuild']:3d} | 成本指数 {a['index']:9.0f}"
              f" | 装入 {_avg(a['rounds_kept']):.1f} 轮")
        print(f"  B 指针  每轮 {_avg(b['tokens']):7.0f} tok | 重建 {b['rebuild']:3d} | 成本指数 {b['index']:9.0f}"
              f" | 装入 {_avg(b['rounds_kept']):.1f} 轮 | 指针 {sum(b['pointers'])} 条")
        if c:
            print(f"  C 统一  每轮 {_avg(c['tokens']):7.0f} tok | 重建 {c['rebuild']:3d} | 成本指数 {c['index']:9.0f}"
                  f" | 装入 {_avg(c['rounds_kept']):.1f} 轮")

    if not tot["rounds"]:
        print("重放轮数为 0")
        return 1
    a_avg, b_avg = _avg(tot["a_tok"]), _avg(tot["b_tok"])
    print("\n===== 合计（%d 轮 / %d 会话）=====" % (tot["rounds"], len(sids)))
    print(f"每轮 token      A {a_avg:8.0f}  →  B {b_avg:8.0f}   "
          f"省 {a_avg - b_avg:7.0f}（{(a_avg - b_avg) / a_avg * 100 if a_avg else 0:.1f}%）")
    print(f"视图重建次数    A {tot['a_rb']:8d}  →  B {tot['b_rb']:8d}")
    print(f"装入历史轮数    A {_avg(tot['a_kept']):8.1f}  →  B {_avg(tot['b_kept']):8.1f}")
    print(f"缓存成本指数    A {tot['a_ix']:8.0f}  →  B {tot['b_ix']:8.0f}   "
          f"{(tot['a_ix'] - tot['b_ix']) / tot['a_ix'] * 100 if tot['a_ix'] else 0:+.1f}%")
    if tot["c_tok"]:
        c_avg = _avg(tot["c_tok"])
        print(f"C 统一 per_tool  每轮 {c_avg:8.0f} tok | 重建 {tot['c_rb']:5d} | "
              f"装入 {_avg(tot['c_kept']):.1f} 轮 | 成本指数 {tot['c_ix']:9.0f}   "
              f"{(tot['a_ix'] - tot['c_ix']) / tot['a_ix'] * 100 if tot['a_ix'] else 0:+.1f}%")
    print(f"落盘指针条数    B 共 {tot['ptr']} 条（每轮新增 {tot['ptr'] / tot['rounds']:.1f} 条）"
          f" → 全被读回 = 每轮多 {tot['ptr'] / tot['rounds']:.1f} 次 read 往返（上限）")
    n_hits = conn.execute(
        "SELECT COUNT(*) FROM messages WHERE role='assistant' "
        "AND content LIKE '%tool_spill%' AND content LIKE '%read_lines%'").fetchone()[0]
    print(f"实测读回次数    历史里引用 data/tool_spill 路径的 assistant 消息：{n_hits} 条")
    return 0


if __name__ == "__main__":
    sys.exit(main())
