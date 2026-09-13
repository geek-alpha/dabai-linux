# -*- coding: utf-8 -*-
"""字符串 → 字符串数组的类型翻译回归测试。

背景：data/longrun/traces 里 4 次「queries: 期望数组(array)，实际是 str」全是白跑——
code_search 的 schema 描述（skills/code_ops/skill.json:28）和实现（code_ops_impl.py:421）
都明确接受分隔符字符串，只有校验层比契约更严。

关键：2/4 次是多关键词（28.jsonl 换行 6 个、9.jsonl 逗号 4 个），所以必须按分隔符切分，
整串包成单元素会让检索静默搜不到——比报错更糟。下面两条用真实 trace 值锁死这一点。
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tool_validation import _coerce_string_array, validate_arguments  # noqa: E402

STR_ARRAY = {"type": "array", "items": {"type": "string"}}

# data/longrun/traces 里的真实调用值（ToolCallStart.arguments）
REAL_MULTI_NEWLINE = ("LONGRUN_MAX_CALLS\nLONGRUN_BLOCK_AFTER\n"
                      "STOP|PAUSE|kill_switch|emergency\nbudget|预算\n"
                      "def main|argparse\nattempt")
REAL_MULTI_COMMA = "idlefish,com.alibaba.wireless,闲鱼,1688"
REAL_SINGLE = "reasoning_content"


def _spec(props, required=None):
    params = {"type": "object", "properties": props}
    if required:
        params["required"] = required
    return {"type": "function", "function": {"name": "t", "parameters": params}}


def _load_impl():
    p = ROOT / "skills" / "code_ops" / "code_ops_impl.py"
    spec = importlib.util.spec_from_file_location("code_ops_impl_t", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _real_code_search_spec():
    skill = json.loads((ROOT / "skills" / "code_ops" / "skill.json").read_text(encoding="utf-8"))
    return next(t for t in skill["tools"] if t["function"]["name"] == "code_search")


# ---------- 单关键词 ----------

def test_single_keyword_wrapped():
    args, err = validate_arguments(_spec({"queries": STR_ARRAY}), {"queries": REAL_SINGLE})
    assert err is None
    assert args["queries"] == [REAL_SINGLE]


# ---------- 多关键词必须切分（真实值回归） ----------

def test_newline_separated_splits():
    args, err = validate_arguments(_spec({"queries": STR_ARRAY}), {"queries": REAL_MULTI_NEWLINE})
    assert err is None
    assert args["queries"] == [
        "LONGRUN_MAX_CALLS", "LONGRUN_BLOCK_AFTER", "STOP|PAUSE|kill_switch|emergency",
        "budget|预算", "def main|argparse", "attempt"]


def test_comma_separated_splits():
    args, err = validate_arguments(_spec({"queries": STR_ARRAY}), {"queries": REAL_MULTI_COMMA})
    assert err is None
    assert args["queries"] == ["idlefish", "com.alibaba.wireless", "闲鱼", "1688"]


def test_cjk_separators_and_blank_dropped():
    ok, out = _coerce_string_array("a，b；c;;  \n d , ", STR_ARRAY)
    assert ok and out == ["a", "b", "c", "d"]


def test_blank_string_becomes_empty_list():
    ok, out = _coerce_string_array("   ", STR_ARRAY)
    assert ok and out == []


# ---------- 反向：不该包装的一律继续报错（防静默放行） ----------

def test_object_items_not_wrapped():
    spec = _spec({"edits": {"type": "array", "items": {"type": "object"}}})
    args, err = validate_arguments(spec, {"edits": "oops"})
    assert args is None and "期望数组" in err


def test_missing_items_not_wrapped():
    args, err = validate_arguments(_spec({"xs": {"type": "array"}}), {"xs": "abc"})
    assert args is None and "期望数组" in err


def test_non_string_not_wrapped():
    args, err = validate_arguments(_spec({"queries": STR_ARRAY}), {"queries": 123})
    assert args is None and "期望数组" in err


def test_existing_list_untouched():
    args, err = validate_arguments(_spec({"queries": STR_ARRAY}), {"queries": ["a", "b"]})
    assert err is None and args["queries"] == ["a", "b"]


def test_items_constraints_still_enforced():
    spec = _spec({"queries": {"type": "array", "items": {"type": "string", "minLength": 3}}})
    args, err = validate_arguments(spec, {"queries": "ab"})
    assert args is None and "长度不足" in err


# ---------- 契约回归：包装结果 == 实现直接收到字符串时的解析结果 ----------

def test_real_spec_matches_impl_parsing():
    """三方一致：schema 描述承诺、实现 _parse_queries 的 str 分支、校验层翻译。

    判据不是「校验通过」，而是「翻译后的数组喂给实现，解析出的关键词与实现直接收到
    原字符串时逐个相同」——否则就是包装引入了语义漂移。
    """
    impl = _load_impl()
    spec = _real_code_search_spec()
    for raw in (REAL_SINGLE, REAL_MULTI_NEWLINE, REAL_MULTI_COMMA, "gap|dur"):
        args, err = validate_arguments(spec, {"queries": raw})
        assert err is None, raw
        assert args["queries"] == impl._parse_queries({"queries": raw}), raw
