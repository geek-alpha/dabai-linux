#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""长跑引擎 → 任务中心视图（只读合成）。

长跑引擎由 systemd 拉起，是独立进程组，既不在 TaskOrchestrator 注册表里、
也跟 server 没有父子关系 —— 所以任务中心从头到尾看不见它，用户只能靠
`runner.py --status` 手动问。这里把它的磁盘事实（state / journal /
heartbeat / runner.lock + systemd 单元状态）合成一条与 orchestrator 同形状的
任务快照，由 /api/tasks 合并进列表。

只读契约：本模块不写任何文件、不启停任何进程；启停走 service_action()，
由显式 API 触发。
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

BASE = Path(__file__).resolve().parents[2]
RUN_DIR = BASE / "data" / "longrun"
STATE = RUN_DIR / "state.json"
JOURNAL = RUN_DIR / "journal.jsonl"
HEARTBEAT = RUN_DIR / "heartbeat"
STOP = RUN_DIR / "STOP"
LOCK = RUN_DIR / "runner.lock"
TRACES = RUN_DIR / "traces"

LEDGER = BASE / "long_horizon.json"
TASK_ID = "longrun-engine"
UNIT = "dabai-longrun.service"
WATCHDOG = "dabai-longrun-watchdog.timer"

# 一轮跑几分钟是常态，心跳只在一轮的头尾更新，所以「滞后」阈值取单轮上限量级；
# 超过它才算可疑，避免把正常长轮误报成卡死。
STALE_AFTER = 1800

_UNIT_CACHE: tuple = (0.0, "")   # (取数时刻, 单元状态)：任务中心每几秒轮询一次，
                                 # 不缓存就会给每个轮询都拉一个 systemctl 子进程。
_UNIT_TTL = 3.0


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def read_state() -> dict:
    s = _read_json(STATE, {}) or {}
    s.setdefault("cycle", 0)
    s.setdefault("budget", {})
    s.setdefault("blocked", {})
    s.setdefault("last_goal", None)
    return s


def read_journal(limit: int = 200) -> list:
    if not JOURNAL.exists():
        return []
    try:
        lines = JOURNAL.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return []
    out = []
    for ln in lines[-limit:]:
        try:
            out.append(json.loads(ln))
        except Exception:
            continue
    return out


# 与 runner.OWNER_HINTS 保持一致：接力棒里出现这些词 = 这轮动作在等主人
OWNER_HINTS = ("等主人", "需主人", "主人若", "主人本机", "主人批", "请主人", "主人先", "等你")


def owner_todos() -> list:
    """只有主人能做的事：显式挂起的 + 接力棒里点名等主人的。

    任务中心一直只回答「引擎在干什么」，回答不了「要主人干什么」——
    引擎卡在主人身上安静空转了 27 轮，屏幕上一点提示都没有。
    """
    todos = []
    for p in (_read_json(LEDGER, {}) or {}).get("projects", []):
        if p.get("stage") != "active":
            continue
        title = p.get("title") or p.get("id")
        if p.get("owner_block"):
            why = str((p.get("owner_block") or {}).get("why") or "").strip()
            todos.append({"id": p.get("id"), "title": title,
                          "why": why or "（未说明）", "kind": "block"})
            continue
        nxt = str(p.get("next") or "").strip()
        if any(h in nxt for h in OWNER_HINTS):
            todos.append({"id": p.get("id"), "title": title, "why": nxt, "kind": "next"})
    return todos


def _proc_alive(pid: int) -> bool:
    """PID 是否活着且确实是 runner（防 PID 复用误判）。"""
    if not pid:
        return False
    try:
        cmd = (Path("/proc") / str(pid) / "cmdline").read_bytes().decode("utf-8", "replace")
    except Exception:
        return False
    return "longrun/runner.py" in cmd


def engine_pid() -> int:
    """runner 拿到单实例锁后会把 PID 写进 runner.lock —— 这是最直接的存活证据，
    不依赖 systemd 是否可用。"""
    try:
        return int(LOCK.read_text(encoding="utf-8").strip() or 0)
    except Exception:
        return 0


def _systemctl(*args: str, timeout: float = 4.0):
    env = dict(os.environ)
    env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    try:
        p = subprocess.run(["systemctl", "--user", *args], capture_output=True,
                           text=True, timeout=timeout, env=env)
        return p.returncode, (p.stdout or p.stderr or "").strip()
    except Exception as e:
        return 127, f"{type(e).__name__}: {e}"


