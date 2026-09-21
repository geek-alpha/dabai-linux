#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""联邦管理员授权：把「能不能用联邦」从自觉变成可验证的凭证。

第一性原理：MCP 只解决「不连接就不挂载」，解决不了「挂上了也随手就调」。
所以许可独立落盘（data/peer_admin_grant.json），只有本模块能写、消费方只读校验，
并且带 scope（read < talk < task）和 TTL —— 一次批准不会变成永久通行证。

为什么单独一级 task：派活是唯一能让**别的机器起子智能体烧 token**的动作，
和「问一句同伴在不在」不是一个代价量级，不能共用一道门。

授权与每次使用都写审计（data/peer_admin_audit.jsonl），事后能查「谁在什么时候
批准了什么、用没用」。

CLI：
  python peer_admin.py grant --scope talk --ttl 900 --why "核对 rpi 版本"
  python peer_admin.py status
  python peer_admin.py revoke --why "收工"
  python peer_admin.py check task        # 消费方/自检用
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
GRANT_FILE = BASE_DIR / "data" / "peer_admin_grant.json"
AUDIT_FILE = BASE_DIR / "data" / "peer_admin_audit.jsonl"

SCOPES = {"read": 1, "talk": 2, "task": 3}
DEFAULT_TTL = {"read": 900, "talk": 600, "task": 300}


def _audit(event: str, **kw) -> None:
    row = {"ts": round(time.time(), 3), "event": event}
    row.update(kw)
    try:
        AUDIT_FILE.parent.mkdir(parents=True, exist_ok=True)
        with AUDIT_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError:
        pass


def grant(scope: str, ttl: int = 0, why: str = "", by: str = "admin") -> dict:
    """写一张限时通行证。ttl 秒后自动失效，不续期、不累积。"""
    scope = str(scope or "").strip().lower()
    if scope not in SCOPES:
        raise ValueError(f"scope 只能是 {'/'.join(SCOPES)}，收到 {scope!r}")
    ttl = int(ttl or DEFAULT_TTL[scope])
    if ttl <= 0:
        raise ValueError("ttl 必须为正数秒")
    now = time.time()
    row = {
        "scope": scope,
        "by": by,
        "why": str(why or "").strip(),
        "ts": round(now, 3),
        "expires_at": round(now + ttl, 3),
        "ttl": ttl,
        "nonce": uuid.uuid4().hex[:12],
    }
    GRANT_FILE.parent.mkdir(parents=True, exist_ok=True)
    GRANT_FILE.write_text(json.dumps(row, ensure_ascii=False, indent=2), encoding="utf-8")
    _audit("grant", scope=scope, ttl=ttl, why=row["why"], by=by, nonce=row["nonce"])
    return row


def load_grant() -> dict:
    """读凭证；过期/损坏一律按「没授权」返回，不抛异常——消费方不该因为凭证坏了崩。"""
    try:
        row = json.loads(GRANT_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(row, dict) or row.get("scope") not in SCOPES:
        return {}
    row["expired"] = float(row.get("expires_at") or 0) <= time.time()
    row["remain_s"] = max(0, int(float(row.get("expires_at") or 0) - time.time()))
    return row


def check(need: str) -> tuple:
    """need 级别的动作能不能做。返回 (ok, 说明)。"""
    need = str(need or "").strip().lower()
    if need not in SCOPES:
        return False, f"未知级别 {need!r}"
    row = load_grant()
    if not row:
        return False, ("没有授权凭证（data/peer_admin_grant.json 不存在或已损坏）")
    if row.get("expired"):
        return False, (f"授权已于 {time.strftime('%H:%M:%S', time.localtime(row['expires_at']))} 过期"
                       f"（scope={row['scope']}，{row.get('why') or '无理由'}）")
    if SCOPES[row["scope"]] < SCOPES[need]:
        return False, (f"当前只授权到 {row['scope']}，这次要 {need}"
                       f"（授权理由：{row.get('why') or '无'}）")
    return True, f"授权有效：{row['scope']}，剩 {row['remain_s']}s（{row.get('why') or '无理由'}）"


def revoke(why: str = "", by: str = "admin") -> bool:
    row = load_grant()
    try:
        GRANT_FILE.unlink()
    except OSError:
        return False
    _audit("revoke", scope=row.get("scope"), why=why, by=by)
    return True


def status() -> dict:
    row = load_grant()
    if not row:
        return {"granted": False, "file": str(GRANT_FILE)}
    return {
        "granted": not row.get("expired"),
        "scope": row["scope"],
        "remain_s": row["remain_s"],
        "expires_at": row["expires_at"],
        "why": row.get("why") or "",
        "by": row.get("by") or "",
        "file": str(GRANT_FILE),
    }


def _fmt_status() -> str:
    st = status()
    if not st.get("granted"):
        extra = "（凭证已过期）" if st.get("scope") else ""
        return f"未授权{extra}。授权：python peer_admin.py grant --scope talk --ttl 600 --why \"…\""
    left = st["remain_s"]
    return (f"已授权：{st['scope']}，剩 {left // 60} 分 {left % 60} 秒，"
            f"理由「{st['why']}」，批准人 {st['by']}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="联邦管理员授权")
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("grant", help="发一张限时通行证")
    g.add_argument("--scope", required=True, choices=list(SCOPES))
    g.add_argument("--ttl", type=int, default=0, help="秒；缺省 read 900 / talk 600 / task 300")
    g.add_argument("--why", default="", help="为什么需要（写进审计）")
    g.add_argument("--by", default="admin")
    sub.add_parser("status", help="看当前授权")
    r = sub.add_parser("revoke", help="立刻收回")
    r.add_argument("--why", default="")
    c = sub.add_parser("check", help="问某级别能不能做")
    c.add_argument("level", choices=list(SCOPES))
    a = ap.parse_args(argv)

    if a.cmd == "grant":
        row = grant(a.scope, a.ttl, a.why, a.by)
        print(f"已授权 {row['scope']}，{row['ttl']} 秒后自动失效。理由「{row['why']}」")
        return 0
    if a.cmd == "status":
        print(_fmt_status())
        return 0
    if a.cmd == "revoke":
        print("已收回授权。" if revoke(a.why) else "本来就没有授权凭证。")
        return 0
    ok, msg = check(a.level)
    print(("可做" if ok else "不可做") + f"：{msg}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
