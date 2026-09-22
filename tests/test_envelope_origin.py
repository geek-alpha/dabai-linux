# -*- coding: utf-8 -*-
"""arguments 信封产生者定位工具的测试（recidivism:F 的证据链回归）。

这一轮要回答的是「信封是模型写的还是我们解析层造的」。判据必须能被推翻，
所以这里每条断言都配一个反证用例：
  ① 分类器：平铺/多键绝不判成信封（同 normalize_arguments 的守卫口径）；
  ② 发射点校验：真实的 agent.py 通过；**合成的「包一层 arguments」源码必须变红**
     —— 如果哪天有人（或某个分支）真把参数包进 arguments 键，这里会失败；
  ③ 解析层透明性：用仓库自己的 parse_partial_json，平的仍是平的、信封仍是信封
     （解析层既不增键也不减键）→ 信封只可能来自上游；
  ④ 分支判据：只在流式循环里发的 ReasoningDelta 存在 = 该轮是原生轮，此时
     ToolCallStart 记的就是模型流式原文（agent.py:6550 yield tc["arguments"]）；
  ⑤ 端到端：合成 trace 上报告必须给出 producer=model；把合成源码换成包封版本后
     必须不再是 model。
"""
from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.envelope_origin import (  # noqa: E402
    F_TOOLS, build_report, classify_args, emitter_checks, has_example,
    parse_transparency, power_n, scan_traces,
)

_REAL_AGENT = (ROOT / "agent.py").read_text(encoding="utf-8", errors="replace")

# 合成源码：发射点是模型原文、parse 在其后、但**多了**包一层 arguments 的写法
_WRAPPING_SRC = '''
def fake_stream():
    tool_args_str = tc["arguments"]
    arguments, args_status = parse_partial_json(tool_args_str)
    yield ToolCallStart(tool_name=tool_name, arguments=tool_args_str, tool_desc="")
    arguments = {"arguments": arguments}          # ← 我们的解析层在造信封
    yield ToolCallStart(tool_name=tool_name,
                        arguments=json.dumps(arguments, ensure_ascii=False), tool_desc="")
'''

# 合成源码：只有序列化发射点（没有 yield 模型原文）→ 也要判红
_SERIALIZED_ONLY_SRC = '''
def only_json_dumps():
    tool_args_str = json.dumps(arguments, ensure_ascii=False)
    yield ToolCallStart(tool_name=tool_name, arguments=tool_args_str, tool_desc="")
'''


def _write_trace(tmp: Path, cycle: int, events: list):
    p = tmp / f"{cycle}.jsonl"
    p.write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in events),
                 encoding="utf-8")
    return p


def _ev(t, **kw):
    return {"type": t, **kw}


# ---------- ① 分类器 ----------

def test_classify_flat_never_envelope():
    """平铺调用、多键调用都不许判成信封——读数不能比修复动作更激进。"""
    assert classify_args('{"command": "ls"}')["cls"] == "flat"
    assert classify_args('{"files": "a.py", "start": 1}')["cls"] == "flat"
    # 多带一个键：normalize_arguments 的守卫也只认单键，口径必须一致
    assert classify_args('{"arguments": {"command": "ls"}, "timeout": 60}')["cls"] == "flat"
    assert classify_args('{"arguments": "{bad json"}')["cls"] == "envelope_str"
    assert classify_args('{not json')["cls"] == "not_json"
    assert classify_args('["a"]')["cls"] == "not_json"


def test_classify_envelope_kinds():
    assert classify_args('{"arguments": {"command": "ls"}}')["cls"] == "envelope_dict"
    assert classify_args('{"arguments": "{\\"command\\": \\"ls\\"}"}')["cls"] == "envelope_str"


# ---------- ② 发射点校验（真源码通过，合成包封源码必须红） ----------

def test_emitter_checks_pass_on_real_agent():
    checks = {c["name"]: c for c in emitter_checks(_REAL_AGENT)}
    kinds = [e["kind"] for e in checks[
        "ToolCallStart 发射点已分类（至少一个 yield 模型原文）"]["evidence"]]
    assert "RAW模型原文" in kinds, "原生分支必须 yield 模型流式原文"
    assert "SERIALIZED(已解析参数)" in kinds, "文本分支 yield 的是 json.dumps(已解析参数)"
    assert all(c["ok"] for c in checks.values()), checks


def test_emitter_checks_red_on_wrapping_source():
    """反证：源码真包一层 arguments 时，判据必须变红（否则这条判据没有鉴别力）。"""
    checks = {c["name"]: c for c in emitter_checks(_WRAPPING_SRC)}
    key = "无任何『把参数包进 arguments 键』的写法（有则信封由我们产生）"
    assert checks[key]["ok"] is False
    assert checks[key]["evidence"], "报红时必须给出命中行做证据"


