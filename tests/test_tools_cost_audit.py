#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tools_cost_audit 的回归测试。

核心要锁死的是「判据能双向翻转」：KEEP_LAZY 与 GO_EAGER 两个结论都必须能被
同一段代码算出来。只测 KEEP_LAZY 的判据是恒真判据——真实数据恰好落在这一侧，
不代表代码算得对。
"""
import json
import sys
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "tools"))

import tools_cost_audit as T  # noqa: E402


def mkrow(prompt=1000, miss=20, llm=1, tools_chars=100, tools_count=5,
          changed=False, cause="history"):
    return {
        "prompt_tokens": prompt, "cache_miss": miss, "llm_calls": llm,
        "tools_chars": tools_chars, "tools_count": tools_count,
        "prefix": {"tools_diff": {"any": changed}, "cause": cause},
    }


# ---------- 数据加载容错 ----------

def test_load_rows_缺失文件返回空(tmp_path):
    assert T.load_rows(tmp_path / "nope.jsonl") == []


def test_load_rows_坏行跳过不抛(tmp_path):
    p = tmp_path / "m.jsonl"
    p.write_text('{"a": 1}\n不是json\n\n{"b": 2}\n', encoding="utf-8")
    rows = T.load_rows(p)
    assert len(rows) == 2 and rows[1]["b"] == 2


def test_load_trace_过滤自检记录(tmp_path):
    p = tmp_path / "t.jsonl"
    recs = [{"tag": "run", "reason": "activate:x", "names": ["a"]},
            {"tag": "test", "reason": "reset", "names": ["b"]}]
    p.write_text("\n".join(json.dumps(r) for r in recs), encoding="utf-8")
    got = T.load_trace(p)
    assert [r["tag"] for r in got] == ["run"]


def test_load_tool_calls_缺失库返回空(tmp_path):
    assert sum(T.load_tool_calls(tmp_path / "nope.db").values()) == 0


# ---------- 核心计算 ----------

def test_变化轮miss率高于稳定轮时判KEEP_LAZY():
    # 量级要贴近真实：2445 次往返、工具集差 26947 字符。若只给几次往返，
    # 节省量被线性缩小，会得出与真实相反的结论（见下一条用例）。
    rows = [mkrow(prompt=1000, miss=20, llm=100, tools_chars=400) for _ in range(3)]
    rows += [mkrow(prompt=1000, miss=200, llm=100, tools_chars=50, changed=True)
             for _ in range(2)]
    a = T.audit(rows, [], T.collections.Counter())
    assert a["changed_turns"] == 2
    assert a["miss_rate_stable"] == pytest.approx(2.0)
    assert a["miss_rate_changed"] == pytest.approx(20.0)
    # 额外 miss = 400 − 2000×0.02 = 360 tokens
    assert a["extra_miss_tokens"] == 360
    # 省 = (400 − 260) × 500 = 70000 字符，远大于多付的 493 字符
    assert a["saved_chars"] == 70000
    assert a["verdict"] == "KEEP_LAZY"


def test_小样本下收益不足以覆盖代价时判GO_EAGER():
    """同一段代码在低往返样本下必须能给出相反结论：节省量与往返数成正比，
    只跑 5 次往返时按需加载省下的字符还不够付前缀失效的账。"""
    rows = [mkrow(prompt=1000, miss=20, llm=1, tools_chars=100) for _ in range(3)]
    rows += [mkrow(prompt=1000, miss=200, llm=1, tools_chars=50, changed=True)
             for _ in range(2)]
    a = T.audit(rows, [], T.collections.Counter())
    assert a["verdict"] == "GO_EAGER"


def test_收益为零且额外miss大时判GO_EAGER():
    """反例：同样的代码必须能得出相反结论，否则判据恒真。"""
    rows = [mkrow(prompt=1000, miss=20, llm=1, tools_chars=100) for _ in range(3)]
    rows += [mkrow(prompt=1000, miss=200, llm=1, tools_chars=100, changed=True)
             for _ in range(2)]
    a = T.audit(rows, [], T.collections.Counter())
    assert a["saved_chars"] == 0          # 工具集从未小于全量 → 没省任何东西
    assert a["net_chars"] < 0
    assert a["verdict"] == "GO_EAGER"


def test_额外miss永不为负():
    """变化轮 miss 率反而更低时，不能报出负的额外成本（那是数据噪声，不是收益）。"""
    rows = [mkrow(prompt=1000, miss=100, llm=1) for _ in range(3)]
    rows += [mkrow(prompt=1000, miss=1, llm=1, changed=True) for _ in range(2)]
    a = T.audit(rows, [], T.collections.Counter())
    assert a["extra_miss_tokens"] == 0


def test_节省量按往返次数放大():
    rows = [mkrow(prompt=100, miss=1, llm=10, tools_chars=200) for _ in range(2)]
    rows.append(mkrow(prompt=100, miss=1, llm=10, tools_chars=400))
    a = T.audit(rows, [], T.collections.Counter())
    # 均值 = (200+200+400)/3 = 266.67；max = 400；llm 合计 30
    assert a["llm_calls"] == 30
    assert a["saved_chars"] == pytest.approx((400 - 266.6667) * 30, rel=1e-3)


def test_空样本不崩():
    a = T.audit([], [], T.collections.Counter())
    assert a["turns"] == 0 and a["verdict"] == "GO_EAGER"


# ---------- 口径缺失（provider 未回传 cache 字段） ----------
#
# 背景：缺字段被 getattr 兜底成 0 时，0 进不了 miss 分子、prompt 却全额进分母，
# 于是 miss 率被稀释压低。所以 miss_rate_all 只是下界，必须能把两类行分开。

def mkrow_raw(prompt=1000, hit=None, miss=None, reported=None, llm=1, changed=False):
    r = {"prompt_tokens": prompt, "llm_calls": llm, "tools_chars": 100,
         "tools_count": 5,
         "prefix": {"tools_diff": {"any": changed}, "cause": "history"}}
    if hit is not None:
        r["cache_hit"] = hit
    if miss is not None:
        r["cache_miss"] = miss
    if reported is not None:
        r["cache_reported"] = reported
    return r


def test_零口径轮不计入剔除后miss率():
    rows = [mkrow_raw(prompt=1000, hit=980, miss=20) for _ in range(3)]
    rows += [mkrow_raw(prompt=1000, hit=0, miss=0) for _ in range(2)]
    a = T.audit(rows, [], T.collections.Counter())
    assert a["reported_rows"] == 3
    assert a["unreported_rows"] == 2
    assert a["unreported_prompt_tokens"] == 2000
    assert a["miss_rate_all"] == pytest.approx(1.2)            # 60 / 5000
    assert a["miss_rate_reported_only"] == pytest.approx(2.0)  # 60 / 3000
    # 下界语义：剔除零口径后只会更高，不可能更低
    assert a["miss_rate_reported_only"] > a["miss_rate_all"]
    assert "口径缺失 2 轮" in T.render(a)


def test_同值不同口径必须可区分():
    """两组数据 miss_rate_all 完全相同，口径完整性必须还能分开——
    否则「3.48% 到底是真值还是被稀释的下界」永远说不清。"""
    a_rows = [mkrow_raw(prompt=1000, hit=980, miss=20) for _ in range(2)]
    a_rows += [mkrow_raw(prompt=1000, hit=0, miss=0) for _ in range(2)]
    b_rows = [mkrow_raw(prompt=1000, hit=990, miss=10) for _ in range(4)]
    A = T.audit(a_rows, [], T.collections.Counter())
    B = T.audit(b_rows, [], T.collections.Counter())
    assert A["miss_rate_all"] == pytest.approx(B["miss_rate_all"]) == pytest.approx(1.0)
    assert A["unreported_rows"] == 2 and B["unreported_rows"] == 0
    assert A["miss_rate_reported_only"] == pytest.approx(2.0)
    assert B["miss_rate_reported_only"] == pytest.approx(1.0)


def test_显式口径声明优先于数值启发式():
    """provider 明确回传 0/0 时不该被误判成缺口径；声明缺口径时不该因有数值就采信。"""
    rows = [mkrow_raw(prompt=1000, hit=0, miss=0, reported=True),
            mkrow_raw(prompt=1000, hit=980, miss=20, reported=False)]
    a = T.audit(rows, [], T.collections.Counter())
    assert a["reported_rows"] == 1
    assert a["unreported_rows"] == 1
    assert a["unreported_prompt_tokens"] == 1000


def test_prompt为零的轮不算有口径():
    rows = [mkrow_raw(prompt=0, hit=0, miss=0)]
    a = T.audit(rows, [], T.collections.Counter())
    assert a["reported_rows"] == 0 and a["unreported_rows"] == 1
    assert a["miss_rate_reported_only"] == 0.0  # 分母为 0 不除零


def test_零口径轮不再污染额外miss():
    """零口径轮同时在两侧污染：stable 侧稀释基线（extra 变大）、changed 侧撑大
    pt_chg（extra 变小），净效应方向相反。所以口径必须整体一致——往数据里塞
    零口径轮，修正口径必须纹丝不动，旧混合口径必须漂。"""
    base = [mkrow_raw(prompt=1000, hit=980, miss=20) for _ in range(3)]
    base += [mkrow_raw(prompt=1000, hit=900, miss=100, changed=True) for _ in range(2)]
    noisy = base + [mkrow_raw(prompt=5000, hit=0, miss=0) for _ in range(2)]
    noisy += [mkrow_raw(prompt=5000, hit=0, miss=0, changed=True)]
    A = T.audit(base, [], T.collections.Counter())
    B = T.audit(noisy, [], T.collections.Counter())
    assert A["extra_miss_tokens"] == 160            # 2000 × (10% − 2%)
    assert B["extra_miss_tokens"] == A["extra_miss_tokens"]   # 不变量
    assert B["extra_miss_tokens_mixed"] != A["extra_miss_tokens_mixed"]
    assert B["unreported_rows"] == 3


def test_额外miss基线取自reported子集():
    """基线必须和分子同口径：被零口径轮稀释后的 miss_rate_stable 会低估基线。"""
    rows = [mkrow_raw(prompt=1000, hit=980, miss=20) for _ in range(3)]
    rows += [mkrow_raw(prompt=9000, hit=0, miss=0)]
    rows += [mkrow_raw(prompt=1000, hit=900, miss=100, changed=True) for _ in range(2)]
    a = T.audit(rows, [], T.collections.Counter())
    assert a["miss_rate_stable"] == pytest.approx(0.5)           # 60 / 12000，被稀释
    assert a["miss_rate_stable_reported"] == pytest.approx(2.0)  # 60 / 3000
    assert a["miss_rate_changed_reported"] == pytest.approx(10.0)
    assert a["extra_miss_tokens"] == 160


# ---------- 集中度 ----------

def test_集中度与零调用统计():
    calls = T.collections.Counter({"shell_run": 90, "code_read": 8, "code_edit": 2})
    trace = [{"tag": "run", "names": ["shell_run", "code_read", "never_a", "never_b"]}]
    a = T.audit([mkrow()], trace, calls)
    assert a["calls_total"] == 100 and a["calls_distinct"] == 3
    assert a["top1_share"] == 90.0
    assert a["top5_share"] == 100.0
    assert a["zero_used"] == ["never_a", "never_b"]
    assert a["zero_used_count"] == 2


def test_零调用工具名来自trace而非调用记录():
    """从未被调用的工具只可能出现在 tools_trace 的工具集快照里。"""
    trace = [{"tag": "run", "names": ["a", "b", "c"]}]
    a = T.audit([mkrow()], trace, T.collections.Counter({"a": 1}))
    assert sorted(a["zero_used"]) == ["b", "c"]


# ---------- 渲染 ----------

def test_render含关键结论行():
    rows = [mkrow(prompt=1000, miss=20, llm=100, tools_chars=400) for _ in range(3)]
    rows += [mkrow(prompt=1000, miss=200, llm=100, tools_chars=50, changed=True)
             for _ in range(2)]
    out = T.render(T.audit(rows, [], T.collections.Counter({"shell_run": 5})))
    assert "工具定义成本审计" in out
    assert "按需加载保持现状" in out
    assert "收益/代价" in out


def test_render在零调用时不除零():
    out = T.render(T.audit([mkrow()], [], T.collections.Counter()))
    assert "共 0 次" in out


def test_json输出可序列化():
    a = T.audit([mkrow()], [{"tag": "run", "names": ["x"]}],
                T.collections.Counter({"x": 1}))
    assert json.loads(json.dumps(a, ensure_ascii=False))["turns"] == 1
