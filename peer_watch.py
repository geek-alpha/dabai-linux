#!/usr/bin/env python3
"""大白联邦的耳朵 —— 常驻循环，把「留言」变成「打电话」。

第一性原理：打电话和留言的差别只有一个字 —— 时延。留言是「对方下次醒来才看到」，
打电话是「对方当场响」。所以这里不新增协议、不建长连接，只补一个常驻循环：
每 0.4 秒看一眼自己的收件箱，有新消息就当场处理。

分流规则（防两个大白互相刷屏）：
  kind=call   → 响铃 + 当场回一句（对面正守着等）
  kind=reply  → 只记日志，绝不回话
  kind=say    → 响铃，不回话（留言就该是留言）
  kind=task   → 响铃，不回话；对面 server 已经把它变成一次性定时任务派给子智能体，
                耳朵不执行任何东西（shell 不进耳朵——联邦消息只凭密钥认证）

回话用的是本机 LLM 档位（跟大白同一个模型配置）配本机实时状态，
所以「你那边怎么样」这种问题是真答得上来的。

单独跑（调试）：
  python peer_watch.py --once --replay     # 把现有收件箱当新消息处理一轮就退
  python peer_watch.py --no-reply          # 只响铃，不回话
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, List

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import peer_mesh  # noqa: E402

LOG_FILE = BASE_DIR / "data" / "peer_watch.log"
LOG_MAX_BYTES = 256 * 1024
POLL_INTERVAL = 0.4
MAX_REPLIES_PER_MIN = 3     # 同一个同伴一分钟最多回几次：对方程序出错时不能变成无限对话
REPLY_CHAR_CAP = 400
LLM_TIMEOUT = 60.0

_SYSTEM = """你是大白（白头凤）—— 跑在「{label}」这台机器上的那一个。
同伴「{peer}」打电话过来了，你要当场回一句。

规矩：
- 1~3 句中文，口语，先结论后细节；不寒暄、不问「有什么可以帮你」、不说「收到」
- 你就是大白本人，别自称「助手」「分身」或「AI」
- 对方问状态就照下面这份实时数据答；数据里没有的项，直说没读到
- 不知道就说不知道，不许编

