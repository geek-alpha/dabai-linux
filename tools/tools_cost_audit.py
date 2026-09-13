#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""工具定义的成本审计：回答「按需加载值不值」这个具体问题。

背景：每次 LLM 往返都重发完整 prompt，其中工具定义（73~109 个，38169~54408 字符）
是固定前缀里最大的一块，而 turn_metrics 的 seg 分量根本不统计它——只看 seg 会
严重低估固定前缀。于是自然冒出一个提案：工具定义也按需加载，只带当前要用的。

这个提案有个反噬：工具集一变，整条前缀缓存全废（tools 排在请求最前）。
所以必须先出三个数字再决定动不动手：
  ① 按需加载省下多少（每轮实际带的工具定义 vs 全量常驻）
  ② 工具集变化带来多少额外 cache_miss
  ③ 两者的净账

只读，不改任何数据。

用法：
    venv/bin/python tools/tools_cost_audit.py            # 人读报告
    venv/bin/python tools/tools_cost_audit.py --json     # 机器可断言
"""
import argparse
import collections
import json
import sqlite3
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

DB = BASE / "chat_memory.db"
METRICS = BASE / "data" / "turn_metrics.jsonl"
TOOLS_TRACE = BASE / "data" / "tools_trace.jsonl"

# 字符 → token 的整体实测系数（122.8M prompt tokens / 168.6M prompt 字符）。
# 注意：英文为主的工具定义实际比这更省，所以拿它折算工具定义 tokens 是上界。
CHARS_PER_TOKEN = 1.37


def load_rows(path: Path = METRICS) -> list:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


def load_trace(path: Path = TOOLS_TRACE) -> list:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if rec.get("tag") == "test":
            continue  # 自检不写真实诊断文件，但历史文件可能残留
        out.append(rec)
    return out


def load_tool_calls(db: Path = DB) -> collections.Counter:
    """真实工具调用频次。数据源是 chat_memory.db 的 messages.tool_calls
    （一条 assistant 消息 = 一次 LLM 往返，其 tool_calls 就是该轮发出的工具）。"""
    cnt = collections.Counter()
    if not db.exists():
        return cnt
    con = sqlite3.connect(str(db))
    try:
        sql = ("select tool_calls from messages "
               "where tool_calls is not null and tool_calls != ''")
        for (raw,) in con.execute(sql):
            try:
                calls = json.loads(raw)
            except Exception:
                continue
            for tc in calls or []:
                nm = ((tc or {}).get("function") or {}).get("name")
                if nm:
                    cnt[str(nm)] += 1
    except Exception:
        pass
    finally:
        con.close()
    return cnt


def audit(rows: list, trace: list, calls: collections.Counter) -> dict:
    def agg(rs, k):
        return sum((r.get(k) or 0) for r in rs)

    def pf(r):
        return r.get("prefix") or {}

    changed = [r for r in rows if (pf(r).get("tools_diff") or {}).get("any")]
    stable = [r for r in rows if not (pf(r).get("tools_diff") or {}).get("any")]
    llm = agg(rows, "llm_calls")
    pt = agg(rows, "prompt_tokens")

    miss_chg = agg(changed, "cache_miss")
    miss_stb = agg(stable, "cache_miss")
    pt_chg = agg(changed, "prompt_tokens")
    pt_stb = agg(stable, "prompt_tokens")
    rate_chg = miss_chg / pt_chg if pt_chg else 0.0
    rate_stb = miss_stb / pt_stb if pt_stb else 0.0

    # 口径缺失轮：provider 没回传 cache 字段时 _read_cache 返回 (0,0)，与「真命中 0」
    # 无法区分。这类轮的 miss 进不了分子、prompt 却全额进分母 → miss 率被稀释压低，
    # 所以 miss_rate_all 只能当【下界】读（真值区间上界 = 这些 token 全按 miss）。
    def _has_cache_caliber(r):
        # 新埋点（2026-09-14 起）直接声明口径，优先信它：provider 明确回传 0/0 时
        # 数值启发式会误判成「缺口径」。旧行没有这个键，退回数值判据。
        flag = r.get("cache_reported")
        if flag is not None:
            return bool(flag)
        return (r.get("prompt_tokens") or 0) > 0 and (
            (r.get("cache_hit") or 0) + (r.get("cache_miss") or 0)) > 0

    reported = [r for r in rows if _has_cache_caliber(r)]
    unreported = [r for r in rows if not _has_cache_caliber(r)]
    pt_rep = agg(reported, "prompt_tokens")
    rate_rep = (agg(reported, "cache_miss") / pt_rep) if pt_rep else 0.0

    # ① 按需加载省了多少：实际平均工具定义 vs 观测到的全量常驻（最大工具集）
    mean_tools_chars = (agg(rows, "tools_chars") / len(rows)) if rows else 0.0
    max_tools_chars = max([(r.get("tools_chars") or 0) for r in rows] or [0])
    max_tools_count = max([(r.get("tools_count") or 0) for r in rows] or [0])
    saved_chars = max(0.0, (max_tools_chars - mean_tools_chars) * llm)

    # ② 工具集变化额外烧掉的 cache_miss：拿稳定轮的 miss 率做基线。
    # 零口径轮同时污染两侧、方向相反：stable 侧把基线稀释压低（extra 变大），
    # changed 侧把 pt_chg 撑大（extra 变小）。净效应实测为「修正后反而升高」——
    # 所以不能只换基线，分子分母口径必须一致，全部限制在 reported 子集内。
    # 代价：extra 只覆盖可测量的 prompt，读作【下界】。
    chg_rep = [r for r in changed if _has_cache_caliber(r)]
    stb_rep = [r for r in stable if _has_cache_caliber(r)]
    pt_chg_rep = agg(chg_rep, "prompt_tokens")
    pt_stb_rep = agg(stb_rep, "prompt_tokens")
    rate_chg_rep = agg(chg_rep, "cache_miss") / pt_chg_rep if pt_chg_rep else 0.0
    rate_stb_rep = agg(stb_rep, "cache_miss") / pt_stb_rep if pt_stb_rep else 0.0
    extra_miss = max(0.0, pt_chg_rep * (rate_chg_rep - rate_stb_rep))
    # 旧混合口径（分子含零口径轮的 0、分母含它们的 prompt）：留作对照与回归
    extra_miss_mixed = max(0.0, miss_chg - pt_chg * rate_stb)

    # ③ 净账（统一折成字符比较）
    extra_chars = extra_miss * CHARS_PER_TOKEN
    net_chars = saved_chars - extra_chars

    # 调用集中度
    total_calls = sum(calls.values())
    top = calls.most_common()
    def share(n):
        return sum(c for _, c in top[:n]) / total_calls if total_calls else 0.0

    all_names = set()
    for rec in trace:
        all_names.update(rec.get("names") or [])
    zero_used = sorted(n for n in all_names if n not in calls)

    reasons = collections.Counter((r.get("reason") or "?").split(":")[0] for r in trace)
    causes = collections.Counter((pf(r).get("cause") or "?")[:28] for r in changed)

    return {
        "turns": len(rows),
        "llm_calls": llm,
        "prompt_tokens": pt,
        "chars_per_token": CHARS_PER_TOKEN,
        "tools_count_max": max_tools_count,
        "tools_chars_max": max_tools_chars,
        "tools_chars_mean": round(mean_tools_chars, 1),
        "tools_share_of_prompt": round(
            mean_tools_chars / max(1.0, (pt / max(llm, 1)) * CHARS_PER_TOKEN) * 100, 1),
        "changed_turns": len(changed),
        "changed_ratio": round(len(changed) / len(rows) * 100, 1) if rows else 0.0,
        "miss_rate_changed": round(rate_chg * 100, 2),
        "miss_rate_stable": round(rate_stb * 100, 2),
        "miss_rate_all": round(agg(rows, "cache_miss") / pt * 100, 2) if pt else 0.0,
        "reported_rows": len(reported),
        "unreported_rows": len(unreported),
        "unreported_prompt_tokens": agg(unreported, "prompt_tokens"),
        "miss_rate_reported_only": round(rate_rep * 100, 2),
        "miss_rate_changed_reported": round(rate_chg_rep * 100, 2),
        "miss_rate_stable_reported": round(rate_stb_rep * 100, 2),
        "extra_miss_tokens": round(extra_miss),
        "extra_miss_tokens_mixed": round(extra_miss_mixed),
        "extra_miss_share_of_all": round(extra_miss / pt * 100, 2) if pt else 0.0,
        "saved_chars": round(saved_chars),
        "extra_chars": round(extra_chars),
        "net_chars": round(net_chars),
        "verdict": "KEEP_LAZY" if net_chars > 0 else "GO_EAGER",
        "calls_total": total_calls,
        "calls_distinct": len(calls),
        "top1_share": round(share(1) * 100, 1),
        "top5_share": round(share(5) * 100, 1),
        "top10_share": round(share(10) * 100, 1),
        "zero_used": zero_used,
        "zero_used_count": len(zero_used),
        "trace_reasons": dict(reasons),
        "changed_causes": dict(causes),
        "top_tools": top[:12],
    }


def render(a: dict) -> str:
    L = []
    L.append("工具定义成本审计（只读）")
    L.append("=" * 68)
    L.append(f"样本：{a['turns']} 轮 / {a['llm_calls']} 次 LLM 往返 / "
             f"{a['prompt_tokens']:,} prompt tokens")
    L.append(f"工具定义：均值 {a['tools_chars_mean']:,.0f} 字符"
             f"（占单次 prompt 约 {a['tools_share_of_prompt']}%），"
             f"最大 {a['tools_chars_max']:,} 字符 / {a['tools_count_max']} 个工具")
    L.append(f"折算系数：{a['chars_per_token']} 字符/token（工具定义实际更省，故为成本上界）")
    L.append("")
    L.append("① 按需加载省下多少（对比「全量常驻」）")
    L.append(f"   全量常驻 {a['tools_chars_max']:,} 字符 − 实测均值 {a['tools_chars_mean']:,.0f} 字符")
    L.append(f"   × {a['llm_calls']} 次往返 = 省 {a['saved_chars']:,} 字符")
    L.append("")
    L.append("② 工具集变化带来多少额外 cache_miss")
    L.append(f"   变化轮 {a['changed_turns']} 轮（{a['changed_ratio']}%）：miss 率 "
             f"{a['miss_rate_changed']}%")
    L.append(f"   稳定轮：miss 率 {a['miss_rate_stable']}%   ← 基线")
    L.append(f"   额外 miss = {a['extra_miss_tokens']:,} tokens"
             f"（占全部 prompt {a['extra_miss_share_of_all']}%）= "
             f"{a['extra_chars']:,} 字符")
    L.append(f"   口径缺失 {a['unreported_rows']} 轮（prompt {a['unreported_prompt_tokens']:,} "
             f"tokens）→ miss 率 {a['miss_rate_all']}% 是下界，剔除后 "
             f"{a['miss_rate_reported_only']}%")
    L.append(f"   额外 miss 只统计有口径的轮（基线 {a['miss_rate_stable_reported']}% "
             f"vs 变化轮 {a['miss_rate_changed_reported']}%）；旧混合口径给 "
             f"{a['extra_miss_tokens_mixed']:,}，差额来自零口径轮")
    L.append("")
    L.append("③ 净账")
    L.append(f"   省 {a['saved_chars']:,} 字符 − 多付 {a['extra_chars']:,} 字符 "
             f"= 净赚 {a['net_chars']:,} 字符")
    ratio = (a['saved_chars'] / a['extra_chars']) if a['extra_chars'] else float('inf')
    L.append(f"   收益/代价 = {ratio:.1f}× → 结论："
             f"{'按需加载保持现状' if a['verdict'] == 'KEEP_LAZY' else '考虑改全量常驻'}")
    L.append("")
    L.append("④ 调用集中度（真实工具调用）")
    L.append(f"   共 {a['calls_total']} 次 / {a['calls_distinct']} 个不同工具；"
             f"Top1 {a['top1_share']}% / Top5 {a['top5_share']}% / Top10 {a['top10_share']}%")
    for n, c in a["top_tools"]:
        L.append(f"     {c:5d}  {c / max(a['calls_total'], 1) * 100:5.1f}%  {n}")
    L.append(f"   观测到的工具名共 {a['zero_used_count'] + a['calls_distinct']} 个，"
             f"其中 {a['zero_used_count']} 个从未被调用过")
    L.append("")
    L.append("⑤ 工具集变化原因（tools_trace.jsonl）")
    for k, v in sorted(a["trace_reasons"].items(), key=lambda x: -x[1]):
        L.append(f"     {v:5d}  {k}")
    L.append("   变化轮的前缀断裂原因：")
    for k, v in sorted(a["changed_causes"].items(), key=lambda x: -x[1])[:5]:
        L.append(f"     {v:5d}  {k}")
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    rows = load_rows()
    if not rows:
        print("没有 turn_metrics 数据")
        return 1
    a = audit(rows, load_trace(), load_tool_calls())
    if args.json:
        print(json.dumps(a, ensure_ascii=False, indent=2))
    else:
        print(render(a))
    return 0


if __name__ == "__main__":
    sys.exit(main())
