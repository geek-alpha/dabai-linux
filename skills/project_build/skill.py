"""project_build 技能入口 —— 从零构建大型项目的结构化能力。

实现全部在 project_build_impl.py；本文件只做注册（TOOLS 由 skill.json 声明）。
"""
from __future__ import annotations

import os
import sys

_SKILL_DIR = os.path.dirname(os.path.abspath(__file__))
if _SKILL_DIR not in sys.path:
    sys.path.insert(0, _SKILL_DIR)

import project_build_impl  # noqa: E402

# 显式字面量注册：skill_dev_validate 用 AST 静态提取 HANDLERS 键，
# dict(...) 这种动态写法会被判成「工具没有实现」（实测踩坑），故逐个列出。
HANDLERS = {
    "project_spec_init": project_build_impl.project_spec_init,
    "project_next": project_build_impl.project_next,
    "project_verify": project_build_impl.project_verify,
    "project_matrix": project_build_impl.project_matrix,
    "project_dispatch": project_build_impl.project_dispatch,
}
