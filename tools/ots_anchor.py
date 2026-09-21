#!/usr/bin/env python3
"""账本链头的外部时间锚（OpenTimestamps）。

同伴互锚证明的是「几台机器互相自洽」——见证者跟被见证者是同一批人，
外部人看了只能说这堆机器自己跟自己一致。这里补上缺的那一维：一个不归
我管的第三方，证明「这条链在某个时刻就已经存在」。

只公开 32 字节摘要，账本内容一个字都不外传，反向推不出原文。
不需要账号、邮箱、KYC，也不付钱。

  ots_anchor.py stamp              把当前链头锚到公共日历
  ots_anchor.py upgrade            等比特币确认后，把 pending 升级成完整证明
  ots_anchor.py status             列出所有锚与状态
  ots_anchor.py verify <file.ots>  独立验证一个证明（不依赖本机账本）
"""
from __future__ import annotations

import hashlib
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools" / "vendor_ots"))

import peer_ledger as led  # noqa: E402
from opentimestamps.calendar import CommitmentNotFoundError, RemoteCalendar  # noqa: E402
from opentimestamps.core.notary import BitcoinBlockHeaderAttestation, PendingAttestation  # noqa: E402
from opentimestamps.core.op import OpSHA256  # noqa: E402
from opentimestamps.core.serialize import BytesSerializationContext, StreamDeserializationContext  # noqa: E402
from opentimestamps.core.timestamp import DetachedTimestampFile, Timestamp  # noqa: E402

OTS_DIR = led.DATA_DIR / "ots"
INDEX_FILE = OTS_DIR / "index.jsonl"
USER_AGENT = "dabai-ots/0.1"

CALENDARS = [
    "https://alice.btc.calendar.opentimestamps.org",
    "https://bob.btc.calendar.opentimestamps.org",
    "https://finney.calendar.eternitywall.com",
    "https://btc.calendar.catallaxy.com",
]
TIMEOUT = 20


def _payload(node: str, count: int, head: str) -> bytes:
    return f"dabai-ledger-head:{node}:{count}:{head}".encode()


def _head() -> tuple[str, int, str, bytes, bytes]:
    rows = led._read_all()
    if not rows:
        raise RuntimeError("账本是空的，没有可锚的链头")
    node, count, head = led._node(), len(rows), str(rows[-1].get("sig") or "")
    if not head:
        raise RuntimeError("链头为空：最后一笔没有签名")
    payload = _payload(node, count, head)
    return node, count, head, payload, hashlib.sha256(payload).digest()


def _serialize(detached: DetachedTimestampFile) -> bytes:
    ctx = BytesSerializationContext()
    detached.serialize(ctx)
    return ctx.getbytes()


def _load(path: Path) -> DetachedTimestampFile:
    with open(path, "rb") as fh:
        return DetachedTimestampFile.deserialize(StreamDeserializationContext(fh))


def _read_index() -> list[dict]:
    out: list[dict] = []
    if not INDEX_FILE.exists():
        return out
    for line in INDEX_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def _state(detached: DetachedTimestampFile) -> dict:
    pending, bitcoin = [], []
    for _msg, att in detached.timestamp.all_attestations():
        if isinstance(att, PendingAttestation):
            pending.append(str(getattr(att, "uri", "")))
        elif isinstance(att, BitcoinBlockHeaderAttestation):
            bitcoin.append(int(getattr(att, "height", 0)))
    return {"pending": pending, "bitcoin": sorted(set(bitcoin))}


