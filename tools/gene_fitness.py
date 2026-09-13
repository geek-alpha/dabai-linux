#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""基因 fitness 台账：把「教训 / 信条 / 接力棒」当候选基因，量它们有没有真的被用上。

背景（2026-09-14）：AlphaEvolve 这类自我迭代框架的骨架只有三件——候选池、客观评估器、
选择压力。候选池早就堆满了（60 条教训 / 信条 / 规则），选择压力却是「新近度」：
tools/lesson_add.py:36 按 FIFO 截断 60 条，agent.py:1860 只注入前 6 条 —— 最新的 6 条
永远在场，其余 54 条从未进过 prompt；tools/conviction.py:79 有 cmd_hit 命中计数，
但全项目零调用点，hits 恒为 0（埋点造好没通电）。
选择压力也已接上（2026-09-14 晚）：agent.py 的 _gene_pick 保留前 2 条新近位，其余
按曝光升序补足——最少曝光优先等于自动轮换，窗口外的 54 条逐轮进窗口被验证。
本脚本是评估器的读出端：算出每条基因的真实曝光窗口与命中次数，输出「零命中清单」。

用法：
  gene_fitness.py                          报表
  gene_fitness.py touch lesson "文本"       埋点：该基因被注入时 +1（写 gene_stats.json）
