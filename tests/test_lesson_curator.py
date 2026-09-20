#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""经验生命周期：只归档不删除，无证据不算陈旧。

背景（2026-09-20）：库只有 FIFO 500 上限，364 条全是 active。对照
hermes-agent/agent/curator.py:188-240 的 apply_automatic_transitions。

契约：
  1. 锚点取 max(写入时刻, 最后一次进 prompt 的时刻)——只看创建时间会把「一直
     在被用但写得很早」的条目误判成陈旧
  2. 从未曝光且刚写入的条目不动（use_count == 0 是没有证据，不是陈旧）
  3. 超过 archive 线 → 移出主库进 archive.json，条目和它的时间戳一起搬，可捞回
  4. 超过 stale 线 → 只标记，仍在库、仍可被注入
  5. dry-run 不改任何文件；--apply 才写
  6. 陈旧条目重新进 prompt → 回 active
"""
import importlib.util
import json
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
D = 86400.0
NOW = 1_000_000_000.0


def _load(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("lesson_curator_under_test",
                                                  BASE / "tools" / "lesson_curator.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "FILE", tmp_path / "mem.json")
    monkeypatch.setattr(mod, "ARCHIVE", tmp_path / "archive.json")
    monkeypatch.setattr(mod, "EXPOSURE", tmp_path / "exposure.jsonl")
    monkeypatch.setattr(mod, "STATE", tmp_path / "state.json")
    return mod


def _seed(mod, items):
    """items: [(text, 写入时刻|None, 最后命中时刻|None)]"""
    lessons, ts, lines = [], {}, []
    for text, written, hit in items:
        k = mod.key_of(text)
        lessons.append(text)
        if written is not None:
            ts[k] = written
        if hit is not None:
            lines.append(json.dumps({"ts": hit, "keys": [f"lesson:{k}"]}, ensure_ascii=False))
    mod.FILE.write_text(json.dumps({"lessons": lessons, "ts": ts}, ensure_ascii=False), encoding="utf-8")
    mod.EXPOSURE.parent.mkdir(parents=True, exist_ok=True)
    mod.EXPOSURE.write_text("\n".join(lines), encoding="utf-8")


def _acts(rows):
    return {t: act for _k, t, act, _a in rows}


def test_recent_write_is_active(tmp_path, monkeypatch):
    mod = _load(tmp_path, monkeypatch)
    _seed(mod, [("刚写的教训", NOW - 1 * D, None)])
    rows, counts = mod.plan(now=NOW)
    # active 且本来就是 active 的条目不入动作表（没动作可做），所以看计数而不是看行
    assert counts == {"active": 1, "stale": 0, "archived": 0, "seeded": 0} and rows == []


def test_old_and_unhit_is_archived(tmp_path, monkeypatch):
    mod = _load(tmp_path, monkeypatch)
    _seed(mod, [("很久没人用的教训", NOW - 200 * D, NOW - 100 * D)])
    rows, counts = mod.plan(now=NOW)
    assert _acts(rows)["很久没人用的教训"] == "archived" and counts["archived"] == 1


def test_recent_hit_beats_old_write(tmp_path, monkeypatch):
    """写了很久但一直在用：锚点是最后一次命中，不许按创建时间归档。"""
    mod = _load(tmp_path, monkeypatch)
    _seed(mod, [("老条目但还在用", NOW - 300 * D, NOW - 2 * D)])
    rows, counts = mod.plan(now=NOW)
    assert counts["active"] == 1 and counts["archived"] == 0, rows


def test_mid_age_is_stale(tmp_path, monkeypatch):
    mod = _load(tmp_path, monkeypatch)
    _seed(mod, [("一个月没用了", NOW - 300 * D, NOW - 40 * D)])
    rows, counts = mod.plan(now=NOW)
    assert _acts(rows)["一个月没用了"] == "stale" and counts["stale"] == 1


def test_no_timestamp_is_seeded_not_archived(tmp_path, monkeypatch):
    mod = _load(tmp_path, monkeypatch)
    _seed(mod, [("存量无时间戳", None, None)])
    rows, counts = mod.plan(now=NOW)
    assert _acts(rows)["存量无时间戳"] == "seed" and counts["archived"] == 0


def test_dry_run_touches_nothing(tmp_path, monkeypatch):
    mod = _load(tmp_path, monkeypatch)
    _seed(mod, [("该归档的", NOW - 200 * D, NOW - 100 * D)])
    before = mod.FILE.read_text(encoding="utf-8")
    mod.plan(now=NOW)
    assert mod.FILE.read_text(encoding="utf-8") == before
    assert not mod.ARCHIVE.exists() and not mod.STATE.exists()


def test_apply_moves_entry_and_its_timestamp(tmp_path, monkeypatch):
    mod = _load(tmp_path, monkeypatch)
    _seed(mod, [("留着的", NOW - 1 * D, None), ("该走的", NOW - 200 * D, NOW - 100 * D)])
    rows, _c = mod.plan(now=NOW)
    mod.apply(rows, now=NOW)
    main = json.loads(mod.FILE.read_text(encoding="utf-8"))
    arch = json.loads(mod.ARCHIVE.read_text(encoding="utf-8"))
    assert main["lessons"] == ["留着的"]
    assert arch["lessons"] == ["该走的"], "归档必须可捞回"
    assert mod.key_of("该走的") in arch["ts"], "时间戳要跟着走，捞回来才有出生证明"
    assert mod.key_of("该走的") not in main["ts"], "主库不许留孤儿时间戳"
    assert set(main["ts"]) == {mod.key_of("留着的")}


def test_stale_then_hit_reactivates(tmp_path, monkeypatch):
    mod = _load(tmp_path, monkeypatch)
    _seed(mod, [("一度陈旧", NOW - 300 * D, NOW - 40 * D)])
    rows, _c = mod.plan(now=NOW)
    mod.apply(rows, now=NOW)
    assert json.loads(mod.STATE.read_text(encoding="utf-8"))[mod.key_of("一度陈旧")]["state"] == "stale"

    _seed(mod, [("一度陈旧", NOW - 300 * D, NOW - 1 * D)])
    rows, _c = mod.plan(now=NOW)
    assert _acts(rows)["一度陈旧"] == "active", "重新进过 prompt 就该回 active"
    mod.apply(rows, now=NOW)
    assert mod.key_of("一度陈旧") not in json.loads(mod.STATE.read_text(encoding="utf-8"))
