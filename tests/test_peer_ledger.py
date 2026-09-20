"""资源账的硬保证只有一条：**改不动**。所以测试的重点是「改了会被抓」，
而不是「记了能读出来」——后者不测也知道，前者才是这个模块存在的理由。"""
import json

import pytest

import peer_ledger as pl


@pytest.fixture
def led(tmp_path, monkeypatch):
    monkeypatch.setattr(pl, "LEDGER_FILE", tmp_path / "ledger.jsonl")
    monkeypatch.setattr(pl, "HEADS_FILE", tmp_path / "heads.json")
    monkeypatch.setattr(pl, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(pl, "METRICS_FILE", tmp_path / "metrics.jsonl")
    monkeypatch.setattr(pl, "KEY_FILE", tmp_path / "node_key.pem")   # 测试自己一把钥匙，不碰真身份
    monkeypatch.setattr(pl, "_ID", {})
    (tmp_path / "metrics.jsonl").write_text("", encoding="utf-8")   # 空文件也要存在：sync 的
    # 「首次运行」分支靠「文件里有没有行」区分，文件不存在会走成读取失败
    monkeypatch.setattr(pl.pm, "cluster_key", lambda create=True: b"test-key-0123456789")
    monkeypatch.setattr(pl.pm, "node_info", lambda create=True: {"node_id": "testnode", "label": "t"})
    monkeypatch.setattr(pl.pm, "_atomic_write",
                        lambda p, t, mode=0o600: p.write_text(t, encoding="utf-8"))
    return pl


def _lines(led):
    return [json.loads(x) for x in led.LEDGER_FILE.read_text(encoding="utf-8").splitlines() if x.strip()]


def _write(led, rows):
    led.LEDGER_FILE.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")


def test_empty_chain_verifies(led):
    v = led.verify()
    assert v["ok"] and v["count"] == 0 and v["balance"] == 0


def test_chain_links_and_balance(led):
    led.append("grant", 1000, "初始")
    led.append("spend", -12.5, "自用")
    led.append("earn", 30, "接单")
    v = led.verify()
    assert v["ok"] and v["count"] == 3
    assert v["balance"] == pytest.approx(1017.5)
    rows = _lines(led)
    assert rows[0]["prev"] == ""
    assert rows[1]["prev"] == rows[0]["sig"]      # 每笔接上一笔的签名
    assert [r["seq"] for r in rows] == [0, 1, 2]


def test_edit_amount_is_caught(led):
    led.append("grant", 1000, "初始")
    led.append("spend", -12.5, "自用")
    led.append("earn", 30, "接单")
    rows = _lines(led)
    rows[1]["delta"] = -0.5                        # 偷偷把花掉的 12.5 改成 0.5
    _write(led, rows)
    v = led.verify()
    assert not v["ok"] and v["broken_at"] == 1
    assert "签名不符" in v["reason"]


def test_delete_middle_row_is_caught(led):
    for i in range(4):
        led.append("spend", -1, f"第{i}笔")
    rows = _lines(led)
    del rows[1]                                    # 抹掉中间一笔
    _write(led, rows)
    v = led.verify()
    assert not v["ok"] and v["broken_at"] == 1
    assert "缺笔" in v["reason"] or "prev" in v["reason"]


def test_tail_truncation_survives_chain_check(led):
    """砍掉尾部，剩下的链完全自洽 —— append-only 链验不出截断。

    这不是缺陷，是链的固有边界：能自证的只有「没被改过」，不能自证「没被删过」。
    补上这个洞靠链头传播 —— 同伴手里存着我上一版的 count/head，一对比就露。
    这条测试就是那个分工的书面证据。
    """
    led.append("grant", 1000, "初始")
    led.append("spend", -50, "自用")
    claim = led.summary() | {"node_id": "rpi"}
    claim.pop("sig", None)                         # 不带签名的摘要：这条测试只管链头历史
    led.remember_head({"node_id": "rpi", "ledger": claim})
    rows = _lines(led)
    _write(led, rows[:-1])                         # 砍掉最后一笔（-50 那笔）
    assert led.verify()["ok"]                      # 链本身看不出问题
    hist = led.peer_heads()["rpi"][-1]
    assert led.check_peer("rpi", hist["count"], hist["head"])["ok"]
    assert led.check_peer("rpi", hist["count"], led.summary()["head"])["ok"] is False


def test_own_key_signs_and_shared_key_is_out(led, monkeypatch):
    """升级后的边界，比旧版强一层，但得说清：

    ① 共享的 cluster.key 再也伪造不了我的账 —— 签名用的是我自己的私钥
    ② 我自己仍能重签自己的链（钥匙在我手里），链本身验不出来 —— 抓它靠锚定
    """
    led.append("grant", 1000, "初始")
    v = led.verify()
    assert v["ok"] and v["algs"] == {"ed25519": 1} and v["pub"] == led.pubkey()
    monkeypatch.setattr(led.pm, "cluster_key", lambda create=True: b"another-key")
    assert led.verify()["ok"]                      # 换共享密钥毫无影响：签名根本不用它
    rows = _lines(led)
    rows[0]["delta"] = 999999
    rows[0]["sig"] = led._sign(rows[0])            # 自己的钥匙能重签自己的账
    _write(led, rows)
    assert led.verify()["ok"]


def test_legacy_hmac_chain_still_verifies(led, monkeypatch):
    """老账不重签：HMAC 签的历史就用 HMAC 验。重签历史等于自己伪造自己。"""
    monkeypatch.setattr(led, "pubkey", lambda create=True: "")   # 模拟没 cryptography / 旧版本
    led.append("grant", 1000, "旧版签的")
    led.append("spend", -10, "自用")
    v = led.verify()
    assert v["ok"] and v["algs"] == {"hmac": 2} and v["pub"] == ""
    assert led.balance() == pytest.approx(990)


def test_mixed_chain_verifies(led, monkeypatch):
    """升级不是断点：老记录和新记录在同一条链上各自按自己的算法验。"""
    real = led.pubkey
    monkeypatch.setattr(led, "pubkey", lambda create=True: "")
    led.append("grant", 1000, "旧版签的")
    monkeypatch.setattr(led, "pubkey", real)
    led.append("spend", -10, "新版签的")
    v = led.verify()
    assert v["ok"] and v["algs"] == {"hmac": 1, "ed25519": 1}
    assert led.balance() == pytest.approx(990)


def _make_peer_chain(led, path, node="rpi"):
    """造一条「同伴的」链：换一把钥匙、换一个账本文件，签完再还回去。
    返回 (链上记录, 公钥, 它自己签过的名片)。"""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    k = Ed25519PrivateKey.generate()
    pub = k.public_key().public_bytes(serialization.Encoding.Raw,
                                      serialization.PublicFormat.Raw).hex()
    old = (led.LEDGER_FILE, led._ID, led.pm.node_info)
    led.LEDGER_FILE, led._ID = path, {"key": k, "pub": pub}
    led.pm.node_info = lambda create=True: {"node_id": node, "label": "r"}
    led.append("grant", 100, "同伴初始")
    led.append("spend", -5, "同伴自用")
    rows = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]
    card = {"node_id": node, "ledger": led.summary()}
    led.LEDGER_FILE, led._ID, led.pm.node_info = old
    return rows, pub, card


