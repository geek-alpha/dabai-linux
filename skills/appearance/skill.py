"""形象与交互（appearance）—— 外观/声音/界面/3D资产，五合一。

合并自原 5 个技能：
- appearance（3D 造型与场景：查看/切换角色模型与背景）
- voice（嗓音：音色/语速/合成引擎）
- interface（界面模式：应用模式切换 / 屏幕 Toast）
- 3d_assets（PMX/PMD 转 VRM + Mixamo 动作库：下载/管理/优化情绪映射）

工具名全部保持原样，只归并目录。
"""
from __future__ import annotations

import os
import sys

_SKILL_DIR = os.path.dirname(os.path.abspath(__file__))
if _SKILL_DIR not in sys.path:
    sys.path.insert(0, _SKILL_DIR)

import appearance_impl  # noqa: E402
import voice_impl  # noqa: E402
import interface_impl  # noqa: E402
import pmx_impl  # noqa: E402
import mixamo_impl  # noqa: E402

HANDLERS = {
    "get_available_models": appearance_impl.available_models,
    "get_available_backgrounds": appearance_impl.available_backgrounds,
    "switch_character_model": appearance_impl.switch_model,
    "switch_background_scene": appearance_impl.switch_bg,
    "switch_tts_settings": voice_impl.switch_tts,
    "switch_app_mode": interface_impl.switch_mode,
    "show_screen_toast": interface_impl.show_toast,
    "pmx_to_vrm": pmx_impl.convert_pmx,
    "anim_status": mixamo_impl.anim_status,
    "anim_start": mixamo_impl.anim_start,
    "anim_stop": mixamo_impl.anim_stop,
    "anim_check_login": mixamo_impl.anim_check_login,
    "anim_save_cookies": mixamo_impl.anim_save_cookies,
    "anim_download": mixamo_impl.anim_download,
    "anim_batch": mixamo_impl.anim_batch,
    "anim_library": mixamo_impl.anim_library,
    "anim_optimize": mixamo_impl.anim_optimize,
}
