"""insert 模式「new 里抄了锚点行」的防呆回归护栏。

踩坑形态（真实发生过三次）：改文件时把锚点行连同新增内容一起写进 new，
结果文件里多出一份锚点内容。重复的 def 在 Python 里是**合法语法**（后定义覆盖前定义），
py_compile 与测试都拦不住，只有肉眼 review 才能发现——所以这道闸门必须在工具层。

口径：扫 new 的**全部行**。最初只比「紧邻锚点的那一端」，实测两头都漏——
before 抄首行时重复的两行中间隔着整个 new（第 2 条用例就是这么打回来的）。
短锚点（strip 后 < 6 字符，如 }、else:）豁免：它在 new 里出现是常态，比不出问题。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

_spec = importlib.util.spec_from_file_location(
    "co_dup_t", ROOT / "skills" / "code_ops" / "code_ops_impl.py")
co = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(co)

FP = Path("x.py")
NORM = "def foo():\n    return 1\n\n\ndef bar():\n    return 2\n"

DUP_HEAD = "def bar():\n    return 3"   # 锚点行抄在首行
DUP_TAIL = "    return 3\ndef bar():"   # 锚点行抄在末行


def _run(norm=NORM, **kw):
    return co._edit_text_once(norm, co._norm_edit(kw), FP, "x.py")


def _blocked(**kw):
    out, notes = _run(**kw)
    assert out is None, f"本该拦下，却产出了：{out!r}"
    assert "与锚点行相同" in notes[0], notes
    return notes[0]


def test_after_head_dup_blocked():
    """after + 抄在首行：紧邻锚点，相邻重复。"""
    assert "第 1 行" in _blocked(mode="insert", old="def bar():",
                                new=DUP_HEAD, position="after")


def test_before_head_dup_blocked():
    """before + 抄在首行：重复的两行中间隔着整个 new——只比紧邻端时这条漏网。"""
    _blocked(mode="insert", old="def bar():", new=DUP_HEAD, position="before")


def test_before_tail_dup_blocked():
    """before + 抄在末行：紧邻锚点，相邻重复。"""
    _blocked(mode="insert", old="def bar():", new=DUP_TAIL, position="before")


def test_after_tail_dup_blocked():
    """after + 抄在末行：隔着整个 new 的重复，同样拦。"""
    _blocked(mode="insert", old="def bar():", new=DUP_TAIL, position="after")


def test_normal_insert_not_blocked():
    """正常插入（new 只给新增行）照旧通过，且锚点行不增殖。"""
    out, _ = _run(mode="insert", old="def bar():",
                  new="    return 3", position="after")
    assert out is not None
    assert out.count("def bar():") == 1
    assert "    return 3" in out


def test_short_anchor_exempt():
    """短锚点豁免：else: 这类行在 new 里出现是常态，宁可放过也不误拦。"""
    norm = "if x:\n    pass\nelse:\n    pass\n"
    out, _ = _run(norm=norm, mode="insert", old="else:",
                  new="else:\n    x = 1", position="after")
    assert out is not None


def test_threshold_boundary_six_chars_blocked():
    """边界：锚点 strip 后恰好 6 字符（return）不豁免，仍要拦。"""
    _blocked(norm="def f():\n    return\n", mode="insert", old="    return",
             new="    return\n    x = 1", position="after")


def test_line_number_after_dup_blocked():
    """行号模式 after：ins 落在 anchor_line 之前，照样扫得到。"""
    _blocked(mode="insert", line_start=1, line_end=1, position="after",
             new="    x = 2\n    return 1")


def test_line_number_before_dup_blocked():
    """行号模式 before：同上。"""
    _blocked(mode="insert", line_start=2, line_end=2, position="before",
             new="    x = 2\n    return 1")


def test_end_to_end_preview_blocks_and_leaves_file_untouched(tmp_path):
    """端到端：走真实 code_edit 入口，被拦时输出警告且文件一字未动。"""
    p = tmp_path / "demo.py"
    p.write_text("def foo():\n    return 1\n", encoding="utf-8")
    out = co.code_edit({"file": str(p), "root": str(tmp_path), "mode": "insert",
                        "old": "def foo():", "new": "def foo():\n    return 99",
                        "position": "after", "preview": True})
    assert "未做任何修改" in out
    assert p.read_text(encoding="utf-8") == "def foo():\n    return 1\n"
