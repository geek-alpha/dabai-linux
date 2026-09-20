#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""经验库写入闸门：负面清单四类硬拒，格式问题只软提示。

背景（2026-09-20）：写进经验库的东西会被后续每一轮对话读进 prompt 当先验。所以
写错一条的代价不是「多一条噪音」，是「下次照着它拒绝干活」——「X 工具不可用」这类
负面断言在故障修好之后仍然生效，来源是 hermes-agent/agent/background_review.py:392-415
的 _DO_NOT_CAPTURE_BLOCK。

契约：
  1. 四类硬拒（对工具的负面断言 / 环境依赖失败 / 未解决的失败写成推荐流程 /
     会话内自愈的瞬时故障）返回 3，且一个字都不写进库
  2. 环境依赖失败里写了 FIX（装什么、配什么）就放行——要留的是修法，不是故障
  3. 瞬时故障写成「重试模式」放行——教训是模式，不是那次故障
  4. 格式问题（缺 WHY、带日期锚点）只提示不拦，返回 0 且条目已入库
  5. --force 是唯一旁路，且只旁路硬拒
"""
import importlib.util
import json
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]


def _load(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("lesson_gate_under_test", BASE / "tools" / "lesson_add.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "FILE", tmp_path / "mem.json")
    monkeypatch.setattr(mod, "ARCHIVE", tmp_path / "archive.json")
    return mod


def _run(mod, monkeypatch, *args):
    monkeypatch.setattr(sys, "argv", ["lesson_add.py", *args])
    return mod.main()


def _lessons(mod):
    if not mod.FILE.exists():
        return []
    return json.loads(mod.FILE.read_text(encoding="utf-8"))["lessons"]


def test_negative_tool_claim_rejected(tmp_path, monkeypatch, capsys):
    mod = _load(tmp_path, monkeypatch)
    assert _run(mod, monkeypatch, "browser 工具坏了，以后别用它") == 3
    assert _lessons(mod) == [], "被拒的条目一个字都不该落库"
    out = capsys.readouterr().out
    assert "拒绝写入" in out and "自我拒绝" in out, "拒绝必须说明理由，不能静默丢"


def test_env_failure_without_fix_rejected(tmp_path, monkeypatch):
    mod = _load(tmp_path, monkeypatch)
    assert _run(mod, monkeypatch, "跑脚本报 No module named requests，环境有问题") == 3
    assert _lessons(mod) == []


def test_env_failure_with_fix_allowed(tmp_path, monkeypatch):
    mod = _load(tmp_path, monkeypatch)
    text = "报 No module named requests 时先 venv/bin/pip install requests —— 大白的 venv 不含全局包，因为解释器隔离"
    assert _run(mod, monkeypatch, text) == 0
    assert _lessons(mod) == [text], "要留的是 FIX，不是故障本身"


def test_unresolved_failure_dressed_as_workflow_rejected(tmp_path, monkeypatch):
    mod = _load(tmp_path, monkeypatch)
    assert _run(mod, monkeypatch, "三种办法都没成功，建议下次按这个流程试一遍") == 3
    assert _lessons(mod) == []


def test_transient_failure_rejected_but_retry_pattern_allowed(tmp_path, monkeypatch):
    mod = _load(tmp_path, monkeypatch)
    assert _run(mod, monkeypatch, "第一次请求超时，重试就成功了") == 3
    assert _lessons(mod) == []
    pattern = "上游超时先重试一次再换端点 —— 瞬时抖动占多数，立刻换路会白丢一次可用响应"
    assert _run(mod, monkeypatch, pattern) == 0
    assert _lessons(mod) == [pattern]


def test_soft_hints_do_not_block_write(tmp_path, monkeypatch, capsys):
    mod = _load(tmp_path, monkeypatch)
    assert _run(mod, monkeypatch, "改长文件用行号模式") == 0
    assert _lessons(mod) == ["改长文件用行号模式"], "缺 WHY 是提示，不是拒绝"
    out = capsys.readouterr().out
    assert "建议" in out and "WHY" in out

    assert _run(mod, monkeypatch, "2026-09-20 那次改了选择器") == 0
    assert len(_lessons(mod)) == 2
    assert "锚点" in capsys.readouterr().out


def test_force_bypasses_hard_reject_only(tmp_path, monkeypatch):
    mod = _load(tmp_path, monkeypatch)
    text = "某工具坏了"
    assert _run(mod, monkeypatch, text) == 3
    assert _run(mod, monkeypatch, "--force", text) == 0
    assert _lessons(mod) == [text], "--force 只旁路硬拒，写入路径本身不变"


def test_failure_as_premise_with_rule_is_allowed(tmp_path, monkeypatch):
    """真库 364 条里的两条假阳性：故障只当引子、后面给做法——那是规则，不是负面断言。

    判据太严会误伤「现象+规则」这个最常用的写法，而误伤的直接后果是下次一律
    加 --force 绕过去，闸门等于不存在。
    """
    mod = _load(tmp_path, monkeypatch)
    samples = [
        "检查类工具坏了，不许让那行凭空消失——返回 None 加调用方 if 跳过 = 读者把没检查读成没违规，要返回显式哨兵",
        "ssh 到 Windows 走 cmd.exe：引号会被吃掉，报 'A' 不是内部或外部命令。要把脚本写成 .ps1 走 -EncodedCommand",
    ]
    for s in samples:
        assert _run(mod, monkeypatch, s) == 0, s[:24]
    assert len(_lessons(mod)) == 2
