#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""规则区预算门禁的契约测试：预算要真能拦、口径不许再分裂。

背景（2026-09-22）：规则区每轮注入 prompt，是纯成本，但「加一条/删哪条」一直
靠感觉。照 deepseek-harness 的做法给它立预算 + 硬门禁时，实测发现两个工具各管
一半、都自称「规则区」：
  · tools/status.py 锚点【工作准则…】→「shell 输出不许用」  行 4969~5112
  · tools/prompt_rules_audit.py 只取 agent_rules 变量       行 4898~4940
两段区间不重叠，谁都没看到全貌。本工具的合计口径就是为修这个而立的。

契约：
  1. manifest 字段齐全，healthy < target（5% 余量规矩）
  2. 合计 = agent_rules 段 + 行为准则段，两段都必须计入（少一段 = 口径分裂复发）
  3. 合计超 target → RED 且退出码 1；范围内 → 退出码 0
  4. 单块超 block_max_chars → PARAGRAPH_WALL（hard → RED）
  5. 状态标注（待实现/TODO…）→ STATUS_ROT（hard → RED）
  6. status.py 重新自己数规则区 → SECOND_SOURCE（hard → RED），口径分裂不许静默复发
  7. 覆盖率匹配必须 normalize 掉「⚠ 」前缀——少了它匹配率假跌到 1%（实测踩过）
  8. --json 输出可被 json.loads 解析

