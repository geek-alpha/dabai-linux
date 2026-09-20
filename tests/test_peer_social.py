"""社会层端到端：两个真实实例（A 进程内 / B 子进程 uvicorn），走真 HTTP。

验的是三件在单机 mock 里验不出来的事：
  ① 去中心化发现：A 从没被告知 C 的地址，只跟 B 交换一次名册就学到了 C
  ② 单向朋友圈：A 加 B 不需要 B 同意，但 B 会收到一条通知
  ③ 互动闭环：A 发动态 → B 收到；B 评论 → A 收到
"""
from __future__ import annotations

import json
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import peer_mesh as pm  # noqa: E402
import peer_social as ps  # noqa: E402

BOOTSTRAP = '''
import json, sys
from pathlib import Path
root = Path(sys.argv[1]); port = int(sys.argv[2]); data = root / "b"
sys.path.insert(0, sys.argv[3])
import peer_mesh as pm, peer_social as ps
data.mkdir(parents=True, exist_ok=True)
pm.DATA_DIR = data
pm.KEY_FILE = root / "cluster.key"
for name, fn in (("NODE_FILE", "node.json"), ("PEERS_FILE", "peers.json"),
                 ("INBOX_FILE", "peer_inbox.jsonl"), ("CURSOR_FILE", "peer_cursor.json"),
                 ("WATCH_CURSOR_FILE", "peer_watch_cursor.json"),
                 ("TASK_LOG_FILE", "peer_tasks.jsonl"),
                 ("TASK_QUOTA_FILE", "peer_task_quota.json"),
                 ("OUTBOX_FILE", "peer_outbox.jsonl")):
    setattr(pm, name, data / fn)
ps.DATA_DIR = data
for name, fn in (("ROSTER_FILE", "peer_roster.json"), ("FRIENDS_FILE", "friends.json"),
                 ("FEED_FILE", "feed.jsonl"), ("SOCIAL_STATE_FILE", "social_state.json")):
    setattr(ps, name, data / fn)
(data / "node.json").write_text(json.dumps(
    {"node_id": "b", "label": "B", "url": f"http://127.0.0.1:{port}"}), encoding="utf-8")
# B 的名册里预置一台 C：A 从没听说过 C，只能靠 gossip 学到
(data / "peer_roster.json").write_text(json.dumps({"nodes": {
    "c": {"url": "http://127.0.0.1:9", "label": "C", "last_seen": 4102444800}}}),
    encoding="utf-8")
import uvicorn
from fastapi import FastAPI
app = FastAPI()
app.include_router(pm.router)
app.include_router(ps.router)
uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
'''


