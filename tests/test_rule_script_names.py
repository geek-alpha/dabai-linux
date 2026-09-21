#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""准则段埋点「shell 跑过哪些脚本」的契约：埋点自己要能自证，否则 0 次分不清两种意思。

背景（2026-09-22）：规则区准则段 3539 字符此前全是 UNMEASURED。call_names 只到
工具名一层，而 94% 的轮（252/269）都用 shell_run——「教训写了吗」「长期事业 log
了吗」全藏在命令参数里。补了 script_names 埋点后，审计能判「经验回流」「长期事业」。

为什么必须有这份测试：埋点失效和「行为没发生」在数据上长得一模一样（都是 0）。
没有纯函数级用例，下一轮看到 0 只会以为「最近没写教训」，不会想到是正则坏了。

契约：
  1. 只认 shell 类工具，非 shell 传什么都返回 []
  2. 认 `python tools/x.py`、`venv/bin/python tools/x.py`、`cd /a && python x.py`
  3. 一次命令跑多个脚本时全都要，去重且保序
  4. 参数里出现 .py 字样但工具不是 shell → []（不许误抓）
  5. 上限 20 个：埋点是观测，不能被超长命令撑爆
"""
import sys
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

import agent  # noqa: E402


# ---------- 契约 1：只认 shell ----------

@pytest.mark.parametrize("tool_name,args", [
    ("code_read", {"files": "tools/lesson_add.py"}),
    ("code_edit", {"file": "a.py"}),
    ("grep", {"pattern": "x.py"}),
    ("", {}),
    (None, {}),
    ("shell_run", {}),                    # 没有 command
    ("shell_run", {"command": ""}),
    ("shell_run", None),
])
def test_non_shell_or_empty_is_empty(tool_name, args):
    assert agent._rule_script_names(tool_name, args) == []


# ---------- 契约 2：常见写法都要认 ----------

@pytest.mark.parametrize("cmd,expected", [
    ("venv/bin/python tools/lesson_add.py \"教训\"", ["lesson_add.py"]),
    ("python tools/long_horizon.py log oss-contrib \"x\"", ["long_horizon.py"]),
    ("cd /home/wxf/dabai && python tools/rule_budget.py", ["rule_budget.py"]),
    ("/home/wxf/dabai/venv/bin/python /home/wxf/dabai/tools/status.py", ["status.py"]),
    ("python3 tools/prompt_rules_audit.py --json", ["prompt_rules_audit.py"]),
    ("python -m pytest tests/", []),      # 模块方式没有 .py，不硬凑
])
def test_shell_command_forms(cmd, expected):
    assert agent._rule_script_names("shell_run", {"command": cmd}) == expected


# ---------- 契约 3：多脚本去重保序 ----------

def test_multiple_scripts_dedup_and_order():
    cmd = "python tools/a.py && python tools/b.py; python tools/a.py"
    assert agent._rule_script_names("shell_run", {"command": cmd}) == ["a.py", "b.py"]


# ---------- 契约 4：非 shell 不许误抓 ----------

def test_py_in_args_of_non_shell_tool():
    assert agent._rule_script_names("code_edit", {"file": "tools/x.py"}) == []


# ---------- 契约 5：上限 ----------

def test_cap_at_20():
    cmd = " && ".join(f"python tools/s{i}.py" for i in range(30))
    assert len(agent._rule_script_names("shell_run", {"command": cmd})) == 20


# ---------- 与 _rule_op_kinds 的分工：删除判定不受影响 ----------

def test_delete_detection_still_works_alongside():
    args = {"command": "rm -f /tmp/x.bak && python tools/lesson_add.py \"y\""}
    assert agent._rule_op_kinds("shell_run", args)[2] == 1
    assert agent._rule_script_names("shell_run", args) == ["lesson_add.py"]
