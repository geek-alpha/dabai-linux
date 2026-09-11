"""一次性诊断：逐轮冷启动的构成（sys / memory / history / tail 各占多少）。

目的：回答「冷启动 60k 到底是哪一块没命中」。聚合指标只会说总量，
而修哪一块取决于断点落在哪一段、那一段多大。
"""
import json
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)


def _fmt(n):
    n = int(n or 0)
    if n >= 1000000:
        return "%.2fM" % (n / 1000000.0)
    if n >= 1000:
        return "%.1fk" % (n / 1000.0)
    return str(n)


def main():
    path = os.path.join(BASE, "data", "turn_metrics.jsonl")
    rows = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except Exception:
                    pass
    print("轮  calls  P0      cold    断点  标签                        可复用  本轮总量  作废")
    for i, r in enumerate(rows):
        p = r.get("prefix") or {}
        tr = r.get("call_trace") or []
        p0 = tr[0].get("p") if tr else 0
        if not p:
            print("%-3d %-6s %-7s %-7s  （无探针数据）"
                  % (i, r.get("llm_calls"), _fmt(p0), _fmt(r.get("cold_miss"))))
            continue
        if p.get("cold"):
            print("%-3d %-6s %-7s %-7s  冷启动首轮（无上轮状态）"
                  % (i, r.get("llm_calls"), _fmt(p0), _fmt(r.get("cold_miss"))))
            continue
        print("%-3d %-6s %-7s %-7s 第%-4s %-28s %-7s %-8s %-7s"
              % (i, r.get("llm_calls"), _fmt(p0), _fmt(r.get("cold_miss")),
                 p.get("break_at"), str(p.get("break_label"))[:28],
                 _fmt(p.get("keep_chars")), _fmt(p.get("cur_chars")),
                 _fmt(p.get("wasted_chars"))))
        if p.get("tools_diff", {}).get("any"):
            td = p["tools_diff"]
            print("      工具集变化：+%d -%d 改%d 序%s"
                  % (len(td.get("added") or []), len(td.get("removed") or []),
                     len(td.get("changed") or []), td.get("order_changed")))
        m = p.get("hist_marks") or {}
        if m:
            print("      分段下标：sys=%s summary=%s memory=%s history=%s tail=%s"
                  % (m.get("sys"), m.get("summary"), m.get("memory"),
                     m.get("history"), m.get("tail")))
        sg = r.get("seg") or {}
        if sg:
            print("      各段字符：sys=%s summary=%s memory=%s hist=%s dyn=%s "
                  "recall=%s work=%s 合计=%s"
                  % (sg.get("sys"), sg.get("summary"), sg.get("memory"),
                     sg.get("hist"), sg.get("dyn"), sg.get("recall"),
                     sg.get("work"), sg.get("total")))
    # 首条（sys）体积与指纹变化：它是唯一必须跨轮恒定的消息
    fps = [str((r.get("seg") or {}).get("sys_md5") or "") for r in rows]
    fps = [h for h in fps if h]
    chg = sum(1 for a, b in zip(fps, fps[1:]) if a != b)
    print("\nsys_prompt 指纹变化 %d 次 / %d 轮（每变一次 = 整条前缀全废）"
          % (chg, len(rows)))


if __name__ == "__main__":
    main()
