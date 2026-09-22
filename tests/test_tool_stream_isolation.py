# -*- coding: utf-8 -*-
"""工具执行流的异常隔离契约测试。

为什么单独钉死这份逻辑：
  批内并行收结果那行 `i, ev = t.result()` 外面没有 try，顺序分支也一样。
  工具执行流一旦抛出非取消异常（心跳/缓存/生成器机制出问题），异常会直接
  逃出 chat_stream，整轮对话连同已完成的工作一起作废——「一个工具拖垮整个
  任务」正是这个形状。隔离层必须由测试守住，否则改回裸 async for 全库测试
  仍然全绿，只有线上整轮被打断才会被发现。
"""

import asyncio

from agent import AIAgent, ToolCallProgress


def _agent(gen_factory):
    """只装隔离层需要的那个依赖，避开 AIAgent 的重量级构造。"""
    a = object.__new__(AIAgent)
    a._supervised_tool_stream = gen_factory
    return a


async def _collect(agen):
    out = []
    async for ev in agen:
        out.append(ev)
    return out


def test_exception_becomes_failed_outcome():
    """工具流抛普通异常 → 降级成 (错误文案, False)，不冒泡。"""
    async def fake(tool_name, arguments):
        yield ToolCallProgress(tool_name, 1.0, "跑着呢")
        raise RuntimeError("boom")

    evs = asyncio.run(_collect(_agent(fake)._tool_stream_isolated("shell_run", {})))
    assert isinstance(evs[0], ToolCallProgress)
    text, ok = evs[-1]
    assert ok is False
    assert "boom" in text and "shell_run" in text


def test_base_exception_also_isolated():
    """非 Exception 的 BaseException（第三方库爱用）同样不许逃出去。"""
    class _Weird(BaseException):
        pass

    async def fake(tool_name, arguments):
        raise _Weird("weird")
        yield  # pragma: no cover

    evs = asyncio.run(_collect(_agent(fake)._tool_stream_isolated("t", {})))
    text, ok = evs[-1]
    assert ok is False and "weird" in text


def test_cancellation_still_propagates():
    """取消必须照常冒泡：隔离层不是「吞掉一切」。"""
    async def fake(tool_name, arguments):
        await asyncio.sleep(30)
        yield ("never", True)

    async def main():
        agen = _agent(fake)._tool_stream_isolated("t", {})
        task = asyncio.ensure_future(_collect(agen))
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            return "cancelled"
        return "swallowed"

    assert asyncio.run(main()) == "cancelled"


def test_normal_path_untouched():
    """正常路径必须原样透传：进度事件 + 最终结果一个不少、顺序不变。"""
    async def fake(tool_name, arguments):
        yield ToolCallProgress(tool_name, 0.5, "心跳")
        yield ("ok-result", True)

    evs = asyncio.run(_collect(_agent(fake)._tool_stream_isolated("read", {})))
    assert isinstance(evs[0], ToolCallProgress)
    assert evs[-1] == ("ok-result", True)
