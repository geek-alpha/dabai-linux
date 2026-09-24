#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""联邦自动更新（kind=release）：闸门链 + 更新器契约。

背景（2026-09-21）：发一版之后，其它机器还得人肉去拉。做法是让耳朵收到
kind=release 时唤醒本机更新器，装哪个版本由 update.py 自己查 GitHub + 校 sha256
+ 失败回滚，留言只当闹钟。

本文件锁死六条：

  1. 默认关 —— 没配过的机器一行代码都不许动
  2. 发送方白名单 —— 只有发布源能叫别人升级
  3. 冷却与尝试上限 —— 坏版本不许反复上机、伪造留言不许变成刷屏
  4. 远端不高于本地时绝不 apply（--check 退出码 0 → 只回话不落盘）
  5. 失败要记账（attempts+1）且冷却立刻生效 —— 崩在半路也不许连环重试
  6. 同一时刻只有一个更新在跑

为什么每条都要测：这是把「同伴说一句话」变成「我本机代码被替换」的通道，
错一条就是错在「谁能让我的机器换代码」上。
"""
import importlib.util
import json
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))


class _P:
    def __init__(self, rc, out="update.py: 一行日志"):
        self.returncode = rc
        self.stdout = out
        self.stderr = ""


def _load(tmp_path, settings, monkeypatch):
    """数据文件、settings.json、更新器都指到 tmp —— 不碰真实联邦状态和真更新器。"""
    spec = importlib.util.spec_from_file_location("peer_autoupdate_under_test",
                                                  BASE / "peer_autoupdate.py")
    pa = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pa)
    pa.BASE_DIR = tmp_path
    pa.STATE_FILE = tmp_path / "peer_autoupdate.json"
    pa.LOG_FILE = tmp_path / "peer_autoupdate.jsonl"
    pa.UPDATER = tmp_path / "update.py"
    pa.UPDATER.write_text("# stub\n", encoding="utf-8")
    (tmp_path / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
    monkeypatch.setattr(pa.peer_mesh, "say", lambda *a, **k: {"ok": True})
    monkeypatch.setattr(pa.peer_mesh, "node_info", lambda create=True: {"node_id": "rpi"})
    return pa


def _runner(pa, monkeypatch, codes):
    """假的更新器调用：按顺序吐出退出码，并记下每次的 argv。"""
    calls = []
    seq = list(codes)

    def fake(cmd, timeout):
        calls.append(cmd)
        return _P(seq.pop(0) if seq else 0)

    monkeypatch.setattr(pa, "_run", fake)
    return calls


def _entry(**kw):
    d = {"from": "aliyun", "kind": "release",
         "text": "v1.1.0 已发布：dabai-1.1.0.tar.gz + .sha256 资产齐全，可以拉。"}
    d.update(kw)
    return d


ON = {"peer": {"auto_update": True}}


def _state(pa):
    return json.loads(pa.STATE_FILE.read_text(encoding="utf-8"))


def test_default_off_does_nothing(tmp_path, monkeypatch):
    pa = _load(tmp_path, {}, monkeypatch)
    d = pa.decide(_entry())
    assert d["run"] is False and "auto_update" in d["reason"]
    calls = _runner(pa, monkeypatch, [10, 0])
    r = pa.run_once(_entry())
    assert r.get("skipped") is True and calls == [], "默认关却动了更新器"


def test_source_allowlist(tmp_path, monkeypatch):
    pa = _load(tmp_path, {"peer": {"auto_update": True, "release_source": ["aliyun"]}},
               monkeypatch)
    d = pa.decide(_entry(**{"from": "wsl"}))
    assert d["run"] is False and "白名单" in d["reason"]
    assert pa.decide(_entry())["run"] is True


def test_kind_gate(tmp_path, monkeypatch):
    pa = _load(tmp_path, ON, monkeypatch)
    d = pa.decide(_entry(kind="say"))
    assert d["run"] is False and "kind" in d["reason"]


def test_cooldown_and_attempt_cap(tmp_path, monkeypatch):
    pa = _load(tmp_path, ON, monkeypatch)
    pa.STATE_FILE.write_text(json.dumps({"last_ts": time.time()}), encoding="utf-8")
    assert pa.decide(_entry())["run"] is False
    assert "冷却" in pa.decide(_entry())["reason"]

    pa.STATE_FILE.write_text(json.dumps({"last_ts": 0, "attempts": {"1.1.0": 2}}),
                             encoding="utf-8")
    d = pa.decide(_entry())
    assert d["run"] is False and "试过" in d["reason"]


def test_already_latest_never_applies(tmp_path, monkeypatch):
    pa = _load(tmp_path, ON, monkeypatch)
    calls = _runner(pa, monkeypatch, [0])
    r = pa.run_once(_entry())
    assert r["ok"] is True and r.get("skipped") is True
    assert len(calls) == 1 and "--check" in calls[0], "远端没新版却 apply 了"
    assert "installed" not in _state(pa)


def test_apply_success_records_and_clears_attempts(tmp_path, monkeypatch):
    pa = _load(tmp_path, ON, monkeypatch)
    pa.STATE_FILE.write_text(json.dumps({"last_ts": 0, "attempts": {"1.1.0": 1}}),
                             encoding="utf-8")
    calls = _runner(pa, monkeypatch, [10, 0])
    r = pa.run_once(_entry())
    assert r["ok"] is True and "已更新" in r["reason"]
    assert [c[-1] for c in calls] == ["--check", "--apply"]
    st = _state(pa)
    assert st["installed"] == "1.1.0" and st["attempts"] == {}


def test_apply_failure_counts_and_cools_down(tmp_path, monkeypatch):
    pa = _load(tmp_path, ON, monkeypatch)
    _runner(pa, monkeypatch, [10, 1])
    r = pa.run_once(_entry())
    assert r["ok"] is False
    st = _state(pa)
    assert st["attempts"] == {"1.1.0": 1}, "失败没记账"
    assert time.time() - float(st["last_ts"]) < 60, "失败后没吃冷却，会连环重试"


def test_check_error_stops_before_apply(tmp_path, monkeypatch):
    pa = _load(tmp_path, ON, monkeypatch)
    calls = _runner(pa, monkeypatch, [3])
    r = pa.run_once(_entry())
    assert r["ok"] is False and len(calls) == 1 and "--check" in calls[0]
    st = _state(pa)
    assert st["attempts"] == {"1.1.0": 1}


def test_single_flight_lock(tmp_path, monkeypatch):
    pa = _load(tmp_path, ON, monkeypatch)
    _runner(pa, monkeypatch, [10, 0])
    pa._LOCK.acquire()
    try:
        r = pa.run_once(_entry())
    finally:
        pa._LOCK.release()
    assert r["ok"] is False and "在跑" in r["reason"]


def test_parse_version_from_publish_text():
    spec = importlib.util.spec_from_file_location("pa_parse", BASE / "peer_autoupdate.py")
    pa = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pa)
    assert pa.parse_version("v1.1.0 已发布：dabai-1.1.0.tar.gz") == "1.1.0"
    assert pa.parse_version("v12.3.45 可以拉") == "12.3.45"
    assert pa.parse_version("随便一句话") == ""


# ── 更新器契约：--root 必须钉死；「已移交」不等于「已装好」 ────────────────
def _detached_runner(pa, monkeypatch, codes):
    """假更新器：apply 那一步吐 detach 标记（模拟 update.py 把自己交给独立 unit 后返回 0）。"""
    calls = []
    seq = list(codes)

    def fake(cmd, timeout):
        calls.append(cmd)
        rc = seq.pop(0) if seq else 0
        out = ("[detached] 已交给独立 unit dabai-updater-1 继续更新"
               if cmd[-1] == "--apply" else "update.py: 一行日志")
        return _P(rc, out)

    monkeypatch.setattr(pa, "_run", fake)
    return calls


def test_updater_cmd_always_pins_root(tmp_path, monkeypatch):
    pa = _load(tmp_path, ON, monkeypatch)
    cmd = pa._updater_cmd("--check")
    assert "--root" in cmd and str(tmp_path) in cmd, \
        "不给 --root，update.py 会用开发机默认路径，装在别处的机器必然失败"


def test_detached_apply_is_confirmed_by_version(tmp_path, monkeypatch):
    pa = _load(tmp_path, ON, monkeypatch)
    pa.DETACH_WAIT = 0          # 不真等，直接看最终判定
    (tmp_path / "VERSION").write_text("1.1.0\n", encoding="utf-8")
    _detached_runner(pa, monkeypatch, [10, 0])
    r = pa.run_once(_entry())
    assert r["ok"] is True and r.get("detached") is True
    assert _state(pa)["installed"] == "1.1.0"


def test_detached_apply_without_version_change_is_not_success(tmp_path, monkeypatch):
    pa = _load(tmp_path, ON, monkeypatch)
    pa.DETACH_WAIT = 0
    (tmp_path / "VERSION").write_text("1.0.3\n", encoding="utf-8")
    _detached_runner(pa, monkeypatch, [10, 0])
    r = pa.run_once(_entry())
    assert r.get("detached") is True
    assert "未确认" in r["reason"], "把『已移交』当成『已装好』就是假成功"
    assert "installed" not in _state(pa), "结果未确认却记了成功"
    assert time.time() - float(_state(pa)["last_ts"]) < 60, "结果未确认也该吃冷却，别反复重试"
