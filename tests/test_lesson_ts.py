#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""经验条目写入时间戳：没有它，「教训学会前后」这类对照无从定位。

背景（2026-09-14）：harness_task_memory.json 的 lessons 是纯字符串列表，条目不带
时间；该文件又在 .gitignore:13（运行时数据，有意不提交），git 历史也拿不到写入时刻。
后果是任何「这条教训写入之后，同类错误还犯过没有」的度量都缺时间分界——数据在手
却切不出前后两段。补法取零破坏路径：lessons 列表格式原样不动（agent.py:2046、
tools/gene_fitness.py:96、tools/status.py:61 三处读取端一行都不用改），时间存进平行的
ts 表，键与 gene_fitness.key_of 同构，直接对齐曝光流水的 "lesson:<key>"。

契约：
  1. 写入后主文件出现 ts，键 = sha1(text)[:12]，值落在写入时刻附近
  2. lessons 元素仍是 str（读取端零改动，这是本方案的全部理由）
  3. 存量条目没有 ts 也不许崩——缺键的语义是「未知」，不是 0
  4. 溢出时 ts 跟着条目进归档、从主库消失（否则同文再写会继承假时间）
  5. 重复文本不重复计时
"""
import importlib.util
import json
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]


def _load(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("lesson_add_ts_under_test", BASE / "tools" / "lesson_add.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "FILE", tmp_path / "mem.json")
    monkeypatch.setattr(mod, "ARCHIVE", tmp_path / "archive.json")
    return mod


def _run(mod, monkeypatch, text):
    monkeypatch.setattr(sys, "argv", ["lesson_add.py", text])
    return mod.main()


def _seed(mod, texts):
    mod.FILE.write_text(json.dumps({"lessons": list(texts)}, ensure_ascii=False), encoding="utf-8")


def _read(mod):
    return json.loads(mod.FILE.read_text(encoding="utf-8"))


def test_ts_written_and_key_matches_gene_fitness(tmp_path, monkeypatch):
    mod = _load(tmp_path, monkeypatch)
    _seed(mod, [])
    t0 = time.time()
    assert _run(mod, monkeypatch, "新教训") == 0
    data = _read(mod)
    assert isinstance(data["lessons"][0], str), "lessons 元素必须仍是 str，读取端才能零改动"
    key = mod._key("新教训")
    assert len(key) == 12
    assert key in data["ts"], "写入必须带时间戳"
    assert t0 - 5 <= data["ts"][key] <= time.time() + 5, "时间戳必须是写入时刻，不是占位值"


def test_legacy_file_without_ts_is_fine(tmp_path, monkeypatch):
    mod = _load(tmp_path, monkeypatch)
    _seed(mod, ["存量一", "存量二"])
    assert _run(mod, monkeypatch, "新教训") == 0
    data = _read(mod)
    assert data["lessons"] == ["新教训", "存量一", "存量二"]
    assert set(data["ts"]) == {mod._key("新教训")}, "存量缺时间就是缺，不许拿现在的时间冒充"


def test_ts_follows_overflow_into_archive(tmp_path, monkeypatch):
    mod = _load(tmp_path, monkeypatch)
    monkeypatch.setattr(mod, "MAX_LESSONS", 3)
    _seed(mod, [])
    for t in ("A", "B", "C", "D"):
        _run(mod, monkeypatch, t)
    main = _read(mod)
    arch = json.loads(mod.ARCHIVE.read_text(encoding="utf-8"))
    assert main["lessons"] == ["D", "C", "B"]
    assert mod._key("A") not in main["ts"], "被挤出的条目不许在主库留下孤儿时间戳"
    assert mod._key("A") in arch["ts"], "归档条目要带上自己的时间，否则捞回来就没了出生证明"
    assert set(main["ts"]) == {mod._key(x) for x in main["lessons"]}


def test_duplicate_does_not_reset_time(tmp_path, monkeypatch):
    mod = _load(tmp_path, monkeypatch)
    _seed(mod, [])
    _run(mod, monkeypatch, "A")
    first = _read(mod)["ts"][mod._key("A")]
    time.sleep(0.01)
    _run(mod, monkeypatch, "A")
    data = _read(mod)
    assert data["ts"][mod._key("A")] == first, "重复写入不许刷新时间，否则「什么时候学会的」会被反复改写"
    assert data["lessons"] == ["A"]
