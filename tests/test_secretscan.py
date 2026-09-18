#!/usr/bin/env python3
"""密钥扫描器的对抗测试：既拦得住真密钥，也不天天喊狼来了。

误报的代价不是「多看一眼」，而是钩子被 --no-verify 绕过 —— 那就等于没有。
所以这里正反两面都测：公开常量（base64 字母表）必须放过，真密钥必须抓。

本文件里的假密钥一律用字符串拼接构造，不写字面量 —— 否则 pre-commit 钩子
扫到自己这个测试文件，就得反过来给它加豁免，等于自己污染自己。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


scan = _load("dabai_secretscan_mod", REPO / "deploy" / "gitguard" / "secretscan.py")


def test_base64_alphabet_is_not_a_secret():
    """Draco/emscripten 生成物里的 base64 字母表被 [keyStr] 词根报过，是误报。

    出处：web/vendor/three/examples/jsm/libs/draco/gltf/draco_decoder.js:21
    """
    line = 'keyStr="' + scan._ALNUM + '+/="'
    assert scan.scan_text(line) == []


def test_urlsafe_alphabet_is_not_a_secret():
    line = 'CHARSET = "' + scan._ALNUM + '-_"'
    assert scan.scan_text(line) == []


def test_alphabet_with_random_prefix_is_still_caught():
    """只做整串精确匹配：字母表前面接上随机串，就不能再当常量放过。"""
    line = 'keyStr="Zx9' + scan._ALNUM + '+/="'
    assert scan.scan_text(line), "前缀一改就该重新判，别把前缀匹配也放行了"


def test_real_openai_style_key_caught():
    line = 'api_key = "sk-' + 'a1B2c3D4e5F6g7H8i9J0k1L2' + '"'
    assert scan.scan_text(line)


def test_real_self_built_key_caught():
    """无固定前缀的自建 key（32 位 hex）靠高熵判据抓，别被常量表误伤。"""
    key = "9f3a1c7e5b2d8046" + "af1e93c7b4d2068e"
    assert scan.scan_text('TIANAPI_QUIZ_KEY = "' + key + '"')


def test_allowlist_marker_respected():
    line = 'STORAGE_KEY = "mixamo_library_progress_v2"  // allowlist secret'
    assert scan.scan_text(line) == []


def test_vendored_draco_decoder_scans_clean():
    """真仓库里那个触发过误报的文件，现在必须干净。"""
    p = REPO / "web" / "vendor" / "three" / "examples" / "jsm" / "libs" / "draco" / "gltf" / "draco_decoder.js"
    if not p.exists():
        import pytest

        pytest.skip("web/vendor 未入仓")
    assert scan.scan_file(str(p)) == []
