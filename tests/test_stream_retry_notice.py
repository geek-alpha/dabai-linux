# -*- coding: utf-8 -*-
"""流式重建的用户可见文案必须和真实等待时长同源。

背景（2026-09-20）：流式重试分支先算出 `_delay`，再分别喂给 TurnStatus 文案
（「网络波动，Ns 后自动重连」）和 asyncio.sleep。文案说 2s 而实际等 30s，
用户会当成卡死并手动打断——自动重连的收益正好被抵消。

为什么不做端到端跑 chat_stream：`_chat_stream_normal` 第一件事就是
`_ensure_initialized()` → `initialize()` 会加载真实 settings.json、建真实 LLM
client、初始化真实记忆库；测试会读到真实 api_key 并往真实会话写数据。
所以走「纯函数 + AST 接线守卫」（与 test_stream_retry_after.py 同一套做法），
守卫覆盖到最关键的一点：文案和 sleep 必须是同一个 `_delay` 变量。
"""
import ast
from pathlib import Path

import agent as agent_mod
from agent import _stream_retry_notice


def test_文案秒数就是真实等待时长():
    assert _stream_retry_notice(3, 30.0) == \
        "网络波动，30s 后自动重连（第 3 次，任务不会中断）"


def test_秒数跟随服务端要求而不是退避序号():
    """变异靶心：文案写成 2*attempt 时，Retry-After: 30 的场景会显示 2s。"""
    text = _stream_retry_notice(1, 30.0)
    assert "30s" in text
    assert "2s" not in text


def test_小数按显示精度四舍五入():
    """0.84s 的指数退避要显示成 1s，不能显示成 0s（看起来像没等）。"""
    for d in (0.84, 1.6, 2.0, 4.4, 29.6, 30.0):
        assert f"{d:.0f}s" in _stream_retry_notice(1, d), d


def test_次数如实反映第几次重建():
    assert "第 7 次" in _stream_retry_notice(7, 5.0)


def _parse_agent():
    return ast.parse(Path(agent_mod.__file__).read_text(encoding="utf-8"))


def _calls(node, name):
    return [n for n in ast.walk(node)
            if isinstance(n, ast.Call) and getattr(n.func, "id", "") == name]


def _enclosing_async_func(tree, target):
    for f in [n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef)]:
        if any(sub is target for sub in ast.walk(f)):
            return f
    return None


def test_接线守卫_提示与sleep用同一个_delay():
    tree = _parse_agent()
    notices = _calls(tree, "_stream_retry_notice")
    assert len(notices) == 1, f"流式重试提示必须走 _stream_retry_notice，实际 {len(notices)} 处"
    names = [a.id for a in notices[0].args if isinstance(a, ast.Name)]
    assert names == ["stream_attempt", "_delay"], names

    func = _enclosing_async_func(tree, notices[0])
    assert func is not None, "提示调用不在任何 async 函数里？"
    sleeps = [n for n in ast.walk(func)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
              and n.func.attr == "sleep"]
    after = [s for s in sleeps if s.lineno > notices[0].lineno]
    assert after, "提示之后必须有 sleep——否则告诉用户要等，代码却没等"
    nearest = min(after, key=lambda s: s.lineno)
    assert isinstance(nearest.args[0], ast.Name), ast.dump(nearest.args[0])
    assert nearest.args[0].id == "_delay", nearest.args[0].id


def test_接线守卫__delay在提示与sleep之间不被重算():
    """同源的前提是同一个值：中间若重算，文案与实睡就漂了。"""
    tree = _parse_agent()
    notice = _calls(tree, "_stream_retry_notice")[0]
    func = _enclosing_async_func(tree, notice)
    stores = [n for n in ast.walk(func)
              if isinstance(n, ast.Name) and n.id == "_delay"
              and isinstance(n.ctx, ast.Store)]
    assert len(stores) == 1, f"_delay 应只算一次，实际赋值行 {[n.lineno for n in stores]}"
