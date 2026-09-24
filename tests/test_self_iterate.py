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


def test_missing_gap_ratio_does_not_kill_stuck(mod):
    """缺账的轮不能把「死磕」判据废掉 —— 第 5/6 轮的真实现场。

    任务书里的 record 命令长期只有四个参数，第 5/6 轮 gap_ratio 全是 null；
    旧判据要求「tail 里每个值都非 None」，于是 rules:plan_rate 指标已连修两轮
    纹丝不动却判不出死磕，第 7 轮又被派了一次。缺账 = 沿用该目标上一个已知读数。
    """
    mod.start()
    state = mod._load()
    state["rounds"] = [
        {"n": 3, "target": "rules:plan_rate", "effective": True, "gap_ratio": 0.342,
         "action": "a", "evidence": "e"},
        {"n": 5, "target": "rules:plan_rate", "effective": True, "gap_ratio": None,
         "action": "a", "evidence": "e"},
        {"n": 6, "target": "rules:plan_rate", "effective": True, "gap_ratio": None,
         "action": "a", "evidence": "e"},
    ]
    mod._save(state)
    assert "rules:plan_rate" in mod.stuck_targets(mod._load())
    assert mod.pick([{"id": "rules:plan_rate", "score": 0.9},
                     {"id": "rules:verify_rate", "score": 0.1}], mod._load())["id"] \
        == "rules:verify_rate"


def test_stuck_needs_one_known_reading(mod):
    """一个读数都没落过账 → 未知，不许判死磕。

    宁可再给一轮，也不许把「没测到」当成「没变」：判据一旦这么退化，
    它就会在指标其实在动的时候也把目标踢出选题池。
    """
    mod.start()
    state = mod._load()
    state["rounds"] = [{"n": i + 1, "target": "rules:x", "effective": True,
                        "gap_ratio": None, "action": "a", "evidence": "e"}
                       for i in range(mod.STUCK_ROUNDS)]
    mod._save(state)
    assert mod.stuck_targets(mod._load()) == set()


def test_moving_metric_beats_missing_ratio(mod):
    """反证：读数真动了（最新一轮落账的新数字与旧值不同）→ 必须撤销死磕判定。

    这条要是红了，说明「沿用旧值」被写成了「无视新值」，判据就退化成恒真规则。
    """
    mod.start()
    state = mod._load()
    state["rounds"] = [
        {"n": 3, "target": "rules:plan_rate", "effective": True, "gap_ratio": 0.342,
         "action": "a", "evidence": "e"},
        {"n": 5, "target": "rules:plan_rate", "effective": True, "gap_ratio": None,
         "action": "a", "evidence": "e"},
        {"n": 7, "target": "rules:plan_rate", "effective": True, "gap_ratio": 0.100,
         "action": "a", "evidence": "e"},
    ]
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


def _gate_case(monkeypatch, mod, count, share, replay):
    """按 code=A 造一份 err/replay 双读数，跑一次 eval_recidivism。"""
    err = {"classes": [
        {"code": "A", "name": "技能未加载", "count": count, "share": share, "fix": "f",
         "recurrence": {"after_first": 24, "span": 65}},
    ]}

    def fake(script, extra=None):
        return replay if script.startswith("replay") else err

    monkeypatch.setattr(mod, "_run_json", fake)
    return mod.eval_recidivism()[0]


def test_recidivism_gate_zeroes_feasible_when_replay_clean(mod, monkeypatch):
    """重放已无同类错的类别 feasible=0：冻结 traces 的计数不会动，别永远顶第一位。"""
    g = _gate_case(monkeypatch, mod, 43, 75.4,
                   {"classes": {"A": {"total": 43, "gone": 43, "still": 0, "clean": 34}}})
    assert g["id"] == "recidivism:A"
    assert g["feasible"] == 0.0 and g["score"] == 0.0
    assert "重放 43 次已无同类错" in g["evidence"]


def test_recidivism_gate_holds_when_replay_partial(mod, monkeypatch):
    """重放只覆盖一部分就保持可行：没全量验证过不算修死（C 类实测 4/9）。"""
    g = _gate_case(monkeypatch, mod, 9, 15.8,
                   {"classes": {"A": {"total": 4, "gone": 4, "still": 0, "clean": 4}}})
    assert g["feasible"] == 1.0 and g["score"] > 0
    assert "重放仅覆盖 4/9 次" in g["evidence"]


