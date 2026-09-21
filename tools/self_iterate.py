#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""自我迭代循环 —— 把「我觉得自己哪里不行」换成「评估器说我哪里不行」。

三件套（AlphaEvolve 骨架，与 tools/gene_fitness.py 同口径）：
    候选池 → 客观评估器 → 选择压力
本模块补的是**闭环**：观测缺口 → 选题 → 行动 → 验证 → 落盘 → 止损。

为什么每轮必须挂在评估器读数上：
    「反思一下自己」这种动作没有可反驳的判据，跑一百轮也只是自我催眠。
    缺口必须能指到一条命令的输出（哪个指标、当前值、证据出处），否则不许进选题池。

有效轮的硬判据（防自嗨，本模块自己核，不信调用者自报）：
    轮末必须留下可被外部复核的产出 —— git 有改动、或落盘记录（接力棒/教训/信条）
    新增。两者都没有 = 无效轮。「我思考了一下」不算产出。

止损（无人值守时的刹车）：
    1. 连续 MAX_STREAK 轮无效 → 停
    2. 总轮数到预算 → 停
    3. 同一目标连修 STUCK_ROUNDS 轮指标没动 → 强制换目标（不许死磕）

CLI：
    self_iterate.py observe                     缺口清单（按优先级）
    self_iterate.py brief                       生成下一轮任务书
    self_iterate.py record --target T --action A --evidence E --since TS
    self_iterate.py status                      当前状态
    self_iterate.py start | stop                开关
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE_FILE = ROOT / "data" / "self_iterate.json"
TASK_ID = "task-self-iterate"
# 调度器靠这个 name 认领「哪些派发算自我迭代的轮次」——两边引用同一个常量，
# 复制一份字符串到 server.py 就会漂移（改名后预算计数静默失效）。
JOB_NAME = "自我迭代循环"

DEFAULT_BUDGET = 24          # 总轮数预算：无人值守时的钱袋子上限
MAX_STREAK = 3               # 连续无效轮上限 → 自动停
STUCK_ROUNDS = 2             # 同一目标连修 N 轮指标没动 → 强制换目标
EVAL_TIMEOUT = 180           # 单个评估器超时（秒）

# 落盘锚点：轮内只要这些文件的 mtime 或行数变了，就算「留下了可复核的产出」。
# 用 mtime 而不是内容 diff —— 追加式文件的「新增一行」比内容比对更不容易误判。
ANCHORS = [
    ROOT / "long_horizon.json",
    ROOT / "conviction.json",
    ROOT / "harness_task_memory.json",
]


def _load() -> dict:
    try:
        if STATE_FILE.exists():
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return {"running": False, "started_at": 0, "budget_rounds": DEFAULT_BUDGET,
            "stop_reason": "", "rounds": [], "dispatched": 0}


def _save(state: dict) -> bool:
    """原子写：中断时不会留下半个 JSON 把整个循环锁死。"""
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = str(STATE_FILE) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        os.replace(tmp, str(STATE_FILE))
        return True
    except Exception as e:
        print(f"⚠ 状态落盘失败：{e}", file=sys.stderr)
        return False


def _now() -> float:
    return time.time()


# ---------- 评估器 ----------
# 每个评估器 = 一条能跑出数字的命令 + 一个把数字翻成「缺口」的解析器。
# feasible 是手工标注的「下一步明确度」，三档，理由写在各自的 fix 注释里 ——
# 假装客观地全给 1.0，等于让量级单独决定选题，那又会回到「哪个数大修哪个」。

def _run_json(script: str, extra: list | None = None) -> dict | None:
    """跑一个评估器并解析 JSON。失败返回 None —— 一个评估器挂了不能拖垮整轮观测。"""
    cmd = [sys.executable, str(ROOT / "tools" / script), "--json"] + (extra or [])
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=EVAL_TIMEOUT, cwd=str(ROOT))
    except subprocess.TimeoutExpired:
        return None
    if p.returncode != 0:
        return None
    try:
        return json.loads(p.stdout)
    except Exception:
        return None


