# -*- coding: utf-8 -*-
"""确定性 LLM 错误不该再烧一次「降级重试」。

背景（2026-09-22）：402 Insufficient Balance 每 40 分钟打断一次自我迭代循环，
日志原文是「LLM 调用重试耗尽（APIStatusError: Error code: 402 …），降级为单次
小预算调用」——读起来像网络抖动，实际 is_transient_error(402)=False，
retry_async 第一次尝试就抛，那次降级必然同样失败。

判据（正反两面都测）：
- 确定性错误（402/401）→ create 只被调 1 次，异常原样抛出；
- 瞬态错误（503）→ 仍然走 6 次重试 + 1 次降级 = 7 次（重试预算 2026-09-26 从
  3 次扩到 6 次，为的是驮过网关分钟级的抽风 400），行为不许被后续改动削掉；
- 网关「无说明文字的 400」（body 只有 model 名）→ 同 503 走重试，不再 1 次判死。
"""
import asyncio
import sys
from pathlib import Path

import httpx
import pytest
from openai import APIStatusError

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

from sub_agents import SubAgentManager  # noqa: E402


def _api_error(status: int, msg: str, body=None) -> APIStatusError:
    """真实 openai 异常对象（带 httpx.Response），不是自造替身。"""
    resp = httpx.Response(
        status,
        request=httpx.Request("POST", "https://api.example/v1/chat/completions"))
    return APIStatusError(msg, response=resp, body=body)


class _FakeCompletions:
    def __init__(self, exc: BaseException) -> None:
        self.calls = 0
        self._exc = exc

    async def create(self, **kw):
        self.calls += 1
        raise self._exc


class _FakeClient:
    def __init__(self, exc: BaseException) -> None:
        self.calls = _FakeCompletions(exc)
        self.chat = type("_Chat", (), {"completions": self.calls})()


def _run(client) -> int:
    mgr = SubAgentManager.__new__(SubAgentManager)   # 绕过 __init__：_llm_call 不用实例字段
    with pytest.raises(APIStatusError):
        asyncio.run(mgr._llm_call(client, "test-model", [], []))
    return client.calls.calls


def test_402余额不足不降级重试():
    assert _run(_FakeClient(_api_error(402, "Insufficient Balance"))) == 1


def test_401鉴权失败不降级重试():
    assert _run(_FakeClient(_api_error(401, "invalid api key"))) == 1


def test_503服务端错误仍走重试加降级(monkeypatch):
    async def no_sleep(_d):
        return None

    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    assert _run(_FakeClient(_api_error(503, "server error"))) == 7


def test_网关无说明文字的400走重试不判死(monkeypatch):
    """定时任务《掌柜巡店·每日》2026-09-26 19:59 就死在这种 400 上（13 秒）。"""
    async def no_sleep(_d):
        return None

    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    err = _api_error(400, "Error code: 400 - {'model': 'deepseek-v4.1-flash'}",
                     body={"model": "deepseek-v4.1-flash"})
    assert _run(_FakeClient(err)) == 7
