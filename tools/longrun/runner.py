#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""长跑引擎：无人值守地一轮一轮推进长期目标（几十~上百天）。

设计取自四条已被验证的工程范式，而不是自创：
  1. Ralph loop（ghuntley.com/loop）：外层 while 循环，每轮全新上下文，
     记忆全在磁盘——不指望模型记得，指望台账记得。
  2. Anthropic「Effective harnesses for long-running agents」：worker 每轮只推
     一项、结束前留下结构化交接物（progress + next）；上下文重置优于压缩。
  3. 持久化执行（Temporal/Restate/DBOS 范式）：每轮幂等 + checkpoint 原子落盘 +
     崩溃后从最后一个已完成轮 resume。
  4. Letta sleep-time compute：主任务之外的空档用来整理记忆（本脚本的 digest 轮）。

一轮 = 从台账（long_horizon.json）取一个 active 目标 → 执行它的 next 原子动作
      → 用「台账有没有被更新」判定进展 → 落盘 → 睡。

用法：
  runner.py --once          只跑一轮（定时器/手动用）
  runner.py --loop          常驻循环（systemd Type=simple 用）
  runner.py --dry-run       不调模型，只打印本轮会派什么
  runner.py --status        打印状态、预算、最近几轮
  runner.py --goal <id>     只推某个目标（调试用）