def _gap(gid: str, title: str, metric: str, evidence: str, action: str,
         gap_ratio: float, feasible: float) -> dict:
    gap_ratio = max(0.0, min(1.0, float(gap_ratio)))
    return {"id": gid, "title": title, "metric": metric, "evidence": evidence,
            "action": action, "gap_ratio": round(gap_ratio, 4),
            "feasible": feasible, "score": round(gap_ratio * feasible, 4)}


def eval_recidivism() -> list:
    """同错复发：首见之后还犯不犯。复发 = 教训没被真正学会，是最高优先的缺口类型。

    feasible=1.0：评估器自己给出了可执行 fix（harness 内自动 skill_help 后重试），
    不需要再调研「怎么办」，缺的是「动手做」。
    """
    d = _run_json("err_recidivism.py", ["--min-rec", "2"])
    if not d:
        return []
    out = []
    for c in d.get("classes") or []:
        rec = c.get("recurrence") or {}
        after = int(rec.get("after_first") or 0)
        if after <= 0:
            continue          # 首见后再没犯 = 已经学会，不该占选题位
        code = c.get("code") or "?"
        out.append(_gap(
            f"recidivism:{code}",
            f"同错复发 · {c.get('name') or code}（{c.get('count')} 次 / 首见后又犯 {after} 个 cycle）",
            f"复发 {after} cycle · 占比 {c.get('share')}%",
            f"err_recidivism.py --json: classes[{code}].count={c.get('count')}, "
            f"share={c.get('share')}%, recurrence.after_first={after}, "
            f"span={rec.get('span')}",
            c.get("fix") or "看原文定位",
            gap_ratio=float(c.get("share") or 0) / 100.0,
            feasible=1.0,
        ))
    return out


def _pra_ratios() -> tuple:
    """达标线从 prompt_rules_audit 导入，不复制常量 —— 双份常量必然漂移。"""
    try:
        sys.path.insert(0, str(ROOT / "tools"))
        import prompt_rules_audit as pra
        return pra.PLAN_MIN_RATIO, pra.VERIFY_MIN_RATIO
    except Exception:
        return 0.5, 0.6


def eval_rule_metrics() -> list:
    """准则段实测：多步轮有没有先列清单、改完码有没有同轮验证。

    达标线从 prompt_rules_audit 导入，不复制常量 —— 双份常量必然漂移。
    样本用最近 3 天加权合并（rate 的分子能反算：plan_rate × multi_rounds），
    单日 20 轮级的波动不该被当成趋势。
    """
    plan_min, verify_min = _pra_ratios()
    d = _run_json("prompt_rules_audit.py")
    if not d:
        return []
    trend = (d.get("trend") or [])[-3:]
    if not trend:
        return []

    def merged(num_key: str, den_key: str):
        num = sum((t.get(num_key) or 0) * (t.get(den_key) or 0) for t in trend)
        den = sum((t.get(den_key) or 0) for t in trend)
        return (num / den if den else None), den

    out = []
    rate, n = merged("plan_rate", "multi_rounds")
    if rate is not None and n >= 20 and rate < plan_min:
        out.append(_gap(
            "rules:plan_rate",
            f"多步轮没先列清单（{rate:.0%} < 达标线 {plan_min:.0%}）",
            f"{rate:.0%} · 样本 {n} 多步轮",
            f"prompt_rules_audit.py --json: trend 最近 3 天加权，"
            f"Σ(plan_rate×multi_rounds)/Σmulti_rounds={rate:.4f}, n={n}, "
            f"达标线 PLAN_MIN_RATIO={plan_min}",
            "查 _plan_miss_note 的 streak 判据是否被 eff_tool_calls 口径卡住（别动阈值）",
            gap_ratio=(plan_min - rate) / plan_min,
            feasible=0.8,   # 机制已有、埋点已通，缺的是核对判据；但提醒类改动效果本身不确定
        ))
    rate, n = merged("verify_rate", "edit_rounds")
    if rate is not None and n >= 20 and rate < verify_min:
        out.append(_gap(
            "rules:verify_rate",
            f"改完码没同轮验证（{rate:.0%} < 达标线 {verify_min:.0%}）",
            f"{rate:.0%} · 样本 {n} 改码轮",
            f"prompt_rules_audit.py --json: trend 最近 3 天加权，"
            f"Σ(verify_rate×edit_rounds)/Σedit_rounds={rate:.4f}, n={n}, "
            f"达标线 VERIFY_MIN_RATIO={verify_min}",
            "给改码轮补同轮验证检查点（模板见 plan_miss：纯函数计数 + armed + 结果注入）",
            gap_ratio=(verify_min - rate) / verify_min,
            feasible=0.8,
        ))
    return out


