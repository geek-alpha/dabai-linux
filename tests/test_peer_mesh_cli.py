"""peer_mesh.py 命令行的退出码：失败必须让调用方看得见。

真实翻车（v1.1.1 发布时）：publish.py 的 ⑦ 靠子进程退出码判「留言成功」，
而 say 分支无论对面回什么都是 return 0 —— orangepi 密钥不匹配、服务端回
403 forbidden，发布日志却写着「✓ orangepi：已留言」。假成功比失败更坏：
失败会有人去查，假成功不会。
"""
import importlib.util
import json
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]


def _load():
    spec = importlib.util.spec_from_file_location("peer_mesh_cli_under_test", BASE / "peer_mesh.py")
    pm = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pm)
    return pm


def test_say_cli_nonzero_when_forbidden(monkeypatch, capsys):
    pm = _load()
    monkeypatch.setattr(pm, "say", lambda *a, **k: {
        "ok": False, "error": "bad or missing key", "code": "forbidden"})
    rc = pm._cli(["say", "orangepi", "hello", "--kind", "release"])
    assert rc == 1
    assert json.loads(capsys.readouterr().out)["error"] == "bad or missing key"


def test_say_cli_zero_on_success(monkeypatch, capsys):
    pm = _load()
    monkeypatch.setattr(pm, "say", lambda *a, **k: {"ok": True, "delivered": True})
    assert pm._cli(["say", "rpi", "hello"]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True


def test_say_cli_still_prints_body_on_failure(monkeypatch, capsys):
    """退出码之外还得留下原文 —— 失败原因（forbidden / unreachable）是排障入口。"""
    pm = _load()
    monkeypatch.setattr(pm, "say", lambda *a, **k: {"ok": False, "error": "unreachable"})
    pm._cli(["say", "rpi", "hi"])
    assert "unreachable" in capsys.readouterr().out


def test_say_cli_usage_error(monkeypatch):
    pm = _load()
    assert pm._cli(["say", "only-node-no-text"]) == 2