def test_recidivism_gate_holds_when_still_failing(mod, monkeypatch):
    """重放里仍报同类错 → 缺口是真的，可行度不降。"""
    g = _gate_case(monkeypatch, mod, 43, 75.4,
                   {"classes": {"A": {"total": 43, "gone": 38, "still": 5, "clean": 30}}})
    assert g["feasible"] == 1.0
    assert "仍报同类错 5 次" in g["evidence"]


def test_recidivism_gate_retires_when_residual_is_structurally_unfixable(mod, monkeypatch):
    """残留行本机制治不了（模型传错参数 / args 被截断不可判）→ 允许退选。

    反证方式：同一组读数把 still_fixable 改成 1，feasible 必须弹回 1.0——
    这条测试同时锁住「不许静默放宽」和「不许把修好的类永远钉住」。
    """
    stale = {"classes": {"A": {"total": 4, "gone": 2, "still": 1,
                               "still_fixable": 0, "unjudgeable": 1, "clean": 2}}}
    g = _gate_case(monkeypatch, mod, 4, 7.0, stale)
    assert g["feasible"] == 0.0 and g["score"] == 0.0
    assert "结构性不可治" in g["evidence"] and "截断不可判 1" in g["evidence"]

    fixable = {"classes": {"A": {"total": 4, "gone": 2, "still": 1,
                                 "still_fixable": 1, "unjudgeable": 1, "clean": 2}}}
    g2 = _gate_case(monkeypatch, mod, 4, 7.0, fixable)
    assert g2["feasible"] == 1.0, "机制能覆盖却仍失败 → 必须继续占选题位"
    assert "仍报同类错 1 次（本轮修复机制可覆盖）" in g2["evidence"]


def test_recidivism_gate_old_reading_without_fixable_key_not_relaxed(mod, monkeypatch):
    """旧读数（无 still_fixable 字段）不许被当成「结构性不可治」而放行。"""
    g = _gate_case(monkeypatch, mod, 4, 7.0,
                   {"classes": {"A": {"total": 4, "gone": 2, "still": 1, "clean": 2}}})
    assert g["feasible"] == 1.0


def test_recidivism_gate_unverified_when_replay_broken(mod, monkeypatch):
    """重放跑不动 → 按「未验证」处理，不许当已修死静默降级。"""
    g = _gate_case(monkeypatch, mod, 43, 75.4, None)
    assert g["feasible"] == 1.0
    assert "重放未覆盖该类" in g["evidence"]


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


def test_brief_hands_down_the_reading(mod, monkeypatch):
    """任务书的 record 命令必须带上本轮读数。

    缺账正是「死磕」判不出来的一半原因（另一半是旧判据把 None 当未知）；
    读数随命令一起交下去，判据就能用事实说话，而不是靠沿用旧值推断。
    """
    monkeypatch.setattr(mod, "EVALUATORS", [])
    monkeypatch.setattr(mod, "observe", lambda: [{
        "id": "rules:plan_rate", "title": "t", "metric": "33% · n=231",
        "evidence": "e", "action": "a", "gap_ratio": 0.342, "feasible": 0.8,
        "score": 0.2736}])
    mod.start()
    mod.tick()
    out = mod.brief()
    assert "--target rules:plan_rate" in out
    assert "--gap-ratio 0.342" in out
    # 批判人格定向到同一个缺口时仍算缺口轮：读数照样要下发（定向到别的目标才不发）
    st = mod._load()
    st["critiques"] = [{"n": 1, "after_round": len(st["rounds"]), "verdict": "v",
                        "directives": [{"claim": "c", "evidence": "e", "refutable": "r",
                                        "target": "rules:plan_rate"}]}]
    mod._save(st)
    out = mod.brief()
    assert "--target rules:plan_rate" in out and "--gap-ratio 0.342" in out


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


# ---------- 选题：已修死的缺口不进池 ----------

def test_pick_skips_infeasible(mod):
    """feasible=0（重放证明已修死）不占选题位，让位给还能动的缺口。"""
    mod.start()
    gaps = [{"id": "dead", "score": 0.0, "feasible": 0.0},
            {"id": "live", "score": 0.1, "feasible": 1.0}]
    assert mod.pick(gaps, mod._load())["id"] == "live"


