# -*- coding: utf-8 -*-
"""子智能体工具表必须绕过渐进披露，否则派出去的 worker 只有 skill_help。

背景（实测，第 10 轮自我迭代循环整轮无效的直接原因）：
`load_local_tools()` 在渐进披露开启时只产出 1 个工具（skill_help），而 skill_help 的
动态注册只作用于主智能体 —— 子智能体的工具表在请求时就冻结了，读说明书也补不回来。
定时任务《自我迭代循环》的 profile 是空串 → `_tool_defs(None)` 直接返回这份残表，
worker 汇报「我手里没有能跑命令的工具」，一条命令都跑不了。

判据（可推翻）：把 SubAgentManager._tools_base 里补齐技能 schema 的那段去掉，
test_基础工具表_不是只有skill_help 与 test_空档案_也拿得到shell_run 立刻变红。
"""
import pathlib
import sys

BASE = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

import sub_agents  # noqa: E402


def _names(tools) -> set:
    return {str((t.get("function") or {}).get("name") or "") for t in tools}


def test_基础工具表_不是只有skill_help():
    """残表判据：工具表只剩 skill_help 时，任何派活都是死路。"""
    names = _names(sub_agents.SubAgentManager()._tools_base())
    assert len(names) > 20, f"工具表疑似被渐进披露掏空：{sorted(names)[:10]}"
    assert "shell_run" in names, "code_ops 是基本生存能力，必须在内"


def test_空档案_也拿得到shell_run():
    """profile 为空（留空=全能力通用执行者）时不许退化成残表。

    定时任务 sched-372bc7ac51（自我迭代循环）就是 profile="" 这条路径。
    """
    m = sub_agents.SubAgentManager()
    names = _names(m._tool_defs(None))
    assert "shell_run" in names and "code_edit" in names


def test_不泄漏递归下发口子():
    """补齐的技能 schema 里含 sub_agent_* 自身，必须滤掉，防无限递归派活。"""
    names = _names(sub_agents.SubAgentManager()._tools_base())
    leak = [n for n in names if n.startswith("sub_agent_")]
    assert leak == [], f"子智能体拿到了递归下发工具：{leak}"
