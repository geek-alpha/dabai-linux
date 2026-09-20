# -*- coding: utf-8 -*-
"""MCP 图片回灌：image content 落盘 + [[IMG:]] 标记，base64 绝不进上下文。

背景（2026-09-20 实测）：MCP 的 image content 原先走 json.dumps 拍平 —— 一张
200x200 PNG 的 base64 就 784 字符全灌进工具结果，模型读不出画面，上下文先被
撑爆（真实截图 1.7MB → 230 万字符）。修法是落盘 + 标记，复用 agent 的多模态通道。
"""
import base64
import io
import os
import sys

import pytest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "skills", "mcp"))

import mcp_client as mc  # noqa: E402
import agent  # noqa: E402


def _png(size=(200, 200), color=(255, 0, 0)) -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "PNG")
    return buf.getvalue()


def _img_item(raw: bytes, mime="image/png") -> dict:
    return {"type": "image", "data": base64.b64encode(raw).decode(), "mimeType": mime}


@pytest.fixture()
def imgdir(tmp_path, monkeypatch):
    monkeypatch.setattr(mc, "_MCP_IMG_DIR", str(tmp_path / "mcp_images"))
    return tmp_path / "mcp_images"


def test_plain_text_unchanged(imgdir):
    out = mc.format_tool_result({"content": [{"type": "text", "text": "hello"}]})
    assert out == "hello"


def test_structured_content_fallback(imgdir):
    """回归 fc1b131：只给 structuredContent 的 server 不能返回「（工具无输出）」。"""
    out = mc.format_tool_result({"content": [], "structuredContent": {"a": 1}})
    assert out == '{"a": 1}'


def test_is_error_prefix_kept(imgdir):
    out = mc.format_tool_result({"isError": True, "content": [{"type": "text", "text": "炸了"}]})
    assert out.startswith("[工具报错]")


def test_image_lands_on_disk_and_marks(imgdir):
    raw = _png()
    out = mc.format_tool_result({"content": [
        {"type": "text", "text": "已截图"}, _img_item(raw)]})
    paths = agent._img_marks(out)
    assert len(paths) == 1, out
    assert os.path.isfile(paths[0])
    with open(paths[0], "rb") as f:
        assert f.read() == raw, "落盘字节与 server 返回的不一致"
    assert paths[0].endswith(".png")
    assert "已截图" in out


def test_base64_never_enters_context(imgdir):
    """核心断言：图片再大，进上下文的也只是几十字符的标记。"""
    raw = _png(size=(900, 900))
    assert len(raw) > 3000
    out = mc.format_tool_result({"content": [_img_item(raw)]})
    assert len(out) < 400, f"输出 {len(out)} 字符，base64 灌进来了：{out[:120]}"
    assert base64.b64encode(raw).decode()[:64] not in out


def test_mark_survives_truncation(imgdir):
    """结果会被截断（前端 2000 / 旧轮压缩 250 字符），标记必须在最前面。"""
    out = mc.format_tool_result({"content": [
        _img_item(_png()), {"type": "text", "text": "后" * 5000}]})
    assert out.startswith("[[IMG:"), out[:80]
    assert "[[IMG:" in out[:60], "标记太靠后，截断后会被切掉"


def test_agent_can_consume_the_mark(imgdir):
    """跨模块契约：落盘路径必须真能被 agent 的多模态通道抽出来。"""
    out = mc.format_tool_result({"content": [_img_item(_png())]})
    marks = agent._img_marks(out)
    assert marks and os.path.isabs(marks[0])
    url = agent._img_data_url(marks[0])
    assert url.startswith("data:image/jpeg;base64,"), "agent 读不出落盘图，通道是断的"


def test_same_image_written_once(imgdir):
    raw = _png()
    out1 = mc.format_tool_result({"content": [_img_item(raw)]})
    out2 = mc.format_tool_result({"content": [_img_item(raw)]})
    assert agent._img_marks(out1) == agent._img_marks(out2), "同图应命中同一路径"
    assert len(list(imgdir.glob("*.png"))) == 1
    assert not list(imgdir.glob("*.tmp*")), "临时文件没清干净"


