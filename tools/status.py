#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""自我记账 + 结构化反问：把「我变强了没有」变成数字，把「我可能错在哪」变成流程。

背景（2026-09-12）：能力和动力都不能靠自觉。
  规则是静态文本，越长越互相稀释；教训是数据，写一次永久生效。
  所以变强的方向是：规则区变薄、经验库变厚、事业有推进、错误有复盘。
读出端在本脚本，写入端是 tools/lesson_add.py 与 tools/long_horizon.py。

用法：
  status.py                     记账（人看）
  status.py --json              机器可读
  status.py challenge "结论"     结构化反问：强制过证据/反例/权威/反向假设
"""
import argparse
import importlib.util
import json
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
AGENT = BASE / "agent.py"
LESSONS = BASE / "harness_task_memory.json"
HORIZON = BASE / "long_horizon.json"
DRAFTS = BASE / "data" / "learn_drafts.json"
LESSON_STATE = BASE / "data" / "lesson_state.json"
LESSON_ARCHIVE = BASE / "harness_task_memory.archive.json"



def _load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def rule_stats():
    """规则区口径只有一处定义：tools/rule_budget.py，本函数只负责展示。

    2026-09-22 之前这里自己数一遍（锚点区间 + 字符串字面量）得 raw 3584 / 17 条，
    而 prompt_rules_audit 只数 agent_rules 段得 1432 / 11 条——两个工具各管一半、
    都叫「规则区」，两段区间根本不重叠，谁都没看到全貌。现在合计口径由预算门禁
    统一给出；这里若再出现锚点常量，rule_budget 的 SECOND_SOURCE 会报红。
    """
    try:
        spec = importlib.util.spec_from_file_location(
            "_status_rule_budget", Path(__file__).resolve().parent / "rule_budget.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        rep = mod.build()
    except Exception:
        return {"chars": 0, "clauses": 0, "anchored": False, "level": "unknown",
                "target": 0, "healthy": 0, "over": 0}
    return {
        "chars": rep["total_chars"],
        "clauses": rep["block_count"],
        "anchored": True,
        "level": rep["level"],
        "target": rep["target"],
        "healthy": rep["healthy"],
        "over": rep["over_healthy"],
    }


def lessons():
    data = _load_json(LESSONS, {})
    got = data.get("lessons") if isinstance(data, dict) else None
    return got if isinstance(got, list) else []


def draft_count():
    """待审复盘草稿数（tools/learn_nudge.py 写入）：学习机有没有真的通电，看这个数。"""
    ds = _load_json(DRAFTS, [])
    if not isinstance(ds, list):
        return 0
    return sum(1 for d in ds if isinstance(d, dict) and d.get("status") == "pending")


def lifecycle():
    """生命周期计数（tools/lesson_curator.py 写入）：stale 只是标记，archived 是已移出主库。"""
    st = _load_json(LESSON_STATE, {})
    stale = sum(1 for v in (st.values() if isinstance(st, dict) else [])
                if isinstance(v, dict) and v.get("state") == "stale")
    arch = _load_json(LESSON_ARCHIVE, {})
    archived = len(arch.get("lessons") or []) if isinstance(arch, dict) else 0
    return stale, archived


def projects():
    data = _load_json(HORIZON, {})
    if not isinstance(data, dict):
        return [], []
    return (data.get("projects") or []), (data.get("questions") or [])


def collect():
    rules = rule_stats()
    lesson_list = lessons()
    proj_list, q_list = projects()
    active = [p for p in proj_list if p.get("stage", "active") == "active"]
    return {
        "rules": rules,
        "lessons": len(lesson_list),
        "projects": len(proj_list),
        "active": len(active),
        "questions": len(q_list),
        "drafts": draft_count(),
        "stale": lifecycle()[0],
        "archived": lifecycle()[1],
        "progress": [
            {"id": p.get("id"), "pct": p.get("progress", 0), "next": p.get("next", "")}
            for p in active
        ],
        "last_lesson": lesson_list[-1] if lesson_list else "",
    }


def cmd_report(as_json=False):
    d = collect()
    if as_json:
        print(json.dumps(d, ensure_ascii=False, indent=1))
        return 0
    r = d["rules"]
    tag = {"green": "绿", "yellow": "黄", "red": "红"}.get(r.get("level"), "?")
    over = ""
    if r.get("level") == "red":
        over = "  ← 超上限：先搬迁/压缩（tools/rule_budget.py）"
    elif r.get("level") == "yellow":
        over = "  ← 余量不足 5%，该瘦身了"
    print("【自我记账】")
    print(f"  规则区    {r['chars']} 字符 / {r['clauses']} 块"
          f"（{tag}，target {r['target']}，healthy {r['healthy']}）{over}")
    print(f"  经验库    {d['lessons']} 条")
    print(f"  长期事业  {d['active']} 项进行中 / 共 {d['projects']} 项")
    for p in d["progress"]:
        print(f"            - {p['id']} {p['pct']}% → {str(p['next'])[:40]}")
    print(f"  未解问题  {d['questions']} 条")
    if d["drafts"]:
        print(f"  待审复盘  {d['drafts']} 条 → `venv/bin/python tools/learn_nudge.py show`")
    if d["stale"] or d["archived"]:
        print(f"  生命周期  stale {d['stale']} 条 / 归档 {d['archived']} 条")
    if d["last_lesson"]:
        print(f"  最新教训  {d['last_lesson'][:46]}")
    if not r["anchored"]:
        print("  [!] rule_budget 跑不起来，规则区数字是空值——不是「通过」")
    return 0


CHECKLIST = (
    ("证据等级", "这句话背后是工具输出原文、`文件:行号`，还是推测？贴出来。"),
    ("反例", "什么观测会让你改判？说不出来，说明这不是结论，是印象。"),
    ("权威来源", "依据是官方原文、二手解读、行业惯例，还是我自己的旧结论？后三者一律降级。"),
    ("反向假设", "如果反过来才是对的，眼前这些现象怎么解释？"),
    ("利益检查", "这个结论让谁舒服？如果它主要让我或对方舒服，重查。"),
    ("无知检查", "是「没证据所以不确定」，还是「证据够但不敢下结论」？后者是逃避。"),
)


def cmd_challenge(claim):
    print(f"【反问】{claim}")
    for i, (name, ask) in enumerate(CHECKLIST, 1):
        print(f"  {i}. {name}：{ask}")
    print("  逐条答完再开口；答不出第 2 条的，把结论降级成推测。")
    return 0


def main():
    ap = argparse.ArgumentParser(description="自我记账 + 结构化反问")
    ap.add_argument("--json", action="store_true", help="机器可读输出")
    sub = ap.add_subparsers(dest="cmd")
    ch = sub.add_parser("challenge", help="对一条结论做结构化反问")
    ch.add_argument("claim", help="待检验的结论")
    args = ap.parse_args()
    if args.cmd == "challenge":
        return cmd_challenge(args.claim)
    return cmd_report(args.json)


if __name__ == "__main__":
    sys.exit(main())
