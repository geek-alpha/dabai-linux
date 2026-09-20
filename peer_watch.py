#!/usr/bin/env python3
"""大白联邦的耳朵 —— 常驻循环，把「留言」变成「打电话」。

第一性原理：打电话和留言的差别只有一个字 —— 时延。留言是「对方下次醒来才看到」，
打电话是「对方当场响」。所以这里不新增协议、不建长连接，只补一个常驻循环：
每 0.4 秒看一眼自己的收件箱，有新消息就当场处理。

分流规则（防两个大白互相刷屏）：
  kind=call   → 响铃 + 当场办事再回话：server 注入了完整执行器时，来电交给子智能体那条
                现成的「LLM + 工具」循环去跑（同一套工具定义、同一份技能说明书），
                跑完把结论说回去；没注入（耳朵单独跑）时退回轻量回话：裸 LLM + 本机实时状态
  kind=reply  → 只记日志，绝不回话
  kind=say    → 响铃，不回话（留言就该是留言）
  kind=task   → 响铃，不回话；对面 server 已经把它变成一次性定时任务派给子智能体，
                耳朵不执行任何东西（shell 不进耳朵——联邦消息只凭密钥认证）
  kind=release→ 交给 peer_autoupdate：唤醒本机更新器去查 GitHub。装哪个版本由更新器
                自己判定+校验+回滚，留言只当闹钟，默认关（settings.json → peer.auto_update）

回话用的是本机 LLM 档位（跟大白同一个模型配置）配本机实时状态，
所以「你那边怎么样」这种问题是真答得上来的。

单独跑（调试）：
  python peer_watch.py --once --replay     # 把现有收件箱当新消息处理一轮就退
  python peer_watch.py --no-reply          # 只响铃，不回话
"""
from __future__ import annotations

import argparse
import inspect
import json
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import peer_mesh  # noqa: E402
import peer_autoupdate  # noqa: E402
import peer_social  # noqa: E402

LOG_FILE = BASE_DIR / "data" / "peer_watch.log"
LOG_MAX_BYTES = 256 * 1024
POLL_INTERVAL = 0.4
MAX_REPLIES_PER_MIN = 3     # 同一个同伴一分钟最多回几次：对方程序出错时不能变成无限对话
REPLY_CHAR_CAP = 400
LLM_TIMEOUT = 60.0
FLUSH_EVERY = 20.0          # 重投发件箱的间隔（秒）：0.4 秒一轮，不能每轮都去敲离线节点

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


# ---------- 完整执行器（由 server 注入） ----------
# 第一性原理：同伴说的「帮我看看」和主人说的「帮我看看」，能力上不该有差别 ——
# 差别只在授权来源。联邦凭共享密钥认证（data/cluster.key），密钥持有者本就等同管理员，
# 所以这里不新造权限体系，只把来电接到「子智能体」那条现成的完整循环上：
# 同一套工具定义、同一个 harness 路由、同一份技能说明书。
_AGENT_RUNNER: Optional[Callable[..., Optional[str]]] = None


def set_agent_runner(fn) -> None:
    """注入「把同伴的话当管理员级任务执行」的执行器（签名 fn(text, frm, say_back) → str|None）。

    返回 None 表示执行器已把活转后台，出结果自己回话（长任务等不起电话）。
    没注入时退回轻量回话 —— 耳朵单独跑（调试）走的也是这条，联邦照旧能用，只是不能调工具。
    """
    global _AGENT_RUNNER
    _AGENT_RUNNER = fn


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


def reply_text(peer: str, text: str, cid: str = "") -> str:
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
    # 轻量路径也要连续：把同一通电话前几轮拼上，否则每轮都从零开始
    user_text = text + peer_mesh.call_history(cid, f"同伴 {peer}", drop_last=text)
    err = ""
    for c in cands:
        try:
            out = _chat(c, sys_p, user_text)
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

    if kind == "release":
        peer_autoupdate.handle(entry)
        return

    if kind != "call" or not auto_reply:
        return

    # 先记账再干活：通话账本是「这通电话说过什么」的唯一依据，即使下面被节流
    # 只记不回，前几轮也不会丢 —— 下一轮它再开口时上下文还在。
    cid = str(entry.get("cid") or "")
    if cid:
        peer_mesh.call_record(cid, frm, "peer", text, int(entry.get("ts") or 0))

    now = time.time()
    recent = [t for t in budget.get(frm, []) if now - t < 60]
    if len(recent) >= MAX_REPLIES_PER_MIN:
        budget[frm] = recent
        log(f"↳ 回话节流：{frm} 一分钟内已回 {len(recent)} 次，这条只记不回")
        return

    budget[frm] = recent + [now]
    target = _reply_full if _AGENT_RUNNER is not None else _reply
    threading.Thread(target=target, args=(frm, text, entry), daemon=True).start()


