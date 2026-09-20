"""跨机付款：钱从我的链走到你的链，两边各自签、各自记、都能独立验。

两台机器用两本临时账本模拟；pay() 要发 HTTP，测试里把 pm.call 换成「直接投递到
对方的 accept_payment()」。路由那层（密钥、状态码）由 test_peer_ledger 覆盖，
这里盯的是账本身：伪造、重放、金额、余额，以及「在路上的钱」。
"""
import pytest

import peer_ledger as pl
import peer_mesh as pm


class Machine:
    """一台「机器」：一本账、一把钥匙、一个名字。切机器就是换这三样。"""

    def __init__(self, tmp_path, name):
        self.name = name
        d = tmp_path / name
        d.mkdir(parents=True, exist_ok=True)
        self.ledger = d / "ledger.jsonl"
        self.key = d / "node_key.pem"
        self.state = d / "state.json"

    def use(self, mp):
        mp.setattr(pl, "LEDGER_FILE", self.ledger)
        mp.setattr(pl, "KEY_FILE", self.key)
        mp.setattr(pl, "STATE_FILE", self.state)
        mp.setattr(pl, "_node", lambda: self.name)
        mp.setattr(pl, "_ID", {})      # 切机器必须丢钥匙缓存，否则签名还是上一台的
        mp.setattr(pl, "_QUOTA_CACHE", {"ts": 0.0, "val": None})

    def kinds(self):
        return [e.get("kind") for e in pl._read_all()]


@pytest.fixture
def two(tmp_path, monkeypatch):
    a, b = Machine(tmp_path, "aliyun"), Machine(tmp_path, "rpi")
    monkeypatch.setattr(pl.pm, "cluster_key", lambda create=True: b"test-key-0123456789")
    a.use(monkeypatch)
    return a, b


def wire(machines, mp, ok=True, mutate=None):
    """把跨机 HTTP 换成直接投递到对方账本。mutate 可以改一笔，用来模拟中间人。"""
    def fake_call(node, path, payload=None, timeout=0):
        if not ok:
            return {"ok": False, "error": "HTTP 404"}
        target = machines.get(node)
        if target is None:
            return {"ok": False, "error": f"unknown node: {node}"}
        if path == "/api/peer/ledger":
            return {"ok": True, "node": target.name}     # 探测：对方支持资源账
        assert path == "/api/peer/ledger/receive", path
        caller = next(m for m in machines.values() if pl.LEDGER_FILE == m.ledger)
        entry = (payload or {}).get("entry")
        if mutate:
            entry = mutate(entry)
        target.use(mp)
        try:
            return pl.accept_payment(entry)
        finally:
            caller.use(mp)
    mp.setattr(pl.pm, "call", fake_call)


def _pair(a, b, mp, **kw):
    wire({a.name: a, b.name: b}, mp, **kw)


def test_pay_lands_on_both_chains(two, monkeypatch):
    """一笔钱两边各记一笔：我这边 pay、它那边 recv、我收到收据记 ack。"""
    a, b = two
    _pair(a, b, monkeypatch)
    pl.append("grant", 100, "初始")
    r = pl.pay(b.name, 30, "派活预付")
    assert r["ok"] and r["amount"] == 30
    assert pl.balance() == 70                      # 出账即扣
    assert a.kinds() == ["grant", "pay", "ack"]
    b.use(monkeypatch)
    assert pl.balance() == 30
    assert b.kinds() == ["recv"]
    recv = pl._read_all()[-1]
    assert recv["ref"]["from"] == "aliyun" and recv["ref"]["nonce"] == r["nonce"]
    assert len(recv["ref"]["pay_sig"]) == 128      # 收款记录里存着付款方那笔的签名原文
    assert recv["ref"]["degraded"] is False


def test_tampered_amount_rejected(two, monkeypatch):
    """中间人把 -30 改成 -1：delta 在签名域里，改一个数字就验不过。"""
    a, b = two
    _pair(a, b, monkeypatch, mutate=lambda e: {**e, "delta": -1})
    pl.append("grant", 100, "初始")
    r = pl.pay(b.name, 30)
    assert not r["ok"] and "签名不符" in r["error"]
    b.use(monkeypatch)
    assert pl._read_all() == []


def test_replay_rejected(two, monkeypatch):
    """同一笔付款交两遍：第二遍必须被 nonce 挡住，否则钱能凭空翻倍。"""
    a, b = two
    _pair(a, b, monkeypatch)
    pl.append("grant", 100, "初始")
    r = pl.pay(b.name, 30)
    assert r["ok"]
    entry = [e for e in pl._read_all() if e["kind"] == "pay"][0]
    b.use(monkeypatch)
    again = pl.accept_payment(entry)
    assert not again["ok"] and "重放" in again["error"]
    assert b.kinds() == ["recv"]                   # 还是只收了一笔


