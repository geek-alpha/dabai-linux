#!/usr/bin/env python3
"""同错复发报告：某类工具报错第一次出现之后，还犯不犯。

为什么需要它：`turn_metrics` 只有 tool_errors 计数（分不清哪类错），且 KEEP_LINES=300
（实测 228 轮≈1.4 天）会滚掉历史，跨周对比无从谈起。

数据源与各自的局限（不要混着读）：
  data/longrun/traces/*.jsonl  ToolCallResult 带 tool_name + success + result 原文
                              → 能分类、能按 cycle 排时序；但没有 ts 字段，只能按 cycle 编号定位
  data/tool_err_trace.jsonl    交互轮长期流水，只有工具名、没有报错原文
                              → 只能做工具级复发，不能做错误类型级
  harness_task_memory.json     ts 表（教训写入时刻），键与 gene_fitness.key_of 同构

用法：
    venv/bin/python tools/err_recidivism.py            # 文本报告
    venv/bin/python tools/err_recidivism.py --json     # 机器可读
    venv/bin/python tools/err_recidivism.py --min-rec 2  # 只列复发≥2 次的类
"""
import argparse
import collections
import glob
import json
import os
import re
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TRACE_DIR = os.path.join(BASE_DIR, "data", "longrun", "traces")
ERR_TRACE_PATH = os.path.join(BASE_DIR, "data", "tool_err_trace.jsonl")
MEMORY_PATH = os.path.join(BASE_DIR, "harness_task_memory.json")

# 分类靠报错原文里的特征串。顺序有意义：先判更具体的。
# 每一类的「怎么修」写在 fix 里——报告要能直接推出下一步动作，不是只给数字。
CLASSES = [
    ("A", "技能未加载", ("尚未注册", "请先调用 skill_help"),
     "工具已存在但技能未激活：可在 harness 内自动 skill_help 后重试，省掉一次往返"),
    ("B", "工具名幻觉", ("不存在或未注册",),
     "模型编了工具名：把可用工具名清单更显眼地放进 prompt"),
    ("C", "参数类型错", ("参数校验失败",),
     "schema 与模型预期不符：检查该工具的参数类型声明"),
    ("D", "工具不可用", ("不属于", "不可用"),
     "工具被禁用：检查技能激活状态"),
]
OTHER = ("E", "其他", (), "看原文")


def classify(result) -> str:
    """报错原文 → 类别代号。无法归类返回 'E'。"""
    text = str(result or "")
    for code, _name, needles, _fix in CLASSES:
        if any(n in text for n in needles):
            return code
    return OTHER[0]


def class_meta(code: str):
    for c, name, _n, fix in CLASSES:
        if c == code:
            return name, fix
    return OTHER[1], OTHER[3]


def _cycle_of(path: str) -> int:
    m = re.match(r"(\d+)", os.path.basename(path))
    return int(m.group(1)) if m else -1


def read_longrun_traces(trace_dir: str = TRACE_DIR) -> dict:
    """扫全部 longrun trace，返回调用数/失败分类/按 cycle 的时序。"""
    files = [p for p in glob.glob(os.path.join(trace_dir, "*.jsonl"))
             if _cycle_of(p) >= 0]
    files.sort(key=_cycle_of)

    total = 0
    by_tool = collections.Counter()
    fail_by_tool = collections.Counter()
    fail_by_class = collections.Counter()
    tool_by_class = collections.defaultdict(collections.Counter)
    cycles_with_class = collections.defaultdict(list)
    calls_per_cycle = collections.Counter()

    for path in files:
        cyc = _cycle_of(path)
        try:
            fh = open(path, encoding="utf-8")
        except OSError:
            continue
        with fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if row.get("type") != "ToolCallResult":
                    continue
                total += 1
                calls_per_cycle[cyc] += 1
                name = row.get("tool_name") or "?"
                by_tool[name] += 1
                if row.get("success"):
                    continue
                code = classify(row.get("result"))
                fail_by_class[code] += 1
                fail_by_tool[name] += 1
                tool_by_class[code][name] += 1
                cycles_with_class[code].append(cyc)

    return {
        "files": len(files),
        "cycle_range": [min(calls_per_cycle), max(calls_per_cycle)] if calls_per_cycle else [0, 0],
        "total": total,
        "fail": sum(fail_by_class.values()),
        "by_tool": dict(by_tool),
        "fail_by_tool": dict(fail_by_tool),
        "fail_by_class": dict(fail_by_class),
        "tool_by_class": {k: dict(v) for k, v in tool_by_class.items()},
        "cycles_with_class": {k: sorted(v) for k, v in cycles_with_class.items()},
    }


def read_interactive_errs(path: str = ERR_TRACE_PATH) -> dict:
    """读交互轮的长期报错流水（只有工具名，没有报错原文）。"""
    rows = []
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        return {"rows": [], "n": 0, "fail_by_tool": {}}
    counter = collections.Counter()
    for r in rows:
        for name in r.get("err_names") or []:
            counter[name] += 1
    return {"rows": rows, "n": len(rows), "fail_by_tool": dict(counter)}