def unit_state(refresh: bool = False) -> str:
    global _UNIT_CACHE
    ts, val = _UNIT_CACHE
    if not refresh and val and time.time() - ts < _UNIT_TTL:
        return val
    rc, out = _systemctl("is-active", UNIT)
    val = out.splitlines()[0].strip() if out else ("unknown" if rc else "unknown")
    _UNIT_CACHE = (time.time(), val or "unknown")
    return _UNIT_CACHE[1]


def trace_cycles() -> list:
    """哪些轮次留下了可下钻的 trace（文件名即轮次）。"""
    if not TRACES.exists():
        return []
    out = []
    for p in TRACES.glob("*.jsonl"):
        try:
            out.append(int(p.stem))
        except ValueError:
            continue
    return sorted(out)


def read_trace(cycle: int, limit: int = 4000) -> dict:
    """读某一轮的 trace 事件流（下钻用）。不存在不是错误，是“这轮没留”。"""
    p = TRACES / f"{int(cycle)}.jsonl"
    if not p.exists():
        return {"ok": False, "cycle": int(cycle), "events": [], "lines": 0,
                "error": "该轮没有 trace（引擎升级前跑的轮次）"}
    events = []
    for ln in p.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            events.append(json.loads(ln))
        except Exception:
            continue
    total = len(events)
    return {"ok": True, "cycle": int(cycle), "events": events[-limit:],
            "lines": total, "truncated": total > limit, "path": str(p)}


def heartbeat_age() -> float:
    try:
        return max(0.0, time.time() - HEARTBEAT.stat().st_mtime)
    except Exception:
        return -1.0


def _fmt_age(sec: float) -> str:
    if sec < 0:
        return "无心跳"
    if sec < 60:
        return f"{int(sec)} 秒前"
    if sec < 3600:
        return f"{int(sec // 60)} 分钟前"
    return f"{sec / 3600:.1f} 小时前"

_ARG_KEYS = ("path", "file_path", "file", "command", "query", "pattern",
             "url", "name", "skill", "symbol", "root")


def _arg_hint(arguments) -> str:
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
        v = data.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip().replace("\n", " ")[:70]
    for v in data.values():
        if isinstance(v, str) and v.strip():
            return v.strip().replace("\n", " ")[:70]
    return raw[:70]


def live_cycle() -> dict:
    """此刻正在跑的轮次（从 trace 现场取证）。

    journal 只在轮次收工时落盘——只看它，一轮跑 20 分钟的长活会被读成「没在跑」，
    任务中心那一栏就僵在上一轮的时间戳上。心跳新鲜、且这一轮还没落盘，才算进行中。
    """
    age = heartbeat_age()
    if age < 0 or age > 180:
        return {}
    fresh = [p for p in TRACES.glob("*.jsonl")
             if p.stem.isdigit() and time.time() - p.stat().st_mtime < 180]
    if not fresh:
        return {}
    p = max(fresh, key=lambda q: q.stat().st_mtime)
    cycle = int(p.stem)
    # 已落盘 = 这轮早收工了；刚跑完那一秒不该被报成「进行中」
    done = {int(e.get("cycle") or 0) for e in read_journal(80) if e.get("kind") == "run"}
    if cycle in done:
        return {}
    started, goal, calls, last = None, None, 0, None
    for ln in p.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            ev = json.loads(ln)
        except Exception:
            continue
        typ = ev.get("type")
        if typ == "start":
            started = ev.get("t") or started
        elif typ == "prompt":
            started = started or ev.get("t")
            goal = goal or ev.get("goal")
        elif typ == "ToolCallStart":
            calls += 1
            last = {"name": ev.get("tool_name") or "?", "hint": _arg_hint(ev.get("arguments"))}
    return {"cycle": cycle, "goal": goal or "",
            "started": started or p.stat().st_mtime, "calls": calls, "last": last,
            "idle": int(time.time() - p.stat().st_mtime)}


def _hm(t: float) -> str:
    return time.strftime("%m-%d %H:%M:%S", time.localtime(t or 0))


