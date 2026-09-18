#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""「会话控制权」两条改动的回归契约：插话排队 + 交付即停。

背景（2026-09-14）：用户提出两件事——
  1. AI 执行中用户插话，不该打断，应排队（Claude Code 的 queue messages 范式）；
     —— 2026-09-15 反转：排队导致用户「打字打断不了」，改回打字即打断
     （text 分支直接走 allow_interrupt=True；仅界面自动上报的 ui 消息不打断）。
     排队机制（pending_messages / _drain_pending）已整段删除，不要再加回来。
  2. AI 下完结论若用户一分钟内没明说「继续」，默认停下，不许一路自我迭代。
  落点：server.py 的 `_is_stop_word`（停止词精确匹配）+ agent.py 的「交付即停」提示词
  + settings.json 的 max_tool_rounds=0（不设上限）。

三条契约：
  1. 停止词精确匹配：集合内才打断；「别停/继续/全自动/继续挖」一律不误伤——
     这是最危险的回退点：用户说「继续」想进连续模式，若被当成「停止」会清空排队并打断。
  2. max_tool_rounds=0（不限制）：任务有多长由任务决定；护栏改由 repeat_guard_rounds 承担。
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


def test_tool_rounds_unlimited_but_guarded():
    """工具轮数不许设人工上限（0=不限制），护栏交给死循环检测而不是砍轮数。

    历史：先锁 12、后锁 80——同一类错误：任务有多长由任务决定，不由配置决定。
    撞上限的表现是最后一轮正文被静默清空，用户拿到「本轮没有产出正文」，得打字
    「继续」。契约：max_tool_rounds=0（跑到模型自己不再调用工具为止），失控风险
    由 repeat_guard_rounds（连续 N 轮完全相同的工具+参数才停）和轮内上下文压缩兜住。
    """
    a = json.loads((BASE / "settings.json").read_text(encoding="utf-8")).get("agent", {})
    v = a.get("max_tool_rounds")
    assert v == 0, f"max_tool_rounds={v!r}：人工上限会在大项目半路截断任务，必须是 0"
    g = a.get("repeat_guard_rounds")
    assert isinstance(g, int) and 0 < g <= 200, f"死循环护栏 repeat_guard_rounds={g!r} 必须存在且有界"


def test_turn_checkpoint_on_for_hot_reload():
    """断点续跑必须开着：改核心 .py 触发的热重载会掐掉进行中的轮。

    历史：turn_checkpoint=false 时热重载掐断的轮没有任何落盘，恢复无从谈起，
    用户看到的就是「本轮没有产出正文，可能被截断」。开着才有得恢复：每轮工具
    执行前落盘（save_turn_checkpoint）+ 启动/重连时自动续跑（server.py 的
    _resume_pending_turns / load_turn_checkpoint）。
    """
    a = json.loads((BASE / "settings.json").read_text(encoding="utf-8")).get("agent", {})
    assert a.get("turn_checkpoint") is True, "turn_checkpoint 必须开着，否则热重载=任务直接死"
    src = (BASE / "agent.py").read_text(encoding="utf-8", errors="replace")
    srv = (BASE / "server.py").read_text(encoding="utf-8", errors="replace")
    assert "def save_turn_checkpoint" in src and "def load_turn_checkpoint" in src
    assert "_resume_pending_turns" in srv


def test_deliver_stop_prompt_present():
    """「交付即停」「一请求一交付」硬规则仍在提示词里，防止重写提示词时被删。"""
    src = (BASE / "agent.py").read_text(encoding="utf-8", errors="replace")
    assert "交付即停" in src
    assert "一请求一交付" in src


def test_no_continue_tail_on_normal_finish():
    """中断时自动收尾，不把「说继续」推回给用户。

    历史一：正常交付追加「（本轮先交付到这里，说『继续』我接着往下做）」，
    用户每次都要打字「继续」才能往下走。
    历史二：无正文时落库「说『继续』可从断点接着干」——本质还是把「判断任务
    断在哪、要不要接着跑」的成本转嫁给用户。
    契约：无正文先自动补一次无工具收尾（_closing_summary），拿到正文就正常
    交付；连收尾都失败才如实说明中断。
    """
    src = (BASE / "agent.py").read_text(encoding="utf-8", errors="replace")
    assert "说『继续』我接着往下做" not in src
    assert "说『继续』可从断点接着干" not in src
    # 自动收尾：定义 + 在无正文分支被调用
    assert "async def _closing_summary" in src
    assert "await self._closing_summary(" in src
