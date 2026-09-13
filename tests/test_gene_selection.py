#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""基因选择压力：注入窗口按「新近位 + 最少曝光优先」轮换，不按曝光降序。

背景（2026-09-14）：埋点通电后拿到第一组真实数据——60 条教训里只有 6 条进过 prompt。
下一步本该「按命中降序取窗口」，但那是个假信号：inject 记的是曝光次数（自变量），
不是价值（因变量）。按曝光降序排，已在窗口的 6 条会永久霸占窗口，窗口外 54 条永远
拿不到验证机会——富者愈富，测出来的 fitness 只是自己上一轮的曝光。

契约（四条）：
  1. 前 fresh 条无条件保新近性（刚踩的坑必须立刻能用）
  2. 其余按曝光升序补足：零曝光的先进窗口
  3. 每轮注入 +1 后下一轮窗口自然换成别的未验证基因（自动轮换，无需状态）
  4. 报表口径与注入口径一致：gene_fitness.build() 的 in_window 必须等于
     agent._gene_pick 的选取结果——两处各算一遍必然漂移
"""
import importlib.util
import sys
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

import agent  # noqa: E402


def _load_gf(stats_path):
    """加载 tools/gene_fitness.py 并把埋点文件指到 tmp，避免污染真实台账。"""
    spec = importlib.util.spec_from_file_location("gf_under_test", BASE / "tools" / "gene_fitness.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.STATS = stats_path
    # 曝光流水也要隔离：报表一旦读到生产流水，就会用真实那一轮的基因去标测试的假基因，
    # 「在场」全空——测试会变成「有没有跑过真实对话」的函数。
    mod.EXPOSURE = Path(stats_path).parent / "gene_exposure.jsonl"
    return mod


def _bump(gf, picked):
    """模拟一轮注入：被选中的基因曝光 +1。"""
    stats = gf.load_stats()
    for x in picked:
        k = gf.key_of(x)
        e = stats.get(k) or {"inject": 0}
        e["inject"] = int(e.get("inject", 0)) + 1
        stats[k] = e
    gf.save_stats(stats)


def test_order_by_exposure_ascending_and_stable(tmp_path):
    gf = _load_gf(tmp_path / "stats.json")
    gf.save_stats({
        gf.key_of("a"): {"inject": 3},
        gf.key_of("b"): {"inject": 1},
        # c 未埋点，视为 0 次
    })
    assert gf.order_by_exposure(["a", "b", "c"]) == ["c", "b", "a"]
    # 同曝光保持传入顺序（= 新近优先），不因排序打乱
    assert gf.order_by_exposure(["x", "y"]) == ["x", "y"]


def test_gene_pick_keeps_fresh_head(monkeypatch, tmp_path):
    gf = _load_gf(tmp_path / "stats.json")
    monkeypatch.setattr(agent, "_gene_mod", lambda: gf)
    items = [f"L{i}" for i in range(20)]
    pick = agent._gene_pick(items, 6)
    assert pick[:2] == ["L0", "L1"], "新近位必须无条件保留"
    assert len(pick) == 6


def test_pick_prefers_unexposed(monkeypatch, tmp_path):
    """核心回归：上一轮在场的基因要让位给零曝光的，而不是靠曝光降序自我强化。"""
    gf = _load_gf(tmp_path / "stats.json")
    monkeypatch.setattr(agent, "_gene_mod", lambda: gf)
    items = [f"L{i}" for i in range(20)]
    gf.save_stats({gf.key_of(x): {"inject": 1} for x in items[:6]})
    pick = agent._gene_pick(items, 6)
    assert pick[:2] == ["L0", "L1"]
    assert "L6" in pick and "L7" in pick, "零曝光的必须顶进窗口"
    assert "L5" not in pick, "上轮在场且非新近位的必须被换下"


def test_rotation_advances_each_round(monkeypatch, tmp_path):
    gf = _load_gf(tmp_path / "stats.json")
    monkeypatch.setattr(agent, "_gene_mod", lambda: gf)
    items = [f"L{i}" for i in range(20)]
    gf.save_stats({})
    rounds = []
    for _ in range(3):
        pick = agent._gene_pick(items, 6)
        rounds.append(pick)
        _bump(gf, pick)
    assert rounds[0] == ["L0", "L1", "L2", "L3", "L4", "L5"], "首轮全是 0 曝光，按新近序"
    assert rounds[1][:2] == ["L0", "L1"] and "L6" in rounds[1]
    assert set(rounds[0]) != set(rounds[1]), "窗口必须真的轮换"
    assert "L10" in rounds[2], "连续三轮应推进到更后面的未验证基因"


def test_report_window_matches_pick(monkeypatch, tmp_path):
    """报表的『在场』口径必须等于注入端选取——两处各算一遍必然漂移。"""
    gf = _load_gf(tmp_path / "stats.json")
    monkeypatch.setattr(agent, "_gene_mod", lambda: gf)
    items = [f"L{i}" for i in range(20)]
    gf.save_stats({gf.key_of(x): {"inject": 1} for x in items[:6]})
    monkeypatch.setattr(gf, "caps", lambda: {"lesson": 6, "conviction": 0, "longterm": 0})
    monkeypatch.setattr(gf, "fresh_default", lambda: 2)
    monkeypatch.setattr(gf, "read_genes",
                        lambda: [{"kind": "lesson", "order": i, "text": t} for i, t in enumerate(items)])
    _cp, _stats, built = gf.build()
    in_win = [r["text"] for r in built if r["in_window"]]
    assert in_win == agent._gene_pick(items, 6)


def test_pick_falls_back_when_module_missing(monkeypatch):
    """埋点模块加载失败时退回新近序，不许抛异常打断注入。"""
    monkeypatch.setattr(agent, "_gene_mod", lambda: None)
    items = [f"L{i}" for i in range(10)]
    assert agent._gene_pick(items, 4) == ["L0", "L1", "L2", "L3"]


def test_holdout_rotates_cover_all(monkeypatch):
    """恒在场基因必须轮流缺席：n=3 连跑 3 轮，每轮缺 1 条且三轮覆盖全部。

    这是可辨识性的唯一来源：只要某条基因每轮都在场，它的效应就永远估不出。
    """
    items = ["A", "B", "C"]
    rounds = []
    for seq in range(3):
        monkeypatch.setattr(agent, "_GENE_HOLDOUT_SEQ", seq)
        rounds.append(agent._gene_holdout(items, 3))
    assert [len(r) for r in rounds] == [2, 2, 2], "每轮恰好留空 1 条"
    absent = [set(items) - set(r) for r in rounds]
    assert all(len(a) == 1 for a in absent), "每轮只能缺席 1 条"
    assert len(set.union(*absent)) == 3, "三轮必须覆盖全部候选，否则有的基因永远估不出"


def test_holdout_stable_within_round(monkeypatch):
    """轮内多次构建 prompt 必须一致——seq 只在轮末递增。"""
    monkeypatch.setattr(agent, "_GENE_HOLDOUT_SEQ", 7)
    a = agent._gene_holdout(["A", "B", "C"], 3)
    b = agent._gene_holdout(["A", "B", "C"], 3)
    assert a == b


def test_holdout_single_candidate_never_drops(monkeypatch):
    """只剩一条时不留空：整段消失是故障，不是对照组。"""
    monkeypatch.setattr(agent, "_GENE_HOLDOUT_SEQ", 5)
    assert agent._gene_holdout(["only"], 3) == ["only"]
    assert agent._gene_holdout([], 3) == []


def test_round_reset_advances_holdout(monkeypatch):
    """轮末 reset 必须推进 seq——不推进就是每轮留空同一条，等于没随机化。"""
    monkeypatch.setattr(agent, "_GENE_HOLDOUT_SEQ", 100)
    agent._GENE_ROUND_KEYS.clear()
    agent._gene_round_reset()
    assert agent._GENE_HOLDOUT_SEQ == 101


def test_longterm_block_injects_what_it_logs(monkeypatch):
    """注入几行就埋几个键——报表的『在场』全靠这个等式，少一个就是假数据。"""
    got = []
    monkeypatch.setattr(agent, "_gene_touch", lambda pairs: got.append(pairs))
    block = agent._harness_longterm_block(3)
    if not got:
        pytest.skip("无 active 项目，注入段为空")
    n_lines = sum(1 for l in block.split("\n") if l.startswith("▸ "))
    assert n_lines == len(got[0]), "注入行数必须等于埋点键数"


def test_conviction_block_injects_what_it_logs(monkeypatch):
    got = []
    monkeypatch.setattr(agent, "_gene_touch", lambda pairs: got.append(pairs))
    block = agent._harness_conviction_block(4)
    if not got:
        pytest.skip("无信条，注入段为空")
    n_lines = sum(1 for l in block.split("\n") if l.startswith("- "))
    assert n_lines >= len(got[0]), "埋点的信条必须都真的注入了"