"""
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
STATS = BASE / "gene_stats.json"
AGENT = BASE / "agent.py"
# 曝光流水：一行一轮，记「本轮哪些基因进了 prompt」。累计次数反推不出集合，两者都要。
EXPOSURE = BASE / "data" / "gene_exposure.jsonl"
# 同轮的效率指标落盘：对账用——流水行的 metrics 必须与它逐字段同值，否则埋点没接线
TURN_METRICS = BASE / "data" / "turn_metrics.jsonl"
# 子智能体终态审计：一行一个状态事件（queued/running/done/error/cancelled），updated_at 是毫秒。
# 任务结局是这套闭环里唯一不依赖 provider 口径的因变量——token 与耗时是过程量，成败才是结局。
SUBAGENT_HIST = BASE / "data" / "sub_agents.jsonl"
SUB_TERMINAL = ("done", "error", "cancelled")

# 注入窗口的 cap 从 agent.py 源码解析，不复制常量——双份常量必然漂移
CAP_PAT = r"def {}\(cap: int = (\d+)"
# 新近位数量同样从 agent.py 解析（_gene_pick 的 fresh 默认值），不复制常量
FRESH_PAT = r"def _gene_pick\(items: list, cap: int, fresh: int = (\d+)"
SOURCES = (
    ("lesson", "_harness_lessons_block", BASE / "harness_task_memory.json"),
    ("conviction", "_harness_conviction_block", BASE / "conviction.json"),
    ("longterm", "_harness_longterm_block", BASE / "long_horizon.json"),
)


def key_of(text):
    return hashlib.sha1(str(text).encode("utf-8")).hexdigest()[:12]


def caps():
    try:
        src = AGENT.read_text(encoding="utf-8")
    except Exception:
        return {}
    out = {}
    for kind, fn, _ in SOURCES:
        m = re.search(CAP_PAT.format(fn), src)
        out[kind] = int(m.group(1)) if m else 0
    return out


def fresh_default():
    """agent.py:_gene_pick 保留的新近位数；解析不到按 0（全部按曝光升序排）。"""
    try:
        m = re.search(FRESH_PAT, AGENT.read_text(encoding="utf-8"))
        return int(m.group(1)) if m else 0
    except Exception:
        return 0


def load_stats():
    try:
        d = json.loads(STATS.read_text(encoding="utf-8"))
    except Exception:
        return {}
    g = d.get("genes") if isinstance(d, dict) else None
    return g if isinstance(g, dict) else {}


def save_stats(g):
    tmp = str(STATS) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"genes": g}, f, ensure_ascii=False, indent=1)
    os.replace(tmp, STATS)


def read_genes():
    """按「会不会进 prompt」的顺序读三类基因。order 即注入顺序（越靠前越可能进窗口）。"""
    rows = []
    f = BASE / "harness_task_memory.json"
    if f.exists():
        try:
            ls = json.loads(f.read_text(encoding="utf-8")).get("lessons") or []
        except Exception:
            ls = []
        for i, t in enumerate(ls):
            rows.append({"kind": "lesson", "order": i, "text": str(t)})

    f = BASE / "conviction.json"
    if f.exists():
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            d = {}
        for i, c in enumerate(d.get("convictions") or []):
            rows.append({"kind": "conviction", "order": i, "text": str(c.get("text", "")),
                         "hits": c.get("hits", 0)})

    f = BASE / "long_horizon.json"
    if f.exists():
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            d = {}
        for i, p in enumerate([x for x in (d.get("projects") or []) if x.get("stage") == "active"]):
            rows.append({"kind": "longterm", "order": i, "text": str(p.get("title") or p.get("id"))})
    return rows


def build():
    cp = caps()
    fresh = fresh_default()
    stats = load_stats()
    rows = read_genes()
    for r in rows:
        r["key"] = key_of(r["text"])
        r["cap"] = cp.get(r["kind"], 0)
        s = stats.get(r["key"]) or {}
        r["inject"] = s.get("inject", 0)
        r["last"] = s.get("last", "")
        r["in_window"] = False
    mark_window(rows, cp, fresh)
    return cp, stats, rows


def mark_window(rows, cp, fresh):
    """标出「在场」：优先用最近一轮曝光流水，没有流水才用当前 stats 复现。

    为什么不能一律复现（实测 2026-09-14）：注入端按 +1 之前的计数选取，报表读的是
    +1 之后的计数——被选中的基因曝光一涨就可能排到后面，复现出来的窗口与当时实际
    注入的那 6 条必然不等（11 条里对不上）。流水是真实记录，复现只是近似。
    """
    exp = read_exposure(limit=1)
    if exp:
        keys = {str(k) for k in (exp[-1].get("keys") or [])}
        for r in rows:
            r["in_window"] = f"{r['kind']}:{r['key']}" in keys
            r["win_src"] = "最近一轮流水"
        return "最近一轮流水"
    for kind, _fn, _path in SOURCES:
        sub = [r for r in rows if r["kind"] == kind]
        win = (sub[:fresh] + sorted(sub[fresh:], key=lambda r: r["inject"]))[:cp.get(kind, 0)]
        for r in win:
            r["in_window"] = True
        for r in sub:
            r["win_src"] = "按当前曝光复现"
    return "按当前曝光复现"


def report():
    cp, stats, rows = build()
    print("基因 fitness 台账（评估器读出端）")
    print(f"埋点文件 gene_stats.json：{'已存在' if STATS.exists() else '不存在（未埋点，命中数只能看 conviction.json 自带字段）'}")
    print("")
    for kind, fn, path in SOURCES:
        sub = [r for r in rows if r["kind"] == kind]
        cap = cp.get(kind, 0)
        win = [r for r in sub if r["in_window"]]
        # 判据是「曝光为 0」而不是「不在窗口内」：后者恒等于 len(sub)-cap，不随轮换变化
        zero_exp = [r for r in sub if not (r["inject"] or r.get("hits", 0))]
        print(f"[{kind}] 库里 {len(sub)} 条 · 注入窗口 {cap} 条 · 实际在场 {len(win)} 条 "
              f"· 曝光为 0 {len(zero_exp)} 条")
        for r in sub[:3]:
            # 优先用埋点计数：conviction.json 自带的 hits 字段全项目零调用点，恒为 0
            hit = r["inject"] or r.get("hits", 0)
            print(f"    {r['key']} 命中 {hit} · {'在场' if r['in_window'] else '窗口外'} · {r['text'][:56]}")
        if len(sub) > 3:
            print(f"    ...（其余 {len(sub) - 3} 条见窗口外清单）")
        print("")
    zero = [r for r in rows if not r["in_window"]]
    print(f"当前窗口外的基因（共 {len(zero)} 条；曝光为 0 的逐轮顶进窗口，不是被淘汰）：")
    for r in zero[:12]:
        print(f"    {r['kind']:10s} {r['key']} {r['text'][:60]}")
    if len(zero) > 12:
        print(f"    ...（还有 {len(zero) - 12} 条）")
    print("")
    exp = read_exposure()
    last = exp[-1] if exp else {}
    tagged = [r for r in exp if isinstance(r.get("metrics"), dict) and r["metrics"]]
    print(f"曝光流水 data/gene_exposure.jsonl：{len(exp)} 轮 · 带结局标签 {len(tagged)} 轮 · "
          f"最近一轮 {last.get('turn', '（无）')} 记了 {len(last.get('keys') or [])} 条基因")
    if not tagged:
        print("    结局列全空：现有流水都写在结局埋点接线之前（agent.py:_gene_flush_exposure 传 metrics 之后才带）——不是采集失败。")
    else:
        index, baseline = outcome_index(exp)
        print(f"    基线（{len(tagged)} 轮带结局的轮次中位）：工具轮数 {_fmt(baseline.get('tool_rounds'))}"
              f" · 报错 {_fmt(baseline.get('tool_errors'))} · 耗时 {_fmt(baseline.get('duration_ms'), 'ms')}")
        oc = [r["metrics"].get("task_outcomes") for r in tagged]
        oc = [o for o in oc if isinstance(o, dict) and o]
        if oc:
            tot = {}
            for o in oc:
                for k, v in o.items():
                    tot[k] = tot.get(k, 0) + int(v)
            print(f"    任务结局（{len(oc)}/{len(tagged)} 轮有终态任务）："
                  + " · ".join(f"{k} {tot[k]}" for k in sorted(tot)))
        else:
            # 空列有两种原因：本时段真没任务结束（正常），或埋点没接线（故障）。
            # 用同一个窗口去 sub_agents 审计里反查一遍——两种原因在报表上长得一模一样，
            # 不反查的话「没接线」会一直显示成「没任务」，假绿比报错更难发现。
            lo = float(tagged[-2].get("ts", 0)) if len(tagged) > 1 else 0.0
            hi = float(tagged[-1].get("ts", 0))
            miss = task_outcomes(lo, hi)
            if miss:
                print(f"    任务结局：末轮窗口内其实有终态任务 {miss}，流水却没记——埋点没接线"
                      "（或这行是旧代码写的，重启未生效）。")
            else:
                print("    任务结局：末轮窗口内也没有终态任务（不是采集失败）。")
        ranked = sorted(index.items(), key=lambda kv: (-kv[1]["rounds"], kv[0]))[:8]
        for k, slot in ranked:
            med = {mk: median([m.get(mk) for m in slot["metrics"] if _num(m.get(mk))])
                   for mk in ("tool_rounds", "tool_errors", "duration_ms")}
            print(f"    {slot['rounds']:>3} 轮在场 · {k:26s} 工具轮数 {_fmt(med['tool_rounds'])}"
                  f" · 报错 {_fmt(med['tool_errors'])} · 耗时 {_fmt(med['duration_ms'], 'ms')}")
        top = ranked[0][1]["rounds"] if ranked else 0
        print(f"    门槛（GENE_FITNESS.md 第 4 节）：在场 ≥20 轮 + 有对照 + 人工复核；"
              f"当前最多 {top} 轮在场——只观测，不淘汰。")
        fixed, thin, ok = identifiability(exp)
        if fixed:
            print(f"    可辨识性：{len(fixed)} 条恒在场——没有对照组，样本再多也估不出效应；"
                  "要估它必须让它有缺席轮次（注入端随机留空），这是实验设计，不是等样本：")
            for k, c, tot in fixed[:6]:
                print(f"        {c}/{tot} 在场 · {k}")
        if thin:
            print(f"    可辨识性：{len(thin)} 条有对照组但观测不足（最多 "
                  f"{max(c for _, c, _ in thin)} 轮在场）——只记录，不下结论。")
        if ok:
            print(f"    可辨识性：{len(ok)} 条已达样本门槛。")
        # 自动对账：流水行的 metrics 与同轮 turn_metrics 必须逐字段同值。不自动对账就得靠
        # 人工 tail 两处，而「忘了传实参」正是行为测试测不到、只能靠对账发现的失败。
        cur = last_turn_metrics()
        m = last.get("metrics") if isinstance(last.get("metrics"), dict) else {}
        if not m:
            print("    对账：最近一轮流水没有结局标签（写在接线前，或 flush 未带 metrics）——不能默认它在采。")
        elif not cur or abs(float(cur.get("ts", 0)) - float(last.get("ts", 0))) > 60:
            print("    对账：流水末行与 turn_metrics 末行不是同一轮（相隔 >60s），跳过。")
        else:
            bad = [f"{f} 流水 {m.get(f)} / turn_metrics {cur.get(f)}"
                   for f in ("tool_rounds", "tool_errors", "tool_calls", "duration_ms")
                   if f in m and f in cur and cur.get(f) != m.get(f)]
            print("    对账（末行流水 vs turn_metrics 末行）：" + ("逐字段一致" if not bad else "；".join(bad)))
    samples, drops = task_samples(exp)
    if samples or drops:
        done = sum(1 for s in samples if s["status"] == "done")
        print(f"任务级样本（单位＝任务，自变量＝提交轮在场基因）：可归属 {len(samples)} 个"
              f"（done {done} · 其他 {len(samples) - done}）")
        if drops:
            print("    归不上：" + " · ".join(f"{k} {v}" for k, v in sorted(drops.items()))
                  + "——流水断档时硬归会接错轮次，宁可不算。")
        idx, base = task_effect(samples)
        if base:
            tot = sum(base.values())
            print("    基线（同批任务）：" + " · ".join(f"{k} {v}/{tot}" for k, v in sorted(base.items())))
        for k, slot in sorted(idx.items(), key=lambda kv: (-kv[1]["tasks"], kv[0]))[:6]:
            oc = " · ".join(f"{s} {c}" for s, c in sorted(slot["outcomes"].items()))
            print(f"    {slot['tasks']:>3} 个任务 · {k:26s} 结局 {oc}")
    else:
        print("任务级样本：审计里还没有终态任务——不是采集失败。")
    print("结论口径：窗口外的基因不是「没用」，是「没被验证过」——按新近度淘汰等于拿没测当没用。")
    src = (rows[0].get("win_src") if rows else "") or "按当前曝光复现"
    print(f"在场口径：{src}；轮换规则＝前 {fresh_default()} 条新近位 + "
          "曝光最少的补足（agent.py:_gene_pick）；每轮注入即 +1，未验证基因自动轮换。")
    return 0


def touch(kind, text):
    g = load_stats()
    k = key_of(text)
    e = g.get(k) or {"kind": kind, "preview": str(text)[:80], "inject": 0, "last": ""}
    e["inject"] = int(e.get("inject", 0)) + 1
    e["last"] = time.strftime("%Y-%m-%d %H:%M:%S")
    g[k] = e
    save_stats(g)
    print(f"{kind} {k} 注入 +1（累计 {e['inject']}）")
    return 0


def touch_batch(pairs, now=None):
    """进程内批量埋点：返回生效条数，不打印、不读 stdin（注入端直接调用）。

    条目接受 dict（{"kind","text"}）或二元组 (kind, text) —— agent.py 的注入端
    用元组形式，省掉每轮构造 60 个字典。无有效条目时不写盘。
    """
    items = []
    for p in pairs if isinstance(pairs, list) else []:
        if isinstance(p, dict):
            kind, text = str(p.get("kind", "")), str(p.get("text", ""))
        else:
            try:
                kind, text = str(p[0]), str(p[1])
            except Exception:
                continue
        if text:
            items.append((kind, text))
    if not items:
        return 0
    g = load_stats()
    now = now or time.strftime("%Y-%m-%d %H:%M:%S")
    for kind, text in items:
        k = key_of(text)
        e = g.get(k) or {"kind": kind, "preview": text[:80], "inject": 0, "last": ""}
        e["inject"] = int(e.get("inject", 0)) + 1
        e["last"] = now
        g[k] = e
    save_stats(g)
    return len(items)


def log_exposure(keys, turn="", path=None, metrics=None):
    """追加一行「本轮哪些基因进了 prompt」，返回写入行数（0 或 1）。

    与 gene_stats.json 的分工：那个存累计次数，够做轮换排序；这个存每轮的键集合，
    够把「基因在场的那一轮」和「那一轮的结局」对上。累计值反推不出集合，不能合并。
    重复键保序去重：同一轮三块注入可能撞同一条。异常吞掉——观测不许成为故障源。

    metrics 是本轮的结局标签（工具轮数/报错数/耗时）。只有 keys 时流水回答的是
    「谁在场」；带上 metrics 才能问「它在场的那一轮，结局是变好还是没变」。
    """
    ks = []
    for k in (keys if isinstance(keys, list) else []):
        s = str(k)
        if s and s not in ks:
            ks.append(s)
    if not ks:
        return 0
    p = Path(path) if path else EXPOSURE
    row = {"ts": time.time(), "turn": str(turn or ""), "keys": ks}
    if isinstance(metrics, dict) and metrics:
        row["metrics"] = metrics
    try:
        line = json.dumps(row, ensure_ascii=False)
    except Exception:
        # 指标里混进不可序列化对象时丢指标、保曝光：keys 是自变量，丢了这一轮
        # 就再也对不上任何结局；指标只是少一个观测点。
        row.pop("metrics", None)
        try:
            line = json.dumps(row, ensure_ascii=False)
        except Exception:
            return 0
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        return 1
    except Exception:
        return 0


def read_exposure(path=None, limit=0):
    """读曝光流水（limit>0 时取最近 limit 行）。坏行跳过，不抛异常。"""
    p = Path(path) if path else EXPOSURE
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
    except Exception:
        return []
    if limit and limit > 0:
        lines = lines[-limit:]
    out = []
    for ln in lines:
        try:
            d = json.loads(ln)
        except Exception:
            continue
        if isinstance(d, dict) and isinstance(d.get("keys"), list):
            out.append(d)
    return out


def median(vals):
    """中位数：偶数个取中间两个的均值。空集返回 None——不是 0，0 会被读成「很健康」。"""
    xs = sorted(float(v) for v in vals)
    if not xs:
        return None
    n = len(xs)
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _sec(v):
    """时间戳统一成秒：毫秒（~1.7e12）与秒（~1.7e9）的分界取 1e11。"""
    v = float(v)
    return v / 1000.0 if v > 1e11 else v


def _fmt(v, unit=""):
    return "—" if v is None else f"{v:g}{unit}"


def task_outcomes(start, end, path=None):
    """取 (start, end] 窗口内到达终态的子任务，按终态分桶（done/error/cancelled）。

    为什么归到「到达终态的那一轮」：提交轮只知道派了活，结局在终态那一刻才存在。
    代价是因果链被拉长一轮（提交轮注入的基因，结局记在完成轮），换来的是一个
    不依赖 provider 口径的真因变量。

    同一 id 可能有多行（queued→running→done），按 id 去重取最后一行；漏掉这步，
    一个任务会被数成多次，结局列就成了噪声。
    """
    p = Path(path) if path else SUBAGENT_HIST
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
    except Exception:
        return {}
    lo, hi = float(start), float(end)
    last = {}
    for i, ln in enumerate(lines):
        if not ln.strip():
            continue
        try:
            d = json.loads(ln)
        except Exception:
            continue
        if not isinstance(d, dict) or str(d.get("status") or "") not in SUB_TERMINAL:
            continue
        ts = d.get("updated_at")
        if not _num(ts):
            continue
        ts = _sec(ts)
        if not (lo < ts <= hi):
            continue
        last[str(d.get("id") or f"line{i}")] = str(d["status"])
    out = {}
    for st in last.values():
        out[st] = out.get(st, 0) + 1
    return out


def task_samples(exp, path=None, max_lag=1800.0):
    """按任务聚合样本：每个终态任务 → 提交时刻那一轮的在场基因。

    为什么样本单位是任务而不是轮：轮级标签的产出率实测只有 6%（终态任务稀疏地落
    在几十轮里），28 个槽位要几千轮才够——等于永远等不到。任务才是天然样本：一个
    任务一条，且自变量是它真正被派活那一刻的注入集合。

    为什么用提交轮而不是执行期并集：子任务提交后独立运行，主 agent 之后注入的基因
    管不到它。因果链只在提交那一刻成立，执行期并集是把不存在的效应算进去。

    归属规则：取第一行 ts >= created 的流水（轮末落盘，ts 必然晚于轮内提交）。滞后
    超过 max_lag 判归不上——流水断档时硬归会把它接到几小时后的另一轮，那条样本的
    自变量就完全是假的。归不上的原因要计数：产出率是能不能拟合的前提。

    返回 (samples, drops)：samples 每项 {"id","status","created","ts","turn","keys"}。
    """
    p = Path(path) if path else SUBAGENT_HIST
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
    except Exception:
        return [], {}
    tasks = {}
    for i, ln in enumerate(lines):
        if not ln.strip():
            continue
        try:
            d = json.loads(ln)
        except Exception:
            continue
        if not isinstance(d, dict):
            continue
        t = tasks.setdefault(str(d.get("id") or f"line{i}"), {})
        c = d.get("created_at")
        if _num(c):
            c = _sec(c)
            if "created" not in t or c < t["created"]:
                t["created"] = c
        if str(d.get("status") or "") in SUB_TERMINAL and _num(d.get("updated_at")):
            # 按行序覆盖：同一 id 的 queued→running→done 只有最后一次状态算数
            t["status"] = str(d["status"])
            t["end"] = _sec(d["updated_at"])
    rows = sorted((r for r in exp if _num(r.get("ts"))), key=lambda r: float(r["ts"]))
    samples, drops = [], {}
    for tid, t in tasks.items():
        if "status" not in t:
            continue
        if "created" not in t:
            drops["无提交时刻"] = drops.get("无提交时刻", 0) + 1
            continue
        row = next((r for r in rows if float(r["ts"]) >= t["created"]), None)
        if row is None:
            drops["流水还没写到那一轮"] = drops.get("流水还没写到那一轮", 0) + 1
            continue
        if float(row["ts"]) - t["created"] > max_lag:
            drops["流水断档"] = drops.get("流水断档", 0) + 1
            continue
        samples.append({"id": tid, "status": t["status"], "created": t["created"],
                        "ts": float(row["ts"]), "turn": row.get("turn", ""),
                        "keys": list(row.get("keys") or [])})
    samples.sort(key=lambda s: s["ts"])
    return samples, drops


def task_effect(samples):
    """把任务级样本折成「基因 → 它影响过的任务结局」，并给同期基线。

    基线必须来自同一批任务：定时探针和写代码的成败率差一个量级，跨批比等于拿任务
    难度冒充基因效应。只观测不判断——每条基因目前的样本量都远不到能下结论的程度。
    返回 (index, base)：index[key] = {"tasks": 在场任务数, "outcomes": {状态: 次数}}。
    """
    base, index = {}, {}
    for s in samples:
        st = str(s.get("status") or "?")
        base[st] = base.get(st, 0) + 1
        for k in s.get("keys") or []:
            slot = index.setdefault(str(k), {"tasks": 0, "outcomes": {}})
            slot["tasks"] += 1
            slot["outcomes"][st] = slot["outcomes"].get(st, 0) + 1
    return index, base


def outcome_index(exp, metric_keys=("tool_rounds", "tool_errors", "duration_ms")):
    """把曝光流水折成「基因键 → 它在场那些轮次的结局」，并给出同期基线。

    为什么必须带基线：结局中位数本身没有参照系——全店都在报错的那一轮，任何在
    场的基因看起来都糟。基线与它同源（同一批轮次），比跨期对比更可比。

    只观测不判断：单条基因的结局中位在样本到门槛前说明不了它有没有用，所以这里
    只产出数字，淘汰判据留在 report 的门槛提示与人工复核里。
    返回 (index, baseline)；index[key] = {"rounds": 在场轮数, "metrics": [结局 dict]}。
    """
    index, base = {}, {k: [] for k in metric_keys}
    for row in exp:
        m = row.get("metrics")
        m = m if isinstance(m, dict) else {}
        for k in (row.get("keys") or []):
            slot = index.setdefault(str(k), {"rounds": 0, "metrics": []})
            slot["rounds"] += 1
            if m:
                slot["metrics"].append(m)
        for mk in metric_keys:
            if _num(m.get(mk)):
                base[mk].append(m[mk])
    return index, {mk: median(vs) for mk, vs in base.items()}


def identifiability(exp, min_rounds=20):
    """按「能不能估出效应」给基因分类——这是拟合的前提，不是拟合本身。

    效应量要求自变量有变化：一条基因若每轮都在场，就永远没有对照组，样本量再大
    也估不出它的效应——不是「还没测到」，是「设计上测不到」。实测 2026-09-14：
    4 轮带标签的流水里，3 条 longterm + 2 条 conviction 100% 在场，23 条 lesson
    各只在场 1~2 轮。这个退化不修，攒到 1000 轮也拟合不出那 5 条。

    返回 (fixed, thin, ok)，三类互斥（恒在场只算 fixed，不能又算「已达门槛」——
    否则报表会把「永远估不出」谎报成「已达标」）；无带标签流水时全空。
    """
    tagged = [r for r in exp if isinstance(r.get("metrics"), dict) and r["metrics"]]
    n = len(tagged)
    if not n:
        return [], [], []
    seen = {}
    for row in tagged:
        for k in (row.get("keys") or []):
            seen[str(k)] = seen.get(str(k), 0) + 1
    fixed = sorted((k, c, n) for k, c in seen.items() if c == n)
    thin = sorted((k, c, n) for k, c in seen.items() if c != n and c < min_rounds)
    ok = sorted((k, c, n) for k, c in seen.items() if c != n and c >= min_rounds)
    return fixed, thin, ok


def last_turn_metrics(path=None):
    """读 turn_metrics.jsonl 的末行（坏行往前找）。对账用；文件缺失返回 {}。"""
    p = Path(path) if path else TURN_METRICS
    try:
        lines = [ln for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]
    except Exception:
        return {}
    for ln in reversed(lines):
        try:
            d = json.loads(ln)
        except Exception:
            continue
        if isinstance(d, dict):
            return d
    return {}


def order_by_exposure(items):
    """按曝光次数升序重排候选（同曝光保持传入顺序，即新近优先）。未埋点视为 0 次。

    只排序，不淘汰：曝光次数是自变量，不是价值——任何按曝光淘汰的规则都会把
    「没测过」当成「没用」。给注入端做最少曝光优先的轮换选取用。
    """
    stats = load_stats()
    texts = [str(x) for x in (items if isinstance(items, list) else [])]
    return sorted(texts, key=lambda t: int((stats.get(key_of(t)) or {}).get("inject", 0)))


def touch_many():
    """从 stdin 读 JSON 数组批量埋点：命令行手动埋点用。"""
    try:
        pairs = json.loads(sys.stdin.read() or "[]")
    except Exception:
        print('touch-many 需要 stdin 传 JSON 数组：[{"kind":"lesson","text":"..."}]')
        return 2
    print(f"批量埋点 {touch_batch(pairs)} 条")
    return 0


def main():
    if sys.argv[1:2] == ["touch-many"]:
        return touch_many()
    if len(sys.argv) >= 4 and sys.argv[1] == "touch":
        return touch(sys.argv[2], " ".join(sys.argv[3:]))
    if len(sys.argv) >= 3 and sys.argv[1] == "touch":
        print('用法：gene_fitness.py touch <lesson|conviction|longterm> "文本"')
        return 2
    return report()


if __name__ == "__main__":
    sys.exit(main())
