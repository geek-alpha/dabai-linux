#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回合结束的复盘触发器：计数器到点就把「这轮值不值得写教训」落成一条可审草稿。

背景（2026-09-20）：经验库的写入端（tools/lesson_add.py）只能靠自觉调用——对话层
没有任何东西在回合结束时提醒「刚才那轮踩坑了」，学习机通不通电取决于当轮心情。
对照 hermes-agent/agent/turn_finalizer.py:629-657：它用 _iters_since_skill（工具迭代数）
与 _turns_since_memory（用户轮数）双计数器，在回复投递之后才触发复盘，整段包在
suppress(Exception) 里——复盘失败不许影响一次对话的交付。

本模块只做三件事：记账、到点落草稿、失败静默。草稿不是教训：它不进 prompt，要人过
一眼再用 lesson_add.py 转正。自动生成的「教训」正是那个脚本的闸门要拦的东西。

用法：
  learn_nudge.py show     列待审草稿
  learn_nudge.py done 3   把第 3 条标成已处理
"""
import json
import os
import re
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
STATE = BASE / "data" / "learn_nudge.json"
DRAFTS = BASE / "data" / "learn_drafts.json"
ITER_INTERVAL = 15  # 工具迭代数阈值（对齐 hermes 的 _skill_nudge_interval）
TURN_INTERVAL = 10  # 用户轮数阈值（对齐 hermes 的 _memory_nudge_interval）
MAX_DRAFTS = 50

# 用户纠正也是信号，而且常常不带工具报错：那类坑只有从这句话里才看得出来。
_CORRECTION = re.compile(r"不对|错了|不是这样|垃圾|没用|还是不行|怎么又|为什么还|白改|又坏")


def _load(path, default):
    try:
        v = json.loads(path.read_text(encoding="utf-8"))
        return v if isinstance(v, type(default)) else default
    except Exception:
        return default


def _save(path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = str(path) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def _signal(tool_errors, err_names, user_msg) -> bool:
    """本轮有没有值得复盘的信号：报错、报错工具名、用户纠正。

    没信号就不落草稿——每 10 轮无条件产一条「没事发生」，50 条上限很快被垃圾填满，
    待审列表变成噪音，人就不看了，等于没通电。
    """
    return bool(int(tool_errors or 0) or list(err_names or []) or _CORRECTION.search(str(user_msg or "")))


def tick(tool_rounds=0, tool_calls=0, tool_errors=0, err_names=None,
         user_msg="", reply="", now=None):
    """回合结束调用一次，返回落下的草稿 dict（到点且有信号）或 None。

    调用方必须吞异常：复盘触发器不该有能力弄坏一次对话。
    """
    st = _load(STATE, {})
    st["iters"] = int(st.get("iters", 0) or 0) + max(0, int(tool_rounds or 0))
    st["turns"] = int(st.get("turns", 0) or 0) + 1
    st["last_turn_at"] = round(float(now if now is not None else time.time()), 1)
    due_iter = st["iters"] >= ITER_INTERVAL
    due_turn = st["turns"] >= TURN_INTERVAL
    if not (due_iter or due_turn):
        _save(STATE, st)
        return None
    if not _signal(tool_errors, err_names, user_msg):
        st["skipped"] = int(st.get("skipped", 0) or 0) + 1
        st["iters"] = 0
        st["turns"] = 0
        _save(STATE, st)
        return None
    draft = {
        "at": st["last_turn_at"],
        "due": "iters" if due_iter else "turns",
        "iters": st["iters"],
        "turns": st["turns"],
        "tool_calls": int(tool_calls or 0),
        "tool_errors": int(tool_errors or 0),
        "err_names": [str(x) for x in (err_names or [])][:8],
        "user_msg": str(user_msg or "")[:200],
        "reply": str(reply or "")[:200],
        "status": "pending",
    }
    ds = _load(DRAFTS, [])
    ds.append(draft)
    _save(DRAFTS, ds[-MAX_DRAFTS:])
    st["iters"] = 0
    st["turns"] = 0
    _save(STATE, st)
    return draft


def drafts() -> list:
    return _load(DRAFTS, [])


def pending() -> list:
    return [d for d in drafts() if isinstance(d, dict) and d.get("status") == "pending"]


def mark_done(idx: int) -> bool:
    """按 show 里显示的 1-based 序号标记已处理。"""
    ds = drafts()
    pends = [d for d in ds if d.get("status") == "pending"]
    if idx < 1 or idx > len(pends):
        return False
    pends[idx - 1]["status"] = "done"
    _save(DRAFTS, ds)
    return True


def cmd_show() -> int:
    pends = pending()
    st = _load(STATE, {})
    print(f"【待审复盘草稿】{len(pends)} 条（阈值：迭代 {ITER_INTERVAL} / 轮数 {TURN_INTERVAL}）")
    print(f"  当前计数  迭代 {st.get('iters', 0)} / 轮数 {st.get('turns', 0)}"
          f"；无信号跳过 {st.get('skipped', 0)} 次")
    for i, d in enumerate(pends, 1):
        print(f"  {i}. [{time.strftime('%m-%d %H:%M', time.localtime(d.get('at', 0)))}]"
              f" 迭代{d.get('iters')} 轮数{d.get('turns')} 报错{d.get('tool_errors')}"
              f" {','.join(d.get('err_names') or []) or '-'}")
        print(f"     用户：{d.get('user_msg', '')[:90]}")
        print(f"     回复：{d.get('reply', '')[:90]}")
    if pends:
        print("  转正：`venv/bin/python tools/lesson_add.py \"...\"`；处理完标掉：`tools/learn_nudge.py done N`")
    return 0


def main() -> int:
    args = sys.argv[1:]
    if not args or args[0] == "show":
        return cmd_show()
    if args[0] == "done" and len(args) > 1:
        try:
            ok = mark_done(int(args[1]))
        except ValueError:
            ok = False
        print("已标记" if ok else "序号不存在")
        return 0 if ok else 2
    print(__doc__.strip().splitlines()[-2].strip())
    return 2


if __name__ == "__main__":
    sys.exit(main())
