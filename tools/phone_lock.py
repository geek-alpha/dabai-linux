#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""手机资源互斥锁：一台手机同一时刻只允许一个持有者。

为什么需要它：长跑引擎的 worker 和主对话会同时操作同一台手机。adb 命令交叉
执行不是「可能点错」，是必然点错——一边 swipe 一边 tap，读到的控件树属于谁
都说不清。

锁语义用 flock：进程退出（含 kill -9）内核自动释放，不会留下死锁。文件里写的
owner/pid/since 只是给人看的线索，判「真持有」永远靠 flock 试探，不靠文件内容
——文件内容在释放后会残留。

用法（Python）：
    from phone_lock import hold
    with hold("longrun_biz-negotiate", wait=15) as (ok, info):
        if not ok: ...

用法（命令行）：
    phone_lock.py acquire --owner me [--wait 15] [--ttl 600]
    phone_lock.py release
    phone_lock.py status
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import sys
import time
from contextlib import contextmanager
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
LOCK = BASE / "data" / "locks" / "phone.lock"

_HELD: dict = {}   # pid -> (fh, owner)：同进程可重入，否则自己第二次调用会把自己挡住


def _read_info() -> dict:
    try:
        return json.loads(LOCK.read_text(encoding="utf-8") or "{}") or {}
    except Exception:
        return {}


def _write_info(fh, owner: str, ttl: int) -> None:
    fh.seek(0)
    fh.truncate()
    fh.write(json.dumps({"owner": owner, "pid": os.getpid(),
                         "since": round(time.time(), 1), "ttl": ttl},
                        ensure_ascii=False))
    fh.flush()


def _try_take(owner: str, ttl: int):
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    fh = open(LOCK, "a+", encoding="utf-8")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    _write_info(fh, owner, ttl)
    return fh


def acquire(owner: str, wait: float = 0, ttl: int = 0):
    """拿到锁返回文件句柄，拿不到返回 None（wait 秒内每 0.3s 重试一次）。

    同进程内重复 acquire 直接复用第一次的句柄：flock 按 open file description
    计权，同一进程开两个 fd 抢同一把锁也会自己阻塞自己。
    """
    me = os.getpid()
    if me in _HELD:
        return _HELD[me][0]
    deadline = time.time() + max(0.0, float(wait or 0))
    while True:
        fh = _try_take(owner, ttl)
        if fh is not None:
            _HELD[me] = (fh, owner)
            return fh
        if time.time() >= deadline:
            return None
        time.sleep(0.3)


def release() -> bool:
    got = _HELD.pop(os.getpid(), None)
    if not got:
        return False
    fh, _ = got
    try:
        fcntl.flock(fh, fcntl.LOCK_UN)
    except Exception:
        pass
    fh.close()
    return True


def holder() -> dict:
    """当前真持有者；没人持有返回 {}。靠 flock 试探，不信文件里写的。"""
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    fh = open(LOCK, "a+", encoding="utf-8")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        info = _read_info()
        fh.close()
        return info or {"owner": "未知", "pid": 0, "since": 0}
    try:
        fcntl.flock(fh, fcntl.LOCK_UN)
    except Exception:
        pass
    fh.close()
    return {}


@contextmanager
def hold(owner: str, wait: float = 0, ttl: int = 0):
    """with hold(...) as (ok, info)：拿不到锁时 ok=False、info 是占用者。"""
    if acquire(owner, wait, ttl) is None:
        yield False, holder()
        return
    try:
        yield True, {"owner": owner, "pid": os.getpid(), "since": time.time(), "ttl": ttl}
    finally:
        release()


def _describe(info: dict) -> str:
    if not info:
        return "空闲"
    held = int(time.time() - float(info.get("since") or 0)) if info.get("since") else -1
    age = f"，已持有 {held}s" if held >= 0 else ""
    ttl = info.get("ttl") or 0
    over = "（超过 TTL，疑似卡住）" if ttl and held > ttl else ""
    return f"{info.get('owner')}（pid {info.get('pid')}{age}）{over}"


def main() -> int:
    ap = argparse.ArgumentParser(prog="phone_lock", description="手机资源互斥锁")
    ap.add_argument("action", choices=["acquire", "release", "status"])
    ap.add_argument("--owner", default="cli")
    ap.add_argument("--wait", type=float, default=0)
    ap.add_argument("--ttl", type=int, default=0, help="只用于提示「疑似卡住」，不强制抢占")
    a = ap.parse_args()

    if a.action == "status":
        info = holder()
        print(("占用中：" + _describe(info)) if info else "空闲")
        return 0
    if a.action == "release":
        print("已释放" if release() else "本进程没持有这把锁（锁由持有进程退出自动释放）")
        return 0
    if acquire(a.owner, a.wait, a.ttl) is None:
        print("占用中：" + _describe(holder()))
        return 1
    print(f"OK {a.owner} 拿到手机锁")
    return 0


if __name__ == "__main__":
    sys.exit(main())