EVALUATORS = [eval_recidivism, eval_rule_metrics]


def observe() -> list:
    """跑全部评估器，合并缺口并按 score 降序。观测失败不是缺口，是噪音 —— 不混进来。"""
    gaps: list = []
    for fn in EVALUATORS:
        try:
            gaps.extend(fn() or [])
        except Exception as e:
            print(f"⚠ 评估器 {fn.__name__} 出错（跳过）：{e}", file=sys.stderr)
    gaps.sort(key=lambda g: -g["score"])
    return gaps


# ---------- 产出核查（有效轮的唯一判据来源） ----------

def _mtime(p: Path) -> float:
    try:
        return p.stat().st_mtime
    except Exception:
        return 0.0


def _git_touched(since_ts: float) -> list:
    """工作区里 since 之后被改过的文件。

    先看 git status 再按 mtime 过滤：只看 mtime 会把「别人/上一轮留下的脏文件」
    算成本轮产出，只看 status 又会把旧改动算进来。两者相交才是这一轮真动过的。
    """
    try:
        p = subprocess.run(["git", "status", "--porcelain"], capture_output=True,
                           text=True, timeout=30, cwd=str(ROOT))
    except Exception:
        return []
    if p.returncode != 0:
        return []
    out = []
    for line in p.stdout.splitlines():
        rel = line[3:].strip().strip('"')
        if " -> " in rel:                      # rename 行：取新名
            rel = rel.split(" -> ")[-1]
        f = ROOT / rel
        if f.is_file() and _mtime(f) >= since_ts:
            out.append(rel)
    return out


def produced_since(since_ts: float) -> dict:
    """轮内留下了什么可被外部复核的产出。空手而归就是无效轮。"""
    files = _git_touched(since_ts)
    anchors = [a.name for a in ANCHORS if _mtime(a) >= since_ts]
    return {"files": files, "anchors": anchors,
            "effective": bool(files or anchors)}


# ---------- 选题与止损 ----------

def _streak(state: dict) -> int:
    """尾部连续无效轮数。"""
    n = 0
    for r in reversed(state.get("rounds") or []):
        if r.get("effective"):
            break
        n += 1
    return n


def stuck_targets(state: dict) -> set:
    """同一目标连修 STUCK_ROUNDS 轮、缺口数值纹丝不动的目标。

    为什么要这个：死磕一个修不动的缺口，是无人值守时最贵的失败模式 ——
    预算烧完了，别处的缺口一个没碰。
    """
    out = set()
    by_target: dict = {}
    for r in state.get("rounds") or []:
        t = r.get("target")
        if not t:
            continue
        by_target.setdefault(t, []).append(r.get("gap_ratio"))
    for t, ratios in by_target.items():
        tail = ratios[-STUCK_ROUNDS:]
        if len(tail) >= STUCK_ROUNDS and len(set(tail)) == 1 and tail[0] is not None:
            out.add(t)
    return out


def pick(gaps: list, state: dict) -> dict | None:
    """选题：优先级最高的、且没被判定为「死磕」的缺口。"""
    skip = stuck_targets(state)
    for g in gaps:
        if g["id"] not in skip:
            return g
    return None


