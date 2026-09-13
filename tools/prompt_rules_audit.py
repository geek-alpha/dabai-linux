#!/usr/bin/env python3
"""规则区命中率审计（只读，不改任何东西）。

为什么要有它：提示词规则区每轮都注入，是纯成本；但「哪条规则值得留」一直靠
感觉裁。本工具把每条规则的成本（字符数 + 占系统提示词比例）和它针对的行为的
实测数据摆在一起，让删除决定有据可依。

五类判定：
  MEASURED_OK    有埋点，该规则针对的坏行为实测 < 1%    → 规则零收益，候选删除
  MEASURED_GAP   有埋点，坏行为仍显著（>= 1%）          → 问题还在，规则没治好
  MEASURED_USED  有埋点，该行为确实在发生              → 规则有作用面，不能删
  INSUFFICIENT   有数据源能回答这个问题，但样本不够    → 等样本，补埋点没用
  UNMEASURED     没有任何数据源能反映该行为            → 要补的是埋点

2026-09-14：agent.py 为「删除/画图/音乐」三条补了计数埋点（turn_metrics 的
delete_ops / img_gen_calls / music_calls）。计数只回答「行为发生过没有」——
发生过就是规则的作用面；没发生不等于规则没用。

2026-09-14（判定拆类）：原先 UNMEASURED 把「没埋点」和「有埋点但样本不足」混
成一类，报告只写「无埋点 7 条」——读者会去补 7 个埋点，实际只有 4 条真缺数据
源，另 3 条（画图/音乐/防重复委派）埋点早就就位、缺的是样本。混在一起会让行动
指引整个错向，所以拆开：INSUFFICIENT 的下一步是等样本，UNMEASURED 才是补埋点。

数据来源：
  成本：agent.py 里 agent_rules 字符串字面量（用 AST 取，不能用正则——串里有转义）
  占比：data/turn_metrics.jsonl 的 seg.sys（系统提示词总字符）
  收益：data/turn_metrics.jsonl 的 tool_calls/batches/re_reads/truncations/mergeable_calls
  委派：data/sub_agents.jsonl（委派记录，弱证据：只有条数与任务长度）

用法：
  python tools/prompt_rules_audit.py            # 人读报告
  python tools/prompt_rules_audit.py --json     # 机读（供测试断言）
"""
from __future__ import annotations

import ast
import hashlib
import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
AGENT_PY = BASE / "agent.py"
METRICS = BASE / "data" / "turn_metrics.jsonl"
SUBAGENTS = BASE / "data" / "sub_agents.jsonl"

# 坏行为率低于此值 → 该规则针对的问题实测几乎不发生
LOW_RATE = 0.01

# 委派指纹判据：同一任务指纹被反复委派时，怎么区分「定时调度」与「原样重发」。
# 定时任务的时间间隔落在整点小时上（±5min），重发没有这个规律。
SCHED_TOL = 300
SCHED_PERIOD = 3600
# 唯一任务数不到这个量，即使零重复也不能判「规则零收益」（样本太小）
DELEGATE_MIN_UNIQUE = 8

# 规则 → 指标映射。key 是规则文本的稳定前缀（改文本会让映射失配并报错，
# 避免审计表悄悄过期）。kind=metric 的规则能从 turn_metrics 直接量化；
# kind=none 的规则当前无任何日志能反映，只能标 UNMEASURED。
# 三条「行为暴露量」指标名 → 中文标签，只回答行为发生过没有。
_OP_METRICS = {
    "img_gen_calls": "画图",
    "music_calls": "音乐",
    "delete_ops": "删除",
}
RULE_MAP = [
    ("委派任务用 delegate_agent_task", "delegate", "subagent"),
    ("简单任务直接做", "delegate", "subagent"),
    ("防重复委派", "delegate", "subagent"),
    ("删除类任务", "metric", "delete_ops"),
    ("画图/图片", "metric", "img_gen_calls"),
    ("音乐/听歌", "metric", "music_calls"),
    ("并行优先", "parallel", "batch_ratio"),
    ("不重复读", "metric", "re_reads"),
    ("长文件", "metric", "truncations"),
    ("摸清大项目", "none", None),
    ("说重点", "none", None),
]


