#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""任务级样本：把样本单位从「轮」换成「任务」。

背景（2026-09-14）：轮级结局标签的产出率实测只有 6%——终态任务稀疏地落在几十轮
流水里，28 个槽位要几千轮才够，等于永远等不到。任务才是天然样本：一个任务一条。

本文件锁死四条契约：

  1. 归属提交轮：取第一行 ts >= created 的流水（轮末落盘，ts 必然晚于轮内提交）
  2. 滞后设上限：流水断档时硬归会接错轮次，判归不上并计数，不静默接错
  3. 只有终态任务是样本：queued/running 还没有结局，不是因变量
  4. 自变量是提交轮的注入集合：子任务提交后独立运行，执行期注入的基因管不到它
"""
import importlib.util
import json
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))


def _load_gf(tmp_path):
    """加载 tools/gene_fitness.py，把审计/埋点/流水都指到 tmp，不碰真实台账。"""
    spec = importlib.util.spec_from_file_location("gf_tasks_under_test", BASE / "tools" / "gene_fitness.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.STATS = tmp_path / "stats.json"
    mod.EXPOSURE = tmp_path / "gene_exposure.jsonl"
    mod.SUBAGENT_HIST = tmp_path / "sub_agents.jsonl"
    return mod


T = 1_700_000_000.0  # 真实量级：秒 ~1.7e9 / 毫秒 ~1.7e12，1e11 是分界


def _write_hist(gf, rows):
    gf.SUBAGENT_HIST.write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")


def _task(i, created, status=None, end=None):
    """一个任务的 queued 行（+ 可选终态行）。字段与 sub_agents.py 的审计行一致。"""
    rows = [{"id": i, "status": "queued", "created_at": int(created * 1000)}]
    if status:
        rows.append({"id": i, "status": status, "created_at": int(created * 1000),
                     "updated_at": int(end * 1000)})
    return rows


def _exp(ts, keys):
    return {"ts": ts, "turn": f"t{int(ts)}", "keys": list(keys)}


def test_sample_attaches_to_submit_round(tmp_path):
    gf = _load_gf(tmp_path)
    _write_hist(gf, _task("a", T + 10, "done", T + 200))
    exp = [_exp(T + 60, ["lesson:x"]), _exp(T + 300, ["lesson:y"])]
    samples, drops = gf.task_samples(exp)
    assert len(samples) == 1 and not drops
    assert samples[0]["keys"] == ["lesson:x"], "归属提交轮，不是完成轮"
    assert samples[0]["status"] == "done"


def test_first_row_at_or_after_submit_wins(tmp_path):
    gf = _load_gf(tmp_path)
    _write_hist(gf, _task("a", T + 100, "done", T + 900))
    exp = [_exp(T + 10, ["k1"]), _exp(T + 60, ["k2"]), _exp(T + 120, ["k3"])]
    samples, _ = gf.task_samples(exp)
    assert samples[0]["keys"] == ["k3"], "T+60 早于提交时刻，必须跳过"


def test_lag_beyond_limit_is_dropped_not_misattached(tmp_path):
    gf = _load_gf(tmp_path)
    _write_hist(gf, _task("a", T + 10, "done", T + 20000))
    samples, drops = gf.task_samples([_exp(T + 4010, ["lesson:x"])])
    assert not samples, "滞后 4000s 的流水不能当作它的提交轮"
    assert sum(drops.values()) == 1


def test_pending_when_no_round_written_yet(tmp_path):
    gf = _load_gf(tmp_path)
    _write_hist(gf, _task("a", T + 9999, "done", T + 10000))
    samples, drops = gf.task_samples([_exp(T + 10, ["lesson:x"])])
    assert not samples
    assert "流水还没写到那一轮" in drops


def test_missing_created_is_counted_not_guessed(tmp_path):
    gf = _load_gf(tmp_path)
    _write_hist(gf, [{"id": "a", "status": "done", "updated_at": int((T + 200) * 1000)}])
    samples, drops = gf.task_samples([_exp(T + 60, ["lesson:x"])])
    assert not samples and "无提交时刻" in drops


def test_non_terminal_is_not_a_sample(tmp_path):
    gf = _load_gf(tmp_path)
    _write_hist(gf, _task("a", T + 10) + _task("b", T + 10, "running", T + 50))
    samples, drops = gf.task_samples([_exp(T + 60, ["lesson:x"])])
    assert not samples and not drops, "没结局就没有因变量，也不该算「归不上」"


def test_last_status_wins_for_same_id(tmp_path):
    gf = _load_gf(tmp_path)
    rows = _task("a", T + 10, "done", T + 200)
    rows.append({"id": "a", "status": "error", "updated_at": int((T + 300) * 1000)})
    _write_hist(gf, rows)
    samples, _ = gf.task_samples([_exp(T + 60, ["lesson:x"])])
    assert samples[0]["status"] == "error", "同一 id 多行只取最后状态"


def test_accepts_seconds_and_milliseconds(tmp_path):
    gf = _load_gf(tmp_path)
    _write_hist(gf, [{"id": "a", "status": "queued", "created_at": T + 10},
                     {"id": "a", "status": "done", "updated_at": T + 200}])
    samples, _ = gf.task_samples([{"ts": T + 60, "keys": ["lesson:x"]}])
    assert len(samples) == 1, "秒级时间戳同样要能归属"


def test_survives_missing_file_and_broken_lines(tmp_path):
    gf = _load_gf(tmp_path)
    assert gf.task_samples([]) == ([], {}), "文件不存在不是故障"
    gf.SUBAGENT_HIST.write_text(
        "{坏行\n" + json.dumps(_task("a", T + 10)[0]) + "\n", encoding="utf-8")
    samples, drops = gf.task_samples([_exp(T + 60, ["lesson:x"])])
    assert not samples and not drops, "只有 queued 行（没有终态）不算样本"


def test_effect_counts_each_task_once_and_gives_baseline(tmp_path):
    gf = _load_gf(tmp_path)
    samples = [
        {"id": "a", "status": "done", "keys": ["k1", "k2"]},
        {"id": "b", "status": "error", "keys": ["k1"]},
    ]
    idx, base = gf.task_effect(samples)
    assert idx["k1"] == {"tasks": 2, "outcomes": {"done": 1, "error": 1}}
    assert idx["k2"]["tasks"] == 1
    assert base == {"done": 1, "error": 1}, "基线必须来自同一批任务"


def test_report_shows_task_level_samples(tmp_path, capsys):
    gf = _load_gf(tmp_path)
    _write_hist(gf, _task("a", T + 10, "done", T + 200))
    gf.EXPOSURE.write_text(json.dumps(_exp(T + 60, ["lesson:x"])) + "\n", encoding="utf-8")
    gf.report()
    out = capsys.readouterr().out
    assert "任务级样本" in out and "可归属 1 个" in out
    assert "lesson:x" in out, "报表要能看到基因级的任务结局分布"
