#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""落盘套娃的回归用例：读型工具的结果绝不被指针化。

背景（2026-09-23 实战踩坑）：超阈值的工具结果被整条换成一行落盘指针，指针写着
「用 read_lines 读它」——而 read_lines 自己的结果也是 tool 消息，同样超阈值、
同样被换成新指针。于是读回落盘判据连走 8 轮（137b1bc7e4→e03778f5e3→4a4f912942→…），
每轮读到的都是提示本身，判据一个字节都没进上下文；且 read_lines 加的行号前缀让
内容哈希每轮都变，按内容去重完全失效，套娃链无上限。

契约（改这里等于改契约）：
  1. 读型工具（read_lines/code_read/read_file/read_json/cat）的结果永不落盘、永不被指针化；
  2. 读型结果的最新一轮上限比普通工具宽（_READ_TOOL_MAX_CHARS），否则「读回」等于没读回；
  3. 非读型工具（shell_run 等）仍走指针化，成本优化不能被这条修复取消；
  4. 含指针标记的文本永不落盘（第二道熔断，挡住任何没走读型名单的路径）。
"""
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

import memory as M  # noqa: E402


def _round(tool_name, text, call_id="c1"):
    return [
        {"role": "user", "content": "读一下"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": call_id, "type": "function",
             "function": {"name": tool_name, "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": call_id, "content": text},
    ]


def _pack(rnd, is_newest=True):
    packed, _ = M._pack_one_round(
        rnd, 0, 100000, is_newest,
        max_chars_per_tool=M.SHORT_TERM_MAX_CHARS_PER_TOOL,
        pointer_only=M.TOOL_POINTER_ONLY,
        pointer_min_chars=M.TOOL_POINTER_MIN_CHARS)
    return [m for m in packed if m.get("role") == "tool"][0]["content"]


def _spill_files():
    return [p for p in M._TOOL_SPILL_DIR.iterdir()
            if p.is_file() and p.name != "index.jsonl" and not p.name.endswith(".tmp")]


def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(M, "_TOOL_SPILL_DIR", tmp_path)


def test_read_tool_result_never_becomes_pointer(monkeypatch, tmp_path):
    """主 bug：读型结果超阈值时进上下文的是正文，不是又一行指针。"""
    _isolate(monkeypatch, tmp_path)
    text = "判据行：左右 UpperArm 的 rest 世界方向 " + "A" * 4000 + "尾部结论"
    out = _pack(_round("read_lines", text))
    assert M._POINTER_MARK not in out
    assert "判据行" in out and "尾部结论" in out
    assert not _spill_files()


def test_read_tool_newest_gets_wider_limit(monkeypatch, tmp_path):
    """上限比普通工具宽：500 字读不回判据，_READ_TOOL_MAX_CHARS 能。"""
    _isolate(monkeypatch, tmp_path)
    text = "x" * 2500
    read_out = _pack(_round("read_lines", text))
    shell_out = _pack(_round("shell_run", text))
    assert len(read_out) == M._READ_TOOL_MAX_CHARS
    assert len(shell_out) < M.SHORT_TERM_MAX_CHARS_PER_TOOL


def test_old_round_read_tool_not_widened(monkeypatch, tmp_path):
    """历史轮不放宽：越旧越该丢。"""
    _isolate(monkeypatch, tmp_path)
    out = _pack(_round("read_lines", "y" * 1500), is_newest=False)
    assert len(out) <= M.SHORT_TERM_MAX_CHARS_PER_TOOL


def test_readback_loop_creates_no_new_files(monkeypatch, tmp_path):
    """8 轮读回落盘文件：文件数不增长，且每轮都能看见正文头部。"""
    _isolate(monkeypatch, tmp_path)
    original = "第一段判据 " + "Z" * 3000
    _pack(_round("shell_run", original))          # 第 1 轮：大块产出落盘
    assert len(_spill_files()) == 1
    spill = _spill_files()[0]

    for i in range(8):                            # 后 8 轮：读它
        body = spill.read_text(encoding="utf-8")
        numbered = "\n".join(f"{n:>4}│ {ln}" for n, ln in enumerate(body.splitlines(), 1))
        out = _pack(_round("read_lines", numbered, call_id=f"c{i}"))
        assert M._POINTER_MARK not in out
        assert "第一段判据" in out
        assert len(_spill_files()) == 1


def test_readback_shape_exempts_when_tool_name_missing(monkeypatch, tmp_path):
    """工具名还原不出时靠产物形状兜底——真实落盘记录的 tool 字段全为空就是这个场景。"""
    _isolate(monkeypatch, tmp_path)
    body = "\n".join(f"{n:>6}│ 第 {n} 行判据内容" for n in range(1, 200))
    # 只有 tool 消息、没有配对的 assistant tool_calls：名字必然还原不出
    out = _pack([{"role": "user", "content": "读一下"},
                 {"role": "tool", "content": body}])
    assert M._POINTER_MARK not in out
    assert "判据内容" in out
    assert not _spill_files()


def test_readback_shape_detector(monkeypatch, tmp_path):
    """形状判据本身：行号前缀 ≥3 行才算读取产物，普通命令输出不算。"""
    _isolate(monkeypatch, tmp_path)
    assert M._is_readback_text("\n".join(f"{n:>6}│ x" for n in range(1, 5)))
    assert not M._is_readback_text("$ ls -la\n[exit=0]\ntotal 12\n" * 50)


def test_pointer_text_is_never_spilled(monkeypatch, tmp_path):
    """第二道熔断：指针文本落盘只会再造一条读回链。"""
    _isolate(monkeypatch, tmp_path)
    text = f"【工具结果原文已落盘 data/tool_spill/abc1234567.txt（5000字）· 用 read_lines 读它，别重跑命令】\n" + "q" * 3000
    assert M._spill_tool_text(text, "shell_run") == ""
    assert not _spill_files()


def test_shell_run_still_pointerized(monkeypatch, tmp_path):
    """回归保护：普通工具的成本优化不能被这条修复取消。"""
    _isolate(monkeypatch, tmp_path)
    out = _pack(_round("shell_run", "build ok\n" + "L" * 4000))
    assert out.startswith(M._POINTER_MARK)
    assert len(_spill_files()) == 1


def test_spill_index_skips_read_tools(monkeypatch, tmp_path):
    """摘要索引不给读型结果建指针：正文在源文件里，索引它等于重复落盘。"""
    _isolate(monkeypatch, tmp_path)
    msgs = _round("read_lines", "r" * 2000)
    assert M._spill_index_for(msgs) == ""
    assert not _spill_files()