def test_pick_none_when_only_infeasible(mod):
    """只剩已修死的缺口 → 返回 None（本轮该去学东西，不是重修一遍）。"""
    mod.start()
    assert mod.pick([{"id": "dead", "score": 0.0, "feasible": 0.0}], mod._load()) is None


def test_pick_keeps_gap_without_feasible_field(mod):
    """缺 feasible 字段按未知处理 —— 手写缺口不该被当成不可行。"""
    mod.start()
    assert mod.pick([{"id": "unknown", "score": 0.5}], mod._load())["id"] == "unknown"


# ---------- 重启恢复：该不该把循环拉起来 ----------

def test_resume_plan_false_when_user_stopped(mod):
    """用户按过停止 → 重启也不许自己开回来。"""
    mod.start(budget=3)
    mod.stop("手动停止")
    p = mod.resume_plan(mod._load())
    assert p["resume"] is False
    assert p["reason"] == "手动停止"


def test_resume_plan_true_when_running(mod):
    """在跑 → 该恢复，且说明停在哪一轮（断点可见）。"""
    mod.start(budget=3)
    mod.tick()
    p = mod.resume_plan()
    assert p["resume"] is True
    assert "2/3" in p["reason"]


def test_resume_plan_false_when_budget_exhausted(mod):
    """预算耗尽后重启不能偷偷续命 —— 无人值守时那就是无限烧钱。"""
    mod.start(budget=1)
    mod.tick()
    p = mod.resume_plan()
    assert p["resume"] is False
    assert "预算" in p["reason"]


def test_resume_plan_false_when_streak_dead(mod):
    """连续无效轮已经触发自动停 → 重启不该把它救活。"""
    mod.start(budget=9)
    state = mod._load()
    state["rounds"] = _rounds(3, effective=False)
    state["dispatched"] = 3
    mod._save(state)
    p = mod.resume_plan()
    assert p["resume"] is False
    assert "连续" in p["reason"]


# ---------- 第二自我：批判人格 ----------
# 这一段的判据不是「批判能生成」，而是「自嗨型批判进不来」：
# 没有证据出处、没有可推翻判据、挂在不存在目标上的指引，必须一个字都写不进去。

def _ok_directive(target="learn"):
    return {"claim": "先跑 prompt_rules_audit 把口径确认清楚",
            "evidence": "tools/prompt_rules_audit.py:1 与轮 #2 的证据串",
            "refutable": "若采样轮数 <2 且 plan_rate 未动，这条指引作废",
            "target": target}


def _ok_payload(target="learn"):
    return {"verdict": "本轮有效，但方向偏向指标打磨而非总目标",
            "alignment": "服务于 self-iterate 的验收标准：有效轮要可复核",
            "risk": "预算被次要指标吃掉",
            "directives": [_ok_directive(target)]}


@pytest.fixture()
def fake_ledger(monkeypatch, mod):
    """台账/信条读盘隔离：测试不该依赖真仓库此刻的事业清单。"""
    monkeypatch.setattr(mod, "_read_root_json", lambda name: {
        "long_horizon.json": {
            "projects": [{"id": "self-iterate", "title": "T", "stage": "active",
                          "value": "V", "next": "N", "progress": 1, "log": []}],
            "questions": [{"text": "Q?", "status": "open"}],
        },
        "conviction.json": {"convictions": [{"text": "先做出来，再谈对不对"}],
                            "vetoes": [{"claim": "不绕风控采集"}]},
    }.get(name, {}))
    return mod


def test_critique_rejected_without_evidence(mod, fake_ledger):
    """「我觉得」不许进 —— 拒收且一个字都不写。"""
    p = _ok_payload()
    p["directives"][0]["evidence"] = "我觉得"
    assert any("evidence" in e for e in mod.validate_critique(p))
    with pytest.raises(ValueError):
        mod.submit_critique(p)
    assert mod.latest_critique() is None


def test_critique_rejected_without_refutable(mod, fake_ledger):
    p = _ok_payload()
    p["directives"][0]["refutable"] = ""
    assert any("refutable" in e for e in mod.validate_critique(p))