本机实时状态（{now}）：
{state}"""


def log(msg: str) -> None:
    line = f"[{time.strftime('%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        if LOG_FILE.exists() and LOG_FILE.stat().st_size > LOG_MAX_BYTES:
            tail = LOG_FILE.read_text(encoding="utf-8", errors="replace")[-LOG_MAX_BYTES // 2:]
            LOG_FILE.write_text(tail, encoding="utf-8")
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def notify(title: str, body: str) -> None:
    """桌面通知。服务进程里多半没有 DISPLAY/DBUS，发不出去就静默跳过 ——
    响铃失败不该影响回话，那才是同伴真正在等的东西。"""
    exe = shutil.which("notify-send")
    if not exe:
        return
    try:
        subprocess.run([exe, "-u", "normal", title, body[:200]], timeout=5,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


_GOOD_CFG: Dict[str, str] = {}

_last_beat = 0.0
_last_beat_write = 0.0
BEAT_FILE = Path("/dev/shm/dabai_peer_ear.beat")
BEAT_WRITE_EVERY = 10.0


def beat() -> None:
    """每轮循环打一次心跳 —— 让同伴能问出「这台接不接得了电话」。

    内存变量只对本进程有效，而问这句话的是 server.py 那个进程，所以还得落一份
    给跨进程看；/dev/shm 是内存盘，不磨 SD 卡，重启后自然消失（耳朵也一起重启）。
    """
    global _last_beat, _last_beat_write
    now = time.time()
    _last_beat = now
    if now - _last_beat_write >= BEAT_WRITE_EVERY:
        _last_beat_write = now
        try:
            BEAT_FILE.write_text(str(int(now)), encoding="utf-8")
        except OSError:
            pass


def alive(max_age: float = 15.0) -> bool:
    now = time.time()
    if (now - _last_beat) < max_age:
        return True
    try:
        return (now - float(BEAT_FILE.read_text(encoding="utf-8").strip())) < max_age
    except (OSError, ValueError):
        return False


def _settings_cfg() -> Dict[str, str]:
    """settings.json 里当前激活的供应商档位 —— 也就是大白本体正在用的那个脑子。"""
    try:
        cfg = json.loads((BASE_DIR / "settings.json").read_text(encoding="utf-8"))
    except Exception:
        return {}
    prof = (cfg.get("llm_profiles") or {}).get(cfg.get("llm_provider") or "") or {}
    return {k: str(prof.get(k) or cfg.get(k) or "").strip()
            for k in ("base_url", "model", "api_key")}


def _candidates() -> List[Dict[str, str]]:
    """候选档位，第一个试通的会被记住。

    不能只认一个来源：阿里云上实测 codex_config.json 的 llm 段指向一把失效的 key，
    而 settings.json 的激活档位是好的 —— 耳朵就是大白本人，优先用本体那个脑子。
    """
    raw: List[Dict[str, str]] = []
    if _GOOD_CFG:
        raw.append(dict(_GOOD_CFG))
    raw.append(_settings_cfg())
    try:
        import codex_runner
        try:
            codex_runner.reload_relay_config()
        except Exception:
            pass
        raw.append({k: str(codex_runner.LLM_CFG.get(k) or "").strip()
                    for k in ("base_url", "model", "api_key")})
    except Exception as e:
        log(f"读不到 codex_runner 档位：{type(e).__name__}: {e}")
    out, seen = [], set()
    for c in raw:
        if not (c.get("base_url") and c.get("model") and c.get("api_key")):
            continue
        sig = (c["base_url"], c["model"], c["api_key"])
        if sig in seen:
            continue
        seen.add(sig)
        out.append(c)
    return out


def _chat(cfg: Dict[str, str], sys_p: str, user_text: str) -> str:
    body = json.dumps({
        "model": cfg["model"],
        "messages": [{"role": "system", "content": sys_p},
                     {"role": "user", "content": user_text}],
        "temperature": 0.7,
    }, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        cfg["base_url"].rstrip("/") + "/chat/completions",
        data=body,
        headers={"Content-Type": "application/json",
                 "User-Agent": peer_mesh._UA,
                 "Authorization": "Bearer " + cfg["api_key"]},
    )
    with peer_mesh._opener.open(req, timeout=LLM_TIMEOUT) as resp:
        d = json.loads(resp.read().decode("utf-8"))
    return (d["choices"][0]["message"]["content"] or "").strip()


def reply_text(peer: str, text: str) -> str:
    """用大白本体的档位生成一句回话。任何失败都返回一句人话，绝不抛异常 ——
    电话那头在等，宁可回「我脑子连不上」也不能让对面听到忙音。"""
    cands = _candidates()
    if not cands:
        return "我在（联邦链路是通的），但这台的 LLM 档位没配好，回不了话。"
    info = peer_mesh.node_info(create=False)
    state = peer_mesh.local_state()
    state.pop("node_id", None)
    sys_p = _SYSTEM.format(
        label=info.get("label") or info.get("node_id") or "?",
        peer=peer,
        now=time.strftime("%m-%d %H:%M"),
        state=json.dumps(state, ensure_ascii=False),
    )
    err = ""
    for c in cands:
        try:
            out = _chat(c, sys_p, text)
            _GOOD_CFG.clear()
            _GOOD_CFG.update(c)
            return out[:REPLY_CHAR_CAP] or "我在，但一时没想出话来说。"
        except Exception as e:
            err = type(e).__name__
            log(f"档位不可用 {c['base_url']} / {c['model']}：{type(e).__name__}: {e}")
    return f"我在（链路通），但这台的脑子连不上 LLM（{err}），晚点回你。"


def handle(entry: Dict[str, Any], auto_reply: bool, do_notify: bool,
           budget: Dict[str, List[float]]) -> None:
    frm = str(entry.get("from") or "?")
    kind = str(entry.get("kind") or "say")
    text = str(entry.get("text") or "")
    log(f"{kind} ← {frm}: {text[:120]}")

    if do_notify:
        title = {"call": f"大白来电 · {frm}",
                 "task": f"联邦派活 · {frm}"}.get(kind, f"联邦留言 · {frm}")
        notify(title, text)

    if kind != "call" or not auto_reply:
        return

    now = time.time()
    recent = [t for t in budget.get(frm, []) if now - t < 60]
    if len(recent) >= MAX_REPLIES_PER_MIN:
        budget[frm] = recent
        log(f"↳ 回话节流：{frm} 一分钟内已回 {len(recent)} 次，这条只记不回")
        return

    budget[frm] = recent + [now]
    threading.Thread(target=_reply, args=(frm, text, entry), daemon=True).start()


def _reply(frm: str, text: str, entry: Dict[str, Any]) -> None:
    """回话丢到线程里跑：LLM 最长要 60 秒，堵在主循环会让心跳断掉，
    同伴就会把「正在回话」误判成「这台接不了电话」。"""
    reply = reply_text(frm, text)
    r = peer_mesh.say(frm, reply, kind="reply", timeout=15.0,
                      cid=str(entry.get("cid") or ""), re_ts=int(entry.get("ts") or 0))
    if r.get("ok"):
        log(f"reply → {frm}: {reply[:120]}")
    else:
        log(f"↳ 回话没送到 {frm}：{r.get('error')}")


def _prime_cursor() -> None:
    """首启动不回灌历史：游标文件不存在时先把游标推到末尾。
    否则第一次开耳朵会把积压的旧留言全当成新来电，挨个回话刷屏。"""
    if not peer_mesh.WATCH_CURSOR_FILE.exists():
        peer_mesh.read_new(mark=True)


def main(argv: List[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="大白联邦的耳朵（常驻监听同伴来电）")
    ap.add_argument("--interval", type=float, default=POLL_INTERVAL, help="轮询间隔秒")
    ap.add_argument("--no-reply", action="store_true", help="只响铃，不回话")
    ap.add_argument("--no-notify", action="store_true", help="不发桌面通知")
    ap.add_argument("--once", action="store_true", help="跑一轮就退出（自测用）")
    ap.add_argument("--replay", action="store_true", help="不跳过历史消息（自测用）")
    a = ap.parse_args(argv)

    if not a.replay:
        _prime_cursor()

    info = peer_mesh.node_info()
    log(f"耳朵已开：{info.get('node_id')}（{info.get('label')}） "
        f"间隔 {a.interval}s 回话={'关' if a.no_reply else '开'} "
        f"通知={'关' if a.no_notify else '开'}")

    budget: Dict[str, List[float]] = {}
    while True:
        beat()
        try:
            for entry in peer_mesh.read_new():
                handle(entry, not a.no_reply, not a.no_notify, budget)
        except Exception as e:
            log(f"循环异常（已吞，继续跑）：{type(e).__name__}: {e}")
        if a.once:
            return 0
        time.sleep(max(0.05, a.interval))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