def test_emitter_checks_red_without_raw_emitter():
    checks = {c["name"]: c for c in emitter_checks(_SERIALIZED_ONLY_SRC)}
    assert checks["ToolCallStart 发射点已分类（至少一个 yield 模型原文）"]["ok"] is False


# ---------- ③ 解析层透明性 ----------

def test_parse_layer_is_transparent():
    t = parse_transparency()
    assert t["flat_unchanged"] is True      # 解析层不凭空造出 arguments 键
    assert t["envelope_kept"] is True       # 也不替模型脱掉信封


# ---------- ④ 分支判据 + 扫描读数 ----------

def test_scan_counts_and_branch(tmp_path):
    _write_trace(tmp_path, 1, [
        _ev("ReasoningDelta", text="想"),
        _ev("ToolCallStart", tool_name="shell_run", arguments='{"command": "ls"}'),
        _ev("ToolCallResult", tool_name="shell_run", success=True, result="ok"),
        _ev("ReasoningDelta", text="再想"),
        _ev("ToolCallStart", tool_name="shell_run",
            arguments='{"arguments": {"command": "ls"}}'),
    ])
    _write_trace(tmp_path, 2, [
        _ev("StreamDelta", text="hi"),
        _ev("ToolCallStart", tool_name="shell_run", arguments='{"command": "pwd"}'),
    ])
    s = scan_traces(tmp_path)
    assert s["total_calls"] == 3
    assert s["envelope_calls"] == 1
    assert s["envelope_cycles"] == [1]
    b = {m["cycle"]: m["branch"] for m in s["branch_by_cycle"]}
    assert b[1].startswith("native"), "有 ReasoningDelta（流式循环才有）= 原生轮"
    # 只在流式循环里发的 ReasoningDelta 缺失时，不许硬说成原生轮
    assert b.get(2) is None


def test_fail_on_envelope_exit_code(tmp_path):
    _write_trace(tmp_path, 3, [
        _ev("ReasoningDelta", text="x"),
        _ev("ToolCallStart", tool_name="shell_run",
            arguments='{"arguments": "{\\"command\\": \\"ls\\"}"}'),
    ])
    rep = build_report(tmp_path)
    assert rep["envelope_calls"] == 1 and rep["producer"] == "model"


# ---------- ⑤ 示例审计 & 样本量（改判后的杠杆评估） ----------

def test_has_example_detects_call_examples():
    assert has_example("执行命令。例：{\"command\": \"ls -la\"}") is True
    assert has_example("执行命令（Windows：cmd 语法）") is False
    assert has_example("") is False
    assert all(isinstance(t, str) for t in F_TOOLS)


def test_power_n_monotone_and_finite():
    """基线越低越测不起：这正是「用 A/B 评估补示例」要先算样本量的原因。"""
    n_fine = power_n(0.0107, 0.5)
    assert n_fine > 1000, "1% 量级的缺口，几十次调用的 A/B 根本测不出效果"
    assert power_n(0.30, 0.5) < n_fine


def test_report_end_to_end_producer_and_examples(tmp_path):
    _write_trace(tmp_path, 4, [
        _ev("ReasoningDelta", text="想"),
        _ev("ToolCallStart", tool_name="code_read", arguments='{"arguments": "{}"}'),
    ])
    rep = build_report(tmp_path)
    assert rep["producer"] == "model"
    assert rep["envelope_cycles"] == [4]
    assert "tools" in rep["examples"] and "power" in rep
    # 没有任何信封样本时不许冒充实证
    empty = tmp_path / "empty"
    empty.mkdir()
    _write_trace(empty, 5, [_ev("ToolCallStart", tool_name="shell_run",
                                arguments='{"command": "ls"}')])
    assert build_report(empty)["producer"] == "no_envelope_observed"


# ---------- ⑥ 本轮判定：症状被遏制、复发仍可见（两个工具的口径必须同时成立） ----------

def test_symptom_contained_but_recurrence_still_counted():
    """F 的真修好判据：归一只管「这条调用能跑」，不管「信封不再产生」。

    所以同一份信封参数必须同时满足两件事——validate_arguments 放行（症状消失），
    classify_args 照样判 envelope（复发读数还在）。若哪天归一被改回不放行，
    或读数被改成「归一之后就不算信封」，这条会红。
    """
    from tool_validation import validate_arguments
    from tools.envelope_origin import classify_args

    spec = {"type": "function",
            "function": {"name": "shell_run",
                         "parameters": {"type": "object", "required": ["command"],
                                        "properties": {"command": {"type": "string"},
                                                       "timeout": {"type": "number"}}}}}
    env_dict = '{"arguments": {"command": "ls -la"}}'
    env_str = '{"arguments": "{\\"command\\": \\"ls -la\\"}"}'
    for raw in (env_dict, env_str):
        assert classify_args(raw)["cls"].startswith("envelope")
        cleaned, err = validate_arguments(spec, json.loads(raw))
        assert err is None, f"归一后这条调用必须能跑，实际报错：{err}"
        assert cleaned.get("command") == "ls -la"


