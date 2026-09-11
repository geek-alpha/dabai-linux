"""离线重放：用真实消息库数据，复现滞回历史视图的重建率。

背景（两个结论互相矛盾，必须用真实数据定论）：
  - tools/hist_view_probe.py（合成数据）：9 轮完全追加、2 轮重建；
  - tools/cold_audit.py（真实运行）：断点几乎每轮都落在 history[0]，可复用只有 2.4k。

合成 ≠ 真实：真实消息的长度分布、工具轮条数、被打断轮、以及「同一轮由新变旧后
被 per_round/per_tool 截断」的比例都不一样。本探针直接读消息库，按轮切分后
逐轮重放「memory 打包 → 滞回视图」，并区分两条重建路径：
  (A) 锚点找不到（view[-1] 不在新 packed 里）→ 视图被迫重建；
  (B) 视图超上限被 _trim_hist_view 截回一半 → 头部变，前缀作废。
分辨这两条很重要：A 是缺陷（可以修），B 是设计（只能调参数）。
"""
import json
import os
import sys
import types

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import agent as A   # noqa: E402
import memory as M  # noqa: E402


def hcfg():
    with open(os.path.join(BASE, "settings.json"), "r", encoding="utf-8") as f:
        m = (json.load(f) or {}).get("memory") or {}
    return {
        "budget": m.get("short_term_max_tokens", M.SHORT_TERM_MAX_TOKENS),
        "per_round": m.get("short_term_max_chars_per_round", M.SHORT_TERM_MAX_CHARS_PER_ROUND),
        "min_rounds": m.get("short_term_min_rounds", M.SHORT_TERM_MIN_ROUNDS),
        "per_tool": m.get("short_term_max_chars_per_tool", M.SHORT_TERM_MAX_CHARS_PER_TOOL),
        "per_call": m.get("short_term_max_chars_per_tool_call", M.SHORT_TERM_MAX_CHARS_PER_TOOL_CALL),
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


def common_prefix(a, b):
    n = 0
    for x, y in zip(a, b):
        if A._hist_msg_key(x) == A._hist_msg_key(y):
            n += 1
        else:
            break
    return n


def main():
    M._init_db()
    conn = M._get_db()
    srows = conn.execute(
        "SELECT session_id, COUNT(*) AS c, MAX(id) AS mx FROM messages "
        "GROUP BY session_id ORDER BY mx DESC LIMIT 5").fetchall()
    print("最近会话（按最新消息排序）：")
    for r in srows:
        print("  %-40s %5d 条" % (r["session_id"], r["c"]))
    if not srows:
        print("消息库为空，无法重放。")
        return

    sid = srows[0]["session_id"]
    rows = conn.execute(
        "SELECT id, role, content, tool_calls, tool_results FROM messages "
        "WHERE session_id=? AND (source != 'auto' OR role = 'assistant') "
        "ORDER BY id", (sid,)).fetchall()
    print("\n重放会话：%s（%d 条消息）" % (sid, len(rows)))

    rounds, cur = [], None
    for m in rows:
        if m["role"] == "user":
            cur = [m]
            rounds.append(cur)
        elif cur is not None:
            cur.append(m)
        else:
            cur = [m]
            rounds.append(cur)
    print("切出 %d 轮" % len(rounds))
    if len(rounds) < 3:
        print("轮数太少，无法观察重建。")
        return

    cfg = hcfg()
    print("参数：budget=%s per_round=%s min_rounds=%s per_tool=%s keep_tools=%s"
          % (cfg["budget"], cfg["per_round"], cfg["min_rounds"],
             cfg["per_tool"], cfg["keep_tools"]))

    start = max(1, len(rounds) - 14)
    fake = types.SimpleNamespace(memory=types.SimpleNamespace(session_id=sid))
    prev_view = prev_packed = None
    stat = {"app": 0, "anchor": 0, "trim": 0}
    print("\n轮  packed  view   锚点在packed  前缀(view)  前缀(packed)  判定")
    for k in range(start, len(rounds) + 1):
        flat = [m for rnd in rounds[:k] for m in rnd][-200:]
        raw = [{"role": m["role"], "content": m["content"] or "",
                "tool_calls": _pj(m["tool_calls"]),
                "tool_results": _pj(m["tool_results"])} for m in flat]
        packed = M._pack_history_records(
            raw, cfg["budget"], cfg["per_round"],
            min_rounds=cfg["min_rounds"], max_chars_per_tool=cfg["per_tool"],
            max_chars_per_tool_call=cfg["per_call"], keep_last_tools=cfg["keep_tools"])
        anchor_ok = "-"
        if prev_view:
            key = A._hist_msg_key(prev_view[-1])
            anchor_ok = "是" if any(A._hist_msg_key(m) == key for m in packed) else "否"
        view = A.AIAgent._stable_history_messages(fake, packed)
        if prev_view is None:
            print("%-3d %-6d %-6d %-12s %-10s %-13s 首次"
                  % (k, len(packed), len(view), anchor_ok, "-", "-"))
        else:
            cv = common_prefix(prev_view, view)
            cp = common_prefix(prev_packed, packed)
            if len(view) >= len(prev_view) and cv == len(prev_view):
                stat["app"] += 1
                verdict = "追加"
            elif cv == 0 and anchor_ok == "否":
                stat["anchor"] += 1
                verdict = "★重建(锚点丢)"
            elif cv == 0:
                stat["trim"] += 1
                verdict = "★重建(trim)"
            else:
                verdict = "部分"
            print("%-3d %-6d %-6d %-12s %-10d %-13d %s"
                  % (k, len(packed), len(view), anchor_ok, cv, cp, verdict))
        prev_view, prev_packed = view, packed

    print("\n小计：完全追加 %d，锚点丢失重建 %d，trim 重建 %d"
          % (stat["app"], stat["anchor"], stat["trim"]))
    print("HIST_VIEW_MAX_TOKENS=%s（视图 token 超它才 trim）" % A.HIST_VIEW_MAX_TOKENS)
    print("末轮视图 token 估算=%d" % A._hist_view_tokens(prev_view))


if __name__ == "__main__":
    main()
