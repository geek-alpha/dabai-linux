#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""规则区审计工具的契约：成本要算对、判定要可复现、映射表不许悄悄过期。

背景（2026-09-14）：规则区 11 条 / 1432 字符 / 占系统提示词 26%，但「删哪条」
一直靠感觉。做审计工具时发现真正的卡点是数据——11 条里只有 4 条能给出结论，
其余 7 条得分清是「缺数据源」（补埋点）还是「有埋点但样本不足」（等样本）：
混成一类会让下一轮去补根本不需要的埋点。

契约：
  1. 映射表必须覆盖 agent.py 里每一条规则——规则文本一改就报 drift，
     否则审计表会悄悄过期，得出的「零收益候选」是假的
  2. 并行度 < 2.0/批 → MEASURED_GAP；>= 2.0 → MEASURED_OK
  3. 坏行为率 < 1% → MEASURED_OK（规则零收益，候选删）；>= 1% → MEASURED_GAP
  4. 无埋点的规则必须如实标 UNMEASURED，不许猜成 OK 或 GAP
  5. 数据文件缺失 → 不抛异常，如实报空样本
  6. 没有 seg.sys 数据时 render 不许崩（占比未知要能表达）

这些用例不写真实日志，全部造数据；只有覆盖性用例读真实 agent.py。
"""
import importlib.util
import json
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parents[1]


def _load_audit():
    """tools/ 不是包，按文件路径加载。每次拿干净模块，避免用例互相污染。"""
    path = BASE / "tools" / "prompt_rules_audit.py"
    spec = importlib.util.spec_from_file_location("prompt_rules_audit_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _rows(tool_calls, batches, tool_rounds=None, re_reads=0, truncations=0, sys_chars=5000):
    return [{
        "ts": 1_789_000_000.0,
        "tool_calls": tool_calls,
        "batches": batches,
        "tool_rounds": tool_rounds if tool_rounds is not None else tool_calls,
        "re_reads": re_reads,
        "truncations": truncations,
        "seg": {"sys": sys_chars},
    }]


def _write_metrics(tmp_path, rows):
    p = tmp_path / "turn_metrics.jsonl"
    p.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows),
                 encoding="utf-8")
    return p


# ---------- 契约 1：映射表覆盖性 ----------

def test_rule_map_covers_every_rule():
    """真实 agent.py 的每条规则都能在 RULE_MAP 里找到归属。"""
    mod = _load_audit()
    rep = mod.build()
    assert rep["map_drift"] is False, f"未覆盖的规则：{rep['unmatched']}"
    assert rep["mapped"] == rep["rules_count"] > 0


def test_drift_is_reported_and_rc_nonzero(tmp_path, monkeypatch, capsys):
    """规则文本改了而映射表没跟上 → 必须显式报 drift，不许静默给出结论。"""
    mod = _load_audit()
    # 假装 agent.py 多了一条谁也不认识的规则
    monkeypatch.setattr(mod, "extract_rules",
                        lambda: (["⚠ 一条映射表没覆盖的新规则"], "⚠ 一条映射表没覆盖的新规则"))
    monkeypatch.setattr(mod, "METRICS", _write_metrics(tmp_path, []))
    monkeypatch.setattr(mod, "SUBAGENTS", tmp_path / "none.jsonl")
    monkeypatch.setattr(mod.sys, "argv", ["prompt_rules_audit.py"])
    assert mod.main() == 1
    out = capsys.readouterr().out
    assert "映射表失配" in out


# ---------- 契约 2/3：判定阈值 ----------

def test_batch_ratio_below_target_is_gap():
    mod = _load_audit()
    agg = mod.totals(_rows(tool_calls=100, batches=100))   # 全串行
    verdict, obs = mod.judge("parallel", "batch_ratio", agg, [])
    assert verdict == "MEASURED_GAP"
    assert "1.0" in obs


def test_batch_ratio_at_target_is_ok():
    mod = _load_audit()
    agg = mod.totals(_rows(tool_calls=100, batches=50, tool_rounds=50))  # 每往返 2 个
    verdict, _ = mod.judge("parallel", "batch_ratio", agg, [])
    assert verdict == "MEASURED_OK"


def test_batch_ratio_judges_llm_rounds_not_exec_batches():
    """判据必须是「每次往返发几个工具」，不是执行分批——后者是调度器切的，模型管不了。

    实测 194 轮：per_batch 1.078 / per_llm_round 1.372。用 per_batch 当判据是错配，
    而且它只吃墙钟不吃 token；能省 token 的只有往返次数。这个用例锁死判据来源。
    """
    mod = _load_audit()
    # 每批 2 个（执行层并行了）但每往返只有 1 个（模型没合并）→ 仍该判 GAP
    agg = mod.totals(_rows(tool_calls=100, batches=50, tool_rounds=100))
    assert agg["per_batch"] == 2.0 and agg["per_llm_round"] == 1.0
    verdict, obs = mod.judge("parallel", "batch_ratio", agg, [])
    assert verdict == "MEASURED_GAP"
    assert "往返合并率" in obs


def test_batch_ratio_cost_text_marks_theoretical_upper_bound():
    """那个省钱的百分比是理论上界——必须写明，否则下一轮又去优化已定案的死路。"""
    mod = _load_audit()
    rows = _rows(tool_calls=100, batches=100, tool_rounds=100)
    rows[0].update({"llm_calls": 100, "prompt_tokens": 5_000_000, "mergeable_calls": 3})
    agg = mod.totals(rows)
    _, obs = mod.judge("parallel", "batch_ratio", agg, [])
    assert "理论上界" in obs
    assert "可合并空间只剩 3 次" in obs


def test_low_bad_rate_is_zero_benefit_candidate():
    mod = _load_audit()
    # 3220 次调用里 3 次重读 = 0.09% < 1%
    agg = mod.totals(_rows(tool_calls=3220, batches=3220, re_reads=3))
    verdict, obs = mod.judge("metric", "re_reads", agg, [])
    assert verdict == "MEASURED_OK"
    assert "0.09%" in obs


def test_high_truncation_rate_is_gap():
    mod = _load_audit()
    agg = mod.totals(_rows(tool_calls=1000, batches=1000, truncations=58))
    verdict, _ = mod.judge("metric", "truncations", agg, [])
    assert verdict == "MEASURED_GAP"


# ---------- 契约 4：无埋点必须如实标注 ----------

def test_rules_without_instrumentation_are_unmeasured():
    """摸清大项目/说重点这类规则没有任何日志能反映，不许猜成 OK 或 GAP。

    删除/画图/音乐三条已在 2026-09-14 补了计数埋点，但零暴露样本下不是
    UNMEASURED（数据源不缺）而是 INSUFFICIENT（缺样本）——见暴露量用例。
    """
    mod = _load_audit()
    agg = mod.totals([])
    for label in ("删除类任务", "画图/图片", "音乐/听歌", "摸清大项目", "说重点"):
        assert any(m[0] == label for m in mod.RULE_MAP), f"映射表缺 {label}"
    verdict, _ = mod.judge("none", None, agg, [])
    assert verdict == "UNMEASURED"


def test_delegate_rules_report_counts_but_stay_unmeasured():
    """委派类只有条数与任务长度，判不了「本该自己做」——弱证据必须标 UNMEASURED。"""
    mod = _load_audit()
    subs = [{"task": "x" * 100}, {"task": "y" * 300}]
    verdict, obs = mod.judge("delegate", "subagent", mod.totals([]), subs)
    assert verdict == "UNMEASURED"
    assert "2 条" in obs and "200 字符" in obs


# ---------- 契约 5/6：缺数据不许崩 ----------

def test_missing_metrics_file_is_empty_not_crash(tmp_path, monkeypatch):
    mod = _load_audit()
    monkeypatch.setattr(mod, "METRICS", tmp_path / "nope.jsonl")
    monkeypatch.setattr(mod, "SUBAGENTS", tmp_path / "nope.jsonl")
    rep = mod.build()
    assert rep["agg"]["turns"] == 0
    assert rep["agg"]["tool_calls"] == 0
    assert rep["trend"] == []


def test_render_without_sys_chars_does_not_crash(tmp_path, monkeypatch):
    """没有 seg.sys 时占比未知，报告要能表达，不许 ZeroDivision/格式化崩。"""
    mod = _load_audit()
    monkeypatch.setattr(mod, "METRICS", _write_metrics(
        tmp_path, [{"ts": 1_789_000_000.0, "tool_calls": 1, "batches": 1,
                    "tool_rounds": 1, "re_reads": 0, "truncations": 0, "seg": {}}]))
    monkeypatch.setattr(mod, "SUBAGENTS", tmp_path / "none.jsonl")
    rep = mod.build()
    assert rep["rules_share_of_sys"] is None
    out = mod.render(rep)
    assert "占比未知" in out


def test_cost_share_uses_seg_sys(tmp_path, monkeypatch):
    """成本占比 = 规则区字符 / 系统提示词字符，取自 seg.sys。"""
    mod = _load_audit()
    monkeypatch.setattr(mod, "METRICS", _write_metrics(
        tmp_path, _rows(tool_calls=10, batches=5, sys_chars=4000)))
    monkeypatch.setattr(mod, "SUBAGENTS", tmp_path / "none.jsonl")
    rep = mod.build()
    expect = round(rep["rules_total_chars"] / 4000, 4)
    assert rep["rules_share_of_sys"] == expect
    assert rep["sys_chars"] == 4000


# ---------- 契约 7：行为暴露量（0 次 ≠ 规则没用） ----------

def test_op_rules_are_instrumented_not_unmeasured_by_design():
    """删除/画图/音乐三条已接上计数埋点，不再是「设计上就无埋点」。"""
    mod = _load_audit()
    kinds = {m[0]: (m[1], m[2]) for m in mod.RULE_MAP}
    assert kinds["删除类任务"] == ("metric", "delete_ops")
    assert kinds["画图/图片"] == ("metric", "img_gen_calls")
    assert kinds["音乐/听歌"] == ("metric", "music_calls")


@pytest.mark.parametrize("metric", ["img_gen_calls", "music_calls", "delete_ops"])
def test_op_rules_with_zero_exposure_are_insufficient_not_unmeasured(metric):
    """埋点就位但行为 0 次：不许猜，也不许报成「无埋点」——缺的是样本不是数据源。"""
    mod = _load_audit()
    agg = mod.totals(_rows(tool_calls=100, batches=100))
    verdict, obs = mod.judge("metric", metric, agg, [])
    assert verdict == "INSUFFICIENT"
    assert "0 次" in obs


def test_op_rules_with_traffic_are_used_not_deletable():
    """行为确实在发生 → 规则有实际作用面，绝不能进「零收益候选」。"""
    mod = _load_audit()
    rows = _rows(tool_calls=100, batches=100)
    rows[0]["music_calls"] = 7
    agg = mod.totals(rows)
    verdict, obs = mod.judge("metric", "music_calls", agg, [])
    assert verdict == "MEASURED_USED"
    assert "7 次" in obs


def test_render_handles_every_verdict():
    """render 的标签表必须覆盖全部判定——漏一个就是 KeyError 崩报告。"""
    mod = _load_audit()
    for v in ("MEASURED_OK", "MEASURED_GAP", "MEASURED_USED",
              "INSUFFICIENT", "UNMEASURED"):
        rep = {"map_drift": False, "rules_count": 1, "rules_total_chars": 10,
               "sys_chars": 100, "rules_share_of_sys": 0.1, "agg": mod.totals([]),
               "trend": [],
               "entries": [{"label": "某规则", "chars": 10, "share_of_rules": 1.0,
                            "verdict": v, "observed": "观测"}]}
        assert "某规则" in mod.render(rep), v


def test_real_metrics_carry_op_keys_or_report_zero(tmp_path, monkeypatch):
    """真实 turn_metrics 里这三个键可能还没出现（旧行）——必须当 0 处理，不许崩。"""
    mod = _load_audit()
    monkeypatch.setattr(mod, "METRICS", _write_metrics(
        tmp_path, _rows(tool_calls=10, batches=5)))          # 旧行：无三个新键
    monkeypatch.setattr(mod, "SUBAGENTS", tmp_path / "none.jsonl")
    rep = mod.build()
    assert rep["agg"]["img_gen_calls"] == 0
    assert rep["agg"]["music_calls"] == 0
    assert rep["agg"]["delete_ops"] == 0
    assert "埋点已就位" in mod.render(rep)      # 零暴露 → 如实报「未被触发」


# ---------- 契约 8：委派指纹（定时重复 vs 原样重发） ----------

def _sub(aid: str, task: str, ts_ms: int, event: str = "spawn") -> dict:
    return {"id": aid, "task": task, "created_at": ts_ms, "event": event}


def test_delegation_hourly_repeats_count_as_sched():
    """整小时间隔的重复是定时调度，不是「原样重发」。"""
    mod = _load_audit()
    base = 1_789_300_000_000
    subs = [_sub("a", "T", base), _sub("b", "T", base + 3_600_000),
            _sub("c", "T", base + 7_200_000)]
    d = mod.delegation_stats(subs)
    assert (d["records"], d["unique_tasks"]) == (3, 1)
    assert d["sched_repeats"] == 2
    assert d["suspect_repeats"] == 0


def test_delegation_short_gap_counts_as_suspect():
    """5 分钟内的同任务再委派 = 疑似原样重发，规则没拦住。"""
    mod = _load_audit()
    base = 1_789_300_000_000
    d = mod.delegation_stats([_sub("a", "T", base), _sub("b", "T", base + 300_000)])
    assert d["suspect_repeats"] == 1
    assert d["sched_repeats"] == 0


def test_delegation_near_hour_gap_still_sched():
    """59 分钟（离整点差 60s）仍算定时调度——定时器有抖动。"""
    mod = _load_audit()
    base = 1_789_300_000_000
    d = mod.delegation_stats([_sub("a", "T", base), _sub("b", "T", base + 3_540_000)])
    assert d["sched_repeats"] == 1


def test_delegation_state_events_do_not_inflate_counts():
    """同一 id 的 spawn/status/done 三行是状态事件，不是三次委派。"""
    mod = _load_audit()
    base = 1_789_300_000_000
    subs = [_sub("a", "T", base), _sub("a", "T", base, "status"), _sub("a", "T", base, "done")]
    d = mod.delegation_stats(subs)
    assert d["records"] == 3
    assert d["unique_tasks"] == 1
    assert d["sched_repeats"] == d["suspect_repeats"] == 0


def test_dup_rule_suspect_repeat_is_a_gap():
    """真有重发 → MEASURED_GAP（问题仍在），不许算零收益。"""
    mod = _load_audit()
    base = 1_789_300_000_000
    subs = [_sub("a", "T", base), _sub("b", "T", base + 120_000)]
    agg = mod.totals(_rows(tool_calls=10, batches=5))
    verdict, obs = mod.judge("delegate", "subagent", agg, subs, "防重复委派")
    assert verdict == "MEASURED_GAP"
    assert "疑似原样重发 1 次" in obs


def test_dup_rule_small_sample_is_insufficient():
    """零重复但样本太小（唯一任务 < 8）→ 不许判零收益；且缺的是样本不是数据源。"""
    mod = _load_audit()
    base = 1_789_300_000_000
    subs = [_sub("a", "T", base), _sub("b", "U", base + 60_000)]
    agg = mod.totals(_rows(tool_calls=10, batches=5))
    verdict, obs = mod.judge("delegate", "subagent", agg, subs, "防重复委派")
    assert verdict == "INSUFFICIENT"
    assert "样本不足" in obs


def test_dup_rule_enough_sample_zero_repeat_is_ok():
    """样本够了且零重复 → 才是零收益候选。"""
    mod = _load_audit()
    base = 1_789_300_000_000
    subs = [_sub(f"a{i}", f"T{i}", base + i * 60_000) for i in range(9)]
    agg = mod.totals(_rows(tool_calls=10, batches=5))
    verdict, obs = mod.judge("delegate", "subagent", agg, subs, "防重复委派")
    assert verdict == "MEASURED_OK"
    assert "无重发" in obs


def test_other_delegate_rules_keep_old_judgement():
    """另两条委派规则没有指纹可判，仍标 UNMEASURED——别被新判据顺带改判。"""
    mod = _load_audit()
    agg = mod.totals(_rows(tool_calls=10, batches=5))
    for label in ("委派任务用 delegate_agent_task", "简单任务直接做"):
        verdict, _ = mod.judge("delegate", "subagent", agg, [_sub("a", "T", 1)], label)
        assert verdict == "UNMEASURED", label


# ---------- 契约 9：样本不足 ≠ 缺数据源（决定下一轮该补埋点还是等样本） ----------

def test_insufficient_is_distinct_from_unmeasured():
    """拆类的意义全在行动指引：INSUFFICIENT 补埋点没用，UNMEASURED 才要补埋点。

    混成一类时报告写「无埋点 N 条」，读者会去补 N 个埋点——实测 11 条里只有
    4 条真缺数据源，另 3 条（画图/音乐/防重复委派）埋点早就就位。这个用例锁
    死两类的分界：数据源能不能回答这个问题，而不是样本大不大。
    """
    mod = _load_audit()
    empty = mod.totals([])
    # 有数据源、能回答这个问题、只是样本不够 → INSUFFICIENT
    assert mod.judge("metric", "music_calls", empty, [])[0] == "INSUFFICIENT"
    assert mod.judge("delegate", "subagent", empty,
                     [{"task": "T", "created_at": 1_000}], "防重复委派")[0] == "INSUFFICIENT"
    # 数据源有但回答不了这个问题（只有条数与任务长度）→ UNMEASURED，不是样本问题
    assert mod.judge("delegate", "subagent", empty,
                     [{"task": "T", "created_at": 1_000}], "简单任务直接做")[0] == "UNMEASURED"
    # 没有任何数据源能回答这个问题 → UNMEASURED
    assert mod.judge("none", None, empty, [])[0] == "UNMEASURED"


def test_render_separates_the_two_undecidable_buckets():
    """报告必须把两类分开列，且各自写清下一步动作——混着列等于没拆。"""
    mod = _load_audit()
    rep = {"map_drift": False, "rules_count": 2, "rules_total_chars": 20,
           "sys_chars": 100, "rules_share_of_sys": 0.2, "agg": mod.totals([]),
           "trend": [],
           "entries": [
               {"label": "画图", "chars": 10, "share_of_rules": 0.5,
                "verdict": "INSUFFICIENT", "observed": "埋点已就位，0 次"},
               {"label": "说重点", "chars": 10, "share_of_rules": 0.5,
                "verdict": "UNMEASURED", "observed": "无任何日志"},
           ]}
    out = mod.render(rep)
    assert "样本不足  （1）：画图" in out
    assert "无数据源  （1）：说重点" in out
    assert "补埋点没用" in out
    assert "要补的是埋点" in out
