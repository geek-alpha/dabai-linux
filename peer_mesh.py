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

TS_WINDOW = 300          # 签名时间窗（秒）：足够容忍时钟漂移，又不足以让抓包重放
PROBE_TIMEOUT = 6.0      # 单实例探测超时
CALL_POLL = 0.4          # 等对方接电话的轮询间隔（秒）：读本地文件，代价约等于零
KEY_HEADER = "X-Phoenix-Key"

router = APIRouter(prefix="/api/peer", tags=["peer"])


# ---------- 存储层 ----------

def _atomic_write(path: Path, text: str, mode: int = 0o600) -> None:
    """tmp + rename 原子写：树莓派断电频繁，写一半的密钥文件会让整个集群失联。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
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
    _atomic_write(KEY_FILE, key + "\n")
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
            out.append(json.loads(line))
        except ValueError:
            continue
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
    tail = [ln for ln in lines[start:] if ln.strip()]
    items: List[Dict[str, Any]] = []
    for line in tail[-preview:]:
        try:
            items.append(json.loads(line))
        except ValueError:
            continue
    return {"count": len(tail), "items": items}


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


def accept_task(entry: Dict[str, Any]) -> Dict[str, Any]:
    """把一条 kind=task 消息翻译成一次性定时任务。任何失败都返回 dict，绝不抛异常。

    三道闸门按「代价从低到高」排：开关 → 危险模式 → 配额，全过才派单。
    """
    frm = str(entry.get("from") or "?")
    text = str(entry.get("text") or "").strip()[:TASK_CHAR_CAP]
    row: Dict[str, Any] = {"ts": int(time.time()), "from": frm, "text": text[:400]}

    if not text:
        row["result"] = "empty"
        _audit_task(row)
        return {"ok": False, "error": "空任务"}
    if not task_gate_enabled():
        row["result"] = "disabled"
        _audit_task(row)
        return {"ok": False, "error": "本机已关掉联邦派活（settings.json → peer.allow_remote_task=false）"}
    bad = dangerous_in(text)
    if bad:
        row["result"] = "blocked"
        row["reason"] = bad
        _audit_task(row)
        return {"ok": False, "blocked": True, "error": f"含不可逆动作「{bad}」，已拦下、没有自动执行"}
    if not _quota_take(frm):
        row["result"] = "quota"
        _audit_task(row)
        return {"ok": False, "error": f"{frm} 一小时内已派满 {TASK_QUOTA_PER_HOUR} 单，这条只记录不执行"}

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
        try:
            return json.loads(e.read().decode("utf-8"))
        except Exception:
            return {"ok": False, "error": f"HTTP {e.code}"}
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


def say(node_id: str, text: str, kind: str = "say",
        timeout: float = PROBE_TIMEOUT, cid: str = "", re_ts: int = 0) -> Dict[str, Any]:
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
    return call(node_id, "/api/peer/say", payload, timeout)


def state(node_id: str, timeout: float = PROBE_TIMEOUT) -> Dict[str, Any]:
    return call(node_id, "/api/peer/state", {}, timeout)


def my_inbox(mark_read: bool = False, limit: int = 50) -> List[Dict[str, Any]]:
    return read_inbox(mark_read=mark_read, limit=limit)


def call_peer(node_id: str, text: str, wait: float = 90.0) -> Dict[str, Any]:
    """打电话：说一句，然后守在收件箱等对方回话。

    对方那台跑着耳朵（peer_watch）时会秒级回；没人接就等满 wait 秒返回
    answered=False —— 消息仍在对方收件箱里，打不通不等于没送到。
    用 cid 而不是时间戳认回音：两台机器的时钟不需要同步。
    """
    me = node_info().get("node_id", "")
    sent_ts = int(time.time())
    cid = f"{me}-{sent_ts}-{secrets.token_hex(3)}"
    r = say(node_id, text, kind="call", cid=cid)
    if not r.get("ok"):
        return {"ok": False, "error": r.get("error", "send failed"), "known": r.get("known")}
    deadline = time.time() + max(0.0, wait)
    while True:
        for m in _tail(50):
            if m.get("kind") != "reply" or m.get("from") != node_id:
                continue
            # cid 是主判据；旧版服务端不落 cid（滚动升级期间），用「不早于我拨号」兜底
            if m.get("cid") == cid or int(m.get("ts") or 0) >= sent_ts - 5:
                return {"ok": True, "answered": True, "from": node_id,
                        "reply": m.get("text", ""), "cid": cid,
                        "latency_s": round(time.time() - sent_ts, 1)}
        if time.time() >= deadline:
            break
        time.sleep(CALL_POLL)
    return {"ok": True, "answered": False, "from": node_id, "cid": cid,
            "latency_s": round(time.time() - sent_ts, 1),
            "note": f"{node_id} 没接（{int(wait)} 秒内没回话），话已留在它收件箱"}


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
            return {"node_id": n, "online": False, "error": r.get("error", "unreachable")}
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
            else:
                print(f"  ○ {r['node_id']:<10} 离线（{r.get('error', '')}）")
        return 0

    if cmd == "say":
        if len(rest) < 2:
            print("用法: peer_mesh.py say <node_id> <文本>")
            return 2
        print(json.dumps(say(rest[0], " ".join(rest[1:])), ensure_ascii=False))
        return 0

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
    print("命令: init | key | add-peer | status | say | call | inbox | state")
    return 2


if __name__ == "__main__":
    import sys
    raise SystemExit(_cli(sys.argv[1:]))
