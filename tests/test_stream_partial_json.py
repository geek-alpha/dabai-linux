"""stream_partial_json 三态解析的行为锁定。

核心回归：流被截断时必须报 partial（而不是笼统的"解析失败"），
且 partial 的结果绝不能被当 ok 用去执行工具。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from stream_partial_json import BROKEN, OK, PARTIAL, parse_partial_json  # noqa: E402


def test_empty_args_is_ok_empty_dict():
    assert parse_partial_json("") == ({}, OK)
    assert parse_partial_json("   \n") == ({}, OK)


def test_complete_json_is_ok():
    value, status = parse_partial_json('{"path": "a.py", "line": 3}')
    assert status == OK
    assert value == {"path": "a.py", "line": 3}


def test_trailing_marker_ignored():
    # 模型偶尔在参数后跟 </tool_call>，旧实现靠 rfind("}") 截断，这里必须直接吃掉
    value, status = parse_partial_json('{"path": "a.py"}</tool_call>')
    assert status == OK
    assert value == {"path": "a.py"}


def test_brace_inside_string_value_not_misparsed():
    # 参数里含 } 是常态（code_edit 的 new 内容），不能被当成 JSON 边界
    value, status = parse_partial_json('{"content": "def f():\\n  return {}\\n"}')
    assert status == OK
    assert value == {"content": "def f():\n  return {}\n"}


def test_truncated_after_value_is_partial():
    value, status = parse_partial_json('{"path": "a.py", "line": 3')
    assert status == PARTIAL
    assert value == {"path": "a.py", "line": 3}


def test_truncated_mid_string_is_partial():
    value, status = parse_partial_json('{"content": "def f():\\n  pass')
    assert status == PARTIAL
    assert value == {"content": "def f():\n  pass"}


def test_truncated_with_trailing_comma_is_partial():
    value, status = parse_partial_json('{"path": "a.py",')
    assert status == PARTIAL
    assert value == {"path": "a.py"}


def test_truncated_with_brace_in_string_is_partial():
    value, status = parse_partial_json('{"content": "x}')
    assert status == PARTIAL
    assert value == {"content": "x}"}


def test_broken_returns_none():
    value, status = parse_partial_json("not json at all")
    assert status == BROKEN
    assert value is None


def test_array_args_are_ok():
    value, status = parse_partial_json('["a.py", "b.py"]')
    assert status == OK
    assert value == ["a.py", "b.py"]


def test_partial_status_is_not_ok():
    # 闸门：调用方若用 status == OK 判可执行，截断参数必须被挡住
    for buf in ('{"a": 1', '{"a": "x'):
        value, status = parse_partial_json(buf)
        assert status != OK