def _isolate(tmp: Path, node_id: str, port: int, monkeypatch) -> Path:
    """把 A 这台实例的落盘路径全指到 tmp —— 别碰到真实的联邦数据。"""
    data = tmp / "a"
    data.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(pm, "DATA_DIR", data)
    monkeypatch.setattr(pm, "KEY_FILE", tmp / "cluster.key")
    for name, fn in (("NODE_FILE", "node.json"), ("PEERS_FILE", "peers.json"),
                     ("INBOX_FILE", "peer_inbox.jsonl"), ("CURSOR_FILE", "peer_cursor.json"),
                     ("WATCH_CURSOR_FILE", "peer_watch_cursor.json"),
                     ("TASK_LOG_FILE", "peer_tasks.jsonl"),
                     ("TASK_QUOTA_FILE", "peer_task_quota.json"),
                     ("OUTBOX_FILE", "peer_outbox.jsonl")):
        monkeypatch.setattr(pm, name, data / fn)
    monkeypatch.setattr(ps, "DATA_DIR", data)
    for name, fn in (("ROSTER_FILE", "peer_roster.json"), ("FRIENDS_FILE", "friends.json"),
                     ("FEED_FILE", "feed.jsonl"), ("SOCIAL_STATE_FILE", "social_state.json")):
        monkeypatch.setattr(ps, name, data / fn)
    (data / "node.json").write_text(json.dumps(
        {"node_id": node_id, "label": node_id.upper(), "url": f"http://127.0.0.1:{port}"}),
        encoding="utf-8")
    return data


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _wait(port: int, timeout: float = 30.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/docs", timeout=1):
                return
        except Exception:
            time.sleep(0.3)
    raise AssertionError(f"B 实例没起来（port {port}）")


@pytest.fixture()
def two_nodes(tmp_path, monkeypatch):
    port = _free_port()
    boot = tmp_path / "b_boot.py"
    boot.write_text(BOOTSTRAP, encoding="utf-8")
    (tmp_path / "cluster.key").write_bytes(b"x" * 32)
    proc = subprocess.Popen([sys.executable, str(boot), str(tmp_path), str(port), str(BASE)],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        _wait(port)
        data_a = _isolate(tmp_path, "a", _free_port(), monkeypatch)
        pm.set_peer("b", f"http://127.0.0.1:{port}", "B")     # 唯一的人工种子
        yield data_a, tmp_path / "b"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def _feed(path: Path) -> list:
    try:
        return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]
    except OSError:
        return []


def test_gossip_discovers_unknown_node(two_nodes):
    """A 只认识 B，交换一次名册就该认识 C —— 这就是「上网就被所有大白看到」。"""
    assert "c" not in ps.roster()
    r = ps.gossip_once()
    assert r["reached"] == 1, r
    assert "c" in ps.roster(), "没从 B 学到 C，发现机制不成立"
    assert pm.peers().get("c", {}).get("url") == "http://127.0.0.1:9", "学到的节点没并进地址簿"


def test_announce_puts_me_into_their_roster(two_nodes):
    data_a, data_b = two_nodes
    ps.announce()
    nodes = json.loads((data_b / "peer_roster.json").read_text(encoding="utf-8"))["nodes"]
    assert "a" in nodes, "B 的名册里没有 A：上线广播没生效"
    assert nodes["a"]["url"].startswith("http://127.0.0.1:")


def test_friend_is_one_sided_but_notified(two_nodes):
    data_a, data_b = two_nodes
    ps.gossip_once()
    r = ps.friend_add("b", "同一台机器的两个身份")
    assert r["ok"] and r["added"], r
    assert "b" in ps.friends()
    assert r["notified"], "对方没收到被关注通知"
    assert any(x.get("type") == "followed" for x in _feed(data_b / "feed.jsonl"))


def test_post_and_comment_round_trip(two_nodes):
    data_a, data_b = two_nodes
    ps.gossip_once()
    ps.friend_add("b")
    r = ps.post("联邦社会层上线了")
    assert r["delivered"] == 1, r
    got = [x for x in _feed(data_b / "feed.jsonl") if x.get("type") == "post"]
    assert got and got[-1]["text"] == "联邦社会层上线了", "B 没收到 A 的动态"

    # A 自己的收信路由也挂起来（进程内 ASGI，不占端口）：两个方向都要真跑
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    app = FastAPI()
    app.include_router(pm.router)
    app.include_router(ps.router)
    cli = TestClient(app)

    # 方向一：B 发动态 → A 收（等价于 B 的 post() 推过来）
    ts = int(time.time())
    text_b = "B 这边也上线了"
    resp = cli.post("/api/peer/social/post",
                    headers={pm.KEY_HEADER: pm.cluster_key().decode()},
                    json={"from": "b", "text": text_b, "ts": ts,
                          "sig": pm.sign("b", ts, text_b), "label": "B"})
    assert resp.json()["ok"], resp.text
    item_b = [x for x in ps.feed_items() if x.get("text") == text_b][0]["id"]

    # 方向二：A 评论 B 的动态 → 真 HTTP 打到 B 子进程
    c = ps.comment(item_b, "第一个评论")
    assert c["notified"], c
    b_comments = [x for x in _feed(data_b / "feed.jsonl") if x.get("type") == "comment"]
    assert b_comments and b_comments[-1]["re"] == item_b, "B 没收到 A 的评论"

    # 方向三：B 评论 A 的动态 → 打 A 的收信路由
    ts = int(time.time())
    text_c = "回你一句"
    resp = cli.post("/api/peer/social/comment",
                    headers={pm.KEY_HEADER: pm.cluster_key().decode()},
                    json={"from": "b", "item_id": got[-1]["id"], "text": text_c,
                          "ts": ts, "sig": pm.sign("b", ts, text_c)})
    assert resp.json()["ok"], resp.text
    mine = [x for x in ps.feed_items()
            if x.get("type") == "comment" and x.get("re") == got[-1]["id"]]
    assert mine and mine[-1]["author"] == "b", "A 没收到 B 的评论"
