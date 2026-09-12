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
import fcntl
import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parents[2]
RUN_DIR = BASE / "data" / "longrun"
JOURNAL = RUN_DIR / "journal.jsonl"
STATE = RUN_DIR / "state.json"
STOP = RUN_DIR / "STOP"
HEARTBEAT = RUN_DIR / "heartbeat"
LEDGER = BASE / "long_horizon.json"
CLI = BASE / "dabai_cli.py"
PY = BASE / "venv" / "bin" / "python"

INTERVAL = int(os.environ.get("LONGRUN_INTERVAL", "300"))
CALL_TIMEOUT = int(os.environ.get("LONGRUN_TIMEOUT", "1800"))
MAX_CALLS = int(os.environ.get("LONGRUN_MAX_CALLS", "200"))
BLOCK_AFTER = 3           # 连续 N 轮无进展 → 该目标冷却
BLOCK_SECONDS = 6 * 3600  # 冷却时长
OUT_TAIL = 800            # journal 里保留的输出尾巴字符数


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


def load_state() -> dict:
    s = read_json(STATE, {})
    s.setdefault("cycle", 0)
    s.setdefault("failures", {})
    s.setdefault("blocked", {})
    s.setdefault("budget", {"date": datetime.now().strftime("%Y-%m-%d"), "calls": 0})
    s.setdefault("last_goal", None)
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


def touch_heartbeat() -> None:
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    HEARTBEAT.write_text(str(int(now())), encoding="utf-8")


# ---------- 预算闸门 ----------

def budget_ok(s: dict) -> bool:
    today = datetime.now().strftime("%Y-%m-%d")
    if s["budget"].get("date") != today:
        s["budget"] = {"date": today, "calls": 0}
    return int(s["budget"].get("calls", 0)) < MAX_CALLS


def budget_spend(s: dict) -> None:
    s["budget"]["calls"] = int(s["budget"].get("calls", 0)) + 1


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
        out.append(p)
    return out


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
   venv/bin/python tools/long_horizon.py log {gid} "本轮做了什么" --ev "证据" --progress {progress} --next "下一轮的原子动作"