这些用例全部造数据；只有 manifest 与覆盖率两条读真实文件。
"""
import importlib.util
import json
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]

BUDGET = {
    "target_chars": 1000,
    "headroom_ratio": 0.05,
    "block_max_chars": 100,
    "max_blocks": 10,
}


def _load():
    """tools/ 不是包，按文件路径加载。每次拿干净模块，避免用例互相污染。"""
    path = BASE / "tools" / "rule_budget.py"
    spec = importlib.util.spec_from_file_location("rule_budget_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _patch(mod, monkeypatch, work_live="", agent_live="", budget=None, dup=None):
    monkeypatch.setattr(mod, "load_budget", lambda: dict(budget or BUDGET))
    monkeypatch.setattr(mod, "work_rules", lambda: {
        "ok": True, "raw": len(work_live), "live": work_live, "lines": [1, 2], "lits": 1})
    monkeypatch.setattr(mod, "agent_rules", lambda: {
        "ok": True, "raw": len(agent_live), "live": agent_live,
        "rules": [agent_live] if agent_live else []})
    monkeypatch.setattr(mod, "second_source", lambda: dup or [])
    monkeypatch.setattr(mod, "audit_coverage",
                        lambda total: {"ok": True, "map_size": 1,
                                       "covered_chars": total, "ratio": 1.0})


# ---------- 契约 1：manifest ----------

def test_manifest_字段齐全且_healthy_低于_target():
    """真实 manifest 必须能解析，且余量比例在 (0,1) 之间。"""
    mod = _load()
    b = mod.load_budget()
    assert b["target_chars"] > 0
    assert 0 < b["headroom_ratio"] < 1
    assert b["block_max_chars"] > 0
    assert b["max_blocks"] > 0


# ---------- 契约 2：两段口径 ----------

def test_合计必须包含两段(monkeypatch):
    """agent_rules 段 100 + 行为准则段 200 → 合计 300，两段各自报用量。"""
    mod = _load()
    _patch(mod, monkeypatch, work_live="x" * 200, agent_live="y" * 100)
    rep = mod.build()
    assert rep["total_chars"] == 300
    assert rep["segments"]["agent_rules"]["live"] == 100
    assert rep["segments"]["work_rules"]["live"] == 200


# ---------- 契约 3：超额就红 ----------

def test_超_target_变红且退出码1(monkeypatch, capsys):
    mod = _load()
    _patch(mod, monkeypatch, work_live="x" * 1100)
    monkeypatch.setattr(mod.sys, "argv", ["rule_budget.py"])
    assert mod.build()["level"] == "red"
    assert mod.main() == 1
    assert "RED" in capsys.readouterr().out


def test_范围内退出码0(monkeypatch, capsys):
    mod = _load()
    _patch(mod, monkeypatch, work_live="【规则】" + "短" * 20)
    monkeypatch.setattr(mod.sys, "argv", ["rule_budget.py"])
    assert mod.build()["level"] in ("green", "yellow")
    assert mod.main() == 0


def test_超_healthy_但未超_target_是黄(monkeypatch):
    """合计落在 (healthy, target] → 黄：余量不足，但还没欠债。"""
    mod = _load()
    # 每块 95 字符 × 10 块 = 959，落在 (healthy 950, target 1000] 内
    _patch(mod, monkeypatch,
           work_live="\n".join(f"【块{i}】" + "字" * 91 for i in range(10)))
    rep = mod.build()
    assert rep["level"] == "yellow"
    assert rep["over_healthy"] > 0
    assert rep["over_target"] == 0


# ---------- 契约 4/5/6：hard 判据 ----------

def test_单块超上限报段落墙(monkeypatch):
    mod = _load()
    _patch(mod, monkeypatch, work_live="【巨块】" + "字" * 120)
    codes = [v["code"] for v in mod.build()["violations"]]
    assert "PARAGRAPH_WALL" in codes
    assert mod.build()["level"] == "red"


def test_状态标注报_STATUS_ROT(monkeypatch):
    mod = _load()
    _patch(mod, monkeypatch, work_live="【某规则】待实现的功能，别指望。")
    codes = [v["code"] for v in mod.build()["violations"]]
    assert "STATUS_ROT" in codes
    assert mod.build()["level"] == "red"


def test_第二事实源复活就报红(monkeypatch):
    """status.py 又自己数规则区 → 必须红，口径分裂不许静默复发。"""
    mod = _load()
    _patch(mod, monkeypatch, work_live="【规则】短", dup=["status.py 又自己数规则区"])
    codes = [v["code"] for v in mod.build()["violations"]]
    assert "SECOND_SOURCE" in codes
    assert mod.build()["level"] == "red"


def test_second_source_能认出锚点常量(tmp_path):
    """守卫本身要有效：假模块里出现锚点常量就必须被点名。"""
    mod = _load()
    (tmp_path / "status.py").write_text(
        'RULE_START = "【工作准则（任何模式下"\n', encoding="utf-8")
    hits = mod.second_source(tmp_path)
    assert hits and "RULE_START" in hits[0] and "status.py" in hits[0]


def test_second_source_不误报引用(tmp_path):
    """注释和函数体里引用锚点不算第二份定义——守卫只认模块级赋值。"""
    mod = _load()
    (tmp_path / "reader.py").write_text(
        'def f():\n    return audit.RULE_START\n\n# 见 audit.RULE_START\n',
        encoding="utf-8")
    assert mod.second_source(tmp_path) == []


def test_真实_tools_只有一处锚点定义():
    """tools/ 下不许再有第二份锚点常量——口径只留 prompt_rules_audit 一处。"""
    mod = _load()
    assert mod.second_source() == []


# ---------- 契约 7：覆盖率 normalize ----------

def test_覆盖率匹配忽略警告前缀(monkeypatch):
    """带「⚠ 」前缀的规则也要算被覆盖——少了 normalize 会假跌到 1%（实测踩过）。"""
    mod = _load()
    real_rules, _text = mod._load(mod.AUDIT_PY, "_rb_audit_probe").extract_rules()
    fake = ["⚠ " + r.lstrip("⚠ ").strip() for r in real_rules]
    monkeypatch.setattr(mod, "agent_rules", lambda: {
        "ok": True, "raw": 0, "live": "", "rules": fake})
    # 分母只放 agent_rules 段，准则段清空——否则分子会超出分母，测的就不是 normalize 了
    monkeypatch.setattr(mod, "work_rules",
                        lambda: {"ok": True, "raw": 0, "live": "", "lines": None,
                                 "blocks": []})
    cov = mod.audit_coverage(sum(len(r) for r in fake))
    assert cov["ok"] is True
    assert cov["ratio"] > 0.9, f"normalize 失效，覆盖率假跌到 {cov['ratio']:.0%}"


def test_跨块重复只认真实句子不认命令前缀():
    """同一命令前缀在多个块里出现是格式，不是「同一事实两个家」。"""
    mod = _load()
    assert mod.dup_across_blocks([
        ("甲", 0, "先跑 `venv/bin/python tools/x.py list` 看用量"),
        ("乙", 0, "再跑 `venv/bin/python tools/y.py log` 写账")]) == []
    hits = mod.dup_across_blocks([
        ("甲", 0, "动手前先列出将要删除的清单（路径+原因）让用户确认"),
        ("乙", 0, "动手前先列出将要删除的清单（路径+原因）让用户确认")])
    assert hits and hits[0][0] == "甲"


def test_跨块重复不报同一工具在不同语境被提到():
    """标识符出现两次不等于重复——按标识符判会永远误报（实测踩过）。"""
    mod = _load()
    assert mod.dup_across_blocks([
        ("甲", 0, "委派任务用 delegate_agent_task（进展展示在右侧任务中心）"),
        ("乙", 0, "只有大型多步骤任务才后台化或委派 delegate_agent_task")]) == []


def test_覆盖率两段都算(monkeypatch):
    """只算 agent_rules 段时，行为准则段在分母里、不在分子里，覆盖率会假跌。"""
    mod = _load()
    body = "【摸清大项目】" + "y" * 100
    monkeypatch.setattr(mod, "agent_rules", lambda: {
        "ok": True, "raw": 0, "live": "", "rules": []})
    monkeypatch.setattr(mod, "work_rules",
                        lambda: {"ok": True, "raw": 0, "live": "", "lines": None,
                                 "blocks": [("摸清大项目", body)]})
    cov = mod.audit_coverage(len(body))
    assert cov["covered_chars"] == len(body)
    assert cov["ratio"] == 1.0


def test_覆盖率漏报是_warn_不是_hard(monkeypatch):
    """覆盖率不足只警告不拦——它是欠债提示，不是新规则写错。"""
    mod = _load()
    _patch(mod, monkeypatch, work_live="【规则】短")
    monkeypatch.setattr(mod, "audit_coverage",
                        lambda total: {"ok": True, "map_size": 1,
                                       "covered_chars": 0, "ratio": 0.0})
    rep = mod.build()
    covs = [v for v in rep["violations"] if v["code"] == "COVERAGE_GAP"]
    assert covs and covs[0]["level"] == "warn"
    assert rep["level"] == "yellow"


# ---------- 契约 8：机读 ----------

def test_json_可解析(monkeypatch, capsys):
    mod = _load()
    _patch(mod, monkeypatch, work_live="【规则】短")
    monkeypatch.setattr(mod.sys, "argv", ["rule_budget.py", "--json"])
    mod.main()
    rep = json.loads(capsys.readouterr().out)
    assert rep["total_chars"] > 0
    assert "blocks" in rep and "violations" in rep


def test_list_模式列逐块用量(monkeypatch, capsys):
    mod = _load()
    _patch(mod, monkeypatch, work_live="【规则甲】a\n【规则乙】b")
    monkeypatch.setattr(mod.sys, "argv", ["rule_budget.py", "--list"])
    mod.main()
    out = capsys.readouterr().out
    assert "甲" in out and "乙" in out
