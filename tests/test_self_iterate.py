# -*- coding: utf-8 -*-
"""自我迭代循环的判据测试。

重点不在「跑得通」，在「自嗨的路被堵住了」：
有效轮必须同时有外部产出和证据字符串、连续无效必须自动停、死磕必须换目标。
"""
import importlib.util
import json
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "tools" / "self_iterate.py"


def _load():
    spec = importlib.util.spec_from_file_location("self_iterate_test", SRC)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def mod(tmp_path, monkeypatch):
    m = _load()
    monkeypatch.setattr(m, "STATE_FILE", tmp_path / "self_iterate.json")
    monkeypatch.setattr(m, "ANCHORS", [tmp_path / "anchor.json"])
    # 隔离 git：默认认为「没有代码改动」，要测 git 路径的用例自己覆盖
    monkeypatch.setattr(m, "_git_touched", lambda since: [])
    return m


def _rounds(n, effective, target="t1", gap_ratio=None):
    return [{"n": i + 1, "target": target, "effective": effective,
             "gap_ratio": gap_ratio, "action": "a", "evidence": "e"} for i in range(n)]


# ---------- 有效轮判据 ----------

def test_effective_requires_both_output_and_evidence(mod, tmp_path):
    """只有证据字符串、没有任何外部产出 → 无效轮（防「我思考了」）。"""
    st = mod.start()
    assert st["running"] is True
    e = mod.record("t", "想了一下", "我觉得我懂了", since_ts=time.time())
    assert e["effective"] is False
    assert e["files"] == [] and e["anchors"] == []


def test_effective_when_anchor_written_and_evidence_given(mod, tmp_path):
    anchor = tmp_path / "anchor.json"
    anchor.write_text("{}", encoding="utf-8")
    since = time.time() - 1
    e = mod.record("t", "落盘了接力棒", "long_horizon next 已更新", since_ts=since)
    assert e["effective"] is True
    assert "anchor.json" in e["anchors"]


def test_output_without_evidence_is_not_effective(mod, tmp_path):
    """反证的另一半：有产出但没证据 → 无效。光「动了」不等于「证明了」。"""
    (tmp_path / "anchor.json").write_text("{}", encoding="utf-8")
    e = mod.record("t", "改了", "   ", since_ts=time.time() - 1)
    assert e["effective"] is False


def test_produced_since_reads_git_and_anchors(mod, monkeypatch, tmp_path):
    monkeypatch.setattr(mod, "_git_touched", lambda since: ["tools/x.py"])
    (tmp_path / "anchor.json").write_text("{}", encoding="utf-8")
    prod = mod.produced_since(time.time() - 1)
    assert prod["files"] == ["tools/x.py"]
    assert prod["anchors"] == ["anchor.json"]
    assert prod["effective"] is True


# ---------- 止损 ----------

def test_verdict_stops_on_invalid_streak(mod):
    mod.start()
    state = mod._load()
    state["rounds"] = _rounds(mod.MAX_STREAK, effective=False)
    mod._save(state)
    v = mod.verdict(mod._load())
    assert v["action"] == "stop"
    assert "无有效产出" in v["reason"]


def test_valid_round_resets_streak(mod):
    """有效轮必须把 streak 清零。

    用例必须是「无效轮总数 ≥ MAX_STREAK、但尾部连续无效 < MAX_STREAK」：
    只用 2 个无效轮时，「数全部」和「数尾部连续」都得 continue，反证照不出差别。
    """
    mod.start()
    state = mod._load()
    state["rounds"] = _rounds(mod.MAX_STREAK, False) + _rounds(1, True)
    mod._save(state)
    assert mod.verdict(mod._load())["action"] == "continue"


def test_verdict_stops_on_budget(mod):
    mod.start(budget=2)
    state = mod._load()
    state["rounds"] = _rounds(2, True)
    mod._save(state)
    v = mod.verdict(mod._load())
    assert v["action"] == "stop"
    assert "预算耗尽" in v["reason"]


def test_record_auto_stops_when_streak_reached(mod):
    mod.start()
    since = time.time()
    for _ in range(mod.MAX_STREAK):
        mod.record("t", "空转", "无产出", since_ts=since)
    state = mod._load()
    assert state["running"] is False
    assert "无有效产出" in state["stop_reason"]


def test_brief_refuses_after_stop(mod):
    mod.stop("手动停止")
    assert "已停" in mod.brief()


# ---------- 死磕与选题 ----------

