"""代码工程（code_ops）—— 检索/分析/修改/验证 + 工作区切换 + GitHub 协作，三合一。

合并自原 3 个技能：
- code_ops（代码工程：检索/分析/修改/验证/自审，含 shell/sys_search/worktree 合并）
- workspace_switch（工作区切换：与前端同一套 /api/workspace* 接口）
- github（GitHub 协作：PR 审查 / issue 修复，纯提示词技能，无工具）

工具名全部保持原样，只归并目录。
"""
from __future__ import annotations

import os
import sys

_SKILL_DIR = os.path.dirname(os.path.abspath(__file__))
if _SKILL_DIR not in sys.path:
    sys.path.insert(0, _SKILL_DIR)

import code_ops_impl  # noqa: E402
import workspace_impl  # noqa: E402

HANDLERS = {}
HANDLERS.update(code_ops_impl.HANDLERS)
HANDLERS.update(workspace_impl.HANDLERS)