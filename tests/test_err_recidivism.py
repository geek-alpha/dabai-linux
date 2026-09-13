"""同错复发报告（tools/err_recidivism.py）的回归测试。

关键不变量：复发计数必须按 cycle 去重——同一 cycle 内同类错犯两次，
「其后几个 cycle 还犯」只能算 1，否则数字翻倍、看不出到底学没学会。
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))
import err_recidivism as er  # noqa: E402


def _w(path, rows):
    with open(path, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")


def _res(tool, ok, result=""):
    return {"type": "ToolCallResult", "tool_name": tool,
            "success": ok, "result": result}


def test_classify_five_classes():
    assert er.classify("工具 'shell_run' 属于技能 code_ops，但尚未注册。请先调用 skill_help") == "A"
    assert er.classify("工具 'read_file' 不存在或未注册，请先通过 skill_help 确认") == "B"
    assert er.classify("工具参数校验失败：queries: 类型不符，期望 string") == "C"
    assert er.classify("该工具不属于当前技能") == "D"
    assert er.classify("执行超时（>120.0s）") == "E"


def test_classify_specific_class_wins_over_generic():
    # 原文同时含「尚未注册」和「参数校验失败」时归 A——更具体的那类先判。
    text = "工具 'x' 尚未注册；工具参数校验失败：a 类型不符"
    assert er.classify(text) == "A"


def test_recurrence_dedupes_cycles():
    r = er.recurrence([8, 8, 9, 10, 10])
    assert r["first"] == 8
    assert r["after_first"] == 2, "同一 cycle 犯两次只能算一个 cycle"
    assert r["span"] == 2


def test_recurrence_empty():
    r = er.recurrence([])
    assert r["first"] is None and r["after_first"] == 0


def test_read_traces_counts_calls(tmp_path):
    _w(tmp_path / "8.jsonl", [_res("shell_run", True), _res("shell_run", False, "尚未注册")])
    _w(tmp_path / "9.jsonl", [_res("code_read", False, "参数校验失败：x 类型不符")])
    got = er.read_longrun_traces(str(tmp_path))
    assert got["total"] == 3
    assert got["fail"] == 2
    assert got["fail_by_class"] == {"A": 1, "C": 1}
    assert got["cycle_range"] == [8, 9]


def test_attempt_file_shares_cycle_number(tmp_path):
    """8.attempt1-*.jsonl 是同一 cycle 的另一次尝试（实测非副本，调用数各不相同）。

    它必须被计入调用数，但归属同一个 cycle——否则复发计数会把它当两个 cycle，
    把「同一个 cycle 重试了两次」误读成「跨 cycle 又犯了一次」。
    """
    _w(tmp_path / "8.jsonl", [_res("shell_run", False, "尚未注册")])
    _w(tmp_path / "8.attempt1-083629.jsonl", [_res("shell_run", False, "尚未注册")])
    got = er.read_longrun_traces(str(tmp_path))
    assert got["total"] == 2
    assert got["cycle_range"] == [8, 8]
    assert got["cycles_with_class"]["A"] == [8, 8]
    assert er.recurrence(got["cycles_with_class"]["A"])["after_first"] == 0


def test_read_lesson_ts_uses_ts_key(tmp_path):
    p = tmp_path / "m.json"
    p.write_text(json.dumps({"lessons": ["a", "b", "c"], "ts": {"k1": 1.0}}), encoding="utf-8")
    got = er.read_lesson_ts(str(p))
    assert got == {"lessons": 3, "stamped": 1}


def test_read_lesson_ts_missing_file(tmp_path):
    assert er.read_lesson_ts(str(tmp_path / "nope.json")) == {"lessons": 0, "stamped": 0}


def test_read_interactive_errs_missing_file(tmp_path):
    got = er.read_interactive_errs(str(tmp_path / "nope.jsonl"))
    assert got["n"] == 0 and got["fail_by_tool"] == {}


def test_read_interactive_errs_counts_tool_names(tmp_path):
    p = tmp_path / "e.jsonl"
    _w(p, [{"ts": 1, "err_names": ["shell_run", "shell_run", "code_read"]},
           {"ts": 2, "err_names": []}])
    got = er.read_interactive_errs(str(p))
    assert got["n"] == 2
    assert got["fail_by_tool"] == {"shell_run": 2, "code_read": 1}


def test_build_and_render_end_to_end(tmp_path):
    _w(tmp_path / "8.jsonl", [_res("shell_run", False, "尚未注册")])
    _w(tmp_path / "10.jsonl", [_res("shell_run", False, "尚未注册")])
    mem = tmp_path / "m.json"
    mem.write_text(json.dumps({"lessons": ["x"], "ts": {}}), encoding="utf-8")
    rep = er.build_report(str(tmp_path), str(tmp_path / "none.jsonl"), str(mem))
    assert rep["sources"]["longrun"]["fails"] == 2
    top = rep["classes"][0]
    assert top["code"] == "A" and top["share"] == 100.0
    assert top["recurrence"]["after_first"] == 1
    text = er.render(rep)
    assert "A 技能未加载" in text and "首见 cycle 8" in text
