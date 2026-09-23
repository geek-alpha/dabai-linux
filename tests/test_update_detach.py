#!/usr/bin/env python3
"""更新器自脱离（self-detach）的对抗测试。

背景：update.py 被定时器（dabai-update.timer）拉起时跑在 dabai.service 的 cgroup 里，
而 apply 流程第 ⑤ 步是 systemctl stop 这个服务 —— KillMode=control-group 会把
更新器自己一起杀掉：文件没换、服务停着起不来，update.log 里只剩一行「⑤ 停机」。
修法是动手前用 systemd-run 把自己挪进独立 unit。

这里钉住三件事：
  - 判定「自己在不在目标服务 cgroup 里」不能靠猜（cgroup v1/v2 写法不同）
  - 脱离失败必须报成失败、不许继续往下走（宁可不更新，也不要把服务停死）
  - 脱离这一步必须发生在停机之前（顺序错了等于没修）
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
REL = REPO / "deploy" / "release"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


update = _load("dabai_update_detach_mod", REL / "update.py")


class _Done:
    def __init__(self, rc: int = 0, out: str = "", err: str = ""):
        self.returncode, self.stdout, self.stderr = rc, out, err


# ── 判定：自己在不在目标服务的 cgroup 里 ─────────────────────────────────
def test_detects_own_service_cgroup():
    assert update._in_service_cgroup("dabai.service", "0::/system.slice/dabai.service")
    assert update._in_service_cgroup("dabai", "0::/system.slice/dabai.service")
    assert update._in_service_cgroup("myservice", "0::/system.slice/myservice")
    # cgroup v1 的写法（多段冒号）
    assert update._in_service_cgroup("dabai.service", "1:name=systemd:/system.slice/dabai.service")


def test_other_cgroups_are_not_matched():
    assert not update._in_service_cgroup("dabai.service", "")
    assert not update._in_service_cgroup(
        "dabai.service", "0::/user.slice/user-1000.slice/session-1.scope")
    assert not update._in_service_cgroup("", "0::/system.slice/dabai.service")
    # 名字是前缀关系的另一个服务不能被误判 —— 误判的代价是多起一个 unit，不会更坏，
    # 但漏判的代价是把自己杀掉，所以宁可这边严一点
    assert not update._in_service_cgroup("dabai", "0::/system.slice/dabai-worker.service")
    assert not update._in_service_cgroup("dabai", "0::/system.slice/dabai.service/child")


def test_missing_proc_is_not_a_match(monkeypatch):
    def boom(*a, **k):
        raise OSError("/proc 读不到")
    monkeypatch.setattr(update.Path, "read_text", boom, raising=False)
    assert update._in_service_cgroup("dabai.service") is False


# ── 脱离动作本身 ─────────────────────────────────────────────────────────
def test_detach_moves_self_into_transient_unit(monkeypatch, tmp_path):
    seen = {}

    def fake_run(cmd, **k):
        seen["cmd"] = list(cmd)
        return _Done(0)

    monkeypatch.setattr(update.subprocess, "run", fake_run)
    monkeypatch.setattr(update, "_token_in_file", lambda: False)
    monkeypatch.setenv("GITHUB_TOKEN", "tok-abc")
    rc = update.detach_self({"STATE": str(tmp_path)}, ["--apply", "--root", "/tmp/x"])
    assert rc == 0
    cmd = seen["cmd"]
    assert cmd[0] in ("systemd-run", "sudo")           # 非 root 时前面挂 sudo -n
    joined = " ".join(cmd)
    assert "--unit" in joined
    assert "--detached" in joined                       # 防二次脱离导致递归
    assert "--apply" in joined and "/tmp/x" in joined   # 原参数原样带过去
    assert "tok-abc" in joined                          # 文件里没有，只能靠 setenv


def test_token_stays_out_of_argv_when_a_file_has_it(monkeypatch, tmp_path):
    seen = {}

    def fake_run(cmd, **k):
        seen["cmd"] = list(cmd)
        return _Done(0)

    monkeypatch.setattr(update.subprocess, "run", fake_run)
    monkeypatch.setattr(update, "_token_in_file", lambda: True)
    monkeypatch.setenv("GITHUB_TOKEN", "tok-abc")
    assert update.detach_self({"STATE": str(tmp_path)}, ["--apply"]) == 0
    assert "tok-abc" not in " ".join(seen["cmd"])


def test_no_systemd_run_refuses_to_update(monkeypatch, tmp_path):
    def boom(*a, **k):
        raise FileNotFoundError("systemd-run")
    monkeypatch.setattr(update.subprocess, "run", boom)
    # 关键：返回非 0 —— 宁可不更新，也不要在服务 cgroup 里跑停机把自己弄死
    assert update.detach_self({"STATE": str(tmp_path)}, ["--apply"]) == 1


def test_detach_failure_is_reported_not_ignored(monkeypatch, tmp_path):
    def fake_run(cmd, **k):
        return _Done(1, "", "Failed to connect to bus")
    monkeypatch.setattr(update.subprocess, "run", fake_run)
    assert update.detach_self({"STATE": str(tmp_path)}, ["--apply"]) == 1


def test_detach_timeout_is_reported(monkeypatch, tmp_path):
    def fake_run(cmd, **k):
        raise update.subprocess.TimeoutExpired(cmd, 60)
    monkeypatch.setattr(update.subprocess, "run", fake_run)
    assert update.detach_self({"STATE": str(tmp_path)}, ["--apply"]) == 1


# ── 顺序：脱离必须在停机之前 ─────────────────────────────────────────────
def test_every_service_stop_is_guarded_by_detach_check():
    src = (REL / "update.py").read_text(encoding="utf-8").splitlines()
    stops = [i for i, line in enumerate(src) if 'svc("stop"' in line]
    detaches = [i for i, line in enumerate(src) if '_in_service_cgroup(cfg["' in line]
    assert stops, "一个停机点都没找到，这条测试自己失效了"
    for s in stops:
        assert any(d < s for d in detaches), (
            f"第 {s + 1} 行的停机没有前置的脱离检查 —— 原地跑会把自己杀掉")


# ── 自脱离出来的 unit 不能以 root 写盘 ──────────────────────────────────
def test_detached_unit_is_pinned_to_repo_owner(monkeypatch, tmp_path):
    """systemd-run 默认落系统级 unit、User=root。

    不钉住的话，升权就从「起一个平级 unit」悄悄变成「整套更新以 root 跑」：
    root 写出来的 staging/backups 属主是 root，下一次普通用户跑的更新器再也写不进
    同一份暂存目录 —— 报出来却是「下载失败」。这就是 orangepi 那份 root:root 的来源。
    """
    seen = {}

    def fake_run(cmd, **k):
        seen["cmd"] = list(cmd)
        return _Done(0)

    monkeypatch.setattr(update.subprocess, "run", fake_run)
    monkeypatch.setattr(update, "_token_in_file", lambda: True)
    monkeypatch.setattr(update, "_run_user_for", lambda cfg: ("wxf", "wxf"))
    assert update.detach_self({"STATE": str(tmp_path)}, ["--apply"]) == 0
    joined = " ".join(seen["cmd"])
    assert "--property=User=wxf" in joined
    assert "--property=Group=wxf" in joined


def test_run_user_comes_from_install_dir_owner(tmp_path):
    import grp as _grp
    import pwd as _pwd

    st = tmp_path.stat()
    assert update._run_user_for({"ROOT": str(tmp_path)}) == (
        _pwd.getpwuid(st.st_uid).pw_name, _grp.getgrgid(st.st_gid).gr_name)
    assert update._run_user_for({"ROOT": str(tmp_path / "不存在")}) is None


def test_detach_still_runs_when_owner_unknown(monkeypatch, tmp_path):
    """解析不出属主时维持旧行为 —— 但不能因此变成静默：调用方记了一条日志。"""
    seen = {}
    logs = []

    def fake_run(cmd, **k):
        seen["cmd"] = list(cmd)
        return _Done(0)

    monkeypatch.setattr(update.subprocess, "run", fake_run)
    monkeypatch.setattr(update, "_token_in_file", lambda: True)
    monkeypatch.setattr(update, "_run_user_for", lambda cfg: None)
    monkeypatch.setattr(update, "log_line", lambda cfg, msg: logs.append(msg))
    assert update.detach_self({"STATE": str(tmp_path)}, ["--apply"]) == 0
    assert "--property=User=" not in " ".join(seen["cmd"])
    assert any("属主" in m for m in logs)