def test_stuck_target_detected_when_metric_frozen(mod):
    mod.start()
    state = mod._load()
    state["rounds"] = _rounds(mod.STUCK_ROUNDS, True, target="bad", gap_ratio=0.5)
    mod._save(state)
    assert "bad" in mod.stuck_targets(mod._load())


def test_moving_metric_is_not_stuck(mod):
    mod.start()
    state = mod._load()
    state["rounds"] = _rounds(mod.STUCK_ROUNDS, True, target="ok", gap_ratio=0.5)
    for r, g in zip(state["rounds"], (0.5, 0.3)):
        r["gap_ratio"] = g
    mod._save(state)
    assert mod.stuck_targets(mod._load()) == set()


def test_pick_skips_stuck_target(mod):
    mod.start()
    state = mod._load()
    state["rounds"] = _rounds(mod.STUCK_ROUNDS, True, target="bad", gap_ratio=0.5)
    mod._save(state)
    gaps = [{"id": "bad", "score": 0.9}, {"id": "next", "score": 0.1}]
    assert mod.pick(gaps, mod._load())["id"] == "next"


def test_pick_returns_none_when_all_stuck(mod):
    mod.start()
    state = mod._load()
    state["rounds"] = _rounds(mod.STUCK_ROUNDS, True, target="bad", gap_ratio=0.5)
    mod._save(state)
    assert mod.pick([{"id": "bad", "score": 0.9}], mod._load()) is None


# ---------- 缺口评分 ----------

def test_gap_score_clamped_and_ordered(mod):
    g = mod._gap("x", "t", "m", "e", "a", gap_ratio=1.7, feasible=1.0)
    assert g["gap_ratio"] == 1.0 and g["score"] == 1.0
    g2 = mod._gap("y", "t", "m", "e", "a", gap_ratio=-0.5, feasible=1.0)
    assert g2["gap_ratio"] == 0.0


def test_observe_sorts_by_score(mod, monkeypatch):
    monkeypatch.setattr(mod, "EVALUATORS", [
        lambda: [mod._gap("lo", "t", "m", "e", "a", 0.1, 1.0)],
        lambda: [mod._gap("hi", "t", "m", "e", "a", 0.9, 1.0)],
    ])
    assert [g["id"] for g in mod.observe()] == ["hi", "lo"]


def test_broken_evaluator_does_not_kill_observe(mod, monkeypatch):
    def boom():
        raise RuntimeError("评估器挂了")
    monkeypatch.setattr(mod, "EVALUATORS", [boom,
                                            lambda: [mod._gap("ok", "t", "m", "e", "a", 0.5, 1.0)]])
    assert [g["id"] for g in mod.observe()] == ["ok"]


def test_recidivism_skips_non_recurring(mod, monkeypatch):
    """首见后再没犯的类别不该占选题位 —— 那是已经学会的。"""
    fake = {"classes": [
        {"code": "A", "name": "会复发", "count": 5, "share": 50.0, "fix": "f",
         "recurrence": {"after_first": 3, "span": 10}},
        {"code": "B", "name": "已学会", "count": 5, "share": 50.0,
         "recurrence": {"after_first": 0, "span": 10}},
    ]}
    monkeypatch.setattr(mod, "_run_json", lambda *a, **k: fake)
    ids = [g["id"] for g in mod.eval_recidivism()]
    assert ids == ["recidivism:A"]


def test_rule_metric_uses_weighted_merge(mod, monkeypatch):
    """加权合并必须按各天样本量，不能对三天的比率取算术平均。"""
    fake = {"trend": [
        {"plan_rate": 0.0, "multi_rounds": 100, "verify_rate": 1.0, "edit_rounds": 100},
        {"plan_rate": 1.0, "multi_rounds": 1, "verify_rate": 1.0, "edit_rounds": 1},
        {"plan_rate": 1.0, "multi_rounds": 1, "verify_rate": 1.0, "edit_rounds": 1},
    ]}
    monkeypatch.setattr(mod, "_run_json", lambda *a, **k: fake)
    monkeypatch.setattr(mod, "_pra_ratios", lambda: (0.5, 0.6), raising=False)
    gaps = {g["id"]: g for g in mod.eval_rule_metrics()}
    # 算术平均会是 0.67（无缺口）；加权 = 2/102 = 0.0196 → 必须报缺口
    assert "rules:plan_rate" in gaps
    assert gaps["rules:plan_rate"]["gap_ratio"] > 0.9


