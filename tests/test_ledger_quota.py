"""低配额模式：余额见底要真的拦住动作，不是打印一句提示。

测的重点是「闸门真的返回 deny」——直接调 heavy_block 只能证明函数会算数，
证明不了工具调用路径上真被拦住。所以每个拦截用例都过 tool_gate.evaluate。
"""
import pytest

import peer_ledger as pl
import tool_gate


@pytest.fixture
def led(tmp_path, monkeypatch):
    monkeypatch.setattr(pl, "LEDGER_FILE", tmp_path / "ledger.jsonl")
    monkeypatch.setattr(pl, "HEADS_FILE", tmp_path / "heads.json")
    monkeypatch.setattr(pl, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(pl, "METRICS_FILE", tmp_path / "metrics.jsonl")
    monkeypatch.setattr(pl, "_QUOTA_CACHE", {"ts": 0.0, "val": None})
    monkeypatch.setattr(pl.pm, "cluster_key", lambda create=True: b"test-key-0123456789")
    monkeypatch.setattr(pl.pm, "node_info", lambda create=True: {"node_id": "testnode", "label": "t"})
    monkeypatch.setattr(pl.pm, "_atomic_write",
                        lambda p, t, mode=0o600: p.write_text(t, encoding="utf-8"))
    return pl


def _set_balance(led, amount):
    """把余额调成 amount（清链重来）。余额是全部 delta 之和，追加一笔改不动已有余额。"""
    led.LEDGER_FILE.write_text("", encoding="utf-8")
    led.append("grant", amount, "测试额度")
    led._QUOTA_CACHE["val"] = None      # 清 TTL 缓存，否则读到的还是上一状态


def _gate(tool, args=None):
    return tool_gate.evaluate(tool, args or {}, {}, {}, {}, {})


def test_normal_balance_allows_heavy(led):
    """正常态不拦。这些工具本来就归 tool_gate 的高危规则管（可能弹确认卡），
    低配额只该把它们从 ask 变成 deny —— 断言 != deny 才是在测这条边界。"""
    _set_balance(led, 100)
    assert led.quota_mode()["mode"] == "normal"
    assert _gate("delegate_agent_task", {"goal": "x"})[0] != "deny"


def test_zero_balance_blocks_heavy_through_gate(led):
    _set_balance(led, 0)
    assert led.quota_mode()["mode"] == "low"
    verdict, reason, _ = _gate("delegate_agent_task", {"goal": "x"})
    assert verdict == "deny"
    assert "余额见底" in reason


def test_negative_balance_blocks_heavy(led):
    _set_balance(led, 50)
    led.append("spend", -80, "烧穿了")
    led._QUOTA_CACHE["val"] = None
    assert led.balance() == -30
    assert led.quota_mode()["mode"] == "low"
    assert _gate("sub_agent_spawn", {"task": "x"})[0] == "deny"


def test_readonly_tools_still_allowed_when_broke(led):
    """穷的时候更该允许我把情况看清楚——读文件不花钱。"""
    _set_balance(led, 0)
    assert _gate("code_read", {"files": "a.py"})[0] == "allow"
    assert _gate("read_json", {"path": "settings.json"})[0] == "allow"


def test_cheap_write_tools_not_blocked(led):
    """低配额掐的是放大器，不是所有写操作——否则等于把自己锁死。"""
    _set_balance(led, 0)
    assert _gate("code_edit", {"file": "a.py", "old": "x", "new": "y"})[0] == "allow"


def test_missing_ledger_keeps_old_behavior(led):
    """账本不存在 = 这台还没升级。新机制不该悄悄改掉老行为。"""
    assert not led.LEDGER_FILE.exists()
    assert led.quota_mode()["mode"] == "normal"
    assert _gate("delegate_agent_task", {"goal": "x"})[0] != "deny"


def test_empty_existing_ledger_grants_and_is_normal(led):
    """账本已启用但还没花过一分钱：空链余额 0，不能误判成「穷」——那会让刚出生的
    实例第一轮就进低配额。endowment 只发一次，靠「链非空」判断。"""
    led.LEDGER_FILE.write_text("", encoding="utf-8")
    assert led.quota_mode()["mode"] == "normal"
    assert led.balance() == led.DEFAULT_ENDOWMENT
    assert led.quota_mode()["mode"] == "normal"      # 发完额度仍要正常


def test_unreadable_ledger_is_low(led):
    """账本存在但读不动 = 检查坏掉。静默放行比没有检查更危险。

    注意两条路径都收敛到 low：_read_all 把坏行/坏文件当空链（余额 0），
    以及 balance() 直接抛异常。断言只锁「拦住了」和「理由非空」，不锁文案。
    """
    led.LEDGER_FILE.mkdir()          # 目录冒充账本 → read_text 抛 IsADirectoryError
    q = led.quota_mode()
    assert q["mode"] == "low" and q["reason"]
    assert _gate("delegate_agent_task", {"goal": "x"})[0] == "deny"


def test_cache_ttl_holds_old_state_then_refreshes(led):
    _set_balance(led, 100)
    assert led.quota_mode()["mode"] == "normal"
    led.append("spend", -200, "烧穿了")
    assert led.quota_mode()["mode"] == "normal"      # TTL 内不重读：工具调用不该每次都读全链
    led._QUOTA_CACHE["val"] = None
    assert led.quota_mode()["mode"] == "low"


def test_heavy_list_covers_the_amplifiers(led):
    """清单被删空时上面所有拦截用例都会静默通过——所以直接锁住成员。"""
    for name in ("delegate_agent_task", "sub_agent_spawn", "harness_flow_submit",
                 "harness_batch_submit", "sched_add", "peer_task"):
        assert name in led.HEAVY_TOOLS, f"{name} 是烧钱大户，漏了它低配额就形同虚设"


def test_injection_silent_when_normal_and_loud_when_broke(led):
    """见底时必须说话：否则我会一直撞闸门却不知道为什么，压力就变成纯粹的失败。"""
    import agent

    _set_balance(led, 100)
    assert agent._harness_balance_block() == ""
    _set_balance(led, 0)
    block = agent._harness_balance_block()
    assert "低配额模式" in block and "余额见底" in block


def test_balance_cli_shows_mode(led, capsys):
    _set_balance(led, 0)
    led._cli(["balance"])
    assert "配额 低配额" in capsys.readouterr().out
