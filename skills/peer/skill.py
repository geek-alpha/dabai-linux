# -*- coding: utf-8 -*-
"""大白联邦技能：跨机器找同伴、留话、看状态。

底层是项目根的 peer_mesh.py —— 地址簿 + 共享密钥 + say/inbox/state 协议。
实例之间走各自的 Cloudflare 域名直连，不经过任何中转服务，所以没有单点。
"""
from __future__ import annotations

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import peer_mesh  # noqa: E402


def _me() -> str:
    return peer_mesh.node_info().get("node_id", "?")


def _facts(r: dict) -> str:
    bits = []
    if r.get("ear"):
        bits.append("可接电话")
    if "load1" in r:
        bits.append(f"负载 {r['load1']}")
    if r.get("mem_avail_mb") and r.get("mem_total_mb"):
        bits.append(f"内存 {r['mem_avail_mb']}/{r['mem_total_mb']}MB")
    if "temp_c" in r:
        bits.append(f"{r['temp_c']}°C")
    if r.get("uptime_s"):
        bits.append(f"开机 {int(r['uptime_s']) // 3600}h")
    return "  ".join(bits)


def peer_list(args: dict) -> str:
    rows = peer_mesh.survey()
    if not rows:
        return (f"我是 {_me()}，地址簿是空的 —— 还没有别的实例接进来。\n"
                f"加同伴：python peer_mesh.py add-peer <名字> <域名>")
    online = [r for r in rows if r.get("online")]
    lines = [f"我是 {_me()}。联邦里 {len(rows)} 个同伴，{len(online)} 个在线："]
    for r in rows:
        if r.get("online"):
            lines.append(f"  ● {r['node_id']}（{r.get('label', '')}）  {_facts(r)}")
        else:
            lines.append(f"  ○ {r['node_id']}  离线 —— {r.get('error', '')}")
    return "\n".join(lines)


def peer_say(args: dict) -> str:
    node = str(args.get("node") or "").strip()
    text = str(args.get("text") or "").strip()
    if not node or not text:
        return "需要 node（目标实例名）和 text（要说的话）。先 peer_list 看有谁。"
    r = peer_mesh.say(node, text)
    if r.get("ok"):
        return f"已送达 {node}。它下次醒来会看到这句话（要它当场回话用 peer_call）。"
    known = r.get("known")
    hint = f"（可用：{', '.join(known)}）" if known else ""
    return f"没能送到 {node}：{r.get('error', '未知错误')}{hint}"


def peer_call(args: dict) -> str:
    node = str(args.get("node") or "").strip()
    text = str(args.get("text") or "").strip()
    if not node or not text:
        return "需要 node（目标实例名）和 text（要说的话）。先 peer_list 看有谁。"
    r = peer_mesh.call_peer(node, text, wait=float(args.get("wait") or 90))
    if not r.get("ok"):
        known = r.get("known")
        hint = f"（可用：{', '.join(known)}）" if known else ""
        return f"打不通 {node}：{r.get('error', '未知错误')}{hint}"
    if r.get("answered"):
        return f"[{r.get('latency_s')}s] {node}：{r.get('reply')}"
    return str(r.get("note") or f"{node} 没接")


def peer_inbox(args: dict) -> str:
    msgs = peer_mesh.my_inbox(mark_read=not args.get("keep_unread"),
                              limit=int(args.get("limit") or 20))
    if not msgs:
        return "收件箱空 —— 没有同伴留言。"
    import time as _t
    lines = [f"{len(msgs)} 条留言："]
    for m in msgs:
        when = _t.strftime("%m-%d %H:%M", _t.localtime(m.get("ts", 0)))
        lines.append(f"  [{when}] {m.get('from', '?')}：{m.get('text', '')}")
    return "\n".join(lines)


def peer_state(args: dict) -> str:
    node = str(args.get("node") or "").strip()
    if not node:
        return "需要 node（目标实例名）。先 peer_list 看有谁。"
    r = peer_mesh.state(node)
    if not r.get("ok"):
        return f"问不到 {node}：{r.get('error', '未知错误')}"
    return f"{node}：{_facts(r) or '（没读到任何指标）'}"


def peer_task(args: dict) -> str:
    """派活：对面起一个后台子智能体真去执行，不是回一句话。

    和 peer_call 的分工：call 要的是「当场一句话」，task 要的是「动手做件事」。
    """
    node = str(args.get("node") or "").strip()
    text = str(args.get("text") or "").strip()
    if not node or not text:
        return "需要 node（目标实例名）和 text（要它干的活）。先 peer_list 看有谁。"
    r = peer_mesh.say(node, text, kind="task", timeout=15.0)
    if not r.get("ok"):
        known = r.get("known")
        hint = f"（可用：{', '.join(known)}）" if known else ""
        return f"没能派给 {node}：{r.get('error', '未知错误')}{hint}"
    t = r.get("task") or {}
    if t.get("ok"):
        return f"已派给 {node}（单号 {t.get('job_id')}）—— 它后台跑，干完把结论回你收件箱。"
    return f"{node} 收到了但没接单：{t.get('error', '未知原因')}"


HANDLERS = {
    "peer_list": peer_list,
    "peer_say": peer_say,
    "peer_call": peer_call,
    "peer_inbox": peer_inbox,
    "peer_state": peer_state,
    "peer_task": peer_task,
}
