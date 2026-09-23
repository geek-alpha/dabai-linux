"""Windows 正规入口链的守卫测试。

三处病灶有个共同点：**只在走正规入口时复现，手动跑 server.py 一切正常** ——
    uvloop 硬编码   缺模块的机器启动即崩（见 test_win_asgi_extras.py）
    launch.py       不插 sys.path，`import attach_text` 炸
    dabai.bat       UTF-8 无 BOM 被 cmd 按代码页逐字节解析，参数错位
这类「入口与直跑不一致」的差异最难查，所以入口链必须由测试锁死，
否则下次重构又会静默退化。
"""
from __future__ import annotations

import ast
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
LAUNCH = ROOT / "deploy" / "windows" / "launch.py"
BAT = ROOT / "dabai.bat"


def _has_cjk(text: str) -> bool:
    return any("\u4e00" <= c <= "\u9fff" for c in text)


# ---------------------------------------------------------------- launch.py


def _call_linenos(tree: ast.AST, func: str) -> list[int]:
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            if isinstance(fn, ast.Attribute) and fn.attr == func:
                out.append(node.lineno)
    return out


def test_launch_inserts_root_before_run_path():
    """runpy.run_path 对普通 .py 文件不插 sys.path，必须在调用前手动插 ROOT。"""
    tree = ast.parse(LAUNCH.read_text(encoding="utf-8"))
    run_path = _call_linenos(tree, "run_path")
    insert = _call_linenos(tree, "insert")
    assert run_path, "launch.py 里找不到 runpy.run_path 调用"
    assert insert, "launch.py 没有 sys.path.insert —— 走 bat 时 import attach_text 会炸"
    assert min(insert) < min(run_path), (
        f"sys.path.insert 在第 {min(insert)} 行，run_path 在第 {min(run_path)} 行："
        "插入必须在启动之前，否则等于没插")


def test_launch_entry_can_import_sibling_module(tmp_path):
    """行为验证：临时造一个和真实布局同构的 mini 项目，真跑一次 launch.py。"""
    (tmp_path / "attach_text.py").write_text('VALUE = "sibling-ok"\n', encoding="utf-8")
    (tmp_path / "server.py").write_text(
        "import attach_text\nprint('OK:', attach_text.VALUE)\n", encoding="utf-8")
    target = tmp_path / "deploy" / "windows"
    target.mkdir(parents=True)
    shutil.copy2(LAUNCH, target / "launch.py")

    env = dict(os.environ, DABAI_SECRETS_FILE=str(tmp_path / "absent.env"))
    proc = subprocess.run(
        [sys.executable, str(target / "launch.py")],
        capture_output=True, text=True, env=env, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    assert "OK: sibling-ok" in proc.stdout, proc.stdout + proc.stderr


def test_launch_check_mode_does_not_start_entry(tmp_path):
    """--check 只报密钥，不该跑入口。"""
    (tmp_path / "server.py").write_text("raise SystemExit('不该被执行')\n", encoding="utf-8")
    target = tmp_path / "deploy" / "windows"
    target.mkdir(parents=True)
    shutil.copy2(LAUNCH, target / "launch.py")

    env = dict(os.environ, DABAI_SECRETS_FILE=str(tmp_path / "absent.env"))
    proc = subprocess.run(
        [sys.executable, str(target / "launch.py"), "--check"],
        capture_output=True, text=True, env=env, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    assert "密钥文件" in proc.stdout
    assert "不该被执行" not in proc.stdout


# ---------------------------------------------------------------- dabai.bat


def test_bat_exists():
    assert BAT.is_file(), f"找不到 {BAT}"


def test_bat_is_not_utf8_chinese():
    """cmd 按当前代码页逐字节读 bat；UTF-8 中文会错位成别的命令。

    实测症状：--check 被截成 heck、REM 注释被当命令执行。
    """
    raw = BAT.read_bytes()
    try:
        as_utf8 = raw.decode("utf-8")
    except UnicodeDecodeError:
        return  # 不是 UTF-8 —— 正是要的状态
    assert not _has_cjk(as_utf8), (
        "dabai.bat 存成了 UTF-8 中文：cmd 按代码页解析会字节错位，"
        "必须存成 GBK（中文 Windows 默认 936）")


def test_bat_decodes_as_gbk():
    """文件必须能被中文 Windows 的默认代码页整份解开。"""
    text = BAT.read_bytes().decode("gbk")
    assert "@echo off" in text.lower()
    assert text.strip()


def test_bat_has_no_chcp():
    """chcp 会在解析阶段切代码页，和文件编码互相打架；去掉它反而稳。"""
    text = BAT.read_bytes().decode("gbk")
    assert "chcp" not in text.lower(), "dabai.bat 又出现 chcp —— 与 GBK 编码冲突"


def test_bat_has_no_bom_and_keeps_crlf():
    raw = BAT.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf"), "bat 不能有 UTF-8 BOM"
    assert raw.count(b"\r\n") > 50, "bat 行尾必须是 CRLF"


def test_bat_command_lines_are_ascii():
    """中文只许出现在 REM 注释和 echo 的输出文本里。

    命令关键字本身（if / set / call / goto / %PY% …）必须是纯 ASCII，
    这样即使换到英文 Windows 的代码页，也不会把中文解成可执行的东西。
    """
    text = BAT.read_bytes().decode("gbk")
    offenders = []
    for lineno, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.upper().startswith(("REM", "ECHO", "@ECHO")):
            continue
        cut = stripped.lower().find("echo")
        head = stripped if cut < 0 else stripped[:cut]
        if any(ord(c) > 127 for c in head):
            offenders.append(f"{lineno}: {stripped}")
    assert not offenders, "命令行部分混入了非 ASCII 字符：\n" + "\n".join(offenders)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
