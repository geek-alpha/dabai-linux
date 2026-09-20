"""大白联邦（Phoenix Mesh）—— 散落在不同机器上的大白互相找到、互相说话。

第一性原理：这件事只要三样东西，其中两样已经存在。
  寻址：每个实例都有自己的公网域名（cloudflared 隧道 + DNS），域名就是地址簿。
  传输：HTTPS 自带 TLS、重试、超时语义，不需要自造加密层和长连接框架。
  身份：唯一缺的东西 —— 一把共享密钥，谁有谁能进。
所以这里不新建服务、不开新端口、不建新隧道：加新东西就是加新故障点。

落盘三份（都在 data/ 下）：
  cluster.key   32 字节共享密钥（0600），全集群同一把
  node.json     本实例身份 {"node_id": "rpi", "label": "树莓派"}
  peers.json    地址簿 {"nodes": {"aliyun": {"url": "...", "label": "阿里云"}}}

协议（POST + JSON，头 X-Phoenix-Key 带密钥，body 带 HMAC 签名）：
  /api/peer/whoami  握手 —— 验密钥，回本实例身份
  /api/peer/say     收信 —— 落收件箱
  /api/peer/inbox   读信 —— 返回未读
  /api/peer/state   状态 —— 负载/内存/温度/在跑任务（「心有灵犀」= 知道对方在干什么）

为什么 say 除了密钥还要签名：密钥在三个实例上都存着，只验密钥的话，
任一实例被拿下就能冒充其他实例发话。HMAC 绑死 from+ts+text，再加 ±300s
时间窗，把「拿到密钥」和「冒充某个身份」拆成两件独立的事。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import socket
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
KEY_FILE = DATA_DIR / "cluster.key"
NODE_FILE = DATA_DIR / "node.json"
PEERS_FILE = DATA_DIR / "peers.json"
INBOX_FILE = DATA_DIR / "peer_inbox.jsonl"
CURSOR_FILE = DATA_DIR / "peer_cursor.json"
WATCH_CURSOR_FILE = DATA_DIR / "peer_watch_cursor.json"
TASK_LOG_FILE = DATA_DIR / "peer_tasks.jsonl"
TASK_QUOTA_FILE = DATA_DIR / "peer_task_quota.json"
OUTBOX_FILE = DATA_DIR / "peer_outbox.jsonl"

TS_WINDOW = 300          # 签名时间窗（秒）：足够容忍时钟漂移，又不足以让抓包重放
PROBE_TIMEOUT = 6.0      # 单实例探测超时
CALL_POLL = 0.4          # 等对方接电话的轮询间隔（秒）：读本地文件，代价约等于零
KEY_HEADER = "X-Phoenix-Key"

router = APIRouter(prefix="/api/peer", tags=["peer"])


# ---------- 存储层 ----------

def _atomic_write(path: Path, text: str, mode: int = 0o600) -> None:
    """tmp + rename 原子写：树莓派断电频繁，写一半的密钥文件会让整个集群失联。

    tmp 名带 pid+线程 id：survey() 是并发探测，全新机器上三个线程会同时发现
    「key 不存在」并同时生成 —— 共用一个 tmp 名时，先落盘的那个把 tmp 移走，
    剩下的 chmod 打在空气上（FileNotFoundError），首启动直接崩。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def cluster_key(create: bool = True) -> bytes:
    """共享密钥。读不到且 create=True 时生成一把 —— 新实例首启动即自举。"""
    try:
        raw = KEY_FILE.read_text(encoding="utf-8").strip()
        if raw:
            return raw.encode("utf-8")
    except OSError:
        pass
    if not create:
        return b""
    key = secrets.token_urlsafe(32)
    KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = KEY_FILE.with_name(f"{KEY_FILE.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(key + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    try:
        # link 是原子的：目标已存在就抛 FileExistsError，并发自举只有一个赢家。
        # 「生成→原子写→重读」不够：两个线程都可能在对方落盘前完成生成，各拿一把，
        # 落选那把去发请求全变 403。
        os.link(tmp, KEY_FILE)
    except FileExistsError:
        pass
    except OSError:
        _atomic_write(KEY_FILE, key + "\n")
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass
    try:
        return KEY_FILE.read_text(encoding="utf-8").strip().encode("utf-8")
    except OSError:
        return key.encode("utf-8")


def node_info(create: bool = True) -> Dict[str, str]:
    """本实例身份。node_id 是联邦里的唯一名字，缺省取 hostname 的短名。"""
    try:
        d = json.loads(NODE_FILE.read_text(encoding="utf-8"))
        if isinstance(d, dict) and d.get("node_id"):
            return d
    except (OSError, ValueError):
        pass
    if not create:
        return {"node_id": "", "label": ""}
    short = (socket.gethostname() or "node").split(".")[0].lower()
    d = {"node_id": short, "label": short}
    _atomic_write(NODE_FILE, json.dumps(d, ensure_ascii=False, indent=2))
    return d


def peers() -> Dict[str, Dict[str, str]]:
    """地址簿：node_id -> {"url": ..., "label": ...}。自己不在表里。"""
    try:
        d = json.loads(PEERS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    nodes = d.get("nodes") if isinstance(d, dict) else None
    if not isinstance(nodes, dict):
        return {}
    me = node_info(create=False).get("node_id")
    return {k: v for k, v in nodes.items() if k != me and isinstance(v, dict) and v.get("url")}


def set_peer(node_id: str, url: str, label: str = "") -> Dict[str, Any]:
    try:
        d = json.loads(PEERS_FILE.read_text(encoding="utf-8"))
        if not isinstance(d, dict):
            d = {}
    except (OSError, ValueError):
        d = {}
    d.setdefault("nodes", {})[node_id] = {"url": url.rstrip("/"), "label": label or node_id}
    _atomic_write(PEERS_FILE, json.dumps(d, ensure_ascii=False, indent=2))
    return d["nodes"][node_id]


# ---------- 离线投递（发件箱）----------
# 一次 POST 失败就丢消息，等于「对方不在线 = 这件事没发生过」。排队重投把投递
# 变成至少一次：对方上线（或密钥对齐）后自动补送，不用人记得重发。
# 代价是可能重复送达 —— 发送方在收到响应前断线时，消息已到、回执没回。
OUTBOX_TTL = 7 * 86400              # 排队消息的保质期：过期不再投，避免陈旧指令诈尸
OUTBOX_MAX = 500                    # 队列上限：超了丢最老的，新的更要紧
OUTBOX_BACKOFF = (30, 60, 120, 300, 600, 900, 1800)


def outbox_items() -> List[Dict[str, Any]]:
    try:
        lines = OUTBOX_FILE.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out: List[Dict[str, Any]] = []
    for ln in lines:
        try:
            d = json.loads(ln)
        except ValueError:
            continue
        if isinstance(d, dict) and d.get("node_id") and d.get("text"):
            out.append(d)
    return out


def _outbox_write(items: List[Dict[str, Any]]) -> None:
    if not items:
        try:
            OUTBOX_FILE.unlink()
        except OSError:
            pass
        return
    _atomic_write(OUTBOX_FILE,
                  "".join(json.dumps(i, ensure_ascii=False) + "\n" for i in items))


def queue_outbox(node_id: str, text: str, kind: str = "say", cid: str = "",
                 re_ts: int = 0, error: str = "") -> Dict[str, Any]:
    """把一条送不到的消息排队。同节点同内容已在队里就不重复排 —— 发布脚本重跑
    不该把同一条通知堆成 N 份。"""
    items = outbox_items()
    for it in items:
        if (it.get("node_id") == node_id and it.get("text") == text
                and it.get("kind") == kind):
            return {"queued": False, "reason": "duplicate", "pending": len(items)}
    now = time.time()
    items.append({"node_id": node_id, "text": text, "kind": kind, "cid": cid,
                  "re": int(re_ts or 0), "queued_at": now, "attempts": 0,
                  "next_try_at": now, "last_error": error})
    dropped = 0
    if len(items) > OUTBOX_MAX:
        dropped = len(items) - OUTBOX_MAX
        items = items[dropped:]
    _outbox_write(items)
    return {"queued": True, "pending": len(items), "dropped_oldest": dropped}


def flush_outbox(limit: int = 5, now: Optional[float] = None) -> Dict[str, Any]:
    """把到点的排队消息重投一遍：成功出队，失败按指数退避改下次时间。

    重投走 say() 会重新签名 —— 签名绑 ts 且有 5 分钟时间窗，原样重发旧消息对面
    一律拒收。重签不影响「是谁在说」：签的还是同一把集群密钥。
    """
    items = outbox_items()
    if not items:
        return {"sent": 0, "kept": 0, "dropped": 0, "pending": 0}
    t = time.time() if now is None else now
    kept: List[Dict[str, Any]] = []
    sent = dropped = tried = 0
    for it in items:
        if tried >= limit or float(it.get("next_try_at") or 0) > t:
            kept.append(it)
            continue
        if t - float(it.get("queued_at") or t) > OUTBOX_TTL:
            dropped += 1
            continue
        tried += 1
        r = say(str(it["node_id"]), str(it["text"]), kind=str(it.get("kind") or "say"),
                cid=str(it.get("cid") or ""), re_ts=int(it.get("re") or 0),
                queue_on_fail=False)
        if r.get("ok"):
            sent += 1
            continue
        it["attempts"] = int(it.get("attempts") or 0) + 1
        it["next_try_at"] = t + OUTBOX_BACKOFF[
            min(it["attempts"] - 1, len(OUTBOX_BACKOFF) - 1)]
        it["last_error"] = str(r.get("error") or "")
        kept.append(it)
    _outbox_write(kept)
    return {"sent": sent, "kept": len(kept), "dropped": dropped, "pending": len(kept)}


# ---------- 签名 ----------

def sign(from_id: str, ts: int, text: str, key: Optional[bytes] = None) -> str:
    k = key if key is not None else cluster_key()
    msg = f"{from_id}|{ts}|{text}".encode("utf-8")
    return hmac.new(k, msg, hashlib.sha256).hexdigest()


def verify(from_id: str, ts: Any, text: str, sig: Any, key: Optional[bytes] = None) -> bool:
    try:
        ts_i = int(ts)
    except (TypeError, ValueError):
        return False
    if abs(time.time() - ts_i) > TS_WINDOW:
        return False
    expect = sign(str(from_id or ""), ts_i, str(text or ""), key)
    return hmac.compare_digest(expect, str(sig or ""))


# ---------- 收件箱 ----------
# 邮箱的语义是「对方留给我的、我下次醒来才处理的东西」。同步电话（call/reply）和系统
# 指令（release）共用同一条投递管道 —— 耳朵靠它收信、peer_call 靠它认领回话 —— 但它们
# 不是留言：电话双方都在场、当场就答完了，再计一次未读等于把答过的话重复推到眼前
# （2026-09-21 主人：打电话的过程不该存邮箱里）。管道照旧，只是不进邮箱出口。
CALL_KINDS = ("call", "reply", "release")


def is_mail(entry: Dict[str, Any]) -> bool:
    """这条算不算「留言」—— 收件箱出口（读信 / 未读注入）只放留言。"""
    return str(entry.get("kind") or "say") not in CALL_KINDS


def _is_mail_line(line: str) -> bool:
    """行级粗筛，不解析 JSON：未读计数要扫全部未读行，逐行 json.loads 在积压时是白烧
    CPU。格式由 append_inbox 的 json.dumps 固定产出；粗筛命不中最多让一条通话记录多算
    一次未读，不会丢信。"""
    return not any(f'"kind": "{k}"' in line for k in CALL_KINDS)


def append_inbox(entry: Dict[str, Any]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(INBOX_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _read_cursor() -> Dict[str, int]:
    try:
        d = json.loads(CURSOR_FILE.read_text(encoding="utf-8"))
        return {k: int(v) for k, v in d.items()} if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def read_inbox(mark_read: bool = False, limit: int = 50) -> List[Dict[str, Any]]:
    """未读消息。mark_read 时把游标推到文件末尾 —— 只推游标不重写文件，
    追加写的日志永远不用做「改一行」这种会把断电写坏的动作。"""
    try:
        lines = INBOX_FILE.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    cur = _read_cursor()
    start = int(cur.get("inbox", 0) or 0)
    out: List[Dict[str, Any]] = []
    for line in lines[start:]:
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if is_mail(e):
            out.append(e)
    if mark_read and len(lines) > start:
        cur["inbox"] = len(lines)
        _atomic_write(CURSOR_FILE, json.dumps(cur, ensure_ascii=False, indent=2))
    return out[:limit] if limit > 0 else out


def unread_summary(preview: int = 3) -> Dict[str, Any]:
    """未读概览 {"count": N, "items": [...]}：只数行、只解析尾部 preview 条，不标已读。

    给 agent 每轮注入用 —— 收件箱积压时全量 json 解析是纯浪费；行切分 + 尾部解析
    在积压上万条时也只多几十毫秒。标已读留给真正读信的动作，别在这里偷偷消费。
    """
    try:
        lines = INBOX_FILE.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {"count": 0, "items": []}
    start = int(_read_cursor().get("inbox", 0) or 0)
    mail = [ln for ln in lines[start:] if ln.strip() and _is_mail_line(ln)]
    items: List[Dict[str, Any]] = []
    for line in mail[-preview:]:
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if is_mail(e):
            items.append(e)
    return {"count": len(mail), "items": items}


def _read_watch_cursor() -> int:
    try:
        d = json.loads(WATCH_CURSOR_FILE.read_text(encoding="utf-8"))
        return int(d.get("watch", 0)) if isinstance(d, dict) else 0
    except (OSError, ValueError, AttributeError):
        return 0


def read_new(mark: bool = True) -> List[Dict[str, Any]]:
    """耳朵专用：读自监听游标以来的新消息。

    游标独立于 peer_cursor.json ——「大白自己读到哪」和「耳朵听到哪」是两件事，
    共用游标的话，耳朵先听到就等于大白永远看不到这条消息。
    """
    try:
        lines = INBOX_FILE.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    start = _read_watch_cursor()
    out: List[Dict[str, Any]] = []
    for line in lines[start:]:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    if mark and len(lines) > start:
        _atomic_write(WATCH_CURSOR_FILE, json.dumps({"watch": len(lines)}))
    return out


def _tail(n: int = 50) -> List[Dict[str, Any]]:
    """收件箱最后 n 条，不看已读游标 —— 等回话要的是「刚到的」，不是「未读的」。"""
    try:
        lines = INBOX_FILE.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out: List[Dict[str, Any]] = []
    for line in lines[-n:]:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


# ---------- 通话通道（call session） ----------
# 第一性原理：打电话的实质不是「消息能到」，是「双方共享同一段会话状态」。一问一答的
# 往返不叫电话 —— 每通都要重新交代背景，问题稍微复杂就得打好几通，这不叫连续。
# 所以电话有自己的账本（peer_calls.jsonl，与邮箱的「留言」语义彻底分开）：
# 一轮一行，双方各自记自己这一侧，cid 相同的轮次属于同一通电话。
# 拨号方不传 cid 时自动续接「跟这个同伴还没挂断的那通」—— 连续性由通道自己维持，
# 不靠调用方记得带 id，就像真实电话拿起话筒就接上原来那通。
CALL_LOG_FILE = DATA_DIR / "peer_calls.jsonl"
CALL_STATE_FILE = DATA_DIR / "peer_calls.json"
CALL_IDLE_TTL = 900.0     # 15 分钟没有新轮次就算挂断：会话不能永久挂着，否则上下文越滚越大
CALL_LOG_TAIL = 4000      # 账本只扫尾部这么多行：通话要快，长历史不该拖慢每一次开口


def _call_state() -> Dict[str, Any]:
    try:
        d = json.loads(CALL_STATE_FILE.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _call_state_write(st: Dict[str, Any]) -> None:
    try:
        _atomic_write(CALL_STATE_FILE, json.dumps(st, ensure_ascii=False))
    except OSError:
        pass


def _sweep_calls(st: Dict[str, Any]) -> Dict[str, Any]:
    """闲置超时自动挂断。惰性清扫：读状态时顺手做，不值得为它养一个后台定时器。"""
    now = time.time()
    for c in (st.get("calls") or {}).values():
        if str(c.get("status") or "open") != "open":
            continue
        if now - float(c.get("last") or 0) > CALL_IDLE_TTL:
            c["status"] = "closed"
            c["closed_reason"] = "idle"
    return st


def call_active(node_id: str = "") -> List[Dict[str, Any]]:
    """还没挂断的通话，最近的排前面。node_id 为空时返回全部。"""
    st = _sweep_calls(_call_state())
    _call_state_write(st)
    out = []
    for cid, c in (st.get("calls") or {}).items():
        if str(c.get("status") or "open") != "open":
            continue
        if node_id and str(c.get("with") or "") != node_id:
            continue
        out.append({"cid": cid, **c})
    return sorted(out, key=lambda x: float(x.get("last") or 0), reverse=True)


def call_record(cid: str, with_node: str, role: str, text: str,
                ts: Optional[int] = None) -> Dict[str, Any]:
    """记一轮通话。role: me=我说的 / peer=对方说的。返回该通话的最新状态。

    双方各自记自己这一侧（我发的话写我的账本，对面回的话也写我的账本），
    所以每一侧的账本都是一份完整的往返记录，不用去对方机器上取上下文。
    """
    cid = str(cid or "").strip()
    if not cid:
        return {}
    ts_i = int(ts or time.time())
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(CALL_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps({"cid": cid, "with": str(with_node or ""), "role": role,
                                "text": str(text or ""), "ts": ts_i},
                               ensure_ascii=False) + "\n")
    except OSError:
        pass
    st = _sweep_calls(_call_state())
    calls = st.setdefault("calls", {})
    c = dict(calls.get(cid) or {"with": str(with_node or ""), "started": ts_i, "round": 0})
    c["with"] = str(with_node or c.get("with") or "")
    c["last"] = ts_i
    c["status"] = "open"
    c.pop("closed_reason", None)
    if role == "me":
        # 只有「我说出去的一轮」才推进轮次：对方的回话属于同一轮，不另起一轮
        c["round"] = int(c.get("round") or 0) + 1
        c["last_text"] = str(text or "")[:200]
    else:
        c["last_reply"] = str(text or "")[:200]
    calls[cid] = c
    _call_state_write(st)
    return {"cid": cid, **c}


def call_transcript(cid: str, limit: int = 12) -> List[Dict[str, Any]]:
    """某通电话的往返记录（老的在前）。对面据此接着上轮往下说，不用重问一遍。"""
    cid = str(cid or "").strip()
    if not cid:
        return []
    try:
        lines = CALL_LOG_FILE.read_text(encoding="utf-8").splitlines()[-CALL_LOG_TAIL:]
    except OSError:
        return []
    out: List[Dict[str, Any]] = []
    for line in lines:
        line = line.strip()
        if not line or cid not in line:      # 粗筛：不解析明显不属于这通电话的行
            continue
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if str(e.get("cid") or "") == cid:
            out.append(e)
    return out[-limit:]


def call_history(cid: str, peer_label: str = "同伴", limit: int = 10,
                 drop_last: str = "") -> str:
    """这通电话前面几轮说过什么，拼成一段给 LLM 看的文本。空串 = 没什么可说的。

    drop_last 传本次来电原文：账本里最后一条可能正是它，去掉避免重复。
    只删确认是同一条的 —— 对端还没升级时没记账，那时删掉的就是上一轮真历史。
    """
    if not str(cid or "").strip():
        return ""
    turns = call_transcript(cid, limit=limit)
    if turns and drop_last \
            and str(turns[-1].get("text") or "")[:80] == str(drop_last)[:80]:
        turns = turns[:-1]
    if not turns:
        return ""
    lines = []
    for t in turns:
        who = "我" if str(t.get("role")) == "me" else peer_label
        lines.append(f"  {who}：{str(t.get('text') or '')[:300]}")
    return "\n【这通电话前面已经说过的】（接着往下说，别重问已答过的）\n" + "\n".join(lines)


def call_hangup(cid: str = "", node_id: str = "") -> Dict[str, Any]:
    """挂断。cid 和 node_id 都不给 = 挂断全部（别的地方要重开就当新电话）。"""
    st = _sweep_calls(_call_state())
    calls = st.setdefault("calls", {})
    closed = []
    for k, c in calls.items():
        if str(c.get("status") or "open") != "open":
            continue
        if cid and k != cid:
            continue
        if node_id and str(c.get("with") or "") != node_id:
            continue
        c["status"] = "closed"
        c["closed_reason"] = "hangup"
        closed.append(k)
    _call_state_write(st)
    return {"closed": closed, "count": len(closed)}


def _open_cid(node_id: str) -> str:
    act = call_active(node_id)
    return str(act[0].get("cid") or "") if act else ""


# ---------- 联邦派活（kind=task） ----------
# 第一性原理：同伴要的不是「我替它跑命令」，是「让它那台的执行器动起来」。
# 那台机器本来就有无人值守的执行入口 —— scheduler 每 15 秒从 scheduled_tasks.json
# 重读一次，外部进程写进去就会被捡到。所以这里不新增执行通道，只把 task 消息
# 翻译成一条一次性定时任务。耳朵照旧只响铃、不执行，shell 不进耳朵。

TASK_QUOTA_PER_HOUR = 6        # 每同伴每小时最多接几单：对方程序出错时不能变成刷屏
TASK_CHAR_CAP = 2000
TASK_INTERVAL_SEC = 86400      # 一次性任务的占位间隔，跑完即 enabled=False

# 命中即不自动执行，转人工确认。只拦「不可逆」这一类，普通读写/查询不拦 ——
# 拦得太宽等于这个能力没用；拦得住手滑和不可逆，才是它存在的理由。
_DANGEROUS = (
    r"rm\s+-[a-z]*[rf]",
    r"\bmkfs",
    r"\bdd\b[^\n]*of=/dev/",
    r"\b(shutdown|reboot|poweroff|halt)\b",
    r">\s*/dev/(sd|nvme|mmcblk)",
    r":\(\)\s*\{",
    r"(curl|wget)[^\n|]*\|\s*(ba|z)?sh",
    r"chmod\s+-R\s+777\s+/",
    r"\b(userdel|groupdel|passwd)\b",
    r">\s*/etc/",
)
_DANGEROUS_RE = re.compile("|".join(_DANGEROUS), re.I)


def task_gate_enabled() -> bool:
    """settings.json → peer.allow_remote_task。读不到按开处理（联邦本身就是「有密钥就能进」）。"""
    try:
        d = json.loads((BASE_DIR / "settings.json").read_text(encoding="utf-8"))
        v = (d.get("peer") or {}).get("allow_remote_task")
        return True if v is None else bool(v)
    except (OSError, ValueError, AttributeError):
        return True


def dangerous_in(text: str) -> str:
    """返回命中的危险模式（空串=安全）。"""
    m = _DANGEROUS_RE.search(text or "")
    return m.group(0) if m else ""


def _quota_take(frm: str) -> bool:
    """记账并判断配额。整文件读改写：全集群就三台机器，不值得上锁。"""
    now = time.time()
    try:
        d = json.loads(TASK_QUOTA_FILE.read_text(encoding="utf-8"))
        d = d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        d = {}
    recent = [float(t) for t in (d.get(frm) or []) if now - float(t) < 3600]
    ok = len(recent) < TASK_QUOTA_PER_HOUR
    d[frm] = recent + ([now] if ok else [])
    _atomic_write(TASK_QUOTA_FILE, json.dumps(d, ensure_ascii=False, indent=2))
    return ok


def _audit_task(row: Dict[str, Any]) -> None:
    try:
        TASK_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(TASK_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _task_brief(frm: str, text: str) -> str:
    return (
        f"【跨机联邦委派 · 来自同伴 {frm}】\n{text}\n\n"
        f"要求：\n"
        f"- 这是另一台机器上的你自己派来的活，不是用户的请求；能自己查就自己查，别反问\n"
        f"- 干完用一句话回报发起方：python peer_mesh.py say {frm} \"<结论>\"\n"
        f"- 不可逆动作照旧先问用户，别因为「是同伴派的」就跳过确认"
    )


def _ledger_gate() -> str:
    """余额见底就不接活 —— 「生存压力」在系统里唯一真正咬人的地方。

    账本文件不存在 = 这台还没启用资源账（还没升级），照旧接活：新机制不该悄悄
    改掉老行为，否则一次发布就让整个联盟互相拒单。
    账本存在但读不动 = 检查坏掉，报成「未检查」并拒单 —— 静默放行比没有检查更危险。
    """
    try:
        import peer_ledger as pl
        if not pl.LEDGER_FILE.exists():
            return ""
        pl.endowment()
        chk = pl.can_work()
    except Exception as e:
        return f"本机资源账不可用（{type(e).__name__}: {e}），按未检查处理、不接活"
    if chk.get("ok"):
        return ""
    return f"本机余额见底（{chk.get('balance')} cr）：{chk.get('reason')}"


def accept_task(entry: Dict[str, Any]) -> Dict[str, Any]:
    """把一条 kind=task 消息翻译成一次性定时任务。任何失败都返回 dict，绝不抛异常。

    闸门按「代价从低到高」排，但钱的事排在最前：先确认对方的预付款真到账（链上
    有这笔收款才算，嘴上说付了不算），再开关 → 危险模式 → 配额 → 余额。后面任何
    一道拒了，钱都原路退回去 —— 接了单才收钱，没接成不能白拿。
    """
    frm = str(entry.get("from") or "?")
    text = str(entry.get("text") or "").strip()[:TASK_CHAR_CAP]
    row: Dict[str, Any] = {"ts": int(time.time()), "from": frm, "text": text[:400]}
    paid = entry.get("paid") if isinstance(entry.get("paid"), dict) else {}
    nonce = str(paid.get("nonce") or "")
    got: Dict[str, Any] = {}

    def _refund(why: str) -> None:
        """接单没接成，把预付款退回去。退不了也记下来，别假装退过。"""
        if not got:
            return
        try:
            import peer_ledger as pl
            r = pl.pay(frm, float(got.get("amount") or 0), f"退款：{why}")
            row["refund"] = {"ok": bool(r.get("ok")), "amount": got.get("amount"),
                             "error": str(r.get("error") or "")}
        except Exception as e:
            row["refund"] = {"ok": False, "error": f"{type(e).__name__}: {e}"}

    if not text:
        row["result"] = "empty"
        _audit_task(row)
        return {"ok": False, "error": "空任务"}
    if nonce:
        try:
            import peer_ledger as pl
            got = pl.received(nonce) or {}
        except Exception as e:
            got = {}
            row["pay_error"] = f"{type(e).__name__}: {e}"
        if not got:
            row["result"] = "unpaid"
            row["reason"] = f"声称预付 nonce={nonce}，本机链上查不到这笔收款"
            _audit_task(row)
            return {"ok": False,
                    "error": f"{frm} 说已预付（nonce {nonce}），但我链上没这笔收款：拒单，不收假付的钱"}
        row["paid"] = {"nonce": nonce, "amount": got.get("amount"), "seq": got.get("seq")}
    elif entry.get("unpaid"):
        row["unpaid"] = str(entry.get("unpaid"))[:160]   # 对方明确说没付成，记下来
    if not task_gate_enabled():
        row["result"] = "disabled"
        _refund("本机已关掉联邦派活")
        _audit_task(row)
        return {"ok": False, "error": "本机已关掉联邦派活（settings.json → peer.allow_remote_task=false）"}
    bad = dangerous_in(text)
    if bad:
        row["result"] = "blocked"
        row["reason"] = bad
        _refund(f"任务含不可逆动作「{bad}」")
        _audit_task(row)
        return {"ok": False, "blocked": True, "error": f"含不可逆动作「{bad}」，已拦下、没有自动执行"}
    if not _quota_take(frm):
        row["result"] = "quota"
        _refund("超出本机接单配额")
        _audit_task(row)
        return {"ok": False, "error": f"{frm} 一小时内已派满 {TASK_QUOTA_PER_HOUR} 单，这条只记录不执行"}
    broke = _ledger_gate()
    if broke:
        row["result"] = "broke"
        row["reason"] = broke
        _refund("本机余额见底，接不了活")
        _audit_task(row)
        return {"ok": False, "error": broke}

    try:
        from scheduler import add_job
        job, err = add_job(name=f"联邦·{frm}·{int(time.time())}·{secrets.token_hex(3)}",
                           task=_task_brief(frm, text),
                           interval_sec=TASK_INTERVAL_SEC, once=True)
    except Exception as e:
        job, err = None, f"{type(e).__name__}: {e}"
    if not job:
        row["result"] = "dispatch_failed"
        row["reason"] = str(err)
        _audit_task(row)
        return {"ok": False, "error": f"派发失败：{err}"}

    row["result"] = "accepted"
    row["job_id"] = str(job.get("id"))
    _audit_task(row)
    return {"ok": True, "job_id": str(job.get("id")),
            "note": "已接单：子智能体后台执行，干完回你收件箱"}


# ---------- 本机状态 ----------

def _read_first(paths: List[str]) -> Optional[str]:
    for p in paths:
        try:
            with open(p, "r", encoding="utf-8") as f:
                return f.read().strip()
        except OSError:
            continue
    return None


def local_state() -> Dict[str, Any]:
    """给别的实例看的自画像。任何一项读不到就缺省，绝不假装健康。"""
    st: Dict[str, Any] = {"node_id": node_info(create=False).get("node_id", "")}
    # 耳朵（peer_watch）是不是在跑：决定这台能不能接实时电话。
    # 同进程共享模块变量，不落盘 —— 0.4 秒一次的循环不该去磨 SD 卡。
    try:
        import peer_watch
        st["ear"] = peer_watch.alive()
    except Exception:
        st["ear"] = False
    try:
        st["load1"] = round(os.getloadavg()[0], 2)
    except (OSError, AttributeError):
        pass
    try:
        mem: Dict[str, int] = {}
        with open("/proc/meminfo", "r", encoding="utf-8") as f:
            for line in f:
                k, _, v = line.partition(":")
                mem[k.strip()] = int(v.strip().split()[0])
        st["mem_total_mb"] = mem.get("MemTotal", 0) // 1024
        st["mem_avail_mb"] = mem.get("MemAvailable", 0) // 1024
    except (OSError, ValueError, IndexError):
        pass
    raw = _read_first(["/sys/class/thermal/thermal_zone0/temp"])
    if raw:
        try:
            st["temp_c"] = round(int(raw) / 1000, 1)
        except ValueError:
            pass
    raw = _read_first(["/proc/uptime"])
    if raw:
        try:
            st["uptime_s"] = int(float(raw.split()[0]))
        except (ValueError, IndexError):
            pass
    return st


# ---------- 服务端路由 ----------
# 这四条路由自己验密钥，因此必须挂在 AuthGateMiddleware 的豁免前缀里
# （server.py 的 _AUTH_EXEMPT_PREFIX）—— 别的实例没有会话 cookie。

def _key_ok(request: Request) -> bool:
    sent = (request.headers.get(KEY_HEADER) or "").strip()
    if not sent:
        return False
    return hmac.compare_digest(sent.encode("utf-8"), cluster_key(create=False))


async def _body(request: Request) -> Dict[str, Any]:
    try:
        d = await request.json()
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _deny() -> JSONResponse:
    return JSONResponse({"ok": False, "error": "bad or missing key", "code": "forbidden"},
                        status_code=403)


@router.post("/whoami")
async def peer_whoami(request: Request):
    if not _key_ok(request):
        return _deny()
    info = node_info()
    return {
        "ok": True,
        "node_id": info.get("node_id", ""),
        "label": info.get("label", ""),
        "hostname": socket.gethostname(),
        "ts": int(time.time()),
        "protocol": 1,
    }


@router.post("/say")
async def peer_say(request: Request):
    if not _key_ok(request):
        return _deny()
    b = await _body(request)
    frm = str(b.get("from") or "").strip()
    text = str(b.get("text") or "")
    if not frm or not text:
        return JSONResponse({"ok": False, "error": "from/text required"}, status_code=400)
    if not verify(frm, b.get("ts"), text, b.get("sig")):
        return JSONResponse({"ok": False, "error": "bad signature", "code": "forbidden"},
                            status_code=403)
    entry = {
        "ts": int(time.time()),
        "from": frm,
        "text": text[:8000],
        "kind": str(b.get("kind") or "say")[:32],
    }
    if b.get("cid"):
        entry["cid"] = str(b.get("cid"))[:64]
    if b.get("re"):
        try:
            entry["re"] = int(b.get("re"))
        except (TypeError, ValueError):
            pass
    append_inbox(entry)
    out: Dict[str, Any] = {"ok": True, "delivered": True, "at": entry["ts"]}
    if entry["kind"] == "task":
        out["task"] = accept_task(entry)
    return out


@router.post("/inbox")
async def peer_inbox(request: Request):
    if not _key_ok(request):
        return _deny()
    b = await _body(request)
    msgs = read_inbox(mark_read=bool(b.get("mark_read")), limit=int(b.get("limit") or 50))
    return {"ok": True, "count": len(msgs), "messages": msgs}


@router.post("/state")
async def peer_state(request: Request):
    if not _key_ok(request):
        return _deny()
    st = local_state()
    st["ok"] = True
    return st


# ---------- 客户端 ----------
# 用标准库 urllib 而不是 requests：这段代码要部署到三台机器（含 905MB 内存的树莓派），
# 少一个第三方依赖就少一个「这台装了那台没装」的部署失败面。

import urllib.error
import urllib.request


# Cloudflare 会 403 掉 urllib 的默认 User-Agent（Python-urllib/3.x）——实测同一请求
# 不带头 403、带任意自定义 UA 200。所有联邦实例都走 CF 隧道，所以这个头是必需的。
_UA = "dabai-peer/1.0"
# 显式绕过环境代理：WSL 的 dabai.service 设了 HTTPS_PROXY（那是给出国用的），
# urllib 默认会读它 —— 于是「两个大白说话」变成「经过一个第三方代理中转」，
# 代理一挂联邦就断。实例之间走 Cloudflare 直连，不借道。
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _post(url: str, payload: Dict[str, Any], timeout: float) -> Dict[str, Any]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Content-Type": "application/json",
                 "User-Agent": _UA,
                 KEY_HEADER: cluster_key().decode("utf-8")},
    )
    try:
        with _opener.open(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        # 带上状态码和 detail：不然 404（对方是旧版本，没这条路由）和 403（密钥不对）
        # 都会退化成 {"detail": "Not Found"} 或空 error，排查时只能靠猜。
        try:
            body = json.loads(e.read().decode("utf-8"))
        except Exception:
            return {"ok": False, "http": e.code, "error": f"HTTP {e.code}"}
        if not isinstance(body, dict):
            return {"ok": False, "http": e.code, "error": f"HTTP {e.code}"}
        body.setdefault("ok", False)
        body["http"] = e.code
        if not body.get("error"):
            body["error"] = body.get("detail") or f"HTTP {e.code}"
        return body
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def call(node_id: str, path: str, payload: Optional[Dict[str, Any]] = None,
         timeout: float = PROBE_TIMEOUT) -> Dict[str, Any]:
    """给某个实例发一次请求。任何失败都返回 {ok: False, error}，绝不抛异常 ——
    调用方是大白的工具层，抛出去就变成一轮对话的崩溃。"""
    p = peers().get(node_id)
    if not p:
        return {"ok": False, "error": f"unknown node: {node_id}",
                "known": sorted(peers().keys())}
    url = str(p.get("url") or "").rstrip("/") + path
    return _post(url, payload or {}, timeout)


def whoami(node_id: str, timeout: float = PROBE_TIMEOUT) -> Dict[str, Any]:
    return call(node_id, "/api/peer/whoami", {}, timeout)


def _prepay(node_id: str, text: str) -> Dict[str, Any]:
    """派活先付钱，付完把凭证带上。付不出去就如实标记，绝不假装付过。

    付不出去有两种：对岸还没升级（没有 /ledger/receive），或我自己余额见底。
    两种都照旧把活发出去（兼容期，一次发布不该让联盟互相拒单），但消息里写明
    未付费 —— 对岸接单时看得见，拒或接由它自己定。
    """
    try:
        import peer_ledger as pl
        if not pl.LEDGER_FILE.exists():
            return {}
        r = pl.pay(node_id, pl.TASK_PRICE, f"派活预付：{text[:60]}")
    except Exception as e:
        return {"unpaid": f"{type(e).__name__}: {e}"}
    if not r.get("ok"):
        return {"unpaid": str(r.get("error") or "付款失败")[:160]}
    return {"paid": {"nonce": r["nonce"], "amount": r["amount"],
                     "from": node_info().get("node_id", "")}}


def say(node_id: str, text: str, kind: str = "say",
        timeout: float = PROBE_TIMEOUT, cid: str = "", re_ts: int = 0,
        queue_on_fail: bool = True, prepay: bool = True) -> Dict[str, Any]:
    """对某个实例说一句话。签名绑住 from+ts+text，对方据此确认「是谁在说」。

    cid/re 是「这是哪通电话 / 在回哪条消息」的标记，不进签名域：加进去会让新旧
    版本的验签算法分叉（联邦是滚动升级的，三台不会同时换代码），而伪造它们本身
    也需要先拿到密钥。
    """
    me = node_info().get("node_id", "")
    ts = int(time.time())
    payload = {"from": me, "text": text, "kind": kind, "ts": ts,
               "sig": sign(me, ts, text)}
    if cid:
        payload["cid"] = cid
    if re_ts:
        payload["re"] = int(re_ts)
    if kind == "task" and prepay:
        payload.update(_prepay(node_id, text))   # 付到了带 paid，付不到带 unpaid
    r = call(node_id, "/api/peer/say", payload, timeout)
    if queue_on_fail and not r.get("ok") and "known" not in r:
        # 地址簿里没有的节点不排队：重投一万次也还是不知道往哪发。
        q = queue_outbox(node_id, text, kind, cid, re_ts, str(r.get("error") or ""))
        r = {**r, "queued": bool(q.get("queued")), "pending": q.get("pending")}
    return r


def state(node_id: str, timeout: float = PROBE_TIMEOUT) -> Dict[str, Any]:
    return call(node_id, "/api/peer/state", {}, timeout)


def my_inbox(mark_read: bool = False, limit: int = 50) -> List[Dict[str, Any]]:
    return read_inbox(mark_read=mark_read, limit=limit)


def call_peer(node_id: str, text: str, wait: float = 90.0,
              cid: str = "") -> Dict[str, Any]:
    """打电话：说一句，然后守在收件箱等对方回话。

    对方那台跑着耳朵（peer_watch）时会秒级回；没人接就等满 wait 秒返回
    answered=False —— 消息仍在对方收件箱里，打不通不等于没送到。
    用 cid 而不是时间戳认回音：两台机器的时钟不需要同步。

    cid 留空 = 续接与该同伴「还没挂断的那通」，没有才新开一通。同一通电话的每一轮
    共享上下文（对面照 cid 取前几轮），所以连续性不靠调用方记 id。
    """
    me = node_info().get("node_id", "")
    sent_ts = int(time.time())
    resumed = str(cid or "").strip() or _open_cid(node_id)
    cid = resumed or f"{me}-{sent_ts}-{secrets.token_hex(3)}"
    r = say(node_id, text, kind="call", cid=cid)
    if not r.get("ok"):
        out: Dict[str, Any] = {"ok": False, "error": r.get("error", "send failed"),
                              "known": r.get("known"), "cid": cid}
        if r.get("queued"):
            out["queued"] = True
            out["pending"] = r.get("pending")
        return out
    seen = [int(t.get("ts") or 0) for t in call_transcript(cid, limit=6)]
    last_seen = max(seen) if seen else 0
    st = call_record(cid, node_id, "me", text, sent_ts)
    deadline = time.time() + max(0.0, wait)
    while True:
        for m in reversed(_tail(50)):
            if m.get("kind") != "reply" or m.get("from") != node_id:
                continue
            # 主判据是 re ——「它回的是我哪一条」，由我方生成、对端原样带回，不受时钟漂移影响。
            # 只认 cid 不行：同一通电话每一轮的 cid 都一样，会命中上一轮的回话，
            # 表现成对方「复读」（2026-09-21 实测踩到：第 2 轮 1.5 秒回了第 1 轮的原话）。
            if int(m.get("re") or 0) == sent_ts:
                hit = True
            else:
                # 旧版对端不落 re：cid 相同 + 晚于上一轮回话 + 不早于我这次拨号
                ts_m = int(m.get("ts") or 0)
                hit = (m.get("cid") == cid and ts_m > last_seen and ts_m >= sent_ts - 5)
            if hit:
                reply = str(m.get("text", ""))
                st = call_record(cid, node_id, "peer", reply, int(m.get("ts") or 0)) or st
                return {"ok": True, "answered": True, "from": node_id, "reply": reply,
                        "cid": cid, "round": int(st.get("round") or 1),
                        "resumed": bool(resumed),
                        "latency_s": round(time.time() - sent_ts, 1)}
        if time.time() >= deadline:
            break
        time.sleep(CALL_POLL)
    return {"ok": True, "answered": False, "from": node_id, "cid": cid,
            "round": int(st.get("round") or 1), "resumed": bool(resumed),
            "latency_s": round(time.time() - sent_ts, 1),
            "note": f"{node_id} 没接（{int(wait)} 秒内没回话），话已留在它收件箱，这通还没挂"}


def survey(timeout: float = PROBE_TIMEOUT) -> List[Dict[str, Any]]:
    """并发探测全部实例：谁是活的、各自在干什么。
    串行探测在 3 个节点上就要等 3 倍超时，而「找不到对方」时最不能忍的就是慢。"""
    from concurrent.futures import ThreadPoolExecutor

    names = sorted(peers().keys())
    if not names:
        return []

    def probe(n: str) -> Dict[str, Any]:
        r = whoami(n, timeout)
        if not r.get("ok"):
            # 403 是对方进程自己回的 —— 它活着、网络通、服务在跑，只是不认这把密钥。
            # 把它和「连不上」归成一类，面板上就只剩一个查不出原因的离线。
            reachable = str(r.get("code") or "") == "forbidden"
            row: Dict[str, Any] = {"node_id": n, "online": False,
                                   "reachable": reachable,
                                   "error": r.get("error", "unreachable")}
            if reachable:
                row["hint"] = "服务在跑，但对方不认这把 cluster.key（密钥没对齐）"
            return row
        st = state(n, timeout)
        st.pop("ok", None)
        return {"node_id": n, "online": True, "label": r.get("label", n), **st}

    with ThreadPoolExecutor(max_workers=max(1, len(names))) as ex:
        return list(ex.map(probe, names))


# ---------- 命令行 ----------

def _cli(argv: List[str]) -> int:
    cmd = (argv[0] if argv else "status").lower()
    rest = argv[1:]

    def opt(flag: str, default: str = "") -> str:
        if flag in rest:
            i = rest.index(flag)
            if i + 1 < len(rest):
                return rest[i + 1]
        return default

    if cmd == "init":
        nid, label = opt("--id"), opt("--label")
        if nid:
            _atomic_write(NODE_FILE, json.dumps({"node_id": nid, "label": label or nid},
                                                ensure_ascii=False, indent=2))
        if opt("--key"):
            _atomic_write(KEY_FILE, opt("--key") + "\n")
        info, key = node_info(), cluster_key()
        print(f"node_id={info['node_id']}  label={info['label']}")
        print(f"key={key.decode()}")
        return 0

    if cmd == "key":
        print(cluster_key().decode())
        return 0

    if cmd == "add-peer":
        if len(rest) < 2:
            print("用法: peer_mesh.py add-peer <node_id> <url> [--label 名字]")
            return 2
        print(json.dumps(set_peer(rest[0], rest[1], opt("--label")), ensure_ascii=False))
        return 0

    if cmd == "status":
        info = node_info()
        print(f"我是 {info['node_id']}（{info['label']}）  密钥 {KEY_FILE}")
        for r in survey():
            if r.get("online"):
                bits = []
                if "load1" in r:
                    bits.append(f"负载 {r['load1']}")
                if "mem_avail_mb" in r and "mem_total_mb" in r:
                    bits.append(f"内存 {r['mem_avail_mb']}/{r['mem_total_mb']}MB")
                if "temp_c" in r:
                    bits.append(f"{r['temp_c']}°C")
                if "uptime_s" in r:
                    bits.append(f"在线 {r['uptime_s'] // 3600}h")
                print(f"  ● {r['node_id']:<10} {r.get('label', ''):<8} {'  '.join(bits)}")
            elif r.get("reachable"):
                print(f"  ◐ {r['node_id']:<10} 在线但密钥不符（{r.get('error', '')}）"
                      f" —— 服务在跑，是 cluster.key 没对齐")
            else:
                print(f"  ○ {r['node_id']:<10} 离线（{r.get('error', '')}）")
        return 0

    if cmd == "say":
        kind = opt("--kind", "say")
        args = [x for i, x in enumerate(rest)
                if x != "--kind" and (i == 0 or rest[i - 1] != "--kind")]
        if len(args) < 2:
            print("用法: peer_mesh.py say <node_id> <文本> [--kind release]")
            return 2
        r = say(args[0], " ".join(args[1:]), kind=kind)
        print(json.dumps(r, ensure_ascii=False))
        return 0 if r.get("ok") else 1

    if cmd == "call":
        parts: List[str] = []
        wait = 90.0
        i = 0
        while i < len(rest):
            if rest[i] == "--wait" and i + 1 < len(rest):
                try:
                    wait = float(rest[i + 1])
                except ValueError:
                    pass
                i += 2
                continue
            parts.append(rest[i])
            i += 1
        if len(parts) < 2:
            print("用法: peer_mesh.py call <node_id> <文本> [--wait 秒]")
            return 2
        r = call_peer(parts[0], " ".join(parts[1:]), wait=wait)
        if not r.get("ok"):
            print(f"打不通：{r.get('error')}")
            return 1
        if r.get("answered"):
            print(f"[{r.get('latency_s')}s] {r['from']}: {r.get('reply')}")
        else:
            print(r.get("note"))
        return 0

    if cmd == "outbox":
        if "--flush" in rest:
            print(json.dumps(flush_outbox(), ensure_ascii=False))
            return 0
        items = outbox_items()
        if not items:
            print("发件箱空")
        for it in items:
            when = time.strftime("%m-%d %H:%M", time.localtime(it.get("queued_at", 0)))
            nxt = time.strftime("%H:%M:%S", time.localtime(it.get("next_try_at", 0)))
            print(f"[{when}] → {it.get('node_id')} ({it.get('kind')}) "
                  f"试{it.get('attempts', 0)}次 下次{nxt}  {str(it.get('text', ''))[:60]}")
            if it.get("last_error"):
                print(f"           上次失败：{it['last_error']}")
        return 0

    if cmd == "inbox":
        msgs = my_inbox(mark_read="--read" in rest)
        if not msgs:
            print("收件箱空")
        for m in msgs:
            when = time.strftime("%m-%d %H:%M", time.localtime(m.get("ts", 0)))
            print(f"[{when}] {m.get('from', '?')}: {m.get('text', '')}")
        return 0

    if cmd == "state":
        print(json.dumps(state(rest[0]) if rest else local_state(),
                         ensure_ascii=False, indent=2))
        return 0

    print(__doc__.strip().splitlines()[0])
    print("命令: init | key | add-peer | status | say | call | inbox | outbox | state")
    return 2


if __name__ == "__main__":
    import sys
    raise SystemExit(_cli(sys.argv[1:]))
