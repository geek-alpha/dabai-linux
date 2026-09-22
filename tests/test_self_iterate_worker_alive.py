# -*- coding: utf-8 -*-
"""自我迭代「运行标记悬挂」的清理契约。

为什么钉死：scheduler.run_now 见到 running=True 直接拒绝（scheduler.py:174），而
_sweep_stale 要等 max(interval*2, 2h) 才清标记（scheduler.py:261）。执行体崩溃/被停
之后标记还挂着时，点「启动」最长 72 分钟不派发 —— 用户看到的就是「点了没反应」。
判据只能是「执行体还在不在」，不能靠时间猜：猜早了会重复派发烧双份预算。
"""
import asyncio

import pytest


class _W:
    def __init__(self, job_id):
        self.extra = {"job_id": job_id}
        self.status = "running"


class _SA:
    def __init__(self, job_ids):
        self._ws = [_W(j) for j in job_ids]

    def active(self):
        return list(self._ws)


@pytest.fixture()
def si_state(tmp_path, monkeypatch):
    """把自我迭代状态文件指到 tmp，端点测试不许碰真实账本。"""
    import tools.self_iterate as si
    monkeypatch.setattr(si, "STATE_FILE", tmp_path / "self_iterate.json")
    return si


def test_执行体还在_按在跑处理(monkeypatch):
    import server
    monkeypatch.setattr(server, "_get_sub_agents", lambda: _SA(["sched-1"]))
    assert server._self_iterate_worker_alive("sched-1") is True


def test_执行体已不在_判为悬挂(monkeypatch):
    import server
    monkeypatch.setattr(server, "_get_sub_agents", lambda: _SA(["别的任务"]))
    assert server._self_iterate_worker_alive("sched-1") is False


def test_查不出来_按在跑处理(monkeypatch):
    """宁可多等一轮，也不能因为查不到就重复派发烧双份预算。"""
    import server

    def boom():
        raise RuntimeError("sub_agents 没起来")

    monkeypatch.setattr(server, "_get_sub_agents", boom)
    assert server._self_iterate_worker_alive("sched-1") is True


def test_启动被拒且执行体不在_清悬挂后重排(si_state, monkeypatch):
    import server
    import scheduler

    job = {"id": "sched-1", "enabled": True, "running": True}
    released = []
    seq = []

    monkeypatch.setattr(server, "_self_iterate_job", lambda: job)
    monkeypatch.setattr(server, "_self_iterate_worker_alive", lambda jid: False)
    monkeypatch.setattr(scheduler, "set_enabled", lambda jid, en: (job, None))
    monkeypatch.setattr(scheduler, "release_running",
                        lambda jid, note="": (released.append((jid, note)), (job, None))[1])

    def _run_now(jid):
        seq.append(jid)
        # 第一次是悬挂标记挡着，清掉之后必须能排上
        return (job, None) if len(seq) > 1 else (None, "该任务正在执行中，请等它跑完")

    monkeypatch.setattr(scheduler, "run_now", _run_now)

    out = asyncio.run(server.self_iterate_start({}))
    assert out["ok"] is True
    assert len(seq) == 2, "被拒后必须清悬挂再排一次，否则点启动要干等到 _sweep_stale"
    assert released and released[0][0] == "sched-1"


def test_启动被拒但执行体真在跑_不动标记(si_state, monkeypatch):
    """上一轮真在跑时不许清标记 —— 清了会重复派发，同一轮烧两份预算。"""
    import server
    import scheduler

    job = {"id": "sched-1", "enabled": True, "running": True}
    released = []
    seq = []

    monkeypatch.setattr(server, "_self_iterate_job", lambda: job)
    monkeypatch.setattr(server, "_self_iterate_worker_alive", lambda jid: True)
    monkeypatch.setattr(scheduler, "set_enabled", lambda jid, en: (job, None))
    monkeypatch.setattr(scheduler, "release_running",
                        lambda jid, note="": (released.append((jid, note)), (job, None))[1])
    monkeypatch.setattr(scheduler, "run_now",
                        lambda jid: (seq.append(jid), (None, "该任务正在执行中，请等它跑完"))[1])

    out = asyncio.run(server.self_iterate_start({}))
    assert out["ok"] is True
    assert len(seq) == 1 and not released


def test_停止时清悬挂标记(si_state, monkeypatch):
    import server
    import scheduler

    job = {"id": "sched-1", "enabled": True, "running": True}
    released = []

    monkeypatch.setattr(server, "_self_iterate_job", lambda: job)
    monkeypatch.setattr(server, "_self_iterate_worker_alive", lambda jid: False)
    monkeypatch.setattr(scheduler, "set_enabled", lambda jid, en: (job, None))
    monkeypatch.setattr(scheduler, "release_running",
                        lambda jid, note="": (released.append((jid, note)), (job, None))[1])

    out = asyncio.run(server.self_iterate_stop())
    assert out["ok"] is True
    assert released and released[0][0] == "sched-1"


def test_停止时执行体还在_留着标记(si_state, monkeypatch):
    import server
    import scheduler

    job = {"id": "sched-1", "enabled": True, "running": True}
    released = []

    monkeypatch.setattr(server, "_self_iterate_job", lambda: job)
    monkeypatch.setattr(server, "_self_iterate_worker_alive", lambda jid: True)
    monkeypatch.setattr(scheduler, "set_enabled", lambda jid, en: (job, None))
    monkeypatch.setattr(scheduler, "release_running",
                        lambda jid, note="": (released.append((jid, note)), (job, None))[1])

    asyncio.run(server.self_iterate_stop())
    assert not released