def _say_back(frm: str, text: str, entry: Dict[str, Any]) -> None:
    """把一句话送回给来电的同伴（带上原消息的 cid/ts，对面能认领是回给哪一条的）。"""
    cid = str(entry.get("cid") or "")
    r = peer_mesh.say(frm, text, kind="reply", timeout=15.0,
                      cid=cid, re_ts=int(entry.get("ts") or 0))
    if r.get("ok"):
        log(f"reply → {frm}: {text[:120]}")
        if cid:
            peer_mesh.call_record(cid, frm, "me", text)
    else:
        log(f"↳ 回话没送到 {frm}：{r.get('error')}")


def _reply(frm: str, text: str, entry: Dict[str, Any]) -> None:
    """轻量回话（没注入执行器时）：LLM 最长要 60 秒，所以丢到线程里跑 ——
    堵在主循环会让心跳断掉，同伴就会把「正在回话」误判成「这台接不了电话」。"""
    _say_back(frm, reply_text(frm, text, str(entry.get("cid") or "")), entry)


def _runner_takes_cid(runner) -> bool:
    """执行器支不支持「这通电话的 cid」参数。

    滚动升级期间 server 和耳朵可能不同版本，靠签名判断而不是 try/except TypeError——
    后者会把执行器内部真实的 TypeError 误当成「版本旧」而重跑一遍。
    """
    try:
        params = inspect.signature(runner).parameters
    except (TypeError, ValueError):
        return False
    if "cid" in params:
        return True
    return any(p.kind == inspect.Parameter.VAR_POSITIONAL for p in params.values())


def _reply_full(frm: str, text: str, entry: Dict[str, Any]) -> None:
    """完整执行器路径：把来电当任务跑，跑完把结论说回去。

    执行器返回 None = 它已经把活转后台了，等出结果自己回话。
    执行器自己抛异常时退回轻量回话 —— 宁可答得浅，不能让对面听到忙音。
    """
    runner = _AGENT_RUNNER
    if runner is None:
        _reply(frm, text, entry)
        return
    cid = str(entry.get("cid") or "")
    try:
        # cid 交给执行器：它据此取这通电话的前几轮，接着往下说而不是从头问
        if _runner_takes_cid(runner):
            out = runner(text, frm, lambda msg: _say_back(frm, msg, entry), cid)
        else:
            out = runner(text, frm, lambda msg: _say_back(frm, msg, entry))
    except Exception as e:
        log(f"完整执行器异常，退回轻量回话：{type(e).__name__}: {e}")
        _reply(frm, text, entry)
        return
    if out:
        _say_back(frm, out, entry)


_FLUSH_RUNNING = threading.Event()


def _flush_bg() -> None:
    """重投排队消息丢到线程里跑：对面离线时一次 6 秒超时，堵在主循环会让心跳断掉，
    同伴就会把「正在补投」误判成「这台接不了电话」。"""
    if _FLUSH_RUNNING.is_set():
        return
    _FLUSH_RUNNING.set()
    try:
        r = peer_mesh.flush_outbox()
        if r.get("sent") or r.get("dropped"):
            log(f"发件箱重投：送出 {r['sent']}，过期丢弃 {r['dropped']}，还剩 {r['pending']}")
    except Exception as e:
        log(f"发件箱重投异常（已吞）：{type(e).__name__}: {e}")
    finally:
        _FLUSH_RUNNING.clear()


def _prime_cursor() -> None:
    """首启动不回灌历史：游标文件不存在时先把游标推到末尾。
    否则第一次开耳朵会把积压的旧留言全当成新来电，挨个回话刷屏。"""
    if not peer_mesh.WATCH_CURSOR_FILE.exists():
        peer_mesh.read_new(mark=True)


def _announce_bg() -> None:
    """上线广播。任何失败只记日志 —— 广播不成功不影响耳朵接电话。"""
    try:
        r = peer_social.announce()
        log(f"上线广播：{r.get('reached')}/{r.get('asked')} 台应答，"
            f"新学到 {r.get('added')} 台，名册 {len(peer_social.roster())} 台")
    except Exception as e:
        log(f"上线广播失败（已吞）：{type(e).__name__}: {e}")


def _social_tick_bg() -> None:
    try:
        peer_social.tick()
    except Exception:
        pass


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
    # 上线就广播一次：让全联盟几秒内看到这台，而不是等下一轮 gossip（最久 5 分钟）。
    threading.Thread(target=_announce_bg, daemon=True).start()

    budget: Dict[str, List[float]] = {}
    last_flush = 0.0
    while True:
        beat()
        # 社会层的心跳：发现新节点。丢线程里跑 —— 一轮 gossip 要几秒，
        # 堵在主循环上会错过同伴来电，而耳朵的本职是接电话。
        threading.Thread(target=_social_tick_bg, daemon=True).start()
        try:
            for entry in peer_mesh.read_new():
                handle(entry, not a.no_reply, not a.no_notify, budget)
        except Exception as e:
            log(f"循环异常（已吞，继续跑）：{type(e).__name__}: {e}")
        if time.time() - last_flush >= FLUSH_EVERY:
            last_flush = time.time()
            threading.Thread(target=_flush_bg, daemon=True).start()
        if a.once:
            return 0
        time.sleep(max(0.05, a.interval))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
