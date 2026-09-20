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


# ---------------- 图片总量上限（2026-09-20） ----------------
# 背景：_IMG_MAX_PER_RESULT 只管单次。20 轮工具各 4 张 = 80 张 ≈ 81920 token 全留在
# 上下文里，而 _compact_tool_history 原先只压文字、不碰多模态消息——图一多压缩就失效。

def _img_msg(path: str, pad: int = 4000) -> dict:
    return {"role": "user", "content": [
        {"type": "text", "text": f"【android 的图片】{path}"},
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + "A" * pad}},
    ]}


def _weight(ms) -> int:
    """与 _compact_tool_history 内部同一把尺子：多模态按 _IMG_TOKEN_EST、
    文本按 estimate_tokens。混用字符数会让「降级省了多少」量出假差异。"""
    import memory
    return sum(agent._IMG_TOKEN_EST if isinstance(m.get("content"), list)
               else memory.estimate_tokens(str(m.get("content") or "")) for m in ms)


def _heavy_history(rounds: int = 20, per: int = 4, tlen: int = 100) -> list:
    messages = [{"role": "system", "content": "系统"}]
    for r in range(rounds):
        messages.append({"role": "assistant", "content": "", "tool_calls": [
            {"id": f"c{r}", "type": "function",
             "function": {"name": "android", "arguments": '{"command":"annotate"}'}}]})
        messages.append({"role": "tool", "tool_call_id": f"c{r}", "content": "x" * tlen})
        for k in range(per):
            messages.append(_img_msg(f"/tmp/r{r}_{k}.png"))
    return messages


def _dropped(messages) -> list:
    return [m["content"] for m in messages
            if isinstance(m.get("content"), str) and "【图已省略】" in m["content"]]


def test_image_heavy_history_drops_down_to_keep_newest():
    """图主导时压缩必须真降总量：文字压干也回不到预算内，能降的只有图。"""
    messages = _heavy_history()
    before = _weight(messages)
    agent._compact_tool_history(messages, budget=3000,
                                keep_rounds=agent.KEEP_NEWEST_TOOL_ROUNDS)
    assert _weight(messages) < before
    assert len(_imgs(messages)) == agent._IMG_KEEP_NEWEST, "没降到硬下限（或降过头）"
    assert _weight(messages) >= agent._IMG_TOKEN_EST * agent._IMG_KEEP_NEWEST, \
        "预算小于图本身时不该继续降——最近 8 张是模型当前工作所依据的"


def test_dropped_image_keeps_path():
    """省略的是像素，不是路径：模型仍知道看过这张图、文件在哪，能按路径重读。"""
    messages = _heavy_history()
    agent._compact_tool_history(messages, budget=3000,
                                keep_rounds=agent.KEEP_NEWEST_TOOL_ROUNDS)
    dropped = _dropped(messages)
    assert len(dropped) == 80 - agent._IMG_KEEP_NEWEST
    assert any("/tmp/r0_0.png" in c for c in dropped), "最老那张的路径丢了"
    assert any("【android 的图片】" in c for c in dropped), "图注（工具名/路径）丢了"


def test_keeps_newest_images_intact():
    """保留的必须是最近 8 张（当前工作依据），不能反过来砍掉新的。"""
    messages = _heavy_history()
    agent._compact_tool_history(messages, budget=3000,
                                keep_rounds=agent.KEEP_NEWEST_TOOL_ROUNDS)
    kept_text = [str(m["content"]) for m in _imgs(messages)]
    assert any("/tmp/r19_3.png" in t for t in kept_text), "最新一张被降级了"
    assert not any("/tmp/r0_0.png" in t for t in kept_text), "最老一张没被降级"


def test_stops_dropping_once_within_budget():
    """够用就停：只降到回到预算内，不是每次都一刀切降到 8 张。"""
    messages = _heavy_history()
    budget = _weight(messages) - agent._IMG_TOKEN_EST * 3
    agent._compact_tool_history(messages, budget=budget,
                                keep_rounds=agent.KEEP_NEWEST_TOOL_ROUNDS)
    assert len(_imgs(messages)) > agent._IMG_KEEP_NEWEST, "降过头了（够用就停失效）"
    assert _weight(messages) <= budget


