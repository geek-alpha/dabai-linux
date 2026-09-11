"""断点归因排行：把逐轮「断在哪一段、白烧多少」按原因聚合排序。

用法：
  python tools/break_attrib.py            # 归因排行 + 最近 12 轮
  python tools/break_attrib.py --corr     # 再叠加「历史视图事件」做交叉验证

为什么单独成工具：prefix_audit 给的是逐轮明细，看得见「哪轮贵」，
看不见「哪种原因贵」。而修哪里完全取决于归因排行——
本次排查就是靠它一眼看出「history[0] 占 49%、sys_prompt 占 26%」，
而不是继续盯着单轮数字猜。
"""
import io
import json
import os
import sys
from collections import Counter, defaultdict

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load(name):
    p = os.path.join(BASE, "data", name)
    if not os.path.exists(p):
        return []
    out = []
    for line in io.open(p, encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            pass
    return out


def _fmt(n):
    n = int(n or 0)
    if n >= 1000000:
        return "%.1fM" % (n / 1000000.0)
    if n >= 1000:
        return "%.1fk" % (n / 1000.0)
    return str(n)


def _norm_label(lab):
    """history[45]（共 55 条）→ history[N]：条数只是窗口位置，不是原因。"""
    if lab.startswith("history["):
        return "history[N]（窗口起点滑动）"
    return lab


def attrib():
    rows = _load("turn_metrics.jsonl")
    if not rows:
        print("暂无轮次指标（data/turn_metrics.jsonl 为空）")
        return
    by = defaultdict(lambda: {"n": 0, "wasted": 0})
    for r in rows:
        pf = r.get("prefix") or {}
        if not pf:
            continue
        lab = pf.get("break_label") or ("无上轮状态（冷启动）" if pf.get("cold") else "无数据")
        lab = _norm_label(lab)
        by[lab]["n"] += 1
        by[lab]["wasted"] += pf.get("wasted_chars") or 0
    tot = sum(v["wasted"] for v in by.values()) or 1

    print("=== 断点归因排行（共 %d 轮，白烧合计 %s 字符）===" % (len(rows), _fmt(tot)))
    print("  %-26s %5s %10s %8s %10s" % ("断点段", "轮数", "白烧", "占比", "平均/轮"))
    for lab, v in sorted(by.items(), key=lambda kv: -kv[1]["wasted"]):
        print("  %-26s %5d %10s %7.1f%% %10s"
              % (lab, v["n"], _fmt(v["wasted"]), 100.0 * v["wasted"] / tot,
                 _fmt(v["wasted"] // max(v["n"], 1))))

    print()
    print("=== 最近 12 轮（brk≈history 末尾 = 健康；brk<=3 = 整段历史白付）===")
    print("  %-8s %-22s %6s %6s %6s %10s"
          % ("ts", "断点段", "第几条", "上轮条", "本轮条", "白烧"))
    for r in rows[-12:]:
        pf = r.get("prefix") or {}
        print("  %-8s %-22s %6s %6s %6s %10s"
              % (str(r.get("ts"))[-6:], (pf.get("break_label") or "-")[:22],
                 pf.get("break_at"), pf.get("prev_msgs"), pf.get("cur_msgs"),
                 _fmt(pf.get("wasted_chars") or 0)))


def corr():
    """交叉验证：坏轮次是不是都挨着「历史视图被重建」的那一刻。

    这是本次定位根因的关键一步——只看向量指标永远分不清
    「窗口在滑动」和「视图被清掉重建」。
    """
    rows = _load("turn_metrics.jsonl")
    hv = [r for r in _load("hist_view_trace.jsonl") if r.get("tag") == "run"]
    print()
    print("=== 历史视图事件（真实运行）模式分布：%s ==="
          % dict(Counter(r.get("mode") for r in hv)))
    if not hv:
        print("  （无 run 样本：多为 tag=test 的合成数据，用 --corr 前先跑几轮真实对话）")
        return
    for label, cond in (("坏轮次(brk<=3)", lambda b: b <= 3),
                        ("好轮次(brk>10)", lambda b: b > 10)):
        print()
        print("=== %s ===" % label)
        for r in rows:
            pf = r.get("prefix") or {}
            b = pf.get("break_at")
            if b is None or not cond(b):
                continue
            ts = r.get("ts") or 0
            near = sorted([e for e in hv if abs((e.get("t") or 0) - ts) < 120],
                          key=lambda e: abs((e.get("t") or 0) - ts))[:2]
            print("--- ts=%s brk=%s 上轮 %s 条 / 本轮 %s 条 / 白烧 %s"
                  % (str(ts)[-6:], b, pf.get("prev_msgs"), pf.get("cur_msgs"),
                     _fmt(pf.get("wasted_chars") or 0)))
            for e in near:
                print("      Δt=%+5.0fs mode=%-12s packed=%s prev=%s anchor=%s view=%s"
                      % ((e.get("t") or 0) - ts, e.get("mode"), e.get("packed"),
                         e.get("prev"), e.get("anchor"), e.get("view")))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    attrib()
    if "--corr" in sys.argv:
        corr()
