# -*- coding: utf-8 -*-
"""自我迭代轮「注入主对话流」的契约。

为什么钉死：这一轮从后台子智能体搬到了主人眼前的对话流，判据只有三条能被证伪——
  ① 主人正在对话时必须让位（不抢轮、不扣预算、不注入）；
  ② 注入的那一轮必须静音且不进记忆（任务书不是主人说的话）；
  ③ 前端必须拿到「第 N 轮开始」的横幅，否则主人还是只看到对话里凭空多出一段。
"""
import asyncio

import pytest


class _Running:
    def done(self):
        return False


class _State:
    user_id = "default"
    current_model = None
    current_background = None
    current_bgm = None

    def __init__(self):
        self.active_task = None
        self._ws_history = []
        self.n = 0

    def new_session(self):
        self.n += 1
        return "sid-%d" % self.n


@pytest.fixture()
def env(monkeypatch):
    import server
    import scheduler
    import tools.self_iterate as si

    calls = {"released": [], "sent": [], "stream": [], "noted": []}

    monkeypatch.setattr(scheduler, "release_running",
                        lambda jid, note="": (calls["released"].append((jid, note)), ({}, None))[1])

    async def _send(ws, payload):
        calls["sent"].append(payload)

    monkeypatch.setattr(server, "safe_send_json", _send)
    monkeypatch.setattr(server, "_register_active_turn", lambda *a, **kw: None)

    async def _stream(ws, text, history, sid, state, **kw):
        calls["stream"].append({"text": text, "sid": sid, "kw": kw})

    monkeypatch.setattr(server, "handle_user_message_stream", _stream)
    monkeypatch.setattr(si, "note_dispatch_result", lambda text: calls["noted"].append(text))
    server._SELF_ITERATE_TURN.clear()
    yield server, si, calls
    server._SELF_ITERATE_TURN.clear()


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        out = loop.run_until_complete(coro)
        loop.run_until_complete(asyncio.sleep(0))  # 让注入的对话轮 task 跑完
        return out
    finally:
        loop.close()


def _job():
    return {"id": "sched-1", "name": "自我迭代循环"}


def test_主人正在对话_让位不注入(env):
    server, si, calls = env
    st = _State()
    st.active_task = _Running()
    ok = _run(server._self_iterate_fire_in_chat(_job(), st))
    assert ok is False
    assert calls["stream"] == []
    assert calls["released"] and calls["released"][0][0] == "sched-1"


def test_上一轮没收尾_让位(env):
    server, si, calls = env
    server._SELF_ITERATE_TURN["active"] = True
    ok = _run(server._self_iterate_fire_in_chat(_job(), _State()))
    assert ok is False and calls["stream"] == []
    assert calls["released"], "让位必须解掉 running 标记，否则下一轮也派不出去"


def test_正常轮_注入且静音不进记忆(env, monkeypatch):
    server, si, calls = env
    monkeypatch.setattr(server, "_self_iterate_tick",
                        lambda job: {"running": True, "dispatched": 2, "budget_rounds": 6})
    monkeypatch.setattr(si, "brief", lambda: "【自我迭代 · 第 2/6 轮】\n目标缺口：x")
    ok = _run(server._self_iterate_fire_in_chat(_job(), _State()))
    assert ok is True
    assert len(calls["stream"]) == 1
    kw = calls["stream"][0]["kw"]
    assert kw["speak"] is False, "任务书不该被朗读"
    assert kw["msg_source"] == "auto", "任务书不是主人说的话，不许触发用户记忆抽取"
    assert kw["record_history"] is False, "迭代过程不进主人对话上下文"
    assert server._SELF_ITERATE_TURN.get("round") == 2
    assert any("自我迭代" in str(p.get("text") or "") for p in calls["sent"]), \
        "前端必须收到「第 N 轮开始」横幅"


def test_任务书报停_不注入(env, monkeypatch):
    server, si, calls = env
    monkeypatch.setattr(server, "_self_iterate_tick",
                        lambda job: {"running": True, "dispatched": 1, "budget_rounds": 6})
    monkeypatch.setattr(si, "brief", lambda: "⛔ 自我迭代已停：预算耗尽")
    ok = _run(server._self_iterate_fire_in_chat(_job(), _State()))
    assert ok is False and calls["stream"] == []
    assert calls["noted"], "任务书报停要记账，别让预算悬着"


def test_轮末收尾_清标记并记账(env):
    """标记不清，后面每一轮对话收尾都会误判成迭代轮；记账是环境阻塞退回预算的唯一入口。"""
    server, si, calls = env
    server._SELF_ITERATE_TURN.update({"active": True, "round": 3, "job_id": "sched-1"})
    _run(server._self_iterate_turn_done("余额不足 402", interrupted=False))
    assert not server._SELF_ITERATE_TURN.get("active")
    assert calls["noted"] == ["余额不足 402"]
    assert any("第 3 轮结束" in str(p.get("text") or "") for p in calls["sent"])
