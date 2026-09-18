"""编排层：把采集 → 选题 → 草稿 → 审阅 → 发布串成一条命令。

设计原则：
- 默认不发。除 `post` 和 `run --auto-post` 外，所有命令都只读或只写本地库。
- 三道闸门拦住误发：草稿必须 approved、当日配额未满、该热点没发过。
- `run` 是给定时任务用的：采集+生成草稿，把待审清单打出来。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    from tools.x_ip import compose, notify, sources, store, topics, wecom_bot, x_api
else:
    from . import compose, notify, sources, store, topics, wecom_bot, x_api


def _p(*a, **kw):
    print(*a, **kw)


# ---------- verify ----------

def cmd_verify(args) -> int:
    st = x_api.selftest()
    _p(("✓" if st["ok"] else "✗") + f" OAuth 签名自检：{st['got']}")
    cred = x_api.load_cred()
    miss = x_api.missing(cred)
    if miss:
        _p("✗ X 凭证缺失：" + ", ".join(miss))
        _p(f"  填 {x_api.CRED_PATH} 或设环境变量后重试")
    else:
        r = x_api.verify(cred)
        if r["ok"]:
            u = r["user"]
            _p(f"✓ X 账号连通：@{u.get('username')} ({u.get('name')})")
        else:
            _p(f"✗ X 校验失败 [{r.get('status')}]：{json.dumps(r.get('error'), ensure_ascii=False)[:300]}")
    provs = compose._providers()
    _p(f"LLM 通道 {len(provs)} 个：" + ", ".join(f"{p['name']}/{p['model']}" for p in provs))
    if provs:
        try:
            txt, used = compose.call_llm(
                [{"role": "user", "content": "只回两个字：收到"}], timeout=40.0
            )
            _p(f"✓ LLM 可用：{used} → {txt.strip()[:20]}")
        except Exception as e:
            _p(f"✗ LLM 全挂：{e}")
    return 0


# ---------- collect ----------

def cmd_collect(args) -> int:
    only = [s.strip() for s in args.sources.split(",")] if args.sources else None
    t0 = time.time()
    items, errs = sources.fetch_all(only=only, per_source=args.per)
    r = store.upsert_topics(items)
    _p(f"采集 {time.time() - t0:.1f}s：{r['total']} 条（新增 {r['added']}，更新 {r['updated']}）")
    if errs:
        _p("源错误：" + "；".join(errs))
    return 0


# ---------- top ----------

def cmd_top(args) -> int:
    rows = topics.rank(topics.from_db(limit=args.pool), min_score=args.min_score)
    if args.pillar:
        rows = [r for r in rows if args.pillar in (r.get("pillars") or [])]
    if not rows:
        _p("没有过阈选题。先跑 collect，或降低 --min-score。")
        return 0
    _p(f"待选 {len(rows)} 条（按 IP 契合度排序）：\n")
    _p(topics.brief(rows, args.limit))
    return 0


# ---------- draft ----------

def cmd_draft(args) -> int:
    persona = topics.load_persona()
    if args.key:
        t = store.get_topic(args.key)
        if not t:
            _p(f"找不到选题 {args.key}")
            return 1
        rows = topics.rank(topics.from_db(limit=args.pool), min_score=0)
        row = next((r for r in rows if r["key"] == args.key), None)
        if not row:
            row = {"key": t["key"], "title": t["title"], "pillars": [], "rank": t["rank"],
                   "source": t["source"], "url": t["url"]}
    else:
        rows = topics.rank(topics.from_db(limit=args.pool), min_score=args.min_score)
        if not rows:
            _p("没有过阈选题，先跑 collect。")
            return 1
        row = rows[0]

    _p(f"选题：{row['title']}")
    _p(f"      {topics.explain(row)}\n")
    cands, src = compose.generate(row, persona, n=args.count, extra=args.angle or "")
    _p(f"[{src}]")
    if not cands:
        _p("没生成任何候选。")
        return 1
    ids = store.add_drafts(row["key"], cands, model=src)
    for did, c in zip(ids, cands):
        flag = "⚠ " + "；".join(c["problems"]) if c["problems"] else "✓ 可发"
        _p(f"\n#{did} · {c['angle']} · 加权 {c['weighted']} · {flag}")
        _p(c["text"])
    _p(f"\n已存为待审草稿：{ids}")
    return 0


# ---------- drafts / 审阅 ----------

def cmd_drafts(args) -> int:
    rows = store.list_drafts(status=args.status, limit=args.limit)
    if not rows:
        _p(f"没有 {args.status} 状态的草稿。")
        return 0
    for r in rows:
        _p(f"#{r['id']} [{r['status']}] {r['angle']} · 话题：{(r['topic_title'] or '')[:44]}")
        _p("    " + r["text"].replace("\n", "\n    "))
        _p("")
    return 0


def cmd_export(args) -> int:
    """把草稿导出成一行条目的纯推文，方便手动复制到 x.com 发布（不占 API 额度）。"""
    rows = store.list_drafts(status=args.status, limit=args.limit)
    if not rows:
        _p(f"没有 {args.status} 状态的草稿。")
        return 0
    out = []
    for r in rows:
        txt = (r["text"] or "").strip()
        if not txt:
            continue
        out.append(txt)
        if args.topic:
            out.append(f"【话题】{r['topic_title'] or ''}")
    _p("\n\n---\n\n".join(out))
    _p(f"\n共 {len(out)} 条。直接复制粘贴到 x.com 发帖即可——手动发不消耗 API 额度。")
    return 0


def _set_status(args, status: str) -> int:
    d = store.get_draft(args.id)
    if not d:
        _p(f"找不到草稿 #{args.id}")
        return 1
    store.set_draft_status(args.id, status)
    _p(f"#{args.id} → {status}")
    return 0


def cmd_approve(args) -> int:
    return _set_status(args, "approved")


def cmd_reject(args) -> int:
    return _set_status(args, "rejected")


def cmd_edit(args) -> int:
    d = store.get_draft(args.id)
    if not d:
        _p(f"找不到草稿 #{args.id}")
        return 1
    store.update_draft_text(args.id, args.text)
    probs = compose.validate(args.text, topics.load_persona())
    _p(f"#{args.id} 已改，加权 {compose.weighted_len(args.text)}"
       + ("；⚠ " + "；".join(probs) if probs else "；✓ 可发"))
    return 0


# ---------- post ----------

def _gate(draft: dict, args, persona: dict, db: str | None = None) -> str:
    """返回拦截原因，空串 = 放行。

    db 必须可传：去重和配额要读库，写死了就没法用临时库验证闸门本身。
    """
    db = db or store.DEFAULT_DB
    if draft["status"] == "posted":
        return "该草稿已发过"
    if draft["status"] != "approved" and not args.force:
        return f"草稿状态是 {draft['status']}，需先 approve（或加 --force）"
    probs = compose.validate(draft["text"], persona)
    if probs and not args.force:
        return "内容有问题：" + "；".join(probs)
    if draft["topic_key"] in store.posted_topic_keys(db=db):
        return "这个热点已经发过，IP 不能复读"
    quota = persona.get("daily_quota", 3)
    n = store.count_posts_today(db=db)
    if n >= quota and not args.force:
        return f"今日已发 {n} 条，达配额 {quota}"
    return ""


def cmd_notify(args) -> int:
    """把草稿推送到企业微信群——手动发布前先推到手机审一眼。"""
    if args.test:
        res = notify.send_text(args.test)
        if res["ok"]:
            _p("✓ 测试消息已送达企业微信")
            return 0
        _p(f"✗ 测试失败：{res['error']}")
        return 1
    rows = store.list_drafts(status=args.status, limit=args.limit)
    if not rows:
        _p(f"没有 {args.status} 状态的草稿。")
        return 0
    ok_n = 0
    for r in rows:
        txt = (r["text"] or "").strip()
        if not txt:
            continue
        head = f"#X草稿{r['id']} [{r['angle'] or '?'}]".strip()
        body = txt
        if args.topic and r.get("topic_title"):
            body += f"\n\n【话题】{r['topic_title']}"
        res = notify.send_text(f"{head}\n\n{body}")
        if res["ok"]:
            ok_n += 1
            _p(f"✓ 已推送 #{r['id']} → 企业微信")
        else:
            _p(f"✗ 推送 #{r['id']} 失败：{res['error']}")
    _p(f"共推送 {ok_n}/{len(rows)} 条。")
    return 0 if ok_n == len(rows) else 1


def cmd_post(args) -> int:
    persona = topics.load_persona()
    d = store.get_draft(args.id)
    if not d:
        _p(f"找不到草稿 #{args.id}")
        return 1
    if args.text:
        store.update_draft_text(args.id, args.text)
        d["text"] = args.text

    reason = _gate(d, args, persona)
    if reason:
        _p(f"✗ 拦住不发：{reason}")
        return 1

    if args.dry_run:
        _p("— dry-run，不真发 —")
        _p(f"加权长度 {compose.weighted_len(d['text'])} / 上限 {persona.get('safe_weighted')}")
        _p(d["text"])
        return 0

    r = x_api.post_text(d["text"])
    store.record_post(
        draft_id=d["id"],
        topic_key=d["topic_key"],
        text=d["text"],
        ok=r["ok"],
        tweet_id=r.get("tweet_id", ""),
        tweet_url=r.get("tweet_url", ""),
        http_status=r.get("status", 0),
        error="" if r["ok"] else json.dumps(r.get("error") or r.get("raw"), ensure_ascii=False)[:500],
        raw=json.dumps(r.get("raw"), ensure_ascii=False)[:2000],
    )
    if r["ok"]:
        _p(f"✓ 已发：{r.get('tweet_url')}")
    else:
        _p(f"✗ 发布失败 [{r.get('status')}]：{json.dumps(r.get('error') or r.get('raw'), ensure_ascii=False)[:300]}")
    return 0 if r["ok"] else 1


def cmd_post_text(args) -> int:
    persona = topics.load_persona()
    probs = compose.validate(args.text, persona)
    if probs and not args.force:
        _p("✗ 拦住不发：内容有问题 → " + "；".join(probs))
        return 1
    if args.dry_run:
        _p("— dry-run —\n" + args.text)
        return 0
    r = x_api.post_text(args.text)
    store.record_post(None, "manual:" + str(int(time.time())), args.text, r["ok"],
                      r.get("tweet_id", ""), r.get("tweet_url", ""), r.get("status", 0),
                      "" if r["ok"] else json.dumps(r.get("error"), ensure_ascii=False)[:400])
    _p(("✓ 已发：" + str(r.get("tweet_url"))) if r["ok"] else f"✗ 失败 [{r.get('status')}] {r.get('error')}")
    return 0 if r["ok"] else 1


# ---------- daily（定时推送草稿到企业微信） ----------

def cmd_daily(args) -> int:
    """每天定时入口：采集热点 → 生成草稿 → 推到企业微信（手动审阅后发布）。

    与 run 的区别：不受发布配额/时段限制（草稿不占配额），多一步推送。
    """
    persona = topics.load_persona()
    items, errs = sources.fetch_all(per_source=args.per)
    r = store.upsert_topics(items)
    _p(f"采集 {r['total']} 条（新增 {r['added']}）" + (f"｜源错误：{errs}" if errs else ""))

    rows = topics.rank(topics.from_db(limit=60), min_score=args.min_score)
    skip = store.posted_topic_keys() | store.topics_with_open_drafts()
    rows = [x for x in rows if x["key"] not in skip]
    if not rows:
        _p("没有新选题。")
        return 0

    made = []
    for row in rows[: args.n]:
        cands, src = compose.generate(row, persona, n=args.count)
        good = [c for c in cands if not c["problems"]]
        if not good:
            continue
        ids = store.add_drafts(row["key"], good, model=src)
        made.append((row, list(zip(ids, good))))
        _p(f"\n▸ {row['title'][:60]}")
        for did, c in zip(ids, good):
            _p(f"  #{did} · {c['angle']} · 加权 {c['weighted']}")

    if not made:
        _p("生成了候选但都不合规，未入库。")
        return 0

    pushed = 0
    for row, pairs in made:
        for did, c in pairs[: args.push]:
            md = f"**📬 X 草稿 #{did}**｜{row['title'][:40]}\n\n{c['text']}\n\n回复 **发** 走发布流程"
            wecom_bot.queue_push(args.chatid, md)
            pushed += 1
    _p(f"\n已排队推送 {pushed} 条草稿 → 企业微信 {args.chatid}（serve 30 秒内发出）")
    return 0


# ---------- run（定时任务入口） ----------

def _in_window(windows: list[str]) -> bool:
    """当前时间是否落在发布时段内。格式 "HH:MM-HH:MM"，支持跨午夜。"""
    if not windows:
        return True
    now = time.localtime()
    cur = now.tm_hour * 60 + now.tm_min
    for w in windows:
        try:
            a, b = w.split("-")
            h1, m1 = (int(x) for x in a.split(":"))
            h2, m2 = (int(x) for x in b.split(":"))
        except (ValueError, TypeError):
            continue
        s, e = h1 * 60 + m1, h2 * 60 + m2
        if s <= e:
            if s <= cur <= e:
                return True
        elif cur >= s or cur <= e:  # 跨午夜
            return True
    return False


def cmd_run(args) -> int:
    persona = topics.load_persona()
    if args.window and not _in_window(persona.get("post_windows") or []):
        _p(f"当前不在发布时段 {persona.get('post_windows')}，跳过本轮。")
        return 0
    quota = persona.get("daily_quota", 3)
    used = store.count_posts_today()
    if used >= quota:
        _p(f"今日已发 {used}/{quota}，跳过本轮。")
        return 0

    items, errs = sources.fetch_all(per_source=args.per)
    r = store.upsert_topics(items)
    _p(f"采集 {r['total']} 条（新增 {r['added']}）" + (f"｜源错误：{errs}" if errs else ""))

    rows = topics.rank(topics.from_db(limit=60), min_score=args.min_score)
    skip = store.posted_topic_keys() | store.topics_with_open_drafts()
    rows = [x for x in rows if x["key"] not in skip]
    if not rows:
        _p("没有新选题。")
        return 0

    budget = min(args.n, quota - used)
    made = []
    for row in rows[:budget]:
        cands, src = compose.generate(row, persona, n=args.count)
        good = [c for c in cands if not c["problems"]]
        if not good:
            continue
        ids = store.add_drafts(row["key"], good, model=src)
        made.append((row, list(zip(ids, good))))
        _p(f"\n▸ {row['title'][:60]}")
        for did, c in zip(ids, good):
            _p(f"  #{did} · {c['angle']} · 加权 {c['weighted']}")

    if not made:
        _p("生成了候选但都不合规，未入库。")
        return 0

    if not args.auto_post:
        _p(f"\n共 {sum(len(v) for _, v in made)} 条待审草稿。审阅：cli.py drafts；批准：cli.py approve <id>")
        return 0

    # auto-post：每个选题挑第一条已生成的好草稿发出去
    posted = 0
    for row, pairs in made:
        if store.count_posts_today() >= quota:
            _p("配额已满，停止自动发。")
            break
        did, c = pairs[0]
        store.set_draft_status(did, "approved")
        d = store.get_draft(did)
        reason = _gate(d, argparse.Namespace(force=False), persona)
        if reason:
            _p(f"✗ #{did} 拦住：{reason}")
            continue
        res = x_api.post_text(c["text"])
        store.record_post(did, row["key"], c["text"], res["ok"], res.get("tweet_id", ""),
                          res.get("tweet_url", ""), res.get("status", 0),
                          "" if res["ok"] else json.dumps(res.get("error"), ensure_ascii=False)[:400])
        _p(("✓ " if res["ok"] else "✗ ") + f"#{did} → {res.get('tweet_url') or res.get('error')}")
        posted += 1 if res["ok"] else 0
    _p(f"自动发布 {posted} 条。")
    return 0


# ---------- stats ----------

def cmd_stats(args) -> int:
    s = store.stats()
    persona = topics.load_persona()
    _p(f"库：{store.DEFAULT_DB}")
    _p(f"选题 {s['topics']} 条（待选 {s['topics_new']}）｜草稿待审 {s['drafts_pending']}")
    _p(f"已发 {s['posted_ok']} 条，失败 {s['posted_fail']} 条｜今日 {s['posted_today']}/{persona.get('daily_quota')}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="x_ip", description="X 时事推文 IP 工作流")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("verify", help="校验 OAuth 签名 / X 凭证 / LLM 通道").set_defaults(fn=cmd_verify)

    c = sub.add_parser("collect", help="采集热点入库")
    c.add_argument("--per", type=int, default=15)
    c.add_argument("--sources", default="", help="逗号分隔，默认全部")
    c.set_defaults(fn=cmd_collect)

    t = sub.add_parser("top", help="看选题排行")
    t.add_argument("--limit", type=int, default=15)
    t.add_argument("--pool", type=int, default=80)
    t.add_argument("--min-score", type=float, default=None)
    t.add_argument("--pillar", default="", help="只看某个领域")
    t.set_defaults(fn=cmd_top)

    d = sub.add_parser("draft", help="生成草稿")
    d.add_argument("--key", default="", help="指定选题 key")
    d.add_argument("--pool", type=int, default=80)
    d.add_argument("--count", type=int, default=3)
    d.add_argument("--min-score", type=float, default=None)
    d.add_argument("--angle", default="", help="额外写作要求")
    d.set_defaults(fn=cmd_draft)

    ds = sub.add_parser("drafts", help="列出草稿")
    ds.add_argument("--status", default="pending")
    ds.add_argument("--limit", type=int, default=20)
    ds.set_defaults(fn=cmd_drafts)

    ex = sub.add_parser("export", help="导出草稿为纯推文（手动粘贴发布，不占 API 额度）")
    ex.add_argument("--status", default="pending")
    ex.add_argument("--limit", type=int, default=50)
    ex.add_argument("--topic", action="store_true", help="每条后附带话题标题")
    ex.set_defaults(fn=cmd_export)

    a = sub.add_parser("approve", help="批准草稿")
    a.add_argument("id", type=int)
    a.set_defaults(fn=cmd_approve)

    rj = sub.add_parser("reject", help="否决草稿")
    rj.add_argument("id", type=int)
    rj.set_defaults(fn=cmd_reject)

    e = sub.add_parser("edit", help="改草稿正文")
    e.add_argument("id", type=int)
    e.add_argument("text")
    e.set_defaults(fn=cmd_edit)

    nt = sub.add_parser("notify", help="把草稿推到企业微信群（需要 wecom_webhook，单向通知）")
    nt.add_argument("--status", default="approved", help="推哪个状态的草稿（默认 approved）")
    nt.add_argument("--limit", type=int, default=10)
    nt.add_argument("--topic", action="store_true", help="每条附带话题标题")
    nt.add_argument("--test", default="", help="发一条测试消息（内容=该值），不发草稿")
    nt.set_defaults(fn=cmd_notify)

    po = sub.add_parser("post", help="发布草稿")
    po.add_argument("id", type=int)
    po.add_argument("--text", default="", help="发布前替换正文")
    po.add_argument("--dry-run", action="store_true")
    po.add_argument("--force", action="store_true", help="跳过审批/配额/去重闸门")
    po.set_defaults(fn=cmd_post)

    pt = sub.add_parser("post-text", help="直接发一条（不进选题库）")
    pt.add_argument("text")
    pt.add_argument("--dry-run", action="store_true")
    pt.add_argument("--force", action="store_true")
    pt.set_defaults(fn=cmd_post_text)

    r = sub.add_parser("run", help="定时任务入口：采集+生成草稿（可选自动发）")
    r.add_argument("--n", type=int, default=2, help="本轮处理几个选题")
    r.add_argument("--count", type=int, default=3, help="每个选题生成几条")
    r.add_argument("--per", type=int, default=15)
    r.add_argument("--min-score", type=float, default=None)
    r.add_argument("--auto-post", action="store_true", help="不审阅直接发（慎用）")
    r.add_argument("--window", action="store_true",
                   help="只在 persona.post_windows 时段内干活（定时任务建议开）")
    r.set_defaults(fn=cmd_run)

    dl = sub.add_parser("daily", help="每日定时：采集+草稿+推到企业微信（手动发布）")
    dl.add_argument("--n", type=int, default=3, help="本轮处理几个选题")
    dl.add_argument("--count", type=int, default=1, help="每个选题生成几条")
    dl.add_argument("--push", type=int, default=1, help="每个选题推送几条草稿到微信")
    dl.add_argument("--per", type=int, default=15)
    dl.add_argument("--min-score", type=float, default=None)
    dl.add_argument("--chatid", default="WangXingFeng", help="企业微信会话 ID（单聊=userid）")
    dl.set_defaults(fn=cmd_daily)

    sub.add_parser("stats", help="状态概览").set_defaults(fn=cmd_stats)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
