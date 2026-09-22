# -*- coding: utf-8 -*-
"""子智能体的 reasoning_content 回传自愈（thinking 渠道 400）。

背景（2026-09-22）：定时自我迭代循环的子智能体汇报
`BadRequestError: 400 ... The reasoning_content in the thinking mode must be
passed back to the API.` 主智能体早有这套自愈（agent.py:_retry_with_reasoning_echo），
子智能体的 _llm_call 没有——它自产的 assistant 消息永远不带 reasoning_content
（_run_loop 只写 content + tool_calls），一旦上游严格就整轮判死。

取证：tools/reasoning_echo_400_probe.py 六种形状（含子智能体真实形状）实测当前渠道
全 200，复现不出——所以这是间歇严格校验下的结构性缺口，按「结构上不可能缺字段」修。

判据（正反两面）：
- 命中 reasoning 400 → 补字段重发一次（调用次数 2），字段补在 assistant 上；
- 补空串仍被拒 → 补非空占位再发（3 次）；
- 两级都被拒 → 抛原错，不无限重试（3 次封顶）；
- 别的 400（参数非法）→ 不吞不补发（1 次），原来的确定性错误行为不被削弱。
"""
import asyncio
import sys
import types
from pathlib import Path

import httpx
import pytest
from openai import APIStatusError

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

from sub_agents import SubAgentManager  # noqa: E402

REASONING_400 = (
    "Error code: 400 - {'error': {'message': 'The `reasoning_content` in the "
    "thinking mode must be passed back to the API.', "
    "'type': 'invalid_request_error'}}")
PARAM_400 = (
    "Error code: 400 - {'error': {'message': 'parameter is invalid: tools', "
    "'type': 'invalid_request_error'}}")


def _api_error(status: int, msg: str) -> APIStatusError:
    """真实 openai 异常对象（带 httpx.Response），不是自造替身。"""
    resp = httpx.Response(
        status,
        request=httpx.Request("POST", "https://api.example/v1/chat/completions"))
    return APIStatusError(msg, response=resp, body=None)


class _SeqCompletions:
    """按顺序返回预设结果；序列用尽后重复最后一个（用于「一直被拒」场景）。"""

    def __init__(self, seq) -> None:
        self.seen: list = []
        self._seq = list(seq)

    async def create(self, **kw):
        self.seen.append(kw)
        item = self._seq[min(len(self.seen) - 1, len(self._seq) - 1)]
        if isinstance(item, BaseException):
            raise item
        return item


def _messages() -> list:
    return [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "干活"},
        {"role": "assistant", "content": "a1",
         "tool_calls": [{"id": "1", "type": "function",
                         "function": {"name": "f", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "1", "content": "r1"},
        {"role": "assistant", "content": "a2",
         "tool_calls": [{"id": "2", "type": "function",
                         "function": {"name": "f", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "2", "content": "r2"},
    ]


def _manager(seq):
    comp = _SeqCompletions(seq)
    client = types.SimpleNamespace(
        chat=types.SimpleNamespace(completions=comp))
    mgr = SubAgentManager.__new__(SubAgentManager)   # 绕过 __init__：_llm_call 不用实例字段
    return mgr, client, comp


def _ok():
    return types.SimpleNamespace(usage=None, choices=[])


def test_命中reasoning400自动补空串重发():
    mgr, client, comp = _manager([_api_error(400, REASONING_400), _ok()])
    resp = asyncio.run(mgr._llm_call(client, "m", _messages(), []))
    assert resp.usage is None
    assert len(comp.seen) == 2, "首次 400 + 补齐重发，共 2 次"
    sent = comp.seen[-1]["messages"]
    assistants = [m for m in sent if m.get("role") == "assistant"]
    assert assistants and all("reasoning_content" in m for m in assistants)
    assert all(m["reasoning_content"] == "" for m in assistants)
    others = [m for m in sent if m.get("role") != "assistant"]
    assert all("reasoning_content" not in m for m in others), "非 assistant 不许挂该字段"


def test_补空串仍拒则补非空占位():
    mgr, client, comp = _manager(
        [_api_error(400, REASONING_400), _api_error(400, REASONING_400), _ok()])
    asyncio.run(mgr._llm_call(client, "m", _messages(), []))
    assert len(comp.seen) == 3, "补空串仍拒 → 补占位再发"
    assistants = [m for m in comp.seen[-1]["messages"] if m.get("role") == "assistant"]
    assert all(m["reasoning_content"] for m in assistants), "第二级必须是非空占位"


def test_两级都被拒抛原错且不无限重试():
    err = _api_error(400, REASONING_400)
    mgr, client, comp = _manager([err, err, err, err])
    with pytest.raises(APIStatusError):
        asyncio.run(mgr._llm_call(client, "m", _messages(), []))
    assert len(comp.seen) == 3, "首次 + 两级自愈，封顶 3 次"


def test_别的400不吞不补发():
    err = _api_error(400, PARAM_400)
    mgr, client, comp = _manager([err, err, err, err])
    with pytest.raises(APIStatusError):
        asyncio.run(mgr._llm_call(client, "m", _messages(), []))
    assert len(comp.seen) == 1, "参数类 400 是确定性错误：只发一次、原样抛"


def test_不传msgs时用原messages零侵入():
    mgr, client, comp = _manager([_ok()])
    msgs = _messages()
    asyncio.run(mgr._llm_call(client, "m", msgs, []))
    assert comp.seen[0]["messages"][0]["role"] == "system"
    assert all("reasoning_content" not in m for m in comp.seen[0]["messages"]), \
        "没踩坑时不许改请求形状"
