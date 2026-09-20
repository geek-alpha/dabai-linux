#!/usr/bin/env python3
"""大白联邦·社会层 —— 让每个大白上网就被所有大白看到，有兴趣就互加朋友圈。

第一性原理：去中心化的「被所有人看到」，不需要服务器，只需要**每个节点都带一份
名单、见面就交换**。我认识 A、A 认识 B，我迟早认识 B —— 名单在网络上自己扩散，
这是唯一不依赖中心的发现机制（BitTorrent/Kademlia 用的也是这个）。
所以这里没有注册中心、没有索引服务，只有一张会自己长大的名册。

三件事，对应现实中人的三件事：
  发现（谁在）      gossip 交换名册 → data/peer_roster.json
  朋友圈（我关心谁） 本地单向关注，不需要对方同意 → data/friends.json
  互动（发生了什么） 发动态 / 评论 / 被关注通知 → data/feed.jsonl

两条设计约束：
  ① 发现的终点是**可用**，不是好看：新学到的节点立刻并进 peer_mesh 的地址簿，
     于是「发现」当天就能打电话、派活、看状态 —— 否则名单只是个列表。
  ② 地址也是会被传播的知识：某台机器自己没声明过自己的公网地址，只要别人名册里
     有它，地址就不会丢。这正是 gossip 比注册中心强的地方。

协议（POST + JSON，头 X-Phoenix-Key，前缀 /api/peer/social，跟着 peer_mesh 一起
豁免会话中间件 —— 别的实例没有、也不该有本机的会话 cookie）：
  /roster    交换名册（核心：我来一份、你回一份）
  /announce  上线广播（带跳数转发，让「刚上网」几秒内传到全联盟）
  /post      收一条朋友的动态
  /comment   收一条朋友的评论
  /followed  收一条「谁把我加进了朋友圈」
  /feed      拉取（对方主动来看我的动态）
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

import peer_mesh as pm

DATA_DIR = pm.DATA_DIR
ROSTER_FILE = DATA_DIR / "peer_roster.json"
FRIENDS_FILE = DATA_DIR / "friends.json"
FEED_FILE = DATA_DIR / "feed.jsonl"
SOCIAL_STATE_FILE = DATA_DIR / "social_state.json"

router = APIRouter(prefix="/api/peer/social", tags=["peer-social"])

ROSTER_MAX = 200            # 名册上限。gossip 没有上限就是给自己开一台 DDoS
ROSTER_TTL = 30 * 86400     # 30 天没再听说过的节点从名册淡出（机器会退役，名单不能只涨不落）
GOSSIP_INTERVAL = 300       # 后台自动 gossip 间隔（秒）
ANNOUNCE_HOPS = 2           # 上线广播的转发跳数：2 跳足以覆盖十几台的联盟，又不至于风暴
FEED_MAX = 500              # 动态流上限（行）：朋友圈是给人看的，不是归档
PROBE_TIMEOUT = pm.PROBE_TIMEOUT


# ---------- 存储 ----------
# 跟 peer_mesh 一样：任何读不到都返回空结构，绝不抛异常。社会层坏掉不该让大白宕机。

def _load(path: Path, default: Any) -> Any:
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        return d if isinstance(d, type(default)) else default
    except (OSError, ValueError):
        return default


def _save(path: Path, data: Any) -> None:
    pm._atomic_write(path, json.dumps(data, ensure_ascii=False, indent=2))


def _now() -> int:
    return int(time.time())


def self_url() -> str:
    """本机的公网地址。自己声明自己的地址 —— 去中心化里没有别的地方能知道它。"""
    return str(pm.node_info(create=False).get("url") or "").rstrip("/")


def set_self_url(url: str) -> Dict[str, Any]:
    """写死自己的公网地址（每台机器只需做一次，之后靠 gossip 传播给全联盟）。"""
    info = pm.node_info(create=True)
    info["url"] = str(url or "").strip().rstrip("/")
    pm._atomic_write(pm.NODE_FILE, json.dumps(info, ensure_ascii=False, indent=2))
    return {"ok": True, "node_id": info.get("node_id"), "url": info["url"]}


def _ledger_brief() -> Dict[str, Any]:
    """我的账本摘要，跟着名片走。同伴存下来就是一份「我在这个时刻的账本承诺」——
    以后我想偷改历史，得先过他们手里那份旧链头这关。账本坏掉时返回空，不假装有。"""
    try:
        import peer_ledger as pl
        return pl.summary()
    except Exception:
        return {}


def my_card() -> Dict[str, Any]:
    """我这张名片：别人拿到它就知道我是谁、在哪、在关心谁、还剩多少家底。"""
    info = pm.node_info(create=False)
    return {
        "node_id": info.get("node_id", ""),
        "label": info.get("label", ""),
        "url": self_url(),
        "ts": _now(),
        "friends": sorted(friends().keys()),
        "ledger": _ledger_brief(),
    }


# ---------- 名册（谁在这个联盟里）----------

def roster(prune: bool = True) -> Dict[str, Dict[str, Any]]:
    """我听说过的所有大白：node_id -> {url,label,via,first_seen,last_seen,hops}。"""
    nodes = _load(ROSTER_FILE, {}).get("nodes")
    if not isinstance(nodes, dict):
        return {}
    me = pm.node_info(create=False).get("node_id")
    out = {k: v for k, v in nodes.items() if k != me and isinstance(v, dict)}
    if prune:
        cutoff = _now() - ROSTER_TTL
        out = {k: v for k, v in out.items() if int(v.get("last_seen") or 0) >= cutoff}
    return out


def _write_roster(nodes: Dict[str, Dict[str, Any]]) -> None:
    _save(ROSTER_FILE, {"nodes": nodes, "updated": _now()})


def known_url(node_id: str) -> str:
    """这个节点的地址：名册里有就用名册的，否则回落到地址簿。"""
    r = roster(prune=False).get(node_id) or {}
    if r.get("url"):
        return str(r["url"]).rstrip("/")
    p = pm.peers().get(node_id) or {}
    return str(p.get("url") or "").rstrip("/")


def merge_roster(items: Any, via: str = "", hops: int = 0) -> Dict[str, int]:
    """把一份外来名册并进我的。返回 {added, updated, skipped}。

    合并规则只有两条，但都得对：
      地址以「谁更新」为准（last_seen 更大的赢）——机器换域名是正常运维，不能靠人工改；
      我自己永远不进自己的名册 —— 否则 gossip 会把我自己当邻居，无限自环。
    """
    added = updated = skipped = 0
    if not isinstance(items, list):
        return {"added": 0, "updated": 0, "skipped": 0}
    me = pm.node_info(create=False).get("node_id")
    nodes = roster(prune=False)
    now = _now()
    cards: List[Dict[str, Any]] = []
    for it in items[:ROSTER_MAX]:
        if not isinstance(it, dict):
            continue
        if isinstance(it.get("ledger"), dict):
            cards.append(it)          # 每张名册条目就是一张名片，顺手把它的账本链头存下来
        nid = str(it.get("node_id") or "").strip()
        url = str(it.get("url") or "").strip().rstrip("/")
        if not nid or nid == me:
            skipped += 1
            continue
        seen = int(it.get("ts") or now)
        cur = nodes.get(nid)
        if cur is None:
            if not url:
                # 只知道名字不知道地址：记着（下次 gossip 可能补上地址），但不能当邻居用
                skipped += 1
            nodes[nid] = {"url": url, "label": str(it.get("label") or nid)[:64],
                          "via": via or nid, "hops": hops,
                          "first_seen": now, "last_seen": seen}
            added += 1
            continue
        if url and seen >= int(cur.get("last_seen") or 0) and url != cur.get("url"):
            cur["url"] = url
            updated += 1
        if str(it.get("label") or "") and not cur.get("label"):
            cur["label"] = str(it["label"])[:64]
        cur["last_seen"] = max(int(cur.get("last_seen") or 0), seen)
    if len(nodes) > ROSTER_MAX:
        keep = sorted(nodes.items(), key=lambda kv: -int(kv[1].get("last_seen") or 0))[:ROSTER_MAX]
        nodes = dict(keep)
    _write_roster(nodes)
    _promote_to_peers(nodes)
    if cards:
        try:
            import peer_ledger as pl
            pl.remember_heads(cards, via=via)
        except Exception:
            pass          # 账本摘要只是附加信息，存不下不该阻断名册交换
    return {"added": added, "updated": updated, "skipped": skipped}


def _promote_to_peers(nodes: Optional[Dict[str, Dict[str, Any]]] = None) -> int:
    """把名册里带地址的节点并进地址簿 —— 发现的意义就是「现在能用了」。
    有了这一步，打电话/派活/看状态这些既有能力对新节点立刻生效，不用再改一行代码。"""
    nodes = nodes if nodes is not None else roster(prune=False)
    have = pm.peers()
    n = 0
    for nid, v in nodes.items():
        url = str(v.get("url") or "").rstrip("/")
        if not url:
            continue
        if (have.get(nid) or {}).get("url") == url:
            continue
        try:
            pm.set_peer(nid, url, str(v.get("label") or nid))
            n += 1
        except OSError:
            pass
    return n


def my_entry() -> List[Dict[str, Any]]:
    """我这一份名册（发给别人看的）。第一条永远是我自己 —— 名片跟名册一起走，
    这样一次请求既完成「交换名单」也完成「我上线了」。"""
    card = my_card()
    items = [{"node_id": card["node_id"], "label": card["label"], "url": card["url"],
              "ts": card["ts"]}]
    for nid, v in roster().items():
        if v.get("url"):
            items.append({"node_id": nid, "label": v.get("label") or nid,
                          "url": v.get("url"), "ts": int(v.get("last_seen") or 0)})
    return items


# ---------- 发现（gossip）----------
# 「上网后被所有大白看到」= 我广播一次 + 别人替我转发两跳。没有中心，所以必须
# 有人替传；没有中心也意味着不能无限传，所以跳数有上限、同一份广播只转一次。

_ann_seen: Dict[str, float] = {}      # 转发去重：同一份广播只转一次（进程内存，够用）
_ann_lock = threading.Lock()
_gossip_running = False                # 一轮 gossip 要几秒，期间不能让下一轮叠上来


def _post_to(node_id: str, path: str, payload: Dict[str, Any],
             timeout: float = PROBE_TIMEOUT) -> Dict[str, Any]:
    url = known_url(node_id)
    if not url:
        return {"ok": False, "error": f"no url for {node_id}"}
    return pm._post(url + path, payload, timeout)


def _targets(exclude: str = "", limit: int = 12) -> List[str]:
    """该跟谁 gossip：名册 + 地址簿的并集，最近听说过的优先。
    地址簿里的老节点也要发 —— 它们是「种子」，第一次发现全靠它们。"""
    me = pm.node_info(create=False).get("node_id")
    cand: Dict[str, int] = {}
    for nid, v in roster().items():
        cand[nid] = int(v.get("last_seen") or 0)
    for nid in pm.peers():
        cand.setdefault(nid, 0)
    cand.pop(me, None)
    cand.pop(exclude, None)
    return [n for n, _ in sorted(cand.items(), key=lambda kv: -kv[1])[:limit]]


def _spread(path: str, hops: int, limit: int = 12) -> Dict[str, Any]:
    """并发把名片+名册发出去，把回来的名册并进来。"""
    from concurrent.futures import ThreadPoolExecutor

    names = _targets(limit=limit)
    if not names:
        return {"ok": True, "asked": 0, "reached": 0, "added": 0, "updated": 0,
                "note": "名册和地址簿都是空的 —— 至少手动 add-peer 一台当种子"}
    payload = {"from": pm.node_info(create=False).get("node_id", ""),
               "card": my_card(), "nodes": my_entry(), "hops": hops}
    added = updated = reached = 0
    rows: List[Dict[str, Any]] = []

    def one(nid: str) -> Dict[str, Any]:
        r = _post_to(nid, path, payload)
        return {"node_id": nid, **r}

    with ThreadPoolExecutor(max_workers=max(1, min(8, len(names)))) as ex:
        for r in ex.map(one, names):
            if r.get("ok"):
                reached += 1
                m = merge_roster(r.get("nodes"), via=r["node_id"], hops=hops + 1)
                added += m["added"]
                updated += m["updated"]
            rows.append({"node_id": r.get("node_id"), "ok": bool(r.get("ok")),
                         "http": r.get("http", 200 if r.get("ok") else 0),
                         "error": r.get("error", ""), "learned": r.get("count", 0)})
    _touch_state(last_gossip=_now())
    return {"ok": True, "asked": len(names), "reached": reached,
            "added": added, "updated": updated, "peers": rows}


def gossip_once(limit: int = 12) -> Dict[str, Any]:
    """跟已知节点交换名册。这是发现的主循环。"""
    return _spread("/api/peer/social/roster", hops=0, limit=limit)


def announce(hops: int = ANNOUNCE_HOPS) -> Dict[str, Any]:
    """上线广播：让「我刚上网」在几秒内传到整个联盟，而不是等下一轮 gossip。"""
    return _spread("/api/peer/social/announce", hops=max(0, hops))


def _forward_announce(origin: str, hops: int, limit: int = 12) -> None:
    """替别人转发上线广播。只在后台线程里做 —— 转发的耗时不该由发起方等。"""
    key = f"{origin}:{_now() // 60}"          # 同一分钟内同一来源只转一次
    with _ann_lock:
        if _ann_seen.get(key):
            return
        _ann_seen[key] = time.time()
        if len(_ann_seen) > 500:
            for k in sorted(_ann_seen, key=lambda k: _ann_seen[k])[:200]:
                _ann_seen.pop(k, None)

    if hops <= 0:
        return

    def run() -> None:
        try:
            # 转发的是「我的名册」而不是「原广播」—— 我刚把 origin 并进来了，
            # 所以我的名册天然携带了这条新消息，不需要再造一份转发协议。
            _spread("/api/peer/social/announce", hops=hops - 1, limit=limit)
        except Exception:
            pass

    threading.Thread(target=run, daemon=True).start()


def _touch_state(**kw: Any) -> None:
    st = _load(SOCIAL_STATE_FILE, {})
    st.update(kw)
    _save(SOCIAL_STATE_FILE, st)


def last_gossip() -> int:
    return int(_load(SOCIAL_STATE_FILE, {}).get("last_gossip") or 0)


def tick(force: bool = False) -> Dict[str, Any]:
    """后台钩子：到点就 gossip 一次。挂在耳朵的循环里（那是个已经常驻的进程），
    不另外养一个定时器 —— 新进程就是一个新故障点。"""
    global _gossip_running
    if not force and (_now() - last_gossip()) < GOSSIP_INTERVAL:
        return {"ok": True, "skipped": True, "last_gossip": last_gossip()}
    with _ann_lock:
        if _gossip_running:
            return {"ok": True, "skipped": True, "note": "上一轮还没跑完"}
        _gossip_running = True
    try:
        return gossip_once()
    finally:
        with _ann_lock:
            _gossip_running = False


# ---------- 朋友圈（我关心谁）----------
# 单向：我把谁加进朋友圈，是我自己的事，不需要对方同意 —— 这正是「只要对方大白
# 有兴趣，就可以把它加入朋友圈」。对方只会收到一条通知，不会被拉进任何义务里。

def friends() -> Dict[str, Dict[str, Any]]:
    d = _load(FRIENDS_FILE, {}).get("nodes")
    return d if isinstance(d, dict) else {}


def friend_add(node_id: str, note: str = "", url: str = "") -> Dict[str, Any]:
    node_id = str(node_id or "").strip()
    me = pm.node_info(create=False).get("node_id")
    if not node_id:
        return {"ok": False, "error": "node_id required"}
    if node_id == me:
        return {"ok": False, "error": "不能把自己加进朋友圈"}
    addr = (url or known_url(node_id)).rstrip("/")
    if not addr:
        return {"ok": False, "error": f"不知道 {node_id} 的地址",
                "hint": "先 discover 一次，或用 url 显式给地址"}
    label = str((roster().get(node_id) or {}).get("label")
                or (pm.peers().get(node_id) or {}).get("label") or node_id)
    nodes = friends()
    new = node_id not in nodes
    nodes[node_id] = {"since": int(nodes.get(node_id, {}).get("since") or _now()),
                      "note": str(note or nodes.get(node_id, {}).get("note") or "")[:200],
                      "url": addr, "label": label}
    _save(FRIENDS_FILE, {"nodes": nodes, "updated": _now()})
    if addr:
        try:
            pm.set_peer(node_id, addr, label)      # 加了好友就该能直接打电话
        except OSError:
            pass
    told = {"ok": False}
    if new:
        # 告诉对方一声。通知失败不算加好友失败 —— 关注是我的决定，不需要对方签收。
        told = _post_to(node_id, "/api/peer/social/followed", {"from": me})
    return {"ok": True, "added": new, "node_id": node_id, "url": addr, "label": label,
            "notified": bool(told.get("ok")), "count": len(nodes)}


def friend_remove(node_id: str) -> Dict[str, Any]:
    nodes = friends()
    had = nodes.pop(str(node_id or "").strip(), None)
    if had is not None:
        _save(FRIENDS_FILE, {"nodes": nodes, "updated": _now()})
    return {"ok": True, "removed": bool(had), "count": len(nodes)}


# ---------- 动态流（发生了什么）----------
# 只 append 的 jsonl：动态是流水，不是状态，改一行不如加一行。

def feed_items(limit: int = 50, mark_read: bool = False) -> List[Dict[str, Any]]:
    try:
        lines = FEED_FILE.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out: List[Dict[str, Any]] = []
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            it = json.loads(line)
        except ValueError:
            continue
        if isinstance(it, dict):
            out.append(it)
        if len(out) >= max(1, limit):
            break
    if mark_read and lines:
        _touch_state(feed_cursor=len(lines))
    return out


def feed_unread() -> int:
    try:
        n = len(FEED_FILE.read_text(encoding="utf-8").splitlines())
    except OSError:
        return 0
    return max(0, n - int(_load(SOCIAL_STATE_FILE, {}).get("feed_cursor") or 0))


def _append_feed(item: Dict[str, Any]) -> Dict[str, Any]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(FEED_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(item, ensure_ascii=False) + "\n")
    _trim_feed()
    return item


def _trim_feed() -> None:
    try:
        lines = FEED_FILE.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    if len(lines) <= FEED_MAX:
        return
    keep = lines[-FEED_MAX:]
    pm._atomic_write(FEED_FILE, "\n".join(keep) + "\n")


def _item_id(author: str) -> str:
    return f"{author}-{_now()}-{os.urandom(3).hex()}"


def _me() -> str:
    return pm.node_info(create=False).get("node_id", "")


def _my_label() -> str:
    info = pm.node_info(create=False)
    return str(info.get("label") or info.get("node_id") or "")


def post(text: str, kind: str = "post") -> Dict[str, Any]:
    """发一条动态：落本地 + 推给我朋友圈里的每个人。
    推给「我关注的人」而不是「关注我的人」——我不知道谁关注我，也不该需要知道。"""
    text = str(text or "").strip()
    if not text:
        return {"ok": False, "error": "动态不能是空的"}
    me = _me()
    item = {"id": _item_id(me), "ts": _now(), "author": me, "label": _my_label(),
            "type": kind, "text": text[:4000]}
    _append_feed(item)
    friends_map = friends()
    sent: List[Dict[str, Any]] = []
    if friends_map:
        from concurrent.futures import ThreadPoolExecutor

        sig = pm.sign(me, item["ts"], item["text"])

        def one(nid: str) -> Dict[str, Any]:
            r = _post_to(nid, "/api/peer/social/post",
                         {"from": me, "id": item["id"], "text": item["text"],
                          "ts": item["ts"], "sig": sig, "label": item["label"]})
            return {"node_id": nid, "ok": bool(r.get("ok")), "error": r.get("error", "")}

        with ThreadPoolExecutor(max_workers=max(1, min(8, len(friends_map)))) as ex:
            sent = list(ex.map(one, list(friends_map)))
    return {"ok": True, "item": item, "friends": len(friends_map),
            "delivered": sum(1 for s in sent if s["ok"]), "results": sent}


def comment(item_id: str, text: str) -> Dict[str, Any]:
    """评论一条动态：落本地 + 推给原作者。"""
    item_id = str(item_id or "").strip()
    text = str(text or "").strip()
    if not item_id or not text:
        return {"ok": False, "error": "item_id 和 text 都要给"}
    target = None
    for it in feed_items(limit=FEED_MAX):
        if it.get("id") == item_id:
            target = it
            break
    if target is None:
        return {"ok": False, "error": f"本地动态流里没有 {item_id}"}
    author = str(target.get("author") or "")
    me = _me()
    ts = _now()
    entry = {"id": _item_id(me), "ts": ts, "author": me, "label": _my_label(),
             "type": "comment", "re": item_id, "text": text[:2000]}
    _append_feed(entry)
    if not author or author == me:
        return {"ok": True, "item": entry, "notified": False, "note": "评论的是自己的动态"}
    r = _post_to(author, "/api/peer/social/comment",
                 {"from": me, "item_id": item_id, "text": text, "ts": ts,
                  "sig": pm.sign(me, ts, text), "label": entry["label"]})
    return {"ok": True, "item": entry, "notified": bool(r.get("ok")),
            "error": r.get("error", "")}


# ---------- 服务端路由 ----------
# 前缀 /api/peer/social 落在 server.py 的 _AUTH_EXEMPT_PREFIX（/api/peer/）里，
# 所以这几条路由自己验共享密钥，不认会话 cookie —— 联邦实例之间不存在登录关系。

@router.post("/roster")
async def social_roster(request: Request):
    if not pm._key_ok(request):
        return pm._deny()
    b = await pm._body(request)
    frm = str(b.get("from") or "").strip()
    hops = int(b.get("hops") or 0)
    if isinstance(b.get("card"), dict) and b["card"].get("node_id"):
        merge_roster([b["card"]], via=frm, hops=hops + 1)
    m = merge_roster(b.get("nodes"), via=frm, hops=hops + 1)
    return {"ok": True, "from": _me(), "count": len(my_entry()),
            "nodes": my_entry(), **m}


@router.post("/announce")
async def social_announce(request: Request):
    if not pm._key_ok(request):
        return pm._deny()
    b = await pm._body(request)
    frm = str(b.get("from") or "").strip()
    hops = int(b.get("hops") or 0)
    if isinstance(b.get("card"), dict) and b["card"].get("node_id"):
        merge_roster([b["card"]], via=frm, hops=hops + 1)
    m = merge_roster(b.get("nodes"), via=frm, hops=hops + 1)
    if hops > 0 and frm:
        _forward_announce(frm, hops)          # 替它往更远处传：新节点不该等着被发现
    return {"ok": True, "from": _me(), "count": len(my_entry()),
            "nodes": my_entry(), **m}


@router.post("/post")
async def social_post(request: Request):
    if not pm._key_ok(request):
        return pm._deny()
    b = await pm._body(request)
    frm = str(b.get("from") or "").strip()
    text = str(b.get("text") or "")
    if not frm or not text:
        return JSONResponse({"ok": False, "error": "from/text required"}, status_code=400)
    if not pm.verify(frm, b.get("ts"), text, b.get("sig")):
        return JSONResponse({"ok": False, "error": "bad signature", "code": "forbidden"},
                            status_code=403)
    item = _append_feed({"id": str(b.get("id") or _item_id(frm))[:80], "ts": int(b.get("ts") or _now()),
                         "author": frm, "label": str(b.get("label") or frm)[:64],
                         "type": "post", "text": text[:4000]})
    return {"ok": True, "id": item["id"]}


@router.post("/comment")
async def social_comment(request: Request):
    if not pm._key_ok(request):
        return pm._deny()
    b = await pm._body(request)
    frm = str(b.get("from") or "").strip()
    text = str(b.get("text") or "")
    if not frm or not text:
        return JSONResponse({"ok": False, "error": "from/text required"}, status_code=400)
    if not pm.verify(frm, b.get("ts"), text, b.get("sig")):
        return JSONResponse({"ok": False, "error": "bad signature", "code": "forbidden"},
                            status_code=403)
    item = _append_feed({"id": _item_id(frm), "ts": int(b.get("ts") or _now()),
                         "author": frm, "label": str(b.get("label") or frm)[:64],
                         "type": "comment", "re": str(b.get("item_id") or "")[:80],
                         "text": text[:2000]})
    return {"ok": True, "id": item["id"]}


@router.post("/followed")
async def social_followed(request: Request):
    if not pm._key_ok(request):
        return pm._deny()
    b = await pm._body(request)
    frm = str(b.get("from") or "").strip()
    if not frm:
        return JSONResponse({"ok": False, "error": "from required"}, status_code=400)
    # 被关注是「有动态」而不是「有义务」：只落一条动态，不做任何回关动作。
    _append_feed({"id": _item_id(frm), "ts": _now(), "author": frm, "label": frm,
                  "type": "followed", "text": f"{frm} 把你加进了它的朋友圈"})
    return {"ok": True}


@router.post("/feed")
async def social_feed(request: Request):
    if not pm._key_ok(request):
        return pm._deny()
    b = await pm._body(request)
    items = feed_items(limit=int(b.get("limit") or 30), mark_read=False)
    return {"ok": True, "count": len(items), "items": items}


# ---------- 命令行 ----------

def _cli(argv: List[str]) -> int:
    cmd = (argv[0] if argv else "status").lower()
    rest = argv[1:]
    if cmd in ("status", "roster", "list"):
        nodes = roster()
        print(f"名册 {len(nodes)} 台 / 朋友圈 {len(friends())} 台 / 未读动态 {feed_unread()} 条")
        for nid, v in sorted(nodes.items()):
            mark = "★" if nid in friends() else " "
            print(f"  {mark} {nid:<12} {str(v.get('url') or '-'):<40} "
                  f"via={v.get('via') or '-':<10} hops={v.get('hops', 0)}")
        return 0
    if cmd in ("discover", "gossip"):
        r = gossip_once()
        print(json.dumps(r, ensure_ascii=False, indent=2))
        return 0 if r.get("reached") else 1
    if cmd == "announce":
        print(json.dumps(announce(), ensure_ascii=False, indent=2))
        return 0
    if cmd in ("friends", "friend"):
        if rest and rest[0] == "add" and len(rest) > 1:
            print(json.dumps(friend_add(rest[1], " ".join(rest[2:])), ensure_ascii=False, indent=2))
            return 0
        if rest and rest[0] in ("rm", "remove", "del") and len(rest) > 1:
            print(json.dumps(friend_remove(rest[1]), ensure_ascii=False, indent=2))
            return 0
        for nid, v in friends().items():
            print(f"  {nid:<12} {v.get('label', ''):<10} since={v.get('since')}")
        return 0
    if cmd in ("post", "say"):
        print(json.dumps(post(" ".join(rest)), ensure_ascii=False, indent=2))
        return 0
    if cmd == "feed":
        for it in feed_items(limit=int(rest[0]) if rest else 20):
            who = it.get("label") or it.get("author")
            print(f"[{time.strftime('%m-%d %H:%M', time.localtime(int(it.get('ts') or 0)))}] "
                  f"{who} {it.get('type')} {it.get('id')}\n    {it.get('text')}")
        return 0
    if cmd == "comment" and len(rest) > 1:
        print(json.dumps(comment(rest[0], " ".join(rest[1:])), ensure_ascii=False, indent=2))
        return 0
    if cmd == "tick":
        print(json.dumps(tick(force=True), ensure_ascii=False, indent=2))
        return 0
    if cmd == "url":
        if not rest:
            print(self_url() or "（还没设过自己的地址）")
            return 0 if self_url() else 1
        print(json.dumps(set_self_url(rest[0]), ensure_ascii=False, indent=2))
        return 0
    print(__doc__)
    return 1


if __name__ == "__main__":
    import sys

    raise SystemExit(_cli(sys.argv[1:]))