def extract_rules() -> tuple[list[str], str]:
    """从 agent.py 取规则区文本与每条规则。返回 (规则列表, 完整文本)。"""
    tree = ast.parse(AGENT_PY.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if not any(getattr(t, "id", "") == "agent_rules" for t in node.targets):
            continue
        if not isinstance(node.value, ast.Constant):
            raise SystemExit("agent_rules 不是单个字符串常量，提取逻辑需更新")
        text = node.value.value
        # 末条「工具详细用法…」是尾巴提示，不是规则
        rules = [p.strip() for p in text.split("\n")
                 if p.strip() and not p.strip().startswith("工具详细用法")]
        return rules, text
    raise SystemExit("agent.py 里找不到 agent_rules")


def load_metrics() -> list[dict]:
    if not METRICS.exists():
        return []
    out = []
    for line in METRICS.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def load_subagents() -> list[dict]:
    if not SUBAGENTS.exists():
        return []
    out = []
    for line in SUBAGENTS.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def delegation_stats(subs: list[dict]) -> dict:
    """按任务指纹归组，区分「定时调度重复」与「疑似原样重发」。

    sub_agents.jsonl 里同一个 id 会有多行（spawn/status/done 状态事件），
    必须先按 id 归组取首事件，否则同一任务会被数成多条。
    """
    by_id: dict[str, list[dict]] = {}
    for s in subs:
        by_id.setdefault(str(s.get("id") or ""), []).append(s)
    groups: dict[str, list[float]] = defaultdict(list)
    for evs in by_id.values():
        first = evs[0]
        ts = float(first.get("created_at") or 0) / 1000.0
        fp = hashlib.md5(str(first.get("task") or "").encode("utf-8")).hexdigest()[:8]
        groups[fp].append(ts)

    sched = suspect = 0
    for ts_list in groups.values():
        if len(ts_list) < 2:
            continue
        ts_list.sort()
        gaps = [b - a for a, b in zip(ts_list, ts_list[1:])]
        # 定时调度必须至少跨一个完整周期：5 分钟的间隔虽落在「整点 ±5min」窗口内，
        # 但 round(g/3600) 得 0——那是重发，不是定时。
        hourly = all(round(g / SCHED_PERIOD) >= 1
                     and abs(g - round(g / SCHED_PERIOD) * SCHED_PERIOD) <= SCHED_TOL
                     for g in gaps)
        if hourly:
            sched += len(ts_list) - 1
        else:
            suspect += len(ts_list) - 1
    return {
        "records": len(subs),
        "unique_tasks": len(groups),
        "sched_repeats": sched,
        "suspect_repeats": suspect,
    }


def totals(rows: list[dict]) -> dict:
    s = lambda k: sum(r.get(k, 0) or 0 for r in rows)  # noqa: E731
    tc = s("tool_calls")
    return {
        "turns": len(rows),
        "tool_calls": tc,
        "batches": s("batches"),
        "tool_rounds": s("tool_rounds"),
        "re_reads": s("re_reads"),
        "cross_reads": s("cross_reads"),
        "truncations": s("truncations"),
        "mergeable_calls": s("mergeable_calls"),
        # 规则区三条「行为暴露量」埋点（0 次 = 未被触发，不是规则没用）
        "img_gen_calls": s("img_gen_calls"),
        "music_calls": s("music_calls"),
        "delete_ops": s("delete_ops"),
        "tool_errors": s("tool_errors"),
        "llm_calls": s("llm_calls"),
        "prompt_tokens": s("prompt_tokens"),
        # 每次 LLM 往返都要重发完整 prompt（含全部工具定义），所以
        # 「往返次数 × 单次 prompt」才是成本量级——省往返省的是钱，不只是时间。
        "per_call_tokens": round(s("prompt_tokens") / max(s("llm_calls"), 1)),
        "per_batch": round(tc / max(s("batches"), 1), 2),
        "per_llm_round": round(tc / max(s("tool_rounds"), 1), 2),
        "re_read_rate": round(s("re_reads") / max(tc, 1), 4),
        "trunc_rate": round(s("truncations") / max(tc, 1), 4),
    }


def sys_chars(rows: list[dict]) -> int:
    """系统提示词字符数（取最近一轮的 seg.sys）。"""
    for r in reversed(rows):
        seg = r.get("seg") or {}
        if isinstance(seg, dict) and seg.get("sys"):
            return int(seg["sys"])
    return 0


def judge(kind: str, metric: str | None, agg: dict, subs: list[dict],
          label: str = "") -> tuple[str, str]:
    """返回 (判定, 观测值描述)。label 用于区分同 kind 下的不同规则。"""
    if kind == "none":
        return "UNMEASURED", "无任何日志反映该行为"
    if kind == "delegate":
        n = len(subs)
        lens = [len(str(s.get("task") or "")) for s in subs]
        avg = int(sum(lens) / len(lens)) if lens else 0
        if label.startswith("防重复委派"):
            d = delegation_stats(subs)
            base = (f"委派 {d['records']} 条 / 唯一任务 {d['unique_tasks']} 个；"
                    f"定时重复 {d['sched_repeats']} 次，疑似原样重发 {d['suspect_repeats']} 次")
            if d["suspect_repeats"] > 0:
                return "MEASURED_GAP", base + "——规则没拦住重发"
            if d["unique_tasks"] < DELEGATE_MIN_UNIQUE:
                return "INSUFFICIENT", (base + f"——样本不足（唯一任务 < {DELEGATE_MIN_UNIQUE}），"
                                        "零重复也不足以判零收益")
            return "MEASURED_OK", base + "——无重发，规则零收益候选"
        return "UNMEASURED", f"委派记录 {n} 条，任务均长 {avg} 字符（无法判「是否本该自己做」）"
    if metric == "batch_ratio":
        pb = agg["per_batch"]
        pl = agg["per_llm_round"]
        # 两个并行度不是一回事，判据必须用后者：
        #   per_batch     = 一轮内的执行分批（_plan_batches 切的，只吃墙钟，模型管不了）
        #   per_llm_round = 每次往返发几个工具（这才吃 token——一次往返重发整份 prompt）
        # 用 per_batch 当判据是错配：规则要模型做的是「同轮多发几个」。
        calls, tc = agg.get("llm_calls", 0), agg.get("tool_calls", 0)
        cost = ""
        if calls and tc:
            ideal = tc / 2.0  # 每轮发 2 个工具时的往返次数下界
            save = max(0.0, 1 - ideal / calls)
            # save 是理论上界，不是可拿到的收益：单发往返多为探索式工作流的必然，
            # 可合并空间已由 mergeable_calls 实测。写明上界属性，免得下一轮又去
            # 优化并行度（2026-09-14 已定案：并行度不是 token 杠杆）。
            cost = ("；往返 %d 次 × 每次 %.1fk tokens（合计 %.1fM）。"
                    "每轮发 2 个工具的理论上界省 %.0f%%，但实测可合并空间只剩 %d 次 / %d 轮"
                    "（单发往返是探索式工作的常态，不是调度缺陷；执行并行度只吃墙钟）"
                    % (calls, agg.get("per_call_tokens", 0) / 1000,
                       agg.get("prompt_tokens", 0) / 1e6, save * 100,
                       agg.get("mergeable_calls", 0), agg.get("turns", 0)))
        if pl >= 2.0:
            return "MEASURED_OK", f"每 LLM 轮 {pl} 个工具（>=2 已达标）；每批 {pb} 个{cost}"
        return "MEASURED_GAP", (f"每 LLM 轮 {pl} 个工具 / 每批 {pb} 个"
                               "（往返合并率 < 2，模型同轮发得太少）" + cost)
    if metric == "re_reads":
        r = agg["re_read_rate"]
        tag = "MEASURED_OK" if r < LOW_RATE else "MEASURED_GAP"
        return tag, f"重读率 {r * 100:.2f}%（{agg['re_reads']}/{agg['tool_calls']}）"
    if metric == "truncations":
        r = agg["trunc_rate"]
        tag = "MEASURED_OK" if r < LOW_RATE else "MEASURED_GAP"
        return tag, f"截断率 {r * 100:.2f}%（{agg['truncations']}/{agg['tool_calls']}）"
    if metric in _OP_METRICS:
        n = agg.get(metric, 0)
        label = _OP_METRICS[metric]
        if n <= 0:
            return "INSUFFICIENT", (
                f"埋点已就位（{metric}），但 {agg['turns']} 轮内该行为 0 次"
                "——规则从未被触发，删留仍无据")
        return "MEASURED_USED", (
            f"{label}行为实测 {n} 次（{agg['turns']} 轮 / {agg['tool_calls']} 次调用）"
            "——规则有实际作用面")
    return "UNMEASURED", "映射表未覆盖"


def build() -> dict:
    rules, text = extract_rules()
    rows = load_metrics()
    subs = load_subagents()
    agg = totals(rows)
    sysn = sys_chars(rows)

    entries, unmatched = [], []
    for idx, rule in enumerate(rules, 1):
        # 规则文本以「⚠ 」开头，映射 key 不带前缀——先 normalize 再匹配
        key = rule.lstrip("⚠").strip()
        hit = next((m for m in RULE_MAP if key.startswith(m[0])), None)
        if hit is None:
            unmatched.append(rule[:40])
            continue
        kind, metric = hit[1], hit[2]
        verdict, obs = judge(kind, metric, agg, subs, hit[0])
        entries.append({
            "id": idx,
            "rule": rule,
            "label": hit[0],
            "chars": len(rule),
            "share_of_rules": round(len(rule) / max(len(text), 1), 4),
            "share_of_sys": round(len(rule) / sysn, 4) if sysn else None,
            "hard": rule.startswith("⚠"),
            "verdict": verdict,
            "observed": obs,
        })

    # 分段趋势：坏行为有没有随时间改善
    trend = []
    by_day = defaultdict(list)
    for r in rows:
        by_day[datetime.fromtimestamp(r["ts"]).strftime("%m-%d")].append(r)
    for day in sorted(by_day):
        t = totals(by_day[day])
        trend.append({"day": day, **{k: t[k] for k in
                                     ("turns", "tool_calls", "per_batch", "per_llm_round",
                                      "re_read_rate", "trunc_rate")}})

    return {
        "rules_total_chars": len(text),
        "rules_count": len(rules),
        "sys_chars": sysn,
        "rules_share_of_sys": round(len(text) / sysn, 4) if sysn else None,
        "mapped": len(entries),
        "unmatched": unmatched,
        "map_drift": len(unmatched) > 0,
        "agg": agg,
        "delegation": delegation_stats(subs),
        "trend": trend,
        "entries": entries,
    }


def render(rep: dict) -> str:
    out = []
    out.append("规则区命中率审计（只读）")
    out.append("=" * 68)
    if rep["map_drift"]:
        out.append(f"[!] 映射表失配 {len(rep['unmatched'])} 条规则未覆盖，审计结论不可信：")
        for u in rep["unmatched"]:
            out.append(f"    - {u}")
        out.append("    修 RULE_MAP 后重跑。")
    share = rep["rules_share_of_sys"]
    if share:
        out.append(f"成本：{rep['rules_count']} 条 / {rep['rules_total_chars']} 字符"
                   f"，占系统提示词 {rep['sys_chars']} 字符的 {share * 100:.1f}%")
    else:
        out.append(f"成本：{rep['rules_count']} 条 / {rep['rules_total_chars']} 字符"
                   "（无 seg.sys 数据，占比未知）")
    a = rep["agg"]
    out.append(f"样本：{a['turns']} 轮 / {a['tool_calls']} 次工具调用 / "
               f"{a['tool_rounds']} 次 LLM 往返")
    if a.get("llm_calls"):
        out.append(f"成本量级：{a['llm_calls']} 次 LLM 往返 × 每次约 {a['per_call_tokens']} tokens "
                   f"= 合计 {a['prompt_tokens'] / 1e6:.1f}M prompt tokens"
                   "（每次往返都重发完整 prompt，含全部工具定义）")
    d = rep.get("delegation") or {}
    if d:
        out.append(f"委派：{d['records']} 条记录 / {d['unique_tasks']} 个唯一任务"
                   f"（定时重复 {d['sched_repeats']}、疑似重发 {d['suspect_repeats']}）")
    out.append("")
    out.append(f"{'规则':<22}{'字符':>5}{'占比':>7}  判定 / 实测")
    out.append("-" * 68)
    for e in rep["entries"]:
        tag = {"MEASURED_OK": "零收益?", "MEASURED_GAP": "问题仍在",
               "MEASURED_USED": "行为发生", "INSUFFICIENT": "样本不足",
               "UNMEASURED": "无数据源"}[e["verdict"]]
        out.append(f"{e['label'][:20]:<22}{e['chars']:>5}{e['share_of_rules'] * 100:>6.1f}%  "
                   f"{tag} {e['observed']}")
    out.append("")
    out.append("趋势（坏行为有没有随规则改动改善）")
    out.append("-" * 68)
    out.append(f"{'日期':>6}{'轮':>6}{'调用':>7}{'每批':>7}{'每LLM轮':>9}{'重读率':>8}{'截断率':>8}")
    for t in rep["trend"]:
        out.append(f"{t['day']:>6}{t['turns']:>6}{t['tool_calls']:>7}"
                   f"{t['per_batch']:>7.2f}{t['per_llm_round']:>9.2f}"
                   f"{t['re_read_rate'] * 100:>7.2f}%{t['trunc_rate'] * 100:>7.2f}%")
    out.append("")
    ok = [e["label"] for e in rep["entries"] if e["verdict"] == "MEASURED_OK"]
    gap = [e["label"] for e in rep["entries"] if e["verdict"] == "MEASURED_GAP"]
    un = [e["label"] for e in rep["entries"] if e["verdict"] == "UNMEASURED"]
    ins = [e["label"] for e in rep["entries"] if e["verdict"] == "INSUFFICIENT"]
    used = [e["label"] for e in rep["entries"] if e["verdict"] == "MEASURED_USED"]
    out.append(f"零收益候选（{len(ok)}）：{'、'.join(ok) or '无'}")
    out.append(f"问题仍在  （{len(gap)}）：{'、'.join(gap) or '无'}")
    out.append(f"行为发生  （{len(used)}）：{'、'.join(used) or '无'}（规则有作用面，不可删）")
    out.append(f"样本不足  （{len(ins)}）：{'、'.join(ins) or '无'}（埋点已就位，等样本——补埋点没用）")
    out.append(f"无数据源  （{len(un)}）：{'、'.join(un) or '无'}（要补的是埋点）")
    return "\n".join(out)


def main() -> int:
    rep = build()
    if "--json" in sys.argv:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
    else:
        print(render(rep))
    return 1 if rep["map_drift"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
