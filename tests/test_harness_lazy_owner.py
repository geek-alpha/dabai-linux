# -*- coding: utf-8 -*-
"""属主索引未命中时补齐加载：把「技能还没加载」和「工具不存在」分开。

背景：harness.execute_tool 未命中属主时返回 (None, '')，契约是「交给本地工具路由」。
可本地工具表里现在只剩 skill_help（实测 load_local_tools() 只产出它一个），所以任何
「工具其实存在、只是它的技能还没进索引」的调用都会掉到 fuctions_all_you_need_base
（模块已不存在）→ 回一句「工具 'X' 未找到实现」：模型既没拿到结果，也拿不到可行动的
线索，白跑一轮。这是 err_recidivism A 类（技能未加载，43/57，首见后又犯 24 个 cycle）
的同一笔浪费 —— agent 侧已在 _validate_tool_call 里自动加载，但 harness 路由的调用方
（流程/批量步骤、子智能体）不经过那一层。

判据（可推翻）：把 Harness.tool_owner 里的 `return self._resolve_owner(...)` 去掉，
test_属主缺失_补齐后能加载_且执行拿到结果 与 test_真实harness_属主缺失_自动补齐
两条立刻变红。
"""
import asyncio
import pathlib
import sys
import threading

import pytest

BASE = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

import harness  # noqa: E402
from harness.core import Harness  # noqa: E402


class _FakeSkills:
    """只提供 tool_owner / _rebuild_index / _resolve_owner 真正读写的字段。

    ensure_loaded() 复刻真实现的语义：把 discover() 到但还没进 _loaded 的技能补齐
    （真实实现是 import 它的 skill.py，这里只关心索引能不能命中）。
    """

    def __init__(self, loaded=None, discover=None, disabled=()):
        self._loaded = dict(loaded or {})
        self._discovered = list(discover or [])
        self.disabled = set(disabled)
        self.loaded_calls = 0

    def discover(self):
        return list(self._discovered)

    def ensure_loaded(self):
        self.loaded_calls += 1
        for m in self._discovered:
            if m["name"] not in self._loaded:
                self._loaded[m["name"]] = {"tools": m.get("tools", {})}

    def is_enabled(self, name):
        return name not in self.disabled


class _FakePlugins:
    def __init__(self):
        self._loaded = {}
        self.loaded_calls = 0

    def discover(self):
        return []

    def ensure_loaded(self):
        self.loaded_calls += 1

    def is_enabled(self, name):
        return True


def _harness(loaded=None, discover=None, disabled=()):
    """只借方法、不跑 __init__：不启线程、不读盘、不碰真技能。"""
    h = Harness.__new__(Harness)
    h.skills = _FakeSkills(loaded, discover, disabled)
    h.plugins = _FakePlugins()
    h._tool_index = {}
    h._index_lock = threading.Lock()
    h._loaded = True
    return h


def _tool_spec(name):
    return {"type": "function", "function": {"name": name, "parameters": {"type": "object", "properties": {}}}}


COLD = [{"name": "code_ops", "tools": {"shell_run": _tool_spec("shell_run"),
                                       "code_read": _tool_spec("code_read")}}]


def test_索引未命中_补齐加载后能命中属主():
    """冷启动（技能还没加载）→ tool_owner 现场补齐后拿到属主，而不是 None。"""
    h = _harness(loaded={}, discover=COLD)
    assert h.tool_owner("shell_run") == ("skill", "code_ops")
    assert h.skills.loaded_calls == 1, "补齐应恰好触发一次 ensure_loaded"


def test_属主缺失_补齐后索引真的建起来了():
    """不只是把名字查出来：补齐后索引里整条技能的工具都在（执行路由才走得通）。"""
    h = _harness(loaded={}, discover=COLD)
    assert h.tool_owner("shell_run") == ("skill", "code_ops")
    assert h._tool_index["code_read"] == ("skill", "code_ops"), "同技能的工具应一并进索引"


def test_已在索引里_不重复补齐():
    """热路径不许被兜底拖慢：命中就直接返回，不触发任何 reload。"""
    h = _harness(loaded={"code_ops": {"tools": {"shell_run": {}}}}, discover=COLD)
    h._rebuild_index()
    assert h.tool_owner("shell_run") == ("skill", "code_ops")
    assert h.skills.loaded_calls == 0


def test_禁用技能不因兜底被绕过():
    """启停选择优先：技能被禁用时，补加载也不许把它变成可调用。"""
    h = _harness(loaded={}, discover=COLD, disabled={"code_ops"})
    assert h.tool_owner("shell_run") is None


def test_未知工具仍返回None():
    """幻觉工具名：补齐后仍查不到 → 保持原契约 (None, '')，不抛异常、不误判成技能缺失。"""
    h = _harness(loaded={}, discover=COLD)
    assert h.tool_owner("read_file_nope") is None


def test_补齐失败不炸_退回None(monkeypatch):
    """补齐过程中抛异常（技能目录坏了等）不许把整轮带崩。"""
    h = _harness(loaded={}, discover=COLD)

    def boom():
        raise RuntimeError("技能目录读不了")

    monkeypatch.setattr(h.skills, "ensure_loaded", boom)
    assert h.tool_owner("shell_run") is None


def test_真实harness_属主缺失_自动补齐():
    """端到端：真 harness 上把 code_ops 从加载表里摘掉，execute_tool 仍要跑通。

    这条是「白跑一轮」被判死的现场：修好之前 execute_tool 返回 (None, '')，
    调用方回落到不存在的 fuctions_all_you_need_base，回一句误导性的
    「工具 'shell_run' 未找到实现」。
    """
    h = harness.get_harness()
    h.ensure_loaded()
    if "code_ops" not in h.skills._loaded:
        pytest.skip("本机加载不到 code_ops 技能，跳过端到端")
    h.skills._loaded.pop("code_ops")
    h._rebuild_index()
    assert h._tool_index.get("shell_run") is None, "前置条件：索引里必须确实没有它"
    res, source = asyncio.run(h.execute_tool("shell_run", {"command": "echo lazy-owner-probe"}))
    assert res is not None, "属主缺失时不许静默回落（那就是白跑一轮）"
    assert "lazy-owner-probe" in str(res), res
    assert source == "skill"
