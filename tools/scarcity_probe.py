#!/usr/bin/env python3
"""平台体检器：这个平台的钱，是真的吗？

第一性原理——任何宣称「能赚钱」的平台，可被独立验证的只有两组量：

  claimed   名义额：挂着多少（available / open / reward）
  settled   实付额：真付过多少（paid out / settled / distributed）

  realness = settled / claimed

其余都是叙事。名义额可以随便挂，实付额要过账、要留痕、要有人真收到钱。

取证分级（provenance tier）——这一层是这个工具和「爬个数字」的全部区别：

  T1  结构化 + 单位显式：内嵌 JSON 里的 {value, unit} 对（opire 的 homeKPIs
      就是这个形状）。数值是整数、单位单独声明、不经过渲染。可直接采信。
  T2  结构化但单位不明：裸整数。opire 的 moneyPaidInBounties 若丢掉 unit，
      497587 会被读成 49 万美元，高估 100 倍。可进判决（折价率是比值，单位
      约掉），但报告要标出单位风险。
  T3  DOM 语义属性：如 frantic 的 data-town-settled-amt。语义在属性名里，
      但值可能是模板默认 $0、真值等 JS 异步填——值为 0 时直接排除，
      0 不等于「真的零」。
  T4  渲染文本：靠关键词邻近猜出来的数字。实测不可靠（数字与文案常分属不同
      DOM 节点，opire 的 $4,975.87 上下文关键词零命中），只做旁证。
  --  无口径：页面只有单笔金额、没有总额/实付字段 → UNVERIFIABLE。

硬规则：UNVERIFIABLE 和 UNRENDERED 都不是 PASS。没有证据不等于证据清白——
静默降级的检查比没有检查更危险。

  scarcity_probe.py probe <url> [...]   体检任意平台
  scarcity_probe.py baseline            已知平台基线（含已核实的真值）
  scarcity_probe.py selftest            用已知真值反证判决引擎

不需要账号、不需要 key，抓的都是公开页面。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
USER_AGENT = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/122 Safari/537.36")
TIMEOUT = 25

# 判决阈值。用已核实的真值校准过：opire 0.126% → NOISE，frantic 63.9% → REAL
NOISE_BELOW = 0.01
THIN_BELOW = 0.10
MARGINAL_BELOW = 0.50

UNIT_SCALE = {
    "USD": 1.0, "USDC": 1.0, "USDT": 1.0, "DOLLAR": 1.0, "DOLLARS": 1.0,
    "USD_CENT": 0.01, "USD_CENTS": 0.01, "CENT": 0.01, "CENTS": 0.01,
    "USD_MILLI": 0.001, "MILLI_USD": 0.001,
}

# paid/settled 比 bounty/reward 更具体，分类时优先——moneyPaidInBounties
# 同时含 paid 和 bount，它是实付口径
_SETTLED_RE = re.compile(r"paid|payout|settle|distribut|withdraw|earned", re.I)
_CLAIMED_RE = re.compile(r"available|open|outstanding|unclaimed|remaining|reward|bount|prize", re.I)
# 判「金额还是计数」：moneyPaidInBounties 是金额，bountiesPaid 是计数——
# 两者都含 paid，只能靠 money 词优先来分开
_MONEY_WORD_RE = re.compile(r"money|amount|usd|usdc|value|volume|sum|cents", re.I)
_COUNT_ONLY_RE = re.compile(r"count|number|bount(?:y|ies)|slots|issues|tasks", re.I)

# 模式 A：{value, unit} 显式声明单位。实测 opire 的 homeKPIs 就是这个形状，
# 且它在 HTML 里是转义过的（\"homeKPIs\"），但转义只加反斜杠、不动引号本身，
# 所以直接对原始 HTML 扫描即可命中，不必先 unescape
_A_VALUE_UNIT = re.compile(
    r'"([A-Za-z_][A-Za-z0-9_]*)"\s*:\s*\{\s*"value"\s*:\s*(-?\d+(?:\.\d+)?)'
    r'\s*,\s*"unit"\s*:\s*"([A-Za-z_]+)"\s*\}')
_A_UNIT_VALUE = re.compile(
    r'"([A-Za-z_][A-Za-z0-9_]*)"\s*:\s*\{\s*"unit"\s*:\s*"([A-Za-z_]+)"'
    r'\s*,\s*"value"\s*:\s*(-?\d+(?:\.\d+)?)\s*\}')
# 模式 B：裸整数，无单位声明。可能是计数，也可能是「分」为单位的金额——
# 无法区分，所以只做旁证
_B_BARE = re.compile(r'"([A-Za-z_][A-Za-z0-9_]*)"\s*:\s*(-?\d+(?:\.\d+)?)(?![\d.])')

# 模式 C：DOM 语义属性，如 data-town-settled-amt="$0"
# 属性名要留住：data-town-settled-amt 的语义全在名字里，值只是值
_C_ATTR = re.compile(
    r'data-([a-z0-9-]*(?:settled|paid|payout|claimed|available|reward|bount)[a-z0-9-]*)'
    r'\s*=\s*"([^"]*)"', re.I)
_ATTR_MONEY = re.compile(r"\$\s?([\d][\d,]*(?:\.\d+)?)")

# 模式 D：渲染文本里的金额 + 邻近关键词（旁证）
_D_MONEY = re.compile(r"\$\s?([\d][\d,]*(?:\.\d+)?)")
_TEXT_SETTLED = re.compile(r"paid\s*out|total\s*paid|paid\s*to|distributed|settled|withdrawn|payouts?", re.I)
_TEXT_CLAIMED = re.compile(r"available|open\s+value|total\s+value|reward|prize", re.I)


def _money(v: float, unit: str | None) -> float:
    return v * UNIT_SCALE.get((unit or "USD").upper(), 1.0)


def _classify(key: str) -> str | None:
    if _SETTLED_RE.search(key):
        return "settled"
    if _CLAIMED_RE.search(key):
        return "claimed"
    return None


def _kind(key: str) -> str:
    if _MONEY_WORD_RE.search(key):
        return "money"
    if _COUNT_ONLY_RE.search(key):
        return "count"
    return "money"


def _to_float(s: str) -> float | None:
    try:
        return float(re.sub(r"[^\d.\-]", "", s))
    except ValueError:
        return None


def extract(html: str) -> list[dict]:
    """从 HTML 抽候选 KPI，每条带 tier 与原文证据。

    分级只描述「这个数字有多可信」，不做取舍——取舍在 audit() 里，规则写在
    报告里，可被反驳。
    """
    # Next.js flight 数据把 JSON 转义了一层（\"key\"），key 的闭合引号前会多
    # 一个反斜杠——不先还原，T1/T2 正则在真实页面上零命中（selftest 用构造
    # 数据，暴露不了这个）
    flat = html.replace('\\"', '"')
    cands: list[dict] = []
    seen: set = set()

    def add(key, cls, kind, usd, raw, unit, tier, evidence):
        sig = (key, cls, tier)
        if sig in seen:
            return
        seen.add(sig)
        cands.append({"key": key, "cls": cls, "kind": kind, "usd": usd,
                      "raw": raw, "unit": unit, "tier": tier, "evidence": evidence})

    # T1 结构化 + 单位显式：唯一可以直接采信的一级
    for rx, order in ((_A_VALUE_UNIT, "value_first"), (_A_UNIT_VALUE, "unit_first")):
        for m in rx.finditer(flat):
            if order == "value_first":
                key, raw, unit = m.group(1), float(m.group(2)), m.group(3)
            else:
                key, unit, raw = m.group(1), m.group(2), float(m.group(3))
            cls = _classify(key)
            if cls:
                add(key, cls, _kind(key), _money(raw, unit), raw, unit, "T1", m.group(0)[:110])

    # T2 结构化但单位不明：opire 的 moneyPaidInBounties 若丢掉 unit，497587
    # 会被读成 49 万美元，高估 100 倍。只能当旁证
    for m in _B_BARE.finditer(flat):
        key, raw = m.group(1), float(m.group(2))
        cls = _classify(key)
        if cls and abs(raw) >= 1:
            add(key, cls, _kind(key), raw, raw, None, "T2", m.group(0)[:90])

    # T3 DOM 语义属性：语义在属性名里，但值可能是模板默认值，等 JS 填
    for m in _C_ATTR.finditer(html):
        attr, val = m.group(1), m.group(2)
        cls = _classify(attr)
        if not cls:
            continue
        am = _ATTR_MONEY.search(val)
        add(attr, cls, "money", _to_float(am.group(1)) if am else None,
            val.strip(), None, "T3", f'data-{attr}="{val}"'[:110])

    # T4 渲染文本：数字与文案常分属不同 DOM 节点，只做旁证
    for m in _D_MONEY.finditer(html):
        ctx = re.sub(r"\s+", " ", html[max(0, m.start() - 80):m.start()])
        cls = ("settled" if _TEXT_SETTLED.search(ctx)
               else "claimed" if _TEXT_CLAIMED.search(ctx) else None)
        if cls:
            add(f"text:{cls}", cls, "money", _to_float(m.group(1)), m.group(1),
                None, "T4", ctx[-70:])
    return cands


TIER_RANK = {"T1": 0, "T2": 1, "T3": 2}


def _fmt(v: float | None) -> str:
    if v is None:
        return "?"
    return f"${v:,.0f}" if abs(v) >= 1000 else f"${v:,.2f}"


def _verdict(realness: float) -> str:
    if realness < NOISE_BELOW:
        return "NOISE"
    if realness < THIN_BELOW:
        return "THIN"
    if realness < MARGINAL_BELOW:
        return "MARGINAL"
    return "REAL"


def _pick(cands: list[dict], cls: str, kind: str) -> dict | None:
    """按取证等级取该口径最可信的一个值。

    T1 优先于 T2 优先于 T3；同级取金额最大（总额通常是最大数）。T4 永不参与
    ——实测渲染文本里的数字与文案常分属不同 DOM 节点，靠邻近猜必错。
    """
    pool = [c for c in cands if c["cls"] == cls and c["kind"] == kind
            and c["tier"] in TIER_RANK and c["usd"] is not None]
    if kind == "money":
        # DOM 属性可能是模板默认 $0，真值等 JS 异步填（frantic 的
        # data-town-settled-amt 就是这样）。0 不等于「真的零」
        pool = [c for c in pool if not (c["tier"] == "T3" and c["usd"] == 0)]
    if not pool:
        return None
    return min(pool, key=lambda c: (TIER_RANK[c["tier"]], -c["usd"]))


def audit(cands: list[dict]) -> dict:
    """把候选 KPI 归约成判决。

    realness 是比值，分子分母同单位 → 单位不明不影响判决，只影响绝对金额的
    显示。所以 T2 可以进判决，但报告里要标出它没声明单位。
    """
    settled_money = _pick(cands, "settled", "money")
    claimed_money = _pick(cands, "claimed", "money")
    settled_count = _pick(cands, "settled", "count")
    claimed_count = _pick(cands, "claimed", "count")

    res = {"settled_money": settled_money, "claimed_money": claimed_money,
           "settled_count": settled_count, "claimed_count": claimed_count,
           "realness": None, "settle_rate": None,
           "verdict": "UNVERIFIABLE", "why": "", "unit_risk": False}

    if settled_count and claimed_count and claimed_count["usd"]:
        res["settle_rate"] = settled_count["usd"] / claimed_count["usd"]

    if not settled_money or not claimed_money:
        missing = []
        if not claimed_money:
            missing.append("名义额（挂着多少）")
        if not settled_money:
            missing.append("实付额（真付过多少）")
        res["why"] = ("页面里找不到 " + " 和 ".join(missing) +
                      " 的结构化口径，缺一边就算不出折价率。未检查 ≠ 通过。")
        return res

    if not claimed_money["usd"]:
        res["why"] = "名义额为 0，折价率无定义"
        return res

    res["realness"] = settled_money["usd"] / claimed_money["usd"]
    res["verdict"] = _verdict(res["realness"])
    res["unit_risk"] = any(c["tier"] == "T2" and c["unit"] is None
                           for c in (settled_money, claimed_money))
    res["why"] = (f"实付 {_fmt(settled_money['usd'])} / 名义 "
                  f"{_fmt(claimed_money['usd'])} = {res['realness']:.4%}")
    return res


def baseline() -> list[tuple]:
    """已知平台的真实折价率——判决的标尺。数字来自 scarcity_radar 的实跑缓存，
    不是估的。"""
    f = ROOT / "data" / "scarcity" / "latest.json"
    rows = []
    if not f.exists():
        return rows
    snap = json.loads(f.read_text(encoding="utf-8"))
    for name, st in (snap.get("stats") or {}).items():
        claimed = st.get("open_value_usd") or st.get("total_usd")
        settled = st.get("paid_total_usd")
        if claimed and settled:
            rows.append((name, float(settled), float(claimed),
                         float(settled) / float(claimed), st.get("settle_rate")))
    return rows


_ICON = {"REAL": "✔", "MARGINAL": "~", "THIN": "!", "NOISE": "✘", "UNVERIFIABLE": "?"}


def _diagnose(html: str) -> list[str]:
    """查不到口径时给出原因分类——「页面真没有」和「数据没渲染」是两回事，
    后者说明该改走 API，前者说明这个平台根本不公开可验的量。"""
    notes = []
    if "__NEXT_DATA__" in html:
        notes.append("有 __NEXT_DATA__ 容器（数据可能在其中，但未含总额口径）")
    if "self.__next_f" in html:
        notes.append("有 Next.js flight 数据（self.__next_f）")
    if "data-astro-cid" in html or "__NUXT__" in html:
        notes.append("SSR 框架（Astro/Nuxt），数据多由客户端异步拉取")
    attrs = _C_ATTR.findall(html)
    if attrs:
        names = ", ".join(f"data-{a}" for a, _v in attrs[:4])
        notes.append(f"有 {len(attrs)} 个带值的语义属性：{names}")
    if re.search(r'data-[a-z0-9-]*(?:settled|paid|available)[a-z0-9-]*\s*>', html, re.I):
        notes.append("检测到语义属性但无值（模板占位），真值由 JS 异步填 → 改走 API")
    if not notes:
        notes.append("页面里没有内嵌数据容器，数据可能全在 XHR/API 里")
    return notes


def render(res: dict, top: int = 8) -> str:
    lines = ["=" * 78,
             f"平台体检  {res.get('url', '')}   ({res.get('bytes', 0):,} bytes)",
             "=" * 78]
    v = res["verdict"]
    lines.append(f"  {_ICON[v]} 判决：{v}")
    lines.append(f"    {res['why']}")
    for note in res.get("notes", []):
        lines.append(f"    · {note}")
    if res.get("realness") is not None:
        lines.append(f"    折价率 {res['realness']:.4%}"
                     "   （阈值 <1% NOISE / <10% THIN / <50% MARGINAL / ≥50% REAL）")
    if res.get("settle_rate") is not None:
        lines.append(f"    笔数结算率 {res['settle_rate']:.2%}")
    if res.get("unit_risk"):
        lines.append("    ⚠ 参与判决的数字没声明单位（可能是「分」），绝对金额不可信；"
                     "折价率是比值，不受影响")
    for label, key in (("名义", "claimed_money"), ("实付", "settled_money"),
                       ("名义笔数", "claimed_count"), ("实付笔数", "settled_count")):
        c = res.get(key)
        if c:
            shown = _fmt(c["usd"]) if c["kind"] == "money" else f"{c['usd']:,.0f} 条"
            lines.append(f"    [{label}] {c['key']} = {shown}  tier={c['tier']}"
                         f"  ← {c['evidence'][:68]}")
    rows = baseline()
    if rows and res.get("realness") is not None:
        lines.append("    基线对照（实跑缓存，非估算）：")
        for name, settled, claimed, r, _rate in rows:
            lines.append(f"      {name:9} {r:>9.4%}   实付 {_fmt(settled):>12}"
                         f" / 名义 {_fmt(claimed):>14}")
    others = [c for c in res.get("candidates", []) if c["tier"] in ("T3", "T4")]
    if others:
        lines.append(f"    未参与判决的旁证 {len(others)} 条：")
        for c in others[:top]:
            lines.append(f"      {c['tier']} {c['key']} = {c['usd']}"
                         f"  ← {c['evidence'][:58]}")
    return "\n".join(lines)


def probe(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        html = r.read().decode("utf-8", "replace")
    cands = extract(html)
    res = audit(cands)
    res["url"] = url
    res["bytes"] = len(html)
    res["candidates"] = cands
    res["notes"] = _diagnose(html)
    return res


def selftest() -> int:
    """用已核实的真值反证判决引擎——正反两面都测，判的是这个函数自己的退出码。"""
    fails = []

    def mk(key, cls, usd, tier="T1", unit="USD"):
        return {"key": key, "cls": cls, "kind": "money", "usd": usd, "raw": usd,
                "unit": unit, "tier": tier, "evidence": "selftest"}

    def check(name, got, want):
        ok = got == want
        print(f"  {'✔' if ok else '✘'} {name}: 得到 {got}，期望 {want}")
        if not ok:
            fails.append(name)

    print("反证 1 · 已知真值必须落在正确档位")
    rows = baseline()
    if not rows:
        print("  ✘ 读不到基线缓存 data/scarcity/latest.json，无法反证")
        return 1
    for name, settled, claimed, realness, _rate in rows:
        res = audit([mk("settledTotal", "settled", settled),
                     mk("availableTotal", "claimed", claimed)])
        check(f"{name} realness={realness:.4%}", res["verdict"],
              "NOISE" if realness < 0.01 else "REAL")

    print("反证 2 · 缺口径必须 UNVERIFIABLE，不许降级成通过")
    check("只有名义额", audit([mk("availableTotal", "claimed", 100000.0)])["verdict"], "UNVERIFIABLE")
    check("只有实付额", audit([mk("settledTotal", "settled", 5000.0)])["verdict"], "UNVERIFIABLE")
    check("全空", audit([])["verdict"], "UNVERIFIABLE")

    print("反证 3 · 未渲染的 DOM 属性 $0 不得被当成真实零")
    cands = [mk("availableTotal", "claimed", 1838.0),
             mk("town-settled-amt", "settled", 0.0, tier="T3", unit=None)]
    check("T3 $0 被排除（判 UNVERIFIABLE 而非 NOISE）", audit(cands)["verdict"], "UNVERIFIABLE")

    print("反证 4 · T4 渲染文本永不参与判决")
    cands = [mk("availableTotal", "claimed", 3944928.22),
             mk("text:settled", "settled", 4975.87, tier="T4")]
    check("实付只有 T4", audit(cands)["verdict"], "UNVERIFIABLE")

    print("反证 5 · T2 裸整数可进判决，但必须标出单位风险")
    cands = [mk("moneyPaid", "settled", 497587.0, tier="T2", unit=None),
             mk("moneyAvailable", "claimed", 394492822.0, tier="T2", unit=None)]
    res = audit(cands)
    check("T2 折价率与单位无关", res["verdict"], "NOISE")
    check("标出 unit_risk", res["unit_risk"], True)

    print()
    if fails:
        print(f"✘ 反证失败 {len(fails)} 项：{', '.join(fails)}")
        return 1
    print(f"✔ 全部反证通过（{len(rows)} 组真值 + 8 项边界）")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="平台体检器：这个平台的钱是真的吗")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("probe", help="体检任意平台")
    p.add_argument("urls", nargs="+")
    p.add_argument("--json", action="store_true", help="输出机器可读结果")
    p.add_argument("--top", type=int, default=8, help="旁证显示条数")
    sub.add_parser("baseline", help="已知平台基线（含已核实真值）")
    sub.add_parser("selftest", help="用已知真值反证判决引擎")
    args = ap.parse_args(argv)

    if args.cmd == "selftest":
        return selftest()
    if args.cmd == "baseline":
        rows = baseline()
        if not rows:
            print("读不到基线缓存，先跑 tools/scarcity_radar.py scan")
            return 1
        print("已核实的平台基线（实付 / 名义）")
        for name, settled, claimed, r, rate in rows:
            extra = f"   笔数结算率 {rate:.2%}" if rate else ""
            print(f"  {name:9} {r:>9.4%}   实付 {_fmt(settled):>12}"
                  f" / 名义 {_fmt(claimed):>14}{extra}")
        return 0
    if args.cmd == "probe":
        rc = 0
        for url in args.urls:
            try:
                res = probe(url)
            except Exception as exc:
                print(f"[{url}] 抓取失败 {type(exc).__name__}: {exc}")
                rc = 1
                continue
            if args.json:
                print(json.dumps({k: v for k, v in res.items() if k != "candidates"},
                                 ensure_ascii=False, indent=2))
            else:
                print(render(res, top=args.top))
                print()
        return rc
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
