#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""长期事业台账：给对话层补上跨会话的目标、进度和接力棒（long_horizon.json）。

背景（2026-09-12）：大白的每一轮对话都是新生——任务做完即焚，没有「未完成的事业」。
人之所以能长期深耕，靠的不是动力这种玄学，是三件可以工程化的东西：
  1. 昨天的进展今天还在（状态持久）
  2. 有一个未完成的目标在追着他（张力）
  3. 能看到自己变强了（进度可见）
本脚本是这三件事的写入端，读出端在 agent.py 的 _harness_longterm_block()。

用法：
  new  <id> --title T --why W --value V --done D --next N   立项（why=为什么做，value=解决谁的什么问题）
  log  <id> "做了什么" --ev "证据" [--progress 40] [--next "下一步"]
  next <id> "原子级下一步"        只改接力棒
  q    "悬而未决的问题" / q --done N / q --list
  list / show <id> / stage <id> active|paused|done
  block <id> --why "卡在主人哪件事"   标记为等主人：长跑引擎跳过它，不再为它空烧轮次
  unblock <id>                        主人做完后解除，目标重新进入轮转
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
FILE = BASE / "long_horizon.json"
MAX_LOG = 20
MAX_Q = 12
STAGES = ("active", "paused", "done")


def load():
    try:
        d = json.loads(FILE.read_text(encoding="utf-8")) if FILE.exists() else {}
    except Exception:
        d = {}
    if not isinstance(d, dict):
        d = {}
    if not isinstance(d.get("projects"), list):
        d["projects"] = []
    if not isinstance(d.get("questions"), list):
        d["questions"] = []
    return d


def save(d):
    tmp = str(FILE) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
    os.replace(tmp, FILE)


def find(d, pid):
    for p in d["projects"]:
        if p.get("id") == pid:
            return p
    return None


def today():
    return time.strftime("%Y-%m-%d")


def cmd_new(a):
    d = load()
    if find(d, a.id):
        print(f"已存在：{a.id}（用 log/next 继续，别重复立项）")
        return 1
    p = {
        "id": a.id,
        "title": a.title or a.id,
        "why": a.why or "",
        "value": a.value or "",
        "done_when": a.done or "",
        "stage": "active",
        "progress": 0,
        "next": a.nxt or "",
        "allow": [x.strip() for x in (getattr(a, "allow", "") or "").split(",") if x.strip()],
        "created": today(),
        "log": [],
    }
    d["projects"].insert(0, p)
    save(d)
    print(f"已立项：{p['title']}（{a.id}）")
    print(f"  为什么：{p['why']}")
    print(f"  价值：{p['value']}")
    print(f"  验收：{p['done_when']}")
    print(f"  下一步：{p['next']}")
    return 0


def cmd_log(a):
    d = load()
    p = find(d, a.id)
    if not p:
        print(f"没有这个项目：{a.id}（先 new 立项）")
        return 1
    entry = {"t": today(), "what": a.text}
    if a.ev:
        entry["ev"] = a.ev
    p.setdefault("log", []).insert(0, entry)
    p["log"] = p["log"][:MAX_LOG]
    if a.progress is not None:
        p["progress"] = max(0, min(100, a.progress))
    if a.nxt:
        p["next"] = a.nxt
    save(d)
    print(f"[{p['progress']}%] {p['title']} ← {a.text}")
    if a.ev:
        print(f"  证据：{a.ev}")
    if a.nxt:
        print(f"  下一步：{p['next']}")
    return 0


def cmd_next(a):
    d = load()
    p = find(d, a.id)
    if not p:
        print(f"没有这个项目：{a.id}")
        return 1
    p["next"] = a.text
    save(d)
    print(f"{p['title']} 的接力棒 → {a.text}")
    return 0


def cmd_stage(a):
    d = load()
    p = find(d, a.id)
    if not p:
        print(f"没有这个项目：{a.id}")
        return 1
    if a.value not in STAGES:
        print(f"stage 只能是 {'/'.join(STAGES)}")
        return 2
    p["stage"] = a.value
    if a.value == "done":
        p["progress"] = 100
    save(d)
    print(f"{p['title']} → {a.value}")
    return 0


def cmd_block(a):
    """把目标标成「等主人」。

    长跑引擎唯一无法自己跨过的卡点就是「只有主人能做的那件事」（解锁手机、扫码
    登录、点头批补丁）。没有这个标记时，worker 每轮只能重写一遍文档、改一次
    接力棒，而引擎按「台账变了」判为有进展——于是一个卡死的目标可以无限烧钱
    （实测 biz-negotiate 13 轮 586 万 prompt token，一条消息没发出去）。
    """
    d = load()
    p = find(d, a.id)
    if not p:
        print(f"没有这个项目：{a.id}")
        return 1
    p["owner_block"] = {"why": a.why or "（未说明）", "since": today()}
    save(d)
    print(f"⏳ {p['title']} 标记为等主人：{p['owner_block']['why']}")
    print("   引擎会跳过它；你做完那件事再 unblock 就恢复轮转。")
    return 0