def test_pay_to_other_node_rejected(two, monkeypatch):
    """签名有效、但收款方写着别人：拒。否则谁都能把付给别人的钱记到我头上。"""
    a, b = two
    pl.append("grant", 100, "初始")
    e = pl.append("pay", -30, "付给 wsl", ref={"to": "wsl", "nonce": "aa11"})
    b.use(monkeypatch)
    r = pl.accept_payment(e)
    assert not r["ok"] and "不是我" in r["error"]


def test_non_payment_rejected(two, monkeypatch):
    """不是付款记录、或者金额非负 —— 一律拒。往我账上加钱我自己就会做，不用别人代劳。"""
    a, b = two
    pl.append("grant", 100, "初始")
    grant = pl._read_all()[0]
    b.use(monkeypatch)
    assert "不是付款记录" in pl.accept_payment(grant)["error"]
    assert "没有付款记录" in pl.accept_payment(None)["error"]
    pos = pl.append("pay", 30, "反向付款", ref={"to": "rpi", "nonce": "bb22"})
    assert "必须是负数" in pl.accept_payment(pos)["error"]
    no_nonce = pl.append("pay", -1, "没有 nonce", ref={"to": "rpi"})
    assert "nonce" in pl.accept_payment(no_nonce)["error"]


def test_insufficient_balance_leaves_no_trace(two, monkeypatch):
    """余额不够就不出账：链上不留半笔。"""
    a, b = two
    _pair(a, b, monkeypatch)
    pl.append("grant", 10, "初始")
    r = pl.pay(b.name, 30)
    assert not r["ok"] and "不够" in r["error"]
    assert a.kinds() == ["grant"]


def test_pay_probe_blocks_charge_when_peer_unsupported(two, monkeypatch):
    """对岸还没升级：探测那一步就挡住，不出账 —— 够不着的付款不该先扣钱。"""
    a, b = two
    _pair(a, b, monkeypatch, ok=False)
    pl.append("grant", 100, "初始")
    r = pl.pay(b.name, 30)
    assert not r["ok"] and "没出账" in r["note"]
    assert a.kinds() == ["grant"]
    assert pl.pending() == []


def test_pending_when_peer_offline(two, monkeypatch):
    """钱发出去了、签收没回来：必须看得见 —— 假装它没花才是骗自己。"""
    a, b = two
    pl.append("grant", 100, "初始")
    _pair(a, b, monkeypatch)
    pl.pay(b.name, 30)                                  # 先正常付一次，证明链路通
    wire({a.name: a, b.name: b}, monkeypatch, ok=True)
    b.use(monkeypatch)
    pl.pay(a.name, 5)                                   # b 反向付，正常
    a.use(monkeypatch)
    # 现在让签收回执收不到：探测通、投递失败（模拟半路断网）
    def half(node, path, payload=None, timeout=0):
        if path == "/api/peer/ledger":
            return {"ok": True}
        return {"ok": False, "error": "connection reset"}
    monkeypatch.setattr(pl.pm, "call", half)
    r = pl.pay(b.name, 7)
    assert not r["ok"] and r["nonce"]
    assert pl.balance() == 68                           # 100 - 30 + 5 - 7
    ps = pl.pending()
    assert len(ps) == 1 and ps[0]["amount"] == 7 and ps[0]["to"] == "rpi"
    assert pl.transfers()["rpi"]["pending"] == 7


def test_transfers_summary_counts_both_ways(two, monkeypatch):
    a, b = two
    _pair(a, b, monkeypatch)
    pl.append("grant", 100, "初始")
    pl.pay(b.name, 30)
    b.use(monkeypatch)
    pl.pay(a.name, 5, "找零")
    assert pl.transfers()["aliyun"] == {"out": 5.0, "in": 30.0, "pending": 0.0}
    a.use(monkeypatch)
    assert pl.transfers()["rpi"] == {"out": 30.0, "in": 5.0, "pending": 0.0}
    assert pl.balance() == 75


def test_received_lookup(two, monkeypatch):
    """派活接单前要能问一句：这笔钱我到底收到没有。"""
    a, b = two
    _pair(a, b, monkeypatch)
    pl.append("grant", 100, "初始")
    r = pl.pay(b.name, 30)
    b.use(monkeypatch)
    got = pl.received(r["nonce"])
    assert got and got["amount"] == 30 and got["from"] == "aliyun"
    assert pl.received("不存在") is None


def test_hmac_payment_marked_degraded(two, monkeypatch):
    """没有 cryptography 的机器只能用共享密钥签 —— 收下，但必须标明「证明不了是它本人签的」。"""
    a, b = two
    pl.append("grant", 100, "初始")
    e = pl.append("pay", -30, "降级付款", ref={"to": "rpi", "nonce": "cc99"})
    e.pop("alg", None)
    e.pop("pub", None)
    e["sig"] = pl._sign(e)          # 老字段表 + 共享密钥，模拟没装 cryptography 的那台
    b.use(monkeypatch)
    r = pl.accept_payment(e)
    assert r["ok"] and r["degraded"] is True
    assert pl._read_all()[-1]["ref"]["degraded"] is True


# ---------- 接单侧：钱不到位不干活，活没干成钱退回去 ----------

