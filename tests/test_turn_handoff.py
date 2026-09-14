#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""「会话控制权」两条改动的回归契约：插话排队 + 交付即停。

背景（2026-09-14）：用户提出两件事——
  1. AI 执行中用户插话，不该打断，应排队（Claude Code 的 queue messages 范式）；
  2. AI 下完结论若用户一分钟内没明说「继续」，默认停下，不许一路自我迭代。
  落点：server.py 的 `_is_stop_word`（停止词精确匹配，非停止词进 pending_messages
  队列）+ agent.py 的「交付即停」提示词 + settings.json 的 max_tool_rounds 兜底。

三条契约：
  1. 停止词精确匹配：集合内才打断；「别停/继续/全自动/继续挖」一律不误伤——
     这是最危险的回退点：用户说「继续」想进连续模式，若被当成「停止」会清空排队并打断。
  2. max_tool_rounds 有界（12，>0）：锁死「无限深挖」的兜底，防止改回 0。
  3. 「交付即停」「一请求一交付」硬规则仍在提示词里，防止重写提示词时被删。

被测函数来源：server.py 的 _STOP_WORDS / _is_stop_word 用 AST 抽出单独 exec
（不 import server，避免拉起 FastAPI app 与事件循环）。
"""
import ast
import json
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parents[1]

WANTED_FUNCS = {"_is_stop_word"}
WANTED_NAMES = {"_STOP_WORDS"}


def _load_stop_word():
    """AST 抽出 server.py 的 _STOP_WORDS + _is_stop_word 单独 exec，避免 import 整个 server。"""
    src = (BASE / "server.py").read_text(encoding="utf-8", errors="replace")
    tree = ast.parse(src)
    nodes = []
    for n in tree.body:
        if isinstance(n, ast.FunctionDef) and n.name in WANTED_FUNCS:
            nodes.append(n)
        elif isinstance(n, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id in WANTED_NAMES for t in n.targets
        ):
            nodes.append(n)
    if len(nodes) != 2:
        pytest.skip("server.py 里没找到 _STOP_WORDS + _is_stop_word（结构变了，契约需重审）")
    ns = {}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(BASE / "server.py"), "exec"), ns)
    return ns["_is_stop_word"]


@pytest.fixture(scope="module")
def is_stop_word():
    return _load_stop_word()


# (输入, 是否该打断)
CASES = [
    ("停止", True),
    ("停", True),
    ("停下", True),
    ("闭嘴", True),
    ("取消", True),
    ("stop", True),          # 大小写不敏感
    (" Stop ", True),        # 前后空白 trim
    ("别继续", True),        # 显式停止意图
    ("别停", False),         # 关键：进连续模式，绝不能当停止
    ("继续", False),         # 关键：进连续模式
    ("继续挖", False),
    ("全自动", False),
    ("别停，继续", False),   # 非精确匹配，不误伤
    ("", False),
]


@pytest.mark.parametrize("text,want", CASES, ids=[c[0] or "<空>" for c in CASES])
def test_stop_word_exact_match(is_stop_word, text, want):
    assert is_stop_word(text) is want, f"_is_stop_word({text!r}) 应为 {want}"


def test_max_tool_rounds_bounded():
    """max_tool_rounds 有界（12），锁死「无限深挖」——改回 0 就是回归。"""
    cfg = json.loads((BASE / "settings.json").read_text(encoding="utf-8"))
    v = cfg.get("agent", {}).get("max_tool_rounds")
    assert isinstance(v, int) and v > 0, f"max_tool_rounds 应为正整数，实际 {v!r}"
    assert v == 12, f"max_tool_rounds 契约值 12 被改：{v!r}"


def test_deliver_stop_prompt_present():
    """「交付即停」「一请求一交付」硬规则仍在提示词里，防止重写提示词时被删。"""
    src = (BASE / "agent.py").read_text(encoding="utf-8", errors="replace")
    assert "交付即停" in src
    assert "一请求一交付" in src


def test_deliver_stop_tail_present():
    """交付即停的兜底文案（说『继续』接着做）仍在工具循环收尾处。"""
    src = (BASE / "agent.py").read_text(encoding="utf-8", errors="replace")
    assert "说『继续』我接着往下做" in src
