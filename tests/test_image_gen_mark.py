"""AI 画图的产出必须能被模型自己看见（[[IMG:]] 标记）。

背景（2026-09-20）：create_image 原先只回 `/generated/xxx.png` 访问链接——前端和大屏
能显示，模型看不见自己刚画的东西，无法自检质量（画错手、错字只能等用户发现）。
同 MCP 图片回灌一样，补在产出端即可，agent 的多模态通道早就通了。
"""
import base64
import io
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "skills" / "media"))

import image_gen_impl as ig  # noqa: E402
import agent  # noqa: E402


def _png(size=(64, 64)):
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", size, (200, 30, 60)).save(buf, "PNG")
    return buf.getvalue()


class _Resp:
    def __init__(self, payload):
        self.status_code = 200
        self._payload = payload
        self.text = str(payload)[:500]

    def json(self):
        return self._payload

    def raise_for_status(self):
        return None


@pytest.fixture
def gen(tmp_path, monkeypatch):
    """把输出目录指到 tmp_path，并挡掉任务登记（不碰真实任务库）。"""
    out = tmp_path / "generated"
    monkeypatch.setattr(ig, "OUT_DIR", out)
    monkeypatch.setattr(ig, "_register_task", lambda *a, **kw: None)
    return out


def _fake_requests(raw, status=200, payload=None):
    class _Fake:
        @staticmethod
        def post(url, **kw):
            r = _Resp(payload if payload is not None else {
                "images": [{"url": "data:image/png;base64,"
                                   + base64.b64encode(raw).decode()}]})
            r.status_code = status
            return r

    return _Fake


def test_success_returns_img_mark(gen, monkeypatch):
    raw = _png()
    monkeypatch.setattr(ig, "requests", _fake_requests(raw))
    out = ig._generate_blocking("一只凤凰", "1024x1024",
                                {"base_url": "https://x", "model": "m", "api_key": "k"})
    marks = agent._img_marks(out)
    assert len(marks) == 1, out
    p = Path(marks[0])
    assert p.is_absolute(), f"标记必须是绝对路径，agent 才能读：{p}"
    assert p.is_file() and p.read_bytes() == raw, "落盘字节与接口返回不一致"
    assert p.parent == gen, f"图必须落在 OUT_DIR 里：{p}"
    assert "/generated/" in out, "人类可读的访问链接不能丢"


def test_agent_can_actually_read_the_mark(gen, monkeypatch):
    """跨模块契约：标记路径必须真能被 agent 的多模态通道编码成 data URL。"""
    monkeypatch.setattr(ig, "requests", _fake_requests(_png()))
    out = ig._generate_blocking("x", "1024x1024",
                                {"base_url": "https://x", "model": "m", "api_key": "k"})
    url = agent._img_data_url(agent._img_marks(out)[0])
    assert url.startswith("data:image/jpeg;base64,"), "agent 读不出生成的图，通道是断的"


def test_no_mark_when_api_fails(gen, monkeypatch):
    """接口报错时绝不能带标记 —— 那等于让模型看一张不存在的图。"""
    monkeypatch.setattr(ig, "requests", _fake_requests(
        b"", status=500, payload={"error": "boom"}))
    out = ig._generate_blocking("x", "1024x1024",
                                {"base_url": "https://x", "model": "m", "api_key": "k"})
    assert agent._img_marks(out) == []
    assert "500" in out
    assert not gen.exists() or not list(gen.iterdir())


def test_no_mark_when_save_fails(gen, monkeypatch):
    monkeypatch.setattr(ig, "requests", _fake_requests(b"not a png",
                                                       payload={"images": [{"url": "http://a/b.png"}]}))

    class _BadGet:
        @staticmethod
        def post(url, **kw):
            return _Resp({"images": [{"url": "http://a/b.png"}]})

        @staticmethod
        def get(url, **kw):
            raise OSError("network down")

    monkeypatch.setattr(ig, "requests", _BadGet)
    out = ig._generate_blocking("x", "1024x1024",
                                {"base_url": "https://x", "model": "m", "api_key": "k"})
    assert agent._img_marks(out) == []
    assert "保存失败" in out


def test_no_key_no_mark(gen):
    out = ig._generate_blocking("x", "1024x1024",
                                {"base_url": "https://x", "model": "m", "api_key": ""})
    assert agent._img_marks(out) == []
    assert "API Key" in out