def test_critique_rejected_on_unknown_target(mod, fake_ledger):
    """指引必须挂到总目标或评估器缺口上，不能凭空发明一个方向。"""
    p = _ok_payload(target="我自己想的")
    errs = mod.validate_critique(p)
    assert any("target" in e for e in errs)
    assert mod.target_ok("self-iterate") and mod.target_ok("rules:plan_rate")
    assert mod.target_ok("recidivism:A") and not mod.target_ok("learn2")


def test_critique_rejects_directive_flood(mod, fake_ledger):
    p = _ok_payload()
    p["directives"] = [_ok_directive() for _ in range(mod.MAX_DIRECTIVES + 1)]
    assert any("最多" in e for e in mod.validate_critique(p))


def test_critique_rejects_empty_directives(mod, fake_ledger):
    p = _ok_payload()
    p["directives"] = []
    assert any("至少" in e for e in mod.validate_critique(p))


def test_critique_accepted_and_persisted(mod, fake_ledger):
    mod.start()
    e = mod.submit_critique(_ok_payload(target="self-iterate"))
    assert e["n"] == 1 and e["after_round"] == 0
    assert mod.latest_critique()["verdict"] == e["verdict"]
    assert mod.latest_critique(fresh_only=True) is not None
    assert mod.status()["critic"]["targets"] == ["self-iterate"]
    assert mod.status()["critic"]["fresh"] is True


def test_brief_injects_critique_and_hands_over_target(mod, fake_ledger, monkeypatch):
    """批判定向优先于评估器缺口 —— 否则「结合总目标」只是一句装饰。"""
    monkeypatch.setattr(mod, "EVALUATORS", [
        lambda: [mod._gap("rules:plan_rate", "多步轮没列清单", "28%",
                          "audit --json: 0.28", "查判据", 0.4, 0.8)]])
    mod.start()
    mod.submit_critique(_ok_payload(target="self-iterate"))
    out = mod.brief()
    assert "第二自我（批判人格）" in out
    assert "本轮目标：self-iterate" in out
    assert "rules:plan_rate 让位" in out
    assert "--target self-iterate" in out      # 契约里的 target 跟着换
    assert "改判条件" in out


def test_stale_critique_does_not_hijack_next_round(mod, fake_ledger, monkeypatch):
    """批判必须每轮重新挣得：针对旧轮次的指引不许继续霸占选题位。"""
    monkeypatch.setattr(mod, "EVALUATORS", [
        lambda: [mod._gap("rules:plan_rate", "多步轮没列清单", "28%",
                          "audit --json: 0.28", "查判据", 0.4, 0.8)]])
    mod.start()
    mod.submit_critique(_ok_payload(target="self-iterate"))
    state = mod._load()
    state["rounds"] = _rounds(1, True)
    mod._save(state)
    assert mod.latest_critique() is not None
    assert mod.latest_critique(fresh_only=True) is None
    out = mod.brief()
    assert "第二自我" not in out
    assert "本轮目标：" not in out
    assert "目标缺口：rules:plan_rate" in out


def test_brief_falls_back_to_gap_without_critique(mod, fake_ledger, monkeypatch):
    monkeypatch.setattr(mod, "EVALUATORS", [
        lambda: [mod._gap("rules:plan_rate", "多步轮没列清单", "28%",
                          "audit --json: 0.28", "查判据", 0.4, 0.8)]])
    mod.start()
    out = mod.brief()
    assert "目标缺口：rules:plan_rate" in out and "第二自我" not in out


def test_critique_pack_carries_vision_and_ledger(mod, fake_ledger, monkeypatch):
    """材料包必须真带上愿景/台账/信条/近期变更，否则批判只能凭记忆发挥。"""
    monkeypatch.setattr(mod, "EVALUATORS", [
        lambda: [mod._gap("rules:plan_rate", "多步轮没列清单", "28%",
                          "audit --json: 0.28", "查判据", 0.4, 0.8)]])
    monkeypatch.setattr(mod, "_git", lambda args, timeout=20: "abc123 上一轮改动")
    mod.start()
    mod.record("learn", "学了点东西", "docs/x.md:12", since_ts=time.time() - 1)
    pack = mod.critique_pack()
    assert "批判人格" in pack
    assert "长期事业台账" in pack and "self-iterate" in pack
    assert "悬而未决" in pack and "Q?" in pack
    assert "信条与拒绝记录" in pack and "先做出来" in pack
    assert "rules:plan_rate" in pack and "abc123" in pack
    assert "critique-submit" in pack