4. 卡住了也要落盘：卡点写进 --ev，--next 换成绕过它的动作（缩小范围/换工具/换路径），别把同一个动作原样留给下一轮。
5. 不可逆动作（删除文件、推送远端、发布上线、对外发消息、花钱）一律不做，只写进 --next 等主人批。
6. 别重读已读过的长文件；先用 symbols / code_search 定位再定点读。
7. 本轮最多 20 分钟。做不完就按第 4 条落盘交接，别硬撑。
"""


def build_prompt(goal: dict, state: dict) -> str:
    gid = goal.get("id")
    prev = {}
    for e in reversed(read_journal(80)):
        if e.get("goal") == gid and e.get("kind") == "run":
            prev = e
            break
    return PROMPT.format(
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
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
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
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit() or int(entry.name) == me:
            continue
        try:
            cmd = (entry / "cmdline").read_bytes().replace(b"\x00", b" ").decode("utf-8", "replace")
        except Exception:
            continue
        if "dabai_cli.py" in cmd and "longrun_" in cmd:
            try:
                ppid = int((entry / "stat").read_text().split(")", 1)[1].split()[1])
            except Exception:
                continue
            if ppid != 1:
                continue          # 父进程还活着 = 不是孤儿，别碰
            try:
                os.kill(int(entry.name), signal.SIGKILL)
                killed += 1
            except Exception:
                pass
    return killed


def call_agent(prompt: str, user: str) -> tuple:
    """独立进程跑一轮，全新上下文（Ralph loop 的核心：每轮干净开局）。

    跑的过程中持续刷心跳：否则一轮长活（可能 20 分钟）会被看门狗误判成卡死。
    """
    argv = [str(PY), str(CLI), prompt, "-q", "-u", user]
    t0 = now()
    # 独立会话 = 独立进程组：超时时连同它拉起的所有子进程一起杀干净
    p = subprocess.Popen(argv, cwd=str(BASE), stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, text=True, start_new_session=True)
    last_hb = t0
    while True:
        rc = p.poll()
        if rc is not None:
            break
        if now() - t0 > CALL_TIMEOUT:
            try:
                os.killpg(os.getpgid(p.pid), signal.SIGKILL)
            except Exception:
                p.kill()
            p.wait()
            return 124, f"（超时 {CALL_TIMEOUT}s，已杀）", now() - t0
        if now() - last_hb > 60:
            touch_heartbeat()
            last_hb = now()
        time.sleep(2)
    out, err = p.communicate()
    out = out or ""
    if err and err.strip():
        out += "\n[stderr] " + err.strip()[-400:]
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


# ---------- 一轮 ----------

def one_cycle(state: dict, forced: str = None, dry: bool = False) -> str:
    state["cycle"] = int(state.get("cycle", 0)) + 1
    cycle = state["cycle"]
    goals = load_goals()
    if not goals:
        log(f"第 {cycle} 轮：台账里没有「active + 有 next」的目标，空转跳过")
        if not dry:
            append_journal({"t": now(), "kind": "idle", "cycle": cycle,
                            "note": "无可用目标（stage=active 且 next 非空）"})
            save_state(state)
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
    rc, out, dur = call_agent(prompt, f"longrun_{gid}")
    budget_spend(state)

    after = find_project(gid)
    stamp_after = goal_stamp(after) if after else ""
    progressed = bool(after) and stamp_after != stamp_before

    entry = {
        "t": now(), "kind": "run", "cycle": cycle, "goal": gid,
        "action": (goal.get("next") or "")[:300],
        "exit": rc, "dur": round(dur, 1),
        "progressed": progressed, "ok": (rc == 0 and progressed),
        "out_tail": (out or "")[-OUT_TAIL:],
        "evidence": extract_evidence(out),
        "stamp_before": stamp_before, "stamp_after": stamp_after,
    }

    if rc == 0 and progressed:
        state["failures"][gid] = 0
        log(f"    ✓ 有进展（台账已更新，{dur:.0f}s）")
    else:
        n = int(state["failures"].get(gid, 0)) + 1
        state["failures"][gid] = n
        reason = "调用失败/超时" if rc != 0 else "台账没变（上一轮没落盘接力棒）"
        log(f"    ✗ 无进展：{reason}（连续 {n} 次，{dur:.0f}s）")
        entry["reason"] = reason
        if n >= BLOCK_AFTER:
            state["blocked"][gid] = now() + BLOCK_SECONDS
            state["failures"][gid] = 0
            entry["blocked"] = True
            log(f"    ⏸ [{gid}] 连续 {BLOCK_AFTER} 轮无进展，冷却 {BLOCK_SECONDS // 3600}h")
    state["last_goal"] = gid
    append_journal(entry)
    save_state(state)
    return "run"


# ---------- 循环 ----------

def sleep_seconds(state: dict) -> int:
    blocked = state.get("blocked", {})
    goals = load_goals()
    if goals and all(blocked.get(g["id"], 0) >= now() for g in goals):
        return 3600
    return INTERVAL


def run_loop(state: dict) -> int:
    log(f"长跑引擎启动：间隔 {INTERVAL}s / 单轮上限 {CALL_TIMEOUT}s / 日调用上限 {MAX_CALLS}")
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


def cmd_status() -> int:
    s = load_state()
    print(f"状态文件：{STATE}")
    print(f"  轮次：{s['cycle']}  今日调用：{s['budget'].get('calls')}/{MAX_CALLS}"
          f"  上次目标：{s.get('last_goal')}")
    hb = HEARTBEAT.stat().st_mtime if HEARTBEAT.exists() else 0
    print(f"  心跳：{ts(hb) if hb else '无'}（{int(now() - hb) if hb else '-'} 秒前）")
    print(f"  急停文件：{'存在（已停）' if STOP.exists() else '无'}")
    bl = {k: ts(v) for k, v in (s.get("blocked") or {}).items() if v > now()}
    print(f"  冷却中的目标：{bl or '无'}")
    print(f"  active 且有待办的目标：{[g['id'] for g in load_goals()]}")
    rows = [e for e in read_journal(200) if e.get("kind") == "run"][-5:]
    print("  最近 5 轮：")
    for e in rows:
        print(f"    [{ts(e['t'])}] {e.get('goal')} exit={e.get('exit')} "
              f"进展={'是' if e.get('progressed') else '否'} {e.get('dur')}s")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(prog="longrun", description="长跑引擎（无人值守推进长期目标）")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--once", action="store_true", help="只跑一轮")
    g.add_argument("--loop", action="store_true", help="常驻循环")
    g.add_argument("--status", action="store_true", help="打印状态")
    g.add_argument("--dry-run", action="store_true", help="不调模型，只打印本轮会派什么")
    ap.add_argument("--goal", help="只推指定目标 id")
    a = ap.parse_args()

    RUN_DIR.mkdir(parents=True, exist_ok=True)
    state = load_state()

    if a.status:
        return cmd_status()
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
