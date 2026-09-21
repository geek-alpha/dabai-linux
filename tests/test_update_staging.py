#!/usr/bin/env python3
"""暂存目录的权限坑：能建 ≠ 能写，清不掉 ≠ 没这事。

两个症状都来自 2026-09-20 orangepi 的 v1.1.8 升级失败（apply rc=1）：

    ✘ 下载失败（已尝试 3 次）：[Errno 13] Permission denied:
      "/var/lib/dabai-update/staging/dabai-1.1.8.tar.gz.part"

/staging 属 root:root、finisher 以 uid=1000 跑，写不进去。旧代码两处把这件事藏起来：
state_dir 只探父目录（父目录确实可写，探测通过），run() 里 rmtree(ignore_errors=True)
又把「清不掉」吞掉 —— 于是六秒之后才以「下载失败」暴露，看着像网络故障。

这里测的就是这两处：候选目录要按「真能写」挑，权限错要当场快失败并给出修法。
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
REL = REPO / "deploy" / "release"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


update = _load("dabai_update_staging_mod", REL / "update.py")
tru = _load("dabai_test_release_update", Path(__file__).with_name("test_release_update.py"))


def cfg_for(state: Path) -> dict:
    cfg = dict(update.DEFAULTS)
    cfg["STATE"] = str(state)
    return cfg


@pytest.fixture
def home_fallback(tmp_path, monkeypatch):
    """把 $HOME 挪进 tmp，别把真家的 ~/.local/state/dabai-update 建出来。"""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home / ".local" / "state" / "dabai-update"


# ── ① 候选目录要按「真能写」挑 ────────────────────────────────────────────
def test_state_dir_skips_candidate_whose_staging_is_unwritable(tmp_path, home_fallback):
    state = tmp_path / "state"
    st = state / "staging"
    st.mkdir(parents=True)
    (st / "旧残片.part").write_text("x", encoding="utf-8")
    st.chmod(0o555)                       # 父目录可写、子目录写不进去 = orangepi 的现场
    try:
        assert update.state_dir(cfg_for(state)) == home_fallback
    finally:
        st.chmod(0o755)


def test_state_dir_creates_staging_and_backups(tmp_path, home_fallback):
    state = tmp_path / "state"
    assert update.state_dir(cfg_for(state)) == state
    assert (state / "staging").is_dir()
    assert (state / "backups").is_dir()


def test_state_dir_error_names_the_chown_fix(tmp_path, home_fallback):
    state = tmp_path / "state"
    (state / "staging").mkdir(parents=True)
    (state / "backups").mkdir()
    for d in (state / "staging", state / "backups"):
        d.chmod(0o555)
    home_fallback.mkdir(parents=True)
    home_fallback.chmod(0o555)            # 两个候选都写不进去
    try:
        with pytest.raises(SystemExit) as ei:
            update.state_dir(cfg_for(state))
        msg = str(ei.value)
        assert "chown" in msg and str(state) in msg
    finally:
        for d in (state / "staging", state / "backups", home_fallback):
            d.chmod(0o755)


# ── ② 权限错当场快失败，不装成网络故障 ────────────────────────────────────
def test_download_permission_error_fails_fast(tmp_path, monkeypatch):
    stage = tmp_path / "staging"
    stage.mkdir()
    stage.chmod(0o555)
    calls = []

    def boom(url, token, tmp):
        calls.append(tmp)
        raise PermissionError(13, "Permission denied", str(tmp))

    monkeypatch.setattr(update, "_fetch_whole", boom)
    try:
        with pytest.raises(RuntimeError) as ei:
            update.download("http://example.invalid/x.tar.gz",
                            stage / "dabai-1.1.8.tar.gz", "", attempts=3)
    finally:
        stage.chmod(0o755)
    assert len(calls) == 1, "权限错不该重试 3 次（白等 2+4 秒，还把权限问题伪装成网络问题）"
    assert "写不进暂存目录" in str(ei.value) and "chown" in str(ei.value)


# ── ③ 端到端：老暂存目录写不进去，这次升级照样装得成 ──────────────────────
def test_apply_survives_stale_unwritable_staging(tmp_path, monkeypatch):
    inst = tru.make_instance(tmp_path, "1.0.0")
    tar, man, digest = tru.make_package(
        tmp_path, {"server.py": "NEW SERVER\n", "agent.py": "NEW AGENT\n",
                   "VERSION": "1.5.0\n"}, version="1.5.0")
    state = tmp_path / "state"
    (state / "staging").mkdir(parents=True)
    (state / "staging" / "旧残片.part").write_text("x", encoding="utf-8")
    (state / "staging").chmod(0o555)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    try:
        rc, out = tru.run_update(tru.apply_args(inst, state, tar))
    finally:
        (state / "staging").chmod(0o755)
    assert rc == 0, out
    assert (inst / "VERSION").read_text().strip() == "1.5.0"
    assert (inst / "server.py").read_text().strip() == "NEW SERVER"
    assert (state / "staging" / "旧残片.part").exists(), "别人的老目录一个字节都不该动"
