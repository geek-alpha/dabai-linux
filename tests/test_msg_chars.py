#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""prompt 字符数口径的回归用例：_msg_prompt_chars 的语义契约。

背景（2026-09-13）：pending_chars 曾漏计 reasoning_content（实测单条少 162 字符，
长轮追加量低估 46%）。根因不是某一行写错，而是「逐字段累加 + 新字段漏登记」这个
模式本身 —— 下一次加字段（多模态图注就漏过一次）必然重犯。
补丁改成反向白名单：只排除 role/tool_call_id/name/type，其余字段默认计入。

被测函数来源（按优先级）：
  1. agent.py —— 补丁落地后的正身；
  2. dryrun/agent_patched.py —— 补丁未落地时，验证待批补丁的语义（落地后该分支自然失效）。
两者都拿不到 helper 就 skip：宁可显式跳过，也不假装通过。
"""
import ast
import os
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parents[1]
DRYRUN_COPY = BASE / "data/longrun/ws/self-evolution/dryrun/agent_patched.py"
WANTED = {"_MSG_FRAMEWORK_KEYS", "_msg_prompt_chars"}

# (消息, 期望字符数, 标签)。期望值就是契约，改这里等于改口径。
CASES = [
    ({"role": "assistant", "content": "abc"}, 3, "纯 content"),
    ({"role": "assistant", "content": "abc", "reasoning_content": "de"}, 5, "reasoning 计入"),
    # str([{'id': 'x'}]) == "[{'id': 'x'}]" = 13 字符（repr 口径，与 agent.py 累加点一致）
    ({"role": "assistant", "content": None, "tool_calls": [{"id": "x"}]}, 13, "tool_calls 计入"),
    ({"role": "tool", "tool_call_id": "abc", "content": "ab"}, 2, "框架字段不计"),
    ({"role": "user", "content": "x", "future_field": "yyy"}, 4, "未来新字段默认计入"),
    ({"role": "assistant", "content": None}, 0, "None 不计"),
]


def _candidates():
    env = os.environ.get("DABAI_PATCHED_AGENT")
    paths = [Path(env)] if env else []
    return paths + [BASE / "agent.py", DRYRUN_COPY]


def _load_helper(path):
    """AST 抽出 helper 单独 exec：不 import agent，避免拉起整个进程。"""
    src = path.read_text(encoding="utf-8", errors="replace")
    if "_msg_prompt_chars" not in src:
        return None
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return None
    nodes = [n for n in tree.body
             if (isinstance(n, ast.FunctionDef) and n.name in WANTED)
             or (isinstance(n, ast.Assign) and any(
                 isinstance(t, ast.Name) and t.id in WANTED for t in n.targets))]
    if len(nodes) != 2:
        return None
    ns = {}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), "exec"), ns)
    return ns["_msg_prompt_chars"]


@pytest.fixture(scope="module")
def helper():
    """取第一个含 helper 的来源：agent.py 落地后正身优先，dryrun 副本只是过渡。"""
    for p in _candidates():
        if not p.exists():
            continue
        f = _load_helper(p)
        if f is not None:
            return p, f
    pytest.skip("agent.py 与 dryrun 副本都没有 _msg_prompt_chars（补丁未落地）")


@pytest.mark.parametrize("msg,want,label", CASES, ids=[c[2] for c in CASES])
def test_msg_prompt_chars(helper, msg, want, label):
    path, f = helper
    assert f(dict(msg)) == want, "%s 口径不符：%s" % (label, path)


def test_historical_bug_reasoning_not_dropped(helper):
    """复现历史 bug：漏计 reasoning 时 chars 少 162 字符（verify_echo_accounting 实测值）。"""
    path, f = helper
    msgs = [{"role": "assistant", "content": "x" * 100, "reasoning_content": "推" * 162}]
    chars_raw = sum(f(m) for m in msgs)
    assert chars_raw - 100 == 162, "reasoning 又被漏计了：%s" % path
