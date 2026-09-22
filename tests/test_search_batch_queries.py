# -*- coding: utf-8 -*-
"""search_batch 的 queries 必须同时收 JSON 串与真数组（双向契约回归）。

背景：data/longrun/traces/15.jsonl 里同一个查询载荷出现 7 次——6 次写成 JSON 字符串、
1 次写成真数组，那 1 次被校验层拦下（queries: 类型不符，期望 string，实际是 list）。
模型对同一语义两种写法都自然，契约必须两种都收，且必须落到同一组 CLI 查询项。

核心判据不是「校验通过」，而是「数组直传与 JSON 串传，CLI 解析出的查询项逐条相同」：
数组若走 str(list) 会变成 Python repr（单引号不是合法 JSON），CLI 的 json.loads 失败后
落到「按分隔符切分」分支，把整段 JSON 文本当成一个查询词——静默搜错，比报错更糟。
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tool_validation import validate_arguments  # noqa: E402

# data/longrun/traces/15.jsonl 的真实调用值（第 4 次是数组，其余 6 次是同一载荷的 JSON 串）
REAL_LIST = ["闲鱼网页版 可以发布商品吗 卖家中心 擦亮",
             "1688 网页版 消息中心 询盘 在线聊天 入口",
             "1688 采购助手插件 批量询盘 批量下单 功能"]
REAL_JSON_STR = json.dumps(REAL_LIST, ensure_ascii=False)

UNION = {"type": ["string", "array"]}


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _search_skill():
    return _load(ROOT / "skills" / "search" / "skill.py", "search_skill_t")


def _cli():
    return _load(ROOT / "skills/search/engines/anysearch-skill-main/scripts/anysearch_cli.py",
                 "anysearch_cli_t")


def _batch_spec():
    skill = json.loads((ROOT / "skills" / "search" / "skill.json").read_text(encoding="utf-8"))
    return next(t for t in skill["tools"] if t["function"]["name"] == "search_batch")


def _spec(props, required=None):
    params = {"type": "object", "properties": props}
    if required:
        params["required"] = required
    return {"type": "function", "function": {"name": "t", "parameters": params}}


def _captured_queries(monkeypatch, payload):
    """跑 search_batch 但截住 _run，返回真正传给 CLI 的 --queries 值。"""
    mod = _search_skill()
    seen: dict = {}

    def fake_run(cmd):
        seen["cmd"] = cmd
        return "ok"

    monkeypatch.setattr(mod, "_run", fake_run)
    out = mod.search_batch(payload)
    assert "cmd" in seen, out
    cmd = seen["cmd"]
    return cmd[cmd.index("--queries") + 1]


# ---------- 校验层：真实值两种写法都要过 ----------

def test_real_trace_array_passes_validation():
    args, err = validate_arguments(_batch_spec(), {"queries": REAL_LIST, "max_results": 5})
    assert err is None, err
    assert args["queries"] == REAL_LIST


def test_real_trace_json_string_still_passes():
    args, err = validate_arguments(_batch_spec(), {"queries": REAL_JSON_STR})
    assert err is None, err
    assert args["queries"] == REAL_JSON_STR


# ---------- 实现层：两种写法必须解析出同一组查询项 ----------

def test_array_and_json_string_give_identical_cli_items(monkeypatch):
    cli = _cli()
    from_arr = cli._parse_query_items(_captured_queries(monkeypatch, {"queries": REAL_LIST}), [])
    from_str = cli._parse_query_items(_captured_queries(monkeypatch, {"queries": REAL_JSON_STR}), [])
    assert from_arr == from_str
    assert [i["query"] for i in from_arr] == REAL_LIST


def test_array_payload_is_valid_json(monkeypatch):
    raw = _captured_queries(monkeypatch, {"queries": REAL_LIST})
    assert raw.startswith("[")
    assert json.loads(raw) == REAL_LIST
    # 反证：旧行为 str(list) 产出的是 Python repr，json.loads 必炸——这正是查询被弄丢的原因
    with pytest.raises(json.JSONDecodeError):
        json.loads(str(REAL_LIST))


def test_object_item_keeps_its_fields(monkeypatch):
    payload = [{"query": "AAPL", "domain": "finance", "max_results": 3}]
    raw = _captured_queries(monkeypatch, {"queries": payload})
    assert json.loads(raw) == payload
    items = _cli()._parse_query_items(raw, [])
    assert items[0]["query"] == "AAPL" and items[0]["domain"] == "finance"


def test_query_shorthand_untouched(monkeypatch):
    raw_items = _cli()._parse_query_items("", ["AAPL", "GOOG"])
    assert [i["query"] for i in raw_items] == ["AAPL", "GOOG"]


# ---------- 校验层：联合类型不能变成「什么都放行」 ----------

def test_union_accepts_both_branches():
    for v in ("abc", ["a", "b"]):
        args, err = validate_arguments(_spec({"q": UNION}), {"q": v})
        assert err is None, (v, err)
        assert args["q"] == v


def test_union_rejects_type_in_no_branch():
    args, err = validate_arguments(_spec({"q": UNION}), {"q": {"a": 1}})
    assert args is None and "类型不符" in err


def test_union_enum_reported_as_range_not_type():
    """枚举越界必须报「取值范围」，不能被联合类型的类型错盖掉——否则修正方向被带偏。"""
    spec = _spec({"q": {"type": ["string", "array"], "enum": ["a", "b"]}})
    args, err = validate_arguments(spec, {"q": "zzz"})
    assert args is None
    assert "取值" in err and "类型不符" not in err


def test_union_array_branch_still_checks_items():
    # 注意 items 里的 123 不算错：_coerce_scalar 允许数字→字符串（与改动前的宽松规则一致）
    spec = _spec({"q": {"type": ["string", "array"], "items": {"type": "string"}}})
    args, err = validate_arguments(spec, {"q": ["ok", {"a": 1}]})
    assert args is None and "q[1]" in err


def test_plain_string_schema_unchanged():
    args, err = validate_arguments(_spec({"q": {"type": "string"}}), {"q": 7})
    assert err is None and args["q"] == "7"
