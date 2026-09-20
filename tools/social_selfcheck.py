#!/usr/bin/env python3
"""社会层自检 —— 回答三个问题：路由活了吗、名册里有谁、广播有几台应答。

改完 peer_social.py / server.py 后必须重启 myservice 才生效（core_autorestart=false），
这个脚本就是重启后的验收器。它只走真 HTTP、不直接调函数 —— 所以「运行中的进程里到底
有没有这条路由」能被它抓出来（直接调函数会永远通过，因为函数在当前进程里总是新的）。

用法：
  venv/bin/python tools/social_selfcheck.py                 # 默认 port 8001
  venv/bin/python tools/social_selfcheck.py --wait 120      # 等端口恢复的秒数
  venv/bin/python tools/social_selfcheck.py --no-announce   # 只验路由，不往外广播
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import peer_mesh as pm      # noqa: E402
import peer_social as ps    # noqa: E402

UA = "dabai-social-selfcheck/1.0"


def _post(base: str, path: str, payload: dict, timeout: float = 12.0):
    req = urllib.request.Request(
        base + path,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "X-Phoenix-Key": pm.cluster_key(create=False).decode(),
            "User-Agent": UA,
        },
        method="POST",
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        return e.code, {"_body": e.read().decode("utf-8", "replace")[:200]}
    except Exception as e:
        return 0, {"_err": f"{type(e).__name__}: {e}"}


def wait_up(base: str, seconds: int) -> bool:
    end = time.time() + seconds
    while time.time() < end:
        code, _ = _post(base, "/api/peer/social/roster", {}, timeout=5)
        if code == 200:
            return True
        time.sleep(2)
    return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8001)
    ap.add_argument("--wait", type=int, default=90, help="等端口恢复的秒数")
    ap.add_argument("--no-announce", action="store_true")
    args = ap.parse_args()
    base = f"http://127.0.0.1:{args.port}"

    print(f"=== {time.strftime('%F %T')} 社会层自检 (port {args.port}) ===", flush=True)

    if not wait_up(base, args.wait):
        print(f"[FAIL] {args.wait}s 内 roster 路由没起来：路由没生效，或服务没起来", flush=True)
        return 1
    print("[OK] 路由活着：POST /api/peer/social/roster -> 200", flush=True)

    code, body = _post(base, "/api/peer/social/roster", {})
    names = [n.get("node_id") for n in (body.get("nodes") or [])]
    print(f"[i] 本机名册 {body.get('count')} 台：{names}", flush=True)
    print(f"[i] 合并结果：learned={body.get('learned')} updated={body.get('updated')}", flush=True)

    if not args.no_announce:
        for label, fn in (("announce", ps.announce), ("gossip", ps.gossip_once)):
            try:
                print(f"[i] {label}: {json.dumps(fn(), ensure_ascii=False)}", flush=True)
            except Exception as e:
                print(f"[warn] {label} 失败：{type(e).__name__}: {e}", flush=True)

    local = ps.roster(prune=False)
    print(f"[i] 名册文件 {len(local)} 台：{sorted(local.keys())}", flush=True)
    print(f"[i] 地址簿 {sorted(pm.peers().keys())}", flush=True)
    print(f"[i] 朋友圈 {sorted(ps.friends().keys())}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
