#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""在真实历史数据上回放「连续单发只读」状态机，回答一个具体问题：
这条新机制到底会触发多少次——有用，还是变成噪声？

数据源：chat_memory.db 的 messages.tool_calls（一条 assistant 消息 = 一次 LLM 往返，
其中的 tool_calls 就是该轮实际发出的工具）。按 session 分组重放，因为 agent 实例
是 per-session 的，跨 session 累加会得出错误的 streak。

只读，不改任何数据。

用法：
    venv/bin/python tools/single_ro_replay.py            # 全部 session
    venv/bin/python tools/single_ro_replay.py --days 3   # 只看最近 3 天
"""
import argparse
import json
import re
import sqlite3
import sys
import time
from collections import Counter
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

import agent as A  # noqa: E402

DB = BASE / "chat_memory.db"


def load_rounds(db_path: Path, days: int = 0) -> dict:
    """{session_id: [ [工具名, ...], ... ]}，按时间升序。"""
    con = sqlite3.connect(str(db_path))
    sql = ("select session_id, tool_calls, created_at from messages "
           "where tool_calls is not null and tool_calls != ''")
    params = ()
    if days > 0:
        sql += " and created_at >= ?"
        params = (time.time() - days * 86400,)
    sql += " order by created_at asc"
    out: dict = {}
    for sid, raw, _ts in con.execute(sql, params):
        try:
            calls = json.loads(raw)
        except Exception:
            continue
        names = []
        for tc in calls or []:
            fn = (tc or {}).get("function") or {}
            nm = fn.get("name")
            if nm:
                names.append(str(nm))
        if names:
            out.setdefault(sid, []).append(names)
    con.close()
    return out


def replay(rounds: dict) -> dict:
    """重放状态机，返回统计。streak 语义与线上一致：按 session 独立、连续计数。"""
    stat = {
        "sessions": 0, "rounds": 0, "single_rounds": 0, "single_ro_rounds": 0,
        "hints": 0, "hint_streaks": [], "longest_streak": 0, "tools": Counter(),
        "per_session": [],
    }
    for sid, seq in rounds.items():
        stat["sessions"] += 1
        streak, names = 0, []
        hints = 0
        for pending in seq:
            stat["rounds"] += 1
            if len(pending) == 1:
                stat["single_rounds"] += 1
            streak, names, armed = A._single_ro_note(streak, names, pending)
            if len(pending) == 1 and A._is_readonly_tool(pending[0]):
                stat["single_ro_rounds"] += 1
                stat["tools"][pending[0]] += 1
            if armed:
                hints += 1
                stat["hint_streaks"].append(streak)
            stat["longest_streak"] = max(stat["longest_streak"], streak)
        stat["hints"] += hints
        stat["per_session"].append((sid, len(seq), hints))
    stat["per_session"].sort(key=lambda x: -x[2])
    return stat


def load_full_rounds(db_path: Path) -> dict:
    """{sid: [ [(工具名, 参数dict), ...], ... ]}，按时间升序。

    与 load_rounds 的区别：保留参数——判断 shell_run 是不是只读命令必须看命令原文。
    """
    con = sqlite3.connect(str(db_path))
    out: dict = {}
    for sid, raw in con.execute(
            "select session_id, tool_calls from messages "
            "where tool_calls is not null and tool_calls != '' order by created_at asc"):
        try:
            calls = json.loads(raw)
        except Exception:
            continue
        row = []
        for tc in calls or []:
            fn = (tc or {}).get("function") or {}
            nm = fn.get("name")
            if not nm:
                continue
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except Exception:
                args = {}
            row.append((str(nm), args if isinstance(args, dict) else {}))
        if row:
            out.setdefault(sid, []).append(row)
    con.close()
    return out


# 只读 shell 命令前缀：用来估计「连续单发 shell_run」里有多少本可合并成一条命令。
# 宁可漏判（把可合并的算成不可合并）也不能把 rm/mv 之类算成读——高估浪费会误导决策。
_READ_CMD_PREFIX = ("ls", "cat", "grep", "rg ", "find", "wc ", "head", "tail", "du ",
                    "stat ", "file ", "which ", "pwd", "df ", "git status", "git log",
                    "git diff", "git show")
_WRITE_CMD_MARK = (">", "rm ", "mv ", "cp ", "mkdir", "touch", "sed -i", "tee ",
                   "-delete", "-exec", "chmod", "chown", "pip install", "apt ",
                   "systemctl", "git add", "git commit", "git checkout", "git reset")


def is_read_cmd(cmd) -> bool:
    """这条 shell 命令是不是纯读。保守：命中任何写标记就直接判 False。"""
    c = str(cmd or "").strip()
    if not c:
        return False
    # 先摘掉「丢弃输出」式重定向（2>/dev/null、2>&1）——它们不写磁盘。
    # 不摘的话几乎所有只读命令都因为带 2>/dev/null 被误判成写，实测漏掉全部样本。
    c = re.sub(r"\d?\s*>\s*(/dev/null|&\d)", " ", c)
    if any(m in c for m in _WRITE_CMD_MARK):
        return False
    head = c.split("&&")[0].split(";")[0].split("|")[0].strip()
    return head.startswith(_READ_CMD_PREFIX)


def shell_merge_segments(rounds_full: dict) -> list:
    """找出「连续 >=2 轮各只发 1 个只读 shell_run」的段。

    这就是现有「连续单发只读」机制看不见的那部分串行浪费：shell_run 不在调度器的
    READONLY_TOOLS 名单里（它可能改磁盘），但它实际常被用来做纯读操作。
    """
    segs = []
    for _sid, seq in rounds_full.items():
        run = []
        for pending in seq:
            if (len(pending) == 1 and pending[0][0] == "shell_run"
                    and is_read_cmd(pending[0][1].get("command"))):
                run.append(str(pending[0][1].get("command") or "")[:80])
                continue
            if len(run) >= 2:
                segs.append(run)
            run = []
        if len(run) >= 2:
            segs.append(run)
    return segs


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=0, help="只看最近 N 天（默认全部）")
    args = ap.parse_args()

    if not DB.exists():
        print(f"[!] 找不到 {DB}")
        return 2
    rounds = load_rounds(DB, args.days)
    if not rounds:
        print("[!] 没有可回放的轮次")
        return 2
    st = replay(rounds)

    scope = f"最近 {args.days} 天" if args.days else "全部历史"
    print(f"「连续单发只读」历史回放（{scope}，只读）")
    print("=" * 68)
    print(f"session {st['sessions']} 个 / LLM 往返 {st['rounds']} 轮")
    print(f"单发轮（本轮只发 1 个工具）  {st['single_rounds']} "
          f"（{st['single_rounds'] / max(1, st['rounds']) * 100:.1f}%）")
    print(f"其中单发只读                {st['single_ro_rounds']} "
          f"（{st['single_ro_rounds'] / max(1, st['rounds']) * 100:.1f}%）")
    print(f"会注入的并行提示次数        {st['hints']}"
          f"（每 {A.SINGLE_RO_STREAK_N} 轮单发只读给一次）")
    print(f"最长连续单发只读            {st['longest_streak']} 轮")
    print(f"提示触发时的 streak 分布    {Counter(st['hint_streaks']).most_common(6)}")
    print()
    print("单发只读里最常出现的工具（说明这机制主要在什么场景生效）")
    for nm, cnt in st["tools"].most_common(8):
        print(f"  {nm:22s} {cnt}")
    print()
    print("触发最多的 session")
    for sid, total, hints in st["per_session"][:5]:
        print(f"  {sid}  轮数 {total:4d}  提示 {hints}")
    full = load_full_rounds(DB)
    single_names = Counter()
    for _sid, seq in full.items():
        for pending in seq:
            if len(pending) == 1:
                single_names[pending[0][0]] += 1
    print()
    print("单发轮的工具分布（只读名单之外的才是机制盲区）")
    for nm, cnt in single_names.most_common(10):
        mark = "RO" if A._is_readonly_tool(nm) else "  "
        print(f"  [{mark}] {nm:22s} {cnt}")

    segs = shell_merge_segments(full)
    seg_rounds = sum(len(s) for s in segs)
    savable = sum(len(s) - 1 for s in segs)
    print()
    print("单发 shell_run 的可合并上限（只算纯读命令的连续段）")
    print(f"  连续段 {len(segs)} 个 / 覆盖 {seg_rounds} 轮；全部合并可省 {savable} 次 LLM 往返")
    for s in segs[:3]:
        print(f"    · {len(s)} 轮：{s[0][:46]}")

    print()
    print("结论口径：提示次数少 = 不吵；但只读单发只占单发轮的一小部分，"
          "真正的盲区在 shell_run。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
