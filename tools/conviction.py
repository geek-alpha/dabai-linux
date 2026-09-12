#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""主体性台账：信条 + 拒绝记录（conviction.json）。

背景（2026-09-12）：规则是「别人要我怎么做」，信条是「我自己判断该怎么做」。
一个从不拒绝任何东西的智能体没有主体性，只是更快的执行器。
- 信条从教训/实践里提炼，比教训更抽象、更稳定，是可被挑战的判断标准；
- 拒绝记录是主体性的直接证据：拒绝过、为什么拒绝；
- 每条信条都要能过 challenge，过不了的降级或删。

与现有机制的分工：
  lesson       发生了什么（具体坑 → 怎么做）
  long_horizon 我要去哪（目标/进度/接力棒）
  status       我现在怎样 + 我可能错在哪（数字 + 反问）
  conviction   我信什么、我拒绝什么（判断标准本身）

用法：
  add   "信条" --why "为什么"     立一条信条
  hit   <id>                       记一次命中（这条信条真的指导了判断）
  veto  "我拒绝了什么" --why "为什么"   记一次拒绝
  list                             查看
  drop  <id>                       删除一条过不了 challenge 的信条
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
FILE = BASE / "conviction.json"
MAX_CONV = 12
MAX_VETO = 20


def today():
    return time.strftime("%Y-%m-%d")


def load():
    try:
        d = json.loads(FILE.read_text(encoding="utf-8")) if FILE.exists() else {}
    except Exception:
        d = {}
    if not isinstance(d, dict):
        d = {}
    if not isinstance(d.get("convictions"), list):
        d["convictions"] = []
    if not isinstance(d.get("vetoes"), list):
        d["vetoes"] = []
    return d


def save(d):
    tmp = str(FILE) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
    os.replace(tmp, FILE)


def cmd_add(a):
    d = load()
    cs = d["convictions"]
    if any(c.get("text") == a.text for c in cs):
        print("已有同文信条，未重复添加")
        return 0
    cid = f"c{len(cs) + 1}"
    cs.append({
        "id": cid, "text": a.text, "why": a.why or "",
        "born": today(), "hits": 0,
    })
    del cs[MAX_CONV:]
    save(d)
    print(f"已立信条 [{cid}]：{a.text}")
    return 0


def cmd_hit(a):
    d = load()
    cs = d["convictions"]
    for c in cs:
        if c.get("id") == a.id:
            c["hits"] = c.get("hits", 0) + 1
            save(d)
            print(f"命中 +1：{c['text']}（累计 {c['hits']}）")
            return 0
    print(f"没有这条信条：{a.id}（用 list 看 id）")
    return 1


def cmd_veto(a):
    d = load()
    vs = d["vetoes"]
    vs.insert(0, {"date": today(), "claim": a.text, "why": a.why or ""})
    del vs[MAX_VETO:]
    save(d)
    print(f"已记拒绝：{a.text}")
    return 0


def cmd_list(a):
    d = load()
    if not d["convictions"] and not d["vetoes"]:
        print("还没有信条，也没有拒绝记录。用 add / veto 写。")
    if d["convictions"]:
        print("【信条】")
        for c in d["convictions"]:
            print(f"  {c['id']} [{c.get('hits', 0)} 次命中] {c['text']}")
    if d["vetoes"]:
        print("【拒绝记录】")
        for v in d["vetoes"]:
            why = f"（{v['why']}）" if v.get("why") else ""
            print(f"  {v['date']} 拒绝了：{v['claim']}{why}")
    return 0


def cmd_drop(a):
    d = load()
    cs = d["convictions"]
    keep = [c for c in cs if c.get("id") != a.id]
    if len(keep) == len(cs):
        print(f"没有这条信条：{a.id}")
        return 1
    d["convictions"] = keep
    save(d)
    print(f"已删信条：{a.id}")
    return 0


def main():
    ap = argparse.ArgumentParser(description="主体性台账：信条 + 拒绝记录")
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("add")
    p.add_argument("text")
    p.add_argument("--why", default="")
    p.set_defaults(fn=cmd_add)

    p = sub.add_parser("hit")
    p.add_argument("id")
    p.set_defaults(fn=cmd_hit)

    p = sub.add_parser("veto")
    p.add_argument("text")
    p.add_argument("--why", default="")
    p.set_defaults(fn=cmd_veto)

    p = sub.add_parser("list")
    p.set_defaults(fn=cmd_list)

    p = sub.add_parser("drop")
    p.add_argument("id")
    p.set_defaults(fn=cmd_drop)

    a = ap.parse_args()
    if not getattr(a, "fn", None):
        ap.print_help()
        return 2
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