def test_rule_metric_needs_min_sample(mod, monkeypatch):
    fake = {"trend": [{"plan_rate": 0.0, "multi_rounds": 3,
                       "verify_rate": 0.0, "edit_rounds": 3}]}
    monkeypatch.setattr(mod, "_run_json", lambda *a, **k: fake)
    assert mod.eval_rule_metrics() == []


# ---------- 任务中心合成条目 ----------

def test_snapshot_none_when_never_run(mod):
    assert mod.snapshot() is None


def test_snapshot_running_entry(mod):
    mod.start()
    snap = mod.snapshot(full=True)
    assert snap["id"] == mod.TASK_ID
    assert snap["status"] == "running"
    assert snap["extra"]["self_iterate"] is True
    assert snap["extra"]["si"]["budget"] == mod.DEFAULT_BUDGET


def test_snapshot_error_status_when_stopped_by_streak(mod):
    mod.start()
    state = mod._load()
    state["rounds"] = _rounds(mod.MAX_STREAK, False)
    state["running"] = False
    state["stop_reason"] = "连续 3 轮无有效产出 —— 空转比不动更贵，停下等指令"
    mod._save(state)
    snap = mod.snapshot()
    assert snap["status"] == "error"
    assert "连续" in snap["error"]


def test_snapshot_marks_ineffective_round(mod):
    mod.start()
    mod.record("t", "空转", "无", since_ts=time.time())
    snap = mod.snapshot()
    assert "✘ 无效" in snap["steps"][0]


def test_state_file_is_valid_json(mod):
    mod.start()
    data = json.loads(mod.STATE_FILE.read_text(encoding="utf-8"))
    assert data["running"] is True


# ---------- 派发计数（预算不能靠执行体自觉） ----------

def test_tick_counts_dispatch(mod):
    mod.start()
    for _ in range(3):
        mod.tick()
    assert mod.spent(mod._load()) == 3


def test_budget_exhausts_by_dispatch_without_record(mod):
    """执行体一轮都没记账，预算照样要耗尽 —— 否则无人值守时就是无限烧钱。"""
    mod.start(budget=2)
    mod.tick()
    mod.tick()
    state = mod._load()
    assert state["running"] is False
    assert "预算耗尽" in state["stop_reason"]


def test_spent_takes_max_of_dispatch_and_record(mod):
    mod.start()
    mod.tick()
    (mod.STATE_FILE.parent / "anchor.json").write_text("{}", encoding="utf-8")
    mod.record("t", "a", "e", since_ts=time.time() - 1)
    mod.record("t", "a", "e", since_ts=time.time() - 1)
    assert mod.spent(mod._load()) == 2


def test_brief_round_number_follows_dispatch(mod, monkeypatch):
    monkeypatch.setattr(mod, "EVALUATORS", [])   # 不真跑评估器：这里只验轮号口径
    mod.start()
    mod.tick()
    state = mod._load()
    state["rounds"] = []
    mod._save(state)
    assert "第 2/24 轮" in mod.brief()


# ---------- 清除已完成（合成条目的老坑） ----------

def test_dismiss_refuses_while_running(mod):
    mod.start()
    assert mod.dismiss() is False
    assert mod.snapshot() is not None


def test_dismiss_hides_stopped_entry(mod):
    mod.start()
    mod.stop("手动停止")
    assert mod.dismiss() is True
    assert mod.snapshot() is None


def test_dismiss_noop_when_never_run(mod):
    assert mod.dismiss() is False


def test_restart_brings_entry_back_after_dismiss(mod):
    """清掉之后重新启动，条目必须回来 —— 否则用户以为它还在跑。"""
    mod.start()
    mod.stop("手动停止")
    mod.dismiss()
    assert mod.snapshot() is None
    mod.start()
    assert mod.snapshot() is not None


def test_restart_after_budget_exhausted_gets_fresh_budget(mod):
    """额度花完后重新点按钮，必须是新的预算周期 —— 否则一启动就又被判超额。"""
    mod.start(budget=2)
    mod.tick()
    mod.tick()
    assert mod._load()["running"] is False
    mod.start(budget=2)
    assert mod.used(mod._load()) == 0
    assert mod.verdict(mod._load())["action"] == "continue"
    mod.tick()
    assert mod._load()["running"] is True


def test_used_is_per_epoch_not_cumulative(mod):
    mod.start(budget=5)
    mod.tick()
    mod.tick()
    mod.stop("手动停止")
    mod.start(budget=5)
    assert mod.used(mod._load()) == 0
    assert mod.spent(mod._load()) == 2      # 累计不清零，历史留着