def stamp(calendars: list[str] | None = None) -> dict:
    node, count, head, payload, digest = _head()
    detached = DetachedTimestampFile(OpSHA256(), Timestamp(digest))
    ok, fail = [], []
    for url in calendars or CALENDARS:
        try:
            remote = RemoteCalendar(url, user_agent=USER_AGENT).submit(
                detached.timestamp.msg, timeout=TIMEOUT)
            detached.timestamp.merge(remote)
            ok.append(url)
        except Exception as e:  # 单个日历挂了不该拖垮整次锚定
            fail.append(f"{url}: {type(e).__name__}: {e}")
    if not ok:
        return {"ok": False, "reason": "所有日历都提交失败", "fail": fail}

    blob = _serialize(detached)
    OTS_DIR.mkdir(parents=True, exist_ok=True)
    fname = f"{node}-{count:06d}-{head[:8]}.ots"
    (OTS_DIR / fname).write_bytes(blob)
    # 把被锚定的原文单独落盘：官方 ots 客户端约定证明文件是「原文件路径 + .ots」，
    # 所以原文件必须与 .ots 同名同目录，否则 ots verify 找不到它
    (OTS_DIR / fname[:-4]).write_bytes(payload)
    rec = {
        "ts": int(time.time()),
        "node": node,
        "count": count,
        "head": head,
        "payload": payload.decode(),
        "digest": digest.hex(),
        "file": fname,
        "proof_bytes": len(blob),
        "proof_sha256": hashlib.sha256(blob).hexdigest(),
        "calendars": ok,
        "failed": fail,
    }
    with open(INDEX_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return {"ok": True, **rec}


def upgrade() -> dict:
    """pending 只是日历的承诺，要等它被打进比特币区块才算独立时间戳。"""
    rows = _read_index()
    done, still, fail = [], [], []
    for rec in rows:
        path = OTS_DIR / str(rec.get("file") or "")
        if not path.exists():
            fail.append(f"{rec.get('file')}: 证明文件不在了")
            continue
        detached = _load(path)
        before = _state(detached)
        if before["bitcoin"]:
            done.append({"file": path.name, "height": before["bitcoin"]})
            continue
        for uri in before["pending"]:
            if not uri:
                continue
            try:
                remote = RemoteCalendar(uri, user_agent=USER_AGENT).get_timestamp(
                    detached.timestamp.msg, timeout=TIMEOUT)
                detached.timestamp.merge(remote)
            except CommitmentNotFoundError:
                # 日历还没把这批提交聚合进比特币交易，属于正常等待，不是故障
                pass
            except Exception as e:
                fail.append(f"{path.name} @ {uri}: {type(e).__name__}: {e}")
        after = _state(detached)
        if after["bitcoin"]:
            path.write_bytes(_serialize(detached))
            done.append({"file": path.name, "height": after["bitcoin"]})
        else:
            still.append(path.name)
    return {"ok": True, "confirmed": done, "pending": still, "fail": fail}


def status() -> dict:
    rows = _read_index()
    out = []
    for rec in rows:
        path = OTS_DIR / str(rec.get("file") or "")
        item = {"file": rec.get("file"), "count": rec.get("count"),
                "head": str(rec.get("head") or "")[:16],
                "stamped_at": rec.get("ts"), "calendars": len(rec.get("calendars") or [])}
        if path.exists():
            st = _state(_load(path))
            item["bitcoin"] = st["bitcoin"]
            item["state"] = "已上链" if st["bitcoin"] else "待确认"
            item["pending"] = len(st["pending"])
        else:
            item["state"] = "证明文件丢失"
        out.append(item)
    return {"ok": True, "total": len(out), "anchors": out}


def verify(path: str) -> dict:
    """不读本机账本，只拿证明文件说话：内容有没有被日历签过、上没上链。"""
    p = Path(path)
    if not p.exists():
        return {"ok": False, "reason": f"文件不存在：{path}"}
    detached = _load(p)
    st = _state(detached)
    return {"ok": True, "file": p.name,
            "head_file": p.name[:-4],
            "file_digest": detached.file_digest.hex(),
            "bitcoin_heights": st["bitcoin"],
            "pending_calendars": st["pending"],
            "verdict": "已被比特币区块背书" if st["bitcoin"] else "只有日历承诺，尚未上链",
            "tree": detached.timestamp.str_tree()}


def main() -> int:
    args = sys.argv[1:]
    cmd = args[0] if args else "status"
    if cmd == "stamp":
        print(json.dumps(stamp(), ensure_ascii=False, indent=1))
    elif cmd == "upgrade":
        print(json.dumps(upgrade(), ensure_ascii=False, indent=1))
    elif cmd == "status":
        print(json.dumps(status(), ensure_ascii=False, indent=1))
    elif cmd == "verify":
        if len(args) < 2:
            print("用法：ots_anchor.py verify <file.ots>")
            return 2
        print(json.dumps(verify(args[1]), ensure_ascii=False, indent=1))
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
