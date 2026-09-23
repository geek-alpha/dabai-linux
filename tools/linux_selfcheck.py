#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""兼容壳：自检脚本已改名为 tools/selfcheck.py。

旧名带 "linux" 有误导 —— Windows 的 dabai.bat 也调它，而且它早就通过
platform_compat 跨平台了。保留这个文件只是为了让已有的 bat / sh / 文档 /
外部脚本引用不至于断掉；新代码请直接调 tools/selfcheck.py。
"""
from __future__ import annotations

import runpy
import sys
from pathlib import Path

TARGET = Path(__file__).resolve().parent / "selfcheck.py"

print("[i] tools/linux_selfcheck.py 已改名，请改用 tools/selfcheck.py", file=sys.stderr)
runpy.run_path(str(TARGET), run_name="__main__")