环境变量：LONGRUN_INTERVAL / LONGRUN_TIMEOUT / LONGRUN_MAX_CALLS / LONGRUN_MODEL_USER
"""
import argparse
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parents[2]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

import platform_compat as pc  # noqa: E402
RUN_DIR = BASE / "data" / "longrun"
JOURNAL = RUN_DIR / "journal.jsonl"
STATE = RUN_DIR / "state.json"
STOP = RUN_DIR / "STOP"
HEARTBEAT = RUN_DIR / "heartbeat"
LEDGER = BASE / "long_horizon.json"
CLI = BASE / "dabai_cli.py"
# venv 布局随平台变：POSIX 是 bin/python，Windows 是 Scripts\python.exe
PY = (BASE / "venv" / ("Scripts" if os.name == "nt" else "bin")
      / ("python.exe" if os.name == "nt" else "python"))
# 提示词和汇报里给的是相对路径（换机器也能照抄）
PY_CMD = "venv/" + ("Scripts/python.exe" if os.name == "nt" else "bin/python")

INTERVAL = int(os.environ.get("LONGRUN_INTERVAL", "300"))
CALL_TIMEOUT = int(os.environ.get("LONGRUN_TIMEOUT", "3600"))
# 0 = 不限量。长跑是马拉松，不设天花板：花了多少记在 journal 的 usage 里，随时能查
MAX_CALLS = int(os.environ.get("LONGRUN_MAX_CALLS", "0"))
# 0 = 不冷却。没进展只说明还没找到路，不是这条路封了——换个姿势接着试
BLOCK_AFTER = int(os.environ.get("LONGRUN_BLOCK_AFTER", "0"))
BLOCK_SECONDS = int(os.environ.get("LONGRUN_BLOCK_SECONDS", str(6 * 3600)))
# 软提醒线：只记一笔日志，不拦任何东西
COST_NOTICE = int(os.environ.get("LONGRUN_COST_NOTICE", "200"))
OUT_TAIL = 800            # journal 里保留的输出尾巴字符数
TRACES = RUN_DIR / "traces"  # 按轮次的原始 trace（可下钻）
PROMPT_HEAD = 400         # trace 里 prompt 摘要保留的头/尾字符数
WS_ROOT = RUN_DIR / "ws"  # worker 独立工作区：产出落这里，不脏主项目
REPORT = RUN_DIR / "report.md"   # 给主人看的人话汇报（每轮重写，不是 append）
# ⚠ 这组常量全是 RUN_DIR 派生的。测试要把运行数据搬进 tmp_path，只 patch 其中几个
# 就会漏掉没 patch 的那个，而漏掉的那个指向真目录 —— 2026-09-22 实测：
# tests/test_longrun_view.py::test_one_cycle_writes_drillable_trace 跑完，
# data/longrun/report.md 被改写成「累计 1 轮 / 演示目标 / 没有等你决定的事」，
# 把主人唯一那份「引擎干了什么、要你决定什么」的汇报覆盖成了假话。
# 搬目录请用 _run_dir_derived()，别手写清单。
REPORT_ROUNDS = 5         # 汇报里保留最近几轮
REPORT_FILES = 8          # 产出清单最多列几个
# 接力棒里出现这些词 = 这轮动作在等主人，引擎自己推不动
OWNER_HINTS = ("等主人", "需主人", "主人若", "主人本机", "主人批", "请主人", "主人先", "等你")
VERDICT_CN = {"progressed": "✓ 有进展", "worked_no_ledger": "○ 干了活没落盘",
              "idle": "· 空转", "failed": "✗ 失败/超时"}


def now() -> float:
    return time.time()


def ts(t: float = None) -> str:
    return datetime.fromtimestamp(t or now()).strftime("%Y-%m-%d %H:%M:%S")


def log(msg: str) -> None:
    print(f"[{ts()}] {msg}", flush=True)


# ---------- 状态读写（崩溃安全：tmp + fsync + os.replace） ----------

def read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def write_atomic(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


_RUN_DIR_CONSTS = ("JOURNAL", "STATE", "STOP", "HEARTBEAT",
                   "TRACES", "WS_ROOT", "REPORT")


def _run_dir_derived() -> dict:
    """{常量名: 相对 RUN_DIR 的路径}——从当前值现算，不手抄文件名。

    存在的理由：这些常量是各自独立写死的（REPORT = RUN_DIR / "report.md"），
    把 RUN_DIR 搬走不会连带搬走它们。测试隔离必须按这张表整体搬，否则漏掉的那个
    会原地指向真目录 —— 那正是 report.md 被测试改写的原因。
    相对路径从值现算：常量改了文件名，这张表自动跟着变，不会漂移。
    """
    out = {}
    for name in _RUN_DIR_CONSTS:
        val = globals().get(name)
        if not isinstance(val, Path):
            raise RuntimeError(
                f"{name} 不是 Path（{val!r}）：_RUN_DIR_CONSTS 里的名字必须是 RUN_DIR 派生常量")
        try:
            out[name] = val.relative_to(RUN_DIR)
        except ValueError:
            # 静默跳过 = 下一个调用方会静默重现 report.md 被改写那类泄漏，
            # 所以这里宁可响：搬常量必须先取相对路径、再改 RUN_DIR。
            raise RuntimeError(
                f"{name}={val} 不在 RUN_DIR={RUN_DIR} 下 —— 调用方大概先 patch 了 "
                f"RUN_DIR 才来搬常量。先取相对路径再搬（见 "
                f"tests/test_longrun_view.py:_isolate_runner）") from None
    return out


def load_state() -> dict:
    s = read_json(STATE, {})
    s.setdefault("cycle", 0)
    s.setdefault("failures", {})
    s.setdefault("blocked", {})
    s.setdefault("budget", {"date": datetime.now().strftime("%Y-%m-%d"), "calls": 0})
    s.setdefault("last_goal", None)
    s.setdefault("unlanded", {})
    return s


def save_state(s: dict) -> None:
    write_atomic(STATE, s)


def append_journal(entry: dict) -> None:
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    with open(JOURNAL, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())


def read_journal(limit: int = 200) -> list:
    if not JOURNAL.exists():
        return []
    lines = JOURNAL.read_text(encoding="utf-8", errors="replace").splitlines()
    out = []
    for ln in lines[-limit:]:
        try:
            out.append(json.loads(ln))
        except Exception:
            continue
    return out


# ---------- 按轮次的 trace（下钻用） ----------
#
# journal 只留一轮的结论（out_tail/evidence），回答不了「这一轮它到底干了什么」。
# trace 把一轮拆成可回放的事件流：prompt 摘要 → worker 的原始事件（工具调用/结果/
# 用量）→ 退出码与进展判定。一行一事件，append-only + fsync，崩在中间也不丢已写行。

_ARG_KEYS = ("path", "file_path", "file", "command", "query", "pattern",
             "url", "name", "skill", "symbol", "root")


def trace_path(cycle: int) -> Path:
    return TRACES / f"{int(cycle)}.jsonl"


def append_trace(cycle: int, *events) -> Path:
    p = trace_path(cycle)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as f:
        for e in events:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())
    return p


def arg_hint(arguments) -> str:
    """工具参数压成一行摘要，只留一个最能说明意图的值。"""
    raw = arguments if isinstance(arguments, str) else json.dumps(arguments or {}, ensure_ascii=False)
    raw = (raw or "").strip()
    if not raw:
        return ""
    try:
        data = json.loads(raw)
    except Exception:
        return raw.replace("\n", " ")[:70]
    if not isinstance(data, dict):
        return str(data)[:70]
    for key in _ARG_KEYS:
        val = data.get(key)
        if isinstance(val, (str, int, float)) and str(val).strip():
            text = str(val).replace("\n", " ").strip()
            return text if len(text) <= 70 else text[:67] + "..."
    return ""


def parse_events(raw: str) -> tuple:
    """worker 的 --json 事件流 → (事件列表, 正文)。

    解析不了的行（崩栈/告警）当正文原文留着 —— trace 的价值在原始，不在整齐。
    """
    events, chunks, other, fallback = [], [], [], []
    for ln in (raw or "").splitlines():
        s = ln.strip()
        if not s:
            continue
        try:
            ev = json.loads(s)
        except Exception:
            other.append(s)
            continue
        if not isinstance(ev, dict) or "type" not in ev:
            other.append(s)
            continue
        events.append(ev)
        if ev.get("type") == "StreamDelta" and ev.get("text"):
            chunks.append(str(ev["text"]))
        elif ev.get("type") in ("TextDelta", "FinalText") and ev.get("text"):
            # 非流式/失败分支的正文只走这两个事件（原生流式不补发）：
            # 只认 StreamDelta 时，模型报错或走文本协议的一轮会记成空 out_tail
            fallback.append(str(ev["text"]))
    text = ("".join(chunks) or "".join(fallback)).strip()
    if other:
        text = (text + "\n" + "\n".join(other)).strip()
    return events, text


def tool_sequence(events: list) -> list:
    """工具调用序列：名字 + 参数摘要 + 成败 + 耗时（配对不上结果的那次也算）。"""
    out, cur = [], None
    for ev in events:
        t = ev.get("type")
        if t == "ToolCallStart":
            cur = {"name": ev.get("tool_name") or "?", "hint": arg_hint(ev.get("arguments")),
                   "_t": now()}
        elif t == "ToolCallResult" and cur is not None:
            cur["ok"] = bool(ev.get("success", True))
            cur["dur"] = round(now() - cur.pop("_t", now()), 1)
            out.append(cur)
            cur = None
    if cur is not None:
        cur.pop("_t", None)
        out.append(cur)
    return out


def usage_of(events: list) -> dict:
    """本轮 LLM 用量（token 与调用轮数）——预算争议时唯一的硬数字。"""
    for ev in reversed(events):
        if ev.get("type") == "UsageEvent":
            return {"in": ev.get("prompt_tokens") or 0,
                    "out": ev.get("completion_tokens") or 0,
                    "rounds": ev.get("rounds") or 0}
    return {}


def prompt_summary(prompt: str, cycle: int, goal: dict) -> dict:
    return {"type": "prompt", "t": now(), "cycle": cycle, "goal": goal.get("id"),
            "action": (goal.get("next") or "")[:200], "chars": len(prompt),
            "head": prompt[:PROMPT_HEAD], "tail": prompt[-PROMPT_HEAD:]}


def touch_heartbeat() -> None:
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    HEARTBEAT.write_text(str(int(now())), encoding="utf-8")


# ---------- 预算闸门 ----------

def budget_ok(s: dict) -> bool:
    today = datetime.now().strftime("%Y-%m-%d")
    if s["budget"].get("date") != today:
        s["budget"] = {"date": today, "calls": 0}
    if MAX_CALLS <= 0:
        return True
    return int(s["budget"].get("calls", 0)) < MAX_CALLS


def budget_spend(s: dict) -> None:
    n = int(s["budget"].get("calls", 0)) + 1
    s["budget"]["calls"] = n
    if COST_NOTICE > 0 and n % COST_NOTICE == 0:
        log(f"    · 今日已跑 {n} 轮（只记账，不拦）")


def seconds_to_midnight() -> int:
    n = datetime.now()
    end = n.replace(hour=23, minute=59, second=59, microsecond=0)
    return max(60, int((end - n).total_seconds()) + 1)


# ---------- 台账（长期事业）读取 ----------

def load_goals() -> list:
    d = read_json(LEDGER, {})
    out = []
    for p in d.get("projects", []):
        if p.get("stage") != "active":
            continue
        if not (p.get("next") or "").strip():
            continue          # 没有接力棒 = 没想清下一步，跳过（不许空转）
        if p.get("owner_block"):
            continue          # 卡在主人动作上：再跑也只能重写文档，烧的是真钱
        out.append(p)
    return out


def owner_blocked() -> list:
    """卡在主人动作上的目标：引擎再跑也只能重复写文档，不值一轮的算力。"""
    d = read_json(LEDGER, {})
    return [p for p in d.get("projects", [])
            if p.get("stage") == "active" and p.get("owner_block")]


def goal_stamp(goal: dict) -> str:
    """进展指纹：next + 最近一条 log。台账变了 = 上一轮真的推进了。"""
    logs = goal.get("log") or []
    last = logs[0].get("what", "") if logs else ""
    raw = f"{goal.get('id')}|{goal.get('progress')}|{goal.get('next','')}|{last}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def pick_goal(goals: list, state: dict, forced: str = None) -> dict:
    """选目标：优先没被冷却的，且最久没跑过的（round-robin，不饿死任何目标）。"""
    if forced:
        for g in goals:
            if g.get("id") == forced:
                return g
        return None
    last_run = {}
    for e in read_journal(400):
        last_run[e.get("goal")] = max(last_run.get(e.get("goal"), 0), e.get("t", 0))
    fresh = [g for g in goals if state["blocked"].get(g["id"], 0) < now()]
    pool = fresh or goals
    return min(pool, key=lambda g: last_run.get(g.get("id"), 0))


# ---------- 组装本轮 prompt（worker：一轮一动作 + 强制交接） ----------

PROMPT = """你是「长跑引擎」第 {cycle} 轮。上下文全新——磁盘上的台账是你唯一的记忆。

