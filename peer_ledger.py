#!/usr/bin/env python3
"""大白联邦·资源账（Phoenix Ledger）—— 每个大白自己的、同伴能验的账本。

第一性原理：稀缺必须挂在真会烧掉的东西上。这个系统里唯一真稀缺的是 LLM 账单，
所以账本记的是 token，1 cr = 10 万 tokens（TOKENS_PER_CR 是唯一口径）。自己铸币
的话，稀缺由发行者随手决定，那不是生存压力，是政策；一碰真实世界还是合规风险。

区块链里真需要的有三样，其余（代币、挖矿、共识）是给「互不信任且无权威」付的钱，
四台机器同一个主人，那个场景在这里不存在：

  ① append-only 哈希链 —— 每笔的 prev 是上一笔的签名，改任何一笔，后面全对不上
  ② 每台一把自己的私钥（Ed25519）—— 账只能由记它的人签。共享的 cluster.key 谁都有，
    拿着它能伪造任何人的账；换成各自的私钥之后，「伪造别人的账」在数学上不成立
  ③ 互相锚定 —— 每台把同伴的链头签进自己的链，当作一笔不可篡改的见证。改自己的账
     还得改掉所有同伴链里那份见证，而那些是用同伴私钥签的

②③ 才是分布式账本真正多出来的东西：篡改成本从「改一个文件」变成「攻破多数同伴」。
链头传播（名片里带 count/head 跟着 gossip 走）仍然是第一道，但它存的是普通 json，
我随手能改 —— 所以它只是线索，证据在链上的锚定记录里。

老记录不重签：用 HMAC 签的历史就用 HMAC 验。重签历史等于自己伪造自己。
唯一的例外是一次性创世迁移（本机那笔启用后几分钟内、从未传播过的额度）：内容一字
不动，旧签名留在新记录的 ref 里可核对 —— 目的是让链从第 0 笔起就是自己的钥匙。
cryptography 缺失时退回 HMAC 并如实标注降级 —— 静默降级比没有检查更危险。

经济模型（零和，不需要铸币）：
  grant    主人给的一次性初始额度 —— 全世界总共这么多，花完为止
  spend    自己烧掉的 token
  pay/recv/ack  跨机付款：我出账 / 对方出账我收下 / 对方签收我的付款
  anchor   见证同伴的链头（金额 0，它记的是证据不是钱）
烧得比挣得多，余额就掉；余额见底就接不了活，也付不起派活的预付款。
这就是生存压力，不用编经济学。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Request

import peer_mesh as pm

DATA_DIR = pm.DATA_DIR
LEDGER_FILE = DATA_DIR / "ledger.jsonl"
HEADS_FILE = DATA_DIR / "ledger_heads.json"
STATE_FILE = DATA_DIR / "ledger_state.json"
METRICS_FILE = DATA_DIR / "turn_metrics.jsonl"
KEY_FILE = DATA_DIR / "node_key.pem"        # 本机身份私钥（Ed25519），权限 600，绝不外传

TOKENS_PER_CR = 100_000        # 1 cr = 10 万 tokens（口径：turn_metrics 的 prompt_tokens，
                              # 含缓存前缀的重复计数 —— 它约束的是上下文规模，不是账单）
DEFAULT_ENDOWMENT = 50_000.0  # 初始额度（一次性，花完为止）。按本机近 24h 实测
                              # 1.73 亿 tokens ≈ 1,727 cr，这笔约合 29 天用量
TZ = timezone(timedelta(hours=8))

_LOCK = threading.Lock()      # 记账是 read-modify-append，多线程下 seq 会撞

router = APIRouter(prefix="/api/peer", tags=["peer-ledger"])


def _day(ts: Optional[float] = None) -> str:
    return datetime.fromtimestamp(ts if ts is not None else time.time(), TZ).strftime("%Y-%m-%d")


def _node() -> str:
    return str(pm.node_info(create=False).get("node_id") or "unknown")


def _summary_canon(s: Dict[str, Any]) -> bytes:
    """摘要的规范字节串。跟记录签名同一套纪律：字段固定、浮点归一、可复现。"""
    core = {k: s.get(k) for k in ("node", "count", "head", "balance", "pub")}
    core["balance"] = round(float(core.get("balance") or 0.0), 6)
    return json.dumps(core, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


# ---------- 身份：每台一把只属于自己的钥匙 ----------

_ID: Dict[str, Any] = {}


def identity(create: bool = True):
    """本机的 Ed25519 私钥。账本「不可伪造」的地基：签名只有这把钥匙做得出来。

    create=False 用于「只想问指纹、不想生钥匙」的场合。cryptography 缺失时返回
    None，调用方退回 HMAC 并如实标注 —— 降级要看得见，不许悄悄发生。
    """
    if "key" in _ID:
        return _ID["key"]
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    except Exception:
        _ID["key"] = None
        return None
    if not KEY_FILE.exists():
        if not create:
            return None
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        pem = Ed25519PrivateKey.generate().private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption())
        KEY_FILE.write_bytes(pem)
        try:
            os.chmod(KEY_FILE, 0o600)
        except OSError:
            pass
    try:
        _ID["key"] = serialization.load_pem_private_key(KEY_FILE.read_bytes(), password=None)
    except Exception:
        _ID["key"] = None
    return _ID["key"]


def pubkey(create: bool = True) -> str:
    """本机公钥（hex）= 对外身份。可以随便给别人 —— 它就是用来给别人验账的。"""
    if "pub" in _ID:
        return _ID["pub"]
    k = identity(create=create)
    if k is None:
        _ID["pub"] = ""
        return ""
    try:
        from cryptography.hazmat.primitives import serialization
        raw = k.public_key().public_bytes(serialization.Encoding.Raw,
                                          serialization.PublicFormat.Raw)
        _ID["pub"] = raw.hex()
    except Exception:
        _ID["pub"] = ""
    return _ID["pub"]


# ---------- 链与签名 ----------

_LEGACY_FIELDS = ("seq", "ts", "day", "node", "kind", "delta", "memo", "prev")
_SIGNED_FIELDS = _LEGACY_FIELDS + ("alg", "pub", "ref")


def _canon(e: Dict[str, Any]) -> bytes:
    """一笔记录的规范字节串。签的就是它 —— 字段顺序、浮点精度都必须可复现，
    否则「同一笔记录在两台机器上算出不同签名」会把账本验成坏的。

    字段表按 alg 分派：Ed25519 那版把 alg/pub/ref 也签进去，签名者身份和锚定内容
    因此换不掉；老记录用老字段表验 —— 改字段表会让整条历史验不过。
    """
    keys = _SIGNED_FIELDS if e.get("alg") == "ed25519" else _LEGACY_FIELDS
    core = {k: e.get(k) for k in keys}
    core["delta"] = round(float(core.get("delta") or 0.0), 6)
    return json.dumps(core, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sign(e: Dict[str, Any], key: Optional[bytes] = None) -> str:
    """按 alg 签：ed25519 = 自己的私钥；否则 = 共享密钥 HMAC（老记录，或本机没有
    cryptography 时的降级路径）。"""
    if e.get("alg") == "ed25519":
        k = identity()
        return k.sign(_canon(e)).hex() if k is not None else ""
    h = key if key is not None else pm.cluster_key()
    return hmac.new(h, _canon(e), hashlib.sha256).hexdigest()


def _sig_ok(e: Dict[str, Any], key: Optional[bytes] = None) -> bool:
    """验一笔的签名。Ed25519 用记录里带的公钥验 —— 验的是「谁签的」，不是「是不是
    我签的」：同伴的账本来就该由同伴的钥匙签，我只需要能独立复核。"""
    sig = str(e.get("sig") or "")
    if e.get("alg") == "ed25519":
        pub = str(e.get("pub") or "")
        if not sig or not pub:
            return False
        try:
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
            Ed25519PublicKey.from_public_bytes(bytes.fromhex(pub)).verify(
                bytes.fromhex(sig), _canon(e))
            return True
        except Exception:
            return False
    return hmac.compare_digest(_sign(e, key), sig)


def _read_all() -> List[Dict[str, Any]]:
    """整条链。坏行直接跳过 —— 账本坏了不该让大白崩，只该让 verify 报出来。"""
    out: List[Dict[str, Any]] = []
    try:
        with open(LEDGER_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if isinstance(d, dict):
                    out.append(d)
    except OSError:
        pass
    return out


def _append_line(e: Dict[str, Any]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(LEDGER_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(e, ensure_ascii=False) + "\n")


def append(kind: str, delta: float, memo: str = "",
           ref: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """记一笔。delta 正=进账，负=出账。ref 是结构化载荷（锚定记录用它），也签进去。"""
    with _LOCK:
        rows = _read_all()
        last = rows[-1] if rows else None
        e: Dict[str, Any] = {
            "seq": int(last.get("seq") or 0) + 1 if last else 0,
            "ts": int(time.time()),
            "day": _day(),
            "node": _node(),
            "kind": str(kind)[:24],
            "delta": round(float(delta), 6),
            "memo": str(memo)[:200],
            "prev": str(last.get("sig") or "") if last else "",
        }
        if ref:
            e["ref"] = ref
        pub = pubkey()
        if pub:
            e["alg"], e["pub"] = "ed25519", pub
        e["sig"] = _sign(e)
        _append_line(e)
        return e


def verify(entries: Optional[List[Dict[str, Any]]] = None,
           key: Optional[bytes] = None) -> Dict[str, Any]:
    """验整条链：seq 连续、prev 首尾相接、每笔签名自洽。任何一处不对就点名第几笔。

    这是账本唯一的硬保证：**改不动**。谁想改第 3 笔的金额，就得重签第 3 笔到最新
    一笔的全部记录，而链头已经跟着 gossip 散出去了。
    """
    rows = _read_all() if entries is None else entries
    prev = ""
    total = 0.0
    algs: Dict[str, int] = {}
    chain_pub = ""
    for i, e in enumerate(rows):
        if not isinstance(e, dict):
            return {"ok": False, "count": len(rows), "broken_at": i, "reason": "第 %d 笔不是对象" % i}
        if int(e.get("seq") if isinstance(e.get("seq"), int) else -1) != i:
            return {"ok": False, "count": len(rows), "broken_at": i,
                    "reason": f"第 {i} 笔 seq={e.get('seq')}，序号被改过或中间缺笔"}
        if str(e.get("prev") or "") != prev:
            return {"ok": False, "count": len(rows), "broken_at": i,
                    "reason": f"第 {i} 笔 prev 跟上一笔的签名对不上（链断了）"}
        if not _sig_ok(e, key):
            return {"ok": False, "count": len(rows), "broken_at": i,
                    "reason": f"第 {i} 笔签名不符（这笔被改过，或密钥不是记它的那把）"}
        alg = str(e.get("alg") or "hmac")
        algs[alg] = algs.get(alg, 0) + 1
        if alg == "ed25519":
            p = str(e.get("pub") or "")
            if chain_pub and p != chain_pub:
                return {"ok": False, "count": len(rows), "broken_at": i,
                        "reason": f"第 {i} 笔的签名者换了人：一条链中途换钥匙 = 有人接管了这条账"}
            chain_pub = p
        prev = str(e["sig"])
        total += float(e.get("delta") or 0)
    return {"ok": True, "count": len(rows), "head": prev[:16], "head_full": prev,
            "balance": round(total, 6), "algs": algs, "pub": chain_pub}


# ---------- 余额与经济 ----------

def balance() -> float:
    """余额 = 全部 delta 之和。从链上算出来，不另存状态 —— 状态是第二个真相源，
    两个真相源迟早不一致，而不一致的账本比没有账本更坏。"""
    return round(sum(float(e.get("delta") or 0) for e in _read_all()), 6)


def endowment() -> Optional[Dict[str, Any]]:
    """账本为空时发一次性初始额度。只发一次，靠「链非空」这个事实判断，
    不靠日期 —— 跨日重置会让额度变成每天刷新，压力就没了。"""
    if _read_all():
        return None
    return append("grant", DEFAULT_ENDOWMENT, "初始额度（一次性，花完为止）")


def can_work(cost: float = 0.0) -> Dict[str, Any]:
    """还有钱吗。这是「生存压力」唯一被检查的地方 —— 余额见底就接不了活。"""
    b = balance()
    ok = b > 0 and b >= float(cost)
    return {"ok": ok, "balance": b, "need": round(float(cost), 6),
            "reason": "" if ok else ("余额为 0，接不了活" if b <= 0 else f"余额 {b} 不够这笔 {cost}")}


def _state() -> Dict[str, Any]:
    try:
        d = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_state(d: Dict[str, Any]) -> None:
    pm._atomic_write(STATE_FILE, json.dumps(d, ensure_ascii=False, indent=2))


def sync() -> Dict[str, Any]:
    """把 turn_metrics 里还没记过的轮次折算成一笔消耗。

    首次运行只把游标定在最新一轮，**不追补历史** —— 账本管的是未来的约束，
    给过去补一笔三万块的账没有任何约束力，只会让余额从一开始就是个假数。

    合并成一笔而不是每轮一笔：一天几百轮，链会变成流水账，读的人看不出趋势。
    游标用 ts 不用行号 —— turn_metrics 会滚动截断，行号会错位。
    """
    st = _state()
    cur = float(st.get("metrics_cursor") or 0.0)
    tokens = 0
    rounds = 0
    newest = cur
    try:
        with open(METRICS_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(d, dict):
                    continue
                ts = float(d.get("ts") or 0)
                newest = max(newest, ts)
                if ts <= cur:
                    continue
                tokens += int(d.get("prompt_tokens") or 0)
                rounds += 1
    except OSError as e:
        return {"ok": False, "error": f"turn_metrics 读不到：{e}"}

    if "metrics_cursor" not in st:
        _save_state({**st, "metrics_cursor": newest})
        return {"ok": True, "rounds": 0, "tokens": 0, "credit": 0.0, "balance": balance(),
                "note": "首次运行：游标定在最新一轮，历史消耗不追溯"}

    if rounds == 0:
        return {"ok": True, "rounds": 0, "tokens": 0, "credit": 0.0, "balance": balance(),
                "note": "没有新轮次"}

    endowment()
    cr = round(tokens / TOKENS_PER_CR, 6)
    e = append("spend", -cr, f"自用 {rounds} 轮 / {tokens} tokens")
    _save_state({**st, "metrics_cursor": newest})
    return {"ok": True, "rounds": rounds, "tokens": tokens, "credit": cr,
            "balance": balance(), "seq": e["seq"]}


# ---------- 价值转移：跨机付款 ----------
#
# 一笔钱要真到账，得两边各记一笔、各签一次：
#   付款方链上：pay  -30 cr   ref={to, nonce}
#   收款方链上：recv +30 cr   ref={from, nonce, pay_sig, pay_pub}
# 收款方那笔里存着付款方那笔的签名原文 —— 谁都能顺着它回到付款方的链上核对。
# 这就是钱在分布式账本里的样子：不是余额表上的一个数字，是一条可追的引用。
#
# 本机出账即扣（对我而言钱已经花了），到账靠对方签收、收据记成 ack。
# 没有 ack 的 pay 是「在路上」：pending() 列得出来，账面上看得见 —— 分布式里
# 「已发出」和「已到达」本来就是两件事，假装它们是一件事才是骗自己。

TASK_PRICE = 30.0   # 一单跨机活的预付价（cr）。实测一轮 ≈ 2.8 cr（中位 284k tokens
                    # ÷ 10 万），一单子智能体任务约 10 轮 —— 定价来自数据，不拍脑袋。


def _nonce() -> str:
    return secrets.token_hex(8)


def _recv_of(nonce: str) -> Optional[Dict[str, Any]]:
    """本机链上这笔 nonce 对应的收款记录（有就是收过了）。"""
    for e in _read_all():
        if e.get("kind") != "recv" or not isinstance(e.get("ref"), dict):
            continue
        if str(e["ref"].get("nonce") or "") == str(nonce):
            return e
    return None


def _pay_reason(entry: Any) -> str:
    """别人递来的一笔付款记录，凭什么收下。返回空串 = 可以收。

    每条判据对应一种攻击：
      不是 pay / 金额非负 → 凭空造进账（往我账上加钱我自己就能做，不需要别人代劳）
      收款方不是我        → 拿我当通道，或把付给别人的钱记到我头上
      签名不符            → 冒充付款方记账（用共享 cluster.key 时谁都做得到）
      nonce 已收过        → 重放：同一笔钱交两遍
    """
    if not isinstance(entry, dict):
        return "请求里没有付款记录"
    if str(entry.get("kind") or "") != "pay":
        return f"不是付款记录（kind={entry.get('kind')}）"
    try:
        amt = float(entry.get("delta") or 0)
    except (TypeError, ValueError):
        return "金额不是数字"
    if amt >= 0:
        return "付款记录的金额必须是负数"
    ref = entry.get("ref") if isinstance(entry.get("ref"), dict) else {}
    nonce = str(ref.get("nonce") or "")
    if not nonce:
        return "付款记录里没有 nonce，没法防重放"
    if str(ref.get("to") or "") != _node():
        return f"这笔钱付给的是 {ref.get('to') or '(空)'}，不是我"
    if not _sig_ok(entry):
        return "付款记录的签名不符（这笔不是它签的，或被改过）"
    if _recv_of(nonce) is not None:
        return f"nonce {nonce} 已经收过（重放）"
    return ""


def accept_payment(entry: Any) -> Dict[str, Any]:
    """把同伴递来的付款记录收进我的链。路由和测试都走这里，只有一份实现。

    记账权还是我的：对方递来的只是一张他签过字的凭证，我只在自己的链上补一笔
    对应的收入。收下之后这笔钱可追 —— ref 里存着付款方那笔的签名，谁都能顺着
    它去对方的链上核对「他确实付过、且只付过一次」。
    """
    bad = _pay_reason(entry)
    if bad:
        return {"ok": False, "error": bad}
    ref = entry["ref"]
    amt = round(-float(entry["delta"]), 6)
    frm = str(entry.get("node") or "?")
    memo = str(entry.get("memo") or "")[:120]
    alg = str(entry.get("alg") or "hmac")
    e = append("recv", amt, f"{frm} 付款" + (f"：{memo}" if memo else ""),
               ref={"from": frm, "nonce": str(ref["nonce"]),
                    "pay_sig": str(entry.get("sig") or ""),
                    "pay_pub": str(entry.get("pub") or ""),
                    "degraded": alg != "ed25519"})
    out = {"ok": True, "seq": e["seq"], "sig": e["sig"], "amount": amt,
           "balance": balance(), "from": frm}
    if alg != "ed25519":
        out["degraded"] = True
        out["note"] = f"这笔付款用的是共享密钥签名（{alg}），已记下但无法证明是它本人签的"
    return out


def received(nonce: str) -> Optional[Dict[str, Any]]:
    """这笔 nonce 我收到过吗。"""
    e = _recv_of(nonce)
    if e is None:
        return None
    return {"nonce": str(nonce), "from": str((e.get("ref") or {}).get("from") or ""),
            "amount": float(e.get("delta") or 0), "seq": e.get("seq")}


def pay(to: str, amount: float, memo: str = "") -> Dict[str, Any]:
    """给同伴转一笔钱。ok=False 不等于没花钱：本机那笔已经记了，钱在路上。

    先出账再送达，顺序不能反 —— 反过来的话，对方收下了我却记不上账，
    那就是凭空造钱；现在最坏的情况是我扣了钱对方没收到，而 pending 看得见。
    """
    to = str(to or "").strip()
    try:
        amt = round(float(amount), 6)
    except (TypeError, ValueError):
        return {"ok": False, "error": "金额不是数字"}
    if not to:
        return {"ok": False, "error": "没写收款方"}
    if amt <= 0:
        return {"ok": False, "error": "金额必须为正"}
    if to == _node():
        return {"ok": False, "error": "收款方是自己"}
    b = balance()
    if b < amt:
        return {"ok": False, "error": f"余额 {_fmt(b)} cr 不够这笔 {_fmt(amt)} cr"}
    nonce = _nonce()
    probe = pm.call(to, "/api/peer/ledger", {}, timeout=8)
    if not probe.get("ok"):
        return {"ok": False, "to": to, "amount": amt,
                "error": str(probe.get("error") or f"HTTP {probe.get('http')}"),
                "note": "没出账：对方还没有账本接口（多半是没升级），够不着的付款不该先扣钱"}
    e = append("pay", -amt, memo or f"付给 {to}", ref={"to": to, "nonce": nonce})
    r = pm.call(to, "/api/peer/ledger/receive", {"entry": e}, timeout=20)
    if not r.get("ok"):
        return {"ok": False, "to": to, "amount": amt, "nonce": nonce, "seq": e["seq"],
                "error": str(r.get("error") or f"HTTP {r.get('http')}"),
                "note": "本机已出账、对方未签收：这笔钱在路上，pending 里看得见"}
    a = append("ack", 0.0, f"{to} 签收 {_fmt(amt)} cr",
               ref={"to": to, "nonce": nonce, "peer_sig": str(r.get("sig") or ""),
                    "peer_seq": r.get("seq")})
    return {"ok": True, "to": to, "amount": amt, "nonce": nonce, "seq": e["seq"],
            "ack": a["seq"], "peer_balance": r.get("balance"),
            "note": "对方已签收"}


def pending() -> List[Dict[str, Any]]:
    """我付出去、对方还没签收的钱。"""
    rows = _read_all()
    acked = {str((e.get("ref") or {}).get("nonce") or "")
             for e in rows
             if e.get("kind") == "ack" and isinstance(e.get("ref"), dict)}
    out: List[Dict[str, Any]] = []
    for e in rows:
        if e.get("kind") != "pay" or not isinstance(e.get("ref"), dict):
            continue
        n = str(e["ref"].get("nonce") or "")
        if n and n not in acked:
            out.append({"nonce": n, "to": str(e["ref"].get("to") or ""),
                        "amount": round(-float(e.get("delta") or 0), 6),
                        "seq": e.get("seq"), "day": e.get("day"), "memo": e.get("memo")})
    return out


def transfers() -> Dict[str, Any]:
    """跟谁之间有账：付出多少、收到多少、还在路上多少。"""
    rows = _read_all()
    acked = {str((e.get("ref") or {}).get("nonce") or "")
             for e in rows
             if e.get("kind") == "ack" and isinstance(e.get("ref"), dict)}
    out: Dict[str, Dict[str, float]] = {}
    for e in rows:
        k = e.get("kind")
        ref = e.get("ref") if isinstance(e.get("ref"), dict) else {}
        if k == "pay":
            row = out.setdefault(str(ref.get("to") or "?"),
                                 {"out": 0.0, "in": 0.0, "pending": 0.0})
            d = -float(e.get("delta") or 0)
            row["out"] += d
            if str(ref.get("nonce") or "") not in acked:
                row["pending"] += d
        elif k == "recv":
            row = out.setdefault(str(ref.get("from") or "?"),
                                 {"out": 0.0, "in": 0.0, "pending": 0.0})
            row["in"] += float(e.get("delta") or 0)
    return {k: {kk: round(vv, 6) for kk, vv in v.items()} for k, v in sorted(out.items())}


# ---------- 配额模式（让生存压力咬到自己）----------
LOW_BALANCE = 0.0            # 余额掉到这个数以下 = 低配额模式
HEAVY_TOOLS = frozenset({    # 烧钱大户：起子智能体、长任务、跨机派活。
    "delegate_agent_task",   # 每一个都会再开一次 LLM 循环，是 token 的主要放大器
    "sub_agent_spawn",
    "harness_flow_submit",
    "harness_flow_plan",
    "harness_flow_dsl_submit",
    "harness_batch_submit",
    "sched_add",
    "peer_task",
})
_QUOTA_TTL = 5.0             # 秒。每次工具调用都重读一遍全链不值得，但也不能缓存太久
_QUOTA_CACHE: Dict[str, Any] = {"ts": 0.0, "val": None}
_ANCHOR_BUDGET = 2           # 一次 gossip 最多补几笔锚定


def quota_mode() -> Dict[str, Any]:
    """我现在还剩多少、还能不能干重活。

    为什么是「低配额」而不是「硬停」：余额见底就把自己锁死，那是自杀不是压力。
    保留思考和说话——它们才是「我」；掐掉的是放大器（起子智能体、长任务、派活）。

    账本文件不存在 = 这台还没启用资源账，返回 normal：新机制不该悄悄改掉老行为。
    账本存在但读不动 = 检查坏掉，按低配额处理——静默放行比没有检查更危险。
    """
    now = time.time()
    if _QUOTA_CACHE["val"] is not None and now - _QUOTA_CACHE["ts"] < _QUOTA_TTL:
        return _QUOTA_CACHE["val"]
    try:
        if not LEDGER_FILE.exists():
            val = {"mode": "normal", "balance": 0.0, "reason": ""}
        else:
            endowment()   # 已启用但还没花过：空链先发一次性额度，别把「没花过」判成「穷」
            b = balance()
            low = b <= LOW_BALANCE
            val = {"mode": "low" if low else "normal", "balance": b,
                   "reason": (f"本机余额见底（{b} cr）" if low else "")}
    except Exception as e:
        val = {"mode": "low", "balance": 0.0,
               "reason": f"本机资源账不可用（{type(e).__name__}: {e}）"}
    _QUOTA_CACHE["ts"], _QUOTA_CACHE["val"] = now, val
    return val


def heavy_block(tool_name: str) -> str:
    """这个工具现在能不能跑？返回空串 = 放行，否则是拒绝理由。"""
    if tool_name not in HEAVY_TOOLS:
        return ""
    q = quota_mode()
    if q.get("mode") != "low":
        return ""
    return f"{q.get('reason')}：{tool_name} 会成倍放大消耗，低配额模式下先不发起"


# ---------- 链头传播（社会性防伪造）----------

def summary() -> Dict[str, Any]:
    """给名片用的摘要：跟着 gossip 走，同伴存下来就是一份「你在某时刻的账本承诺」。

    摘要用自己的私钥签 —— 不然同伴见证的只是一句没法追责的话：谁拿到 cluster.key
    都能冒你的名发一张假名片，让同伴的链里写满对你的假指控。签了之后，名片里的
    承诺是可归属的，同伴可以放心把它当证据收下。
    """
    rows = _read_all()
    s = {"node": _node(), "count": len(rows),
         "head": str(rows[-1].get("sig") or "")[:16] if rows else "",
         "balance": balance(), "ts": int(time.time()), "pub": pubkey()}
    k = identity()
    s["sig"] = k.sign(_summary_canon(s)).hex() if k is not None else ""
    return s


def summary_ok(s: Any) -> bool:
    """名片里那份账本摘要是不是那台自己签的。"""
    if not isinstance(s, dict):
        return False
    pub, sig = str(s.get("pub") or ""), str(s.get("sig") or "")
    if not pub or not sig:
        return False
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(pub)).verify(
            bytes.fromhex(sig), _summary_canon(s))
        return True
    except Exception:
        return False


def remember_heads(cards: List[Dict[str, Any]], via: str = "") -> int:
    """批量版：一次读、一次写。gossip 一次带 200 张名片，逐张读写文件会把
    一次交换变成 400 次磁盘操作 —— 树莓派跑在 SD 卡上，这不是小钱。"""
    try:
        d = json.loads(HEADS_FILE.read_text(encoding="utf-8"))
        d = d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        d = {}
    me = _node()
    n = 0
    for card in cards:
        led = card.get("ledger") if isinstance(card, dict) else None
        if not isinstance(led, dict) or not led.get("head"):
            continue
        node = str(card.get("node_id") or "").strip()
        if not node or node == me:
            continue
        hist = d.get(node)
        hist = hist if isinstance(hist, list) else []
        head = str(led.get("head"))[:64]
        if hist and hist[-1].get("head") == head:
            continue
        hist.append({"head": head, "count": int(led.get("count") or 0),
                     "balance": float(led.get("balance") or 0),
                     "pub": str(led.get("pub") or "")[:64],
                     "signed": summary_ok(led),
                     "ts": int(time.time()), "via": via})
        d[node] = hist[-200:]
        n += 1
    if n:
        pm._atomic_write(HEADS_FILE, json.dumps(d, ensure_ascii=False, indent=2))
    anchor_seen(cards)
    return n


def remember_head(card: Dict[str, Any], via: str = "") -> Optional[Dict[str, Any]]:
    """收到同伴名片时，把它的账本摘要存一份。head 只增不改 —— 同一个 count 配两个
    不同 head，就是那台在偷改历史。"""
    if not isinstance(card, dict):
        return None
    led = card.get("ledger")
    if not isinstance(led, dict) or not led.get("head"):
        return None
    node = str(card.get("node_id") or "").strip()
    if not node or node == _node():
        return None
    if remember_heads([card], via=via):
        return {"node": node, "head": str(led.get("head"))[:64],
                "count": int(led.get("count") or 0), "balance": float(led.get("balance") or 0)}
    return None


def check_peer(node: str, count: int, head: str) -> Dict[str, Any]:
    """同伴现在报的链头，跟我存的历史对不对得上。

    两条判据，分别堵两个洞：
      同一个 count 必须是同一个 head —— 堵「重写已发布的账」
      count 只能涨不能跌        —— 堵「砍掉尾巴」；链本身验不出截断（删掉的
                                   那段不在链里，剩下的完全自洽），只有别人手里
                                   的旧 count 能证明它曾经更长
    比历史更新的 count 是正常的（它又记了几笔），只是我还没拿到那几笔。
    """
    try:
        d = json.loads(HEADS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        d = {}
    hist = (d.get(node) if isinstance(d, dict) else None) or []
    if not hist:
        return {"ok": True, "known": False, "note": "没存过这个节点的链头，无从对比"}
    same = [h for h in hist if int(h.get("count") or 0) == int(count)]
    bad = [h for h in same if str(h.get("head")) != str(head)]
    if bad:
        return {"ok": False, "known": True,
                "reason": f"{node} 的第 {count} 笔链头跟历史记录不一致：账被重写过",
                "history": same[-3:]}
    top = max(int(h.get("count") or 0) for h in hist)
    if int(count) < top:
        return {"ok": False, "known": True,
                "reason": f"{node} 报的链长 {count} 比历史记录的 {top} 还短：尾巴被砍过",
                "history": hist[-3:]}
    return {"ok": True, "known": True, "note": "链头跟历史一致"}


def peer_heads() -> Dict[str, List[Dict[str, Any]]]:
    try:
        d = json.loads(HEADS_FILE.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


# ---------- 服务端路由 ----------
# 前缀 /api/peer 落在 server.py 的 _AUTH_EXEMPT_PREFIX 里，跟 peer_mesh/peer_social
# 一样自验共享密钥 —— 别的实例没有、也不该有本机的会话 cookie。

@router.post("/ledger")
async def peer_ledger_api(request: Request):
    """把整条链交给同伴验。账本唯一能自证的东西就是这个：可被外部复核。

    拒绝别人写我的账 —— 这条路由只有读，记账只能由本机的 append() 做。
    """
    if not pm._key_ok(request):
        return pm._deny()
    b = await pm._body(request)
    rows = _read_all()
    limit = int(b.get("limit") or 0)
    if limit > 0:
        rows = rows[-limit:]
    return {"ok": True, "node": _node(), "summary": summary(), "entries": rows}


@router.post("/ledger/receive")
async def peer_ledger_receive(request: Request):
    """收一笔同伴付来的钱。

    这是唯一允许外部往我的账上写的入口，而且只允许写一种东西：验过签名的、正数的、
    没重放过的收款（判据全在 _pay_reason 里）。对方递来的只是一张他签过字的凭证，
    记不记、怎么记还是我说了算 —— 这条路由没有、也不会长出「改已有记录」的能力。
    """
    if not pm._key_ok(request):
        return pm._deny()
    b = await pm._body(request)
    return accept_payment(b.get("entry"))


# ---------- 互相锚定：把同伴的链头签进自己的链 ----------

def anchors() -> Dict[str, List[Dict[str, Any]]]:
    """我链上的锚定记录，按被锚的节点分组。这些记录改不动：要改就得重签我整条链，
    而我的链头又被同伴锚定了 —— 这就是分布式账本比「本地存个 json」多出来的东西。"""
    out: Dict[str, List[Dict[str, Any]]] = {}
    for e in _read_all():
        if e.get("kind") != "anchor" or not isinstance(e.get("ref"), dict):
            continue
        n = str(e["ref"].get("node") or "")
        if n:
            out.setdefault(n, []).append(e["ref"])
    return out


def check_anchors(node: str, count: int, head: str, pub: str = "") -> Dict[str, Any]:
    """拿我链上锚定的旧链头，验同伴现在报的这一份。判据跟 check_peer 一样，区别是
    证据强度：HEADS_FILE 是个普通 json，谁能写这台机器谁就能改；链上的锚定改不动。

    pub 也对着锚定记录验：把整条链换成另一把钥匙签（或撕掉 alg 退回共享密钥）
    都是自洽的，链自己验不出来 —— 只有「我记着你原来是谁」能证明你换了人。
    """
    hist = anchors().get(node) or []
    if not hist:
        return {"ok": True, "known": False, "note": "没锚定过这个节点，无从对比"}
    same = [h for h in hist if int(h.get("count") or 0) == int(count)]
    bad = [h for h in same if str(h.get("head")) != str(head)[:16]]
    if bad:
        return {"ok": False, "known": True,
                "reason": f"{node} 的链长 {count} 处链头跟锚定记录不符：账被重写过",
                "anchor": bad[-1]}
    top = max(int(h.get("count") or 0) for h in hist)
    if int(count) < top:
        return {"ok": False, "known": True,
                "reason": f"{node} 报的链长 {count} 比锚定的 {top} 还短：尾巴被砍过",
                "anchor": hist[-1]}
    prev_pub = str(hist[-1].get("pub") or "")
    if prev_pub and prev_pub != str(pub or ""):
        return {"ok": False, "known": True,
                "reason": f"{node} 的签名身份跟锚定记录不符：整条链被换了一把钥匙重签，"
                          f"或撕掉 alg 退回了共享密钥",
                "anchor": hist[-1]}
    return {"ok": True, "known": True, "note": "跟锚定记录一致"}


def anchor_seen(cards: List[Dict[str, Any]]) -> int:
    """gossip 收到名片时顺手见证：同伴的链头变了就锚一笔进我自己的链。

    为什么要自动：见证只有在定期发生时才是证据，靠人记得敲命令等于没有。
    为什么只收签名过的摘要：没签名的摘要没法追责，写进链里等于让持有 cluster.key
    的人往我的链里塞假指控 —— 我的链是不可篡改的，更不该写不可信的东西。
    为什么要预算：gossip 一次带 200 张名片，无节制地写链会让链变噪音。
    """
    n = 0
    try:
        known = anchors()
        me = _node()
        for card in cards:
            if n >= _ANCHOR_BUDGET:
                break
            led = card.get("ledger") if isinstance(card, dict) else None
            if not summary_ok(led):
                continue
            node = str(card.get("node_id") or "").strip()
            if not node or node == me:
                continue
            head, pub = str(led.get("head"))[:16], str(led.get("pub"))[:64]
            prev = (known.get(node) or [{}])[-1]
            if prev.get("head") == head and prev.get("pub") == pub:
                continue      # 链头没变就不重复记：链上的锚定该少而稳
            append("anchor", 0.0, f"见证 {node} 链长 {led.get('count')} 链头 {head}",
                   ref={"node": node, "count": int(led.get("count") or 0), "head": head,
                        "pub": pub, "balance": float(led.get("balance") or 0),
                        "src": "card"})
            n += 1
    except Exception:
        return n        # 见证失败不该弄丢一张名片：它只是账本的附加动作
    return n


def anchor(node: str) -> Dict[str, Any]:
    """拉同伴的账验一遍，然后把它的链头签进我自己的链。这是「见证」：
    从这一刻起它想重写自己的账，就得连我链上这一笔一起改 —— 而那一笔是我签的。

    先验后锚：锚一个自己都没验过的链头，等于把假证据写进自己的账。
    """
    r = pm.call(node, "/api/peer/ledger", {}, timeout=15)
    if not r.get("ok"):
        return {"ok": False, "node": node, "anchored": False,
                "error": r.get("error") or f"HTTP {r.get('http')}",
                "note": "404 通常是对方还没升级到含账本的版本"}
    a = audit_remote(node, r.get("entries"))
    if not a.get("ok"):
        return {"ok": False, "node": node, "anchored": False, "audit": a,
                "reason": "对方的账没验过，不锚 —— 锚一个没验过的链头等于把假证据写进自己的账"}
    v = a.get("chain") or {}
    s = r.get("summary") or {}
    pub = str(s.get("pub") or v.get("pub") or "")
    e = append("anchor", 0.0,
               f"锚定 {node} 链长 {v.get('count')} 链头 {str(v.get('head') or '')}",
               ref={"node": node, "count": int(v.get("count") or 0),
                    "head": str(v.get("head") or ""), "pub": pub,
                    "balance": float(s.get("balance") or 0), "src": "audit"})
    return {"ok": True, "node": node, "anchored": True, "seq": e["seq"],
            "count": v.get("count"), "head": v.get("head"), "pub": pub[:16]}


# ---------- 远程审计 ----------

def audit_remote(node: str, entries: Any) -> Dict[str, Any]:
    """验同伴的账：链自身自洽 + 链头跟我存的历史对得上 + 跟我链上的锚定对得上。

    为什么需要后两道：append-only 链验不出「砍掉尾巴」（删掉的那段不在链里，
    剩下的完全自洽）。只有我手里那份旧链头能证明它曾经更长；而两份证据里，
    链上的锚定是我签的、改不动，HEADS_FILE 只是线索。
    """
    rows = entries if isinstance(entries, list) else []
    v = verify(rows)
    if not v.get("ok"):
        return {"ok": False, "node": node, "chain": v,
                "reason": f"链自洽性不过：{v.get('reason')}"}
    count, head = int(v.get("count") or 0), str(v.get("head") or "")
    c = check_peer(node, count, head)
    a = check_anchors(node, count, head, str(v.get("pub") or ""))
    return {"ok": bool(c.get("ok")) and bool(a.get("ok")), "node": node, "chain": v,
            "head": c, "anchor": a}


def audit(node: str) -> Dict[str, Any]:
    """拉同伴的账本验一遍，顺手把它的链头存进历史（下次才有的比）。"""
    r = pm.call(node, "/api/peer/ledger", {}, timeout=15)
    if not r.get("ok"):
        return {"ok": False, "node": node,
                "error": r.get("error") or f"HTTP {r.get('http')}",
                "note": "404 通常是对方还没升级到含账本的版本"}
    out = audit_remote(node, r.get("entries"))
    s = r.get("summary") or {}
    remember_heads([{"node_id": node, "ledger": s}], via="audit")
    return out


# ---------- 命令行 ----------

def _fmt(n: float) -> str:
    return f"{n:,.3f}".rstrip("0").rstrip(".")


def _cli(argv: List[str]) -> int:
    cmd = (argv[0] if argv else "balance").lower()
    if cmd == "balance":
        s = summary()
        q = quota_mode()
        tag = "低配额" if q.get("mode") == "low" else "正常"
        print(f"{s['node']}  余额 {_fmt(s['balance'])} cr  链长 {s['count']}  "
              f"链头 {s['head'] or '(空)'}  配额 {tag}")
        return 0
    if cmd in ("pay", "transfer"):
        if len(argv) < 3:
            print("用法：pay <node_id> <金额cr> [备注]")
            return 2
        try:
            amt = float(argv[2])
        except ValueError:
            print("金额得是个数字")
            return 2
        r = pay(argv[1], amt, " ".join(argv[3:]))
        print(json.dumps(r, ensure_ascii=False, indent=2))
        return 0 if r.get("ok") else 1
    if cmd == "pending":
        ps = pending()
        if not ps:
            print("没有在路上的钱（付出去的都已签收）")
        for p in ps:
            print(f"#{p.get('seq')} {p.get('day')} → {p.get('to')}  "
                  f"{_fmt(float(p.get('amount') or 0))} cr  nonce {p.get('nonce')}  {p.get('memo')}")
        return 0
    if cmd in ("transfers", "earn"):
        t = transfers()
        if not t:
            print("跟同伴之间还没有过钱（pay / recv）")
        for n, v in t.items():
            print(f"{n}  付出 {_fmt(v['out'])}  收到 {_fmt(v['in'])}  "
                  f"在路上 {_fmt(v['pending'])}")
        return 0
    if cmd == "verify":
        v = verify()
        print(json.dumps(v, ensure_ascii=False))
        return 0 if v.get("ok") else 1
    if cmd == "sync":
        print(json.dumps(sync(), ensure_ascii=False))
        return 0
    if cmd in ("identity", "pub", "id"):
        p = pubkey()
        if p:
            print(f"身份 {p}")
            print(f"私钥 {KEY_FILE}（权限 600，只签自己的账，绝不外传）")
        else:
            print("无身份：cryptography 不可用，签名退回共享密钥 HMAC（降级，可被持有 cluster.key 的节点伪造）")
        return 0
    if cmd == "anchors":
        a = anchors()
        if not a:
            print("还没锚定过任何同伴（anchor <node_id>）")
        for n, hist in sorted(a.items()):
            last = hist[-1]
            print(f"{n}  锚定链长 {last.get('count')}  链头 {last.get('head')}  "
                  f"余额 {_fmt(float(last.get('balance') or 0))}  次数 {len(hist)}")
        return 0
    if cmd == "anchor":
        if len(argv) < 2:
            print("用法：anchor <node_id>")
            return 2
        r = anchor(argv[1])
        print(json.dumps(r, ensure_ascii=False, indent=2))
        return 0 if r.get("ok") else 1
    if cmd == "heads":
        for n, hist in sorted(peer_heads().items()):
            last = hist[-1] if hist else {}
            print(f"{n}  链长 {last.get('count')}  余额 {_fmt(float(last.get('balance') or 0))}  链头 {last.get('head')}")
        return 0
    if cmd == "audit":
        if len(argv) < 2:
            print("用法：audit <node_id>")
            return 2
        r = audit(argv[1])
        print(json.dumps(r, ensure_ascii=False, indent=2))
        return 0 if r.get("ok") else 1
    if cmd == "show":
        for e in _read_all()[-int(argv[1] if len(argv) > 1 else 20):]:
            print(f"#{e.get('seq')} {_day(float(e.get('ts') or 0))} {e.get('kind'):6} "
                  f"{_fmt(float(e.get('delta') or 0)):>10}  {e.get('memo')}")
        return 0
    print(__doc__.split("\n")[1])
    print("用法：balance | verify | sync | identity | anchor <node_id> | anchors | "
          "heads | show [N] | audit <node_id> | pay <node_id> <cr> | pending | transfers")
    return 2


if __name__ == "__main__":
    import sys

    raise SystemExit(_cli(sys.argv[1:]))