def read_lesson_ts(path: str = MEMORY_PATH) -> dict:
    """教训写入时刻表。返回 {条目数, 有时间戳的条数}。"""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {"lessons": 0, "stamped": 0}
    lessons = data.get("lessons") or []
    ts = data.get("ts") or {}
    return {"lessons": len(lessons), "stamped": len(ts)}


def recurrence(cycles) -> dict:
    """给定某类报错出现的 cycle 序列，算复发情况。

    判据是「首次出现之后还犯不犯」——只在首次之前出现的不算复发，
    否则早期高密度会掩盖「有没有学会」。
    """
    if not cycles:
        return {"first": None, "after_first": 0, "span": 0, "cycles": []}
    # 去重：同一个 cycle 里同类错犯两次只算一个 cycle，否则「复发几个 cycle」会被翻倍。
    cycles = sorted(set(cycles))
    first = cycles[0]
    after = [c for c in cycles if c > first]
    return {
        "first": first,
        "after_first": len(after),
        "span": cycles[-1] - first,
        "cycles": cycles,
    }


def build_report(trace_dir: str = TRACE_DIR, err_path: str = ERR_TRACE_PATH,
                 memory_path: str = MEMORY_PATH) -> dict:
    traces = read_longrun_traces(trace_dir)
    inter = read_interactive_errs(err_path)
    lessons = read_lesson_ts(memory_path)

    rec = {}
    for code, cycles in traces["cycles_with_class"].items():
        rec[code] = recurrence(cycles)

    ranked = []
    for code, count in sorted(traces["fail_by_class"].items(),
                              key=lambda kv: -kv[1]):
        name, fix = class_meta(code)
        ranked.append({
            "code": code,
            "name": name,
            "fix": fix,
            "count": count,
            "share": round(100.0 * count / max(traces["fail"], 1), 1),
            "tools": traces["tool_by_class"].get(code, {}),
            "recurrence": rec.get(code, {}),
        })

    return {
        "sources": {
            "longrun": {
                "files": traces["files"],
                "cycle_range": traces["cycle_range"],
                "calls": traces["total"],
                "fails": traces["fail"],
                "fail_rate": round(100.0 * traces["fail"] / max(traces["total"], 1), 1),
            },
            "interactive": {"rows": inter["n"], "fail_by_tool": inter["fail_by_tool"]},
            "lessons": lessons,
        },
        "classes": ranked,
        "tool_rank": sorted(traces["by_tool"].items(), key=lambda kv: -kv[1]),
        "fail_tool_rank": sorted(traces["fail_by_tool"].items(), key=lambda kv: -kv[1]),
    }


def render(report: dict, min_rec: int = 1) -> str:
    src = report["sources"]
    lr = src["longrun"]
    out = []
    out.append("工具报错复发报告（同错复发率）")
    out.append("")
    out.append("数据源")
    out.append("  longrun traces  %d 文件 / cycle %d~%d / %d 次调用，失败 %d（%.1f%%）"
               % (lr["files"], lr["cycle_range"][0], lr["cycle_range"][1],
                  lr["calls"], lr["fails"], lr["fail_rate"]))
    inter = src["interactive"]
    if inter["rows"]:
        out.append("  交互轮流水      %d 行，报错工具：%s"
                   % (inter["rows"], inter["fail_by_tool"]))
    else:
        out.append("  交互轮流水      0 行（data/tool_err_trace.jsonl 从本轮起积累）")
    out.append("  经验库时间戳    %d 条中 %d 条有时间"
               % (src["lessons"]["lessons"], src["lessons"]["stamped"]))
    out.append("")

    if not report["classes"]:
        out.append("没有可分类的报错（longrun trace 里 ToolCallResult 全成功或缺文件）。")
        return "\n".join(out)

    out.append("报错分类（longrun，可分类 %d 次）" % lr["fails"])
    for item in report["classes"]:
        tools = " ".join("%s×%d" % (k, v) for k, v in
                         sorted(item["tools"].items(), key=lambda kv: -kv[1])[:5])
        out.append("  %s %-10s %3d  %5.1f%%   %s"
                   % (item["code"], item["name"], item["count"], item["share"], tools))
    out.append("")

    out.append("复发（判据：首次出现之后还犯不犯）")
    shown = 0
    for item in report["classes"]:
        r = item["recurrence"]
        if not r or r["first"] is None:
            continue
        if r["after_first"] < min_rec:
            continue
        shown += 1
        out.append("  %s %s：首见 cycle %d；其后 %d 个 cycle 仍复发（跨 %d 个 cycle）"
                   % (item["code"], item["name"], r["first"], r["after_first"], r["span"]))
        out.append("      → %s" % item["fix"])
    if not shown:
        out.append("  没有达到复发门槛的类别（--min-rec %d）" % min_rec)
    return "\n".join(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="同错复发报告")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--min-rec", type=int, default=1, help="只列复发≥N 次的类")
    ap.add_argument("--trace-dir", default=TRACE_DIR)
    ap.add_argument("--err-trace", default=ERR_TRACE_PATH)
    ap.add_argument("--memory", default=MEMORY_PATH)
    args = ap.parse_args(argv)

    report = build_report(args.trace_dir, args.err_trace, args.memory)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=1))
    else:
        print(render(report, args.min_rec))
    return 0


if __name__ == "__main__":
    sys.exit(main())
