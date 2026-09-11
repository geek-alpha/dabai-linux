"""现场诊断日志速览：tools_trace / hist_view_trace。

只读，不改任何运行数据。用来回答两个问题：
  1) 工具集还会不会被 evict（前缀整条失效的最大来源）
  2) 历史视图的锚点是否稳定（append 占多数 = 滞回生效）

用法：python tools/trace_report.py [文件...]
"""
import collections
import json
import os
import sys
import time

DEFAULT = ("data/tools_trace.jsonl", "data/hist_view_trace.jsonl")


def _fmt_ts(r):
    ts = r.get("ts") or r.get("t") or 0
    try:
        return time.strftime("%m-%d %H:%M:%S", time.localtime(float(ts)))
    except Exception:
        return "  ?  "


def report(path):
    print(f"=== {path}")
    if not os.path.exists(path):
        print("  (文件不存在：还没产生样本)")
        return
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception:
                pass
    print(f"  样本 {len(rows)} 条")
    if not rows:
        return

    # 来源分离：测试/探针的合成样本不能和真实运行混在一起看
    tags = collections.Counter(str(r.get("tag") or "legacy") for r in rows)
    print("  来源分布: " + ", ".join(f"{k}×{v}" for k, v in tags.most_common()))
    rows = [r for r in rows if str(r.get("tag") or "legacy") == "run"]
    if not rows:
        print("  ⚠ 无真实运行样本（全是测试/探针造的，真实跑几轮再看）")
        return
    print(f"  真实运行样本 {len(rows)} 条")

    # 归类字段：tools_trace 用 reason，hist_view_trace 用 mode
    kind = collections.Counter()
    for r in rows:
        k = r.get("action") or r.get("mode") or r.get("reason") or r.get("event") or "?"
        kind[str(k)] += 1
    print("  类型分布: " + ", ".join(f"{k}×{v}" for k, v in kind.most_common()))

    # evict 专项：这是唯一会让整条前缀失效的动作
    ev = [r for r in rows
          if str(r.get("action") or r.get("mode") or r.get("reason") or "").startswith("evict")]
    print(f"  ★ evict 次数: {len(ev)}  (0 = 前缀不再被整条砍掉)")
    for r in ev[:5]:
        print("     " + json.dumps(r, ensure_ascii=False)[:200])

    # 工具数量走势：只增不减 = 期望行为
    counts = [(_fmt_ts(r), r.get("count")) for r in rows if r.get("count") is not None]
    if counts:
        print("  工具数走势: " + " → ".join(str(c) for _, c in counts[-12:]))

    # 实例分布：同一 sid 反复 reset_sid 时，用来分辨「视图被清」还是「换了实例」。
    # 只看 inst 会漏掉「同一实例但视图被清」，所以两者一起看。
    insts = [(r.get("inst"), r.get("mode") or r.get("reason")) for r in rows
             if r.get("inst") is not None]
    if insts:
        uniq = collections.OrderedDict()
        for i, m in insts:
            uniq.setdefault(str(i), []).append(str(m))
        print(f"  实例数: {len(uniq)}  (同一会话 reset_sid 反复出现时看这里)")
        for i, ms in list(uniq.items())[-6:]:
            print(f"     {i}: {len(ms)} 条  " + ",".join(ms[:6]))

    # 前缀断裂：这几种 mode 会让已发过的历史作废。restore_lost 也算——它虽然
    # 「读回了视图」，但锚点丢了、实际走的是全量重建，白烧一分不少。
    # 但「新实例首轮的 reset_sid」不算断裂——那时 psid 为空，压根没有前缀可断，
    # 计进去只会造假警报（实测 4 条 reset_sid 里 1 条正是这种）。
    def _real_break(r) -> bool:
        m = str(r.get("mode") or r.get("reason"))
        if m not in ("reset_sid", "anchor_lost", "trim", "restore_lost"):
            return False
        return not (m == "reset_sid" and "psid" in r and not str(r.get("psid") or ""))

    brk = collections.Counter(
        str(r.get("mode") or r.get("reason")) for r in rows if _real_break(r))
    if brk:
        print("  ★ 前缀断裂: " + ", ".join(f"{k}×{v}" for k, v in brk.most_common()))
    else:
        print("  ★ 前缀断裂: 0  (历史只追加，无重建)")

    # 跨进程接回：这条线就是「重启不再等于换会话」的验收证据。
    # 没有它，就只能人肉翻 mode 字段确认修复到底生效没有。
    _rs = sum(1 for r in rows if str(r.get("mode")) == "restore")
    _rl = sum(1 for r in rows if str(r.get("mode")) == "restore_lost")
    if _rs or _rl:
        _t = f"  ★ 跨进程接回: restore×{_rs}"
        if _rl:
            _t += f", 锚点丢失退化重建×{_rl}"
        else:
            _t += "（视图跨重启存活）"
        print(_t)
    else:
        print("  ★ 跨进程接回: 0  (还没遇到「重启后同会话」的样本)")


    # 重置归因：psid 一出来，reset_sid 就不用猜了。
    # 没有这一段时，「同一 sid 反复 reset_sid」只能靠人肉翻时间戳。
    resets = [r for r in rows if str(r.get("mode")) == "reset_sid"]
    if resets:
        why = collections.Counter()
        for r in resets:
            if "psid" not in r:
                why["无psid(旧样本)"] += 1
                continue
            psid, sid = str(r.get("psid") or ""), str(r.get("sid") or "")
            if not psid:
                why["新实例/进程重启(psid空)"] += 1
            elif psid != sid:
                why["会话切换(预期)"] += 1
            else:
                why["同会话清视图(真bug)"] += 1
        print("  ★ 重置归因: " + ", ".join(f"{k}×{v}" for k, v in why.most_common()))
        # 终审：只有「同会话却把视图清了」才是真 bug，其余都是预期。
        bug = why.get("同会话清视图(真bug)", 0)
        print("  ★ 滞回判定: " + ("真实路径生效（无同会话清视图）" if not bug
                              else f"同会话清视图 {bug} 次，需修"))

    print("  最近 8 条:")
    for r in rows[-8:]:
        print("    " + _fmt_ts(r) + "  " + json.dumps(r, ensure_ascii=False)[:200])


def main():
    targets = sys.argv[1:] or list(DEFAULT)
    for p in targets:
        report(p)
        print()


if __name__ == "__main__":
    main()
