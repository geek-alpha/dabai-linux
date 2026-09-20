"""联邦离线投递（发件箱）：对方不在线，也不能把消息悄悄丢掉。

真实缺口：say() 就是一次 POST，orangepi 密钥不符时发布通知直接失败 ——
「服务器还在，但没人收到」这件事只能靠人记得重发。发件箱把投递变成至少一次。
"""
import importlib.util
import time
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]


def _load(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("peer_mesh_outbox_under_test",
                                                  BASE / "peer_mesh.py")
    pm = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pm)
    monkeypatch.setattr(pm, "OUTBOX_FILE", tmp_path / "peer_outbox.jsonl")
    monkeypatch.setattr(pm, "NODE_FILE", tmp_path / "node.json")
    monkeypatch.setattr(pm, "KEY_FILE", tmp_path / "cluster.key")
    return pm


def test_say_failure_queues_message(tmp_path, monkeypatch):
    pm = _load(tmp_path, monkeypatch)
    monkeypatch.setattr(pm, "call", lambda *a, **k: {
        "ok": False, "error": "bad or missing key", "code": "forbidden"})
    r = pm.say("orangepi", "v1.1.4 已发布", kind="release")
    assert r["queued"] is True
    items = pm.outbox_items()
    assert len(items) == 1
    assert items[0]["node_id"] == "orangepi"
    assert items[0]["kind"] == "release"
    assert items[0]["attempts"] == 0


def test_unknown_node_is_not_queued(tmp_path, monkeypatch):
    """地址簿里没有的节点，重投一万次也还是不知道往哪发。"""
    pm = _load(tmp_path, monkeypatch)
    monkeypatch.setattr(pm, "call", lambda *a, **k: {
        "ok": False, "error": "unknown node: nope", "known": ["rpi"]})
    r = pm.say("nope", "hi")
    assert "queued" not in r
    assert pm.outbox_items() == []


def test_duplicate_is_not_queued_twice(tmp_path, monkeypatch):
    """发布脚本重跑不该把同一条通知堆成 N 份。"""
    pm = _load(tmp_path, monkeypatch)
    monkeypatch.setattr(pm, "call", lambda *a, **k: {"ok": False, "error": "unreachable"})
    pm.say("orangepi", "v1.1.4 已发布", kind="release")
    pm.say("orangepi", "v1.1.4 已发布", kind="release")
    assert len(pm.outbox_items()) == 1


def test_flush_delivers_and_clears(tmp_path, monkeypatch):
    pm = _load(tmp_path, monkeypatch)
    monkeypatch.setattr(pm, "call", lambda *a, **k: {"ok": False, "error": "unreachable"})
    pm.say("orangepi", "派活")
    monkeypatch.setattr(pm, "call", lambda *a, **k: {"ok": True})
    r = pm.flush_outbox()
    assert r["sent"] == 1 and r["pending"] == 0
    assert pm.outbox_items() == []


def test_flush_resigns_with_fresh_ts(tmp_path, monkeypatch):
    """签名绑 ts 且只有 5 分钟时间窗：原样重发旧消息，对面一律拒收。"""
    pm = _load(tmp_path, monkeypatch)
    monkeypatch.setattr(pm, "call", lambda *a, **k: {"ok": False, "error": "unreachable"})
    pm.say("orangepi", "派活")
    seen = {}

    def spy(node_id, path, payload, timeout):
        seen.update(payload)
        return {"ok": True}

    monkeypatch.setattr(pm, "call", spy)
    pm.flush_outbox()
    assert abs(int(seen["ts"]) - int(time.time())) < 5
    assert seen["sig"] == pm.sign(seen["from"], seen["ts"], seen["text"])


def test_flush_backs_off_after_failure(tmp_path, monkeypatch):
    pm = _load(tmp_path, monkeypatch)
    monkeypatch.setattr(pm, "call", lambda *a, **k: {"ok": False, "error": "unreachable"})
    pm.say("orangepi", "派活")
    now = time.time() + 1
    r = pm.flush_outbox(now=now)
    assert r["sent"] == 0 and r["pending"] == 1
    it = pm.outbox_items()[0]
    assert it["attempts"] == 1
    assert it["next_try_at"] >= now + pm.OUTBOX_BACKOFF[0] - 1
    assert pm.flush_outbox(now=now + 5)["pending"] == 1
    assert pm.outbox_items()[0]["attempts"] == 1


def test_flush_drops_expired(tmp_path, monkeypatch):
    """过期不再投：陈旧的派活指令诈尸比丢了更坏。"""
    pm = _load(tmp_path, monkeypatch)
    monkeypatch.setattr(pm, "call", lambda *a, **k: {"ok": False, "error": "unreachable"})
    pm.say("orangepi", "陈年指令")
    r = pm.flush_outbox(now=time.time() + pm.OUTBOX_TTL + 10)
    assert r["dropped"] == 1 and r["pending"] == 0


def test_queue_caps_size_dropping_oldest(tmp_path, monkeypatch):
    pm = _load(tmp_path, monkeypatch)
    monkeypatch.setattr(pm, "call", lambda *a, **k: {"ok": False, "error": "unreachable"})
    monkeypatch.setattr(pm, "OUTBOX_MAX", 3)
    for i in range(5):
        pm.say("orangepi", f"第{i}条")
    items = pm.outbox_items()
    assert len(items) == 3
    assert items[-1]["text"] == "第4条"


def test_survey_marks_key_mismatch_as_reachable(tmp_path, monkeypatch):
    """服务活着但密钥不符 ≠ 离线：403 是对方进程自己回的，它必须活着才回得出。"""
    pm = _load(tmp_path, monkeypatch)
    monkeypatch.setattr(pm, "peers", lambda: {
        "orangepi": {"url": "https://x", "label": "香橙派"}})
    monkeypatch.setattr(pm, "whoami", lambda n, t: {
        "ok": False, "error": "bad or missing key", "code": "forbidden"})
    row = pm.survey()[0]
    assert row["online"] is False and row["reachable"] is True
    assert "cluster.key" in row["hint"]


def test_survey_unreachable_is_not_reachable(tmp_path, monkeypatch):
    pm = _load(tmp_path, monkeypatch)
    monkeypatch.setattr(pm, "peers", lambda: {"rpi": {"url": "https://x", "label": "树莓派"}})
    monkeypatch.setattr(pm, "whoami", lambda n, t: {"ok": False, "error": "URLError: timed out"})
    row = pm.survey()[0]
    assert row["reachable"] is False
    assert "hint" not in row