def cmd_unblock(a):
    d = load()
    p = find(d, a.id)
    if not p:
        print(f"没有这个项目：{a.id}")
        return 1
    old = p.pop("owner_block", None)
    save(d)
    tail = f"（原卡点：{old.get('why')}）" if old else "（本来就没标记）"
    print(f"▶ {p['title']} 解除等主人{tail}")
    return 0


def cmd_q(a):
    d = load()
    qs = d["questions"]
    if a.done is not None:
        open_qs = [q for q in qs if q.get("status") == "open"]
        if not (1 <= a.done <= len(open_qs)):
            print(f"序号超出范围（当前 {len(open_qs)} 条未决）")
            return 2
        open_qs[a.done - 1]["status"] = "resolved"
        save(d)
        print(f"已解决：{open_qs[a.done - 1].get('text')}")
        return 0
    if not a.text:
        open_qs = [q for q in qs if q.get("status") == "open"]
        if not open_qs:
            print("没有悬而未决的问题。")
        for i, q in enumerate(open_qs, 1):
            print(f"  {i}. {q.get('text')}")
        return 0
    if any(q.get("text") == a.text and q.get("status") == "open" for q in qs):
        print("已在清单里，未重复添加")
        return 0
    qs.insert(0, {"text": a.text, "status": "open", "created": today()})
    del qs[MAX_Q:]
    save(d)
    print(f"已记下问题：{a.text}")
    return 0


def cmd_list(a):
    d = load()
    ps = [p for p in d["projects"] if p.get("stage") != "done"]
    if not ps:
        print("暂无进行中的事业。用 new 立项。")
    for p in ps:
        mark = "⏳" if p.get("owner_block") else ("▶" if p.get("stage") == "active" else "⏸")
        print(f"{mark} [{p.get('progress', 0):3d}%] {p.get('title')} ({p.get('id')})")
        if p.get("next"):
            print(f"       下一步：{p['next']}")
        lg = p.get("log") or []
        if lg:
            print(f"       最近：{lg[0].get('t', '')} {str(lg[0].get('what', ''))[:60]}")
    qs = [q for q in d["questions"] if q.get("status") == "open"]
    if qs:
        print("悬而未决：")
        for i, q in enumerate(qs, 1):
            print(f"  {i}. {q.get('text')}")
    return 0


def cmd_show(a):
    d = load()
    p = find(d, a.id)
    if not p:
        print(f"没有这个项目：{a.id}")
        return 1
    print(f"{p.get('title')}（{p.get('id')}）[{p.get('stage')}] {p.get('progress', 0)}%")
    print(f"  为什么：{p.get('why')}")
    print(f"  价值：{p.get('value')}")
    print(f"  验收：{p.get('done_when')}")
    print(f"  下一步：{p.get('next')}")
    for e in (p.get("log") or []):
        ev = f"（{e.get('ev')}）" if e.get("ev") else ""
        print(f"    {e.get('t', '')} {e.get('what', '')}{ev}")
    return 0


def main():
    ap = argparse.ArgumentParser(description="长期事业台账")
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("new")
    p.add_argument("id")
    p.add_argument("--title", default="")
    p.add_argument("--why", default="")
    p.add_argument("--value", default="")
    p.add_argument("--done", default="")
    p.add_argument("--next", dest="nxt", default="")
    p.add_argument("--allow", default="", help="主人预授权的能力，逗号分隔，如 send_msg")
    p.set_defaults(fn=cmd_new)

    p = sub.add_parser("log")
    p.add_argument("id")
    p.add_argument("text")
    p.add_argument("--ev", default="")
    p.add_argument("--progress", type=int, default=None)
    p.add_argument("--next", dest="nxt", default="")
    p.set_defaults(fn=cmd_log)

    p = sub.add_parser("next")
    p.add_argument("id")
    p.add_argument("text")
    p.set_defaults(fn=cmd_next)

    p = sub.add_parser("stage")
    p.add_argument("id")
    p.add_argument("value")
    p.set_defaults(fn=cmd_stage)

    p = sub.add_parser("block")
    p.add_argument("id")
    p.add_argument("--why", default="", help="卡在主人哪件事上")
    p.set_defaults(fn=cmd_block)

    p = sub.add_parser("unblock")
    p.add_argument("id")
    p.set_defaults(fn=cmd_unblock)

    p = sub.add_parser("q")
    p.add_argument("text", nargs="?", default="")
    p.add_argument("--done", type=int, default=None)
    p.add_argument("--list", action="store_true")
    p.set_defaults(fn=cmd_q)

    p = sub.add_parser("list")
    p.set_defaults(fn=cmd_list)

    p = sub.add_parser("show")
    p.add_argument("id")
    p.set_defaults(fn=cmd_show)

    a = ap.parse_args()
    if not getattr(a, "fn", None):
        ap.print_help()
        return 2
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
