# -*- coding: utf-8 -*-
"""长跑条目的「清除」契约：清掉的是显示，不是数据；正在跑的绝不许藏。

背景：长跑引擎是只读合成条目（server.py:2589），orchestrator 的 clear_finished()
够不着它 —— 引擎早停了，条目却永远挂在任务中心，用户看到的就是「已停止却清除
不了」。修法是把「这一轮我看过了」记进 dismissed.json，而不是删运行数据。
"""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE / "tools" / "longrun"))

import status_view as sv  # noqa: E402


def _load(monkeypatch, tmp_path, cycle=72, running=False, dismissed=None):
    """把模块的取数面全部打桩：状态、单元、进程、清除标记。"""
    monkeypatch.setattr(sv, "DISMISSED", tmp_path / "dismissed.json")
    monkeypatch.setattr(sv, "read_state", lambda: {"cycle": cycle})
    monkeypatch.setattr(sv, "engine_pid", lambda: (4242 if running else None))
    monkeypatch.setattr(sv, "_proc_alive", lambda pid: bool(running))
    monkeypatch.setattr(sv, "unit_state", lambda refresh=False: ("active" if running else "inactive"))
    # 桩要从被打桩的取数面取 cycle，不能闭包捕获 _load 的参数 —— 否则后面改
    # read_state 时假快照不跟着变，测的就是桩自己而不是实现。
    monkeypatch.setattr(sv, "_snapshot",
                        lambda full: {"id": sv.TASK_ID,
                                      "status": ("running" if sv.engine_running() else "cancelled"),
                                      "extra": {"cycle": int(sv.read_state().get("cycle") or 0)}})
    if dismissed is not None:
        (tmp_path / "dismissed.json").write_text(
            json.dumps({"cycle": dismissed, "at": 0}), encoding="utf-8")


def test_stopped_entry_clears_then_comes_back_on_new_cycle(monkeypatch, tmp_path):
    _load(monkeypatch, tmp_path, cycle=72)
    assert sv.snapshot() is not None, "清除前条目应该在"
    assert sv.dismiss() is True
    assert sv.snapshot() is None, "清除后引擎仍停着，条目应该消失"
    monkeypatch.setattr(sv, "read_state", lambda: {"cycle": 73})
    assert sv.snapshot() is not None, "引擎跑出新轮次，条目必须自动回来"


def test_running_engine_can_never_be_dismissed(monkeypatch, tmp_path):
    _load(monkeypatch, tmp_path, cycle=72, running=True)
    assert sv.dismiss() is False, "引擎在跑时清除必须被拒绝"
    assert not (tmp_path / "dismissed.json").exists(), "拒绝时不许留下任何标记"
    assert sv.snapshot() is not None


def test_never_dismissed_cycle_zero_is_not_a_hit(monkeypatch, tmp_path):
    """cycle=0 是引擎没跑过的默认值 —— 哨兵必须用 None，否则条目永久消失。"""
    _load(monkeypatch, tmp_path, cycle=0)
    assert sv.read_dismissed()["cycle"] is None
    assert sv.snapshot() is not None


def test_dismissed_file_only_stores_cycle_marker(monkeypatch, tmp_path):
    _load(monkeypatch, tmp_path, cycle=72)
    sv.dismiss()
    data = json.loads((tmp_path / "dismissed.json").read_text(encoding="utf-8"))
    assert data["cycle"] == 72 and "at" in data
    assert set(data) == {"cycle", "at"}, "标记文件只该有这两项，不许夹带运行数据"


@pytest.mark.parametrize("broken", ["not-json", "[]", '{"cycle": "x"}'])
def test_broken_marker_falls_back_to_not_dismissed(monkeypatch, tmp_path, broken):
    """标记文件损坏时按「没清除过」处理：宁可多显示一条，不能把条目吞掉。"""
    _load(monkeypatch, tmp_path, cycle=72)
    (tmp_path / "dismissed.json").write_text(broken, encoding="utf-8")
    assert sv.read_dismissed()["cycle"] is None
    assert sv.snapshot() is not None


def test_unreadable_unit_state_refuses_dismiss(monkeypatch, tmp_path):
    """单元状态取不到 = 「未检查」，不是「没在跑」——必须拒绝清除。

    实测本机 systemctl --user 报 bus 错误（单元是系统级的），那时若把错误文本
    当成非 active，正在跑的引擎会被一清就从列表里藏掉。
    """
    _load(monkeypatch, tmp_path, cycle=72)
    monkeypatch.setattr(sv, "unit_state",
                        lambda refresh=False: "Failed to connect to user scope bus")
    assert sv.engine_running() is True, "判据坏掉时要算「可能在跑」"
    assert sv.dismiss() is False
    assert sv.snapshot() is not None


def test_snapshot_reads_failure_still_returns_error_card(monkeypatch, tmp_path):
    """取数炸了要出「读取失败」卡片 —— 别让清除逻辑把它吞成 None。"""
    _load(monkeypatch, tmp_path, cycle=72, dismissed=72)
    monkeypatch.setattr(sv, "_snapshot", lambda full: (_ for _ in ()).throw(RuntimeError("boom")))
    snap = sv.snapshot()
    assert snap is not None and snap["status"] == "error"
