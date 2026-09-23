# -*- coding: utf-8 -*-
"""see_image 回归：来源解析、多图上限、读图能力闸门、失败留痕。

用 importlib 直接加载技能模块，不走 harness 的技能加载（避免测试受技能开关影响）。
"""
import base64
import importlib.util
import io
import os
import sys
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

import pytest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

_SPEC = importlib.util.spec_from_file_location(
    "vision_skill", os.path.join(BASE, "skills", "vision", "skill.py"))
vision = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(vision)


def _png(color=(255, 0, 0), size=(40, 30)) -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "PNG")
    return buf.getvalue()


@pytest.fixture()
def eyes(monkeypatch):
    """强制判定为「看得见图」——本机 settings.json 的读图配置不该影响这些用例。"""
    monkeypatch.setattr(vision, "_status", lambda: (True, "test-model", "测试桩"))


def _marks(out: str) -> list:
    return [ln for ln in out.splitlines() if ln.startswith("[[IMG:")]


def test_local_file_injects_mark(tmp_path, eyes):
    p = tmp_path / "a.png"
    p.write_bytes(_png())
    out = vision.see_image({"images": str(p)})
    marks = _marks(out)
    assert len(marks) == 1, out
    # 本地图同样过内容寻址落盘：注入的是校验过的稳定副本，原文件之后被删也不影响
    copy = marks[0][len("[[IMG:"):-2]
    assert copy.startswith(str(vision._IMG_DIR)) and os.path.isfile(copy), out
    assert open(copy, "rb").read() == p.read_bytes()
    assert out.splitlines()[0].startswith("[[IMG:"), "标记必须在结果开头（结果会被截断）"


def test_data_url_and_dedup(eyes):
    d = "data:image/png;base64," + base64.b64encode(_png()).decode()
    out = vision.see_image({"images": [d, d]})
    assert len(_marks(out)) == 1, "同一张图重复传只该注入一次"


def test_multi_image_cap(tmp_path, eyes):
    items = []
    for i in range(6):
        p = tmp_path / f"m{i}.png"
        p.write_bytes(_png(color=(i * 30 % 255, 0, 0)))
        items.append(str(p))
    out = vision.see_image({"images": items})
    assert len(_marks(out)) == vision._MAX_IMAGES
    assert "未处理" in out


def test_missing_file_keeps_note(eyes):
    out = vision.see_image({"images": "/tmp/definitely-not-here-9x8.png"})
    assert not _marks(out)
    assert "看图失败" in out and "文件不存在" in out


def test_blind_model_refuses(monkeypatch):
    monkeypatch.setattr(vision, "_status", lambda: (False, "blind-model", "名字无视觉线索且无实测记录"))
    out = vision.see_image({"images": "https://example.com/a.png"})
    assert "读图不可用" in out and "blind-model" in out
    assert not _marks(out), "不支持读图却留了标记 —— 会诱导 agent 注入"


def test_http_download(tmp_path, eyes):
    img = tmp_path / "web.png"
    img.write_bytes(_png())

    class H(SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=str(tmp_path), **kw)

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{srv.server_address[1]}/web.png"
        out = vision.see_image({"url": url})
        assert len(_marks(out)) == 1, out
        assert "PNG 40×30" in out
    finally:
        srv.shutdown()
        srv.server_close()


def test_non_image_url_reports_type(tmp_path, eyes):
    (tmp_path / "page.html").write_text("<h1>hi</h1>", encoding="utf-8")

    class H(SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=str(tmp_path), **kw)

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{srv.server_address[1]}/page.html"
        out = vision.see_image({"url": url})
        assert "不是图片" in out and "read_web" in out
    finally:
        srv.shutdown()
        srv.server_close()


def test_sandbox_guard_for_normal_user(tmp_path, eyes):
    import sandbox as sb
    box = tmp_path / "sb"
    box.mkdir()
    outside = tmp_path / "outside.png"
    outside.write_bytes(_png())
    inside = box / "in.png"
    inside.write_bytes(_png(color=(0, 200, 0)))

    token = sb.push(sb.Actor(uid="t1", role=sb.USER_ROLE, sandbox=box))
    try:
        out = vision.see_image({"images": str(outside)})
        assert "沙箱拒绝" in out and not _marks(out), out

        out = vision.see_image({"url": "http://127.0.0.1:9/a.png"})
        assert "内网" in out and not _marks(out), out

        out = vision.see_image({"images": "in.png"})  # 相对路径按沙箱解析
        assert len(_marks(out)) == 1, out
    finally:
        sb.pop(token)


def test_admin_may_read_loopback(eyes):
    """管理员不受内网闸门限制（本机快照是正当需求）。"""
    assert vision._url_guard("http://127.0.0.1:8080/snapshot.jpg") == ""