def service_action(action: str) -> tuple:
    """启停长跑引擎（显式调用；watchdog 一并处理，否则它会把停掉的服务拉起来）。"""
    action = (action or "").strip().lower()
    if action in ("start", "resume"):
        # 先清急停文件：STOP 存在时 runner 会立刻优雅退出，等于「启动了但没跑」。
        try:
            STOP.unlink(missing_ok=True)
        except Exception:
            pass
        rc1, o1 = _systemctl("start", UNIT)
        rc2, o2 = _systemctl("start", WATCHDOG)
        unit_state(refresh=True)
        ok = rc1 == 0
        return ok, (f"长跑引擎已启动（{o1 or 'ok'}）" if ok else f"启动失败：{o1}")
    if action in ("stop", "pause"):
        # 先停 watchdog：它按心跳判活，会立刻把刚停掉的服务拉起来。
        _systemctl("stop", WATCHDOG)
        rc, out = _systemctl("stop", UNIT)
        unit_state(refresh=True)
        return rc == 0, (f"长跑引擎已停止（{out or 'ok'}）" if rc == 0 else f"停止失败：{out}")
    if action == "restart":
        _systemctl("stop", WATCHDOG)
        rc, out = _systemctl("restart", UNIT)
        _systemctl("start", WATCHDOG)
        unit_state(refresh=True)
        return rc == 0, (f"长跑引擎已重启（{out or 'ok'}）" if rc == 0 else f"重启失败：{out}")
    if action == "status":
        return True, unit_state(refresh=True)
    return False, f"未知动作：{action}（可用 start/stop/restart/status）"


def snapshot(full: bool = False) -> dict:
    """合成任务中心里的一条长跑任务。任何异常都不该拖垮任务中心列表。"""
    try:
        return _snapshot(full)
    except Exception as e:
        return {"id": TASK_ID, "kind": "longrun", "channel": "longrun",
                "title": "长跑引擎（状态读取失败）", "status": "error",
                "steps": [], "logs": [], "result": "", "error": f"{type(e).__name__}: {e}",
                "confirm": False, "extra": {"longrun": True}, "dsh_session_id": "",
                "agent": _agent_meta(), "created_at": 0, "updated_at": 0}


def _agent_meta() -> dict:
    try:
        from task_orchestrator import AGENTS
        if "longrun" in AGENTS:
            return dict(AGENTS["longrun"])
    except Exception:
        pass
    return {"name": "长跑引擎", "icon": "♾️", "color": "#a78bfa",
            "desc": "无人值守推进长期目标的后台引擎。"}


