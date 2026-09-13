#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""基因曝光流水：一行一轮记下「本轮哪些基因进了 prompt」。

背景（2026-09-14）：gene_stats.json 只存累计曝光次数，够做轮换排序，但反推不出
「这条基因在场的那一轮发生了什么」——没有集合就没有对照，任何 fitness 都算不出来。
本文件锁死流水四条契约：

  1. 一轮一行：一轮内 lessons/longterm/conviction 三块注入合并成一行，不是三行
  2. 追加不覆盖：第二轮写完，第一轮那行逐字节不变
  3. 空轮不写：没注入任何基因的轮次不留空行
  4. 口径一致：流水行的 keys 集合 = gene_fitness 报表的「在场」集合（同一 key_of）
"""
import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

import agent  # noqa: E402


def _load_gf(tmp_path):
    """加载 tools/gene_fitness.py，把埋点与流水都指到 tmp，不碰真实台账。"""
    spec = importlib.util.spec_from_file_location("gf_exposure_under_test", BASE / "tools" / "gene_fitness.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.STATS = tmp_path / "stats.json"
    mod.EXPOSURE = tmp_path / "gene_exposure.jsonl"
    return mod


def _rows(path):
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


@pytest.fixture(autouse=True)
def _clean_round():
    """agent 是进程内单例，缓冲必须在每个用例前后清干净，否则用例间互相污染。"""
    agent._gene_round_reset()
    yield
    agent._gene_round_reset()


def test_log_exposure_appends_one_row(tmp_path):
    gf = _load_gf(tmp_path)
    assert gf.log_exposure(["lesson:aaa", "longterm:bbb"], turn="2026-09-14 10:00:00") == 1
    rows = _rows(gf.EXPOSURE)
    assert len(rows) == 1
    assert rows[0]["keys"] == ["lesson:aaa", "longterm:bbb"]
    assert rows[0]["turn"] == "2026-09-14 10:00:00"
    assert isinstance(rows[0]["ts"], float)


def test_log_exposure_dedupes_and_skips_empty(tmp_path):
    gf = _load_gf(tmp_path)
    assert gf.log_exposure([]) == 0
    assert not gf.EXPOSURE.exists(), "空轮不该留下空行"
    gf.log_exposure(["a", "a", "", "b"])
    assert _rows(gf.EXPOSURE)[0]["keys"] == ["a", "b"]


def test_read_exposure_skips_bad_lines(tmp_path):
    gf = _load_gf(tmp_path)
    gf.EXPOSURE.write_text('{"keys":["a"]}\n不是 json\n{"keys":"oops"}\n{"keys":["b"]}\n', encoding="utf-8")
    assert [r["keys"] for r in gf.read_exposure()] == [["a"], ["b"]]
    assert [r["keys"] for r in gf.read_exposure(limit=1)] == [["b"]]


def test_round_buffer_merges_three_blocks(monkeypatch, tmp_path):
    """一轮内三块注入 → 轮末 flush 一次 → 恰一行，keys 合并去重。"""
    gf = _load_gf(tmp_path)
    monkeypatch.setattr(agent, "_gene_mod", lambda: gf)
    agent._gene_touch([("lesson", "L1"), ("lesson", "L2")])
    agent._gene_touch([("longterm", "P1")])
    agent._gene_touch([("conviction", "C1"), ("lesson", "L1")])
    assert not gf.EXPOSURE.exists(), "注入时只进缓冲，轮末才落盘"
    assert agent._gene_flush_exposure() == 1
    rows = _rows(gf.EXPOSURE)
    assert len(rows) == 1
    assert rows[0]["keys"] == [f"lesson:{gf.key_of('L1')}", f"lesson:{gf.key_of('L2')}",
                               f"longterm:{gf.key_of('P1')}", f"conviction:{gf.key_of('C1')}"]


def test_flush_appends_second_round_without_rewriting_first(monkeypatch, tmp_path):
    gf = _load_gf(tmp_path)
    monkeypatch.setattr(agent, "_gene_mod", lambda: gf)
    agent._gene_touch([("lesson", "L1")])
    agent._gene_flush_exposure()
    first = gf.EXPOSURE.read_text(encoding="utf-8")
    agent._gene_touch([("lesson", "L2")])
    agent._gene_flush_exposure()
    text = gf.EXPOSURE.read_text(encoding="utf-8")
    assert text.startswith(first), "旧行必须逐字节不变（追加不是覆盖）"
    assert len(_rows(gf.EXPOSURE)) == 2


def test_flush_noop_without_injection(monkeypatch, tmp_path):
    gf = _load_gf(tmp_path)
    monkeypatch.setattr(agent, "_gene_mod", lambda: gf)
    assert agent._gene_flush_exposure() == 0
    assert not gf.EXPOSURE.exists()


def test_touch_and_flush_survive_module_missing(monkeypatch):
    """埋点模块加载失败：不抛异常、不留缓冲残渣。"""
    monkeypatch.setattr(agent, "_gene_mod", lambda: None)
    agent._gene_touch([("lesson", "L1")])
    assert agent._gene_flush_exposure() == 0
    assert agent._gene_round_reset() == 0


def test_exposure_keys_match_report_window(monkeypatch, tmp_path):
    """口径一致：流水行的 keys = _gene_pick 的选取 = 报表的「在场」。

    这条锁的是一个实测过的坑：注入端按 +1 之前的计数选取，报表读的是 +1 之后的计数，
    所以「用当前 stats 复现窗口」必然与当时实际注入的那几条对不上（实测 11 条不等）。
    有流水时报表必须以流水为准。
    """
    gf = _load_gf(tmp_path)
    monkeypatch.setattr(agent, "_gene_mod", lambda: gf)
    items = [f"L{i}" for i in range(20)]
    gf.save_stats({gf.key_of(x): {"inject": 1} for x in items[:6]})
    monkeypatch.setattr(gf, "caps", lambda: {"lesson": 6, "conviction": 0, "longterm": 0})
    monkeypatch.setattr(gf, "fresh_default", lambda: 2)
    monkeypatch.setattr(gf, "read_genes",
                        lambda: [{"kind": "lesson", "order": i, "text": t} for i, t in enumerate(items)])
    pick = agent._gene_pick(items, 6)
    _cp, _st, built = gf.build()
    assert [r["text"] for r in built if r["in_window"]] == pick, "无流水时按当前曝光复现"
    assert all(r.get("win_src") == "按当前曝光复现" for r in built)
    agent._gene_touch([("lesson", t) for t in pick])
    agent._gene_flush_exposure()
    keys = set(_rows(gf.EXPOSURE)[0]["keys"])
    assert keys == {f"lesson:{gf.key_of(t)}" for t in pick}
    _cp2, _st2, built2 = gf.build()
    assert {r["text"] for r in built2 if r["in_window"]} == set(pick)
    assert all(r.get("win_src") == "最近一轮流水" for r in built2)


# ---- 因变量：结局标签与在场基因同写一行（2026-09-14 P1）----

def test_log_exposure_carries_metrics(tmp_path):
    gf = _load_gf(tmp_path)
    m = {"tool_rounds": 3, "tool_errors": 1, "duration_ms": 1234}
    assert gf.log_exposure(["a"], turn="t", metrics=m) == 1
    assert _rows(gf.EXPOSURE)[0]["metrics"] == m


def test_log_exposure_omits_empty_metrics(tmp_path):
    gf = _load_gf(tmp_path)
    gf.log_exposure(["a"])
    gf.log_exposure(["b"], metrics={})
    for row in _rows(gf.EXPOSURE):
        assert "metrics" not in row, "没指标就别留空壳字段，空壳会被当成「这一轮结局是空」"


def test_log_exposure_bad_metrics_keeps_row(tmp_path):
    """指标不可序列化：丢指标、保曝光。曝光丢了，这一轮就永远对不上任何结局。"""
    gf = _load_gf(tmp_path)
    assert gf.log_exposure(["a"], metrics={"bad": {1, 2}}) == 1
    row = _rows(gf.EXPOSURE)[0]
    assert row["keys"] == ["a"] and "metrics" not in row


def test_flush_passes_metrics_through(monkeypatch, tmp_path):
    gf = _load_gf(tmp_path)
    monkeypatch.setattr(agent, "_gene_mod", lambda: gf)
    agent._gene_touch([("lesson", "L1")])
    assert agent._gene_flush_exposure({"tool_rounds": 2, "tool_errors": 0}) == 1
    assert _rows(gf.EXPOSURE)[0]["metrics"] == {"tool_rounds": 2, "tool_errors": 0}


def test_flush_metrics_optional(monkeypatch, tmp_path):
    """旧调用点（不传 metrics）行为不变：没有结局标签也不该丢曝光。"""
    gf = _load_gf(tmp_path)
    monkeypatch.setattr(agent, "_gene_mod", lambda: gf)
    agent._gene_touch([("lesson", "L1")])
    assert agent._gene_flush_exposure() == 1
    assert "metrics" not in _rows(gf.EXPOSURE)[0]


def test_record_turn_metrics_flush_carries_outcome():
    """接线守卫：_record_turn_metrics 必须把本轮结局实参传给 flush。

    这条测行为测不到——忘了传参时流水照样每轮写、keys 照样涨，只是 metrics 永远为空，
    看上去「因变量已采集」实则没有。所以直接查调用形态。
    """
    src = (BASE / "agent.py").read_text(encoding="utf-8")
    assert "def _record_turn_metrics():" in src, "agent.py 里找不到 _record_turn_metrics"
    i = src.index("def _record_turn_metrics():")
    m = re.search(r"_gene_flush_exposure\(\s*\{(.*?)\}\s*\)", src[i:i + 3000], re.S)
    assert m, "_record_turn_metrics 里 flush 必须带本轮 metrics 实参（字面 dict）"
    for field in ("tool_rounds", "tool_errors", "tool_calls", "duration_ms"):
        assert field in m.group(1), f"结局字段 {field} 必须进流水，否则因变量算不出来"


# ---- P2：报表读出「在场轮数 / 结局中位」，并自动对账（2026-09-14）----

def _stage_report(gf, monkeypatch, rows, turn_metrics=None):
    """让 report() 只读 tmp：流水、turn_metrics、基因源全部隔离，不碰生产文件。"""
    gf.EXPOSURE.write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    gf.TURN_METRICS = gf.EXPOSURE.parent / "turn_metrics.jsonl"
    if turn_metrics is not None:
        gf.TURN_METRICS.write_text(json.dumps(turn_metrics, ensure_ascii=False) + "\n", encoding="utf-8")
    monkeypatch.setattr(gf, "read_genes", lambda: [])
    monkeypatch.setattr(gf, "caps", lambda: {})
    monkeypatch.setattr(gf, "fresh_default", lambda: 2)


def test_median_handles_empty_odd_even(tmp_path):
    gf = _load_gf(tmp_path)
    assert gf.median([]) is None, "空集不能返回 0——0 会被读成「很健康」"
    assert gf.median([5]) == 5
    assert gf.median([3, 1, 2]) == 2
    assert gf.median([1, 2, 3, 4]) == 2.5


def test_outcome_index_counts_rounds_and_baseline(tmp_path):
    """在场轮数统计所有行；结局样本只取带 metrics 的行——旧行不能拉低样本也不该丢在场。"""
    gf = _load_gf(tmp_path)
    exp = [
        {"ts": 1.0, "keys": ["a", "b"], "metrics": {"tool_rounds": 10, "tool_errors": 0, "duration_ms": 100}},
        {"ts": 2.0, "keys": ["a"], "metrics": {"tool_rounds": 4, "tool_errors": 2, "duration_ms": 300}},
        {"ts": 3.0, "keys": ["a", "b"]},
    ]
    index, baseline = gf.outcome_index(exp)
    assert index["a"]["rounds"] == 3 and len(index["a"]["metrics"]) == 2
    assert index["b"]["rounds"] == 2 and len(index["b"]["metrics"]) == 1
    assert baseline["tool_rounds"] == 7 and baseline["tool_errors"] == 1 and baseline["duration_ms"] == 200


def test_report_flags_untagged_rows(monkeypatch, tmp_path, capsys):
    gf = _load_gf(tmp_path)
    _stage_report(gf, monkeypatch, [{"ts": 1.0, "keys": ["a"]}, {"ts": 2.0, "keys": ["b"]}])
    assert gf.report() == 0
    out = capsys.readouterr().out
    assert "带结局标签 0 轮" in out
    assert "结局列全空" in out


def test_report_prints_outcome_columns(monkeypatch, tmp_path, capsys):
    gf = _load_gf(tmp_path)
    m = {"tool_rounds": 10, "tool_errors": 0, "tool_calls": 17, "duration_ms": 1000}
    _stage_report(gf, monkeypatch, [{"ts": 100.0, "turn": "t1", "keys": ["lesson:aaaa"], "metrics": m}],
                  turn_metrics=dict(m, ts=100.5))
    gf.report()
    out = capsys.readouterr().out
    assert "带结局标签 1 轮" in out
    assert "1 轮在场 · lesson:aaaa" in out
    assert "门槛（GENE_FITNESS.md 第 4 节）" in out
    assert "逐字段一致" in out


def test_report_reports_reconcile_mismatch(monkeypatch, tmp_path, capsys):
    """对账失败要打出具体字段与两侧取值——只说「不一致」等于让人重新 tail 两处。"""
    gf = _load_gf(tmp_path)
    _stage_report(gf, monkeypatch,
                  [{"ts": 100.0, "turn": "t1", "keys": ["a"],
                    "metrics": {"tool_rounds": 10, "tool_errors": 0}}],
                  turn_metrics={"ts": 100.5, "tool_rounds": 3, "tool_errors": 0})
    gf.report()
    assert "tool_rounds 流水 10 / turn_metrics 3" in capsys.readouterr().out


def test_report_skips_reconcile_across_rounds(monkeypatch, tmp_path, capsys):
    """两处末行不是同一轮时不能报不一致——那会把「还没跑过新一轮」误报成埋点坏了。"""
    gf = _load_gf(tmp_path)
    _stage_report(gf, monkeypatch,
                  [{"ts": 100.0, "keys": ["a"], "metrics": {"tool_rounds": 1}}],
                  turn_metrics={"ts": 999.0, "tool_rounds": 1})
    gf.report()
    assert "不是同一轮" in capsys.readouterr().out


def test_record_turn_metrics_computes_duration_once():
    """耗时只能算一次并复用：两处各算一次，流水会差出 flush 写盘那几毫秒。

    实测 2026-09-14 06:38:42 那轮（P1 首次真链路）：流水 duration_ms 225811 /
    turn_metrics 225821，对账直接报不一致。那 10ms 是测量时刻不同，不是数据错——
    但留着它，对账就永远在报假警，真漂移会被淹掉。所以锁成单一来源。
    """
    src = (BASE / "agent.py").read_text(encoding="utf-8")
    i = src.index("def _record_turn_metrics():")
    j = src.find("\n        def ", i + 1)
    body = src[i:j if j != -1 else len(src)]
    assert body.count("time.monotonic() - eff_start") == 1, (
        "耗时必须只算一次再复用；两处各算一次 = 对账永远报「测量时刻不同」的假不一致")


def test_identifiability_empty(tmp_path):
    mod = _load_gf(tmp_path)
    assert mod.identifiability([]) == ([], [], [])


def test_identifiability_all_fixed(tmp_path):
    """每轮都在场 → 全判 fixed：没有缺席轮次就没有对照组，样本再多也估不出效应。"""
    mod = _load_gf(tmp_path)
    exp = [{"keys": ["a", "b"], "metrics": {"tool_rounds": 1}} for _ in range(5)]
    fixed, thin, ok = mod.identifiability(exp)
    assert fixed == [("a", 5, 5), ("b", 5, 5)]
    assert thin == [] and ok == []


def test_identifiability_mixed(tmp_path):
    mod = _load_gf(tmp_path)
    exp = [
        {"keys": ["always", "rot"], "metrics": {"tool_rounds": 1}},
        {"keys": ["always"], "metrics": {"tool_rounds": 2}},
    ]
    fixed, thin, ok = mod.identifiability(exp)
    assert fixed == [("always", 2, 2)]
    assert thin == [("rot", 1, 2)]


def test_identifiability_ignores_untagged(tmp_path):
    """没带结局标签的轮次不算样本：算进去会把「在场率」稀释成假对照。"""
    mod = _load_gf(tmp_path)
    exp = [
        {"keys": ["a"], "metrics": {"tool_rounds": 1}},
        {"keys": ["a", "b"]},
    ]
    fixed, thin, ok = mod.identifiability(exp)
    assert fixed == [("a", 1, 1)]
    assert thin == [] and ok == []


def test_identifiability_min_rounds_gate(tmp_path):
    """门槛是参数，三类互斥：恒在场进 fixed，达标进 ok，其余进 thin。"""
    mod = _load_gf(tmp_path)
    exp = [
        {"keys": ["always", "warm"], "metrics": {"tool_rounds": 1}},
        {"keys": ["always", "warm"], "metrics": {"tool_rounds": 1}},
        {"keys": ["always"], "metrics": {"tool_rounds": 1}},
    ]
    fixed, thin, ok = mod.identifiability(exp, min_rounds=2)
    assert fixed == [("always", 3, 3)]
    assert ok == [("warm", 2, 3)]
    assert thin == []
    # 门槛抬到 3：warm 掉回 thin；三类不重叠（同一键不能同时在 ok 和 thin）
    fixed, thin, ok = mod.identifiability(exp, min_rounds=3)
    assert fixed == [("always", 3, 3)]
    assert ok == []
    assert thin == [("warm", 2, 3)]
