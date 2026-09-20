# -*- coding: utf-8 -*-
"""任务级重试必须听服务端的 Retry-After（不只是 agent 主循环）。

背景（2026-09-20）：全仓 4 个 backoff_delay 调用点里，harness/tasks.py 的
_leaf_failed 是唯一没传 retry_after 的 —— 而且它连异常对象都拿不到，
只收到一句 msg 字符串（异常在 _leaf_body 里被格式化成文本就丢了）。

取证（改前实测）：LLM 步骤抛 429 + Retry-After: 7 时，任务 0.82s 后就重撞，
服务端要求等 7s —— 等于把限流又加重一次。
"""
import asyncio
import pathlib

import pytest

from harness.tasks import Task, TaskSystem


class _FakeHarness:
    def __init__(self, base):
        self.base_dir = pathlib.Path(base)


class _Resp:
    def __init__(self, headers):
        self.status_code = 429
        self.headers = headers


def _http_err(headers):
    class _Err(Exception):
        response = _Resp(headers)

    return _Err("429 Too Many Requests")


def _run_leaf(tmp_path, exc, attempts=1):
    """跑一次 _leaf_body（真实执行链：_execute → _exec_llm → 抛错 → 归档）。

    返回 (抓到的 delay 列表, 任务)。
    """
    ts = TaskSystem(_FakeHarness(tmp_path))
    delays = []
    ts._enqueue = lambda t, delay=0.0: delays.append(delay)

    async def boom(system, prompt, max_tokens=800):
        raise exc

    ts.set_llm_executor(boom)
    t = Task("t1", "演示", "task", {"kind": "llm", "prompt": "hi"}, max_attempts=3)
    t.attempts = attempts
    asyncio.run(ts._leaf_body(t))
    return delays, t, ts


def _exponential_range(ts, attempt=1):
    """纯指数退避（base 取自真实配置）叠抖动的取值范围。"""
    base = float(ts._cfg["backoff"])
    raw = base * (2 ** (attempt - 1))
    return 0.8 * raw, 1.2 * raw


def test_task_retry_respects_retry_after(tmp_path):
    """服务端说等 7s 就等 7s —— 精确听，不叠抖动、不按指数。"""
    delays, t, _ = _run_leaf(tmp_path, _http_err({"retry-after": "7"}))
    assert delays == [7.0], f"没听服务端：{delays}（status={t.status_text}）"


def test_task_retry_without_header_uses_exponential(tmp_path):
    """没有 Retry-After 的普通异常：退回指数退避（base 取自真实配置，叠抖动）。"""
    delays, _, ts = _run_leaf(tmp_path, RuntimeError("boom"))
    lo, hi = _exponential_range(ts)
    assert len(delays) == 1, delays
    assert lo <= delays[0] <= hi, (delays, lo, hi)


def test_task_retry_after_capped(tmp_path):
    """服务端给 3600s 也不能把任务挂死一小时：cap 优先。"""
    delays, _, _ = _run_leaf(tmp_path, _http_err({"retry-after": "3600"}))
    assert delays == [30.0], delays


def test_task_retry_after_http_date(tmp_path):
    """HTTP-date 形式同样要认（与 core._retry_after_of 行为一致）。"""
    from datetime import datetime, timedelta, timezone
    from email.utils import format_datetime

    hdr = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=20), usegmt=True)
    delays, _, _ = _run_leaf(tmp_path, _http_err({"retry-after": hdr}))
    assert len(delays) == 1, delays
    assert 17.0 <= delays[0] <= 20.0, delays


def test_no_backoff_when_attempts_exhausted(tmp_path):
    """重试次数用完 → 直接终态失败，不再排程（Retry-After 也救不回来）。"""
    delays, t, _ = _run_leaf(tmp_path, _http_err({"retry-after": "7"}), attempts=3)
    assert delays == [], delays
    assert t.state == "failed", t.state


@pytest.mark.parametrize("bad", ["abc", ""])
def test_task_retry_after_bad_value_falls_back(tmp_path, bad):
    """坏头值不能当成 0 秒打热循环：退回指数退避。"""
    delays, _, ts = _run_leaf(tmp_path, _http_err({"retry-after": bad}))
    lo, hi = _exponential_range(ts)
    assert len(delays) == 1, delays
    assert lo <= delays[0] <= hi, (delays, lo, hi)