def _snapshot(full: bool) -> dict:
    st = read_state()
    runs = [e for e in read_journal(300) if e.get("kind") in ("run", "idle")]
    last = runs[-1] if runs else {}
    pid = engine_pid()
    alive = _proc_alive(pid)
    unit = unit_state()
    age = heartbeat_age()
    cycle = int(st.get("cycle") or 0)
    goal = str(st.get("last_goal") or last.get("goal") or "")
    budget = st.get("budget") or {}
    blocked = {k: v for k, v in (st.get("blocked") or {}).items() if v > time.time()}
    stale = alive and age > STALE_AFTER
    todos = owner_todos()

    if alive:
        status = "running"
    elif unit == "failed":
        status = "error"
    else:
        status = "cancelled"

    lv = live_cycle()
    if alive and lv:
        el = int(time.time() - (lv.get("started") or time.time()))
        title = (f"长跑引擎 · 第 {lv['cycle']} 轮进行中 · {lv.get('goal') or goal or '—'}"
                 f"（已跑 {el // 60}分{el % 60}秒）")
    elif alive:
        title = f"长跑引擎 · 第 {cycle} 轮 · {goal or '空闲'}"
    else:
        title = f"长跑引擎（已停止）· 第 {cycle} 轮"
        if todos:
            title += f" · ⏳ 等你决定 {len(todos)} 件"

    steps = []
    if lv:
        el = int(time.time() - (lv.get("started") or time.time()))
        lv_last = lv.get("last") or {}
        doing = f"{lv_last.get('name')} {lv_last.get('hint', '')}".strip() if lv_last else "还没调工具"
        steps.append(f"▶ 第 {lv['cycle']} 轮进行中 · 已跑 {el // 60}分{el % 60}秒 · "
                     f"工具 {lv['calls']} 次 · 最近：{doing}")
    for e in runs[-6:]:
        if e.get("kind") == "idle":
            steps.append(f"[{_hm(e.get('t'))}] 第 {e.get('cycle')} 轮：空转（{e.get('note') or ''}）")
            continue
        mark = "✓ 有进展" if e.get("progressed") else f"✗ 无进展（{e.get('reason') or '台账没变'}）"
        steps.append(f"[{_hm(e.get('t'))}] 第 {e.get('cycle')} 轮 [{e.get('goal')}] {mark} "
                     f"{e.get('dur')}s" + ("（已冷却）" if e.get("blocked") else ""))

    if todos:
        # 卡点不抢实时进度位：运行中时 steps[0] 必须仍是「▶ 第 N 轮进行中」
        # （前端第一行是看进度的固定位置，插到前面会让「在跑」看起来像「停了」）。
        # 引擎停了才把卡点顶到最前——那时它就是首要信息。
        steps.insert(1 if lv else 0, "⏳ 等你决定 %d 件：%s" % (
            len(todos), "；".join(str(t["title"]) for t in todos[:3])))

    logs = []
    if alive:
        logs.append(f"引擎在线 pid={pid}，单元 {unit}，心跳 {_fmt_age(age)}")
    else:
        logs.append(f"引擎未运行（单元 {unit}）" + (f"，最后心跳 {_fmt_age(age)}" if age >= 0 else ""))
    if stale:
        logs.append(f"⚠️ 心跳已滞后 {_fmt_age(age)}，可能卡在某一轮（单轮上限 30 分钟）")
    logs.append(f"今日调用 {budget.get('calls', 0)}/200 · 台账目标 {goal or '—'}")
    if blocked:
        logs.append("冷却中的目标：" + "、".join(f"{k}（至 {_hm(v)}）" for k, v in blocked.items()))
    if todos:
        logs.append("⏳ 等主人：" + "；".join(
            f"{t['title']}——{str(t['why'])[:60]}" for t in todos))
    if STOP.exists():
        logs.append(f"急停文件存在：{STOP}（引擎下一轮会优雅退出）")
    if not alive and unit == "active":
        logs.append("单元显示 active 但未发现 runner 进程 —— 可能正在启动或已异常退出")
    for e in runs[-8:]:
        if e.get("kind") == "run":
            logs.append(f"[{_hm(e.get('t'))}] 第 {e.get('cycle')} 轮 exit={e.get('exit')} "
                        f"进展={'是' if e.get('progressed') else '否'} {e.get('dur')}s")

    result = ""
    if last.get("kind") == "run":
        head = f"第 {last.get('cycle')} 轮 · {last.get('goal')} · 本轮动作：{last.get('action')}\n\n"
        result = head + str(last.get("out_tail") or "")

    created = (runs[0].get("t") if runs else 0) or (STATE.stat().st_mtime if STATE.exists() else 0)
    updated = max([t for t in (last.get("t") or 0,
                               (HEARTBEAT.stat().st_mtime if HEARTBEAT.exists() else 0))] or [0])

    base = {
        "id": TASK_ID, "kind": "longrun", "channel": "longrun",
        "title": title, "status": status,
        "steps": steps if full else ((steps[:1] + steps[-3:]) if lv else steps[-4:]),
        "result": result if full else result[:200],
        "error": "" if (last.get("ok") or not last) else f"上一轮未推进：{last.get('reason') or '未知'}",
        "confirm": False,
        "extra": {"longrun": True, "cycle": cycle, "goal": goal,
                  # 只在进程真活着时报 pid：runner.lock 会留着上一代 PID，
                  # 直接透出去会让人以为引擎还在跑。
                  "pid": pid if alive else None,
                  "unit": unit, "heartbeat_age": int(age) if age >= 0 else None,
                  "budget_today": int(budget.get("calls") or 0),
                  "stop_file": STOP.exists(), "blocked": list(blocked),
                  "owner_todos": todos,
                  "live": lv or None,
                  "run_dir": str(RUN_DIR)},
        "dsh_session_id": "", "agent": _agent_meta(),
        "created_at": int((created or 0) * 1000),
        "updated_at": int((updated or time.time()) * 1000),
    }
    if full:
        base["brief"] = ("无人值守推进长期目标的后台引擎：一轮 = 从台账取一个 active 目标 → 执行它的 "
                         "next 原子动作 → 用「台账有没有被更新」判定进展 → 落盘 → 睡。"
                         f"台账：{BASE / 'long_horizon.json'}；运行数据：{RUN_DIR}")
        base["logs"] = logs
        base["log_lines"] = logs
        have = set(trace_cycles())
        rounds = []
        for e in runs[-20:]:
            if e.get("kind") != "run":
                continue
            cyc = int(e.get("cycle") or 0)
            rounds.append({"cycle": cyc, "t": int(e.get("t") or 0), "goal": e.get("goal") or "",
                           "ok": bool(e.get("progressed")), "exit": e.get("exit"),
                           "dur": e.get("dur"), "reason": e.get("reason") or "",
                           "tools": e.get("tools") or [], "tool_count": int(e.get("tool_count") or 0),
                           "usage": e.get("usage") or {}, "trace": cyc in have})
        base["extra"]["rounds"] = rounds
        base["extra"]["trace_cycles"] = sorted(have)[-50:]
    else:
        base["logs_count"] = len(logs)
        base["logs_tail"] = logs[-8:]
    return base


if __name__ == "__main__":
    print(json.dumps(snapshot(full=True), ensure_ascii=False, indent=2))