def test_summary_is_signed_and_forgery_fails(led):
    """名片里的承诺必须可归属：谁拿到 cluster.key 都能冒名发卡，签名才拦得住。"""
    led.append("grant", 1000, "初始")
    s = led.summary()
    assert led.summary_ok(s)
    s["balance"] = 999999                      # 改一个字段，签名就废
    assert led.summary_ok(s) is False
    assert led.summary_ok({"node": "rpi", "count": 1, "head": "x", "balance": 1,
                           "pub": "", "sig": ""}) is False
    assert led.summary_ok(None) is False


def test_anchor_seen_takes_signed_claims_only(led):
    """见证只收签名过的摘要 —— 不可篡改的链里更不该写不可追责的东西。"""
    _, _, card = _make_peer_chain(led, led.LEDGER_FILE.parent / "rpi.jsonl")
    forged = {"node_id": "rpi", "ledger": dict(card["ledger"], sig="")}
    assert led.anchor_seen([forged]) == 0
    assert led.anchors() == {}
    assert led.anchor_seen([{"node_id": "rpi", "ledger": {"head": "x", "count": 1}}]) == 0


def test_anchor_seen_anchors_signed_card_once(led):
    """gossip 顺手见证：链头变了才记一笔，没变不重复写 —— 链上的锚定该少而稳。"""
    _, pub, card = _make_peer_chain(led, led.LEDGER_FILE.parent / "rpi.jsonl")
    assert led.anchor_seen([card]) == 1
    assert led.anchor_seen([card]) == 0
    a = led.anchors()["rpi"][-1]
    assert a["src"] == "card" and a["pub"] == pub
    assert led.verify()["ok"]                      # 见证记录自己也签进链
    assert led.check_anchors("rpi", a["count"], a["head"], pub)["ok"]
    assert led.check_anchors("rpi", a["count"], "deadbeefdeadbeef", pub)["ok"] is False


