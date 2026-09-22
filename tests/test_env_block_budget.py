# -*- coding: utf-8 -*-
"""环境阻塞（402 余额不足 / 401 鉴权）不该吃预算、不该把真因埋掉。

实测 2026-09-22：402 每 40 分钟打断自我迭代一次，派发 0.8 秒即失败，而 dispatched
照涨 —— 24 格预算会被空转吃光，循环最后以「轮数预算耗尽」这个假原因停下（真因是余额）。
"""
import importlib.util
import json
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "tools" / "self_iterate.py"
SRV = (ROOT / "server.py").read_text(encoding="utf-8")

# journalctl -u myservice 2026-09-22 06:13:33 原文（402 那段）
REAL_402 = ("任务《定时·自我迭代循环》出错了：APIStatusError: Error code: 402 - "
            "{'error': {'message': 'Insufficient Balance', 'type': 'unknown_error', "
            "'param': None, 'code': 'invalid_request_error'}}")


def _load():
    spec = importlib.util.spec_from_file_location("self_iterate_envblock_test", SRC)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def mod(tmp_path, monkeypatch):
    m = _load()
    monkeypatch.setattr(m, "STATE_FILE", tmp_path / "self_iterate.json")
    monkeypatch.setattr(m, "ANCHORS", [tmp_path / "anchor.json"])
    monkeypatch.setattr(m, "_git_touched", lambda since: [])
    return m


def _rounds(n):
    return [{"n": i + 1, "target": "t", "effective": True} for i in range(n)]


def _state(mod, **kw):
    """默认摆成实测现场：8 轮已记账、第 9 次派发在飞、预算从 2 起算。"""
    st = {"running": True, "started_at": time.time(), "budget_rounds": 24,
          "epoch_start": 2, "stop_reason": "", "stop_kind": "",
          "rounds": _rounds(8), "dispatched": 9, "env_blocked": 0,
          "env_blocked_reason": "", "last_ts": time.time()}
    st.update(kw)
    mod.STATE_FILE.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
    return st


# ---------- 判据本身 ----------

def test_真实402原文认成环境阻塞(mod):
    assert mod.is_env_block(REAL_402) is True


def test_鉴权失败也算环境阻塞(mod):
    assert mod.is_env_block("APIStatusError: Error code: 401 - invalid_api_key") is True


def test_正常轮报不被误判(mod):
    assert mod.is_env_block("第 9 轮完成：改了 tools/x.py，1264 passed") is False
    assert mod.is_env_block("") is False
    assert mod.is_env_block(None) is False


# ---------- 预算 ----------

def test_环境阻塞当场退回那一格预算(mod):
    _state(mod)
    mod.note_dispatch_result(REAL_402)
    st = mod._load()
    assert st["dispatched"] == 8, "那一格该退回：没换来工作，也不是执行体不记账"
    assert st["env_blocked"] == 1
    # spent = max(记账轮数, 派发次数)：退完只剩 8 笔真账，空转那格不再占额
    assert mod.spent(st) == len(st["rounds"]) == 8


def test_同一次失败重复汇报不会退两次(mod):
    _state(mod)
    mod.note_dispatch_result(REAL_402)
    mod.note_dispatch_result(REAL_402)
    st = mod._load()
    assert st["dispatched"] == 8
    assert st["env_blocked"] == 1


def test_没有待认领的派发就不动账(mod):
    """真记了账的轮（dispatched == rounds）不该被后面的 402 文字牵连。"""
    _state(mod, dispatched=8, rounds=_rounds(8))
    mod.note_dispatch_result(REAL_402)
    st = mod._load()
    assert st["dispatched"] == 8
    assert int(st.get("env_blocked") or 0) == 0