def spent(state: dict) -> int:
    """已花掉的轮数 = max(派发次数, 记账条数)。

    为什么不只数记账条数：执行体不调 record（忘了、挂了、卡住了），轮次就不涨，
    预算永远耗不完 —— 无人值守时这就是无限烧钱。派发计数由调度器写，不靠自觉。
    """
    return max(len(state.get("rounds") or []), int(state.get("dispatched") or 0))


def used(state: dict) -> int:
    """本次启动周期内已花的轮数。预算判它，不判累计 ——
    否则上一轮把额度花完后，重新点按钮会立刻又停（用户看到「启动失败」）。"""
    return max(0, spent(state) - int(state.get("epoch_start") or 0))


def tick() -> dict:
    """派发计数 +1。由调度器在每次派发时调，不是执行体的义务。"""
    state = _load()
    state["dispatched"] = int(state.get("dispatched") or 0) + 1
    state["last_ts"] = _now()
    v = verdict(state)
    if v["action"] == "stop":
        state["running"] = False
        state["stop_reason"] = v["reason"]
    _save(state)
    return state


def verdict(state: dict) -> dict:
    """该不该继续。返回 {action: continue|stop|switch, reason}。

    action=switch 不是停 —— 它表示「目标卡住了，换一个继续」，仍然算在跑。
    """
    rounds = state.get("rounds") or []
    budget = int(state.get("budget_rounds") or DEFAULT_BUDGET)
    if not state.get("running"):
        return {"action": "stop", "reason": "未启动"}
    if used(state) >= budget:
        return {"action": "stop", "reason": f"轮数预算耗尽（{used(state)}/{budget}）"}
    streak = _streak(state)
    if streak >= MAX_STREAK:
        return {"action": "stop",
                "reason": f"连续 {streak} 轮无有效产出 —— 空转比不动更贵，停下等指令"}
    return {"action": "continue", "reason": ""}


# ---------- 记账 ----------

def record(target: str, action: str, evidence: str, since_ts: float,
           note: str = "", gap_ratio: float | None = None) -> dict:
    """记一轮。有效性由 produced_since 自己核，不信调用者自报。

    evidence 是必填的：没有证据字符串的轮次，即使 git 有改动也只算「动了」，
    不算「证明了」。两者同时满足才是有效轮。
    """
    state = _load()
    prod = produced_since(since_ts)
    effective = bool(prod["effective"] and str(evidence or "").strip())
    entry = {
        "n": len(state.get("rounds") or []) + 1,
        "ts": _now(),
        "since": since_ts,
        "target": target,
        "action": action,
        "evidence": evidence,
        "note": note,
        "gap_ratio": gap_ratio,
        "files": prod["files"][:12],
        "anchors": prod["anchors"],
        "effective": effective,
    }
    state.setdefault("rounds", []).append(entry)
    state["last_ts"] = entry["ts"]
    v = verdict(state)
    if v["action"] == "stop":
        state["running"] = False
        state["stop_reason"] = v["reason"]
    _save(state)
    return entry


def start(budget: int = DEFAULT_BUDGET) -> dict:
    state = _load()
    # 每次启动开一个新的预算周期：epoch_start 记下启动时的累计消耗，
    # 预算判「本周期的花费」——不然额度花完后再点按钮，一启动就又被判超额。
    state["epoch_start"] = spent(state)
    state["running"] = True
    state["started_at"] = _now()
    state["stop_reason"] = ""
    state["budget_rounds"] = int(budget or DEFAULT_BUDGET)
    _save(state)
    return state


def stop(reason: str = "手动停止") -> dict:
    state = _load()
    state["running"] = False
    state["stop_reason"] = reason
    _save(state)
    return state


# ---------- 任务书 ----------

