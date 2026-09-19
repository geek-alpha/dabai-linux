#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""轮级文件快照的回归契约：改过的文件能不能整轮退回。

背景（2026-09-19）：借鉴 opencode 的 snapshot —— 它的 /undo 把「对话」和
「文件」绑在一起退。大白原来只有两半：turn_checkpoint 只存消息历史（回滚了
文件还在），.bak-<ts> 只保单个文件（回滚了对话还在），多文件重构做错方向时
得手动两头对齐。

契约（每条都是回退点）：
  1. 轮前内容优先：同一文件本轮被改多次，只保留**第一次捕获**的原内容。
     若改成「每次都覆盖快照」，undo 只能退回上一次编辑，退不回轮前——功能就废了。
  2. 原来不存在的文件，undo 要删掉（新建的产物），不是留着空文件。
  3. undo 幂等：还原过的再还原一次，必须报「本来就一致」而不是又写一遍。
  4. preview=true 不落盘 → 不该占快照（否则 undo 报告里一堆「其实没改」）。
  5. 没有轮标识时不捕获（工具可能在轮外被调用，宁可不记，不能记错轮）。
  6. 覆盖不到的写工具（shell_run 这类）必须被记进 blind，并在 undo 报告里
     如实说出来——沉默地漏掉比报错更危险：用户会以为整轮都退回去了。
  7. 任何异常都不许向上抛：快照是附加能力，失败不能让工具执行失败。
"""
import json
import sys
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

from harness import turn_snapshot as ts  # noqa: E402


@pytest.fixture
def env(tmp_path, monkeypatch):
    """把快照目录挪到临时目录，避免污染真实 data/。"""
    monkeypatch.setattr(ts, "SNAP_DIR", tmp_path / "snaps")
    ts.set_turn("t1")
    yield tmp_path
    ts.set_turn("")


def test_edit_then_undo_restores(env):
    f = env / "a.py"
    f.write_text("old\n", encoding="utf-8")
    ts.capture("code_edit", {"file": str(f)})
    f.write_text("new\n", encoding="utf-8")
    rep = ts.undo("t1")
    assert f.read_text(encoding="utf-8") == "old\n"
    assert "1 个改回" in rep


def test_create_then_undo_deletes(env):
    f = env / "b.py"
    ts.capture("code_create_file", {"path": str(f)})
    f.write_text("x\n", encoding="utf-8")
    ts.undo("t1")
    assert not f.exists()


def test_first_capture_wins(env):
    """契约 1：同一文件改两次，undo 要回到轮前 v0，不是 v1。"""
    f = env / "c.py"
    f.write_text("v0\n", encoding="utf-8")
    ts.capture("code_edit", {"file": str(f)})
    f.write_text("v1\n", encoding="utf-8")
    ts.capture("code_edit", {"file": str(f)})
    f.write_text("v2\n", encoding="utf-8")
    ts.undo("t1")
    assert f.read_text(encoding="utf-8") == "v0\n"


def test_undo_idempotent(env):
    """契约 3。"""
    f = env / "d.py"
    f.write_text("a\n", encoding="utf-8")
    ts.capture("code_edit", {"file": str(f)})
    f.write_text("b\n", encoding="utf-8")
    ts.undo("t1")
    rep2 = ts.undo("t1")
    assert "0 个改回" in rep2
    assert "1 个本来就一致" in rep2


def test_preview_not_captured(env):
    """契约 4。"""
    f = env / "e.py"
    f.write_text("a\n", encoding="utf-8")
    assert ts.capture("code_edit", {"file": str(f), "preview": True}) == []
    assert ts.list_turns() == []


def test_no_turn_no_capture(env):
    """契约 5。"""
    ts.set_turn("")
    f = env / "f.py"
    f.write_text("a\n", encoding="utf-8")
    assert ts.capture("code_edit", {"file": str(f)}) == []


def test_patch_paths(env):
    f = env / "g.py"
    f.write_text("a\n", encoding="utf-8")
    patch = f"--- a/{f.name}\n+++ b/{f.name}\n@@ -1 +1 @@\n-a\n+b\n"
    ts.capture("code_patch", {"patch": patch, "root": str(env)})
    f.write_text("b\n", encoding="utf-8")
    ts.undo("t1")
    assert f.read_text(encoding="utf-8") == "a\n"


def test_blind_tool_recorded(env):
    """契约 6：覆盖不到的写工具要留痕并出现在报告里。"""
    ts.capture("shell_run", {"command": "rm -rf something"})
    man = json.loads((ts.SNAP_DIR / "t1" / "manifest.json").read_text(encoding="utf-8"))
    assert "shell_run" in man["blind"]
    # 造一个真文件让报告能生成
    f = env / "h.py"
    f.write_text("a\n", encoding="utf-8")
    ts.capture("code_edit", {"file": str(f)})
    f.write_text("b\n", encoding="utf-8")
    rep = ts.undo("t1")
    assert "快照覆盖不到" in rep and "shell_run" in rep


def test_readonly_tool_not_blind(env):
    """只读工具不该进 blind（否则报告天天喊狼来了）。"""
    ts.capture("code_read", {"file": "x.py"})
    assert ts.list_turns() == []


def test_non_writing_tools_not_blind(env):
    """code_verify（跑测试）和 code_undo_turn（自己就是还原工具）都不该被点名。"""
    ts.capture("code_verify", {"files": "a.py"})
    ts.capture("code_undo_turn", {})
    ts.capture("image_gen_create", {"prompt": "x"})
    assert ts.list_turns() == []


def test_big_file_skipped(env, monkeypatch):
    monkeypatch.setattr(ts, "MAX_FILE_BYTES", 4)
    f = env / "i.py"
    f.write_text("0123456789", encoding="utf-8")
    ts.capture("code_edit", {"file": str(f)})
    f.write_text("small", encoding="utf-8")
    rep = ts.undo("t1")
    assert "未纳入快照" in rep
    assert f.read_text(encoding="utf-8") == "small"


def test_partial_undo(env):
    """一轮里改对了 1 个、改错了 1 个：只退错的那个。"""
    a, b = env / "p1.py", env / "p2.py"
    a.write_text("a0\n", encoding="utf-8")
    b.write_text("b0\n", encoding="utf-8")
    ts.capture("code_edit", {"files": f"{a},{b}"})
    a.write_text("a1\n", encoding="utf-8")
    b.write_text("b1\n", encoding="utf-8")
    ts.undo("t1", paths=[str(a)])
    assert a.read_text(encoding="utf-8") == "a0\n"
    assert b.read_text(encoding="utf-8") == "b1\n"


def test_unknown_turn_returns_hint(env):
    """指名的轮没快照 → 说清是这一轮没改文件；不指名且一个快照都没有 → 提示无快照。"""
    assert "没有文件快照" in ts.undo("nope")
    assert "没有可还原" in ts.undo("")


def test_prune_keeps_newest(env, monkeypatch):
    import os
    import time

    for i in range(5):
        d = ts.SNAP_DIR / f"t{i}"
        d.mkdir(parents=True, exist_ok=True)
        (d / "manifest.json").write_text("{}", encoding="utf-8")
        os.utime(d, (time.time() + i, time.time() + i))
    assert ts.prune(keep=2) == 3
    assert len(ts.list_turns(limit=99)) == 2


def test_capture_never_raises(env, monkeypatch):
    """契约 7：内部炸了也必须返回空列表，不能把异常抛给工具执行。"""
    def boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(ts, "_paths_for", boom)
    assert ts.capture("code_edit", {"file": "x.py"}) == []
