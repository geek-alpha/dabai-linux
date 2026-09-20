"""中途问用户（request_user_input）契约测试。

盯的是三条边界，而不是「能跑」：
  1. 送达判断——无人在线必须立刻返回，不能干等超时；
  2. 答案配对——resolve 只认自己的 request_id，过期卡片不误伤新提问；
  3. 兜底路径——超时/跳过都返回「按默认继续」的可读说明，绝不吊死一轮。
外加：技能规范合法 + 经 harness 路由真能执行 + Plan Mode 放行（阶段2 的提问通道）。
"""
import asyncio
import importlib.util
import json
import os
import sys
import time

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

import ask_user  # noqa: E402
import plan_mode as PM  # noqa: E402


@pytest.fixture(autouse=True)
def _clean():
    """每个用例前后都清空挂起表与广播钩子——全局单例，别互相污染。"""
    ask_user._PENDING.clear()
    yield
    ask_user._PENDING.clear()
    ask_user.set_broadcast(None)


def _broadcast(answer=None, clients=1, delay=0.0, sink=None):
    """假广播：clients=在线前端数；answer 给定时按 request_id 自动回填。"""
    async def fn(ev):
        if sink is not None:
            sink.append(ev)
        if answer is not None:
            asyncio.get_running_loop().call_later(
                delay, ask_user.resolve, ev["request_id"], answer)
        return clients
    return fn


# ---------- 参数规范化 ----------

def test_选项去空去重截断封顶():
    opts = ask_user.clean_options(["  A  ", "", "A", None, "B" * 200, "C", "D", "E", "F", "G"])
    assert opts[0] == "A" and opts[1] == "B" * ask_user.MAX_OPTION_CHARS
    assert len(opts) == ask_user.MAX_OPTIONS
    assert ask_user.clean_options("单个字符串") == ["单个字符串"]
    assert ask_user.clean_options(None) == []


@pytest.mark.parametrize("raw,expect", [
    (None, ask_user.DEFAULT_TIMEOUT),
    ("abc", ask_user.DEFAULT_TIMEOUT),
    (0, ask_user.DEFAULT_TIMEOUT),
    (-5, ask_user.DEFAULT_TIMEOUT),
    (0.1, ask_user.MIN_TIMEOUT),
    (99999, ask_user.MAX_TIMEOUT),
    (12.5, 12.5),
])
def test_等待秒数夹取(raw, expect):
    assert ask_user.clamp_timeout(raw) == expect


# ---------- 送达与兜底 ----------

def test_空问题直接报错且不推卡片():
    sent = []
    ask_user.set_broadcast(_broadcast(sink=sent))
    out = asyncio.run(ask_user.ask("   "))
    assert "不能为空" in out
    assert sent == []          # 没意义的问题不该弹卡片骚扰用户
    assert ask_user.pending_count() == 0


def test_无人在线立刻返回不干等():
    ask_user.set_broadcast(_broadcast(clients=0))
    t0 = time.monotonic()
    out = asyncio.run(ask_user.ask("选哪个？", timeout=60))
    assert "没送达" in out and "别调用本工具" in out
    assert time.monotonic() - t0 < 2      # 关键：不等 60s
    assert ask_user.pending_count() == 0


def test_无广播钩子也立刻返回():
    out = asyncio.run(ask_user.ask("选哪个？"))
    assert "没送达" in out
    assert ask_user.pending_count() == 0


def test_超时返回按默认继续的说明():
    ask_user.set_broadcast(_broadcast(clients=1))   # 有人在线但一直不答
    t0 = time.monotonic()
    out = asyncio.run(ask_user.ask("选哪个？", timeout=0.5))
    assert "没回答" in out and "默认" in out and "不要重复提问" in out
    assert 0.4 <= time.monotonic() - t0 < 3
    assert ask_user.pending_count() == 0


# ---------- 答案配对 ----------

def test_答案回灌到工具结果():
    ask_user.set_broadcast(_broadcast(answer="毫秒 int"))
    out = asyncio.run(ask_user.ask("时间戳存哪种？", options=["毫秒 int", "ISO 串"]))
    assert out == "用户回答：毫秒 int"
    assert ask_user.pending_count() == 0