def test_critique_pack_survives_missing_ledger(mod, monkeypatch):
    """台账缺失/损坏不该让整轮崩 —— 材料缺一块，批判照跑。"""
    monkeypatch.setattr(mod, "_read_root_json", lambda name: {})
    monkeypatch.setattr(mod, "EVALUATORS", [])
    monkeypatch.setattr(mod, "_git", lambda args, timeout=20: "")
    mod.start()
    pack = mod.critique_pack()
    assert "材料包" in pack and "（空）" in pack


# ---------- 批判 → 用户端输入 ----------
# 判据：批判人格的指引必须以「用户端输入」形态下发（独立 user 消息、效力等同主人指令），
# 而不是任务书里的一段背景文字 —— 后者模型可以当参考绕过。旧指引不许一直霸占通道。

def test_user_input_block_carries_critique(mod, fake_ledger):
    mod.submit_critique(_ok_payload())
    txt = mod.user_input_block()
    assert mod.CRITIC_USER_PREFIX in txt
    assert "效力等同主人" in txt
    assert "prompt_rules_audit.py:1" in txt          # 证据出处跟着一起走
    assert "改判条件" in txt
    assert "不许降级成待办" in txt


def test_user_input_block_demands_critique_when_missing(mod, fake_ledger):
    """批判缺失不再静默返回空串 —— 缺了就让本轮把它补上。

    旧契约是「没有批判就注入空串」，代价是：轮末被重启/打断掉批判后，
    下一轮在没有任何指引的状态下自由发挥，循环退化成自己给自己出题。
    """
    txt = mod.user_input_block()
    assert mod.CRITIC_USER_PREFIX in txt
    assert "补跑第 5 步" in txt and "critique-submit" in txt


def test_user_input_block_drops_stale_critique(mod, fake_ledger):
    """旧指引不许一直霸占用户输入通道：它必须每轮被重新挣得。

    过期后注入的是「本轮补一条新批判」，而不是旧的 directives。
    """
    mod.submit_critique(_ok_payload())
    assert "效力等同主人" in mod.user_input_block()
    mod.record(target="learn", action="改了 X", evidence="pytest 通过", since_ts=0)
    after = mod.user_input_block()
    assert "改判条件" not in after          # 旧指引不再下发
    assert "补跑第 5 步" in after            # 换成「把这一轮的批判补上」


def test_user_input_block_demands_critique_when_directives_empty(mod, fake_ledger):
    """批判在、但没有可用指令 → 同样按「缺批判」处理（空指令等于没指引）。"""
    mod.submit_critique(_ok_payload())
    st = mod._load()
    st["critiques"][-1]["directives"] = []
    mod._save(st)
    assert "补跑第 5 步" in mod.user_input_block()


# ---------- 停摆判据 / 强制唤醒 ----------
# 判据：循环停在「状态说在跑、实际不动」上，必须能被外部识别并给出动作。
# 重启会杀掉正在执行的轮（dispatched 已扣预算、record 没落），没有外力补就得
# 等下一个 40 分钟周期 —— 这是「一晚上只迭代两三轮」的直接原因。

def _job(enabled=True, running=False, last_run_at=0.0):
    return {"id": "sched-x", "enabled": enabled, "running": running,
            "last_run_at": last_run_at, "interval_sec": 2400}


def test_stall_plan_none_when_not_running(mod):
    """没启动就不许「唤醒」—— 用户按过停止的循环不能被看门狗偷偷拉起来。"""
    assert mod.stall_plan(job=_job())["action"] == "none"


def test_stall_plan_resume_when_job_missing(mod):
    mod.start()
    assert mod.stall_plan(job=None)["action"] == "resume"


def test_stall_plan_resume_when_job_disabled(mod):
    mod.start()
    assert mod.stall_plan(job=_job(enabled=False))["action"] == "resume"


def test_stall_plan_release_when_job_running_hung(mod):
    """调度任务的 running 标记悬挂（_sweep_stale 要等 2 小时）→ 先清标记。"""
    mod.start()
    now = time.time()
    p = mod.stall_plan(job=_job(running=True, last_run_at=now - mod.STALL_SEC - 1), now=now)
    assert p["action"] == "release"


