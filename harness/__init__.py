"""大白 Harness —— 稳定的智能体运行时 + 技能（Skill）/ 插件（Plugin）扩展框架。

架构总览：
- harness/core.py      Harness 运行时核心：工具收集、执行路由、健康状态、热重载
- harness/skills.py    技能系统：skills/<名称>/ 目录即一个技能（清单 + 实现）
- harness/plugins.py   插件系统：plugins/<名称>/ 目录即一个插件（清单 + 实现）
- harness/state.py     启停状态持久化（harness_state.json）

对外极简 API：get_harness() 返回全局唯一 Harness 实例。
agent.py / server.py 通过它把技能与插件的工具、提示词片段纳入「大白」的
对话与工具链路 —— 无需改动核心逻辑即可不断扩展新能力。
"""
from __future__ import annotations

from pathlib import Path

_HARNESS_BASE_DIR = Path(__file__).resolve().parent.parent

_harness = None


def get_harness(base_dir=None):
    """返回全局唯一 Harness 实例（懒加载，避免导入期副作用）。"""
    global _harness
    if _harness is None:
        from .core import Harness
        _harness = Harness(base_dir or _HARNESS_BASE_DIR)
    return _harness


def reset_harness():
    """重置全局实例（测试/插件开发用）。"""
    global _harness
    _harness = None


from .skills import Skill, SkillError          # noqa: E402,F401
from .plugins import Plugin, PluginError       # noqa: E402,F401
from .runtime import AgentRuntime, RunSpan     # noqa: E402,F401
from .tasks import TaskSystem, TaskSystemError  # noqa: E402,F401

__all__ = ["get_harness", "reset_harness", "Skill", "SkillError",
           "Plugin", "PluginError", "AgentRuntime", "RunSpan",
           "TaskSystem", "TaskSystemError"]