CONTRACT = """本轮契约（缺任一条，这轮就是无效轮）：
  1. 只做一件事：把上面这个缺口推进到「有客观变化」，不顺手做别的
  2. 留下可复核产出：代码改动 或 落盘（接力棒/教训/信条）——「我想了想」不算
  3. 记证据：self_iterate.py record --target {target} --action "..." --evidence "..." --since {since}
  4. 验证要能被推翻：反证 + 测试，说清什么观测会让你改判
纪律：同一目标连修 {stuck} 轮而指标没动就换目标，别死磕。"""


def job_task_text() -> str:
    """调度任务描述：执行体每一轮照着做的流程。

    放这里而不是 server.py：脚本要预先挂调度任务（主人睡前没人点按钮）时
    必须能用同一份文本 —— 两处各写一份就必然漂移。
    """
    return (
        "自我迭代循环一轮。严格按任务书执行，不要自由发挥：\n"
        "1. 跑 `venv/bin/python tools/self_iterate.py brief`，它给出本轮目标缺口、"
        "证据、建议动作和契约。\n"
        "2. 只干任务书里的那一件事；改完跑验证（反证 + 测试），判据要能被推翻。\n"
        "3. 落盘：`venv/bin/python tools/long_horizon.py log <id> \"...\" --ev \"...\"`，"
        "并把 next 改写成下一轮能直接开跑的原子动作。\n"
        "4. 记账：`venv/bin/python tools/self_iterate.py record --target <任务书的 target> "
        "--action \"...\" --evidence \"...\" --since <任务书里的 since>`。"
        "产出（代码改动/落盘）与证据字符串缺一不可，否则这轮算无效。\n"
        "5. 若任务书开头是「⛔ 自我迭代已停」，直接回报原因，不要重新启动。\n"
        f"纪律：同一目标连修 {STUCK_ROUNDS} 轮指标没动就换目标，别死磕。"
    )


def brief() -> str:
    """生成下一轮任务书。执行体照着做，不额外发挥。"""
    state = _load()
    v = verdict(state)
    rounds = state.get("rounds") or []
    n = used(state) + 1
    budget = int(state.get("budget_rounds") or DEFAULT_BUDGET)

    if v["action"] == "stop":
        return f"⛔ 自我迭代已停：{v['reason']}（共记账 {len(rounds)} 轮）。要续跑请重新 start。"

    gaps = observe()
    gap = pick(gaps, state)
    since = _now()
    head = f"【自我迭代 · 第 {n}/{budget} 轮】"

    if not gap:
        return (f"{head} 没有指标型缺口（评估器全部达标或样本不足）。\n"
                f"本轮是学习轮 —— 学先进 agent 的做法，产出必须可迁移：\n"
                f"  1. 读一个外部项目/论文（search_web / oss_contrib.py repo 挑健康仓库）\n"
                f"  2. 产出「带证据行号的可迁移清单」落 docs/<name>-study.md，不写读后感\n"
                f"  3. 从清单里挑一条真落进本仓，跑测试\n"
                f"  4. 落盘：long_horizon.py log + next\n"
                f"  5. 记轮：self_iterate.py record --target learn --action \"...\" "
                f"--evidence \"...\" --since {since}")

    lines = [head,
             f"目标缺口：{gap['id']} — {gap['title']}",
             f"  指标：{gap['metric']}",
             f"  证据：{gap['evidence']}",
             f"  建议动作：{gap['action']}",
             f"  优先级：score={gap['score']}（缺口 {gap['gap_ratio']} × 可行动 {gap['feasible']}）",
             ""]
    if len(gaps) > 1:
        lines.append("其余候选（本轮不碰）：")
        for g in gaps[1:4]:
            lines.append(f"  - {g['id']}（score={g['score']}）")
        lines.append("")
    lines.append(CONTRACT.format(target=gap["id"], since=int(since), stuck=STUCK_ROUNDS))
    return "\n".join(lines)


# ---------- 任务中心合成条目 ----------

