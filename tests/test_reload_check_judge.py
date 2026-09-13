#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""reload_check 的判据契约：优先用守护落盘的「已加载快照」，缺失才退回进程启动时间。

背景（2026-09-14）：core_autorestart=true 时重启走 os.execv 自替换，PID 与
/proc/<pid>/stat 的 starttime 都冻结在进程创建时刻（实测 19284→19284、
ticks 1214348→1214348）。于是「文件 mtime 晚于进程启动时间」这个判据在每次
自动重启后依然成立——工具永远报「未生效」，照着它去重启是白折腾。

契约（四条）：
  1. 快照相符 → judge=loaded_state、rc=0，即使文件 mtime 远晚于进程启动时间
  2. 快照陈旧 → 点名该文件，rc=1
  3. 快照缺失/损坏 → 退回 proc_start 判据并标注该判据会误报，不许抛异常
  4. 探测不到运行中进程 → rc=2，绝不能打印 [OK]

时序构造：_proc_start / _autorestart_enabled / _restart_check_report 全部替身化，
不碰真实进程，也不真跑重启体检脚本。
"""
import importlib.util
import json
import os
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parents[1]

PROC_START = 1_700_000_000.0
# 远晚于进程启动时间：模拟 execv 冻结 starttime 之后，「文件比进程新」恒成立的现场
FILE_MTIME = 1_800_000_000.0


def _load_reload_check():
    """tools/ 不是包，按文件路径加载。每次调用都拿一份干净模块，避免用例互相污染。"""
    path = BASE / "tools" / "reload_check.py"
    spec = importlib.util.spec_from_file_location("reload_check_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _sig(p: Path) -> list:
    st = p.stat()
    return [st.st_mtime_ns, st.st_size]


@pytest.fixture
def env(tmp_path, monkeypatch):
    mod = _load_reload_check()
    (tmp_path / "data").mkdir()
    agent = tmp_path / "agent.py"
    agent.write_text("x = 1\n", encoding="utf-8")
    os.utime(agent, (FILE_MTIME, FILE_MTIME))

    monkeypatch.setattr(mod, "BASE", tmp_path)
    monkeypatch.setattr(mod, "LOADED_STATE", tmp_path / "data" / "hot_reload_state.json")
    monkeypatch.setattr(mod, "_proc_start", lambda: (4321, PROC_START))
    monkeypatch.setattr(mod, "_autorestart_enabled", lambda: True)
    monkeypatch.setattr(mod, "_restart_check_report", lambda: "")
    return mod, tmp_path, agent


def _write_state(mod, core=None, at=PROC_START + 60):
    mod.LOADED_STATE.parent.mkdir(parents=True, exist_ok=True)
    mod.LOADED_STATE.write_text(
        json.dumps({"pid": 4321, "at": at, "core": core or {}, "ext": {}}),
        encoding="utf-8")


def test_snapshot_match_reports_ok_despite_stale_proc_start(env, capsys):
    """核心回归：文件 mtime 晚于进程启动时间，但快照相符 → 必须报 OK。"""
    mod, _, agent = env
    _write_state(mod, {str(agent): _sig(agent)})

    assert mod.main() == 0
    out = capsys.readouterr().out
    assert "判据：已加载快照" in out
    assert "[OK]" in out


def test_snapshot_mismatch_names_the_file(env, capsys):
    mod, _, agent = env
    _write_state(mod, {str(agent): [1, 1]})

    assert mod.main() == 1
    out = capsys.readouterr().out
    assert "agent.py" in out
    assert "已加载快照" in out


def test_missing_snapshot_falls_back_to_proc_start(env, capsys):
    mod, _, _ = env

    assert mod.main() == 1
    out = capsys.readouterr().out
    assert "判据：进程启动时间" in out
    assert "误报" in out


def test_proc_start_fallback_quiet_when_file_older(env):
    """无快照时旧判据仍可用：文件早于进程启动 = 已加载。"""
    mod, _, agent = env
    old = PROC_START - 3600
    os.utime(agent, (old, old))

    assert mod.main() == 0


def test_corrupt_snapshot_treated_as_missing(env, capsys):
    mod, _, _ = env
    mod.LOADED_STATE.write_text("{ not json", encoding="utf-8")

    assert mod._loaded_state() == {}
    assert mod.main() == 1
    assert "判据：进程启动时间" in capsys.readouterr().out


def test_unknown_proc_is_not_ok(env, monkeypatch, capsys):
    """探测失败 ≠ 已生效：必须 rc=2，不许伪装成 [OK]。"""
    mod, _, _ = env
    monkeypatch.setattr(mod, "_proc_start", lambda: (None, None))

    assert mod.main() == 2
    # 提示语本身含「这不是 [OK]」字样，所以按行首判——只要没有任何一行是结论 [OK]
    assert not [ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("[OK]")]


def test_snapshot_from_other_process_is_not_trusted(env, capsys):
    """别人的快照不能当权威：pid 不符 → 告警并降级判据。

    实测 2026-09-14 06:40：全套测试把夹具快照 {"core": {"/fake/core_x.py": [1, 10]}}
    写进生产 data/hot_reload_state.json，reload_check 随即报「44 个核心文件未生效」——
    判据被污染时所有结论都是假的，哪怕方向看起来对。
    """
    mod, _, agent = env
    _write_state(mod, {str(agent): _sig(agent)})
    mod.LOADED_STATE.write_text(
        json.dumps({"pid": 999999, "at": PROC_START, "core": {str(agent): _sig(agent)}, "ext": {}}),
        encoding="utf-8")

    assert mod.main() == 1
    out = capsys.readouterr().out
    assert "≠ 运行中进程 pid" in out
    assert "判据：已加载快照" not in out


def test_snapshot_without_pid_field_is_still_used(env, capsys):
    """快照缺 pid（旧格式）不算污染——一律降级会白白丢掉唯一可信的判据。"""
    mod, _, agent = env
    mod.LOADED_STATE.parent.mkdir(parents=True, exist_ok=True)
    mod.LOADED_STATE.write_text(
        json.dumps({"at": PROC_START, "core": {str(agent): _sig(agent)}, "ext": {}}),
        encoding="utf-8")

    assert mod.main() == 0
    assert "判据：已加载快照" in capsys.readouterr().out
