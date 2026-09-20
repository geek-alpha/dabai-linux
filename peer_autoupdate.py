#!/usr/bin/env python3
"""联邦自动更新 —— 把「新版可拉」这条留言，变成「本机真的拉下来了」。

第一性原理：留言只是闹钟，版本号不来自留言。耳朵收到 kind=release 时只做一件事 ——
唤醒本机已有的更新器（deploy/release/update.py）。装哪个版本由更新器自己去 GitHub
查、自己校验 sha256、失败自己回滚；留言里那个版本号只用来节流，不参与任何决策。
所以即使有人拿到集群密钥伪造一条「v99 已发布」，最坏结果也只是让它去查一次 GitHub。

默认关（settings.json → peer.auto_update）。自动改本机代码是重动作，
没配过的机器保持「人肉确认」的旧行为 —— 这是有意为之，不是没做完。

闸门按代价从低到高排，任何一道不过就停，且停的原因都写进日志：
  开关 → 消息类型 → 发送方白名单 → 全局冷却 → 单版本尝试次数 → 远端真有新版 → 真更新
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import peer_mesh  # noqa: E402

STATE_FILE = BASE_DIR / "data" / "peer_autoupdate.json"
LOG_FILE = BASE_DIR / "data" / "peer_autoupdate.jsonl"
UPDATER = BASE_DIR / "deploy" / "release" / "update.py"

KIND = "release"
DEFAULT_SOURCES = ("aliyun",)      # 发布源白名单：只有源仓库那台有资格叫别人升级
DEFAULT_COOLDOWN = 1800            # 全局冷却：半小时内最多自动更新一次，防刷
DEFAULT_MAX_ATTEMPTS = 2           # 同一版本最多试两次，坏版本不许反复上机
CHECK_TIMEOUT = 120
APPLY_TIMEOUT = 900

_VER_RE = re.compile(r"v?(\d+\.\d+\.\d+)")
_LOCK = threading.Lock()           # 同一台机器同时只跑一个更新，避免两个线程抢着改文件


def _settings() -> Dict[str, Any]:
    try:
        d = json.loads((BASE_DIR / "settings.json").read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _peer_cfg() -> Dict[str, Any]:
    v = _settings().get("peer")
    return v if isinstance(v, dict) else {}


def enabled() -> bool:
    """读不到就是关 —— 缺省必须落在「不动」那一侧。"""
    return bool(_peer_cfg().get("auto_update", False))


def sources() -> List[str]:
    v = _peer_cfg().get("release_source")
    if isinstance(v, list) and v:
        return [str(x) for x in v]
    if isinstance(v, str) and v:
        return [v]
    return list(DEFAULT_SOURCES)


def cooldown_sec() -> int:
    try:
        return int(_peer_cfg().get("auto_update_cooldown", DEFAULT_COOLDOWN))
    except (TypeError, ValueError):
        return DEFAULT_COOLDOWN


def max_attempts() -> int:
    try:
        return int(_peer_cfg().get("auto_update_max_attempts", DEFAULT_MAX_ATTEMPTS))
    except (TypeError, ValueError):
        return DEFAULT_MAX_ATTEMPTS


def _state() -> Dict[str, Any]:
    try:
        d = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_state(d: Dict[str, Any]) -> None:
    try:
        peer_mesh._atomic_write(STATE_FILE, json.dumps(d, ensure_ascii=False, indent=2))
    except OSError:
        pass


def parse_version(text: str) -> str:
    """留言里的版本号，只用于节流与日志。空串 = 没写版本号，按「未知」处理。"""
    m = _VER_RE.search(text or "")
    return m.group(1) if m else ""


def decide(entry: Dict[str, Any], now: Optional[float] = None) -> Dict[str, Any]:
    """纯判定：该不该动。返回 {"run": bool, "reason": str, "version": str}。

    判定不碰网络、不碰磁盘以外的状态，所以能被测试逐条钉住 ——
    「什么时候不动」比「什么时候动」更需要证据。
    """
    now = time.time() if now is None else now
    frm = str(entry.get("from") or "?")
    kind = str(entry.get("kind") or "say")
    ver = parse_version(str(entry.get("text") or ""))

    if kind != KIND:
        return {"run": False, "reason": f"不是发布通知（kind={kind}）", "version": ver}
    if not enabled():
        return {"run": False, "reason": "本机没开自动更新（settings.json → peer.auto_update）",
                "version": ver}
    if frm not in sources():
        return {"run": False, "reason": f"{frm} 不在发布源白名单（{', '.join(sources())}）",
                "version": ver}

    st = _state()
    last = float(st.get("last_ts") or 0)
    cool = cooldown_sec()
    if last and now - last < cool:
        return {"run": False, "reason": f"冷却中（{int(cool - (now - last))}s 后可再动）",
                "version": ver}
    tries = int((st.get("attempts") or {}).get(ver or "?", 0))
    if ver and tries >= max_attempts():
        return {"run": False, "reason": f"v{ver} 已试过 {tries} 次（上限 {max_attempts()}）",
                "version": ver}
    return {"run": True, "reason": "过闸", "version": ver}


def _run(cmd: List[str], timeout: int) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=str(BASE_DIR), capture_output=True, text=True,
                          timeout=timeout)


def _tail(p: subprocess.CompletedProcess, n: int = 3) -> str:
    out = ((p.stdout or "") + (p.stderr or "")).strip().splitlines()
    return " / ".join(out[-n:]) if out else "无输出"


def _audit(row: Dict[str, Any]) -> None:
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _report(frm: str, text: str) -> None:
    """回报发起方。送不到只记日志 —— 更新本身已经发生，回话失败不该改结论。"""
    try:
        peer_mesh.say(frm, text, kind="reply", timeout=15.0)
    except Exception as e:  # noqa: BLE001 联邦链路任何异常都不该冒泡进耳朵主循环
        _audit({"ts": int(time.time()), "phase": "report", "to": frm,
                "error": f"{type(e).__name__}: {e}"})


def _updater_cmd(flag: str) -> List[str]:
    return [sys.executable, str(UPDATER), flag]


def run_once(entry: Dict[str, Any]) -> Dict[str, Any]:
    """完整流程。任何异常都吞掉并记日志 —— 耳朵的循环不能因为一次更新失败而断。"""
    if not _LOCK.acquire(blocking=False):
        return {"ok": False, "reason": "已有一个更新在跑"}
    try:
        return _run_locked(entry)
    except Exception as e:  # noqa: BLE001
        _audit({"ts": int(time.time()), "phase": "error", "error": f"{type(e).__name__}: {e}"})
        return {"ok": False, "reason": f"{type(e).__name__}: {e}"}
    finally:
        _LOCK.release()


def _run_locked(entry: Dict[str, Any]) -> Dict[str, Any]:
    frm = str(entry.get("from") or "?")
    d = decide(entry)
    _audit({"ts": int(time.time()), "phase": "decide", "from": frm, **d})
    if not d["run"]:
        return {"ok": True, "skipped": True, "reason": d["reason"]}

    ver = d["version"]
    st = _state()
    st["last_ts"] = time.time()          # 先记时间：后面任何一步崩了也吃冷却，不会变成刷屏
    _save_state(st)

    if not UPDATER.is_file():
        _save_state(st)
        _audit({"ts": int(time.time()), "phase": "error", "error": f"找不到更新器 {UPDATER}"})
        return {"ok": False, "reason": "找不到更新器"}

    # ① 先问远端有没有新版（退出码 10 = 有）。版本判定在这里发生，不在留言里。
    try:
        chk = _run(_updater_cmd("--check"), CHECK_TIMEOUT)
    except subprocess.TimeoutExpired:
        _audit({"ts": int(time.time()), "phase": "check", "error": "查新版超时"})
        return {"ok": False, "reason": "查新版超时"}
    if chk.returncode == 0:
        _audit({"ts": int(time.time()), "phase": "check", "result": "already-latest"})
        _report(frm, f"收到 v{ver} 发布通知，但本机查到的远端版本不高于本地，没动。")
        return {"ok": True, "skipped": True, "reason": "已是最新"}
    if chk.returncode != 10:
        _save_state(_bump_attempt(st, ver))
        _audit({"ts": int(time.time()), "phase": "check", "rc": chk.returncode,
                "tail": _tail(chk)})
        _report(frm, f"自动更新中止：查新版失败（退出码 {chk.returncode}）。{_tail(chk, 1)}")
        return {"ok": False, "reason": f"查新版退出码 {chk.returncode}"}

    # ② 真更新。sha256 校验、体检、失败回滚都在 update.py 里，这里不重复实现。
    try:
        ap = _run(_updater_cmd("--apply"), APPLY_TIMEOUT)
    except subprocess.TimeoutExpired:
        st = _bump_attempt(st, ver)
        _save_state(st)
        _audit({"ts": int(time.time()), "phase": "apply", "error": "更新超时"})
        _report(frm, "自动更新超时（已中止），本机状态请人工确认。")
        return {"ok": False, "reason": "更新超时"}

    ok = ap.returncode == 0
    if ok:
        st["installed"] = ver or st.get("installed", "")
        st["attempts"] = {}
        _save_state(st)
        _audit({"ts": int(time.time()), "phase": "apply", "result": "ok", "tail": _tail(ap)})
        _report(frm, f"v{ver} 已自动更新完成（{peer_mesh.node_info(create=False).get('node_id')}）。")
    else:
        _save_state(_bump_attempt(st, ver))
        _audit({"ts": int(time.time()), "phase": "apply", "rc": ap.returncode, "tail": _tail(ap)})
        _report(frm, f"自动更新失败（退出码 {ap.returncode}，update.py 会自行回滚）。{_tail(ap, 1)}")
    return {"ok": ok, "reason": "已更新" if ok else f"退出码 {ap.returncode}"}


def _bump_attempt(st: Dict[str, Any], ver: str) -> Dict[str, Any]:
    key = ver or "?"
    attempts = st.get("attempts") if isinstance(st.get("attempts"), dict) else {}
    attempts[key] = int(attempts.get(key, 0)) + 1
    st["attempts"] = attempts
    return st


def handle(entry: Dict[str, Any]) -> None:
    """耳朵的入口：判定 + 起线程。判定在主循环里同步做（便宜），真更新丢线程（贵）。"""
    if str(entry.get("kind") or "") != KIND:
        return
    threading.Thread(target=run_once, args=(entry,), daemon=True,
                     name="peer-autoupdate").start()


if __name__ == "__main__":
    print(json.dumps({"enabled": enabled(), "sources": sources(),
                      "state": _state()}, ensure_ascii=False, indent=2))
