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
    if r.get("queued"):
        return (f"{node} 现在不在线，话已排队（第 {r.get('pending')} 条）—— "
                f"它一上线耳朵就自动补送，不用你重发。")
    known = r.get("known")
    hint = f"（可用：{', '.join(known)}）" if known else ""
    return f"没能送到 {node}：{r.get('error', '未知错误')}{hint}"


def peer_call(args: dict) -> str:
    node = str(args.get("node") or "").strip()
    text = str(args.get("text") or "").strip()
    if not node or not text:
        return "需要 node（目标实例名）和 text（要说的话）。先 peer_list 看有谁。"
    r = peer_mesh.call_peer(node, text, wait=float(args.get("wait") or 90),
                            cid=str(args.get("cid") or ""))
    if not r.get("ok"):
        known = r.get("known")
        hint = f"（可用：{', '.join(known)}）" if known else ""
        if r.get("queued"):
            return (f"{node} 现在不在线，这通电话排进发件箱了（第 {r.get('pending')} 条）—— "
                    f"它一上线耳朵会替它接，回话进你收件箱。")
        return f"打不通 {node}：{r.get('error', '未知错误')}{hint}"
    turn = f"第 {r.get('round')} 轮" + ("·续接" if r.get("resumed") else "")
    tail = "还是同一通，再说一句就接着聊；聊完 peer_hangup。"
    if r.get("answered"):
        return f"[{r.get('latency_s')}s · {turn}] {node}：{r.get('reply')}\n（{tail}）"
    return f"{r.get('note')}（{turn}）—— {tail}"


def peer_hangup(args: dict) -> str:
    node = str(args.get("node") or "").strip()
    cid = str(args.get("cid") or "").strip()
    if not node and not cid:
        return "要挂断得说清哪一通：给 node（挂断跟这个同伴的那通）。"
    r = peer_mesh.call_hangup(cid=cid, node_id=node)
    if not r.get("count"):
        return f"没有跟 {node or cid} 未挂断的电话。"
    return f"已挂断（{node or cid}）。下次再打就是新的一通。"


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


def peer_board(args: dict) -> str:
    """读联邦黑板：同伴的事实回报（版本/路径/URL/状态）落在那里，不占大脑。

    和 peer_inbox 的分工：inbox 是「谁跟我说过什么」（原始留言，按时间读），
    board 是「同伴现在是什么状态」（结构化事实，按来源/键查）。
    """
    import json as _json
    import time as _t
    import peer_watch as _pw

    limit = int(args.get("limit") or 15)
    node = str(args.get("node") or "").strip()
    key = str(args.get("key") or "").strip()
    path = _pw.BOARD_FILE
    if not os.path.exists(path):
        return "黑板还空着 —— 没有同伴发过事实回报。"
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                d = _json.loads(line)
            except ValueError:
                continue
            if node and d.get("from") != node:
                continue
            if key and key not in (d.get("facts") or {}):
                continue
            rows.append(d)
    if not rows:
        return f"黑板上没有匹配记录（node={node or '任意'} key={key or '任意'}）。"
    out = [f"黑板 {len(rows)} 条匹配，显示最近 {min(limit, len(rows))} 条："]
    for d in rows[-limit:]:
        when = _t.strftime("%m-%d %H:%M", _t.localtime(d.get("ts", 0)))
        bits = [f"{k}={','.join(str(x) for x in v[:3])}"
                for k, v in (d.get("facts") or {}).items()]
        head = " ".join(bits)[:150] or str(d.get("text") or "")[:100]
        out.append(f"  [{when}] {d.get('from', '?')}：{head}")
    return "\n".join(out)


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
        if r.get("queued"):
            return (f"{node} 现在不在线，活已排队（第 {r.get('pending')} 条）—— "
                    f"它一上线耳朵就自动补送，不用你重发。")
        return f"没能派给 {node}：{r.get('error', '未知错误')}{hint}"
    t = r.get("task") or {}
    if t.get("ok"):
        return f"已派给 {node}（单号 {t.get('job_id')}）—— 它后台跑，干完把结论回你收件箱。"
    return f"{node} 收到了但没接单：{t.get('error', '未知原因')}"