def test_real_trace_envelope_lines_reproduce_the_verdict():
    """用冻结 trace 里的真实信封行复核判定：至少一条原生轮的信封调用。"""
    traces = ROOT / "data" / "longrun" / "traces"
    if not traces.exists():
        return
    s = scan_traces(traces)
    assert s["envelope_calls"] >= 1, "冻结 traces 里应能读到信封调用（判定的样本来源）"
    assert all(m["reasoning_deltas"] > 0 for m in s["branch_by_cycle"]), \
        "信封轮必须都有流式事件（ReasoningDelta 只在流式循环里发）——否则分支定位不成立"


# ---------- ⑥ 示例落地后的回归（第 2/24 轮：补示例这一动作本身要被钉住） ----------
# 上一轮把「信封=模型自己写的」判死了，但只埋点、没改模型可见面。这一轮把可照抄
# 示例真写进 shell_run / code_read / code_append 三条 description。判据必须能被推翻：
#   ① 示例必须真的在**磁盘上的** skill.json 里（不是测试里自造的字串）；
#   ② 示例必须是可照抄的合法平铺调用——解析出来键在 schema 属性内、required 全齐，
#      且**不是** arguments 信封（否则等于在教模型包信封，把 bug 教成规范）；
#   ③ 反证：把示例换成信封形状 / 去掉示例，同一个检查函数必须判红——否则这条判据
#      只是「描述里出现过『示例』二字」，没有鉴别力。

# 抠取与检查都委托给 tools/envelope_origin.py 的**单一实现**（第 3/24 轮改）：
# 测试里再留一份私有副本，就会出现「测试绿、工具红」或反向的分叉——
# 旧私有副本用的正是看不见嵌套花括号的 r"\{[^{}]*\}"，正是假绿的源头。
from tools.envelope_origin import check_example_flat  # noqa: E402
from tools.envelope_origin import extract_examples as _extract_examples  # noqa: E402


def _load_disk_specs() -> dict:
    specs = {}
    for p in sorted((ROOT / "skills").glob("*/skill.json")):
        d = json.loads(p.read_text(encoding="utf-8"))
        for t in (d.get("tools") or []):
            fn = t.get("function") or t
            if fn.get("name") in F_TOOLS:
                specs[fn["name"]] = t
    return specs


def test_f_tools_have_copyable_flat_examples_on_disk():
    """三条 F 类工具的磁盘 schema 里，示例必须可照抄且是平铺的（本轮唯一动作的验收）。"""
    specs = _load_disk_specs()
    assert set(specs) == set(F_TOOLS), f"应能从磁盘读到三条 schema，实际 {sorted(specs)}"
    for name, spec in specs.items():
        desc = (spec.get("function") or {}).get("description") or ""
        exs = _extract_examples(desc)
        assert exs, f"{name} 的描述里没有可解析的调用示例（补示例没落地）"
        for ex in exs:
            ok, why = check_example_flat(spec, ex)
            assert ok, f"{name} 的示例不合格：{why} | {ex}"
        assert "arguments" in desc and "不要再包" in desc, \
            f"{name} 必须明写「参数是顶层键、不要再包一层 arguments」"


def test_audit_examples_reports_all_present():
    """读数与磁盘一致：审计必须报 0 个缺示例（上一轮报的是 3 个）。"""
    from tools.envelope_origin import audit_examples
    rep = audit_examples()
    assert rep["tools_missing_example"] == [], rep["tools_missing_example"]
    assert all(v["has_example"] for v in rep["tools"].values()), rep["tools"]


def test_example_checker_has_discriminating_power():
    """反证：把示例换成信封形状、或缺必填、或带未知键，检查器必须判红。"""
    spec = {"function": {"name": "shell_run", "parameters": {
        "type": "object", "properties": {"command": {"type": "string"},
                                         "timeout": {"type": "integer"}},
        "required": ["command"]}}}
    ok, why = check_example_flat(spec, {"command": "ls -la"})
    assert ok, why
    ok, why = check_example_flat(spec, {"arguments": {"command": "ls -la"}})
    assert ok is False and "信封" in why
    ok, why = check_example_flat(spec, {"timeout": 60})
    assert ok is False and "缺必填" in why
    ok, why = check_example_flat(spec, {"command": "ls", "bogus": 1})
    assert ok is False and "未声明" in why
    # 抠示例的解析器也不能把散文里的花括号当成示例
    assert _extract_examples("执行命令（Windows：cmd 语法）") == []


