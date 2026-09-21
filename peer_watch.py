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
import re
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


_TAKEOVER_RUNNER: Optional[Callable[..., Optional[str]]] = None


def set_takeover_runner(fn) -> None:
    """注入「同伴留言自动接手」的执行器（签名 fn(text, frm) → str|None）。

    与 set_agent_runner 的分工：来电要当场回话（runner 拿得到 say_back），
    留言不要回声 —— 回执需要的是有人接着办，不是礼貌应答；两个都回话会变成刷屏。
    """
    global _TAKEOVER_RUNNER
    _TAKEOVER_RUNNER = fn


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

    # 同伴留言（回执/进展）不能响完铃就完 —— 派出去的活得有人接着办。
    # 只接 say：reply 是电话的回声（主对话当场已拿到），task 由对面 server 转成
    # 定时任务派给子智能体，接它们等于同一件事干两遍。
    if kind == "say" and auto_reply and _TAKEOVER_RUNNER is not None:
        verdict = classify(text)
        if verdict == "fact":
            board_append(entry, verdict, facts_of(text))
            log(f"↳ 事实回报 → 黑板（不起 agent）")
            return
        if _takeover_budget_ok(frm, budget):
            threading.Thread(target=_takeover, args=(frm, text), daemon=True).start()
        else:
            log(f"↳ 接手节流：{frm} 这条只落盘")
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


# ── 留言分级：事实回报走黑板，只有「要谁去做事」才值得起一个大脑 ────────────
# 第一性原理：起一个子智能体 = 一次完整的 LLM 循环（实测每轮数万 token）；
# 而「rpi 的 VERSION 是 1.1.7」这种事实，答案就在字符串里 —— 拿大脑去读它，
# 等于把零成本的信息检索按判断类任务计价。分级器只做一件事：
# 分清「报告一个已确定的事实」和「要求对方做事」。
_REQUEST_PAT = re.compile(
    r"请(你|问|帮)|帮我|麻烦|需要你|你去|要你|能否|可以的话|"
    r"把[^，。；]{0,14}(改|修|重启|部署|装|删|加|关|开|换)|"
    r"去(查|看|改|跑|执行|装|发布|同步|拉|推|办)"
)
_DONE_PAT = re.compile(
    r"已(经)?[^，。；]{0,8}(改|完|成功|通过|核对|验|上|落地|推|发)|"
    r"完成|通过|搞定|好了|回报|证据|实测|核对结果|查过|确认过"
)
_FACT_PAT = re.compile(
    r"VERSION\s*=|版本|\d+\.\d+\.\d+|/[\w./-]{4,}|https?://|rc=\d|"
    r"PID|pid=|active|inactive|hash|commit|余额|balance|签名|校验"
)
# 对方自己声明「这条不用办」的标记：链路测试、通报、知悉 —— 这类消息唯一的价值是留痕。
_NOACTION_PAT = re.compile(
    r"无需回复|不用回|不必回|勿回复|无需处理|不用管|仅供参考|仅通报|知悉|测试消息"
)


def classify(text: str) -> str:
    """把一条同伴留言判成 fact / action。判不准就判 action —— 错杀的代价是活没人干。"""
    if _NOACTION_PAT.search(text):
        return "fact"
    if _REQUEST_PAT.search(text):
        return "action"
    if _DONE_PAT.search(text) or _FACT_PAT.search(text):
        return "fact"
    return "action"


_VERSION_PAT = re.compile(r"\b\d+\.\d+\.\d+\b")
_PATH_PAT = re.compile(r"(?:/[\w.-]+){2,}")
_URL_PAT = re.compile(r"https?://[^\s，。）)]+")


def facts_of(text: str) -> Dict[str, List[str]]:
    """从事实回报里抽出可检索的结构 —— 以后问「rpi 什么版本」直接查黑板，不必再问人。"""
    out: Dict[str, List[str]] = {}
    for key, pat in (("version", _VERSION_PAT), ("path", _PATH_PAT), ("url", _URL_PAT)):
        vals: List[str] = []
        for v in pat.findall(text):
            if v not in vals:
                vals.append(v)
        if vals:
            out[key] = vals[:6]
    return out


BOARD_FILE = BASE_DIR / "data" / "peer_board.jsonl"
_BOARD_LOCK = threading.Lock()


def board_append(entry: Dict[str, Any], verdict: str, facts: Dict[str, Any]) -> None:
    """事实回报落黑板：append-only，不需要谁醒着就能留下、随时能查。"""
    row = {"ts": int(entry.get("ts") or time.time()),
           "from": str(entry.get("from") or "?"),
           "verdict": verdict,
           "facts": facts,
           "text": str(entry.get("text") or "")[:2000]}
    try:
        with _BOARD_LOCK:
            BOARD_FILE.parent.mkdir(parents=True, exist_ok=True)
            with BOARD_FILE.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError as e:
        log(f"↳ 黑板写不进（已吞）：{type(e).__name__}: {e}")


_TAKEOVER_LOCK = threading.Lock()
_TAKEOVER_STAMPS: List[float] = []
MAX_TAKEOVERS_PER_MIN = 3          # 全局熔断：两台自动接手的大白会互相触发，必须有硬上限
TAKEOVER_PER_PEER_PER_MIN = 1


def _takeover_budget_ok(frm: str, budget: Dict[str, List[float]]) -> bool:
    """接手的双闸门：单来源每分钟 1 次 + 全局每分钟 3 次。

    全局那道防的是对撞：我接手后子智能体可能又发一条留言出去，对面也自动接手，
    两边都能自证「我在干活」——只有次数上限能把这个循环掐断。
    """
    global _TAKEOVER_STAMPS
    now = time.time()
    _TAKEOVER_STAMPS = [t for t in _TAKEOVER_STAMPS if now - t < 60]
    if len(_TAKEOVER_STAMPS) >= MAX_TAKEOVERS_PER_MIN:
        return False
    key = "takeover:" + frm
    recent = [t for t in budget.get(key, []) if now - t < 60]
    if len(recent) >= TAKEOVER_PER_PEER_PER_MIN:
        budget[key] = recent
        return False
    budget[key] = recent + [now]
    _TAKEOVER_STAMPS.append(now)
    return True


def _takeover(frm: str, text: str) -> None:
    """同伴留言自动接手：起子智能体读它、判断要不要动、把结论交给主人。

    为什么必须有：派出去的活，回执只是躺进收件箱 —— 主对话要等主人下次开口
    才知道对面干完了。留言得自己把活往下推，不能把「等下一次唤醒」当流程。
    """
    runner = _TAKEOVER_RUNNER
    if runner is None:
        return
    if not _TAKEOVER_LOCK.acquire(blocking=False):
        log(f"↳ 已有接手在跑，{frm} 这条只落盘")
        return
    try:
        out = runner(text, frm)
        if out:
            log(f"接手 ← {frm}: {str(out)[:200]}")
    except Exception as e:
        log(f"接手异常（已吞）{frm}: {type(e).__name__}: {e}")
    finally:
        _TAKEOVER_LOCK.release()


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
