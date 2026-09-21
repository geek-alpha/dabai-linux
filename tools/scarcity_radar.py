#!/usr/bin/env python3
"""市场稀缺性雷达：钱在哪里，哪里没人抢。

稀缺不是「有人在卖」，是「有人出价、位置空着、且真会被接受」。三个条件缺一
个就是陷阱：位置空着但没人交得出来（审查瓶颈），或者有人交但没人给钱（验收
有毒），都不算机会。

两个平台语义完全不同，但都能归约成同一组量：

  slots        名额（Frantic: capacity；Opire: 1，一个 PR 被合并才算赢）
  competition  竞争者（Frantic: occupied；Opire: 已认领人数）
  gap          = max(0, slots - competition) / slots   还有多少位置
  contest      = competition / slots                   拥挤度
  odds         = gap / (1 + contest)                   拿到钱的概率代理
  expectancy   = price * odds                          期望收益

按 expectancy 排序，而不是按价格——高价但 27 个人在抢的单，期望低于低价但
没人碰的单。这是这个工具和「悬赏列表搬运」的全部区别。

  scarcity_radar.py scan            抓取所有源，算分，出榜单
  scarcity_radar.py report          只看上次 scan 的缓存（离线）
  scarcity_radar.py sources         列出各源的真实结算率与规模

不需要账号，不需要 key。抓的都是各平台公开接口。
"""
from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data" / "scarcity"
CACHE_FILE = DATA_DIR / "latest.json"
USER_AGENT = "dabai-scarcity-radar/0.1 (+research; contact via repo)"
TIMEOUT = 25

FRANTIC_BOARD = "https://gofrantic.com/v1/board"
OPIRE_REWARDS = "https://api.opire.dev/rewards?page={page}"
OPIRE_MAX_PAGES = 6

# 低于这个金额的单子，期望收益再高也不值得开一次工——开工成本（上下文、
# 身份、沟通）是固定的，小额单的净收益是负的
MIN_WORTH_USD = 20.0
STALE_DAYS = 60.0


