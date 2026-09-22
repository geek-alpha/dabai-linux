# -*- coding: utf-8 -*-
"""子智能体的工具结果截断必须自描述（2026-09-22 第 15 轮自我迭代循环零产出的直接原因）。

实测链条：worker 照任务书去读 skills/code_ops/skill.json 里 shell_run / code_read /
code_append 三条 description 的现状，用 shell_run 打印整份 JSON（实测 25268 字符）。
sub_agents._run_one_tool 当时是 `str(result)[:8000]` 裸砍：code_append 的定义在第
8352 字符、shell_run 在第 14112 字符 —— 两条全被砍掉，而且结果里一个字都没说被砍了。
模型以为「文件就长这样」，于是反复重跑同一条命令：4 轮调用指纹与结果指纹完全相同，
触发原地打转检测被硬停，整轮零产出（status: 本周期 1/24、dispatched 15、记账 14）。

判据（可推翻）：长结果工具（shell_run）放宽到 16000、普通工具仍 2000，且被截断时
必须带「已省略中间 N 字符」。把 _fit_result 换回裸砍，下面四条立刻红。
"""
import importlib
import pathlib
import re
import sys

BASE = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

sa = importlib.import_module("sub_agents")


def test_长结果工具不再被砍在8000():
    """shell_run 结果 12000 字符：裸砍时代只剩 8000，现在必须原样拿到。"""
    raw = "$ cmd\n[exit=0]\n" + "x" * 12000
    out = sa._fit_result("shell_run", raw)
    assert out == raw, f"shell_run 结果被截断到 {len(out)} 字符"
    assert len(out) > 8000


def test_普通工具截断带缺口说明():
    """普通工具仍按 2000 上限，但必须说清被省略了多少 —— 裸砍是这次的病根。"""
    out = sa._fit_result("search_web", "y" * 9000)
    assert len(out) <= 2000, len(out)
    assert "结果被截断" in out and "已省略中间" in out, out[:200]


def test_长结果工具截断也带缺口说明():
    out = sa._fit_result("shell_run", "z" * 20000)
    assert len(out) <= 16000, len(out)
    assert "结果被截断" in out, out[:200]


def test_源码里不再有裸砍():
    """反证：把两处调用改回裸砍切片这条立刻红（只在赋值/返回位置找，
    不误伤 _fit_result 文档里对旧写法的引用）。
    """
    src = (BASE / "sub_agents.py").read_text(encoding="utf-8")
    assert not re.search(r"=\s*str\(result\)\[:8000\]", src)
    assert not re.search(r"return\s+str\(hit\[0\]\)\[:8000\]", src)
    assert "_fit_result(name, result)" in src
