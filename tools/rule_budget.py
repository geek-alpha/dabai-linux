#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""规则区预算门禁（只读；红 = 超额或卫生不合格）。

为什么要有它：规则区每轮都注入 prompt，是纯成本；但「加一条/删哪条」一直靠
感觉。deepseek-harness 的做法是把软要求变成硬门禁——给常设文档设字数上限，
超额就红，并写死处理顺序（先搬迁 → 再压缩 → 最后才提上限）。本脚本把同一套
搬到我们的规则区，顺带把他们的 slop checklist 里能在规则区上跑的那几条做成
检查项。

口径（这是本脚本存在的第二个理由）：
  实测发现两个工具各管一半、都自称「规则区」，谁都没看到全貌——
    · tools/status.py 锚点【工作准则…】→「shell 输出不许用」  行 4969~5112
    · tools/prompt_rules_audit.py 只取 agent_rules 变量       行 4898~4940
  两段区间不重叠。真正的成本是两段之和，所以本脚本按「两段合计」立预算，
  并分别报用量；锚点漂移会被 ANCHOR_DRIFT 抓到。

何时红：
  1. 合计 live 字符 > target_chars（tools/rule_budgets.json）
  2. 单块 > block_max_chars，或块数 > max_blocks
  3. 命中 hard 级卫生判据（段落墙 / 状态标注）
  4. ANCHOR_DRIFT：除 prompt_rules_audit.py 外任何模块又定义了一份规则区锚点
  5. DUPLICATED_RULE：两块里有 20 字符以上的自然语言片段完全重复（命令前缀不算）

怎么查用量：
  python tools/rule_budget.py            # 判定 + 摘要，人读
  python tools/rule_budget.py --list     # 逐块字符数与余量明细
  python tools/rule_budget.py --json     # 机读（供测试断言）

怎么豁免：
  不给逐条豁免。超额只有三步：① 搬迁到该管的层（提示词/经验库/长期事业/信条，
  各回各家）→ ② 压缩 → ③ 提上限，且提额必须改 manifest 并在提交信息里写理由。