def test_stall_plan_wake_on_broken_round(mod):
    """派发了却没记账、且超过 STALL_SEC → 立刻补一轮（重启打断的典型形状）。"""
    mod.start()
    now = time.time()
    st = mod._load()
    st["dispatched"] = 1
    st["last_ts"] = now - mod.STALL_SEC - 1
    mod._save(st)
    p = mod.stall_plan(job=_job(), now=now)
    assert p["action"] == "wake" and "1 轮" in p["reason"]


def test_stall_plan_quiet_while_round_in_flight(mod):
    """正常在跑（派发后 5 分钟）不许当断轮 —— 误判会去撞正在跑的那一轮。"""
    mod.start()
    now = time.time()
    st = mod._load()
    st["dispatched"] = 1
    st["last_ts"] = now - 300
    mod._save(st)
    assert mod.stall_plan(job=_job(), now=now)["action"] == "none"


def test_stall_plan_quiet_after_record(mod):
    """记账落盘后 dispatched 与 rounds 对齐 → 停摆解除。"""
    mod.start()
    mod.record(target="learn", action="改了 X", evidence="pytest 通过", since_ts=0)
    st = mod._load()
    st["dispatched"] = len(st["rounds"])
    mod._save(st)
    assert mod.stall_plan(job=_job())["action"] == "none"


def test_stall_plan_wake_when_cycle_started_but_schedule_far(mod):
    """启动后一直没派发、排期还在一整个 STALL_SEC 之外 → 拉到眼前。

    实测形状：09:56:36 点启动，next_run_at 仍是第 12 轮派发算出的 10:16:26 ——
    界面显示「运行中」，实际第一轮被排在启动前的旧排期上。
    """
    mod.start()
    now = time.time()
    st = mod._load()
    st["started_at"] = now - mod.STALL_SEC - 60
    mod._save(st)
    job = _job(last_run_at=0)
    job["next_run_at"] = now + mod.STALL_SEC + 600
    p = mod.stall_plan(job=job, now=now)
    assert p["action"] == "wake" and "没派发" in p["reason"]


def test_stall_plan_quiet_when_schedule_within_stall_sec(mod):
    """正常节奏不许误伤：排期就在眼前（5 分钟后），等它是设计行为。"""
    mod.start()
    now = time.time()
    st = mod._load()
    st["started_at"] = now - mod.STALL_SEC - 60
    mod._save(st)
    job = _job(last_run_at=0)
    job["next_run_at"] = now + 300
    assert mod.stall_plan(job=job, now=now)["action"] == "none"


def test_note_wake_keeps_last_20(mod):
    """留痕有上限：看门狗每小时都可能动一次，不许把状态文件撑爆。"""
    for i in range(25):
        mod.note_wake("wake", f"r{i}")
    ws = mod._load()["wakeups"]
    assert len(ws) == 20 and ws[-1]["reason"] == "r24"


# ---------- gap_ratio 的计数权不在调用者手里（第 7 轮批判）----------
# 背景：判据的输入由被审判者自己提供，一句话就能让「死磕」判不出来。第 7 轮实测：
# --gap-ratio 是记账命令的一个自由参数，传 0.9、真实读数 0.342 也照单全收，
# 而 stuck_targets 只看这个数变没变。核对放在 record 里，以 observe() 现算为准。

def _gaps(*items):
    return lambda: list(items)


def _one_gap(ratio, gid="rules:x"):
    return {"id": gid, "title": "t", "metric": "m", "evidence": "e",
            "action": "a", "gap_ratio": ratio, "feasible": 0.8, "score": 0.1}