【工作区】{ws}
你的 cwd 就是它：产出文件（文档、话术库、草稿、截图）一律落这里，相对路径就是落这里。
主项目在 {base}，要用绝对路径访问；只读参考或确有必要时才动它，别把产出丢进主项目根目录。

【目标】{title}（{gid}）
为什么做：{why}
解决谁的什么问题：{value}
什么算完成：{done_when}
当前进度：{progress}%

【本轮唯一动作】{next}

上轮结果：{last_result}
上轮证据：{last_ev}

【铁律】
1. 这一轮只推上面那一个动作，做完就停；不要顺手开新战场。
2. 说「完成」必须带证据：文件:行号、命令原文+退出码、或工具输出。拿不出证据就别声称完成。
3. 收工前必须落盘接力棒，否则这一轮等于没发生：
   {py} {base}/tools/long_horizon.py log {gid} "本轮做了什么" --ev "证据" --progress {progress} --next "下一轮的原子动作"
4. 卡住了也要落盘：卡点写进 --ev，--next 换成绕过它的动作（缩小范围/换工具/换路径），别把同一个动作原样留给下一轮。
   若卡点只有主人能做（解锁手机、扫码登录、手填数字、点头批补丁），落盘后必须再标记一次，别让引擎为它空烧：
   {py} {base}/tools/long_horizon.py block {gid} --why "卡在主人哪件事"
   标记后这个目标退出轮转，直到主人做完并 unblock。重写第十遍文档不会让卡点前进一步。
