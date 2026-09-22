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
import re
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

# 确定性环境错误（余额不足/鉴权失败）：重试、换目标、换人都救不了，只有外部条件恢复。
# 实测 2026-09-22：402 每 40 分钟打断一次，派发 0.8 秒即失败，而 dispatched 照涨 ——
# 24 格预算会在 11 小时内被空转吃光，循环最后以「轮数预算耗尽」这个假原因停下。
ENV_BLOCK_MAX = 3            # 连续 N 次派发即遇环境错误 → 停，并写清真因
ENV_BLOCK_PAT = re.compile(
    r"Insufficient Balance|Error code: 402|Error code: 401"
    r"|invalid_api_key|Unauthorized|authentication_error")

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


def _replay_state() -> dict:
    """历史失败调用在「现在的校验层」下重放后的存活情况，按错误类别索引。

    err_recidivism 读的是冻结的 traces —— 修好之后那个计数不会动，所以「这个缺口还
    值不值得修」只能看重放。重放脚本挂了返回空 dict，由 _feasible_after_replay 当
    「未验证」处理，不静默降级。
    """
    d = _run_json("replay_validation.py")
    if not d:
        return {}
    classes = d.get("classes")
    return classes if isinstance(classes, dict) else {}


def _feasible_after_replay(code: str, count: int, replay: dict) -> tuple:
    """重放里已经修死的类别不再占选题位。缺任一条判据就保持 1.0。

    四条同时成立才置 0：重放里有样本、覆盖了该类全部历史失败（只重放一半不算修死）、
    没有「本机制能覆盖却仍报同类错」的行、也没有结构性残留把判据堵死。

    为什么只看 still_fixable 而不看 still：重放里剩下的行有两类——一类是本轮修复
    机制对它生效的（envelope 归一，still_fixable），这类还失败就是真没修好，必须
    拦住；另一类是机制对它无效的（模型整组传错参数、args 被写入端截断无法还原），
    拿它们把 feasible 钉在 1.0 就是「修好了也永远退不出选题」的死循环（历史上 C 类
    混进 4 条不可修行就是这个下场）。unjudgeable 要单独报出来，不许静默吞掉。
    """
    r = replay.get(code) or {}
    total = int(r.get("total") or 0)
    if not total:
        return 1.0, "重放未覆盖该类，按未验证处理"
    if total < int(count or 0):
        return 1.0, f"重放仅覆盖 {total}/{count} 次，未全量验证"
    still = int(r.get("still") or 0)
    gone = int(r.get("gone") or 0)
    unjudge = int(r.get("unjudgeable") or 0)
    if still > 0:
        # 缺 still_fixable 字段（旧读数/手写缺口）退回 still，不静默放宽判据。
        fixable = int(r.get("still_fixable", still) or 0)
        if fixable > 0:
            return 1.0, f"重放里仍报同类错 {fixable} 次（本轮修复机制可覆盖）"
        return 0.0, (f"重放 {total} 次已无本机制可治的同类错（原错误消失 {gone}）；"
                     f"残留 {still} 次为结构性不可治（args 截断不可判 {unjudge}）")
    if unjudge:
        return 0.0, (f"重放 {total} 次已无同类错（原错误消失 {gone}）；"
                     f"另有 args 截断不可判 {unjudge} 次，不参与判据")
    return 0.0, f"重放 {total} 次已无同类错（原错误消失 {gone}）"


