# -*- coding: utf-8 -*-
"""图片注入决策回归：看不见图时只留提示，绝不注入 image_url。

背景：原先两处注入分支都缺 _img_ok 守卫 —— 加了「图片未注入」提示之后，
紧跟着的循环照样把 image_url 塞进请求体，非视觉模型必然撞提供方 400。
"""
import os
import sys

import pytest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import agent  # noqa: E402


@pytest.fixture()
def fake_img(monkeypatch):
    monkeypatch.setattr(agent, "_img_message",
                        lambda path, name="": {"role": "user", "content": [
                            {"type": "text", "text": path},
                            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AAA"}},
                        ]})


def _has_image_url(messages) -> bool:
    for m in messages:
        c = m.get("content")
        if isinstance(c, list) and any(isinstance(p, dict) and p.get("type") == "image_url" for p in c):
            return True
    return False


def test_blind_model_gets_hint_not_image(fake_img):
    messages = []
    got = agent._append_img_messages(messages, ["/tmp/a.png"], "screenshot", False)
    assert got == 0
    assert not _has_image_url(messages), "看不见图却注入了 image_url —— 会撞 400"
    assert any(m.get("content") == agent._IMG_NO_EYES for m in messages)


def test_sighted_model_gets_image(fake_img):
    messages = []
    got = agent._append_img_messages(messages, ["/tmp/a.png"], "screenshot", True)
    assert _has_image_url(messages)
    assert got == agent._IMG_TOKEN_EST * 4
    assert not any(m.get("content") == agent._IMG_NO_EYES for m in messages)


def test_no_marks_touches_nothing(fake_img):
    messages = []
    assert agent._append_img_messages(messages, [], "screenshot", False) == 0
    assert messages == []


def test_marks_parsed_from_tool_result():
    assert agent._img_marks("✓ 已保存\n[[IMG:/tmp/x.png]]\n") == ["/tmp/x.png"]
    assert agent._img_marks("[[IMG:/a.png]] [[IMG:/a.png]] [[IMG:/b.png]]") == ["/a.png", "/b.png"]
    assert agent._img_marks("没有标记") == []


def test_compact_still_fires_with_images():
    """多模态消息按 _IMG_TOKEN_EST 计入预算后，轮内压缩必须照常触发。

    实测（2026-09-13）：6 轮工具 + 6 张图，当量 30146 → 压缩后 9782，
    多模态消息 6 条一条不少（压缩只动文字，不碰图）。
    """
    img = {"role": "user", "content": [
        {"type": "text", "text": "图"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64," + "A" * 4000}},
    ]}
    messages = [{"role": "system", "content": "系统"}]
    for i in range(6):
        messages.append({"role": "assistant", "content": "", "tool_calls": [
            {"id": f"c{i}", "type": "function",
             "function": {"name": "android", "arguments": '{"command":"annotate"}'}}]})
        messages.append({"role": "tool", "tool_call_id": f"c{i}", "content": "x" * 4000})
        messages.append(dict(img))

    def _weight(ms):
        return sum(agent._IMG_TOKEN_EST if isinstance(m.get("content"), list)
                   else len(str(m.get("content"))) for m in ms)

    before = _weight(messages)
    agent._compact_tool_history(messages, budget=agent._IMG_TOKEN_EST * 3,
                               keep_rounds=agent.KEEP_NEWEST_TOOL_ROUNDS)
    after = _weight(messages)
    assert after < before, "1024 估算下压缩没触发"
    assert sum(1 for m in messages if isinstance(m.get("content"), list)) == 6, \
        "压缩动了多模态消息（图被删/被改）"
