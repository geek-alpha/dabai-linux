"""工具未注册时自动加载所属技能（A 类报错的消除）。

背景：data/longrun/traces 里 57 次工具失败中 43 次（75.4%）是「技能未加载」——
模型调了 shell_run，而 code_ops 还没 skill_help 过。报错原文自己写着「请先调用
skill_help 加载该技能后重试」，模型照犯不误（首见 cycle 8，其后 24 个 cycle 仍
复发）。所以把这次往返从「指望模型自觉」改成「分发处自动补」。

这里用桩对象调未绑定的 Agent._validate_tool_call：真实调用需要完整的 Agent
（LLM 客户端、配置、记忆），而这段逻辑只碰 _all_tools / _activate_skill。
"""
import pathlib
import sys
import types

import pytest

BASE = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

import agent  # noqa: E402
import harness  # noqa: E402
from tool_validation import find_tool_spec  # noqa: E402


def _spec(name: str, **props) -> dict:
    """造一个最小可用工具定义（带必填 command，便于验证校验仍然生效）。"""
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": "测试用",
            "parameters": {
                "type": "object",
                "properties": props or {"command": {"type": "string"}},
                "required": ["command"],
            },
        },
    }


class _Stub:
    """只提供 _validate_tool_call / _activate_skill 真正读写的字段。"""

    def __init__(self, tools=None, added=1, loaded_spec=None, boom=False):
        self._all_tools = list(tools or [])
        self._local_tool_names = set()
        self._skill_last_used = {}
        self._activated_skills = set()
        self._skill_order = []
        self._added = added
        self._loaded_spec = loaded_spec
        self._boom = boom
        self.activated = []

    def _max_active_tools(self):
        return 999

    def _max_active_tools_chars(self):
        return 10 ** 9

    def _activate_skill(self, name, restored=False):
        self.activated.append(name)
        if self._boom:
            raise RuntimeError("技能加载炸了")
        if self._added and self._loaded_spec is not None:
            self._all_tools.append(self._loaded_spec)
        return self._added


def _fake_harness(owner):
    return types.SimpleNamespace(tool_owner=lambda _name: owner)


def _call(stub, tool, args):
    return agent.AIAgent._validate_tool_call(stub, tool, args)


def test_owner_skill_auto_loaded_then_validated(monkeypatch):
    """未注册但属于某技能 → 自动加载该技能，原调用直接放行。"""
    monkeypatch.setattr(harness, "get_harness",
                        lambda: _fake_harness(("skill", "demo")))
    stub = _Stub(loaded_spec=_spec("shell_run"))
    args, err = _call(stub, "shell_run", {"command": "echo hi"})
    assert err is None, err
    assert args == {"command": "echo hi"}
    assert stub.activated == ["demo"]


def test_unknown_tool_not_activated(monkeypatch):
    """根本不存在的工具（幻觉名）不许触发任何加载。"""
    monkeypatch.setattr(harness, "get_harness", lambda: _fake_harness(None))
    stub = _Stub()
    args, err = _call(stub, "read_file", {})
    assert args is None
    assert "不存在或未注册" in err
    assert stub.activated == []


def test_plugin_owner_not_auto_loaded(monkeypatch):
    """插件属主不走自动加载：插件没有「按需加载单个」这条路径。"""
    monkeypatch.setattr(harness, "get_harness",
                        lambda: _fake_harness(("plugin", "plug")))
    stub = _Stub(loaded_spec=_spec("plug_tool"))
    args, err = _call(stub, "plug_tool", {"command": "x"})
    assert args is None
    assert "尚未注册" in err
    assert stub.activated == []


def test_activation_adds_nothing_keeps_original_error(monkeypatch):
    """加载了但没拿到工具（技能为空/损坏）→ 保留原错误，不静默放行。"""
    monkeypatch.setattr(harness, "get_harness",
                        lambda: _fake_harness(("skill", "demo")))
    stub = _Stub(added=0)
    args, err = _call(stub, "shell_run", {"command": "echo hi"})
    assert args is None
    assert "尚未注册" in err and 'skill_help("demo")' in err
    assert stub.activated == ["demo"]


def test_activation_exception_is_swallowed(monkeypatch):
    """加载过程抛异常不许把整轮带崩：退回原错误信息。"""
    monkeypatch.setattr(harness, "get_harness",
                        lambda: _fake_harness(("skill", "demo")))
    stub = _Stub(boom=True)
    args, err = _call(stub, "shell_run", {"command": "echo hi"})
    assert args is None
    assert "尚未注册" in err
    assert stub.activated == ["demo"]


def test_auto_loaded_but_bad_args_still_rejected(monkeypatch):
    """自动加载不放松参数校验：缺必填参数照样回填错误给模型。"""
    monkeypatch.setattr(harness, "get_harness",
                        lambda: _fake_harness(("skill", "demo")))
    stub = _Stub(loaded_spec=_spec("shell_run"))
    args, err = _call(stub, "shell_run", {})
    assert args is None
    assert "缺少必填参数" in err
    assert "尚未注册" not in err


def test_real_harness_loads_code_ops(monkeypatch):
    """接真 harness：shell_run 确实不在基础工具里，自动加载后变得可校验。

    这条是整件事的真实前提验证——若哪天 code_ops 被改成常驻，A 类问题自然消失，
    这个用例会失败提醒我们：自动加载逻辑已无对象。
    """
    # 真实 _activate_skill 会写会话技能状态和 tools 流水：测试跑一次就往真实统计里
    # 塞一条假的「激活」，后续观测全被污染。两个落盘副作用在这里掐掉。
    monkeypatch.setattr(agent, "_save_skills_state", lambda *a, **k: None)
    monkeypatch.setattr(agent, "_trace_tools_change", lambda *a, **k: None)
    stub = _Stub(tools=agent.load_local_tools())
    assert find_tool_spec(stub._all_tools, "shell_run") is None, "shell_run 不该在基础工具里"
    stub._activate_skill = lambda name, restored=False: agent.AIAgent._activate_skill(
        stub, name, restored)
    stub._ordered_active_skills = lambda: agent.AIAgent._ordered_active_skills(stub)
    args, err = _call(stub, "shell_run", {"command": "echo hi"})
    assert err is None, err
    assert args == {"command": "echo hi"}
    assert find_tool_spec(stub._all_tools, "shell_run") is not None
    assert "code_ops" in stub._activated_skills
