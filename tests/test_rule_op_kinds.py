#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""规则区「删除/画图/音乐」埋点的契约：分类要准，宁可宽也不能假阴。

背景（2026-09-14）：规则区 11 条里 8 条零埋点，「删哪条」只能靠感觉。给最容易
埋的三条补上计数后，判定语义变了——0 次不等于规则没用，只说明这段时间该行为
没被触发过，所以审计工具标 UNMEASURED 而不是「零收益候选」。

契约：
  1. 画图 = image_gen* ；音乐 = music_* ；删除 = 名字含 delete/remove
  2. shell 删除命令必须认出来：rm / rmdir / unlink / shred / git rm /
     find -delete / truncate -s 0（含 `cd /a && rm b`、`sudo rm x` 这类写法）
  3. 丢弃式重定向既不是删除也不是写：`ls 2>/dev/null` 必须是 (0,0,0)
     —— 这条踩过坑：判据把 2>/dev/null 当写重定向时，357 次单发 shell 判出
     0 个纯读段，差点得出「这里没有浪费」的反向结论
  4. 写重定向不是删除：`cat a.txt > b.txt` 归 (0,0,0)，这条规则只管删除
  5. 非 shell 工具不误判；未知工具一律 (0,0,0)
"""
import sys
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

import agent  # noqa: E402


# ---------- 契约 1：三类工具的归属 ----------

@pytest.mark.parametrize("tool_name,expected", [
    ("image_gen_create", (1, 0, 0)),
    ("image_gen_edit", (1, 0, 0)),
    ("music_search", (0, 1, 0)),
    ("music_play", (0, 1, 0)),
    ("music_lyric", (0, 1, 0)),
    ("music_play_playlist", (0, 1, 0)),
    ("todo_delete", (0, 0, 1)),
    ("agent_profile_delete", (0, 0, 1)),
    ("workspaces_remove", (0, 0, 1)),      # 非文件删除也算「删除请求」
    ("code_read", (0, 0, 0)),
    ("code_edit", (0, 0, 0)),
    ("delegate_agent_task", (0, 0, 0)),
])
def test_tool_name_classification(tool_name, expected):
    assert agent._rule_op_kinds(tool_name, {}) == expected


# ---------- 契约 2：shell 删除命令 ----------

@pytest.mark.parametrize("cmd", [
    "rm -rf /tmp/x",
    "cd /home/wxf/dabai && rm -f a.bak",
    "sudo rm /etc/x",
    "rmdir /tmp/empty",
    "unlink /tmp/a",
    "shred -u secret.txt",
    "git rm --cached a.py",
    'find . -name "*.bak" -delete',
    "truncate -s 0 logs/x.log",
])
def test_shell_delete_commands_detected(cmd):
    assert agent._rule_op_kinds("shell_run", {"command": cmd})[2] == 1


# ---------- 契约 3：丢弃式重定向不是删除 ----------

@pytest.mark.parametrize("cmd", [
    "ls -la data/ 2>/dev/null",
    "cat x.json 2> /dev/null",
    "venv/bin/python tools/x.py 2>&1 | tail -3",
    "systemctl status x > /dev/null 2>&1",
])
def test_discarding_redirect_is_not_delete(cmd):
    assert agent._rule_op_kinds("shell_run", {"command": cmd}) == (0, 0, 0)


# ---------- 契约 4：写重定向不是删除 ----------

def test_write_redirect_is_not_delete():
    assert agent._rule_op_kinds("shell_run", {"command": "cat a.txt > b.txt"}) == (0, 0, 0)


# ---------- 契约 5：非 shell / 未知输入 ----------

@pytest.mark.parametrize("tool_name,args", [
    ("code_read", {"files": "rm -rf x"}),        # 参数里有 rm 字样，但工具不是 shell
    ("shell_run", {}),                           # 没有 command
    ("shell_run", {"command": ""}),
    ("shell_run", None),
    ("", {}),
    (None, {}),
    ("grep", {"pattern": "rm"}),                 # 不在 shell 白名单里
])
def test_non_shell_or_unknown_is_zero(tool_name, args):
    assert agent._rule_op_kinds(tool_name, args) == (0, 0, 0)
