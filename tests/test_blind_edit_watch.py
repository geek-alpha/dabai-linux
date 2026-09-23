#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""「改前必读」埋点的契约。

背景（2026-09-23）：规则区「证据优先」是最大一块（501 字符、占 10.9%），审计却报
「无数据源」——它的判据（改文件前必须读过目标区间）其实精确可测，只是从来没埋过点。
没埋点的规则只能人工判断留删，而人工判断等于没判断。

契约（六条）：
  1. 路径归一化：绝对/相对/./x/带 `:起-止` 后缀/带引号，全部归一到同一个键
     —— 不归一化的话「读过的」与「要改的」永远对不上，指标直接废掉
  2. 读类工具登记范围；read_lines 的 max_lines 换算成区间上界
  3. 编辑类工具取目标：code_edit（file+行号 / edits 数组）、code_append、
     code_patch（文件只出现在 diff 的 `+++ b/路径` 行里，前缀要剥）
  4. 取不到目标不计入分母——宁可不判，也不假警（新建文件不是盲改）
  5. 判定方向是「宁可认为读过」：无行号退化为文件级，空 reads 才算盲改
  6. 两条工具执行分支同挂 + 轮级计数在每轮入口清零（源码级断言，漏挂立刻红）

这些用例不读真实日志；只有源码级契约读真实 agent.py。
"""
import importlib.util
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))


def _load_agent():
    spec = importlib.util.spec_from_file_location("_agent_blind", BASE / "agent.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _agent_lines():
    return (BASE / "agent.py").read_text(encoding="utf-8").splitlines()


class _Stub:
    """只带 _watch_read_edit 需要的那几个字段，用未绑定方法调用。"""


def _mk():
    s = _Stub()
    s._read_files = set()
    s._read_spans = {}
    s._blind_edits = 0
    s._edit_calls = 0
    s._inject_lines = 0
    s._comment_lines = 0
    s._placeholder_comments = 0
    return s


# ---------- 契约 1：路径归一化 ----------

def test_norm_file_collapses_all_spellings():
    mod = _load_agent()
    want = str(BASE / "agent.py")
    assert mod._norm_file("agent.py") == want
    assert mod._norm_file("./agent.py") == want
    assert mod._norm_file(want) == want
    assert mod._norm_file("agent.py:10-40") == want
    assert mod._norm_file('"agent.py"') == want
    assert mod._norm_file("  agent.py  ") == want
    assert mod._norm_file("") == ""
    assert mod._norm_file(None) == ""


def test_norm_file_keeps_distinct_files_apart():
    """归一化不能把不同文件合成一个键——那会把盲改洗成已读。"""
    mod = _load_agent()
    assert mod._norm_file("agent.py") != mod._norm_file("tool_gate.py")
    assert mod._norm_file("tools/x.py") != mod._norm_file("x.py")


# ---------- 契约 2：读类工具的范围登记 ----------

def test_read_spans_of_code_read_splits_and_parses():
    mod = _load_agent()
    got = mod._read_spans_of("code_read", {"files": "a.py:1-10, b.py"})
    assert (str(BASE / "a.py"), 1, 10) in got
    assert (str(BASE / "b.py"), None, None) in got
    assert len(got) == 2


def test_read_spans_of_read_lines_uses_max_lines_bound():
    mod = _load_agent()
    got = mod._read_spans_of("read_lines", {"path": "a.py", "start": 5, "max_lines": 20})
    assert got == [(str(BASE / "a.py"), 5, 24)]


def test_read_spans_of_ignores_non_read_tools():
    mod = _load_agent()
    assert mod._read_spans_of("code_edit", {"file": "a.py"}) == []
    assert mod._read_spans_of("code_read", None) == []


# ---------- 契约 3：编辑目标提取 ----------

def test_edit_targets_code_edit_with_lines():
    mod = _load_agent()
    got = mod._edit_targets_of("code_edit", {"file": "a.py", "line_start": 5, "line_end": 8})
    assert got == [(str(BASE / "a.py"), 5, 8)]


def test_edit_targets_code_edit_line_start_only():
    mod = _load_agent()
    got = mod._edit_targets_of("code_edit", {"file": "a.py", "line_start": 5})
    assert got == [(str(BASE / "a.py"), 5, 5)]


def test_edit_targets_code_edit_batch_edits():
    """批量 edits 数组：每项的行号都要收，且去重后不与顶层 file 重复。"""
    mod = _load_agent()
    got = mod._edit_targets_of("code_edit", {
        "file": "a.py",
        "edits": [{"line_start": 1, "line_end": 2}, {"line_start": 9}]})
    assert (str(BASE / "a.py"), 1, 2) in got
    assert (str(BASE / "a.py"), 9, 9) in got
    assert len(got) == len(set(got))


def test_edit_targets_code_patch_strips_git_prefix():
    """`+++ b/路径` 的 b/ 必须剥掉，否则和读时的键对不上、盲改永远判不出来。"""
    mod = _load_agent()
    got = mod._edit_targets_of("code_patch", {
        "patch": "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-a\n+b\n"})
    assert got == [(str(BASE / "x.py"), None, None)]


def test_edit_targets_code_append():
    mod = _load_agent()
    assert mod._edit_targets_of("code_append", {"path": "out.py"}) == [
        (str(BASE / "out.py"), None, None)]


# ---------- 契约 4：取不到目标不判 ----------

def test_edit_targets_empty_when_unknown():
    """宁可不判（少算一次分母），也不假警——把新建文件说成盲改会让指标失去可信度。"""
    mod = _load_agent()
    assert mod._edit_targets_of("code_edit", {}) == []
    assert mod._edit_targets_of("code_edit", {"old": "x", "new": "y"}) == []
    assert mod._edit_targets_of("code_patch", {"patch": "no diff here"}) == []
    assert mod._edit_targets_of("code_edit", None) == []


def test_code_create_file_is_not_counted():
    """新建文件本来就不该「先读」——它不在编辑类工具里，别把它算成盲改。"""
    mod = _load_agent()
    assert "code_create_file" not in mod._EDIT_TOOLS


# ---------- 契约 5：覆盖判定方向 ----------

def test_span_covered_basic():
    mod = _load_agent()
    assert mod._span_covered([(1, 10)], 5, 8) is True
    assert mod._span_covered([(1, 10)], 50, 60) is False
    assert mod._span_covered([(1, 10)], 10, 20) is True   # 起点落在区间内
    assert mod._span_covered([(1, 10)], 0, 3) is True     # 终点落在区间内


def test_span_covered_falls_back_to_file_level():
    """无行号（整读 / 文本模式改）退化为文件级：宁可认为读过，不假报盲改。"""
    mod = _load_agent()
    assert mod._span_covered([(1, 10)], None, None) is True
    assert mod._span_covered([(None, None)], 9999, 10000) is True


def test_span_covered_empty_reads_is_blind():
    mod = _load_agent()
    assert mod._span_covered([], 5, 8) is False
    assert mod._span_covered([], None, None) is False


# ---------- 端到端：登记 → 判定 → 计数 ----------

def test_watch_read_edit_counts_blind_edits():
    mod = _load_agent()
    s = _mk()
    w = mod.AIAgent._watch_read_edit
    w(s, "code_read", {"files": "a.py:1-20"})
    w(s, "code_edit", {"file": "a.py", "line_start": 5, "line_end": 8})
    assert (s._edit_calls, s._blind_edits) == (1, 0)
    w(s, "code_edit", {"file": "a.py", "line_start": 100, "line_end": 110})
    assert (s._edit_calls, s._blind_edits) == (2, 1)
    w(s, "code_edit", {"file": "b.py", "line_start": 1})
    assert (s._edit_calls, s._blind_edits) == (3, 2)


def test_watch_read_edit_accumulates_across_turns():
    """读过的范围跨轮累积：第 3 轮读过、第 9 轮改，不算盲改（轮级计数才清零）。"""
    mod = _load_agent()
    s = _mk()
    w = mod.AIAgent._watch_read_edit
    w(s, "read_lines", {"path": "a.py", "start": 1, "max_lines": 50})
    mod.AIAgent._reset_read_edit_watch(s)
    assert (s._edit_calls, s._blind_edits) == (0, 0)
    w(s, "code_edit", {"file": "a.py", "line_start": 30, "line_end": 40})
    assert (s._edit_calls, s._blind_edits) == (1, 0)


def test_watch_read_edit_never_raises_on_garbage():
    """埋点绝不能反过来搞坏工具执行：参数是什么形状都不能抛。"""
    mod = _load_agent()
    s = _mk()
    w = mod.AIAgent._watch_read_edit
    for name, args in [("code_read", {"files": 123}), ("code_edit", {"file": []}),
                       ("code_patch", {"patch": None}), ("code_edit", {"line_start": "x"}),
                       (None, None), ("unknown_tool", {})]:
        w(s, name, args)


# ---------- 契约 6：源码级挂载与清零 ----------

def test_watch_is_hooked_on_both_branches():
    """两条工具执行分支必须同挂：只挂一条，另一条上的编辑永远不被判。

    与「清单提示」「验证提示」同款断言——它们都栽过一次「提示只挂在死分支上」。
    """
    lines = _agent_lines()
    calls = [i for i, l in enumerate(lines) if "self._watch_read_edit(" in l]
    assert len(calls) == 2, f"两条分支必须同挂，实际 {len(calls)} 处"
    for i in calls:
        ctx = "\n".join(lines[max(0, i - 4):i + 1])
        assert "_watch_plan_tool" in ctx, f"第 {i + 1} 行的挂载点没跟其它检查点同组"


def test_round_counters_reset_at_turn_entry():
    lines = _agent_lines()
    idx = [i for i, l in enumerate(lines)
           if "self._reset_read_edit_watch()" in l and "def " not in l]
    assert len(idx) == 1, f"每轮入口应只有一个清零点，实际 {len(idx)}"
    ctx = "\n".join(lines[max(0, idx[0] - 8):idx[0] + 2])
    assert "_reset_plan_watch()" in ctx, "清零点必须跟其它轮级检查点挂在同一个每轮入口"


def test_metrics_fields_are_recorded():
    """埋点必须落进 turn_metrics，否则「规则有没有用」还是无从判断。"""
    lines = _agent_lines()
    assert any('"blind_edits": eff_blind_edits' in l for l in lines)
    assert any('"edit_calls": eff_edit_calls' in l for l in lines)
    assert any("eff_blind_edits = 0" in l for l in lines)
    assert any('int(getattr(self, "_blind_edits", 0) or 0)' in l for l in lines)


# ---------- 「注释只写 why」埋点（2026-09-23 同批接入） ----------
# 规则的硬判据是「不留改名占位、不留『已移除』注释」；「有信息量」要语义判断、不可测。

def test_comment_stats_counts_only_whole_line_comments():
    """只认整行注释：shebang/编码声明是给解释器的，行尾注释是工具指令。

    行尾注释混进来会让分母失真——`x = 1  # noqa` 不是解释。
    """
    mod = _load_agent()
    c, ph, tot = mod._comment_stats([
        "#!/usr/bin/env python3",
        "# -*- coding: utf-8 -*-",
        "import os",
        "",
        "    # 为什么这么写：结果会串号",
        "x = 1  # noqa",
        "    // js 行注释",
        "  * 块注释续行",
    ])
    assert (c, ph, tot) == (3, 0, 7)


def test_comment_stats_flags_placeholder_comments():
    """占位/残留注释零误报：正常注释不会写「已移除」。"""
    mod = _load_agent()
    c, ph, _ = mod._comment_stats([
        "# 已移除：旧实现走 v1 分支",
        "# 保留占位，等下一轮补",
        "# 这里必须按字节比对，因为 provider 会改行尾",
    ])
    assert (c, ph) == (3, 2)


def test_injected_code_lines_takes_only_the_new_side():
    """code_patch 只取 `+` 侧：被删掉的旧代码不是我注入的。"""
    mod = _load_agent()
    patch = "--- a/x.py\n+++ b/x.py\n@@\n-old = 1\n+new = 2\n+new2 = 3\n"
    assert mod._injected_code_lines("code_patch", {"patch": patch}) == ["new = 2", "new2 = 3"]
    assert mod._injected_code_lines("code_edit", {"new": "a\nb"}) == ["a", "b"]
    assert mod._injected_code_lines(
        "code_edit", {"new": "a", "edits": [{"new": "c"}, {"new": "d"}]}) == ["a", "c", "d"]
    assert mod._injected_code_lines("code_append", {"content": "z"}) == ["z"]
    assert mod._injected_code_lines("code_create_file", {"content": "q"}) == ["q"]
    assert mod._injected_code_lines("code_read", {"files": "a.py"}) == []


def test_watch_counts_injected_comments_including_new_files():
    """code_create_file 不进盲改分母（新建文件本来就没读过），但注释要算。"""
    mod = _load_agent()
    s = _mk()
    w = mod.AIAgent._watch_read_edit
    w(s, "code_create_file", {"path": "n.py", "content": "# 已移除\nx = 1\n"})
    assert (s._inject_lines, s._comment_lines, s._placeholder_comments) == (2, 1, 1)
    assert (s._edit_calls, s._blind_edits) == (0, 0)
    w(s, "code_edit", {"file": "a.py", "new": "# why\n"})
    assert (s._inject_lines, s._comment_lines, s._placeholder_comments) == (3, 2, 1)
    assert (s._edit_calls, s._blind_edits) == (1, 1)


def test_comment_counters_reset_at_turn_entry():
    mod = _load_agent()
    s = _mk()
    s._inject_lines = s._comment_lines = s._placeholder_comments = 5
    mod.AIAgent._reset_read_edit_watch(s)
    assert (s._inject_lines, s._comment_lines, s._placeholder_comments) == (0, 0, 0)


def test_comment_metrics_fields_are_recorded():
    """埋点必须落进 turn_metrics，否则「注释只写 why」还是无从判断。"""
    lines = _agent_lines()
    for f in ("inject_lines", "comment_lines", "placeholder_comments"):
        assert any(f'"{f}": eff_' in l for l in lines), f
