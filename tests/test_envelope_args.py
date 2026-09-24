# -*- coding: utf-8 -*-
"""arguments 信封归一 + F 类（必填参数缺失）重放覆盖的回归测试。

背景（本轮修的缺口 recidivism:F，4 次 / 首见后又犯 2 个 cycle）：
data/longrun/traces 里 11/1027 次 ToolCallStart 的参数被包成单键 `{"arguments": ...}`，
其中 7 次内层还是 **JSON 字符串**（双重编码）。信封形状下顶层只有 arguments 一个键，
必填检查必然报「缺少 command / files」——模型看到的报错完全指不出真正原因。
那些行里 9 次的记录错误是 A（技能未加载，校验根本没跑到），只有 2 次落到 F 类
（34.jsonl 的 shell_run），所以 err_recidivism 的 F.count 长期偏小。

测试分三层，每层都要能被推翻：
  ① 运行时真的修好了：validate_arguments 拿到真实 trace 里的信封参数 → 不再报错；
  ② 不该剥的绝不剥（宁可不修也不误伤）：内层键不是该工具参数、工具自身声明了
     arguments 参数、多带一个键 —— 三种都保持原样报缺必填；
  ③ 读数与判定：replay 的 F 组必须覆盖 err_recidivism 的 F.count（同源 classify），
     且结构性残留（args 截断不可还原）不许把 self_iterate 的 feasible 钉在 1.0。
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tool_validation import normalize_arguments, validate_arguments  # noqa: E402

SHELL = {"type": "function",
         "function": {"name": "shell_run",
                      "parameters": {"type": "object", "required": ["command"],
                                     "properties": {"command": {"type": "string"},
                                                    "timeout": {"type": "number"}}}}}
CODE_READ = {"type": "function",
             "function": {"name": "code_read",
                          "parameters": {"type": "object", "required": ["files"],
                                         "properties": {"files": {"type": "array"},
                                                        "root": {"type": "string"},
                                                        "max_lines": {"type": "number"}}}}}
# 工具自己就有一个叫 arguments 的参数：单键形态在这里是合法调用
TOOL_WITH_ARGS_PARAM = {"type": "function",
                        "function": {"name": "raw_exec",
                                     "parameters": {"type": "object",
                                                    "required": ["arguments"],
                                                    "properties": {
                                                        "arguments": {"type": "object"}}}}}

CMD = "cd /home/wxf/dabai && ls tools/"


# ---------- ① 运行时确实修好了 ----------

def test_envelope_json_string_unwrapped():
    """34.jsonl 的真实形状：内层是 JSON 字符串。"""
    args = {"arguments": json.dumps({"command": CMD})}
    out, note = normalize_arguments(SHELL, args)
    assert note == "arguments_envelope" and out == {"command": CMD}
    cleaned, err = validate_arguments(SHELL, args)
    assert err is None, err
    assert cleaned == {"command": CMD}


def test_envelope_dict_unwrapped():
    args = {"arguments": {"command": CMD}}
    out, note = normalize_arguments(SHELL, args)
    assert note == "arguments_envelope" and out == {"command": CMD}
    assert validate_arguments(SHELL, args)[1] is None


def test_envelope_inner_not_json_stays_untouched():
    """内层字符串解不出 dict（真·字符串参数）→ 不许剥，保持原样报缺必填。"""
    args = {"arguments": "not json at all"}
    assert normalize_arguments(SHELL, args) == (args, None)
    assert "缺少必填参数: command" in validate_arguments(SHELL, args)[1]


# ---------- ② 不该剥的绝不剥（反证：宁可不修也不误伤） ----------

def test_envelope_with_foreign_keys_not_unwrapped():
    """19.jsonl 真实形状：信封内层是 **别的工具** 的参数（paths ∉ code_read）。

    剥错会造成比原 bug 更差的后果：把「缺 files」换成执行一个参数根本不匹配的调用。
    """
    args = {"arguments": json.dumps({"paths": ["docs/progress-metric.md"]})}
    assert normalize_arguments(CODE_READ, args) == (args, None)
    err = validate_arguments(CODE_READ, args)[1]
    assert "缺少必填参数: files" in err


def test_envelope_extra_key_not_unwrapped():
    """顶层多一个键就不是信封形状（模型可能真在传一个叫 arguments 的字段）。"""
    args = {"arguments": json.dumps({"command": CMD}), "timeout": 30}
    assert normalize_arguments(SHELL, args) == (args, None)


def test_tool_declaring_arguments_param_not_unwrapped():
    """工具自己声明了 arguments 参数时，单键形态是合法调用，剥了会变成缺必填。"""
    args = {"arguments": {"arguments": "payload"}}
    assert normalize_arguments(TOOL_WITH_ARGS_PARAM, args) == (args, None)
    assert validate_arguments(TOOL_WITH_ARGS_PARAM, args)[1] is None


def test_no_schema_skips_key_check_but_keeps_shape_check():
    """拿不到 schema（冷启动）时只按形状判：内层键无法核对，仍按信封处理。"""
    args = {"arguments": json.dumps({"command": CMD})}
    assert normalize_arguments(None, args)[1] == "arguments_envelope"
    assert normalize_arguments(None, {"arguments": {"a": 1}, "b": 2})[1] is None


# ---------- ③ 报错本身要能教会模型 ----------

def test_missing_required_error_carries_required_list_and_example():
    """F 里 1 次是模型整组传错参数（code_read 收到 skill_name）——校验层治不了，
    只能让报错带着「这个工具要什么」。"""
    err = validate_arguments(CODE_READ, {"skill_name": "code_ops"})[1]
    assert "缺少必填参数: files" in err
    assert "本工具必填参数: files" in err
    assert 'code_read(files=[])' in err


def test_hint_only_for_missing_required_not_for_type_errors():
    """反证：类型错的消息不许被塞进必填提示（那是另一类问题的噪声）。"""
    err = validate_arguments(SHELL, {"command": {"a": 1}})[1]
    assert "类型不符" in err
    assert "正确调用示例" not in err


# ---------- ④ 真实 trace 端到端 + 重放读数 ----------

def _real_f_calls():
    """从真实 trace 里取 F 类那两条（34.jsonl shell_run 缺 command）的原始参数。"""
    path = ROOT / "data" / "longrun" / "traces" / "34.jsonl"
    if not path.exists():
        pytest.skip("trace 不在盘上")
    pend, hits = [], []
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        if not line.strip():
            continue
        ev = json.loads(line)
        if ev.get("type") == "ToolCallStart":
            try:
                raw = json.loads(ev.get("arguments") or "{}")
            except ValueError:
                raw = None
            pend.append(raw)
        elif ev.get("type") == "ToolCallResult":
            raw = pend.pop(0) if pend else None
            if str(ev.get("success")) in ("True", "true"):
                continue
            if "缺少必填参数: command" in str(ev.get("result", "")):
                hits.append((ev.get("tool_name"), raw))
    return hits


def test_real_trace_f_rows_now_pass_validation():
    """当年报错的那两条，现在走校验层直接过。"""
    hits = _real_f_calls()
    assert hits, "34.jsonl 里应有 2 条缺 command 的失败"
    for tool, raw in hits:
        assert tool == "shell_run"
        cleaned, err = validate_arguments(SHELL, raw)
        assert err is None, f"{raw!r} -> {err}"
        assert cleaned["command"].startswith("cd /home/wxf/dabai")


def test_replay_f_group_covers_recidivism_and_separates_residual():
    """F 必须在重放里有覆盖，且读数得区分「本机制可治」与「结构性不可治」。

    这条是本轮的判据本体：total 必须等于 err_recidivism 的 F.count（两边同一分母），
    still_fixable 必须为 0（否则说明归一没生效，缺口没真修），不可判的截断行要单列。
    """
    def run(script, extra=()):
        return json.loads(subprocess.run(
            [sys.executable, str(ROOT / "tools" / script), "--json", *extra],
            cwd=str(ROOT), capture_output=True, text=True, timeout=600).stdout)

    err = run("err_recidivism.py")
    if not err["classes"]:
        # data/longrun/ 不入仓（长跑引擎运行态），本机没跑过长跑就分不出错误类型。
        # 这不是代码缺陷 —— 判据本身要在有 longrun trace 的机器上验，缺数据时跳过。
        pytest.skip("本机没有 data/longrun/traces/*.jsonl，err_recidivism 无可分类报错")
    f_count = next(c["count"] for c in err["classes"] if c["code"] == "F")
    f = run("replay_validation.py")["classes"]["F"]
    assert f["total"] == f_count, f"重放分母 {f['total']} ≠ 历史计数 {f_count}"
    assert f["still_fixable"] == 0, "归一没覆盖住的同类错必须为 0"
    assert f["gone"] >= 2, "信封那两条应已消失"
    assert f["args_full"] + f["unjudgeable"] == f["total"]
