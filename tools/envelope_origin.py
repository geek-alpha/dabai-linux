#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""arguments 信封的**产生者定位 + 复发读数**（recidivism:F 的证据工具）。

背景（为什么要有这个工具）：
    data/longrun/traces 里 11/1027 次 ToolCallStart 的参数是单键
    `{"arguments": {...}}`（7 次内层还是 JSON 字符串）。tool_validation.py 的
    normalize_arguments 会把这种信封剥掉，于是「缺少必填参数: command」不再出现——
    但**剥掉的是症状读数，不是复发机制**：信封还在产生，只是错误字符串消失了。
    一旦信封不再报错，靠 ToolCallResult 错误串分类的 err_recidivism 就再也看不见它
    （测不到 = 以为好了）。这个工具补的就是这条盲区。

它回答两个问题，判据都能被推翻：
    1) 信封是谁产生的？——机器校验 agent.py 的三个 ToolCallStart 发射点：
       原生分支（yield 的是 tc["arguments"]，即模型流式原文，且 yield 发生在
       parse_partial_json 之前）、文本分支（yield 的是 json.dumps(已解析参数)）、
       断点续跑分支（str(dict)，单引号 repr）。再全局搜「有没有任何地方把参数
       包进 {"arguments": ...}」——搜到就判「是我们的解析层造的」（本工具会红）。
它回答两个问题，判据都能被推翻：
    1) 信封是谁产生的？——机器校验 agent.py 的三个 ToolCallStart 发射点：
       原生分支（yield 的是 tc["arguments"]，即模型流式原文，且 yield 发生在
       parse_partial_json 之前）、文本分支（yield 的是 json.dumps(已解析参数)）、
       断点续跑分支（str(dict)，单引号 repr）。再全局搜「有没有任何地方把参数
       包进 {"arguments": ...}」——搜到就判「是我们的解析层造的」（本工具会红）。
    2) 信封还在不在产生？——直接数 traces 里信封形状的调用条数（不依赖错误串），
       给基线读数；--fail-on-envelope 可当回归闸门。

第 14 轮实判（recidivism:F）：产生者 = **模型自己写的**，不是我们的解析路径。
    读数 11/1027 = 1.071%，涉及 cycle 10/19/22/28/33/34；六轮全部是原生（流式）轮
    （ReasoningDelta 只在流式循环发，六轮都有），此时 6550 记的就是 tc["arguments"]
    原文，且早于 6562 的 parse；源码里搜不到任何包封写法；parse_partial_json 对
    平铺入参不变、对信封保持原样。因此 tool_validation.normalize_arguments 只是把
    **症状**（报错）关掉，信封仍在产生：F 不是根修，本工具就是它的复发读数。
    改判条件：若某轮信封调用落在非流式（文本协议）轮、或源码里出现包封写法、
    或 parse 对平铺入参凭空补出 arguments 键——三条任一命中，判定即作废。

用法：
    venv/bin/python tools/envelope_origin.py                    # 人读报告
    venv/bin/python tools/envelope_origin.py --json             # 机读
    venv/bin/python tools/envelope_origin.py --fail-on-envelope # 有信封 → 退出码 2
    venv/bin/python tools/envelope_origin.py --save-baseline PATH
    venv/bin/python tools/envelope_origin.py --audit-examples   # 模型可见面的调用示例审计
