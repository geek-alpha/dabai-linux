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
import json
import re
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
AGENT = BASE / "agent.py"
LESSONS = BASE / "harness_task_memory.json"
HORIZON = BASE / "long_horizon.json"

RULE_START = "【工作准则（任何模式下"
RULE_END = "shell 输出不许用"
RULE_WARN = 3000
STR_LIT = re.compile(r'"((?:[^"\\]|\\.)*)"')


def _load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def rule_stats():
    """统计 sys_prompt 规则区的字符数与条款数；锚点失配则退回全文统计。"""
    try:
        lines = AGENT.read_text(encoding="utf-8").splitlines()
    except Exception:
        return {"chars": 0, "clauses": 0, "anchored": False}
    start = end = None
    for i, line in enumerate(lines):
        if start is None and RULE_START in line:
            start = i
        if start is not None and RULE_END in line:
            end = i
            break
    anchored = start is not None and end is not None
    text = "\n".join(lines[start:end + 1] if anchored else lines)
    return {
        "chars": sum(len(m) for m in STR_LIT.findall(text)),
        "clauses": len(re.findall(r"【[^】]{2,20}】", text)),
        "anchored": anchored,
    }


def lessons():
    data = _load_json(LESSONS, {})
    got = data.get("lessons") if isinstance(data, dict) else None
    return got if isinstance(got, list) else []


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
    over = "  ← 超警告线，该审计合并了" if r["chars"] > RULE_WARN else ""
    print("【自我记账】")
    print(f"  规则区    {r['chars']} 字符 / {r['clauses']} 条（警告线 {RULE_WARN}）{over}")
    print(f"  经验库    {d['lessons']} 条")
    print(f"  长期事业  {d['active']} 项进行中 / 共 {d['projects']} 项")
    for p in d["progress"]:
        print(f"            - {p['id']} {p['pct']}% → {str(p['next'])[:40]}")
    print(f"  未解问题  {d['questions']} 条")
    if d["last_lesson"]:
        print(f"  最新教训  {d['last_lesson'][:46]}")
    if not r["anchored"]:
        print("  [!] 规则区锚点未命中，上面数字是全文统计")
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