字符口径：live = 真实注入 prompt 的字符数（字面量里的 \\n 已还原）；raw =
源码字面量原样长度（status.py 报的是这个，两者差的就是转义符）。
"""
from __future__ import annotations

import ast
import importlib.util
import json
import re
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
HERE = Path(__file__).resolve().parent
AGENT_PY = BASE / "agent.py"
MANIFEST = HERE / "rule_budgets.json"
AUDIT_PY = HERE / "prompt_rules_audit.py"

# 行为准则段锚点与字面量提取只在 prompt_rules_audit.py 定义一处，本文件复用；
# 别处再定义一份由 ANCHOR_DRIFT 报红。
BLOCK_HEAD = re.compile(r"【[^】\n]{2,40}】")

# 卫生判据阈值
EMPHASIS_DENSITY = 1 / 300      # 强调标记（** / ⚠ / 「硬规则」）字符密度上限
EMPHASIS_BLOCK_RATIO = 0.25     # 带强调标记的块占比上限——遍地都是等于没强调
COVERAGE_MIN = 0.60             # 命中率审计覆盖到的字符占比下限
DUP_MIN_CHARS = 20              # 跨块重复片段的最小长度——短于此的是术语/标识符，不是重复规则
# 含代码标记的片段不算重复：`venv/bin/python tools/` 这类命令前缀在多个块里出现
# 是格式，不是「同一事实两个家」（每个块调的是不同工具）
DUP_CODEISH = re.compile(r"[`/\\]|\.py\b|_[a-z]")

_HISTORY = re.compile(r"以前|曾经|原本是|原来是|历史上|之前我们|早先")
_TRANSCRIPT = re.compile(r"因为.{0,30}所以|背景（|当时的想法|我考虑过")
_STATUS_ROT = re.compile(r"待实现|尚未支持|未来将|即将支持|TODO|FIXME")


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"加载失败：{path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def load_budget() -> dict:
    data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    for k in ("target_chars", "headroom_ratio", "block_max_chars", "max_blocks"):
        if k not in data:
            raise SystemExit(f"rule_budgets.json 缺字段 {k}")
    return data


def work_rules() -> dict:
    """行为准则段：复用审计侧的锚点与提取器（口径只允许一处）。"""
    try:
        mod = _load(AUDIT_PY, "_rule_budget_work")
        w = mod.extract_work_rules()
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "raw": 0, "live": "", "lines": None, "error": str(e)}
    return w


def agent_rules() -> dict:
    """工具硬规则段：复用 prompt_rules_audit 的提取器，避免第二份口径。"""
    try:
        mod = _load(AUDIT_PY, "_rule_budget_audit")
        rules, text = mod.extract_rules()
    except SystemExit as e:
        return {"ok": False, "raw": 0, "live": "", "rules": [], "error": str(e)}
    return {"ok": True, "raw": len(text), "live": text, "rules": rules}


def split_blocks(live: str) -> list[tuple[str, int, str]]:
    parts = [p.strip() for p in re.split(r"(?=【[^】\n]{2,40}】)", live) if p.strip()]
    out = []
    for p in parts:
        m = BLOCK_HEAD.match(p)
        out.append((m.group(0)[1:-1] if m else "(无标题)", len(p), p))
    return out


def second_source(py_dir: Path | None = None) -> list[str]:
    """锚点常量只允许在 prompt_rules_audit.py 定义一处，别处出现就是第二事实源复活。

    2026-09-22 的实况：status.py 数出 raw 3584 / 17 条、prompt_rules_audit 数出
    1432 / 11 条，两个工具各管一半却都叫「规则区」（两段区间不重叠）。口径分裂
    不会报错，只会让两个数字都失去意义——所以改成扫 tools/ 下所有模块级定义。
    """
    out = []
    for py in sorted((py_dir or HERE).glob("*.py")):
        if py.name == AUDIT_PY.name:
            continue
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError, OSError):
            continue
        for node in tree.body:
            if not isinstance(node, ast.Assign):
                continue
            for t in node.targets:
                name = getattr(t, "id", "")
                if name in ("RULE_START", "RULE_END"):
                    out.append(f"{py.name}:{node.lineno} 又定义了一份 {name}")
    return out


def audit_coverage(live_total: int) -> dict:
    """命中率审计覆盖了多少字符——覆盖不到的规则等于没人审。

    两段都要算：只算 agent_rules 段时，行为准则段 3539 字符在分母里、不在分子里，
    覆盖率一直报 28%——看起来像审计偷懒，实际是它根本没读那一段。
    """
    try:
        mod = _load(AUDIT_PY, "_rule_budget_audit2")
        prefixes = [k for k, _kind, _m in mod.RULE_MAP]
        rules = agent_rules()["rules"]
        blocks = work_rules().get("blocks") or []
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e), "ratio": 0.0, "covered_chars": 0}
    # 规则文本以「⚠ 」开头而映射 key 不带前缀——与 prompt_rules_audit 同款 normalize，
    # 少了它匹配率会假跌到 1%（实测踩过）
    keys = [(r.lstrip("⚠").strip(), len(r)) for r in rules]
    keys += [(title, len(body)) for title, body in blocks]
    covered = sum(n for k, n in keys if any(k.startswith(p) for p in prefixes))
    return {
        "ok": True,
        "map_size": len(prefixes),
        "covered_chars": covered,
        "ratio": covered / live_total if live_total else 0.0,
    }


def dup_across_blocks(blocks: list[tuple[str, int, str]]) -> list[tuple[str, str, str]]:
    """找出被两块以上完整重复的长片段（同一事实两个家）。

    不用「标识符出现次数」当判据：同一工具在「何时用它」和「不确定怎么用就查
    技能说明」两个语境里各出现一次是正常的，按标识符判会永远误报——实测
    delegate_agent_task 就是这么被误报的（两处说的是不同的事）。
    """
    seen_idx: dict[str, set[int]] = {}
    for idx, (_n, _c, body) in enumerate(blocks):
        for i in range(max(len(body) - DUP_MIN_CHARS + 1, 0)):
            gram = body[i:i + DUP_MIN_CHARS]
            if DUP_CODEISH.search(gram):
                continue
            seen_idx.setdefault(gram, set()).add(idx)
    out, done = [], set()
    for gram, idxs in seen_idx.items():
        if len(idxs) < 2:
            continue
        pair = tuple(sorted(idxs))[:2]
        if pair in done:
            continue
        done.add(pair)
        out.append((blocks[pair[0]][0], blocks[pair[1]][0], gram))
    return out


def slop(blocks: list[tuple[str, int, str]], budget: dict) -> list[dict]:
    """deepseek-harness 的 slop checklist 里能在规则区上跑的部分。"""
    out = []
    live_all = "".join(b[2] for b in blocks)

    emph = live_all.count("**") // 2 + live_all.count("⚠") + live_all.count("硬规则")
    dens = emph / max(len(live_all), 1)
    if dens > EMPHASIS_DENSITY:
        out.append({
            "level": "warn", "code": "EMPHASIS_INFLATION",
            "msg": f"强调标记 {emph} 个 / {len(live_all)} 字符（密度 1/{len(live_all)/max(emph,1):.0f}，"
                   f"阈值 1/{int(1/EMPHASIS_DENSITY)}）——只留改变行为的那条",
        })
    marked = sum(1 for _n, _c, b in blocks if "⚠" in b or "硬规则" in b or "**" in b)
    ratio = marked / max(len(blocks), 1)
    if ratio > EMPHASIS_BLOCK_RATIO:
        out.append({
            "level": "warn", "code": "EMPHASIS_INFLATION",
            "msg": f"{marked}/{len(blocks)} 块带强调标记（{ratio*100:.0f}%，上限 "
                   f"{EMPHASIS_BLOCK_RATIO*100:.0f}%）——遍地都是等于没强调，"
                   "只留真正改变行为的那几块",
        })

    for name, n, _ in blocks:
        if n > budget["block_max_chars"]:
            out.append({
                "level": "hard", "code": "PARAGRAPH_WALL",
                "msg": f"「{name}」{n} 字符 > 单块上限 {budget['block_max_chars']}——拆开或把细节下沉到它的家",
            })

    dups = dup_across_blocks(blocks)
    if dups:
        detail = "；".join(f"「{a}」×「{b}」{g!r}" for a, b, g in dups[:3])
        out.append({
            "level": "warn", "code": "DUPLICATED_RULE",
            "msg": f"{len(dups)} 处跨块重复——一个事实只留一个家：{detail}",
        })

    for code, rx, why in (
        ("HISTORY_BLEED", _HISTORY, "规则里写历史，读者要的是当前事实"),
        ("REASONING_TRANSCRIPT", _TRANSCRIPT, "规则里写推理过程，留结论就够"),
        ("STATUS_ROT", _STATUS_ROT, "状态标注会腐烂，仓库现状才是权威"),
    ):
        for name, _n, body in blocks:
            m = rx.search(body)
            if m:
                out.append({
                    "level": "hard" if code == "STATUS_ROT" else "warn",
                    "code": code,
                    "msg": f"「{name}」命中 {m.group(0)!r}——{why}",
                })
                break
    return out


def build() -> dict:
    budget = load_budget()
    work = work_rules()
    agent = agent_rules()

    blocks: list[tuple[str, int, str]] = []
    if agent["ok"]:
        for r in agent["rules"]:
            blocks.append((r[:24], len(r), r))
    if work["ok"]:
        blocks.extend(split_blocks(work["live"]))

    total = (len(agent["live"]) if agent["ok"] else 0) + (len(work["live"]) if work["ok"] else 0)
    target = budget["target_chars"]
    healthy = int(target * (1 - budget["headroom_ratio"]))

    violations = slop(blocks, budget)
    dup_src = second_source()
    if dup_src:
        violations.append({"level": "hard", "code": "SECOND_SOURCE",
                           "msg": "第二事实源复活：" + "；".join(dup_src)})
    if not work["ok"]:
        violations.append({"level": "hard", "code": "ANCHOR_MISS",
                           "msg": f"行为准则段锚点没命中（{RULE_START!r} → {RULE_END!r}），统计不可信"})
    if not agent["ok"]:
        violations.append({"level": "hard", "code": "AGENT_RULES_MISS",
                           "msg": "agent_rules 提取失败：" + str(agent.get("error"))})
    if len(blocks) > budget["max_blocks"]:
        violations.append({"level": "hard", "code": "TOO_MANY_BLOCKS",
                           "msg": f"{len(blocks)} 块 > 上限 {budget['max_blocks']}"})

    # 覆盖率检查必须排在定级之前：它是 warn，而 green 的语义是「零 violation」——
    # 先定级再追加警告，报告会自相矛盾（列着警告却标绿）
    cov = audit_coverage(total)
    if cov.get("ok") and cov["ratio"] < COVERAGE_MIN:
        violations.append({
            "level": "warn", "code": "COVERAGE_GAP",
            "msg": f"命中率审计只覆盖 {cov['covered_chars']} / {total} 字符"
                   f"（{cov['ratio']*100:.0f}%，下限 {COVERAGE_MIN*100:.0f}%）——"
                   "没被覆盖的规则没人审，等于免税区",
        })

    hard = [v for v in violations if v["level"] == "hard"]
    if hard or total > target:
        level = "red"
    elif total > healthy or violations:
        level = "yellow"
    else:
        level = "green"

    return {
        "ok": level != "red",
        "level": level,
        "total_chars": total,
        "target": target,
        "healthy": healthy,
        "over_target": max(total - target, 0),
        "over_healthy": max(total - healthy, 0),
        "to_green": max(total - healthy, 0),
        "segments": {
            "agent_rules": {"live": len(agent["live"]) if agent["ok"] else 0,
                            "raw": agent["raw"], "count": len(agent.get("rules") or [])},
            "work_rules": {"live": len(work["live"]) if work["ok"] else 0,
                           "raw": work["raw"], "lines": work["lines"],
                           "blocks": len(split_blocks(work["live"])) if work["ok"] else 0},
        },
        "blocks": [{"name": n, "chars": c} for n, c, _ in sorted(blocks, key=lambda x: -x[1])],
        "block_count": len(blocks),
        "violations": violations,
        "coverage": cov,
        "budget": budget,
    }


def render(rep: dict) -> str:
    o = []
    o.append("规则区预算门禁（只读）")
    o.append("=" * 68)
    icon = {"green": "GREEN", "yellow": "YELLOW", "red": "RED"}[rep["level"]]
    o.append(f"[{icon}] 合计 {rep['total_chars']} 字符 / {rep['block_count']} 块"
             f"（target {rep['target']}，healthy {rep['healthy']}）")
    s = rep["segments"]
    o.append(f"  agent_rules 段（工具硬规则）  {s['agent_rules']['live']:>5} 字符 / "
             f"{s['agent_rules']['count']} 条")
    o.append(f"  行为准则段（{s['work_rules']['lines']}）      {s['work_rules']['live']:>5} 字符 / "
             f"{s['work_rules']['blocks']} 块  （raw {s['work_rules']['raw']}，差的是转义符）")
    if rep["over_target"]:
        o.append(f"  超 target {rep['over_target']} 字符——冻结天花板，先搬迁或压缩")
    elif rep["over_healthy"]:
        o.append(f"  超 healthy {rep['over_healthy']} 字符（仍在 target 内）——余量不足 5%，该瘦身了")
    else:
        o.append("  余量充足")
    cov = rep["coverage"]
    if cov.get("ok"):
        o.append(f"  进审计流程 {cov['covered_chars']} / {rep['total_chars']} 字符"
                 f"（{cov['ratio']*100:.0f}%）；其中多少条真有数据源能回答，"
                 "见 tools/prompt_rules_audit.py（可量化率只由审计报，不在这里再算一遍）")
    o.append("")
    o.append("最大 8 块")
    o.append("-" * 68)
    for b in rep["blocks"][:8]:
        o.append(f"  {b['chars']:>5}  {b['name']}")
    if rep["violations"]:
        o.append("")
        o.append("卫生判据")
        o.append("-" * 68)
        for v in rep["violations"]:
            o.append(f"  [{v['level'].upper():4}] {v['code']}: {v['msg']}")
    else:
        o.append("")
        o.append("卫生判据：全部通过")
    if rep["level"] != "green":
        o.append("")
        o.append("超额处理顺序（写死，不许跳过）")
        o.append("-" * 68)
        o.append("  ① 搬迁：这条内容属于哪一层？（提示词 / 经验库 / 长期事业 / 信条）搬过去。")
        o.append("  ② 压缩：确实属于规则区的，还能更短吗？")
        o.append("  ③ 提上限：只有前两步都做完才允许，且改 tools/rule_budgets.json 时"
                 "必须在提交信息里写理由。")
        if rep["to_green"]:
            o.append(f"  当前要回到 healthy，需减 {rep['to_green']} 字符。")
    return "\n".join(o)


def render_list(rep: dict) -> str:
    o = [f"规则区用量明细（合计 {rep['total_chars']} / target {rep['target']}）", "-" * 68]
    for b in rep["blocks"]:
        o.append(f"  {b['chars']:>5}  {b['name']}")
    return "\n".join(o)


def main() -> int:
    rep = build()
    if "--json" in sys.argv:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
    elif "--list" in sys.argv:
        print(render_list(rep))
    else:
        print(render(rep))
    return 0 if rep["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
