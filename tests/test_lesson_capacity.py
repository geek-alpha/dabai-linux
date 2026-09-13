#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""经验库容量：上限只防无限膨胀，淘汰必须可见、可捞回。

背景（2026-09-14）：tools/lesson_add.py:36 原本 MAX_LESSONS=60 直接 ls[:60]——
满了就无声砍最老。这是拿新近度当价值：注入窗口已由 agent.py:_gene_pick 的选择
压力控制（新近位 + 最少曝光优先），库容量不影响 prompt 质量，砍掉的最老条目
恰恰可能是唯一没被验证过的那批。契约：

  1. 未满时不写归档文件，零副作用
  2. 溢出条目落进 harness_task_memory.archive.json，不是蒸发
  3. 主库长度严格等于上限，新条目在第 0 位（新近序不变）
  4. 重复文本不重复写入（原有行为不能回归）
"""
import importlib.util
import json
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]


def _load(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("lesson_add_under_test", BASE / "tools" / "lesson_add.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "FILE", tmp_path / "mem.json")
    monkeypatch.setattr(mod, "ARCHIVE", tmp_path / "archive.json")
    return mod


def _run(mod, monkeypatch, text):
    monkeypatch.setattr(sys, "argv", ["lesson_add.py", text])
    return mod.main()


def _seed(mod, n):
    mod.FILE.write_text(json.dumps({"lessons": [f"L{i}" for i in range(n)]}, ensure_ascii=False),
                        encoding="utf-8")


def test_no_archive_before_limit(tmp_path, monkeypatch, capsys):
    mod = _load(tmp_path, monkeypatch)
    _seed(mod, 3)
    assert _run(mod, monkeypatch, "新教训") == 0
    assert not mod.ARCHIVE.exists(), "没满就不该产生归档文件"
    assert json.loads(mod.FILE.read_text(encoding="utf-8"))["lessons"][0] == "新教训"


def test_overflow_archives_oldest_and_keeps_cap(tmp_path, monkeypatch, capsys):
    mod = _load(tmp_path, monkeypatch)
    _seed(mod, mod.MAX_LESSONS)
    assert _run(mod, monkeypatch, "第501条") == 0
    ls = json.loads(mod.FILE.read_text(encoding="utf-8"))["lessons"]
    assert len(ls) == mod.MAX_LESSONS, "主库长度必须严格等于上限"
    assert ls[0] == "第501条"
    arch = json.loads(mod.ARCHIVE.read_text(encoding="utf-8"))["lessons"]
    assert arch == [f"L{mod.MAX_LESSONS - 1}"], "被挤出的最老那条必须可捞回"
    assert "归档" in capsys.readouterr().out, "淘汰必须打印出来，不能无声"


def test_archive_accumulates_across_overflows(tmp_path, monkeypatch):
    mod = _load(tmp_path, monkeypatch)
    _seed(mod, mod.MAX_LESSONS)
    _run(mod, monkeypatch, "第501条")
    _run(mod, monkeypatch, "第502条")
    arch = json.loads(mod.ARCHIVE.read_text(encoding="utf-8"))["lessons"]
    assert arch == [f"L{mod.MAX_LESSONS - 1}", f"L{mod.MAX_LESSONS - 2}"]


def test_duplicate_not_rewritten(tmp_path, monkeypatch):
    mod = _load(tmp_path, monkeypatch)
    _seed(mod, 3)
    assert _run(mod, monkeypatch, "L1") == 0
    ls = json.loads(mod.FILE.read_text(encoding="utf-8"))["lessons"]
    assert ls == ["L0", "L1", "L2"], "重复条目不许插到头部"