def test_under_budget_images_untouched():
    """不超预算时逐字节不动：无谓的降级会让前缀每轮漂移。"""
    messages = _heavy_history(rounds=2, per=2, tlen=50)
    snapshot = [m["content"] for m in messages]
    agent._compact_tool_history(messages, budget=10 ** 9,
                                keep_rounds=agent.KEEP_NEWEST_TOOL_ROUNDS)
    assert [m["content"] for m in messages] == snapshot
    assert len(_imgs(messages)) == 4


def test_downgrade_leaves_plain_text_alone():
    """普通文本 user 消息不能被误降级（_downgrade_img 只认带 image_url 的）。"""
    msgs = [{"role": "user", "content": "普通问题"}]
    assert agent._downgrade_img(msgs[0]) is False
    assert msgs[0]["content"] == "普通问题"
    assert agent._img_is_multimodal({"role": "user", "content": "x"}) is False
    assert agent._img_is_multimodal(_img_msg("/tmp/a.png")) is True


# ---------------- 读不到的图必须留痕（2026-09-20） ----------------
# 背景：_img_message 返回 None（路径不存在/编码失败）时原先静默跳过，模型只看到结果
# 文本里的 [[IMG:路径]]，不知道图根本没进来——会照着不存在的画面下结论。

def _fail_some(*bad):
    """构造「指定路径读不到、其余正常」的 _img_message 替身。"""
    ok = {"role": "user", "content": [
        {"type": "text", "text": "x"},
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AAA"}},
    ]}

    def _f(path, name=""):
        return None if path in bad else {"role": "user", "content": list(ok["content"])}
    return _f


def test_failed_marks_leave_note(monkeypatch):
    """读不到就要说：模型必须知道这张图没进来，不能照路径猜画面。"""
    monkeypatch.setattr(agent, "_img_message", _fail_some("/tmp/img0.png"))
    messages = []
    agent._append_img_messages(messages, _marks(1), "screenshot", True)
    notes = _notes(messages)
    assert len(notes) == 1, notes
    assert "/tmp/img0.png" in notes[0] and "1 张" in notes[0]
    assert not _imgs(messages)


def test_failed_note_lists_at_most_three(monkeypatch):
    """提示语本身不能撑爆上下文：20 个坏路径只列 3 个，其余归成一句。"""
    monkeypatch.setattr(agent, "_img_message", _fail_some(*_marks(20)))
    messages = []
    agent._append_img_messages(messages, _marks(20), "screenshot", True)
    note = _notes(messages)[0]
    assert note.count("/tmp/img") == agent._IMG_FAIL_PATHS_MAX
    assert "等 20 个" in note


def test_failed_note_counts_into_budget(monkeypatch):
    """提示语要算字符当量：漏计就是预算少算一截，且无任何报错。"""
    monkeypatch.setattr(agent, "_img_message", _fail_some("/tmp/img0.png"))
    messages = []
    got = agent._append_img_messages(messages, _marks(1), "screenshot", True)
    assert got == len(_notes(messages)[0]) > 0


def test_all_marks_failed_still_says_so(monkeypatch):
    """全军覆没时更不能沉默：done=0 不是「没什么可说」的理由。"""
    monkeypatch.setattr(agent, "_img_message", _fail_some(*_marks(6)))
    messages = []
    agent._append_img_messages(messages, _marks(6), "screenshot", True)
    assert not _imgs(messages)
    assert "6 张" in _notes(messages)[0]


def test_all_ok_adds_no_fail_note(fake_img):
    """全成功时一个字都不加：白加一条 system 消息会让前缀每轮漂移。"""
    messages = []
    agent._append_img_messages(messages, _marks(2), "screenshot", True)
    assert _notes(messages) == []


def test_fail_note_and_cap_note_coexist(monkeypatch):
    """失败 + 超上限同时发生：两条说明都要有，一条不能盖掉另一条。"""
    monkeypatch.setattr(agent, "_img_message", _fail_some(*_marks(2)))
    messages = []
    agent._append_img_messages(messages, _marks(10), "screenshot", True)
    notes = _notes(messages)
    assert len(notes) == 2, notes
    assert any("未注入" in n for n in notes)
    assert any("上限" in n for n in notes)


