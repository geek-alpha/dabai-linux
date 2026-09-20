#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回合结束的复盘触发：双计数器 + 有信号才落草稿 + 失败静默。

背景（2026-09-20）：经验库写入端只能靠自觉调用，学习机通不通电看当轮心情。
对照 hermes-agent/agent/turn_finalizer.py:629-657：双计数器（工具迭代数 / 用户轮数）
到点、且在回复投递之后才触发复盘，失败被 suppress 掉不影响交付。

契约：
  1. 未到阈值只累加计数，不产生草稿
  2. 到迭代阈值且本轮有报错 → 落一条 pending 草稿，计数器归零
  3. 到阈值但本轮无任何信号 → 不落草稿，计数器归零，skipped +1
     （否则每 10 轮一条「没事发生」，待审列表变成噪音就没人看了）
  4. 用户纠正词算信号——那类坑常常不带工具报错
  5. 草稿滚动上限，不无限膨胀
  6. 状态文件损坏时 tick 不许抛异常：复盘触发器不该有能力弄坏一次对话
"""
import importlib.util
import json
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]


def _load(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("learn_nudge_under_test", BASE / "tools" / "learn_nudge.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "STATE", tmp_path / "state.json")
    monkeypatch.setattr(mod, "DRAFTS", tmp_path / "drafts.json")
    return mod


def test_below_threshold_only_counts(tmp_path, monkeypatch):
    mod = _load(tmp_path, monkeypatch)
    assert mod.tick(tool_rounds=3, tool_errors=1, user_msg="随便", now=100.0) is None
    assert mod.pending() == [], "没到点不许产草稿"
    st = json.loads(mod.STATE.read_text(encoding="utf-8"))
    assert (st["iters"], st["turns"]) == (3, 1), "没到点也要记账，否则计数器永远到不了"


def test_iter_threshold_with_error_writes_draft(tmp_path, monkeypatch):
    mod = _load(tmp_path, monkeypatch)
    mod.tick(tool_rounds=mod.ITER_INTERVAL, tool_calls=4, tool_errors=2,
             err_names=["shell_run", "code_edit"], user_msg="修一下这个", reply="改完了", now=200.0)
    pends = mod.pending()
    assert len(pends) == 1
    d = pends[0]
    assert d["due"] == "iters" and d["tool_errors"] == 2
    assert d["err_names"] == ["shell_run", "code_edit"]
    assert d["at"] == 200.0, "草稿要带自己的时刻，否则事后无法对账"
    st = json.loads(mod.STATE.read_text(encoding="utf-8"))
    assert (st["iters"], st["turns"]) == (0, 0), "落完草稿必须归零，否则每轮都触发"


def test_threshold_without_signal_skips(tmp_path, monkeypatch):
    mod = _load(tmp_path, monkeypatch)
    assert mod.tick(tool_rounds=mod.ITER_INTERVAL, tool_errors=0, user_msg="继续", now=1.0) is None
    assert mod.pending() == [], "没事发生的轮次不该产草稿"
    st = json.loads(mod.STATE.read_text(encoding="utf-8"))
    assert st["skipped"] == 1 and st["iters"] == 0


def test_user_correction_is_a_signal(tmp_path, monkeypatch):
    mod = _load(tmp_path, monkeypatch)
    mod.tick(tool_rounds=mod.ITER_INTERVAL, tool_errors=0, user_msg="你这结论不对", now=3.0)
    assert len(mod.pending()) == 1, "用户纠正常常不带工具报错，漏了它等于漏掉大半教训"


def test_turn_threshold_also_fires(tmp_path, monkeypatch):
    mod = _load(tmp_path, monkeypatch)
    for i in range(mod.TURN_INTERVAL - 1):
        assert mod.tick(tool_rounds=0, user_msg="无事", now=float(i)) is None
    assert len(mod.pending()) == 0
    mod.tick(tool_rounds=0, user_msg="无事", tool_errors=1, now=99.0)
    pends = mod.pending()
    assert len(pends) == 1 and pends[0]["due"] == "turns"


def test_drafts_capped(tmp_path, monkeypatch):
    mod = _load(tmp_path, monkeypatch)
    monkeypatch.setattr(mod, "MAX_DRAFTS", 3)
    for i in range(5):
        mod.tick(tool_rounds=mod.ITER_INTERVAL, tool_errors=1, user_msg="报错了", now=float(i))
    assert len(mod.drafts()) == 3, "草稿也要有上限，否则待审列表会自己长成垃圾场"


def test_corrupt_state_does_not_raise(tmp_path, monkeypatch):
    mod = _load(tmp_path, monkeypatch)
    mod.STATE.parent.mkdir(parents=True, exist_ok=True)
    mod.STATE.write_text("{坏掉的 json", encoding="utf-8")
    mod.tick(tool_rounds=1, user_msg="x", now=1.0)
    assert json.loads(mod.STATE.read_text(encoding="utf-8"))["turns"] == 1


def test_mark_done_removes_from_pending(tmp_path, monkeypatch):
    mod = _load(tmp_path, monkeypatch)
    mod.tick(tool_rounds=mod.ITER_INTERVAL, tool_errors=1, user_msg="报错", now=1.0)
    assert mod.mark_done(1) is True
    assert mod.pending() == []
    assert mod.mark_done(9) is False
