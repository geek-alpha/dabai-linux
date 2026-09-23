# -*- coding: utf-8 -*-
"""Windows 对等性守卫：长跑引擎里不许出现只在 POSIX 上成立的写法。

下面每个模式在 Windows 上都是硬失败（ImportError / FileNotFoundError /
AttributeError），而且失败点在引擎内部——计划任务把进程拉起来后一轮都跑不成，
journal 里只剩一行 traceback。静态守卫比等真机报错便宜，也比"记得别写"可靠。
"""
import importlib.util
import os
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
LONGRUN_DIR = ROOT / "tools" / "longrun"
RUNNER = LONGRUN_DIR / "runner.py"

# (正则, 该换成什么)
FORBIDDEN = [
    (r"^\s*import fcntl", "fcntl 在 Windows 上不存在 → platform_compat.lock_file"),
    (r"start_new_session\s*=", "→ platform_compat.spawn_kwargs(new_group=True)"),
    (r"os\.killpg|os\.getpgid", "→ platform_compat.terminate_tree"),
    (r"""Path\(["']/proc["']\)""", "→ platform_compat.process_tree()"),
    (r"venv/bin/python", "解释器路径随平台变 → 用 PY / PY_CMD"),
]


def _sources():
    return sorted(LONGRUN_DIR.glob("*.py"))


@pytest.mark.parametrize("pattern,why", FORBIDDEN)
def test_no_posix_only_pattern(pattern, why):
    for path in _sources():
        src = path.read_text(encoding="utf-8")
        hits = [f"{path.name}:{i} {ln.strip()}"
                for i, ln in enumerate(src.splitlines(), 1) if re.search(pattern, ln)]
        assert not hits, f"出现 POSIX-only 写法（{why}）：{hits}"


def _load_runner():
    spec = importlib.util.spec_from_file_location("runner_under_test", RUNNER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_py_points_to_real_interpreter():
    """PY 必须按平台解析到真实存在的解释器（Windows 是 Scripts\\python.exe）。"""
    runner = _load_runner()
    assert runner.PY.is_file(), f"PY 指向的解释器不存在：{runner.PY}"
    assert runner.PY_CMD == ("venv/Scripts/python.exe" if os.name == "nt" else "venv/bin/python")


def test_prompt_paths_use_platform_interpreter():
    """给模型看的落盘命令必须是它那台机器上能照抄的路径。"""
    runner = _load_runner()
    prompt = runner.build_prompt({"id": "demo", "title": "演示", "next": "做一件事"},
                                 {"cycle": 1})
    assert str(runner.PY) in prompt
    # 不许出现「按 POSIX 惯例拼死路径」的写法（Windows 上那个文件根本不存在）
    assert "{base}/venv/bin/python" not in prompt


# ---------- status_view：Windows 侧的服务状态与控制 ----------

import importlib  # noqa: E402
import sys  # noqa: E402
import types  # noqa: E402

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _sv():
    return importlib.import_module("tools.longrun.status_view")


class _FakeRun:
    def __init__(self, out="", rc=0):
        self.stdout, self.stderr, self.returncode = out, "", rc


def test_win_task_state_maps_states(monkeypatch):
    """State 枚举名 → systemd 词表；不认识的值一律 unknown，不许猜成「在跑」。"""
    sv = _sv()
    for raw, want in {"Running": "active", "Ready": "inactive", "Disabled": "inactive",
                      "就绪": "unknown", "": "unknown"}.items():
        monkeypatch.setattr(sv.subprocess, "run", lambda *a, _o=raw, **k: _FakeRun(_o))
        assert sv._win_task_state() == want, f"{raw!r} 应映射成 {want}"


def test_win_task_state_survives_missing_powershell(monkeypatch):
    sv = _sv()
    monkeypatch.setattr(sv.subprocess, "run",
                        lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError("powershell")))
    assert sv._win_task_state() == "unknown"


def test_windows_service_action_avoids_systemctl(monkeypatch):
    """Windows 上启停必须走计划任务：systemctl 那条路在 Windows 上必然失败。"""
    sv = _sv()
    calls = []

    def _rec(name):
        def f(*a, **k):
            calls.append(name)
            return True, f"（{name}）"
        return f

    fake = types.SimpleNamespace(stop_service=_rec("stop"), start_service=_rec("start"),
                                 runner_pids=lambda: [])
    monkeypatch.setattr(sv.pc, "IS_WINDOWS", True)
    monkeypatch.setattr(sv, "_wd", lambda: fake)
    monkeypatch.setattr(sv, "_win_task_state", lambda: "inactive")

    def _no_systemctl(*a, **k):
        raise AssertionError("Windows 上不该调 systemctl")
    monkeypatch.setattr(sv, "_systemctl", _no_systemctl)

    ok, msg = sv.service_action("stop")
    assert ok and "已停止" in msg and calls == ["stop"]
    ok, msg = sv.service_action("start")
    assert ok and calls == ["stop", "start"]


def test_unit_state_never_returns_error_text(monkeypatch):
    """systemctl 报错时 stdout 是错误文本——它绝不能被当成状态用。

    实测本机 systemctl --user 连不上 bus 时，`is-active` 的输出是
    「Failed to connect to user scope bus…」，旧写法把它原样当成了状态值。
    """
    sv = _sv()
    monkeypatch.setattr(sv.pc, "IS_WINDOWS", False)
    monkeypatch.setattr(sv, "_systemctl",
                        lambda *a, **k: (127, "Failed to connect to user scope bus"))
    assert sv.unit_state(refresh=True) == "unknown"
    monkeypatch.setattr(sv, "_systemctl", lambda *a, **k: (0, "inactive\n"))
    assert sv.unit_state(refresh=True) == "inactive"