def dismiss() -> bool:
    """「清除已完成」时把已收工的条目从列表里摘掉。返回是否真摘了。

    与 tools/plan_view.py:56 同一个坑：合成条目不在 orchestrator 注册表里，
    批量清除够不着它 —— 不记一笔的话 UI 上消失一秒、下一轮轮询又原样回来。
    还在跑时一律拒绝：那是用户正在看的进度，不是垃圾。
    """
    state = _load()
    if state.get("running"):
        return False
    if not (state.get("rounds") or state.get("stop_reason")):
        return False
    state["dismissed_at"] = _now()
    _save(state)
    return True


def _agent_meta() -> dict:
    return {"name": "自我迭代", "icon": "♾", "color": "#8b5cf6",
            "desc": "按评估器读数找自己的缺口 → 修一处 → 留证据 → 记账；连续无效自动停。"}


def snapshot(full: bool = False):
    """合成任务中心条目；从没跑过就返回 None（不常驻一条空条目刷屏）。"""
    try:
        return _snapshot(full)
    except Exception:
        return None


def _snapshot(full: bool):
    state = _load()
    rounds = state.get("rounds") or []
    if not rounds and not state.get("running"):
        return None
    # 已收工 + 用户点过「清除已完成」→ 不再占位（下次 start 会重新出现）。
    # dismissed_at 必须 > 0 才算「真点过」：两个字段都缺失时 0 >= 0 会成立，
    # 把从没被清除过的条目误判成已清除（测试抓到的真 bug）。
    _dismissed = float(state.get("dismissed_at") or 0)
    if (_dismissed > 0 and _dismissed >= float(state.get("last_ts") or 0)
            and not state.get("running")):
        return None

    running = bool(state.get("running"))
    good = sum(1 for r in rounds if r.get("effective"))
    streak = _streak(state)
    budget = int(state.get("budget_rounds") or DEFAULT_BUDGET)
    spent_n = used(state)
    stop_reason = state.get("stop_reason") or ""

    if running:
        status = "running"
        title = f"自我迭代 · 第 {spent_n + 1}/{budget} 轮 · 有效 {good}/{len(rounds)}"
    elif stop_reason and "连续" in stop_reason:
        status = "error"
        title = f"自我迭代 · 已停（{stop_reason}）· 有效 {good}/{len(rounds)}"
    else:
        status = "done"
        title = f"自我迭代 · 已收工（{stop_reason or '完成'}）· 有效 {good}/{len(rounds)}"

    steps = []
    if rounds:
        last = rounds[-1]
        mark = "✔" if last.get("effective") else "✘ 无效"
        steps.append(f"{mark} 第 {last.get('n')} 轮：{str(last.get('target'))[:40]}")
        steps.append(f"　产出：{', '.join(last.get('files') or []) or '（无代码改动）'}"
                     f"{'｜落盘 ' + ','.join(last.get('anchors') or []) if last.get('anchors') else ''}")
    if running:
        steps.append(f"○ 连续无效 {streak}/{MAX_STREAK}（到顶自动停）")

    ts = state.get("last_ts") or state.get("started_at") or _now()
    extra = {
        "self_iterate": True,
        "si": {
            "running": running,
            "rounds": rounds[-20:],
            "good": good,
            "total": len(rounds),
            "spent": spent_n,
            "streak": streak,
            "budget": budget,
            "stop_reason": stop_reason,
            "started_at": state.get("started_at") or 0,
        },
    }
    base = {
        "id": TASK_ID, "kind": "self_iterate", "channel": "self_iterate",
        "title": title, "status": status, "steps": steps,
        "result": "", "error": stop_reason if status == "error" else "",
        "confirm": False, "extra": extra, "dsh_session_id": "",
        "agent": _agent_meta(),
        "created_at": int((state.get("started_at") or ts) * 1000),
        "updated_at": int(ts * 1000),
    }
    if full:
        base["brief"] = "自我迭代循环：评估器找缺口 → 修一处 → 留证据 → 记账。"
        base["logs"] = [f"#{r.get('n')} [{r.get('target')}] {r.get('action')}｜"
                        f"{'有效' if r.get('effective') else '无效'}｜{r.get('evidence')}"
                        for r in rounds[-30:]]
    return base


