#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把一条经验写进 harness 经验库（harness_task_memory.json）。

背景（2026-09-12）：harness/tasks.py:732 的 _remember_lesson 只在 flow/batch 任务
终态被调用，对话层的错误无从沉淀——rg 全项目除 tasks.py 自身零调用，
harness_task_memory.json 全盘不存在，这台学习机从未通电。本脚本给它一个对话层入口。

用法：python tools/lesson_add.py "教训文本"
"""
import json
import os
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
FILE = BASE / "harness_task_memory.json"
MAX_LESSONS = 60


def main() -> int:
    text = " ".join(sys.argv[1:]).strip()
    if not text:
        print('用法：python tools/lesson_add.py "教训文本"')
        return 2
    try:
        data = json.loads(FILE.read_text(encoding="utf-8")) if FILE.exists() else {}
    except Exception:
        data = {}
    ls = data.get("lessons") if isinstance(data, dict) else None
    ls = [str(x) for x in ls] if isinstance(ls, list) else []
    if text in ls:
        print(f"已存在（第 {ls.index(text) + 1} 条），未重复写入")
        return 0
    ls.insert(0, text)
    ls = ls[:MAX_LESSONS]
    tmp = str(FILE) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"lessons": ls}, f, ensure_ascii=False, indent=1)
    os.replace(tmp, FILE)
    print(f"已记录（共 {len(ls)} 条）：{text[:80]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
