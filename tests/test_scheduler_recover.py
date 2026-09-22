# -*- coding: utf-8 -*-
"""调度器的「重启恢复」契约：清悬挂标记 + 重启宽限。

为什么钉死：这两件事错了都不会报错，只会静默失效 ——
① running 标记不清，任务看着「正在执行」、实际要等 2 小时才被 _sweep_stale 放开；
② 没有宽限，进程每抖动重启一次就白烧一轮预算。
静默失效比崩溃更难发现，所以宁可慢、不可错的边界必须由测试守。
"""
import importlib
import time

import pytest


@pytest.fixture()
def sched(tmp_path, monkeypatch):
    m = importlib.import_module("scheduler")
    monkeypatch.setattr(m, "SCHED_FILE", tmp_path / "scheduled_tasks.json")
    return m


def _job(sched, running=False, interval=2400):
    job, err = sched.add_job("测试任务", "干活", interval)
    assert err is None
    jobs = sched._load()
    jobs[0]["running"] = running
    jobs[0]["next_run_at"] = time.time() - 100      # 已过期
    jobs[0]["runs"] = 3
    sched._save(jobs)
    return job["id"]


def test_release_running_clears_flag_without_counting_run(sched):
    jid = _job(sched, running=True)
    hit, err = sched.release_running(jid, "服务重启：执行体已随进程终止")
    assert err is None
    j = sched._load()[0]
    assert j["running"] is False
    assert j["runs"] == 3                      # 重启不算一次运行
    assert j["last_result"] == ""              # 不伪造结果
    assert "服务重启" in j["last_error"]


def test_release_running_noop_when_not_running(sched):
    """没悬挂就别乱写 —— 否则正常的 last_error 会被覆盖成噪音。"""
    jid = _job(sched, running=False)
    hit, err = sched.release_running(jid, "服务重启")
    assert err is None
    assert sched._load()[0]["last_error"] == ""


def test_release_running_missing_job(sched):
    hit, err = sched.release_running("sched-nope", "x")
    assert hit is None and err


def test_defer_job_pushes_next_run(sched):
    jid = _job(sched)
    assert sched._load()[0]["next_run_at"] < time.time()
    hit, err = sched.defer_job(jid, 90)
    assert err is None
    after = sched._load()[0]["next_run_at"]
    assert after > time.time() + 80
    assert after <= time.time() + 100


def test_defer_job_never_pulls_earlier(sched):
    """本来就在更远的未来 → 原样保留，别把正常节奏拽回眼前。"""
    jid = _job(sched)
    jobs = sched._load()
    jobs[0]["next_run_at"] = time.time() + 1800
    sched._save(jobs)
    sched.defer_job(jid, 90)
    assert sched._load()[0]["next_run_at"] > time.time() + 1700


def test_defer_job_missing(sched):
    hit, err = sched.defer_job("sched-nope", 90)
    assert hit is None and err
