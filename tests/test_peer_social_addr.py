"""名册交换是双向的、地址只能信本人 —— 这两条不成立时不会报错，只会静默自环：
发起方永远零锚定（没人给它存过账本承诺），地址被二手信息改错（实测把同伴的地址
写成第三台机器的域名，于是 gossip 自己发给自己，那台机器从联盟里静默消失）。"""
import json

import pytest

import peer_ledger as pl
import peer_mesh as pm
import peer_social as ps


@pytest.fixture
def social(tmp_path, monkeypatch):
    monkeypatch.setattr(pm, "NODE_FILE", tmp_path / "node.json")
    monkeypatch.setattr(pm, "PEERS_FILE", tmp_path / "peers.json")
    monkeypatch.setattr(ps, "ROSTER_FILE", tmp_path / "roster.json")
    monkeypatch.setattr(ps, "FRIENDS_FILE", tmp_path / "friends.json")
    monkeypatch.setattr(pl, "LEDGER_FILE", tmp_path / "ledger.jsonl")
    monkeypatch.setattr(pl, "HEADS_FILE", tmp_path / "heads.json")
    monkeypatch.setattr(pl, "KEY_FILE", tmp_path / "node_key.pem")
    monkeypatch.setattr(pl, "_ID", {})
    (tmp_path / "node.json").write_text(
        json.dumps({"node_id": "a", "label": "A", "url": "https://a.example"}), encoding="utf-8")
    return tmp_path


def _card(node: str, head: str = "deadbeef") -> dict:
    return {"node_id": node, "label": node.upper(), "url": f"https://{node}.example", "ts": 100,
            "ledger": {"node": node, "count": 3, "head": head, "balance": 10.0,
                       "pub": "00" * 32, "ts": 100, "sig": "ff" * 64}}


def test_my_entry_first_item_is_full_card(social):
    """自己那条得是完整名片：应答方也要拿到我的账本承诺，不然只有发起方存得下锚定。"""
    items = ps.my_entry()
    assert items[0]["node_id"] == "a"
    assert isinstance(items[0].get("ledger"), dict), "自己那条只有瘦字段，应答方学不到账本链头"


def test_reply_card_gets_anchored(social):
    """收到对方名册里的账本摘要 → 落成锚定，这才是「互相见证」。"""
    assert not (social / "heads.json").exists()
    ps.merge_roster([_card("b")], via="b")
    heads = json.loads((social / "heads.json").read_text(encoding="utf-8"))
    assert heads["b"][-1]["head"] == "deadbeef", "应答方的名片没被存成锚定"


def test_secondhand_url_never_overwrites(social):
    """地址只信本人名片；转手的地址只能在本地完全没地址时补种。"""
    ps.merge_roster([_card("b")], via="b")
    assert ps.roster(prune=False)["b"]["url"] == "https://b.example"

    # c 转手说「b 在 c.example」：这是二手消息，信了就把 b 指到 c 身上
    ps.merge_roster([{"node_id": "b", "label": "B", "url": "https://c.example", "ts": 999}], via="c")
    assert ps.roster(prune=False)["b"]["url"] == "https://b.example"
    assert pm.peers()["b"]["url"] == "https://b.example"

    # b 自己的名片说它搬到了 b2.example：第一手，得跟
    ps.merge_roster([{**_card("b"), "url": "https://b2.example", "ts": 1000}], via="b")
    assert ps.roster(prune=False)["b"]["url"] == "https://b2.example"

    # 从没听说过的 d：二手地址也比没有强，先补种
    ps.merge_roster([{"node_id": "d", "label": "D", "url": "https://d.example", "ts": 5}], via="c")
    assert ps.roster(prune=False)["d"]["url"] == "https://d.example"
