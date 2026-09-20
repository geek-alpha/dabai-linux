# -*- coding: utf-8 -*-
"""pytest 会话级设置：临时根目录跟着可用空间走，不写死在 /tmp。

为什么：本机 /tmp 是 874M 的 tmpfs，而发布/打包类用例单次会话就要写 80M+
中间产物（实测跑十几次后 ENOSPC）。ENOSPC 会伪装成「测试莫名失败」——
失败原因跟被测代码毫无关系，这是最贵的一类假信号。主盘还有 28G。

为什么不用 --basetemp 写进 pyproject：那是绝对路径，Windows 上没有 /home，
而 pytest 建 basetemp 用的是不带 parents 的 mkdir，会直接 FileNotFoundError。
这里改成「先看 /tmp 剩余空间，不够才换」——Windows 上 /tmp 本来就够，逻辑自动跳过。

为什么设 PYTEST_DEBUG_TEMPROOT 而不是 config.option.basetemp：
tmpdir 插件在自己的 pytest_configure 里就调用 TempPathFactory.from_config 把
option.basetemp 读进 _given_basetemp，conftest 的 pytest_configure 更晚，改 option
已经无效；而 PYTEST_DEBUG_TEMPROOT 是在 getbasetemp() 时才读的，改得动。
"""

import os
import shutil
import tempfile
from pathlib import Path

_MIN_FREE_BYTES = 2 * 1024 ** 3
_SUBDIR = "dabai-pytest"


def _roomier_root():
    """当前临时目录够大就返回 None（不动），否则返回一个够大的备用根。"""
    try:
        if shutil.disk_usage(tempfile.gettempdir()).free >= _MIN_FREE_BYTES:
            return None
    except OSError:
        return None

    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(Path.home(), ".cache")
    cand = Path(base) / _SUBDIR
    try:
        # pytest 建目录不带 parents，这里先把父级补齐
        cand.mkdir(parents=True, exist_ok=True)
        if shutil.disk_usage(cand).free < _MIN_FREE_BYTES:
            return None
    except OSError:
        return None
    return cand


def pytest_configure(config):
    root = _roomier_root()
    if root is None:
        return
    os.environ["PYTEST_DEBUG_TEMPROOT"] = str(root)
    # 让测试代码里直接调 tempfile 的路径也落在同一块盘上
    os.environ.setdefault("TMPDIR", str(root))