def test_anchor_seen_respects_budget(led):
    cards = [_make_peer_chain(led, led.LEDGER_FILE.parent / f"p{i}.jsonl", node=f"node{i}")[2]
             for i in range(4)]
    assert led.anchor_seen(cards) == 2             # 预算封顶
    assert len(led.anchors()) == 2


def test_anchor_verifies_then_writes_witness(led, monkeypatch):
    """锚定 = 把同伴的链头签进我自己的链。这是分布式账本真正多出来的那一层。"""
    entries, pub, _ = _make_peer_chain(led, led.LEDGER_FILE.parent / "rpi.jsonl")
    head = entries[-1]["sig"][:16]
    monkeypatch.setattr(led.pm, "call", lambda *a, **k: {
        "ok": True, "entries": entries,
        "summary": {"node": "rpi", "count": 2, "head": head, "balance": 95.0, "pub": pub}})
    r = led.anchor("rpi")
    assert r["ok"] and r["anchored"] and r["count"] == 2
    assert led.anchors()["rpi"][-1]["pub"] == pub
    assert led.verify()["ok"]                     # 锚定记录自己也签进了链
    assert led.balance() == pytest.approx(0)       # 锚定不动钱
    assert led.check_anchors("rpi", 2, head, pub)["ok"]
    assert led.check_anchors("rpi", 2, "deadbeefdeadbeef", pub)["ok"] is False   # 重写
    assert led.check_anchors("rpi", 1, head, pub)["ok"] is False                  # 砍尾巴
    assert led.check_anchors("rpi", 2, head, "")["ok"] is False                  # 退回共享密钥


def test_anchor_refuses_tampered_peer(led, monkeypatch):
    """先验后锚：锚一个自己都没验过的链头，等于把假证据写进自己的账。"""
    entries, pub, _ = _make_peer_chain(led, led.LEDGER_FILE.parent / "rpi.jsonl")
    entries[1]["delta"] = -500                     # 同伴偷改了自己的账
    monkeypatch.setattr(led.pm, "call", lambda *a, **k: {
        "ok": True, "entries": entries,
        "summary": {"node": "rpi", "count": 2, "head": entries[-1]["sig"][:16],
                    "balance": -400.0, "pub": pub}})
    r = led.anchor("rpi")
    assert not r["ok"] and not r["anchored"]
    assert led.anchors() == {}                     # 假证据不进我的链


def test_anchor_survives_peer_offline(led, monkeypatch):
    monkeypatch.setattr(led.pm, "call", lambda *a, **k: {"ok": False, "http": 404})
    r = led.anchor("rpi")
    assert not r["ok"] and "404" in r["error"] and led.anchors() == {}