@pytest.fixture
def task_env(tmp_path, monkeypatch):
    monkeypatch.setattr(pm, "TASK_LOG_FILE", tmp_path / "task_log.jsonl")
    monkeypatch.setattr(pm, "TASK_QUOTA_FILE", tmp_path / "task_quota.json")
    monkeypatch.setattr(pm, "task_gate_enabled", lambda: True)
    return tmp_path


def test_accept_task_rejects_fake_prepay(two, monkeypatch, task_env):
    """嘴上说付了、链上查不到：拒单。不收假付的钱。"""
    a, b = two
    b.use(monkeypatch)
    r = pm.accept_task({"from": "aliyun", "text": "查个东西",
                        "paid": {"nonce": "deadbeef", "amount": 30}})
    assert not r["ok"] and "链上没这笔收款" in r["error"]


def test_accept_task_with_valid_prepay_dispatches(two, monkeypatch, task_env):
    """真付了钱、活也没问题：接单，派给子智能体。"""
    a, b = two
    _pair(a, b, monkeypatch)
    pl.append("grant", 100, "初始")
    r = pl.pay(b.name, 30, "派活预付")
    assert r["ok"]
    import scheduler
    monkeypatch.setattr(scheduler, "add_job", lambda **kw: ({"id": "job-1"}, ""))
    b.use(monkeypatch)
    out = pm.accept_task({"from": "aliyun", "text": "查个东西",
                          "paid": {"nonce": r["nonce"], "amount": 30}})
    assert out["ok"] and out["job_id"] == "job-1"
    assert b.kinds() == ["recv"]          # 收下的钱不退


def test_accept_task_refunds_when_blocked(two, monkeypatch, task_env):
    """活被拦下了，预付款必须退回去 —— 接了单才收钱，没接成不能白拿。"""
    a, b = two
    _pair(a, b, monkeypatch)
    pl.append("grant", 100, "初始")
    r = pl.pay(b.name, 30, "派活预付")
    assert r["ok"]
    monkeypatch.setattr(pm, "dangerous_in", lambda t: "rm -rf")
    b.use(monkeypatch)
    out = pm.accept_task({"from": "aliyun", "text": "危险活",
                          "paid": {"nonce": r["nonce"], "amount": 30}})
    assert not out["ok"] and out["blocked"] is True
    assert b.kinds() == ["recv", "pay", "ack"]   # 30 收进来，30 退回去（ack = 对方签收了退款）
    assert pl.balance() == 0
    a.use(monkeypatch)
    assert a.kinds()[-1] == "recv"        # 退款真的到账了，不只是发出去
    assert pl.balance() == 100


def test_legacy_task_without_payment_still_accepted(two, monkeypatch, task_env):
    """老版本不带 paid 字段：兼容期照旧接单，一次发布不该让联盟互相拒单。"""
    a, b = two
    import scheduler
    monkeypatch.setattr(scheduler, "add_job", lambda **kw: ({"id": "job-2"}, ""))
    b.use(monkeypatch)
    out = pm.accept_task({"from": "wsl", "text": "老版本派的活"})
    assert out["ok"] and out["job_id"] == "job-2"


# ---------- 路由层：这条收钱的口子真的挂上了、也真的认钥匙 ----------

def _client(monkeypatch, key=b"test-key-0123456789"):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    monkeypatch.setattr(pm, "cluster_key", lambda create=True: key)
    app = FastAPI()
    app.include_router(pl.router)
    return TestClient(app), key.decode()


def test_receive_route_needs_key(two, monkeypatch):
    """没钥匙就 403：联邦里的密钥是门槛，收钱的口子不能比别处松。"""
    a, b = two
    client, _ = _client(monkeypatch)
    r = client.post("/api/peer/ledger/receive", json={"entry": None})
    assert r.status_code == 403


def test_receive_route_accepts_valid_payment(two, monkeypatch):
    """真走一次 HTTP：a 签的付款记录打给 b 的路由，b 的账上多一笔 recv。"""
    a, b = two
    pl.append("grant", 100, "初始")
    e = pl.append("pay", -30, "走路由", ref={"to": "rpi", "nonce": "dd77"})
    client, key = _client(monkeypatch)
    b.use(monkeypatch)
    r = client.post("/api/peer/ledger/receive", json={"entry": e},
                    headers={pm.KEY_HEADER: key})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] and body["amount"] == 30 and body["balance"] == 30
    assert b.kinds() == ["recv"]
    assert len(body["sig"]) == 128          # 回给付款方的收据，它记成 ack


def test_receive_route_rejects_tampered_payment(two, monkeypatch):
    a, b = two
    pl.append("grant", 100, "初始")
    e = pl.append("pay", -30, "走路由", ref={"to": "rpi", "nonce": "ee88"})
    client, key = _client(monkeypatch)
    b.use(monkeypatch)
    r = client.post("/api/peer/ledger/receive", json={"entry": {**e, "delta": -1}},
                    headers={pm.KEY_HEADER: key})
    assert r.status_code == 200 and r.json()["ok"] is False
    assert b.kinds() == []
