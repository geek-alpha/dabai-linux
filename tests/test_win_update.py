#!/usr/bin/env python3
"""更新器的 Windows 语义测试 —— 同一套用例，两种平台都要过。

为什么要这样测：更新器原来只在 Linux 上跑，`import pwd` / `os.geteuid()` / `systemctl`
这些在 Windows 上不是「行为不同」，是直接崩（ImportError / AttributeError / 找不到命令）。
而 Windows 分支一旦写错，代价是**服务被停掉起不来** —— 那台机器上的大白就没了。

做法：把 update.py 按 os.name="nt" 重新加载一份（模块顶层只用它选分支，不碰系统），
再在 Windows 语义下逐条断言。钉住四件事：
  - Windows 上能 import、能拿到平台默认值（没有 pwd/grp 也得活着）
  - 停/起服务落在进程上（taskkill / 计划任务 / 分离启动），不碰 systemd
  - 需要 systemd 的三步（cgroup 判定 / 自脱离 / 属主解析）在 Windows 上安全空转
  - 体检不看 systemd 状态，只看端口与 HTTP —— 否则 Windows 上永远「体检未通过」

真机验证仍不可替代：这里证明的是分支走对，不是 taskkill 在 Windows 上真能杀掉服务。
"""

from __future__ import annotations

import os
import pathlib
import types
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
REL = REPO / "deploy" / "release"

NETSTAT = """
活动连接

  协议  本地地址          外部地址        状态           PID
  TCP    0.0.0.0:8000           0.0.0.0:0              LISTENING       4321
  TCP    127.0.0.1:8000         0.0.0.0:0              LISTENING       4321
  TCP    0.0.0.0:135            0.0.0.0:0              LISTENING       900
  TCP    127.0.0.1:8001         127.0.0.1:51234        ESTABLISHED     4321
"""


def _load_windows_update() -> types.ModuleType:
    src = (REL / "update.py").read_text(encoding="utf-8")
    mod = types.ModuleType("dabai_update_win")
    mod.__file__ = str(REL / "update.py")
    saved_name, saved_path = os.name, pathlib.Path
    os.name = "nt"
    # os.name='nt' 时 pathlib.Path(...) 会分派到 WindowsPath，而 WindowsPath 在
    # 非 Windows 上直接抛 UnsupportedOperation —— 这是测试环境的限制，不是被测代码
    # 的问题（真 Windows 上 WindowsPath 就是本机实现）。钉成 PosixPath 绕过它。
    pathlib.Path = pathlib.PosixPath
    try:
        exec(compile(src, str(REL / "update.py"), "exec"), mod.__dict__)
    finally:
        os.name, pathlib.Path = saved_name, saved_path
    return mod


win = _load_windows_update()


class _Done:
    def __init__(self, rc: int = 0, out: str = "", err: str = ""):
        self.returncode, self.stdout, self.stderr = rc, out, err


class _Resp:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


# ── 能不能活着加载 ───────────────────────────────────────────────────────
def test_windows_module_imports_without_pwd_grp():
    assert win.IS_WINDOWS is True
    assert win.pwd is None and win.grp is None


def test_windows_defaults_are_user_level():
    assert win.DEFAULTS["SERVICE"] == "DabaiServer"
    # 默认安装目录从用户主目录推出来，不写死 Linux 那台机器上的路径
    assert win.DEFAULTS["ROOT"] == str(Path.home() / "dabai")
    assert win.DEFAULTS["STATE"].endswith("dabai-update")
    assert "/var/lib" not in win.DEFAULTS["STATE"]


def test_windows_state_candidates_are_writable_paths():
    cands = win.state_candidates(win.DEFAULTS)
    assert len(cands) == 2
    assert all(str(c).endswith("dabai-update") for c in cands)


def test_windows_conf_and_secrets_live_under_appdata():
    assert "AppData" in str(win.CONF_FILE) or "appdata" in str(win.CONF_FILE).lower()
    assert win.USER_CONF == win.CONF_FILE
    assert len(win.SECRET_FILES) == 1


# ── 找进程 / 停服务 / 起服务 ─────────────────────────────────────────────
def test_win_listen_pids_parses_netstat(monkeypatch):
    monkeypatch.setattr(win.subprocess, "run", lambda *a, **k: _Done(0, NETSTAT))
    assert win._win_listen_pids("8000") == [4321]
    assert win._win_listen_pids("135") == [900]
    assert win._win_listen_pids("9999") == []
    assert win._win_listen_pids("") == []


def test_win_stop_kills_process_tree(monkeypatch):
    calls = []

    def fake_run(cmd, *a, **k):
        calls.append(list(cmd))
        if cmd[0] == "netstat":
            return _Done(0, NETSTAT)
        return _Done(0, f"成功: 已终止 PID {cmd[2]} 的进程")

    monkeypatch.setattr(win.subprocess, "run", fake_run)
    rc, out = win._win_stop_service({"PORT": "8000"})
    assert rc == 0
    kills = [c for c in calls if c[0] == "taskkill"]
    assert kills == [["taskkill", "/PID", "4321", "/T", "/F"]], kills
    assert "4321" in out


def test_win_stop_without_listener_is_success(monkeypatch):
    monkeypatch.setattr(win.subprocess, "run", lambda *a, **k: _Done(0, NETSTAT))
    rc, out = win._win_stop_service({"PORT": "9999"})
    assert rc == 0 and "已停" in out