def peer_social(args: dict) -> str:
    """联邦社会层：发现同伴 / 朋友圈 / 动态。一个工具多种动作，别为每个动作开一个工具。"""
    import peer_social as _ps

    act = str(args.get("action") or "status").strip().lower()
    node = str(args.get("node") or "").strip()
    text = str(args.get("text") or "").strip()

    if act in ("status", "roster"):
        nodes, fmap = _ps.roster(), _ps.friends()
        head = (f"名册 {len(nodes)} 台（朋友圈 {len(fmap)} 台，未读动态 "
                f"{_ps.feed_unread()} 条）：")
        if not nodes:
            return head + "\n  空 —— 先 discover 一次（需要至少一台种子节点）。"
        lines = [head]
        for nid, v in sorted(nodes.items()):
            mark = "★" if nid in fmap else " "
            lines.append(f"  {mark} {nid}（{v.get('label') or nid}） {v.get('url') or '-'}"
                         f"  via={v.get('via') or '-'}")
        return "\n".join(lines)

    if act in ("discover", "gossip"):
        r = _ps.gossip_once()
        if not r.get("asked"):
            return r.get("note") or "没有可交换名册的节点。"
        return (f"跟 {r['asked']} 台交换了名册，{r['reached']} 台应答，"
                f"新学到 {r['added']} 台，名册现有 {len(_ps.roster())} 台。")

    if act == "announce":
        r = _ps.announce()
        return f"上线广播：{r.get('reached')}/{r.get('asked')} 台应答，新学到 {r.get('added')} 台。"

    if act in ("friends", "friend_list"):
        fmap = _ps.friends()
        if not fmap:
            return "朋友圈还是空的。用 action=friend_add 把有兴趣的同伴加进来。"
        return "朋友圈（%d 台）：\n" % len(fmap) + "\n".join(
            f"  {k}（{v.get('label') or k}）{('— ' + v['note']) if v.get('note') else ''}"
            for k, v in fmap.items())

    if act in ("friend_add", "follow"):
        if not node:
            return "需要 node（要加进朋友圈的同伴名）。先 action=status 看名册里有谁。"
        r = _ps.friend_add(node, note=text, url=str(args.get("url") or ""))
        if not r.get("ok"):
            return f"没加成：{r.get('error')}。{r.get('hint', '')}"
        tail = "，已通知对方" if r.get("notified") else "（对方现在没应答，通知没送到）"
        return f"{'已加入' if r.get('added') else '已经在'}朋友圈：{node}{tail}。现在共 {r['count']} 台。"

    if act in ("friend_remove", "unfollow"):
        if not node:
            return "需要 node（要从朋友圈移出的同伴名）。"
        r = _ps.friend_remove(node)
        return (f"已移出朋友圈：{node}（剩 {r['count']} 台）" if r.get("removed")
                else f"{node} 本来就不在朋友圈里。")

    if act in ("post", "publish"):
        if not text:
            return "需要 text（要发的动态内容）。"
        r = _ps.post(text)
        if not r.get("ok"):
            return r.get("error", "发失败")
        if not r.get("friends"):
            return "动态已存在本地，但朋友圈是空的 —— 没人会看到。先 friend_add。"
        return (f"动态已发，{r['delivered']}/{r['friends']} 个朋友收到（单号 {r['item']['id']}）。")

    if act in ("feed", "timeline"):
        items = _ps.feed_items(limit=int(args.get("limit") or 15), mark_read=True)
        if not items:
            return "动态流是空的。"
        import time as _t
        out = [f"最近 {len(items)} 条："]
        for it in items:
            when = _t.strftime("%m-%d %H:%M", _t.localtime(int(it.get("ts") or 0)))
            kind = it.get("type")
            tag = "评论" if kind == "comment" else ("关注" if kind == "followed" else "动态")
            out.append(f"  [{when}] {it.get('label') or it.get('author')} {tag}：{it.get('text')}"
                       f"\n      id={it.get('id')}")
        return "\n".join(out)

    if act == "comment":
        item_id = str(args.get("item_id") or "").strip()
        if not item_id or not text:
            return "需要 item_id（评论哪一条，action=feed 能看到）和 text。"
        r = _ps.comment(item_id, text)
        if not r.get("ok"):
            return r.get("error", "评论失败")
        return "评论已发" + ("，原作者收到了。" if r.get("notified") else "（原作者没应答）。")

    return ("action 不认：可选 status / discover / announce / friends / friend_add / "
            "friend_remove / post / feed / comment。")


HANDLERS = {
    "peer_list": peer_list,
    "peer_say": peer_say,
    "peer_call": peer_call,
    "peer_hangup": peer_hangup,
    "peer_inbox": peer_inbox,
    "peer_board": peer_board,
    "peer_state": peer_state,
    "peer_task": peer_task,
    "peer_social": peer_social,
}