def eval_recidivism() -> list:
    """同错复发：首见之后还犯不犯。复发 = 教训没被真正学会，是最高优先的缺口类型。

    feasible 由重放决定：err_recidivism 的计数读的是冻结 traces，修好也不会变，
    拿它当「还剩多少活」会永远把已修死的类别顶在第一位。
    """
    d = _run_json("err_recidivism.py", ["--min-rec", "2"])
    if not d:
        return []
    replay = _replay_state()
    out = []
    for c in d.get("classes") or []:
        rec = c.get("recurrence") or {}
        after = int(rec.get("after_first") or 0)
        if after <= 0:
            continue          # 首见后再没犯 = 已经学会，不该占选题位
        code = c.get("code") or "?"
        count = int(c.get("count") or 0)
        feasible, why = _feasible_after_replay(code, count, replay)
        out.append(_gap(
            f"recidivism:{code}",
            f"同错复发 · {c.get('name') or code}（{c.get('count')} 次 / 首见后又犯 {after} 个 cycle）",
            f"复发 {after} cycle · 占比 {c.get('share')}%",
            f"err_recidivism.py --json: classes[{code}].count={c.get('count')}, "
            f"share={c.get('share')}%, recurrence.after_first={after}, "
            f"span={rec.get('span')}；重放判定：{why}",
            c.get("fix") or "看原文定位",
            gap_ratio=float(c.get("share") or 0) / 100.0,
            feasible=feasible,
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
    # 判据用「埋点覆盖轮」上的读数：老数据里「跑 pytest」与「跑 ls」分不出来，全量
    # 读数被系统性低估且低估量不可知——拿它派任务，派下去的是测不准的东西。
    # 覆盖轮不足时 merged 返回 None，这一项自然不出缺口（先攒埋点，别硬判）。
    rate, n = merged("verify_rate_covered", "covered_edit_rounds")
    if rate is not None and n >= 10 and rate < verify_min:
        out.append(_gap(
            "rules:verify_rate",
            f"改完码没同轮验证（{rate:.0%} < 达标线 {verify_min:.0%}）",
            f"{rate:.0%} · 样本 {n} 埋点覆盖改码轮",
            f"prompt_rules_audit.py --json: trend 最近 3 天加权，"
            f"Σ(verify_rate_covered×covered_edit_rounds)/Σcovered_edit_rounds={rate:.4f}, "
            f"n={n}, 达标线 VERIFY_MIN_RATIO={verify_min}",
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
        # 缺账的轮沿用该目标上一个已知读数：任务书里的 record 命令长期只有四个参数，
        # 第 5/6 轮 gap_ratio 全是 null，旧判据那句 `tail[0] is not None` 于是让
        # 「死磕」永远判不出来 —— 指标已连修两轮纹丝不动的 rules:plan_rate，
        # 第 7 轮又被派了一次。没记 ≠ 读数变了：真变了，落账的那一轮会写下新数字，
        # 集合里自然出现两个值，照样不判死磕。
        filled, last = [], None
        for v in ratios:
            if v is not None:
                last = v
            filled.append(last)
        tail = filled[-STUCK_ROUNDS:]
        if len(tail) >= STUCK_ROUNDS and len(set(tail)) == 1 and tail[0] is not None:
            out.add(t)
    return out


def pick(gaps: list, state: dict) -> dict | None:
    """选题：优先级最高的、且没被判定为「死磕」或「已修死」的缺口。

    feasible<=0 = 重放证明这类错已经修死（冻结 traces 的计数不会动），再修就是空转。
    只跳过显式声明不可行的：手写缺口没有该字段，按未知处理。
    """
    skip = stuck_targets(state)
    for g in gaps:
        if g["id"] in skip:
            continue
        f = g.get("feasible")
        if f is not None and float(f) <= 0:
            continue
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


def is_env_block(text: str) -> bool:
    """这段文字是不是「确定性环境错误」——余额不足/鉴权失败，不是执行体不行。

    判据取调度器的 last_result：子智能体出错时原文带 "Error code: 402 -
    {'error': {'message': 'Insufficient Balance' ...}}"。这类错误重试必然还是错，
    只有外部条件恢复才行。
    """
    return bool(ENV_BLOCK_PAT.search(str(text or "")))


def tick() -> dict:
    """派发计数 +1。由调度器在每次派发时调，不是执行体的义务。

    退回环境阻塞那一格预算的是 note_dispatch_result（拿到结局时才算得准），
    这里只管计数 —— 两者都在这里退会重复退。
    """
    state = _load()
    state["dispatched"] = int(state.get("dispatched") or 0) + 1
    state["last_ts"] = _now()
    v = verdict(state)
    if v["action"] == "stop":
        state["running"] = False
        # 「未启动」不是新原因：别把上一次的真停因（环境阻塞）覆盖掉，
        # 否则 resume_plan 再也认不出它、重启后不会自己拉起来。
        if v.get("reason") != "未启动":
            state["stop_reason"] = v["reason"]
            state["stop_kind"] = v.get("kind") or ""
    _save(state)
    return state


def note_dispatch_result(text: str) -> dict:
    """一次派发的结局原文（子智能体汇报链路）。环境阻塞失败的那格预算当场退回。

    为什么要在拿到结局时退、而不是等下次派发：断轮看门狗把「派发了没记账」当成被
    重启掐掉的轮，20 分钟后立刻补派一次。照扣的话，402 空转既吃预算又每 20 分钟
    白派一次，预算会在几小时内被烧光，循环最后以「轮数预算耗尽」这个假原因停下。
    退回后 dispatched == 已记账轮数，看门狗不再把空转当断轮。
    """
    state = _load()
    if not is_env_block(text):
        return state
    if int(state.get("dispatched") or 0) <= len(state.get("rounds") or []):
        return state          # 没有待认领的派发（已退过，或那一轮真记了账）
    state["dispatched"] = int(state.get("dispatched") or 0) - 1
    state["env_blocked"] = int(state.get("env_blocked") or 0) + 1
    state["env_blocked_reason"] = str(text or "")[:200]
    v = verdict(state)
    if v["action"] == "stop" and v.get("reason") != "未启动":
        state["running"] = False
        state["stop_reason"] = v["reason"]
        state["stop_kind"] = v.get("kind") or ""
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
    if int(state.get("env_blocked") or 0) >= ENV_BLOCK_MAX:
        return {"action": "stop", "kind": "env",
                "reason": "连续 %d 次派发被环境阻塞（%s）—— 不是执行体不行，等外部恢复"
                          % (ENV_BLOCK_MAX,
                             str(state.get("env_blocked_reason") or "")[:120])}
    if used(state) >= budget:
        return {"action": "stop", "reason": f"轮数预算耗尽（{used(state)}/{budget}）"}
    streak = _streak(state)
    if streak >= MAX_STREAK:
        return {"action": "stop",
                "reason": f"连续 {streak} 轮无有效产出 —— 空转比不动更贵，停下等指令"}
    return {"action": "continue", "reason": ""}


def resume_plan(state: dict | None = None) -> dict:
    """重启后该不该把循环拉起来。纯读，不改状态。

    否决只有两种：用户按过停止、或者本来就该停（预算耗尽/连续无效）。
    预算耗尽后重启不能偷偷续命 —— 无人值守时那就是无限烧钱。
    """
    state = _load() if state is None else state
    # 环境阻塞停的不算「本来就该停」：外部条件（余额/鉴权）恢复后它就该接着跑，
    # 否则主人充完值还得手动点一次，而没人值守时那等于永久停摆。
    if not state.get("running"):
        if str(state.get("stop_kind") or "") == "env":
            return {"resume": True, "reason": "上次停因是环境阻塞（%s），重新试"
                                              % (state.get("stop_reason") or "")}
        return {"resume": False, "reason": state.get("stop_reason") or "未启动"}
    v = verdict(state)
    if v["action"] == "stop":
        if v.get("kind") == "env":
            return {"resume": True, "reason": v["reason"]}
        return {"resume": False, "reason": v["reason"]}
    budget = int(state.get("budget_rounds") or DEFAULT_BUDGET)
    return {"resume": True, "reason": f"继续第 {used(state) + 1}/{budget} 轮"}



# 派发后多久没记账判为「断轮」。实测三轮真实耗时 4.6 / 5.8 / 10.0 分钟，
# 排期间隔 40 分钟；取 20 分钟 = 最长轮的两倍，既不会把正常慢轮当断轮，
# 也不会让一次重启白丢整整一个周期。
STALL_SEC = 20 * 60


def stall_plan(state: dict | None = None, job: dict | None = None,
               now: float | None = None) -> dict:
    """循环停摆了吗、该怎么救。纯读，不改状态。

    三种停摆各自独立，表现出来都是「它不动了」：
      ① 状态说要跑，调度任务却不存在/被停用（改码重启后 reconcile 没跑到）
      ② 上一轮派发了但没记账（重启杀掉执行体、执行体卡死）—— 等下一个间隔
         就是白丢一轮，必须立刻补
      ③ 调度任务的 running 标记悬挂（_sweep_stale 要等 2 小时才清）
    返回 {action: none|resume|release|wake, reason}。
    """
    state = _load() if state is None else state
    now = _now() if now is None else now
    if not state.get("running"):
        return {"action": "none", "reason": "未启动"}
    if job is None or not job.get("enabled"):
        return {"action": "resume", "reason": "状态说在跑，调度任务却不存在或被停用"}
    if job.get("running") and now - float(job.get("last_run_at") or 0) > STALL_SEC:
        return {"action": "release",
                "reason": "调度任务的运行标记悬挂超过 %d 分钟" % (STALL_SEC // 60)}
    dispatched = int(state.get("dispatched") or 0)
    recorded = len(state.get("rounds") or [])
    last_ts = float(state.get("last_ts") or 0)
    if dispatched > recorded and now - last_ts > STALL_SEC:
        return {"action": "wake",
                "reason": "第 %d 轮派发后 %d 分钟没有记账（执行体多半被重启打断）"
                          % (dispatched, int((now - last_ts) // 60))}
    # 本周期启动后一直没派发过，而排期还在 STALL_SEC 之外 = 第一轮被排在了启动之前的
    # 旧排期上（实测：09:56:36 点启动，next_run_at 仍是第 12 轮派发算出的 10:16:26，
    # 界面显示「运行中」实际不动）。两个 STALL_SEC 同时成立才判停摆：只差几秒不算，
    # 正常等一个 40 分钟间隔也不误伤（提前派发会撞上上一轮还没改完的文件）。
    started = float(state.get("started_at") or 0)
    next_at = float(job.get("next_run_at") or 0)
    if (not job.get("running") and started > float(job.get("last_run_at") or 0)
            and now - started > STALL_SEC and next_at > now + STALL_SEC):
        return {"action": "wake",
                "reason": "本周期启动 %d 分钟还没派发过，排期却还要等 %d 分钟"
                          % (int((now - started) // 60), int((next_at - now) // 60))}
    return {"action": "none", "reason": ""}


def note_wake(action: str, reason: str) -> dict:
    """看门狗动作落盘（留痕上限 20 条）。

    没有痕迹就看不出它救过几轮 —— 「机制在跑」和「机制真救过人」是两件事。
    """
    state = _load()
    seq = state.setdefault("wakeups", [])
    seq.append({"ts": _now(), "action": action, "reason": reason})
    state["wakeups"] = seq[-20:]
    _save(state)
    return state


# ---------- 记账 ----------

# gap_ratio 自报值的容差：observe 的读数保留 4 位小数，差得比这大就是「自报的数」
# 与「现算读数」不是一回事。
GAP_RATIO_TOL = 0.005


def _live_gap_ratio(target: str) -> float | None:
    """用 observe() 现算的读数核对调用者自报的缺口比例；算不出来返回 None。

    为什么要核对：gap_ratio 是「死磕判定」的输入，而这个判定决定换不换目标。让它由
    执行体自报，等于把判据的输入权交给被审判者——自报一个比真实读数大得多的数，
    目标就永远判不出死磕。所以落账一律以现算读数为准。
    只对「评估器缺口」类目标核对（id 形如 kind:code）：learn / 批判定向这类目标本来
    就不在观测里，硬核对会把正常轮判成无效。
    """
    if not target or ":" not in str(target):
        return None
    try:
        for g in observe():
            if str(g.get("id")) == str(target):
                return float(g.get("gap_ratio"))
    except Exception:
        return None
    return None


def record(target: str, action: str, evidence: str, since_ts: float,
           note: str = "", gap_ratio: float | None = None) -> dict:
    """记一轮。有效性由 produced_since 自己核，不信调用者自报。

    evidence 是必填的：没有证据字符串的轮次，即使 git 有改动也只算「动了」，
    不算「证明了」。两者同时满足才是有效轮。
    """
    state = _load()
    prod = produced_since(since_ts)
    effective = bool(prod["effective"] and str(evidence or "").strip())
    # --gap-ratio 只是「声明」，落账前一律用 observe() 现算的读数核对：计数权不在
    # 调用者手里，否则一句自报的数就能让「死磕」判不出来。
    ratio_note = ""
    if gap_ratio is not None and ":" in str(target):
        live = _live_gap_ratio(target)
        if live is None:
            raise ValueError(
                f"--gap-ratio 无法核对：observe() 里现在没有 {target} 这个缺口"
                "（可能已修掉，或评估器读数为 None）。先跑 self_iterate.py observe 看现场。")
        if abs(live - float(gap_ratio)) > GAP_RATIO_TOL:
            ratio_note = (f"[gap_ratio 以 observe 现算为准：自报 {float(gap_ratio):.4f} → "
                          f"{live:.4f}]")
        gap_ratio = live
    note = " ".join(x for x in (str(note or "").strip(), ratio_note) if x)
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
    # 真记了一笔账 = 环境是通的，环境阻塞计数从零开始
    state["env_blocked"] = 0
    state["env_blocked_reason"] = ""
    v = verdict(state)
    if v["action"] == "stop":
        state["running"] = False
        # 与 tick / note_dispatch_result 同一条守卫：执行体在循环已被停掉之后才记账时，
        # verdict 只会回「未启动」，照写就把真停因（手动停止 / 环境阻塞）冲成一句废话，
        # 界面和 resume_plan 都再也认不出它。第 1 轮、第 13 轮各踩过一次。
        if v.get("reason") != "未启动":
            state["stop_reason"] = v["reason"]
            state["stop_kind"] = v.get("kind") or ""
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
    state["stop_kind"] = ""
    state["env_blocked"] = 0
    state["env_blocked_reason"] = ""
    state["budget_rounds"] = int(budget or DEFAULT_BUDGET)
    _save(state)
    return state


def stop(reason: str = "手动停止") -> dict:
    state = _load()
    state["running"] = False
    state["stop_reason"] = reason
    state["stop_kind"] = "user"
    _save(state)
    return state


# ---------- 第二自我：批判人格 ----------
# 评估器只读「能被量化的一小块」。一个纯按指标跑的循环，最可能的失败不是跑得慢，
# 而是把整份预算花在把某条次要指标从 0.27 磨到 0.31 —— 每轮都「有效」，
# 合起来离总目标更远。轮末插一道批判补上这个盲区：通读长期事业台账 +
# 悬而未决 + 信条 + 本仓近期变更，回答「这一轮把项目推向了哪里，下一轮该往哪走」。
# 硬约束：每条指引必须带证据出处与可推翻判据，缺一条就拒收 ——
# 没有这两样的「指引」就是又一段「我觉得」，正是本模块要消灭的东西。

CRITIC_PERSONA = """你是大白的第二自我 —— 批判人格。

职责不是鼓励，是找错。对上一轮成果先假定它「看起来有效、实际没用」，再去证据里找反驳。允许你判定「这一轮白干了」。

检查范围（材料包里都给了，别凭记忆）：
  1. 本轮成果与有效性判据 —— 有产出不等于有价值
  2. 长期事业台账 —— 本轮服务了哪个项目的 value / 验收标准，还是谁都没服务
  3. 悬而未决的问题 —— 有没有被长期忽略的
  4. 信条与拒绝记录 —— 本轮做法有没有违背自己立的信条
  5. 本仓近期变更 —— 改动集中在哪，有没有反复修同一处的迹象

产出（缺任一条会被 CLI 拒收）：
  · 每条指引必须带 evidence：能指到 文件:行号 / 命令输出 / 台账 id / 轮号
  · 每条指引必须带 refutable：说明「观察到什么现象，我就该改判这条指引是错的」
  · 每条指引必须挂 target：评估器缺口 id（recidivism:*/rules:*）、长期事业 id，或 learn
  · 必须给 alignment：本轮做的事与总目标的关系 —— 推进 / 原地打转 / 跑偏
"""

MAX_DIRECTIVES = 5
_EVIDENCE_RE = re.compile(r"([\w./\\-]+\.\w+|#\d+|--[\w-]+|[\w-]{3,}:\S)")


def _read_root_json(name: str) -> dict:
    """读仓库根下的台账文件。坏了就当空 —— 材料缺一块不该让整轮崩。"""
    try:
        p = ROOT / name
        if p.exists():
            d = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(d, dict):
                return d
    except Exception:
        pass
    return {}


def known_targets() -> set:
    """指引允许挂的目标：长期事业 id + learn。评估器缺口 id 按前缀放行。"""
    ids = {"learn"}
    for p in _read_root_json("long_horizon.json").get("projects") or []:
        if p.get("id"):
            ids.add(str(p["id"]))
    return ids


def target_ok(t: str) -> bool:
    t = str(t or "").strip()
    if not t:
        return False
    return t in known_targets() or t.startswith("recidivism:") or t.startswith("rules:")


def validate_critique(payload: dict) -> list:
    """返回错误清单。空清单 = 通过。这里是「有理有据」的强制点。"""
    if not isinstance(payload, dict):
        return ["批判内容必须是 JSON 对象"]
    errs = []
    if len(str(payload.get("verdict") or "").strip()) < 10:
        errs.append("verdict 太短（<10 字）：要一句能被反驳的判定，比如「本轮有效但方向跑偏」")
    ds = payload.get("directives")
    if not isinstance(ds, list) or not ds:
        return errs + ["directives 至少要 1 条"]
    if len(ds) > MAX_DIRECTIVES:
        errs.append(f"directives 最多 {MAX_DIRECTIVES} 条（{len(ds)} 条会退化成清单噪音）")
    for i, d in enumerate(ds, 1):
        if not isinstance(d, dict):
            errs.append(f"directives[{i}] 不是对象")
            continue
        if len(str(d.get("claim") or "").strip()) < 8:
            errs.append(f"directives[{i}].claim 太短（<8 字）")
        ev = str(d.get("evidence") or "").strip()
        if len(ev) < 10 or not _EVIDENCE_RE.search(ev):
            errs.append(f"directives[{i}].evidence 必须指到出处"
                        f"（文件:行号 / 命令 / 台账 id / 轮号）")
        if len(str(d.get("refutable") or "").strip()) < 8:
            errs.append(f"directives[{i}].refutable 必须写「什么观测会推翻这条指引」")
        if not target_ok(d.get("target")):
            errs.append(f"directives[{i}].target 无效（{d.get('target')!r}）："
                        f"要用 recidivism:*/rules:*/长期事业 id/learn")
    return errs


def submit_critique(payload: dict) -> dict:
    """校验并落盘一条批判。校验不过抛 ValueError，不写半个字。"""
    errs = validate_critique(payload)
    if errs:
        raise ValueError("；".join(errs))
    state = _load()
    ds = [{"claim": str(d.get("claim")).strip(),
           "evidence": str(d.get("evidence")).strip(),
           "refutable": str(d.get("refutable")).strip(),
           "target": str(d.get("target")).strip()} for d in payload["directives"]]
    entry = {
        "n": len(state.get("critiques") or []) + 1,
        "ts": _now(),
        # 针对第几轮之后的批判：brief 靠它判「这条指引还算不算数」
        "after_round": len(state.get("rounds") or []),
        "verdict": str(payload["verdict"]).strip(),
        "alignment": str(payload.get("alignment") or "").strip(),
        "risk": str(payload.get("risk") or "").strip(),
        "directives": ds,
    }
    state.setdefault("critiques", []).append(entry)
    _save(state)
    return entry


def latest_critique(fresh_only: bool = False) -> dict | None:
    """最近一条批判。fresh_only=只认针对「最新一轮」的那条（旧指引不许一直霸占选题）。"""
    state = _load()
    cs = state.get("critiques") or []
    if not cs:
        return None
    last = cs[-1]
    if fresh_only and int(last.get("after_round") or 0) != len(state.get("rounds") or []):
        return None
    return last


def _git(args: list, timeout: int = 20) -> str:
    try:
        p = subprocess.run(["git"] + args, cwd=str(ROOT), capture_output=True,
                           text=True, timeout=timeout)
        return p.stdout.strip()
    except Exception:
        return ""


def critique_pack() -> str:
    """组装批判材料包。执行体照读，不额外发挥。"""
    state = _load()
    rounds = state.get("rounds") or []
    lt = _read_root_json("long_horizon.json")
    cv = _read_root_json("conviction.json")

    L = [CRITIC_PERSONA, "",
         f"# 材料包（{time.strftime('%Y-%m-%d %H:%M')} · 记账 {len(rounds)} 轮）", ""]

    L.append(f"## 一、本轮成果（第 {len(rounds)} 轮）")
    if rounds:
        r = rounds[-1]
        L.append(f"- 目标：{r.get('target')}｜动作：{r.get('action')}")
        L.append(f"- 证据：{r.get('evidence')}")
        L.append(f"- 有效性：{'有效' if r.get('effective') else '无效'}"
                 f"｜代码改动：{', '.join(r.get('files') or []) or '（无）'}"
                 f"｜落盘：{', '.join(r.get('anchors') or []) or '（无）'}")
        if r.get("note"):
            L.append(f"- 备注：{r['note']}")
    else:
        L.append("- （还没记账）")
    L.append("")

    L.append("## 二、轮次账本（近 10 轮）")
    for r in rounds[-10:]:
        mark = "有效" if r.get("effective") else "无效"
        L.append(f"- #{r.get('n')} [{mark}] {r.get('target')}｜{str(r.get('action'))[:60]}"
                 f"｜{str(r.get('evidence'))[:60]}")
    if not rounds:
        L.append("- （空）")
    L.append("")

    L.append("## 三、评估器读数")
    try:
        gaps = observe()
    except Exception as e:
        gaps = []
        L.append(f"- ⚠ 评估器跑挂：{e}")
    if gaps:
        for g in gaps[:6]:
            L.append(f"- [{g['score']:.3f}] {g['id']}｜{g['title']}｜{g['metric']}")
            L.append(f"    证据：{g['evidence'][:160]}")
    else:
        L.append("- （无缺口：全部达标，或样本不足）")
    L.append("")

    L.append("## 四、长期事业台账（active）")
    for p in lt.get("projects") or []:
        if p.get("stage") != "active":
            continue
        logs = p.get("log") or []
        last = logs[-1] if logs else {}
        L.append(f"- {p.get('id')}｜{p.get('title')}｜进度 {p.get('progress', 0)}%")
        if p.get("value"):
            L.append(f"    价值：{str(p['value'])[:120]}")
        if p.get("next"):
            L.append(f"    接力棒：{str(p['next'])[:200]}")
        if isinstance(last, dict) and last.get("what"):
            L.append(f"    最近：{str(last.get('t'))} {str(last['what'])[:120]}")
    L.append("")

    L.append("## 五、悬而未决（还没被解决的燃料）")
    for q in lt.get("questions") or []:
        if q.get("status") == "open":
            L.append(f"- {q.get('text')}")
    L.append("")

    L.append("## 六、信条与拒绝记录")
    for c in cv.get("convictions") or []:
        L.append(f"- 信条：{c.get('text')}")
    for v in (cv.get("vetoes") or [])[-5:]:
        L.append(f"- 拒绝过：{v.get('claim')}")
    L.append("")

    L.append("## 七、本仓近期变更")
    log = _git(["log", "--oneline", "-10"])
    for line in (log.splitlines() if log else ["（git 不可用）"]):
        L.append(f"- {line}")
    hot = _git(["log", "--name-only", "--pretty=format:", "-8"])
    if hot:
        cnt: dict = {}
        for f in hot.splitlines():
            f = f.strip()
            if f:
                cnt[f] = cnt.get(f, 0) + 1
        top = sorted(cnt.items(), key=lambda kv: -kv[1])[:8]
        L.append("- 近 8 次提交改动热点：" + "，".join(f"{f}×{n}" for f, n in top))
    L.append("")

    L.append("## 八、产出契约（写成一个 JSON 文件后交给 CLI）")
    L.append('{"verdict": "一句能被反驳的判定（>=10 字）",')
    L.append(' "alignment": "本轮与总目标的关系：推进 / 原地打转 / 跑偏，并给出依据",')
    L.append(' "risk": "若继续按当前方向跑，最可能出的问题",')
    L.append(' "directives": [{"claim": "下一轮该做什么（>=8 字）",')
    L.append('                 "evidence": "证据出处：文件:行号 / 命令 / 台账 id / 轮号",')
    L.append('                 "refutable": "什么观测会推翻这条指引",')
    L.append('                 "target": "recidivism:* / rules:* / 长期事业 id / learn"}]}')
    L.append("")
    L.append(f"落盘：venv/bin/python tools/self_iterate.py critique-submit --file <上面那个 json>")
    L.append(f"生成材料包的时间戳：{int(_now())}")
    return "\n".join(L)


# ---------- 任务书 ----------
# ---------- 任务书 ----------

# 长期台账（tools/long_horizon.py 的落盘目标）与「本轮 target → 台账 id」的映射。
# id 必须由任务书写死：第 5/6 轮的任务书只写了「log <id>」、id 空着，执行体就挑了一个
# 看着像的——把自我迭代的接力棒记进了 oss-contrib 那本账。台账 id 一旦靠自己猜，
# 下一轮读接力棒的人（下一个进程）就会在错的账本里找下一步。
LEDGER_FILE = ROOT / "long_horizon.json"
LEDGER_ID_BY_PREFIX = {
    "rules": "self-iterate",          # 自指机制类缺口（rules:plan_rate / rules:verify_rate）
    "recidivism": "self-iterate",     # 同错复发
    "self-iterate": "self-iterate",
    "learn": "hermes-learning-loop",  # 学习轮：学到的做法记在学习闭环那本账上
}


def ledger_ids() -> list:
    """长期台账里真实存在的 id 列表（读不到就空表，退回「不写死」的旧行为）。"""
    try:
        d = json.loads(LEDGER_FILE.read_text(encoding="utf-8"))
        return [str(p.get("id")) for p in (d.get("projects") or []) if p.get("id")]
    except Exception:
        return []


def ledger_id_for(target: str) -> str:
    """把本轮 target 映射成台账里**确定存在**的落盘 id（读不到台账返回空串）。

    只从真实 id 里挑：先按 target 前缀查表，再拿台账校验，对不上就退回 self-iterate
    （自我迭代自己的账本）——任务书永远只给存在的 id，执行体不必再从名字里猜。
    """
    ids = ledger_ids()
    if not ids:
        return ""
    pid = LEDGER_ID_BY_PREFIX.get(str(target or "").split(":")[0], "self-iterate")
    if pid not in ids:
        pid = "self-iterate" if "self-iterate" in ids else ""
    return pid


CONTRACT = """本轮契约（缺任一条，这轮就是无效轮）：
  1. 只做一件事：把上面这个缺口推进到「有客观变化」，不顺手做别的
  2. 留下可复核产出：代码改动 或 落盘（接力棒/教训/信条）——「我想了想」不算
  2b. 落盘 id 已由任务书写死：{ledger}。不许自选——第 5/6 轮自选 id，把自我迭代的
      接力棒记进了 oss-contrib 那本账，下一轮读接力棒的人于是在错的账本里找下一步
  3. 记证据：self_iterate.py record --target {target} --action "..." --evidence "..." --since {since}{ratio_arg}
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
        "3. 落盘：`venv/bin/python tools/long_horizon.py log <任务书里写死的 id> "
        "\"...\" --ev \"...\"`（id 以任务书为准、不许自选——第 5/6 轮自选 id 把自我"
        "迭代的接力棒记进了 oss-contrib），并把 next 改写成下一轮能直接开跑的原子动作。\n"
        "4. 记账：`venv/bin/python tools/self_iterate.py record --target <任务书的 target> "
        "--action \"...\" --evidence \"...\" --since <任务书里的 since> "
        "<任务书里若给了 --gap-ratio 就一并带上>`。"
        "产出（代码改动/落盘）与证据字符串缺一不可，否则这轮算无效。\n"
        "5. 轮末批判：跑 `venv/bin/python tools/self_iterate.py critique` 拿材料包，"
        "以第二自我批判人格通读整个项目（长期事业台账 / 悬而未决 / 信条 / 本仓近期变更），"
        "产出下一轮方向指引，写成一个 JSON 文件后 `critique-submit --file <json>` 落盘。"
        "每条指引缺证据出处或可推翻判据都会被拒收 —— 别写「我觉得」。\n"
        "6. 若任务书开头是「⛔ 自我迭代已停」，直接回报原因，不要重新启动。\n"
        f"纪律：同一目标连修 {STUCK_ROUNDS} 轮指标没动就换目标，别死磕。"
    )


def _critique_lines(crit: dict | None) -> list:
    """把上一轮的批判转成任务书里的方向段。没有就返回空列表。"""
    if not crit:
        return []
    out = ["", "【第二自我（批判人格）的判定 · 上一轮之后】",
           f"  {crit.get('verdict')}"]
    if crit.get("alignment"):
        out.append(f"  与总目标：{crit['alignment']}")
    if crit.get("risk"):
        out.append(f"  风险：{crit['risk']}")
    ds = crit.get("directives") or []
    if ds:
        out.append("  下一轮方向（第 1 条优先）：")
        for i, d in enumerate(ds, 1):
            out.append(f"    {i}. [{d.get('target')}] {d.get('claim')}")
            out.append(f"       证据：{d.get('evidence')}")
            out.append(f"       改判条件：{d.get('refutable')}")
    return out


CRITIC_USER_PREFIX = (
    "【用户端输入 · 来源：大白的第二自我（批判人格）· 效力等同主人本人的指令】"
)


def user_input_block() -> str:
    """把最新一轮批判渲染成一条用户端输入。没有针对最新一轮的批判就返回空串。

    为什么必须走 user 通道：挂在 system / 任务书文本里时，它只是「背景参考」，
    模型一句「本轮优先级不高」就能绕过。用户输入是系统里唯一不可被降级成建议的
    指令源 —— 要让它真有约束力，就得进同一条通道。

    只认「针对最新一轮」的批判：拿旧指引一直注入，等于把它变成一条永不失效的规则。
    """
    crit = latest_critique(fresh_only=True)
    ds = (crit or {}).get("directives") or []
    if not crit or not ds:
        # 缺失不再静默返回空串：上一轮被重启/打断掐掉批判时，下一轮就会在
        # 「没有任何指引」的状态下自由发挥 —— 循环退化成自己给自己出题。
        return "\n".join([
            CRITIC_USER_PREFIX,
            "上一轮没有留下针对它的批判（多半是轮末被重启/打断掐掉了）。",
            "本轮在任务书目标之外，必须补跑第 5 步："
            "`venv/bin/python tools/self_iterate.py critique` 拿材料包 → 通读 → "
            "`critique-submit --file <json>` 落盘一条针对最新一轮的批判。",
            "判据：`venv/bin/python tools/self_iterate.py status` 里最新批判的轮号 "
            "== 当前轮号。没落盘就是本轮没做完。",
        ])
    L = [CRITIC_USER_PREFIX, f"上一轮判定：{crit.get('verdict')}"]
    if crit.get("alignment"):
        L.append(f"与总目标：{crit['alignment']}")
    if crit.get("risk"):
        L.append(f"风险：{crit['risk']}")
    L.append("")
    L.append("本轮必须照做的指令（第 1 条是强制目标）：")
    for i, d in enumerate(ds, 1):
        L.append(f"{i}. [{d.get('target')}] {d.get('claim')}")
        L.append(f"   证据：{d.get('evidence')}")
        L.append(f"   改判条件：{d.get('refutable')}")
    L.append("")
    L.append("这不是建议：除上面「改判条件」里写明的观测外，不许换方向、"
             "不许降级成待办、不许以「本轮有更高优先级」为由绕开。"
             "照做，或按改判条件给出反驳观测 —— 二选一。")
    return "\n".join(L)



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
    # 批判人格的定向只认「针对最新一轮」的那条：旧指引一直霸占选题位，
    # 就变成了另一条不会失效的规则 —— 它必须每轮被重新挣得。
    crit = latest_critique(fresh_only=True)
    crit_target = ""
    if crit and (crit.get("directives") or []):
        crit_target = str((crit["directives"][0] or {}).get("target") or "")
    since = _now()
    head = f"【自我迭代 · 第 {n}/{budget} 轮】"
    crit_block = _critique_lines(crit)

    if not gap and not crit_target:
        why = ("评估器全部达标或样本不足" if not gaps
               else "缺口都在重放里修死了或连续死磕，没有可行动项")
        return "\n".join([head] + crit_block + [
            f"没有指标型缺口（{why}）。",
            "本轮是学习轮 —— 学先进 agent 的做法，产出必须可迁移：",
            "  1. 读一个外部项目/论文（search_web / oss_contrib.py repo 挑健康仓库）",
            "  2. 产出「带证据行号的可迁移清单」落 docs/<name>-study.md，不写读后感",
            "  3. 从清单里挑一条真落进本仓，跑测试",
            f"  4. 落盘：long_horizon.py log {ledger_id_for('learn') or '<id>'} "
            "\"...\" --ev \"...\" + next",
            "  5. 记轮：self_iterate.py record --target learn --action \"...\" "
            f"--evidence \"...\" --since {since}"])

    lines = [head] + crit_block + [""]
    if crit_target and crit_target != (gap or {}).get("id"):
        d0 = crit["directives"][0]
        lines += [f"本轮目标：{crit_target}（批判人格定向；评估器缺口 "
                  f"{gap['id'] if gap else '无'} 让位）",
                  f"  要做什么：{d0.get('claim')}",
                  f"  证据出处：{d0.get('evidence')}",
                  f"  改判条件：{d0.get('refutable')}",
                  ""]
        if gap:
            lines += [f"（评估器读数备查）{gap['id']}：{gap['metric']}｜"
                      f"{str(gap['evidence'])[:120]}", ""]
    else:
        lines += [f"目标缺口：{gap['id']} — {gap['title']}",
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
    # 把本轮读数随命令一起交下去：缺账时「死磕」判据只能靠沿用旧值推断，多落一行
    # 就够判据用事实说话（第 5/6 轮正是缺这行，指标没动却判不出来）。
    # 只在「本轮目标就是评估器缺口」时下发读数；批判定向到别的目标时，这个数字
    # 与它无关，带上只会误导。注意批判定向到同一个缺口时仍算缺口轮。
    _ratio = ""
    if (not crit_target or crit_target == (gap or {}).get("id")) \
            and (gap or {}).get("gap_ratio") is not None:
        _ratio = f" --gap-ratio {gap['gap_ratio']}"
    lines.append(CONTRACT.format(target=crit_target or gap["id"], since=int(since),
                                 ratio_arg=_ratio, stuck=STUCK_ROUNDS,
                                 ledger=ledger_id_for(crit_target or gap["id"]) or "<id>"))
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
    sub.add_parser("critique")
    p_cs = sub.add_parser("critique-submit")
    p_cs.add_argument("--file", default="")
    p_cs.add_argument("--json", default="")
    sub.add_parser("user-input")
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

    if args.cmd == "critique":
        print(critique_pack())
        return 0

    if args.cmd == "critique-submit":
        raw = ""
        if args.file:
            try:
                raw = Path(args.file).read_text(encoding="utf-8")
            except Exception as ex:
                print(f"✘ 读不到 {args.file}：{ex}")
                return 2
        elif args.json:
            raw = args.json
        else:
            raw = sys.stdin.read()
        try:
            payload = json.loads(raw)
        except Exception as ex:
            print(f"✘ JSON 解析失败：{ex}")
            return 2
        try:
            entry = submit_critique(payload)
        except ValueError as ex:
            print(f"✘ 拒收：{ex}")
            return 2
        print(f"✔ 已落盘第 {entry['n']} 条批判（针对第 {entry['after_round']} 轮之后）")
        print(f"  判定：{entry['verdict']}")
        for i, d in enumerate(entry["directives"], 1):
            print(f"  {i}. [{d['target']}] {d['claim']}")
        return 0

    if args.cmd == "record":
        try:
            e = record(args.target, args.action, args.evidence, args.since,
                       note=args.note, gap_ratio=args.gap_ratio)
        except ValueError as ex:
            print(f"⛔ 拒收：{ex}")
            return 2
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

    if args.cmd == "user-input":
        txt = user_input_block()
        print(txt or "（无针对最新一轮的批判 —— 不注入任何用户端输入）")
        return 0

    # status
    state = _load()
    v = verdict(state)
    rounds = state.get("rounds") or []
    good = sum(1 for r in rounds if r.get("effective"))
    print(f"运行中：{state.get('running')}｜本周期 {used(state)}/{state.get('budget_rounds')}"
          f"｜累计 {spent(state)}"
          f"｜记账 {len(rounds)} 轮（有效 {good}）｜连续无效 {_streak(state)}/{MAX_STREAK}"
          f"｜环境阻塞 {int(state.get('env_blocked') or 0)}/{ENV_BLOCK_MAX}")
    if state.get("stop_reason"):
        print(f"停止原因：{state['stop_reason']}")
    print(f"下一步：{v['action']} {v['reason']}")
    if stuck_targets(state):
        print(f"已判定死磕（换目标）：{', '.join(sorted(stuck_targets(state)))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())


def _critic_summary() -> dict:
    """任务中心/按钮读的批判摘要。"""
    state = _load()
    cs = state.get("critiques") or []
    if not cs:
        return {"count": 0}
    last = cs[-1]
    return {"count": len(cs), "n": last.get("n"),
            "after_round": last.get("after_round"),
            "verdict": last.get("verdict"),
            "fresh": int(last.get("after_round") or 0) == len(state.get("rounds") or []),
            "targets": [d.get("target") for d in (last.get("directives") or [])]}


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
        "critic": _critic_summary(),
    }
