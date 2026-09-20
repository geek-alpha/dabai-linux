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



# ---------------- 单次条数上限（2026-09-20） ----------------
# 背景：_append_img_messages 原先只去重、不限量。MCP 侧 _MCP_IMG_MAX_COUNT 已限 4，
# 但这条通道面向所有产出方——一个工具结果塞 20 个标记，就是 20×1024 token 直接
# 灌进上下文（真实触发路径：android 批量截图把 log 拼起来返回）。

def _marks(n: int, prefix: str = "/tmp") -> list:
    return [f"{prefix}/img{i}.png" for i in range(n)]


def _imgs(messages) -> list:
    return [m for m in messages if isinstance(m.get("content"), list)]


def _notes(messages) -> list:
    return [m["content"] for m in messages
            if m.get("role") == "system" and isinstance(m.get("content"), str)]


def test_over_cap_injects_only_cap(fake_img):
    messages = []
    agent._append_img_messages(messages, _marks(20), "screenshot", True)
    assert len(_imgs(messages)) == agent._IMG_MAX_PER_RESULT


def test_over_cap_says_how_many_left(fake_img):
    """丢弃必须留痕：静默丢弃会让模型以为这次只产出了 4 张图。"""
    messages = []
    agent._append_img_messages(messages, _marks(20), "screenshot", True)
    notes = _notes(messages)
    assert len(notes) == 1, notes
    assert "20" in notes[0] and "16" in notes[0]


def test_at_cap_adds_no_note(fake_img):
    """正好等于上限不加提示：白加一条 system 消息会让前缀每轮漂移。"""
    messages = []
    agent._append_img_messages(messages, _marks(agent._IMG_MAX_PER_RESULT),
                               "screenshot", True)
    assert not _notes(messages)
    assert len(_imgs(messages)) == agent._IMG_MAX_PER_RESULT


def test_failed_marks_do_not_waste_quota(monkeypatch):
    """编码失败的标记不占名额：一个失效路径不该让模型少看一张好图。"""
    calls = {"n": 0}
    ok = {"role": "user", "content": [
        {"type": "text", "text": "x"},
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AAA"}},
    ]}

    def flaky(path, name=""):
        calls["n"] += 1
        return None if path.endswith("img0.png") else {"role": "user",
                                                       "content": list(ok["content"])}

    monkeypatch.setattr(agent, "_img_message", flaky)
    messages = []
    agent._append_img_messages(messages, _marks(6), "screenshot", True)
    assert len(_imgs(messages)) == agent._IMG_MAX_PER_RESULT, "失败的标记占了名额"
    assert calls["n"] == agent._IMG_MAX_PER_RESULT + 1


def test_note_counts_into_budget(fake_img):
    """提示语也要算字符当量：漏计就是预算少算一截，且无任何报错。"""
    messages = []
    got = agent._append_img_messages(messages, _marks(20), "screenshot", True)
    assert got > agent._IMG_TOKEN_EST * 4 * agent._IMG_MAX_PER_RESULT


def test_user_upload_gets_larger_cap(fake_img):
    """用户主动上传放宽到 8：截断用户自己发来的图，比截断工具批量产出更糟。"""
    messages = []
    agent._append_img_messages(messages, _marks(8), "用户上传", True,
                               agent._IMG_MAX_USER_UPLOAD)
    assert len(_imgs(messages)) == agent._IMG_MAX_USER_UPLOAD
    assert not _notes(messages)


def test_blind_model_never_hits_cap_note(fake_img):
    """看不见图时不注入，也就不该出现上限提示——提示说「未注入」必须是真的。"""
    messages = []
    agent._append_img_messages(messages, _marks(20), "screenshot", False)
    assert not _imgs(messages)
    assert _notes(messages) == [agent._IMG_NO_EYES]


def test_cap_aligns_with_mcp_side():
    """两边上限必须一致：MCP 放行 4 张、这里再砍一刀，MCP 侧的说明就成了假话。"""
    sys.path.insert(0, os.path.join(BASE, "skills", "mcp"))
    import mcp_client as mc  # noqa: E402
    assert agent._IMG_MAX_PER_RESULT == mc._MCP_IMG_MAX_COUNT


def test_user_upload_cap_aligns_with_server():
    """与 server.UPLOAD_MAX_FILES 对齐，否则用户传 8 张只看得到一半。"""
    import server  # noqa: E402
    assert agent._IMG_MAX_USER_UPLOAD == server.UPLOAD_MAX_FILES