# ---------- CLI ----------

def main() -> int:
    ap = argparse.ArgumentParser(description="自我迭代循环")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("observe")
    sub.add_parser("brief")
    sub.add_parser("status")
    p_rec = sub.add_parser("record")
    p_rec.add_argument("--target", required=True)
    p_rec.add_argument("--action", required=True)
    p_rec.add_argument("--evidence", required=True)
    p_rec.add_argument("--since", type=float, required=True)
    p_rec.add_argument("--note", default="")
    p_rec.add_argument("--gap-ratio", type=float, default=None)
    p_start = sub.add_parser("start")
    p_start.add_argument("--budget", type=int, default=DEFAULT_BUDGET)
    sub.add_parser("stop")
    sub.add_parser("tick")
    args = ap.parse_args()

    if args.cmd == "observe":
        gaps = observe()
        if not gaps:
            print("（无缺口：评估器全部达标，或样本不足）")
            return 0
        for g in gaps:
            print(f"[{g['score']:.3f}] {g['id']}  {g['title']}")
            print(f"        指标：{g['metric']}")
            print(f"        证据：{g['evidence']}")
            print(f"        动作：{g['action']}")
        return 0

    if args.cmd == "brief":
        print(brief())
        return 0

    if args.cmd == "record":
        e = record(args.target, args.action, args.evidence, args.since,
                   note=args.note, gap_ratio=args.gap_ratio)
        print(f"{'✔ 有效轮' if e['effective'] else '✘ 无效轮'} #{e['n']}｜{e['target']}")
        print(f"  代码改动：{', '.join(e['files']) or '（无）'}")
        print(f"  落盘锚点：{', '.join(e['anchors']) or '（无）'}")
        print(f"  证据：{e['evidence']}")
        if not e["effective"]:
            print("  判据：代码改动/落盘 与 证据字符串 必须同时具备")
        return 0 if e["effective"] else 1

    if args.cmd == "start":
        st = start(args.budget)
        print(f"✔ 自我迭代已启动，预算 {st['budget_rounds']} 轮。brief 可看本轮任务书。")
        return 0

    if args.cmd == "tick":
        st = tick()
        print(f"本周期 {used(st)}/{st.get('budget_rounds')}（累计 {spent(st)}）"
              f"{('｜已停：' + st['stop_reason']) if not st.get('running') else ''}")
        return 0

    if args.cmd == "stop":
        stop()
        print("✔ 已停。")
        return 0

    # status
    state = _load()
    v = verdict(state)
    rounds = state.get("rounds") or []
    good = sum(1 for r in rounds if r.get("effective"))
    print(f"运行中：{state.get('running')}｜本周期 {used(state)}/{state.get('budget_rounds')}"
          f"｜累计 {spent(state)}"
          f"｜记账 {len(rounds)} 轮（有效 {good}）｜连续无效 {_streak(state)}/{MAX_STREAK}")
    if state.get("stop_reason"):
        print(f"停止原因：{state['stop_reason']}")
    print(f"下一步：{v['action']} {v['reason']}")
    if stuck_targets(state):
        print(f"已判定死磕（换目标）：{', '.join(sorted(stuck_targets(state)))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())


def status() -> dict:
    """给 API / 按钮读的汇总。私有状态不往外抛，口径在这里统一。"""
    state = _load()
    v = verdict(state)
    rounds = state.get("rounds") or []
    return {
        "running": bool(state.get("running")),
        "spent": used(state),
        "total_spent": spent(state),
        "budget": int(state.get("budget_rounds") or DEFAULT_BUDGET),
        "rounds": len(rounds),
        "good": sum(1 for r in rounds if r.get("effective")),
        "streak": _streak(state),
        "max_streak": MAX_STREAK,
        "stuck": sorted(stuck_targets(state)),
        "stop_reason": state.get("stop_reason") or "",
        "next": v,
        "last": (rounds[-1] if rounds else None),
    }