def test_连续三次环境阻塞后停且写清真因(mod):
    _state(mod, dispatched=8)
    before = mod.spent(mod._load())
    for _ in range(3):
        mod.tick()                      # 下一次派发
        mod.note_dispatch_result(REAL_402)
    st = mod._load()
    assert st["running"] is False
    assert st["stop_kind"] == "env"
    assert "环境阻塞" in st["stop_reason"]
    assert "Insufficient Balance" in st["stop_reason"]
    assert mod.spent(st) == before, "三次空转不该吃掉预算"


def test_停后再派发不会把真停因覆盖成未启动(mod):
    _state(mod, dispatched=8)
    for _ in range(3):
        mod.tick()
        mod.note_dispatch_result(REAL_402)
    # 停之后 402 还会继续报回来（已经派出去的那几轮）：不许把真停因改成「未启动」，
    # 否则 resume_plan 认不出环境阻塞，主人充完值也不会自动接着跑。
    for _ in range(3):
        mod.tick()
        mod.note_dispatch_result(REAL_402)
    st = mod._load()
    assert st["stop_kind"] == "env"
    assert "环境阻塞" in st["stop_reason"]
    assert mod.resume_plan(st)["resume"] is True


def test_真记了一笔账就清掉环境阻塞计数(mod):
    _state(mod, env_blocked=2, env_blocked_reason=REAL_402)
    mod.record(target="learn", action="修 x", evidence="1264 passed",
               since_ts=time.time() - 1)
    st = mod._load()
    assert int(st.get("env_blocked") or 0) == 0


# ---------- 看门狗与恢复 ----------

def test_退回后看门狗不再把空转当断轮(mod):
    now = time.time()
    _state(mod, last_ts=now - 3600)
    job = {"id": "j1", "enabled": True, "running": False, "last_run_at": now}
    assert mod.stall_plan(job=job, now=now)["action"] == "wake"
    mod.note_dispatch_result(REAL_402)
    assert mod.stall_plan(job=job, now=now)["action"] == "none"


def test_环境阻塞停因可自动恢复(mod):
    _state(mod, running=False, stop_kind="env", stop_reason="连续 3 次派发被环境阻塞")
    plan = mod.resume_plan(mod._load())
    assert plan["resume"] is True, "主人充完值重启后该自己接着跑"


def test_预算和连续无效停因不许自动续命(mod):
    for kind in ("", "user"):
        _state(mod, running=False, stop_kind=kind, stop_reason="轮数预算耗尽（24/24）")
        assert mod.resume_plan(mod._load())["resume"] is False


def test_start清零环境阻塞计数(mod):
    _state(mod, running=False, stop_kind="env", env_blocked=3,
           env_blocked_reason=REAL_402)
    mod.start()
    st = mod._load()
    assert int(st.get("env_blocked") or 0) == 0
    assert st["stop_kind"] == ""


# ---------- server 接线 ----------

def test_汇报链路给自我迭代记账(mod):
    i = SRV.index("async def _deliver_sub_agent_report(")
    seg = SRV[i:SRV.index("\n    ws = getattr(worker, \"ws\", None)", i)]
    assert "_self_iterate_note_result(job_id, message)" in seg


def test_结局处退预算而不是下次派发时(mod):
    i = SRV.index("def _self_iterate_note_result(")
    seg = SRV[i:SRV.index("def _scheduled_user_inputs(", i)]
    assert "note_dispatch_result(" in seg
    assert "set_enabled(job.get(\"id\"), False)" in seg, "停了就别再每 40 分钟派一次"


def test_派发计数不带上次结局(mod):
    i = SRV.index("def _self_iterate_tick(")
    seg = SRV[i:SRV.index("def _self_iterate_note_result(", i)]
    assert "st = tick()" in seg


def test_已停的循环不再派发子智能体(mod):
    i = SRV.index("async def _fire_scheduled_job(")
    seg = SRV[i:SRV.index("asyncio.ensure_future(start_scheduler(", i)]
    assert "if si is not None and not si.get(\"running\")" in seg
    assert "release_running(" in seg, "没派发就不该计 runs，但要解掉 running 标记"