"""
from __future__ import annotations

import argparse
import glob
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
TRACES = ROOT / "data" / "longrun" / "traces"

# F 类（必填参数缺失）实际打到的工具：工具名 → 技能归属（示例审计按这个清单找 schema）
F_TOOLS = ("shell_run", "code_read", "code_append")

# 反证模式：任何「把参数包进 arguments 键」的写法都算我们的解析层在造信封
_WRAP_PATTERNS = (
    r'arguments\s*=\s*\{\s*["\']arguments["\']',
    r'args\s*=\s*\{\s*["\']arguments["\']',
    r'\{\s*["\']arguments["\']\s*:\s*(arguments|args)\b',
)


# ---------- 1. 信封读数（不依赖错误串，所以归一之后照样看得见） ----------

def classify_args(raw: str) -> dict:
    """把一条 ToolCallStart 的参数原文分类。

    envelope_str  —— 顶层恰好 {"arguments": <JSON 字符串>}（双重编码）
    envelope_dict —— 顶层恰好 {"arguments": <dict>}
    flat          —— 顶层是参数本身（正常形态）
    not_json      —— 解析不了（截断/坏数据）
    多带一个键（{"arguments": ..., "timeout": ...}）**不算信封**：归一器的守卫
    也只认单键，这里必须同口径，否则读数会比修复动作更激进。
    """
    try:
        val = json.loads(raw) if raw else {}
    except Exception:
        return {"cls": "not_json", "keys": None, "inner": None}
    if not isinstance(val, dict):
        return {"cls": "not_json", "keys": None, "inner": None}
    keys = list(val.keys())
    if keys != ["arguments"]:
        return {"cls": "flat", "keys": keys, "inner": None}
    inner = val["arguments"]
    if isinstance(inner, str):
        try:
            json.loads(inner)
            return {"cls": "envelope_str", "keys": keys, "inner": "str(json)"}
        except Exception:
            return {"cls": "envelope_str", "keys": keys, "inner": "str(非JSON)"}
    return {"cls": "envelope_dict", "keys": keys, "inner": type(inner).__name__}


def _cycle_of(path: str) -> int:
    m = re.findall(r"(\d+)", Path(path).stem)
    return int(m[0]) if m else -1


def scan_traces(traces_dir: Path = TRACES) -> dict:
    """扫全部 trace：信封调用逐条列出 + 每轮的流式证据（判分支用）。"""
    calls: list = []
    total = 0
    per_cycle: dict = {}
    for p in sorted(glob.glob(str(Path(traces_dir) / "*.jsonl")), key=_cycle_of):
        cyc = _cycle_of(p)
        n_calls = 0
        n_reasoning = 0
        n_stream = 0
        for ln, line in enumerate(open(p, encoding="utf-8"), 1):
            try:
                ev = json.loads(line)
            except Exception:
                continue
            t = ev.get("type")
            if t == "ReasoningDelta":
                n_reasoning += 1
            elif t == "StreamDelta":
                n_stream += 1
            elif t == "ToolCallStart":
                total += 1
                n_calls += 1
                c = classify_args(ev.get("arguments", ""))
                if c["cls"].startswith("envelope"):
                    calls.append({"cycle": cyc, "line": ln, "tool": ev.get("tool_name"),
                                  **c})
        per_cycle[cyc] = {"calls": n_calls, "reasoning_deltas": n_reasoning,
                          "stream_deltas": n_stream}
    env_cycles = sorted({c["cycle"] for c in calls})
    # 分支判据：ReasoningDelta 只在流式循环里发出（agent.py:6223），非流式文本协议
    # 轮（agent.py:6083 stream=False）一条都不会有。信封轮有 reasoning 流 = 原生分支。
    mode = []
    for cyc in env_cycles:
        st = per_cycle.get(cyc, {})
        mode.append({"cycle": cyc, "reasoning_deltas": st.get("reasoning_deltas", 0),
                     "stream_deltas": st.get("stream_deltas", 0),
                     "branch": "native(流式)" if st.get("reasoning_deltas") else "非流式"})
    return {"total_calls": total, "envelope_calls": len(calls),
            "envelope_rate_pct": round(100 * len(calls) / max(total, 1), 3),
            "calls": calls, "per_cycle": per_cycle, "envelope_cycles": env_cycles,
            "branch_by_cycle": mode}


# ---------- 2. 产生者定位：机器校验源代码（能被合成源码推翻） ----------

def _emitter_rhs(lines: list, idx: int) -> tuple:
    """给 ToolCallStart 的 yield 行号，往上找最近的 tool_args_str 赋值，返回 (行号, 右值)。"""
    for j in range(idx - 1, max(-1, idx - 40), -1):
        m = re.match(r"\s*tool_args_str\s*=\s*(.+)$", lines[j])
        if m:
            return j + 1, m.group(1).strip()
    return None, ""


def emitter_checks(source: str) -> list:
    """对 agent.py 源码做三条可推翻的检查。返回 [{name, ok, evidence}]。"""
    lines = source.splitlines()
    out = []

    # ① 每个 ToolCallStart 发射点 yield 的是什么
    emitters = []
    for i, ln in enumerate(lines):
        if "yield ToolCallStart(" in ln:
            yl, rhs = _emitter_rhs(lines, i)
            emitters.append({"line": i + 1, "assign_line": yl, "rhs": rhs,
                             "kind": "RAW模型原文" if 'tc["arguments"]' in rhs
                             else ("SERIALIZED(已解析参数)" if "json.dumps(" in rhs
                                   else ("REPR(str(dict))" if rhs.startswith("str(")
                                         else "未知"))})
    raw_ok = any(e["kind"] == "RAW模型原文" for e in emitters)
    out.append({
        "name": "ToolCallStart 发射点已分类（至少一个 yield 模型原文）",
        "ok": bool(emitters) and raw_ok,
        "evidence": emitters,
    })

    # ② 原生分支：yield 必须发生在 parse_partial_json 之前（否则记的就是解析后的值）
    idx_raw = None
    for e in emitters:
        if e["kind"] == "RAW模型原文":
            idx_raw = e["line"]
            break
    idx_parse = next((i + 1 for i, ln in enumerate(lines)
                      if "arguments, args_status = parse_partial_json(" in ln), None)
    ok2 = bool(idx_raw and idx_parse and idx_raw < idx_parse)
    out.append({
        "name": "原生分支：ToolCallStart 先于 parse_partial_json（记的是模型流式原文）",
        "ok": ok2,
        "evidence": {"tool_call_start_line": idx_raw, "parse_line": idx_parse},
    })

    # ③ 反证：源码里有没有「把参数包进 arguments 键」的写法
    hits = []
    for i, ln in enumerate(lines):
        for pat in _WRAP_PATTERNS:
            if re.search(pat, ln):
                hits.append({"line": i + 1, "text": ln.strip()[:120]})
    out.append({
        "name": "无任何『把参数包进 arguments 键』的写法（有则信封由我们产生）",
        "ok": not hits,
        "evidence": hits,
    })

    # ④ 断点续跑分支用的是 str(dict) → 单引号 repr，与记录里的双引号信封形状不符
    repr_line = next((i + 1 for i, ln in enumerate(lines)
                      if 'str(p.get("arguments") or "")' in ln), None)
    out.append({
        "name": "续跑分支为 repr（单引号），可排除为双引号信封的来源",
        "ok": repr_line is not None,
        "evidence": {"line": repr_line},
    })
    return out


def parse_transparency() -> dict:
    """用仓库自己的 parse_partial_json 证明解析层不增不减键：平的仍是平的，信封仍是信封。"""
    from stream_partial_json import parse_partial_json
    flat = '{"command": "ls -la"}'
    env = '{"arguments": {"command": "ls -la"}}'
    f, sf = parse_partial_json(flat)
    e, se = parse_partial_json(env)
    return {
        "flat_in_keys": list(f.keys()) if isinstance(f, dict) else str(type(f)),
        "flat_out_keys": list(f.keys()) if isinstance(f, dict) else str(type(f)),
        "flat_unchanged": isinstance(f, dict) and list(f.keys()) == ["command"],
        "envelope_in_keys": list(e.keys()) if isinstance(e, dict) else str(type(e)),
        "envelope_kept": isinstance(e, dict) and list(e.keys()) == ["arguments"],
        "status": [sf, se],
    }


# ---------- 3. 示例审计（改判后的杠杆：补调用示例是否降低复发） ----------

def extract_examples(desc: str) -> list:
    """从描述里抠出形如 {...} 的调用示例（括号配对扫描，能看见嵌套的 arguments 信封）。

    为什么不用 re.finditer(r"\\{[^{}]*\\}")：那个模式看不见嵌套花括号，
    `{"arguments": {"command": "ls"}}` 只会被截成 `{"command": "ls"}` ——
    恰好把「教模型包信封」的示例洗成「平铺示例」，判据因此瞎掉（第 2/24 轮的真事故：
    shell_run 的首个照抄正例就是信封形状，而 16 条测试全绿）。
    反面示例（如 {"arguments": {"command": ...}}）不是合法 JSON，天然被跳过。
    """
    out = []
    i = 0
    while True:
        s = desc.find("{", i)
        if s < 0:
            break
        depth, j, instr, esc = 0, s, False, False
        while j < len(desc):
            c = desc[j]
            if instr:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    instr = False
            else:
                if c == '"':
                    instr = True
                elif c == "{":
                    depth += 1
                elif c == "}":
                    depth -= 1
                    if depth == 0:
                        break
            j += 1
        if j >= len(desc):
            break
        try:
            v = json.loads(desc[s:j + 1])
        except ValueError:
            v = None
        if isinstance(v, dict):
            out.append(v)
        i = j + 1
    return out


def check_example_flat(spec: dict, example: dict) -> tuple:
    """示例是否是可照抄的平铺调用。返回 (ok, reason)。

    判红只有三条口径，任一命中就说明示例在教模型包信封或缺必填：
    单键 arguments / 缺 required / 含 schema 未声明的键。

    spec 兼容两种形状：完整 tool 对象 {"type":..., "function":{...}} 或
    直接是 function 对象（skill.json 里两种写法都有）——否则 props 取空，
    所有键都会被误判「未声明」，读数变成假红。
    """
    if list(example.keys()) == ["arguments"]:
        return False, "示例本身是 arguments 信封——等于教模型包信封"
    inner = spec.get("function") or spec
    params = (inner.get("parameters") or {})
    props = params.get("properties") or {}
    required = params.get("required") or []
    missing = [k for k in required if k not in example]
    if missing:
        return False, f"示例缺必填 {missing}"
    unknown = [k for k in example if k not in props]
    if unknown:
        return False, f"示例含 schema 未声明的键 {unknown}"
    return True, ""


def has_example(text: str) -> bool:
    """schema 描述里有没有可照抄的调用示例（不是 schema 结构本身）。"""
    if not text:
        return False
    return bool(re.search(r"示例|例子|例如|例[:：]|e\.g\.|example|for example",
                          text, re.I))


def audit_examples() -> dict:
    """F 类打到的三个工具的模型可见面里，有没有**可照抄且平铺**的调用示例。

    只数「描述里出现过示例二字」是不够的：示例完全可能是包封写法
    （{"arguments": {...}}），那等于把 F 类复发的机制写进模型可见面当规范教。
    所以这里同时报 examples_ok 与 tools_with_envelope_example。
    """
    specs = {}
    for p in sorted(glob.glob(str(ROOT / "skills" / "*" / "skill.json"))):
        try:
            d = json.load(open(p, encoding="utf-8"))
        except Exception:
            continue
        for t in (d.get("tools") or []):
            fn = t.get("function", t)
            if fn.get("name") in F_TOOLS:
                desc = fn.get("description") or ""
                exs = extract_examples(desc)
                bad = []
                for ex in exs:
                    ok, why = check_example_flat(fn, ex)
                    if not ok:
                        bad.append({"example": ex, "reason": why})
                specs[fn["name"]] = {
                    "skill": Path(p).parent.name,
                    "has_example": has_example(json.dumps(fn, ensure_ascii=False)),
                    "example_count": len(exs),
                    "examples": exs,
                    "bad_examples": bad,
                    "examples_ok": bool(exs) and not bad,
                    "required": (fn.get("parameters") or {}).get("required"),
                    "desc_head": (fn.get("description") or "")[:60],
                }
    txt_proto = ""
    agent_src = (ROOT / "agent.py").read_text(encoding="utf-8", errors="replace")
    m = re.search(r"'<tool_call>\{\"name\":\"工具名\".*'\s*,", agent_src)
    if m:
        txt_proto = m.group(0)
    return {"tools": specs,
            "tools_missing_example": [k for k, v in specs.items() if not v["has_example"]],
            "tools_without_flat_example": [k for k, v in specs.items() if not v["examples_ok"]],
            "tools_with_envelope_example": [k for k, v in specs.items() if v["bad_examples"]],
            "text_protocol_example": txt_proto.strip()[:120]}


def power_n(p0: float, rel: float = 0.5, alpha: float = 0.05, power: float = 0.8) -> int:
    """两比例检验所需每组样本量（正态近似），用来判断「A/B 评估」到底做不做得起。"""
    z_a, z_b = 1.96, 0.8416
    p1 = max(p0 * (1 - rel), 1e-9)
    diff = p0 - p1
    if diff <= 0:
        return -1
    num = (z_a + z_b) ** 2 * (p0 * (1 - p0) + p1 * (1 - p1))
    return int(num / diff ** 2) + 1


# ---------- 报告 ----------

def build_report(traces_dir: Path = TRACES, agent_path: Path | None = None) -> dict:
    scan = scan_traces(traces_dir)
    src = (agent_path or (ROOT / "agent.py")).read_text(encoding="utf-8", errors="replace")
    checks = emitter_checks(src)
    trans = parse_transparency()
    examples = audit_examples()
    env_n, tot = scan["envelope_calls"], scan["total_calls"]
    native_cycles = [m for m in scan["branch_by_cycle"] if m["branch"].startswith("native")]
    # 判定：发射点已分类 + 无非解析层包封写法 + 解析层透明 + 信封轮全是流式（原生）轮
    all_native = len(native_cycles) == len(scan["branch_by_cycle"])
    producer_model = (all(c["ok"] for c in checks) and trans["flat_unchanged"]
                      and trans["envelope_kept"] and all_native and env_n > 0)
    if env_n == 0:
        producer = "no_envelope_observed"
    elif producer_model:
        producer = "model"
    else:
        producer = "unknown_or_parse_layer"
    return {
        "producer": producer,
        "producer_rule": "发射点 yield=模型流式原文(tc['arguments']) 早于 parse；"
                         "源码无包封写法；parse_partial_json 不增不减键；"
                         "信封轮全为流式(原生)轮 → 信封由模型自己写出",
        "envelope_calls": env_n, "total_calls": tot,
        "envelope_rate_pct": scan["envelope_rate_pct"],
        "envelope_cycles": scan["envelope_cycles"],
        "branch_by_cycle": scan["branch_by_cycle"],
        "calls": scan["calls"],
        "emitter_checks": checks,
        "parse_transparency": trans,
        "examples": examples,
        "power": {
            "base_rate": env_n / max(tot, 1),
            "n_per_arm_50pct_reduction": power_n(env_n / max(tot, 1), 0.5),
            "n_per_arm_30pct_reduction": power_n(env_n / max(tot, 1), 0.3),
        },
    }


def render(rep: dict) -> str:
    L = []
    L.append("【arguments 信封 · 产生者定位与复发读数】")
    L.append(f"  信封调用 {rep['envelope_calls']}/{rep['total_calls']} "
             f"= {rep['envelope_rate_pct']}%  涉及轮次 {rep['envelope_cycles']}")
    L.append(f"  产生者判定：{rep['producer']}")
    L.append(f"  判据：{rep['producer_rule']}")
    L.append("  —— 机器校验 agent.py ——")
    for c in rep["emitter_checks"]:
        L.append(f"   [{'✔' if c['ok'] else '✘'}] {c['name']}")
        if "发射点" in c["name"]:
            for e in c["evidence"]:
                L.append(f"        yield@{e['line']} ← 赋值@{e['assign_line']} "
                         f"{e['kind']} | {e['rhs'][:70]}")
        elif isinstance(c["evidence"], dict) and c["evidence"]:
            L.append(f"        {c['evidence']}")
        elif isinstance(c["evidence"], list) and c["evidence"]:
            for h in c["evidence"][:5]:
                L.append(f"        {h}")
    t = rep["parse_transparency"]
    L.append(f"  解析层透明性：平铺不变={t['flat_unchanged']} 信封保持={t['envelope_kept']} "
             f"status={t['status']}")
    L.append("  —— 分轮分支（ReasoningDelta 只在流式循环发 → 有 reasoning 即原生轮）——")
    for m in rep["branch_by_cycle"]:
        L.append(f"   cycle{m['cycle']}: {m['branch']} "
                 f"reasoning={m['reasoning_deltas']} stream={m['stream_deltas']}")
    L.append("  —— 信封逐条 ——")
    for c in rep["calls"]:
        L.append(f"   cycle{c['cycle']}:{c['line']} {c['tool']} {c['cls']} "
                 f"inner={c['inner']}")
    ex = rep["examples"]
    L.append("  —— 调用示例审计（F 类打到的工具）——")
    for k, v in ex["tools"].items():
        L.append(f"   {k}（{v['skill']}）: 有示例={v['has_example']} "
                 f"示例数={v['example_count']} 可照抄且平铺={v['examples_ok']} "
                 f"required={v['required']}")
        for b in v["bad_examples"]:
            L.append(f"     ✘ 坏示例 {json.dumps(b['example'], ensure_ascii=False)}：{b['reason']}")
    if ex["tools_missing_example"]:
        L.append(f"   ⚠ 无示例的工具：{ex['tools_missing_example']}")
    if ex["tools_with_envelope_example"]:
        L.append(f"   ⚠ 示例是 arguments 信封（等于教模型包信封）："
                 f"{ex['tools_with_envelope_example']}")
    p = rep["power"]
    L.append(f"  —— 补示例的效果评估要多少样本 ——")
    L.append(f"   基线 {p['base_rate']:.4f}；检测「降 50%」需每组 "
             f"{p['n_per_arm_50pct_reduction']} 次调用，降 30% 需 "
             f"{p['n_per_arm_30pct_reduction']} 次（α=0.05, power=0.8，两组）")
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser(description="arguments 信封产生者定位 + 复发读数")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--traces", default=str(TRACES))
    ap.add_argument("--fail-on-envelope", action="store_true",
                    help="发现信封调用即退出码 2（当回归闸门用）")
    ap.add_argument("--save-baseline", default=None,
                    help="把读数写入指定路径（评估补示例效果的前后对比基线）")
    ap.add_argument("--audit-examples", action="store_true", help="只打示例审计")
    args = ap.parse_args()

    rep = build_report(Path(args.traces))
    if args.audit_examples:
        print(json.dumps(rep["examples"], ensure_ascii=False, indent=1))
    elif args.json:
        print(json.dumps(rep, ensure_ascii=False, indent=1))
    else:
        print(render(rep))
    if args.save_baseline:
        bp = Path(args.save_baseline)
        bp.parent.mkdir(parents=True, exist_ok=True)
        bp.write_text(json.dumps({k: rep[k] for k in
                                  ("producer", "envelope_calls", "total_calls",
                                   "envelope_rate_pct", "envelope_cycles", "power",
                                   "examples")},
                                 ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"[baseline] {bp}")
    if args.fail_on_envelope and rep["envelope_calls"] > 0:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