def test_endowment_only_once(led):
    first = led.endowment()
    assert first and first["delta"] == led.DEFAULT_ENDOWMENT
    assert led.endowment() is None                 # 第二次数不出来
    assert led.verify()["count"] == 1
    assert led.balance() == pytest.approx(led.DEFAULT_ENDOWMENT)


def test_can_work_blocks_at_zero(led):
    assert led.can_work()["ok"] is False           # 空账本 = 没钱
    led.append("grant", 5, "初始")
    assert led.can_work()["ok"] is True
    led.append("spend", -5, "花光")
    r = led.can_work()
    assert r["ok"] is False and "余额为 0" in r["reason"]
    assert led.can_work(cost=99)["ok"] is False    # 钱不够这笔也不行


def test_sync_does_not_backfill_history(led):
    """首次运行不追补历史 —— 给过去补一笔巨额消耗只会让余额从第一天就是假数。"""
    led.METRICS_FILE.write_text(json.dumps({"ts": 100.0, "prompt_tokens": 999_999_999}) + "\n",
                                encoding="utf-8")
    r = led.sync()
    assert r["rounds"] == 0 and r["tokens"] == 0
    assert led.verify()["count"] == 0              # 历史那笔没进链
    with open(led.METRICS_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": 200.0, "prompt_tokens": 100_000}) + "\n")
    r = led.sync()
    assert r["rounds"] == 1 and r["tokens"] == 100_000
    assert r["credit"] == pytest.approx(100_000 / led.TOKENS_PER_CR)
    assert led.verify()["count"] == 2              # grant + 这笔 spend


def test_sync_counts_new_rounds_once(led):
    led.sync()                                     # 先把游标立起来
    rows = [{"ts": 200.0, "prompt_tokens": 4000}, {"ts": 201.0, "prompt_tokens": 6000}]
    led.METRICS_FILE.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    r = led.sync()
    assert r["rounds"] == 2 and r["tokens"] == 10000
    assert r["credit"] == pytest.approx(10000 / led.TOKENS_PER_CR)
    assert led.verify()["count"] == 2              # grant + 这笔 spend
    again = led.sync()
    assert again["rounds"] == 0                    # 游标生效，不重复扣钱
    assert led.verify()["count"] == 2
    with open(led.METRICS_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": 300.0, "prompt_tokens": 1000}) + "\n")
    assert led.sync()["tokens"] == 1000            # 新轮次照样进账


def test_sync_ignores_broken_lines(led):
    led.sync()
    led.METRICS_FILE.write_text('{"ts": 500.0, "prompt_tokens": 2000}\n不是 json\n', encoding="utf-8")
    r = led.sync()
    assert r["ok"] and r["tokens"] == 2000


def test_remember_head_and_detect_rewrite(led):
    led.remember_head({"node_id": "rpi", "ledger": {"head": "aaaa1111", "count": 5, "balance": 10}})
    led.remember_head({"node_id": "rpi", "ledger": {"head": "aaaa1111", "count": 5, "balance": 10}})
    assert len(led.peer_heads()["rpi"]) == 1       # 同一链头不重复存
    assert led.check_peer("rpi", 5, "aaaa1111")["ok"] is True
    assert led.check_peer("rpi", 6, "bbbb2222")["ok"] is True   # 新 count 是它又记了几笔
    led.remember_head({"node_id": "rpi", "ledger": {"head": "cccc3333", "count": 5, "balance": 10}})
    bad = led.check_peer("rpi", 5, "cccc3333")     # 同一个 count 换了链头 = 重写历史
    assert bad["ok"] is False and "重写过" in bad["reason"]
    short = led.check_peer("rpi", 3, "dddd4444")   # 链长倒退 = 砍过尾巴
    assert short["ok"] is False and "尾巴" in short["reason"]


def test_own_card_is_ignored(led):
    led.remember_head({"node_id": "testnode", "ledger": {"head": "x", "count": 1, "balance": 1}})
    assert led.peer_heads() == {}