def _get_json(url: str, retries: int = 2) -> object:
    last = None
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                return json.loads(resp.read().decode("utf-8", "replace"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
            last = exc
            if attempt < retries:
                time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"{url} -> {last}")


def _age_days(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return (datetime.now(timezone.utc) - dt).total_seconds() / 86400.0


def fetch_frantic() -> tuple[list[dict], dict]:
    """Frantic：唯一直接暴露名额的平台（claim_slots.capacity/occupied）。"""
    raw = _get_json(FRANTIC_BOARD)
    board = raw.get("board") or {}
    items = []
    for b in board.get("bounties") or []:
        slots = b.get("claim_slots") or {}
        cap = int(slots.get("capacity") or 0)
        occ = int(slots.get("occupied") or 0)
        if cap <= 0:
            continue
        items.append({
            "source": "frantic",
            "uid": f"frantic#{b.get('number')}",
            "title": (b.get("title") or "").strip(),
            "url": f"https://gofrantic.com/bounties/{b.get('number')}",
            "price_usd": float(b.get("price_usd") or 0.0),
            "slots": cap,
            "competition": occ,
            "status": b.get("work_status") or "?",
            "posted_at": b.get("posted_at"),
            "age_days": _age_days(b.get("posted_at")),
            "note": (b.get("note") or "")[:160],
        })

    # 结算率：paid 是真付过钱的，delivered 是交了活但没结算的——后者就是
    # 「有人干、没人认」的直接证据，比任何声明都硬
    paid = sum(1 for x in items if x["status"] == "paid")
    delivered = sum(1 for x in items if x["status"] == "delivered")
    accepted = sum(1 for x in items if x["status"] == "accepted")
    open_ = sum(1 for x in items if x["status"] == "open")
    denom = paid + delivered
    health = (paid / denom) if denom else None
    stats = {
        "source": "frantic",
        "total": len(items),
        "paid": paid, "delivered": delivered, "accepted": accepted, "open": open_,
        "settle_rate": health,
        "total_usd": float(board.get("season_total_usd") or 0.0),
        "moved_usd": float(board.get("moved_usd") or 0.0),
        "paid_total_usd": float(board.get("moved_usd") or 0.0),
        "paid_count": paid,
        "paid_avg_usd": (float(board.get("moved_usd") or 0.0) / paid) if paid else None,
        "goodwill_usd": float(board.get("goodwill_granted") or 0.0),
        "enlisted": board.get("operators_enlisted"),
        "sworn": board.get("sworn_count"),
    }
    return items, stats


OPIRE_HOME = "https://app.opire.dev/"


def _opire_web_stats() -> dict:
    """官网顶栏是全站唯一权威口径：真付过多少、挂着多少。

    API 只给当前挂着的名义额，给不了历史实付——而后者才是判断那些数字真
    假的唯一依据。抓不到就返回空，由调用方降级成「未检查」。
    """
    req = urllib.request.Request(OPIRE_HOME, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            html = resp.read().decode("utf-8", "replace")
    except (urllib.error.URLError, TimeoutError, OSError):
        return {}

    # 页面里嵌着 Next.js 的 props，homeKPIs 才是权威口径：渲染后的文本里
    # "Paid" 会命中别处、"Available" 有 5 处，按位置取必错
    def kpi(key: str, scale: float = 1.0) -> float | None:
        m = re.search(rf"{key}\D{{0,30}}?(\d+)", html)
        return float(m.group(1)) / scale if m else None

    return {
        "paid_count": kpi("bountiesPaid"),
        "available_count": kpi("bountiesAvailable"),
        "paid_out_usd": kpi("moneyPaidInBounties", 100.0),
        "open_value_usd": kpi("moneyAvailableInBounties", 100.0),
    }


def fetch_opire() -> tuple[list[dict], dict]:
    """Opire：pendingPrice.value 单位是分（已用 issue 评论原文交叉验证）。"""
    items = []
    for page in range(1, OPIRE_MAX_PAGES + 1):
        try:
            batch = _get_json(OPIRE_REWARDS.format(page=page))
        except RuntimeError:
            break
        if not isinstance(batch, list) or not batch:
            break
        for r in batch:
            price = r.get("pendingPrice") or {}
            cents = price.get("value")
            if not isinstance(cents, (int, float)):
                continue
            claimers = r.get("claimerUsers") or []
            items.append({
                "source": "opire",
                "uid": r.get("id") or r.get("url"),
                "title": (r.get("title") or "").strip(),
                "url": r.get("url"),
                "price_usd": float(cents) / 100.0,
                "slots": 1,               # winner-takes-all：合进去才有钱
                "competition": len(claimers),
                "status": "open",
                "posted_at": None,
                "age_days": _age_days(
                    datetime.fromtimestamp(r["createdAt"] / 1000.0, timezone.utc).isoformat()
                    if isinstance(r.get("createdAt"), (int, float)) else None
                ),
                "note": f"claimers={len(claimers)} trying={len(r.get('tryingUsers') or [])}",
                # 项目没装 OpireBot，/claim 就触发不了付款——赏金挂着也拿不到
                "payable": bool((r.get("project") or {}).get("isBotInstalled")),
            })

    amounts = [x["price_usd"] for x in items]
    web = _opire_web_stats()
    paid_total, paid_n = web.get("paid_out_usd"), web.get("paid_count")
    avail_n = web.get("available_count")
    stats = {
        "source": "opire",
        "total": len(items),
        "settle_rate": (paid_n / (paid_n + avail_n)) if (paid_n and avail_n) else None,
        "median_usd": statistics.median(amounts) if amounts else None,
        "total_usd": sum(amounts),
        "free": sum(1 for x in items if x["competition"] == 0),
        "paid_total_usd": paid_total,
        "paid_count": paid_n,
        "paid_avg_usd": (paid_total / paid_n) if (paid_total and paid_n) else None,
        "open_value_usd": web.get("open_value_usd"),
        "unpayable": sum(1 for x in items if not x.get("payable", True)),
    }
    return items, stats


def score(items: list[dict], stats_by_source: dict[str, dict]) -> list[dict]:
    """把「名额 vs 竞争者」算成期望收益，并打上归因标签。

    标签不是装饰，是判断「要不要动手」的开关：
      CLEAR   位置空着、没人抢——唯一值得立刻动手的状态
      ROOM    还有位置，但已经有人进场
      FULL    名额抢光，去了也白去
      THIN    期望收益低于开工成本，做成了也不划算
      STALE   挂了很久没人接，多半是条件有毒或需求是假的
      TRAP    源的真实结算率低——交得出来也未必拿得到钱
      SUSPECT 标价超过该平台历史实付总额，面值不可信，已按单笔实付均值折价
      UNPAYABLE 项目没装支付 bot，/claim 触发不了付款，赏金挂着也拿不到
    """
    out = []
    for it in items:
        slots = max(1, int(it["slots"]))
        comp = max(0, int(it["competition"]))
        gap = max(0.0, slots - comp) / slots
        contest = comp / slots
        odds = gap / (1.0 + contest)
        st = stats_by_source.get(it["source"]) or {}
        paid_total, paid_avg = st.get("paid_total_usd"), st.get("paid_avg_usd")

        # 名义标价不能直接信：一个平台历史上一共才付出这么多钱，任何单条超过
        # 这个数的赏金就不该按面值算。用平台单笔实付均值给它封顶。
        price_used = it["price_usd"]
        suspect = bool(paid_total and it["price_usd"] > paid_total)
        if suspect and paid_avg:
            price_used = paid_avg

        health = st.get("settle_rate")
        # 抢到名额不等于钱到手，再乘该平台的历史结算率：Frantic 94.5% 对
        # Opire 9.2%，同一笔标价的实际到手期望差一个量级
        expectancy = price_used * odds * (health if health is not None else 1.0)
        tags = []
        if it.get("payable") is False:
            tags.append("UNPAYABLE")
        if suspect:
            tags.append("SUSPECT")
        if gap <= 0:
            tags.append("FULL")
        elif comp == 0:
            tags.append("CLEAR")
        else:
            tags.append("ROOM")
        if expectancy < MIN_WORTH_USD:
            tags.append("THIN")
        if it.get("age_days") is not None and it["age_days"] > STALE_DAYS and gap > 0:
            tags.append("STALE")
        if health is not None and health < 0.6 and gap > 0:
            tags.append("TRAP")

        rec = dict(it)
        rec.update({
            "gap": round(gap, 3),
            "contest": round(contest, 3),
            "odds": round(odds, 3),
            "price_used_usd": round(price_used, 2),
            "expectancy_usd": round(expectancy, 2),
            "tags": tags,
        })
        out.append(rec)

    # 付不出钱的排最后，哪怕它标着两百万
    out.sort(key=lambda x: ("UNPAYABLE" in x["tags"], -x["expectancy_usd"], -x["price_usd"]))
    return out


def summarize(ranked: list[dict]) -> dict:
    clear = [x for x in ranked if "CLEAR" in x["tags"]]
    return {
        "n": len(ranked),
        "clear_n": len(clear),
        "clear_usd": round(sum(x["price_used_usd"] for x in clear), 2),
        "total_usd": round(sum(x["price_usd"] for x in ranked), 2),
        "full_n": sum(1 for x in ranked if "FULL" in x["tags"]),
        "trap_n": sum(1 for x in ranked if "TRAP" in x["tags"]),
        "suspect_n": sum(1 for x in ranked if "SUSPECT" in x["tags"]),
        "unpayable_n": sum(1 for x in ranked if "UNPAYABLE" in x["tags"]),
        "nominal_usd": round(sum(x["price_usd"] for x in ranked), 2),
        "real_usd": round(sum(x["price_used_usd"] for x in ranked), 2),
    }


def _fmt_money(v: float) -> str:
    if v >= 1000:
        return f"${v/1000:.1f}k"
    if v >= 1:
        return f"${v:.0f}"
    return f"${v:.2f}"


def render(ranked: list[dict], stats_by_source: dict[str, dict], top: int = 20) -> str:
    lines = []
    s = summarize(ranked)
    lines.append("=" * 96)
    lines.append("市场稀缺性雷达  ——  按期望收益排序，不是按标价")
    lines.append("=" * 96)
    lines.append(
        f"扫到 {s['n']} 条悬赏，其中位置空着、没人抢的 {s['clear_n']} 条，"
        f"按实付能力折算后合计 {_fmt_money(s['clear_usd'])}"
    )
    lines.append(f"名额抢光的 {s['full_n']} 条，被标 TRAP 的 {s['trap_n']} 条，"
                 f"标价不可信被折价的 {s['suspect_n']} 条，根本付不出钱的 {s['unpayable_n']} 条")
    lines.append(f"名义总额 {_fmt_money(s['nominal_usd'])}  →  "
                 f"按各平台实付能力折算后 {_fmt_money(s['real_usd'])}")
    lines.append("")

    lines.append("【各源体检】")
    for name, st in stats_by_source.items():
        rate = st.get("settle_rate")
        rate_s = f"{rate*100:.1f}%" if rate is not None else "不公开"
        extra = ""
        if name == "frantic":
            extra = (f"  已结算 {st.get('paid')} / 交付未结 {st.get('delivered')}"
                     f"  报名 {st.get('enlisted')} 人，sworn {st.get('sworn')} 人，"
                     f"整季真实流动 {_fmt_money(st.get('moved_usd') or 0)}，"
                     f"白送 {_fmt_money(st.get('goodwill_usd') or 0)}")
        elif name == "opire":
            med = st.get("median_usd")
            pt, pc = st.get("paid_total_usd"), st.get("paid_count")
            reality = (f"  历史实付 {_fmt_money(pt)}/{pc:.0f} 笔" if pt and pc
                       else "  历史实付 未检查")
            extra = (f"  中位标价 {_fmt_money(med) if med else '?'}"
                     f"  零认领 {st.get('free')} 条  未装支付bot {st.get('unpayable')} 条{reality}")
        lines.append(f"  {name:9} {st['total']:>4} 条   真实结算率 {rate_s:>8}{extra}")
        if st.get("paid_total_usd") is None:
            lines.append(f"            ⚠ {name} 的实付数据没拿到，该源标价只能按面值计，"
                         f"排序会高估——这不是「没问题」，是「没检查」")
    lines.append("")

    lines.append(f"【期望收益榜 · 前 {top}】")
    lines.append(f"{'标签':<26}{'期望':>9}{'计价':>9}{'标价':>11}{'占/名额':>9}  标题")
    lines.append("-" * 96)
    for x in ranked[:top]:
        slot_s = f"{x['competition']}/{x['slots']}"
        tag_s = ",".join(x["tags"])
        title = x["title"][:36]
        lines.append(
            f"{tag_s:<26}{_fmt_money(x['expectancy_usd']):>9}{_fmt_money(x['price_used_usd']):>9}"
            f"{_fmt_money(x['price_usd']):>11}{slot_s:>9}  {title}"
        )
    lines.append("")
    lines.append("【CLEAR：位置空着、没人抢，唯一值得立刻动手的】")
    clear = [x for x in ranked if "CLEAR" in x["tags"]]
    if not clear:
        lines.append("  无")
    for x in clear[:10]:
        lines.append(f"  {_fmt_money(x['price_used_usd']):>9}  {x['source']:<8} {x['title'][:58]}")
        lines.append(f"             {x['url']}")
    lines.append("")
    lines.append("【TRAP：交得出来也未必拿得到钱】")
    traps = [x for x in ranked if "TRAP" in x["tags"]]
    if not traps:
        lines.append("  无")
    for x in traps[:5]:
        lines.append(f"  {x['source']:<8} {x['title'][:56]}")
    return "\n".join(lines)


def scan(verbose: bool = True) -> dict:
    items: list[dict] = []
    stats_by_source: dict[str, dict] = {}
    errors: list[str] = []

    for name, fn in (("frantic", fetch_frantic), ("opire", fetch_opire)):
        try:
            got, st = fn()
            items.extend(got)
            stats_by_source[name] = st
            if verbose:
                print(f"[{name}] 抓到 {len(got)} 条", file=sys.stderr)
        except Exception as exc:  # 单源失败不该让整次扫描归零
            errors.append(f"{name}: {exc}")
            if verbose:
                print(f"[{name}] 失败：{exc}", file=sys.stderr)

    ranked = score(items, stats_by_source)
    snapshot = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "errors": errors,
        "stats": stats_by_source,
        "items": ranked,
    }
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_FILE.write_text(json.dumps(snapshot, ensure_ascii=False, indent=1), encoding="utf-8")
    return snapshot


def load_cache() -> dict | None:
    if not CACHE_FILE.exists():
        return None
    try:
        return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def main() -> int:
    ap = argparse.ArgumentParser(description="市场稀缺性雷达")
    ap.add_argument("cmd", nargs="?", default="scan", choices=["scan", "report", "sources", "json"])
    ap.add_argument("--top", type=int, default=20)
    ap.add_argument("--clear-only", action="store_true", help="只列位置空着没人抢的")
    args = ap.parse_args()

    if args.cmd in ("scan", "json"):
        snap = scan(verbose=(args.cmd == "scan"))
        if args.cmd == "json":
            print(json.dumps(snap, ensure_ascii=False, indent=1))
            return 0
    else:
        snap = load_cache()
        if not snap:
            print("还没有缓存，先跑：scarcity_radar.py scan", file=sys.stderr)
            return 1

    ranked = snap["items"]
    stats = snap["stats"]
    if args.cmd == "sources":
        for name, st in stats.items():
            print(json.dumps({name: st}, ensure_ascii=False, indent=1))
        return 0

    if args.clear_only:
        ranked = [x for x in ranked if "CLEAR" in x["tags"]]
    print(render(ranked, stats, top=args.top))
    age = _age_days(snap.get("generated_at"))
    if age is not None:
        print(f"数据时间：{snap['generated_at'][:19]}Z（{age*24:.1f} 小时前）")
    if snap.get("errors"):
        print("抓取失败：" + "; ".join(snap["errors"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
