#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""工具报错长期流水：turn_metrics 的 300 行窗口撑不起「同错复发率」。

背景（2026-09-14）：为了回答「某条教训写入之后，同类错误还犯不犯」，需要
【错误类型 + 时间】两样。时间戳上一轮补了（tools/lesson_add.py 的平行 ts 表），
错误类型是这一轮的缺口——turn_metrics 只有 tool_errors 计数（实测 229 轮共 106 次），
分不清是哪个工具、哪类错。

为什么不直接加进 turn_metrics：turn_metrics.py:30-31 是 MAX_LINES=500 /
KEEP_LINES=300，实测 228 轮≈25 小时，即窗口只有约 1.4 天；而「教训写入后同类错误
还犯不犯」要的是跨周窗口，滚掉之后前后两段就切不出来了。所以另开一条不滚动的流水，
只在真有报错时写（实测 229 轮里 65 轮，约 28%），体积可忽略。

契约：
  1. 没有报错就不写文件——不许拿空行当噪音
  2. 有报错写一行，字段 ts/goal/err_names/call_names 齐全，工具名是原始名字
  3. 不滚动：写满 600 行仍全在（这是与 turn_metrics 分开存的全部理由）
  4. goal 截断到 60 字符，别让一个长目标把每行撑大
  5. 落盘失败静默吞掉——观测绝不能成为故障源
"""
import importlib.util
import json
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]


def _load(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("turn_metrics_under_test", BASE / "turn_metrics.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "ERR_TRACE_PATH", str(tmp_path / "tool_err_trace.jsonl"))
    return mod


def _rows(path):
    if not Path(path).exists():
        return []
    return [json.loads(x) for x in Path(path).read_text(encoding="utf-8").splitlines() if x.strip()]


def test_no_error_writes_nothing(tmp_path, monkeypatch):
    mod = _load(tmp_path, monkeypatch)
    mod.record_err([], ["code_read"], "目标")
    assert not Path(mod.ERR_TRACE_PATH).exists(), "无报错轮不该产生文件或空行"


def test_row_fields(tmp_path, monkeypatch):
    mod = _load(tmp_path, monkeypatch)
    mod.record_err(["code_edit"], ["code_read", "code_edit"], "改个东西")
    rows = _rows(mod.ERR_TRACE_PATH)
    assert len(rows) == 1
    r = rows[0]
    assert r["err_names"] == ["code_edit"], "工具名要原样保留，后续要按名字统计复发"
    assert r["call_names"] == ["code_read", "code_edit"], "同一轮调过什么也要留，否则看不出误伤范围"
    assert r["goal"] == "改个东西"
    assert abs(r["ts"] - __import__("time").time()) < 60, "时间戳必须可用来切前后两段"


def test_same_tool_twice_keeps_both(tmp_path, monkeypatch):
    mod = _load(tmp_path, monkeypatch)
    mod.record_err(["shell_run", "shell_run"], ["shell_run"], "跑命令")
    assert _rows(mod.ERR_TRACE_PATH)[0]["err_names"] == ["shell_run", "shell_run"], "错两次要记两次，否则复发强度被抹平"


def test_goal_truncated(tmp_path, monkeypatch):
    mod = _load(tmp_path, monkeypatch)
    mod.record_err(["shell_run"], [], "长" * 500)
    assert len(_rows(mod.ERR_TRACE_PATH)[0]["goal"]) == 60


def test_no_rollover_unlike_turn_metrics(tmp_path, monkeypatch):
    mod = _load(tmp_path, monkeypatch)
    for i in range(600):
        mod.record_err(["shell_run"], ["shell_run"], f"第{i}轮")
    rows = _rows(mod.ERR_TRACE_PATH)
    assert len(rows) == 600, "这条流水不滚动——滚动就等于把跨周窗口砍掉"
    assert rows[0]["goal"] == "第0轮", "最早一行必须在，否则前后对照无从做起"


def test_write_failure_is_silent(tmp_path, monkeypatch):
    mod = _load(tmp_path, monkeypatch)
    monkeypatch.setattr(mod, "ERR_TRACE_PATH", str(tmp_path / "no_such_dir" / "\0bad" / "x.jsonl"))
    mod.record_err(["shell_run"], [], "目标")