def test_oversize_image_skipped(imgdir, monkeypatch):
    monkeypatch.setattr(mc, "_MCP_IMG_MAX_BYTES", 1024)
    out = mc.format_tool_result({"content": [_img_item(_png(size=(400, 400)))]})
    assert agent._img_marks(out) == []
    assert "超过单图上限" in out
    assert not imgdir.exists() or not list(imgdir.glob("*"))


def test_count_cap_keeps_context_bounded(imgdir):
    items = [_img_item(_png(color=(i, 0, 0))) for i in range(mc._MCP_IMG_MAX_COUNT + 1)]
    out = mc.format_tool_result({"content": items})
    assert len(agent._img_marks(out)) == mc._MCP_IMG_MAX_COUNT
    assert "单次上限" in out


def test_bad_base64_degrades_without_raising(imgdir):
    out = mc.format_tool_result({"content": [
        {"type": "image", "data": "不是base64!!!", "mimeType": "image/png"}]})
    assert agent._img_marks(out) == []
    assert "base64" in out


def test_audio_blob_not_dumped(imgdir):
    """音频等二进制同样不能灌 base64 —— 只是从「读不出」变成「读不出还撑爆」。"""
    out = mc.format_tool_result({"content": [
        {"type": "audio", "data": "A" * 2000, "mimeType": "audio/wav"}]})
    assert len(out) < 200, out
    assert "A" * 100 not in out
    assert "未注入" in out


def test_resource_blob_image_dumped(imgdir):
    raw = _png()
    out = mc.format_tool_result({"content": [{"type": "resource", "resource": {
        "uri": "file:///x.png", "mimeType": "image/png",
        "blob": base64.b64encode(raw).decode()}}]})
    marks = agent._img_marks(out)
    assert len(marks) == 1
    with open(marks[0], "rb") as f:
        assert f.read() == raw


def test_resource_non_image_blob_reported_only(imgdir):
    out = mc.format_tool_result({"content": [{"type": "resource", "resource": {
        "uri": "file:///x.pdf", "mimeType": "application/pdf", "blob": "B" * 2000}}]})
    assert len(out) < 200, out
    assert "未注入" in out


def test_disk_failure_degrades(imgdir, monkeypatch):
    """落盘失败必须降级成一句说明，绝不能抛异常打断工具调用。"""
    def boom(*a, **kw):
        raise OSError("disk full")
    monkeypatch.setattr(mc.os, "makedirs", boom)
    out = mc.format_tool_result({"content": [
        {"type": "text", "text": "已截图"}, _img_item(_png())]})
    assert "已截图" in out and "落盘失败" in out
    assert agent._img_marks(out) == []


def test_prune_keeps_dir_bounded(imgdir, monkeypatch):
    """截图每次内容都不同，内容寻址不收敛 —— 目录必须有上限。"""
    monkeypatch.setattr(mc, "_MCP_IMG_KEEP", 5)
    imgdir.mkdir(parents=True, exist_ok=True)
    for i in range(8):
        p = imgdir / f"old{i}.png"
        p.write_bytes(b"x")
        os.utime(p, (1000 + i, 1000 + i))
    out = mc.format_tool_result({"content": [_img_item(_png())]})
    fresh = agent._img_marks(out)
    assert fresh and os.path.isfile(fresh[0]), "新图必须落盘"
    left = list(imgdir.iterdir())
    assert len(left) == mc._MCP_IMG_KEEP // 2, [p.name for p in left]
    assert os.path.isfile(fresh[0]), "清理把刚写的图删了"


def test_prune_not_triggered_below_cap(imgdir):
    imgdir.mkdir(parents=True, exist_ok=True)
    for i in range(3):
        (imgdir / f"k{i}.png").write_bytes(b"x")
    mc.format_tool_result({"content": [_img_item(_png())]})
    assert len(list(imgdir.iterdir())) == 4