# ---------------- 截断不能吃掉 [[IMG:]] 标记（2026-09-20） ----------------
# 背景：_fit_tool_result 在 _img_marks 之前跑（agent.py:422 → 6069），按字符数硬砍。
# 标记被砍成残片 → 正则抽不出路径 → 图静默丢失且没有任何提示，和「读不到不吭声」
# 是同一类病：模型以为这次结果里本来就没有图。

def _head_budget(limit=None):
    limit = limit or agent._TOOL_RESULT_LIMIT
    return limit - min(agent._TRUNC_TAIL, limit // 5) - agent._TRUNC_NOTICE_RESERVE


def test_truncation_keeps_middle_marks():
    """中段被省略时，里面的标记必须补回：文字可以丢，图不能悄悄丢。"""
    raw = "A" * (_head_budget() + 500) + "[[IMG:/tmp/middle.png]]" + "B" * 4000
    out = agent._fit_tool_result(raw, "music_search")
    assert "/tmp/middle.png" in agent._img_marks(out), out[-400:]
    assert len(out) <= agent._TOOL_RESULT_LIMIT


def test_truncation_never_splits_a_mark():
    """跨切口的标记不许砍成残片——残片解析不出路径，图就没了。

    构造要让切口真的落在标记内部（尾部切口 = 长度 - min(600, limit/5)）；
    标记只是落在中段时走的是补回分支，那是另一个用例，这个用例会成假绿。
    """
    tail_len = min(agent._TRUNC_TAIL, agent._TOOL_RESULT_LIMIT // 5)
    m = "[[IMG:/tmp/boundary.png]]"
    raw = "A" * 4000 + m + "B" * (tail_len - 10)
    assert 4000 < len(raw) - tail_len < 4000 + len(m), "构造没跨切口，测不到"
    out = agent._fit_tool_result(raw, "music_search")
    assert "/tmp/boundary.png" in agent._img_marks(out), out[-300:]

    m2 = "[[IMG:/tmp/headcut.png]]"
    head = _head_budget()
    raw2 = "A" * (head - 10) + m2 + "B" * 4000
    assert head - 10 < head < head - 10 + len(m2), "头部构造没跨切口"
    out2 = agent._fit_tool_result(raw2, "music_search")
    assert "/tmp/headcut.png" in agent._img_marks(out2), out2[:300]
    # 残片特征：出现 [[IMG: 的次数多于完整标记数
    assert out2.count("[[IMG:") == len(agent._img_mark_spans(out2))


def test_truncation_marks_capped_with_note():
    """中段标记很多时只补回 4 个，并说明还有多少——提示不能自己撑爆预算。"""
    many = "".join("[[IMG:/tmp/p%02d_%s.png]]" % (i, "x" * 60) for i in range(20))
    out = agent._fit_tool_result("A" * 3000 + many + "B" * 6000, "music_search")
    assert len(agent._img_marks(out)) == agent._IMG_MAX_PER_RESULT
    assert "已省略" in out
    assert len(out) <= agent._TOOL_RESULT_LIMIT


def test_truncation_marks_never_break_the_limit():
    """极端长路径也不许超预算：预算被自己破坏等于截断白做。"""
    many = "".join("[[IMG:/tmp/%s.png]]" % ("x" * 400) for _ in range(6))
    out = agent._fit_tool_result("A" * 3000 + many + "B" * 6000, "music_search")
    assert len(out) <= agent._TOOL_RESULT_LIMIT, len(out)


def test_truncation_without_marks_unchanged():
    """没标记时一个字都不多加：凭空多出的提示会让前缀每轮漂移。"""
    out = agent._fit_tool_result("A" * 9000, "music_search")
    assert "IMG" not in out
    assert out.startswith("A" * 100)


# ---------------- 端到端：截断 → 解析 → 注入（2026-09-20） ----------------
# 背景：截断（_fit_tool_result）、注入上限（_IMG_MAX_PER_RESULT）、失败留痕
# （_IMG_FAIL_NOTE）三段各自有单测，真实链路却是串起来的（agent.py:5931 → 6068）。
# 各自绿而组合漏图，正是刚踩过的坑——所以这里按真实顺序串一次。

def _e2e_raw(limit=None):
    """超长结果：头部 2 个标记（含 1 个坏路径）、中段 1 个、尾部 1 个。

    返回 (原文, 中段标记)。自检写在下面：中段标记必须真落在省略区间，
    否则走不到补回分支，用例会假绿。
    """
    limit = limit or agent._TOOL_RESULT_LIMIT
    tail_len = min(agent._TRUNC_TAIL, limit // 5)
    nom_head = limit - tail_len - agent._TRUNC_NOTICE_RESERVE
    mid = "[[IMG:/tmp/mid.png]]"
    raw = ("A" * 100 + "[[IMG:/tmp/broken.png]]"
           + "A" * 300 + "[[IMG:/tmp/head_ok.png]]"
           + "B" * 2000 + mid + "B" * 2000
           + "C" * 500 + "[[IMG:/tmp/tail_ok.png]]"
           + "D" * (tail_len + 5000))
    assert raw.index(mid) > nom_head, "中段标记没进省略区，测不到补回分支"
    assert raw.index(mid) + len(mid) < len(raw) - tail_len, "中段标记进了尾部保留区"
    assert len(raw) > limit
    return raw, mid


def _e2e_run(raw, img_ok=True):
    """按真实顺序串：截断 → 从截断结果解析标记 → 注入。"""
    fitted = agent._fit_tool_result(raw, "music_search")
    marks = agent._img_marks(fitted)
    messages = []
    added = agent._append_img_messages(messages, marks, "music_search", img_ok)
    return fitted, marks, messages, added


def test_e2e_truncation_to_injection_no_lost_image(monkeypatch):
    """中段标记被补回后必须真的注入——「标记还在、图没进来」是最坏的假象。"""
    monkeypatch.setattr(agent, "_img_message", _fail_some("/tmp/broken.png"))
    raw, _ = _e2e_raw()
    fitted, marks, messages, added = _e2e_run(raw)
    assert marks == ["/tmp/broken.png", "/tmp/head_ok.png", "/tmp/mid.png",
                     "/tmp/tail_ok.png"], marks
    assert len(_imgs(messages)) == 3, [m.get("content") for m in messages]
    notes = _notes(messages)
    assert len(notes) == 1 and "/tmp/broken.png" in notes[0] and "1 张" in notes[0], notes
    # 预算守恒：注入的图 + 提示语都算进去了
    assert added == 3 * agent._IMG_TOKEN_EST * 4 + len(notes[0])
    assert len(fitted) <= agent._TOOL_RESULT_LIMIT


def test_e2e_mark_conservation(fake_img):
    """守恒：解析出的标记全部注入，一个不丢也不凭空多出。"""
    raw, _ = _e2e_raw()
    fitted, marks, messages, added = _e2e_run(raw)
    assert len(marks) == 4, marks
    assert len(_imgs(messages)) == 4
    assert _notes(messages) == []
    assert added == 4 * agent._IMG_TOKEN_EST * 4
    assert len(fitted) <= agent._TOOL_RESULT_LIMIT


def test_e2e_blind_model_never_gets_image(fake_img):
    """看不见图时端到端零 image_url：提示说「未注入」就必须真的没注入。"""
    raw, _ = _e2e_raw()
    fitted, marks, messages, added = _e2e_run(raw, img_ok=False)
    assert not _imgs(messages), "看不见图却注入了 image_url —— 会撞 400"
    assert added == 0
    assert any(m.get("content") == agent._IMG_NO_EYES for m in messages)


def test_e2e_mid_cap_matches_inject_cap(fake_img):
    """中段标记多于补回上限：补回数正好等于注入上限，不多补也不漏补。"""
    limit = agent._TOOL_RESULT_LIMIT
    tail_len = min(agent._TRUNC_TAIL, limit // 5)
    nom_head = limit - tail_len - agent._TRUNC_NOTICE_RESERVE
    many = "".join("[[IMG:/tmp/m%02d_%s.png]]" % (i, "x" * 40) for i in range(9))
    raw = "A" * 500 + "B" * nom_head + many + "C" * (tail_len + 5000)
    fitted = agent._fit_tool_result(raw, "music_search")
    marks = agent._img_marks(fitted)
    assert len(marks) == agent._IMG_MAX_PER_RESULT, marks
    assert "已省略" in fitted
    messages = []
    agent._append_img_messages(messages, marks, "music_search", True)
    assert len(_imgs(messages)) == agent._IMG_MAX_PER_RESULT
    assert _notes(messages) == []


# ---------------- 压缩 / 落盘幂等（2026-09-20） ----------------
# 背景：_compact_tool_history 每轮都会调（agent.py:5252 工具结果入 messages 后、
# agent.py:6102 落断点前），_strip_img_for_disk 也在多处落盘路径上重复调。
# 降级/剥离若不带幂等守卫，第二次调用会静默劣化：降级时 text part 已不在，
# 只剩「工具图片」——路径没了，模型照着看不见的画面下结论；剥离则把同一句
# 「图片已剥离」叠成两句。两者都不报错，只能靠契约测试守。

def _mm(path: str, pad: int = 400) -> dict:
    return {"role": "user", "content": [
        {"type": "text", "text": f"【android 的图片】{path}"},
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + "A" * pad}},
    ]}


def _hist(n_img: int = 12) -> list:
    """n_img 轮「assistant + 工具结果 + 多模态图」，总量远超下面给的小预算。"""
    msgs = [{"role": "system", "content": "系统"}]
    for i in range(n_img):
        msgs.append({"role": "assistant", "content": f"调用工具 {i}"})
        msgs.append({"role": "tool", "tool_call_id": f"c{i}", "content": "x" * 4000})
        msgs.append(_mm(f"/tmp/round{i}.png"))
    return msgs


def _digest(msgs) -> str:
    return "\n".join(str(m.get("content")) for m in msgs)


def test_compact_history_is_idempotent():
    """连调三次收敛到同一状态：路径不丢、条数不增、降级提示不叠加。"""
    msgs = _hist()
    n = len(msgs)
    agent._compact_tool_history(msgs, budget=2000, keep_rounds=1)
    first = _digest(msgs)
    assert "【图已省略】" in first, "预算没触发降级 —— 这个构造测不到幂等"
    for _ in range(2):
        agent._compact_tool_history(msgs, budget=2000, keep_rounds=1)
    assert _digest(msgs) == first, "第二次压缩改动了内容 —— 不幂等"
    assert len(msgs) == n
    assert all(f"/tmp/round{i}.png" in first for i in range(12)), "路径被压缩吃掉"
    assert first.count("【图已省略】") == 12 - agent._IMG_KEEP_NEWEST
    assert sum(1 for m in msgs if isinstance(m.get("content"), list)) == agent._IMG_KEEP_NEWEST


def test_strip_img_for_disk_is_idempotent():
    """剥离连调两次零变化，且不破坏原 messages（base64 只在副本里被剥）。"""
    msgs = _hist(3)
    once = agent._strip_img_for_disk(msgs)
    twice = agent._strip_img_for_disk(once)
    assert _digest(twice) == _digest(once), "第二次剥离又拼了一句说明"
    assert len(once) == len(twice) == len(msgs) == 10
    assert _digest(once).count("图片已剥离") == 3
    assert not any(isinstance(m.get("content"), list) for m in once), "base64 没剥干净"
    assert all(f"/tmp/round{i}.png" in _digest(once) for i in range(3))
    assert sum(1 for m in msgs if isinstance(m.get("content"), list)) == 3, "原列表被就地改了"


def test_downgraded_text_survives_compact_and_strip():
    """跨函数：降级后的文本再走 strip 不重复包装，路径仍可重读。"""
    msgs = _hist(12)
    agent._compact_tool_history(msgs, budget=2000, keep_rounds=1)
    stripped = agent._strip_img_for_disk(msgs)
    txt = _digest(stripped)
    assert txt.count("【图已省略】") == 12 - agent._IMG_KEEP_NEWEST
    assert txt.count("图片已剥离") == agent._IMG_KEEP_NEWEST
    assert all(f"/tmp/round{i}.png" in txt for i in range(12))