def test_广播载荷含选项与前缀():
    sent = []
    ask_user.set_broadcast(_broadcast(answer="A", sink=sent))
    asyncio.run(ask_user.ask("选？", options=["A", "B"]))
    ev = sent[0]
    assert ev["type"] == "bridge_confirm"
    assert ev["request_id"].startswith(ask_user.PREFIX)
    assert ev["options"] == ["A", "B"] and ev["ask"] is True
    assert ev["task"] == "选？"


def test_跳过返回默认提示():
    ask_user.set_broadcast(_broadcast(answer=""))   # 空串 = 用户跳过
    out = asyncio.run(ask_user.ask("选？", options=["A", "B"]))
    assert "跳过" in out and "默认" in out


def test_resolve_未知id返回False():
    assert ask_user.resolve("ask_user:nope", "x") is False
    assert ask_user.resolve("", "x") is False


def test_并发提问各自配对():
    async def _t():
        rids = []

        async def fn(ev):
            rids.append(ev["request_id"])
            return 1

        ask_user.set_broadcast(fn)
        tasks = [asyncio.create_task(ask_user.ask(f"q{i}")) for i in range(3)]
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert len(set(rids)) == 3            # 每次提问独立编号
        for i, rid in enumerate(rids):
            ask_user.resolve(rid, f"a{i}")
        return await asyncio.gather(*tasks)

    out = asyncio.run(_t())
    assert sorted(out) == ["用户回答：a0", "用户回答：a1", "用户回答：a2"]


# ---------- 技能注册与路由 ----------

def _load_skill_module():
    path = os.path.join(_ROOT, "skills", "ask", "skill.py")
    spec = importlib.util.spec_from_file_location("ask_skill_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_技能规范合法():
    import agent
    path = os.path.join(_ROOT, "skills", "ask", "skill.json")
    with open(path, encoding="utf-8") as f:
        manifest = json.load(f)
    tool = manifest["tools"][0]
    assert agent._is_valid_tool_spec(tool)
    assert tool["function"]["name"] == "request_user_input"
    assert manifest["disclosure"] == "full"    # 核心交互工具，不能被渐进披露藏起来


def test_处理器是协程且已登记():
    mod = _load_skill_module()
    assert asyncio.iscoroutinefunction(mod.HANDLERS["request_user_input"])


def test_经harness路由真能执行():
    """端到端：技能加载 → 分发 → await 协程 → 结果回灌。"""
    from harness import get_harness
    ask_user.set_broadcast(_broadcast(answer="毫秒"))
    result, source = asyncio.run(
        get_harness().execute_tool("request_user_input", {"question": "存哪种？"}))
    assert source == "skill"
    assert "用户回答：毫秒" in result


def test_harness能认出工具归属():
    from harness import get_harness
    assert get_harness().tool_owner("request_user_input") == ("skill", "ask")


# ---------- Plan Mode：提问是阶段2 的正当动作 ----------

def test_plan_mode放行提问(tmp_path, monkeypatch):
    monkeypatch.setattr(PM, "_path", lambda: str(tmp_path / "plan_mode.json"))
    monkeypatch.setattr(PM, "_uid", lambda: "tester")
    PM.enter("t")
    try:
        assert PM.check("request_user_input") is None      # 提问必须放行
        assert PM.check("code_edit")                        # 写操作照旧被拦
    finally:
        PM.leave()


# ---------- 服务端分流 ----------

def test_端点回填答案():
    import server

    async def _t():
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        ask_user._PENDING["ask_user:unit"] = (fut, loop)
        resp = await server.harness_bridge_confirm(
            {"request_id": "ask_user:unit", "value": "A"})
        await asyncio.sleep(0)     # 跨线程投递是 call_soon_threadsafe，让出一拍
        return resp, fut

    resp, fut = asyncio.run(_t())
    assert resp["status"] == "answered"
    assert fut.done() and fut.result() == "A"


def test_端点未知提问卡404():
    import server
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as ei:
        asyncio.run(server.harness_bridge_confirm(
            {"request_id": "ask_user:ghost", "value": "A"}))
    assert ei.value.status_code == 404


def test_广播函数返回在线数():
    """提问卡靠返回值判断有没有人能看到——必须是数字，不是 None。"""
    import server

    n = asyncio.run(server._gate_broadcast_safe({"type": "bridge_confirm"}))
    assert isinstance(n, int) and n >= 0