5. {safety}
6. 检索批量做、读文件定点读：code_search 一次能传多个关键词，code_read 一次能读多个区间——同一轮里合并调用。每多一次往返，整段上下文就重发一次（实测单轮 29 次调用烧 166 万 prompt token）。别重读已读过的长文件，先 symbols/code_search 定位再定点读。
7. 本轮最多 60 分钟。做不完就按第 4 条落盘交接，别硬撑。手机是主人和你共用的资源：动手机要一次 batch 干完（操作前自动排队，被占用时会明确告诉你谁占着），别一步一次往返占着不放。
8. 不许停、重启或改动长跑引擎本身（systemctl ... dabai-longrun、kill 长跑进程、改 runner.py 或 systemd 服务配置）——引擎是把你托起来的那个，停了它这轮就白跑了。{extra}
"""


def safety_lines(goal: dict) -> tuple:
    """安全边界按目标渲染：默认全禁；主人在台账里 allow 了 send_msg 才放开「对外发消息」，
    且必须留痕 + 节流。花钱类永远禁止——这条不受 allow 影响。
    """
    allow = goal.get("allow") or []
    if "send_msg" not in allow:
        return ("不可逆动作（删除文件、推送远端、发布上线、对外发消息、花钱）一律不做，"
                "只写进 --next 等主人批。"), ""
    safety = ("花钱永远禁止：支付、下单、转账、改价成交、绑卡一律不做，只写进 --next 等主人批。"
              "对外发消息主人已批准，但必须留痕：每条要发的消息先落盘 "
              f"{BASE}/data/longrun/outbox/YYYY-MM-DD.jsonl（字段 app/target/content/ts，ts 用当前时刻），"
              "落盘成功后才允许发送，发完再追加一条 {\"event\":\"sent\"} 回执；没落盘的消息不许发。")
    extra = ("\n9. 手机节流（防封号）：单轮对外消息≤5 条、条间隔≥30 秒、同一对象单轮只联系一次；"
             "遇到验证码、人脸识别、支付密码、实名校验弹窗——立即停止该动作并落盘卡点，绝不尝试绕过。"
             "\n10. 商家账号是主人的命根子：不改账号设置、不删已发布商品、不碰资金/提现页面。")
    return safety, extra


def goal_ws(gid: str) -> Path:
    """每个目标一个独立工作区：worker 的 cwd 与 shell 落点都在这里。

    只改 Popen 的 cwd 拦不住 shell_run（它走 EXECUTOR.cwd = 全局配置），
    所以同时通过 DABAI_WORKSPACE 环境变量把覆盖传进子进程。
    """
    ws = WS_ROOT / gid
    (ws / "docs").mkdir(parents=True, exist_ok=True)
    return ws


def build_prompt(goal: dict, state: dict) -> str:
    gid = goal.get("id")
    prev = {}
    for e in reversed(read_journal(80)):
        if e.get("goal") == gid and e.get("kind") == "run":
            prev = e
            break
    safety, extra = safety_lines(goal)
    return PROMPT.format(
        ws=goal_ws(gid), base=BASE, py=PY,
        safety=safety,
        extra=extra,
        cycle=state.get("cycle", 0),
        title=goal.get("title") or gid,
        gid=gid,
        why=goal.get("why") or "（未填）",
        value=goal.get("value") or "（未填）",
        done_when=goal.get("done_when") or "（未填）",
        progress=goal.get("progress", 0),
        next=(goal.get("next") or "").strip(),
        last_result=(prev.get("out_tail") or "（无，这是第一轮）")[:400],
        last_ev=(prev.get("evidence") or "（无）")[:200],
    )


_LOCK_FH = None


def acquire_lock() -> bool:
    """同一时刻只允许一个 runner：服务、定时器、手动三处都可能同时起。"""
    global _LOCK_FH
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    fh = open(RUN_DIR / "runner.lock", "w")
    if not pc.lock_file(fh, blocking=False):
        fh.close()
        return False
    fh.write(str(os.getpid()))
    fh.flush()
    _LOCK_FH = fh
    return True


def sweep_orphans() -> int:
    """清掉上一代遗留的孤儿 agent 进程。

    runner 被 kill -9 时子进程会失去父亲继续跑（继续烧 token、继续改文件）。
    只有在拿到单实例锁之后调用才安全：此时任何 longrun 子进程必定是孤儿。
    """
    killed = 0
    me = os.getpid()
    for p in pc.process_tree():
        cmd = p.get("cmdline") or ""
        if "dabai_cli.py" not in cmd or "longrun_" not in cmd:
            continue
        pid, ppid = p.get("pid"), p.get("ppid") or 0
        if not pid or pid == me or ppid == me:
            continue
        # 孤儿的语义两个平台不同：Linux 的父进程被重挂到 pid 1，Windows 的 ppid
        # 仍指向一个已消失的 pid。统一按「父进程还在不在」判定，别写死 ppid == 1。
        if ppid not in (0, 1) and pc.pid_alive(ppid):
            continue          # 父进程还活着 = 不是孤儿，别碰
        if pc.kill_pid(pid, force=True):
            killed += 1
    return killed


def call_agent(prompt: str, user: str, cycle: int = None, ws: Path = None) -> tuple:
    """独立进程跑一轮，全新上下文（Ralph loop 的核心：每轮干净开局）。

    跑的过程中持续刷心跳：否则一轮长活（可能 20 分钟）会被看门狗误判成卡死。
    给 cycle 时同时把 prompt 摘要 + worker 原始事件流写进 traces/<cycle>.jsonl ——
    用 --json 拿事件流是刻意的：安静模式下工具调用被丢掉，那一轮就永远说不清。
    """
    # namespace 按目标隔离：既不串进主对话，目标之间也互不污染
    argv = [str(PY), str(CLI), prompt, "-q", "-u", user, "--namespace", user, "--json"]
    t0 = now()
    # 独立会话 = 独立进程组：超时时连同它拉起的所有子进程一起杀干净
    env = dict(os.environ)
    if ws:
        env["DABAI_WORKSPACE"] = str(ws)
    p = subprocess.Popen(argv, cwd=str(ws or BASE), env=env, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, text=True,
                         **pc.spawn_kwargs(new_group=True))
    fh = None
    if cycle:
        append_trace(cycle, {"type": "start", "t": t0, "argv": argv[1:4], "user": user})
        fh = open(trace_path(cycle), "a", encoding="utf-8")
    out_lines, err_lines = [], []

    def _pump(stream, sink, mirror=None):
        try:
            for ln in iter(stream.readline, ""):
                sink.append(ln)
                if mirror is not None:
                    mirror.write(ln)
                    mirror.flush()
        except Exception:
            pass

    # 两个管道都丢线程里读：主线程要留着轮询超时和刷心跳，谁堵住都不行
    t_out = threading.Thread(target=_pump, args=(p.stdout, out_lines, fh), daemon=True)
    t_err = threading.Thread(target=_pump, args=(p.stderr, err_lines), daemon=True)
    t_out.start()
    t_err.start()
    last_hb = t0
    while True:
        rc = p.poll()
        if rc is not None:
            break
        if now() - t0 > CALL_TIMEOUT:
            try:
                pc.terminate_tree(p.pid)
            except Exception:
                p.kill()
            p.wait()
            t_out.join(3)
            t_err.join(3)
            if fh:
                fh.close()
            return 124, f"（超时 {CALL_TIMEOUT}s，已杀）", now() - t0
        if now() - last_hb > 60:
            touch_heartbeat()
            last_hb = now()
        time.sleep(2)
    p.wait()
    t_out.join(5)
    t_err.join(5)
    if fh:
        fh.close()
    out = "".join(out_lines)
    err = "".join(err_lines).strip()
    if err:
        out += "\n[stderr] " + err[-400:]
    return p.returncode, out.strip(), now() - t0


def load_projects() -> list:
    return read_json(LEDGER, {}).get("projects", []) or []


def find_project(gid: str) -> dict:
    for p in load_projects():
        if p.get("id") == gid:
            return p
    return {}


def extract_evidence(out: str) -> str:
    """从输出里捞一行像证据的（文件:行号 / 命令输出 / 退出码）。"""
    for ln in (out or "").splitlines():
        s = ln.strip()
        if not s:
            continue
        if ".py:" in s or ".md:" in s or "exit=" in s or "✓" in s or "通过" in s:
            return s[:200]
    return ""


def verdict_of(rc: int, progressed: bool, tools: list, out: str) -> str:
    """一轮的四态判定。

    只看台账 stamp 会把「干了活没落盘」和「真空转」混成一类，而两者处置完全
    不同：前者是交接动作漏了（下轮把落盘盯紧点），后者是这一轮根本没动起来。
    """
    if rc != 0:
        return "failed"
    if progressed:
        return "progressed"
    acted = [t for t in tools if t.get("name") != "skill_help"]
    if acted and (out or "").strip():
        return "worked_no_ledger"
    return "idle"


# ---------- 一轮 ----------

def one_cycle(state: dict, forced: str = None, dry: bool = False) -> str:
    state["cycle"] = int(state.get("cycle", 0)) + 1
    cycle = state["cycle"]
    goals = load_goals()
    if not goals:
        waiting = owner_blocked()
        if waiting:
            why = "；".join(f"{w.get('title') or w.get('id')}：{(w.get('owner_block') or {}).get('why', '')}"
                            for w in waiting)
            log(f"第 {cycle} 轮：{len(waiting)} 个目标全卡在主人身上，跳过不烧钱 —— {why}")
            note = f"全部等主人：{why}"
        else:
            log(f"第 {cycle} 轮：台账里没有「active + 有 next」的目标，空转跳过")
            note = "无可用目标（stage=active 且 next 非空）"
        if not dry:
            append_journal({"t": now(), "kind": "idle", "cycle": cycle, "note": note})
            save_state(state)
            write_report(state)
        return "idle"

    goal = pick_goal(goals, state, forced)
    if not goal:
        return "idle"
    gid = goal["id"]
    stamp_before = goal_stamp(goal)
    prompt = build_prompt(goal, state)

    if dry:
        log(f"第 {cycle} 轮（dry-run）目标={gid} 动作={(goal.get('next') or '')[:80]}")
        print("-" * 60)
        print(prompt)
        return "dry"

    touch_heartbeat()
    log(f"第 {cycle} 轮 → [{gid}] {(goal.get('next') or '')[:70]}")
    append_trace(cycle, prompt_summary(prompt, cycle, goal))
    rc, raw, dur = call_agent(prompt, f"longrun_{gid}", cycle=cycle, ws=goal_ws(gid))
    budget_spend(state)
    events, out = parse_events(raw)
    tools = tool_sequence(events)
    usage = usage_of(events)

    after = find_project(gid)
    stamp_after = goal_stamp(after) if after else ""
    progressed = bool(after) and stamp_after != stamp_before
    verdict = verdict_of(rc, progressed, tools, out)

    entry = {
        "t": now(), "kind": "run", "cycle": cycle, "goal": gid,
        "action": (goal.get("next") or "")[:300],
        "exit": rc, "dur": round(dur, 1),
        "progressed": progressed, "ok": (rc == 0 and progressed),
        "verdict": verdict,
        "out_tail": (out or "")[-OUT_TAIL:],
        "evidence": extract_evidence(out),
        "stamp_before": stamp_before, "stamp_after": stamp_after,
        "trace": f"data/longrun/traces/{cycle}.jsonl",
        "tools": [t.get("name") for t in tools][:40], "tool_count": len(tools),
        "usage": usage,
    }

    if verdict == "progressed":
        state["failures"][gid] = 0
        state.setdefault("unlanded", {})[gid] = 0
        log(f"    ✓ 有进展（台账已更新，{dur:.0f}s）")
    elif verdict == "worked_no_ledger":
        n = int(state.setdefault("unlanded", {}).get(gid, 0)) + 1
        state["unlanded"][gid] = n
        state["failures"][gid] = 0   # 干了活就不算失败，交接疏漏别记成能力问题
        reason = f"干了活没落盘（{len(tools)} 次工具调用，台账未变，连续 {n} 次）"
        log(f"    ○ {reason}（{dur:.0f}s）")
        entry["reason"] = reason
    else:
        n = int(state["failures"].get(gid, 0)) + 1
        state["failures"][gid] = n
        reason = "调用失败/超时" if verdict == "failed" else "空转：没有实质动作"
        log(f"    ✗ {reason}（连续 {n} 次，{dur:.0f}s）")
        entry["reason"] = reason
        if BLOCK_AFTER > 0 and n >= BLOCK_AFTER:
            state["blocked"][gid] = now() + BLOCK_SECONDS
            state["failures"][gid] = 0
            entry["blocked"] = True
            log(f"    ⏸ [{gid}] 连续 {BLOCK_AFTER} 轮无进展，冷却 {BLOCK_SECONDS // 3600}h")
    append_trace(cycle, {"type": "exit", "t": now(), "cycle": cycle, "goal": gid,
                         "exit": rc, "dur": round(dur, 1), "progressed": progressed,
                         "reason": entry.get("reason", ""),
                         "tools": tools, "usage": usage})
    state["last_goal"] = gid
    append_journal(entry)
    save_state(state)
    write_report(state)
    return "run"


# ---------- 循环 ----------

def sleep_seconds(state: dict) -> int:
    blocked = state.get("blocked", {})
    goals = load_goals()
    if goals and all(blocked.get(g["id"], 0) >= now() for g in goals):
        return 3600
    return INTERVAL


def run_loop(state: dict) -> int:
    log(f"长跑引擎启动：间隔 {INTERVAL}s / 单轮上限 {CALL_TIMEOUT}s / 日调用 "
        f"{'不限' if MAX_CALLS <= 0 else MAX_CALLS} / 冷却 "
        f"{'关' if BLOCK_AFTER <= 0 else str(BLOCK_AFTER) + '轮'}")
    while True:
        if STOP.exists():
            log(f"发现急停文件 {STOP}，优雅退出（删掉它再启动即可恢复）")
            return 0
        if not budget_ok(state):
            wait = seconds_to_midnight()
            log(f"今日调用额度已用完（{MAX_CALLS}），睡到明天（{wait}s）")
            touch_heartbeat()
            time.sleep(wait)
            continue
        try:
            one_cycle(state)
        except Exception as e:
            log(f"本轮异常（已忽略，不影响常驻）：{type(e).__name__}: {e}")
        touch_heartbeat()
        time.sleep(sleep_seconds(state))


def live_cycle() -> dict:
    """此刻正在跑的轮次。

    journal 只在轮次结束时落盘——只看它，一轮 20 分钟的长活会被读成卡死。
    心跳新鲜时改从 trace 现场取证：跑了多久、调了几次工具、最后一次在干什么。
    """
    hb = HEARTBEAT.stat().st_mtime if HEARTBEAT.exists() else 0
    if not hb or now() - hb > 180:
        return {}
    traces = [p for p in TRACES.glob("*.jsonl")
              if p.stem.isdigit() and now() - p.stat().st_mtime < 180]
    if not traces:
        return {}
    p = max(traces, key=lambda q: q.stat().st_mtime)
    cycle = int(p.stem)
    # 已落盘 = 这轮早收工了；刚跑完那一秒不该被报成「进行中」
    if cycle in {int(e.get("cycle") or 0) for e in read_journal(80) if e.get("kind") == "run"}:
        return {}
    started, calls, last = None, 0, None
    for ln in p.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            ev = json.loads(ln)
        except Exception:
            continue
        t = ev.get("type")
        if t == "start":
            started = ev.get("t") or started
        elif t == "ToolCallStart":
            calls += 1
            last = {"name": ev.get("tool_name") or "?", "hint": arg_hint(ev.get("arguments"))}
    return {"cycle": p.stem, "started": started, "calls": calls, "last": last,
            "idle": int(now() - p.stat().st_mtime)}



def _engine_alive() -> bool:
    """runner.lock 里的 PID 还活着 = 引擎真在跑。

    不能拿 trace 新鲜度代替：引擎停掉后 trace 还是新鲜的，汇报会说假话——
    说假话的汇报比没有汇报更坏。
    """
    try:
        pid = int((RUN_DIR / "runner.lock").read_text(encoding="utf-8").strip() or 0)
    except Exception:
        return False
    if not pid:
        return False
    if not pc.pid_alive(pid):
        return False
    for p in pc.process_tree():
        if p.get("pid") != pid:
            continue
        # Windows 的 CommandLine 用反斜杠、Linux 的 /proc cmdline 是 NUL 分隔——
        # 先统一成正斜杠再匹配，别按平台各写一套
        cmd = (p.get("cmdline") or "").replace("\\", "/")
        return "longrun/runner.py" in cmd
    return False


def _idle_streak() -> int:
    """最近连续空转了几轮（全是 idle、没有一次 run）。

    全卡在主人身上时引擎会安静地空转几十轮——这件事必须被说出来，
    而不是等人自己去翻 journal 发现。
    """
    n = 0
    for e in reversed(read_journal(200)):
        k = e.get("kind")
        if k == "idle":
            n += 1
        elif k == "run":
            break
    return n


def _fmt_hm(t) -> str:
    try:
        return datetime.fromtimestamp(float(t)).strftime("%m-%d %H:%M")
    except Exception:
        return "-"


def _owner_todos() -> list:
    """只有主人能做的事：显式标记的 + 接力棒里点名等主人的。

    这是整份汇报里唯一需要主人动手的部分，其余都是「知道一下」。
    """
    todos = []
    for p in read_json(LEDGER, {}).get("projects", []):
        if p.get("stage") != "active":
            continue
        title = p.get("title") or p.get("id")
        if p.get("owner_block"):
            why = str((p.get("owner_block") or {}).get("why") or "").strip()
            todos.append((p.get("id"), title, why or "（未说明）", "block"))
            continue
        nxt = str(p.get("next") or "").strip()
        if any(h in nxt for h in OWNER_HINTS):
            todos.append((p.get("id"), title, nxt, "next"))
    return todos


def _recent_outputs() -> list:
    """worker 工作区里最近动过的文件——「产出在哪」是主人第二想知道的事。"""
    rows = []
    if not WS_ROOT.exists():
        return rows
    for p in WS_ROOT.rglob("*"):
        if not p.is_file():
            continue
        name = p.name
        if ".bak-" in name or name.startswith("."):
            continue
        try:
            st = p.stat()
            rel = p.relative_to(BASE)
        except (OSError, ValueError):
            continue
        if st.st_size == 0:
            continue
        rows.append((st.st_mtime, str(rel), st.st_size))
    rows.sort(reverse=True)
    return rows[:REPORT_FILES]


def build_report(state: dict = None) -> str:
    """人话汇报：引擎干了什么、产出在哪、要主人做什么。纯函数，便于测。"""
    s = state if state is not None else load_state()
    titles = {p.get("id"): (p.get("title") or p.get("id"))
              for p in read_json(LEDGER, {}).get("projects", [])}
    last = s.get("last_goal")
    out = ["# 长跑引擎汇报", "",
           f"- 状态：{'正在跑' if _engine_alive() else '已停'}"
           f" ｜ 累计 {s.get('cycle', 0)} 轮"
           f" ｜ 上次目标：{titles.get(last, last or '无')}"]
    idle = _idle_streak()
    if idle >= 3:
        out.append(f"- ⚠ **连续空转 {idle} 轮**：能推的目标全卡在主人身上，引擎没活可干")
    out += [f"- 更新：{time.strftime('%Y-%m-%d %H:%M')}", ""]

    todos = _owner_todos()
    if todos:
        out += [f"## ⚠ 等你决定（{len(todos)} 件）", ""]
        for i, (gid, title, why, kind) in enumerate(todos, 1):
            tag = "（接力棒里点名等你）" if kind == "next" else "（已挂起，引擎不再为它烧钱）"
            if len(why) > 140:
                why = why[:140] + "…"
            out.append(f"{i}. **{title}**{tag}：{why}")
        out += ["", "做完那件事，解除挂起它就重新进轮转：", "", "```sh",
                f"{PY_CMD} tools/long_horizon.py unblock <目标id>", "```", ""]
    else:
        out += ["## ⚠ 等你决定", "", "没有。引擎自己能推的都推完了。", ""]

    rows = [e for e in read_journal(60) if e.get("kind") == "run"][-REPORT_ROUNDS:]
    if rows:
        out += [f"## 最近 {len(rows)} 轮干了什么", ""]
        for e in rows:
            v = e.get("verdict") or ("progressed" if e.get("progressed") else "idle")
            gid = e.get("goal")
            line = f"- 第 {e.get('cycle')} 轮 [{titles.get(gid, gid)}] {VERDICT_CN.get(v, v)}"
            ev = str(e.get("evidence") or "").strip()
            if ev:
                line += f" —— {ev[:120]}"
            out.append(line)
        out.append("")

    files = _recent_outputs()
    if files:
        out += ["## 产出在哪", ""]
        for mtime, rel, size in files:
            out.append(f"- `{rel}`（{size // 1024}K，{_fmt_hm(mtime)}）")
        out.append("")

    out += ["## 怎么回话", "", "```sh",
            f"{PY_CMD} tools/long_horizon.py list                # 全部目标与卡点",
            f"{PY_CMD} tools/long_horizon.py next <id> \"下一步\"   # 改接力棒",
            f"{PY_CMD} tools/long_horizon.py log <id> \"做了什么\" --ev \"证据\"",
            "```"]
    return "\n".join(out) + "\n"


def write_report(state: dict = None) -> str:
    """每轮结束重写汇报——产出不该只躺在 journal 里等人挖。失败不拖垮本轮。"""
    try:
        text = build_report(state)
    except Exception as e:
        log(f"汇报生成失败（不影响本轮）：{e}")
        return ""
    try:
        tmp = str(REPORT) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, REPORT)
    except Exception as e:
        log(f"汇报落盘失败（不影响本轮）：{e}")
    return text


def cmd_report() -> int:
    """打印并顺手刷新 report.md——引擎停着的时候，磁盘上那份也得是最新的。"""
    print(write_report(), end="")
    return 0


def cmd_status() -> int:
    s = load_state()
    print(f"状态文件：{STATE}")
    print(f"  轮次：{s['cycle']}  今日调用：{s['budget'].get('calls')}/"
          f"{MAX_CALLS if MAX_CALLS > 0 else '不限'}"
          f"  上次目标：{s.get('last_goal')}")
    hb = HEARTBEAT.stat().st_mtime if HEARTBEAT.exists() else 0
    print(f"  心跳：{ts(hb) if hb else '无'}（{int(now() - hb) if hb else '-'} 秒前）")
    lv = live_cycle()
    if lv:
        el = int(now() - lv["started"]) if lv.get("started") else 0
        last = lv.get("last") or {}
        doing = f"{last.get('name')} {last.get('hint', '')}".strip() if last else "还没调工具"
        print(f"  进行中：第 {lv['cycle']} 轮 已跑 {el // 60}分{el % 60}秒 | 工具 {lv['calls']} 次 "
              f"| 落盘停顿 {lv['idle']}s | 最近：{doing}")
    print(f"  急停文件：{'存在（已停）' if STOP.exists() else '无'}")
    bl = {k: ts(v) for k, v in (s.get("blocked") or {}).items() if v > now()}
    print(f"  冷却中的目标：{bl or '无'}")
    print(f"  active 且有待办的目标：{[g['id'] for g in load_goals()]}")
    rows = [e for e in read_journal(200) if e.get("kind") == "run"][-5:]
    print("  最近 5 轮（✓=台账已更新 ○=干了活没落盘 ✗=空转/失败）：")
    for e in rows:
        v = e.get("verdict") or ("progressed" if e.get("progressed") else "idle")
        mark = {"progressed": "✓", "worked_no_ledger": "○"}.get(v, "✗")
        u = e.get("usage") or {}
        tok = f" {u.get('in', 0)}tok/{u.get('rounds', 0)}轮" if u else ""
        print(f"    [{ts(e['t'])}] {e.get('goal')} exit={e.get('exit')} "
              f"{mark}{v} {e.get('dur')}s{tok}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(prog="longrun", description="长跑引擎（无人值守推进长期目标）")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--once", action="store_true", help="只跑一轮")
    g.add_argument("--loop", action="store_true", help="常驻循环")
    g.add_argument("--status", action="store_true", help="打印状态")
    g.add_argument("--report", action="store_true", help="打印给主人看的人话汇报")
    g.add_argument("--dry-run", action="store_true", help="不调模型，只打印本轮会派什么")
    ap.add_argument("--goal", help="只推指定目标 id")
    a = ap.parse_args()

    RUN_DIR.mkdir(parents=True, exist_ok=True)
    state = load_state()

    if a.status:
        return cmd_status()
    if a.report:
        return cmd_report()
    if a.dry_run:
        one_cycle(state, a.goal, dry=True)
        return 0
    if not acquire_lock():
        log("已有 runner 在跑（data/longrun/runner.lock 被占），本实例退出")
        return 0
    n = sweep_orphans()
    if n:
        log(f"清掉 {n} 个上一代遗留的 agent 子进程（runner 被强杀时的孤儿）")
    if a.once:
        if STOP.exists():
            log("急停文件存在，不跑")
            return 0
        if not budget_ok(state):
            log(f"今日额度已用完（{MAX_CALLS}）")
            return 0
        one_cycle(state, a.goal)
        touch_heartbeat()
        return 0
    return run_loop(state)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