def test_record_uses_live_reading_over_self_reported(mod, monkeypatch, tmp_path):
    """反证判据：自报 0.9、现算 0.342 → 落账必须是 0.342。

    什么观测会推翻它：state 里存着 0.9（自报值照用），或存着 0.342 但 note 里
    没有覆盖说明（读账的人无从知道谁说了算）。
    """
    (tmp_path / "anchor.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(mod, "observe", _gaps(_one_gap(0.342)))
    e = mod.record("rules:x", "改了", "证据", since_ts=time.time() - 1, gap_ratio=0.9)
    assert e["gap_ratio"] == 0.342
    assert "以 observe 现算为准" in e["note"]
    assert mod._load()["rounds"][-1]["gap_ratio"] == 0.342


def test_record_keeps_ratio_when_it_matches(mod, monkeypatch, tmp_path):
    """自报与现算一致（含四舍五入余差）→ 原样落账，不加噪音 note。"""
    (tmp_path / "anchor.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(mod, "observe", _gaps(_one_gap(0.3420)))
    e = mod.record("rules:x", "改了", "证据", since_ts=time.time() - 1, gap_ratio=0.3421)
    assert e["gap_ratio"] == 0.3420
    assert e["note"] == ""


def test_record_rejects_unverifiable_ratio(mod, monkeypatch):
    """自报了一个观测里现在算不出来的缺口 → 拒收：无法核对的读数不许进死磕判定。"""
    monkeypatch.setattr(mod, "observe", _gaps())
    with pytest.raises(ValueError):
        mod.record("rules:x", "改了", "证据", since_ts=time.time(), gap_ratio=0.5)
    assert not mod._load().get("rounds")      # 拒收 = 没落账，调用方看得见、可重跑


def test_non_evaluator_target_skips_the_check(mod, monkeypatch):
    """learn / 批判定向这类目标本来就不在观测里：不许因为核对把它们判成无效。"""
    called = []
    monkeypatch.setattr(mod, "observe", lambda: (called.append(1), [])[1])
    e = mod.record("learn", "学了点东西", "docs/x.md:12", since_ts=time.time())
    assert e["target"] == "learn"
    assert called == []                       # 连 observe 都不跑：省掉一次全量评估


# ---------- 台账落盘 id 由任务书写死（第 7 轮批判）----------
# 背景：第 5/6 轮的任务书只写了「log <id>」，id 空着，执行体自己挑到了 oss-contrib——
# 自我迭代的接力棒记进了开源贡献那本账（long_horizon.json 里那两条 plan_miss 记录）。

def test_ledger_id_only_from_real_ledger(mod):
    """映射结果必须是台账里真实存在的 id。

    2026-09-24 修正：本用例原先断言 "self-iterate" / "hermes-learning-loop" 在台账里
    ——两者在 long_horizon.json 里**从来没有出现过**（git log -S 两个名字都查无提交），
    所以这三条断言从写下那天起就是红的，只是没人跑过这个文件。现在改成对着真实台账
    id 断言：映射结果必须 ∈ ledger_ids()。
    """
    ids = mod.ledger_ids()
    assert ids, "读不到台账，本用例失去意义"
    got_rule = mod.ledger_id_for("rules:verify_rate")
    got_rec = mod.ledger_id_for("recidivism:A")
    got_learn = mod.ledger_id_for("learn")
    for got in (got_rule, got_rec, got_learn):
        assert got in ids, f"{got!r} 不在台账 {ids} 里"
    # 自我迭代自己的账本 = self-evolution（title「自我进化闭环」）
    assert got_rule == got_rec
    # 台账里没有的名字不硬塞，退回自我迭代自己的账本
    assert mod.ledger_id_for("longrun-engine") == got_rule


def test_ledger_id_never_invented(mod, monkeypatch):
    """台账里没有 self-iterate 时返回空串（退回旧的 <id> 写法），不许编一个 id 出来。"""
    monkeypatch.setattr(mod, "ledger_ids", lambda: ["oss-contrib"])
    assert mod.ledger_id_for("rules:plan_rate") == ""
    monkeypatch.setattr(mod, "ledger_ids", lambda: [])
    assert mod.ledger_id_for("rules:plan_rate") == ""


def test_brief_writes_the_ledger_id(mod, monkeypatch):
    """任务书必须写死 id，并说清「不许自选」。

    什么观测会推翻它：把 ledger_ids() 掏空后任务书里仍然出现具体 id（说明那个 id
    是硬编码进文案的、没跟真实台账对齐）。
    """
    mod.start()
    monkeypatch.setattr(mod, "observe", _gaps(_one_gap(0.1983, "rules:verify_rate")))
    text = mod.brief()
    real = mod.ledger_id_for("rules:verify_rate")
    assert real, "台账里应有自我迭代自己的账本 id"
    assert f"落盘 id 已由任务书写死：{real}" in text
    assert "不许自选" in text
    # 台账读不到时退回 `<id>` 占位（宁可显式暴露，也不许编一个 id）
    monkeypatch.setattr(mod, "ledger_ids", lambda: [])
    assert "落盘 id 已由任务书写死：<id>" in mod.brief()


def test_brief_learn_round_writes_its_own_ledger_id(mod, monkeypatch):
    """学习轮没有评估器缺口，落盘 id 同样是任务书写死的（学习闭环那本账）。"""
    mod.start()
    monkeypatch.setattr(mod, "observe", _gaps())
    text = mod.brief()
    real = mod.ledger_id_for("learn")
    assert real, "台账里应有可落的账本 id"
    assert f"long_horizon.py log {real}" in text


def test_记账不许把真停因覆盖成未启动(mod):
    """循环被停掉之后在飞的执行体才记账时，record 不许把真停因冲成「未启动」。

    实测形状：10:24:49 停止（stop_reason=手动停止 / stop_kind=user），10:25:50 那一轮
    的执行体记账，record 里 verdict 只回「未启动」，照写就把停因冲成一句废话 ——
    界面显示「停止原因：未启动」，resume_plan 也认不出它到底为什么停的。
    """
    mod.start()
    mod.stop("手动停止")
    mod.record(target="learn", action="改了 X", evidence="pytest 通过", since_ts=0)
    st = mod._load()
    assert st["running"] is False
    assert st["stop_reason"] == "手动停止"
    assert st["stop_kind"] == "user"


# ---------- 契约：任务书里的落盘 id 必须是台账里真实存在的 id（2026-09-24） ----------
#
# 背景（第 2 轮实测）：LEDGER_ID_BY_PREFIX 把 rules:* 映射到 "self-iterate"，而台账
# long_horizon.json 里的这本账 id 是 "self-evolution"。ledger_id_for() 拿真实 id 校验
# 不过就退回空串，任务书于是渲染成字面量「落盘 id 已由任务书写死：<id>」——执行体照着
# 任务书没法落盘，只能自选 id，正是「第 5/6 轮把接力棒记进 oss-contrib」那类事故的温床。
#
# 什么观测会推翻它：若哪天台账把这本账改名，任务书又出现 `<id>` 占位符，说明对照表与
# 台账再次脱节——那时该改成读台账自动发现，而不是再手写一个常量。


def test_ledger_id_placeholder_never_leaks_into_brief(tmp_path, monkeypatch):
    """任务书里不许出现 `<id>` 占位符；必须给出台账里真实存在的 id。"""
    import json as _json
    m = _load()
    ledger = tmp_path / "long_horizon.json"
    ledger.write_text(_json.dumps({"projects": [
        {"id": "self-evolution", "title": "自我进化闭环"},
        {"id": "pdd-cs", "title": "店铺双系统"}]}, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(m, "LEDGER_FILE", ledger)

    got = m.ledger_id_for("rules:verify_rate")
    assert got == "self-evolution", got
    assert got in [p["id"] for p in _json.loads(ledger.read_text(encoding="utf-8"))["projects"]]
    # 映射表里的每个目标都必须落到一个真实 id 上（learn 这本账不在表里时退回 self-evolution）
    for t in ("rules:plan_rate", "recidivism:whatever", "self-iterate"):
        assert m.ledger_id_for(t) in ("self-evolution", ""), t


def test_ledger_mapping_points_at_existing_id():
    """对照表指向的 id 必须在真实台账里存在（防止再写一个不存在的名字）。

    本轮实测：表里写的是 "self-iterate"，台账里是 "self-evolution" —— 两者对不上，
    任务书于是渲染出 `<id>` 占位符（brief 第 15 行），执行体只能自选 id。
    """
    m = _load()
    ids = m.ledger_ids()
    if not ids:
        pytest.skip("读不到台账，跳过")
    for prefix in ("rules", "recidivism", "self-iterate", "self-evolution"):
        mapped = m.LEDGER_ID_BY_PREFIX.get(prefix)
        assert mapped in ids, f"{prefix} -> {mapped} 不在台账 {ids} 里"


def test_brief_never_renders_placeholder_with_real_ledger():
    """回归：真实台账在场时，任务书的落盘 id 行**不许**出现 `<id>` 占位符。

    这是本轮那个坑的直接判据——修好之前 brief 里就是字面量 `<id>`。
    """
    m = _load()
    if not m.ledger_ids():
        pytest.skip("读不到台账，跳过")
    got = m.ledger_id_for("rules:verify_rate")
    assert got and got != "<id>", got
    assert got in m.ledger_ids()