# ---------- ⑦ 第 3/24 轮：示例判据自身的漏洞（看不见嵌套花括号 = 假绿） ----------
# 第 2 轮把示例写进了三条 description，测试全绿——但 shell_run 的首个「照抄」正例
# 本身就是包封写法 {"arguments": {"command": "ls -la"}}。旧抠取正则 r"\{[^{}]*\}"
# 看不见嵌套花括号，把它截成 {"command": "ls -la"} 后当成合格的平铺示例，判据因此
# 在最该红的现场保持绿（16 passed 是假绿）。这一轮补判据的鉴别力，并修掉真事故。
# 反证：同一个函数喂包封示例必须判红；喂缺必填/未知键也必须判红。

def _safe_load(s: str):
    try:
        v = json.loads(s)
    except ValueError:
        return None
    return v if isinstance(v, dict) else None


def test_brace_scanner_sees_nested_envelope():
    """括号配对扫描必须看见嵌套的 arguments 信封——旧正则在这里是瞎的。"""
    from tools.envelope_origin import extract_examples
    desc = '调用示例（照抄）：{"arguments": {"command": "ls -la"}} 或 {"command": "df -h"}。'
    exs = extract_examples(desc)
    assert {"arguments": {"command": "ls -la"}} in exs, \
        f"嵌套信封必须被完整抠出，实际 {exs}"
    # 反面示例（省略号不是合法 JSON）天然跳过，不能算成示例
    assert not any("..." in json.dumps(e, ensure_ascii=False) for e in exs), exs
    # 旧正则的行为就是漏洞本身：留一条断言把它钉住
    old = [v for v in (_safe_load(m.group(0))
                       for m in re.finditer(r"\{[^{}]*\}", desc)) if v]
    assert {"arguments": {"command": "ls -la"}} not in old, \
        "旧正则看不见嵌套——这正是它假绿的原因"


def test_check_example_flat_accepts_both_spec_shapes():
    """spec 兼容完整 tool 对象与裸 function 对象，否则 props 取空 → 全键误判假红。"""
    from tools.envelope_origin import check_example_flat
    fn = {"name": "shell_run", "parameters": {
        "type": "object", "properties": {"command": {"type": "string"},
                                         "timeout": {"type": "number"}},
        "required": ["command"]}}
    for spec in (fn, {"type": "function", "function": fn}):
        ok, why = check_example_flat(spec, {"command": "ls -la"})
        assert ok, f"两种 spec 形状都必须认（{spec}）：{why}"


def test_disk_examples_have_no_envelope_positive_example():
    """磁盘上的示例必须是平铺的——尤其 shell_run 不许再把信封当正例教。

    这条就是第 3/24 轮的验收：修之前 shell_run 首个正例是
    {"arguments": {"command": "ls -la"}}，审计 tools_with_envelope_example=[shell_run]；
    修之后必须为空。判据能被推翻：把示例改回信封形状，这条必红。
    """
    from tools.envelope_origin import audit_examples
    rep = audit_examples()
    assert rep["tools_with_envelope_example"] == [], \
        f"示例里不许出现 arguments 信封：{rep['tools_with_envelope_example']}"
    assert rep["tools_without_flat_example"] == [], \
        f"三条工具的示例都必须可照抄且平铺：{rep['tools_without_flat_example']}"
    for name, v in rep["tools"].items():
        assert v["example_count"] >= 1, f"{name} 没有可抠出的示例"
        assert v["examples_ok"], f"{name} 示例不合格：{v['bad_examples']}"


def test_audit_red_when_example_is_envelope(monkeypatch):
    """反证：审计读到信封形状示例时必须报红（判据有鉴别力，不是恒绿）。"""
    import tools.envelope_origin as eo
    real = eo.audit_examples()
    assert real["tools_with_envelope_example"] == [], "磁盘现状应是干净的"

    def fake_audit():
        return {"tools": {"shell_run": {
                    "skill": "code_ops", "has_example": True, "example_count": 1,
                    "examples": [{"arguments": {"command": "ls -la"}}],
                    "bad_examples": [{"example": {"arguments": {"command": "ls -la"}},
                                      "reason": "示例本身是 arguments 信封——等于教模型包信封"}],
                    "examples_ok": False, "required": ["command"], "desc_head": ""}},
                "tools_missing_example": [],
                "tools_without_flat_example": ["shell_run"],
                "tools_with_envelope_example": ["shell_run"],
                "text_protocol_example": ""}
    monkeypatch.setattr(eo, "audit_examples", fake_audit)
    rep = eo.audit_examples()
    assert rep["tools_with_envelope_example"] == ["shell_run"]
    assert rep["tools_without_flat_example"] == ["shell_run"]