def test_win_start_prefers_scheduled_task(monkeypatch, tmp_path):
    (tmp_path / "server.py").write_text("", encoding="utf-8")
    calls = []

    def fake_run(cmd, *a, **k):
        calls.append(list(cmd))
        return _Done(0, "成功: 尝试运行")

    def boom(*a, **k):
        raise AssertionError("计划任务已能拉起，不该再自己 Popen")

    monkeypatch.setattr(win.subprocess, "run", fake_run)
    monkeypatch.setattr(win.subprocess, "Popen", boom)
    rc, out = win._win_start_service({"ROOT": str(tmp_path), "ENTRY": "server.py",
                                      "SERVICE": "DabaiServer"})
    assert rc == 0 and "计划任务" in out
    assert calls == [["schtasks", "/run", "/tn", "DabaiServer"]]


def test_win_start_falls_back_to_detached_process(monkeypatch, tmp_path):
    (tmp_path / "server.py").write_text("", encoding="utf-8")
    spawned = {}

    def fake_run(cmd, *a, **k):
        return _Done(1, "", "错误: 找不到计划任务")

    def fake_popen(cmd, **kw):
        spawned["cmd"], spawned["kw"] = cmd, kw
        return object()

    monkeypatch.setattr(win.subprocess, "run", fake_run)
    monkeypatch.setattr(win.subprocess, "Popen", fake_popen)
    rc, out = win._win_start_service({"ROOT": str(tmp_path), "ENTRY": "server.py",
                                      "SERVICE": "DabaiServer"})
    assert rc == 0 and "分离启动" in out
    assert spawned["cmd"][1] == str(tmp_path / "server.py")
    assert spawned["kw"]["cwd"] == str(tmp_path)
    # 必须脱离更新器，否则更新器一退服务就跟着没了
    assert spawned["kw"]["creationflags"] & 0x00000008


def test_win_start_missing_entry_is_error(monkeypatch, tmp_path):
    monkeypatch.setattr(win.subprocess, "run", lambda *a, **k: _Done(1))
    rc, out = win._win_start_service({"ROOT": str(tmp_path), "ENTRY": "server.py",
                                      "SERVICE": ""})
    assert rc == 1 and "找不到入口" in out


def test_svc_routes_to_windows_backend(monkeypatch):
    seen = []
    monkeypatch.setattr(win, "_win_stop_service",
                        lambda cfg: (seen.append(("stop", cfg)) or (0, "ok")))
    monkeypatch.setattr(win, "_win_start_service",
                        lambda cfg: (seen.append(("start", cfg)) or (0, "ok")))
    monkeypatch.setattr(win, "_win_listen_pids", lambda port: [1] if port == "8000" else [])
    cfg = {"PORT": "8000", "SERVICE": "DabaiServer"}
    assert win.svc("stop", "DabaiServer", cfg=cfg)[0] == 0
    assert win.svc("start", "DabaiServer", cfg=cfg)[0] == 0
    assert win.svc("is-active", "DabaiServer", cfg=cfg) == (0, "active")
    assert win.svc("reload", "DabaiServer", cfg=cfg)[0] == 2
    assert [s[0] for s in seen] == ["stop", "start"]
    assert win.svc_active("DabaiServer", cfg) == "active"


# ── 需要 systemd 的三步在 Windows 上安全空转 ─────────────────────────────
def test_cgroup_check_is_false_on_windows():
    # 即便 cgroup 文本里写的正是目标服务，Windows 也不该走脱离流程
    assert win._in_service_cgroup("dabai.service", "0::/system.slice/dabai.service") is False
    assert win._in_service_cgroup("myservice") is False


def test_detach_is_noop_on_windows(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("Windows 上不该调 systemd-run")

    monkeypatch.setattr(win.subprocess, "run", boom)
    assert win.detach_self({"ROOT": "/tmp"}, ["--apply"]) == 0


def test_run_user_for_is_none_on_windows():
    assert win._run_user_for({"ROOT": "/tmp"}) is None


def test_is_root_does_not_use_geteuid_on_windows(monkeypatch):
    def boom():
        raise AssertionError("Windows 没有 os.geteuid")

    monkeypatch.setattr(win.os, "geteuid", boom, raising=False)
    assert win._is_root() is False


# ── 通知与体检 ───────────────────────────────────────────────────────────
def test_desktop_notify_is_false_on_windows():
    assert win._session_buses(1000) == []
    assert win._desktop_notify({"ROOT": "/tmp"}, "标题", "正文") is False


def test_health_skips_systemd_and_uses_port(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("Windows 没有 systemd 状态可查")

    monkeypatch.setattr(win, "svc_active", boom)
    monkeypatch.setattr(win.socket, "create_connection", lambda *a, **k: _Resp())
    monkeypatch.setattr(win.urllib.request, "urlopen", lambda *a, **k: _Resp())
    ok, detail = win.health({"ROOT": "/tmp"}, "DabaiServer", "8000", settle=0.01)
    assert ok, detail
    assert "8000" in detail


def test_health_fails_when_port_dead(monkeypatch):
    def dead(*a, **k):
        raise OSError("connection refused")

    monkeypatch.setattr(win, "svc_active", lambda *a, **k: "active")
    monkeypatch.setattr(win.socket, "create_connection", dead)
    monkeypatch.setattr(win.time, "sleep", lambda *_: None)
    # 时钟必须会走：卡在同一个值上，health 的探活循环就永远不退出（测试会挂死）
    ticks = iter([0.0, 0.0, 100.0])
    monkeypatch.setattr(win.time, "time", lambda: next(ticks, 100.0))
    ok, detail = win.health({"ROOT": "/tmp"}, "DabaiServer", "8000", settle=0.0)
    assert not ok and "8000" in detail
