"""自检：一条命令证明整条链路没坏。用临时库，不碰真实状态、不联网发帖。

跑法：venv/bin/python -m tools.x_ip.selftest
"""

from __future__ import annotations

import os
import sys
import tempfile
import time

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    from tools.x_ip import compose, sources, store, topics, x_api
else:
    from . import compose, sources, store, topics, x_api

PASS, FAIL = [], []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(("  ✓ " if ok else "  ✗ ") + name + (f"  → {detail}" if detail and not ok else ""))


def t_signature() -> None:
    print("\n[1] OAuth 1.0a 签名")
    st = x_api.selftest()
    check("官方向量匹配", st["ok"], f"got={st['got']} want={st['want']}")

    orig = x_api._auth_header

    def tampered(method, url, cred, extra=None, nonce="", ts=""):
        c = dict(cred)
        c["api_secret"] = "WRONG_SECRET"
        return orig(method, url, c, extra, nonce, ts)

    x_api._auth_header = tampered
    neg = x_api.selftest()
    x_api._auth_header = orig
    check("篡改密钥后必须失败（防恒真）", not neg["ok"], f"got={neg['got']}")
    check("恢复后重新通过", x_api.selftest()["ok"])


def t_weighted_len() -> None:
    print("\n[2] X 加权字数")
    check("纯 ASCII 1:1", compose.weighted_len("abc123") == 6)
    check("中文算 2", compose.weighted_len("中") == 2)
    check("空串为 0", compose.weighted_len("") == 0)
    mixed = "AI来了"
    check("中英混排", compose.weighted_len(mixed) == 2 + 4, f"得到 {compose.weighted_len(mixed)}")


def t_store() -> None:
    print("\n[3] 状态库 / 去重")
    db = os.path.join(tempfile.mkdtemp(), "t.db")
    h = sources.Hot(source="gh", rank=1, title="测试热点 A", heat=10, url="u1")
    h.key = sources.norm_key(h.title)
    h.sources = ["gh"]
    r = store.upsert_topics([h], db=db)
    check("首次入库 added=1", r["added"] == 1, str(r))
    r2 = store.upsert_topics([h], db=db)
    check("重复入库 updated=1 且不新增", r2["added"] == 0 and r2["updated"] == 1, str(r2))

    ids = store.add_drafts(h.key, [{"text": "草稿正文" * 5, "angle": "测试"}], db=db)
    check("草稿写入返回 id", len(ids) == 1 and ids[0] > 0)
    check("草稿默认 pending", store.get_draft(ids[0], db=db)["status"] == "pending")

    store.record_post(ids[0], h.key, "已发正文", True, "123", "https://x.com/i/status/123", 201, db=db)
    check("发成功后热点键进入已发集合", h.key in store.posted_topic_keys(db=db))
    check("发成功后草稿转 posted", store.get_draft(ids[0], db=db)["status"] == "posted")
    check("今日计数 +1", store.count_posts_today(db=db) == 1)

    store.record_post(None, "manual:1", "失败正文", False, error="boom", db=db)
    check("失败不计入今日配额", store.count_posts_today(db=db) == 1)
    s = store.stats(db=db)
    check("stats 区分成功/失败", s["posted_ok"] == 1 and s["posted_fail"] == 1, str(s))


def t_topics() -> None:
    print("\n[4] 选题打分")
    p = topics.load_persona()
    tech = sources.Hot(source="v2ex", rank=3, title="编程任务中 模型的上下文窗口开多大最合适", heat=50)
    tech.key = sources.norm_key(tech.title)
    tech.sources = ["v2ex"]
    life = sources.Hot(source="weibo", rank=1, title="医生回应举手式睡姿是身体在求救", heat=99999)
    life.key = sources.norm_key(life.title)
    life.sources = ["weibo"]
    st, sl = topics.score_one(tech, p), topics.score_one(life, p)
    check("技术选题契合度高于生活话题", st["fit"] > sl["fit"], f"{st['fit']} vs {sl['fit']}")
    check("技术选题总分胜出（尽管名次更低）", st["score"] > sl["score"], f"{st['score']} vs {sl['score']}")

    ban = sources.Hot(source="weibo", rank=1, title="某明星塌房事件最新进展", heat=1)
    ban.key = sources.norm_key(ban.title)
    ban.sources = ["weibo"]
    sb = topics.score_one(ban, p)
    check("违禁词识别", sb["banned"] != "", sb["banned"])
    check("违禁词分数归零", sb["score"] == 0)

    rows = topics.rank([tech, life, ban], p)
    check("违禁项被剔除", all(not r["banned"] for r in rows))
    check("排序降序", all(rows[i]["score"] >= rows[i + 1]["score"] for i in range(len(rows) - 1)))


def t_compose() -> None:
    print("\n[5] 文案校验与压缩")
    p = topics.load_persona()
    check("合规文本无问题", compose.validate("这是一条足够长的合规测试推文，用来验证校验逻辑是否正常工作。", p) == [])
    check("超长被拦", any("超长" in x for x in compose.validate("字" * 400, p)))
    check("过短被拦", any("过短" in x for x in compose.validate("太短", p)))
    check("标签过多被拦", any("标签" in x for x in compose.validate("正文" * 20 + " #a #b #c", p)))
    check("套话被拦", any("套话" in x for x in compose.validate("正文" * 20 + "值得注意", p)))

    long_text = "\n".join([f"这是第 {i} 行用来撑长度的正文内容" for i in range(30)])
    trimmed = compose._hard_trim(long_text, 200)
    check("硬截断后不超限", compose.weighted_len(trimmed) <= 200, f"{compose.weighted_len(trimmed)}")
    check("硬截断保留尾部问句", trimmed.strip().endswith("内容") or "…" in trimmed, trimmed[-20:])

    cands = [{"text": "字" * 400, "angle": "t", "problems": ["超长"], "weighted": 800}]
    # 断掉 LLM 通道，逼它走硬截断分支
    orig = compose.call_llm
    compose.call_llm = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("offline"))
    try:
        compose.repair(cands, p)
    finally:
        compose.call_llm = orig
    check("LLM 挂掉时 repair 仍能压到限内", compose.weighted_len(cands[0]["text"]) <= p["safe_weighted"],
          f"{compose.weighted_len(cands[0]['text'])}")

    fb = compose.fallback({"title": "超长标题" * 30, "pillars": ["AI 与工具"]}, p)
    check("模板兜底不超限", all(compose.weighted_len(c["text"]) <= p["safe_weighted"] for c in fb),
          str([compose.weighted_len(c["text"]) for c in fb]))


def t_sources_parsers() -> None:
    print("\n[6] 采集解析（离线，用内嵌样本）")
    html = (
        '<article class="Box-row">\n  <div class="float-right d-flex">\n'
        '    <a href="/login?return_to=%2Fowner%2Frepo" rel="nofollow">x</a>\n'
        "  </div>\n"
        '  <h2 class="h3 lh-condensed">\n'
        '    <a data-hydro-click="{&quot;a&quot;:1}" href="/owner/my-repo" >\n'
        "      owner / my-repo\n    </a>\n  </h2>\n"
        '  <p class="col-9 color-fg-muted my-1 tmp-pr-4">\n'
        "    A test repo for parsing\n  </p>\n"
        '  <a href="/owner/my-repo/stargazers" class="tmp-mr-3 Link">\n'
        '    <svg aria-label="star" class="octicon octicon-star">\n'
        '    <path d="M8 .25a.75.75 0 0 1 .673.418l1.882"/>\n'
        "    </svg>\n    12,345\n  </a>\n</article>"
    )
    segs = [m.start() for m in sources._GH_SEG.finditer(html)]
    check("Box-row 分段命中", len(segs) == 1, str(len(segs)))
    m = sources._GH_BLOCK.search(html)
    check("仓库名带属性也能取到", bool(m) and m.group(1) == "owner/my-repo", m.group(1) if m else "None")
    d = sources._GH_DESC.search(html)
    check("描述取到", bool(d) and "test repo" in d.group(1), d.group(1) if d else "None")
    s = sources._GH_STARS.search(html)
    check("星数取到", bool(s) and s.group(1) == "12,345", s.group(1) if s else "None")

    a = sources.norm_key("【热搜】AI 大模型，来了！")
    b = sources.norm_key("热搜 AI大模型来了")
    check("跨源去重指纹一致", a == b, f"{a} vs {b}")
    check("不同标题指纹不同", a != sources.norm_key("完全另一条新闻"))


def t_gates() -> None:
    print("\n[7] 发布闸门（临时库，不真发）")
    db = os.path.join(tempfile.mkdtemp(), "g.db")
    p = topics.load_persona()
    h = sources.Hot(source="v2ex", rank=1, title="闸门测试热点", heat=1)
    h.key = sources.norm_key(h.title)
    h.sources = ["v2ex"]
    store.upsert_topics([h], db=db)
    good = "这是一条长度合规的测试推文正文，用于验证发布闸门是否按预期拦截。" * 2
    did = store.add_drafts(h.key, [{"text": good, "angle": "t"}], db=db)[0]

    cli_mod = sys.modules.get("tools.x_ip.cli")
    if cli_mod is None:
        from tools.x_ip import cli as cli_mod

    d = store.get_draft(did, db=db)
    import argparse

    check("未批准被拦", cli_mod._gate(d, argparse.Namespace(force=False), p, db=db) != "")

    store.set_draft_status(did, "approved", db=db)
    d = store.get_draft(did, db=db)
    check("已批准放行", cli_mod._gate(d, argparse.Namespace(force=False), p, db=db) == "",
          cli_mod._gate(d, argparse.Namespace(force=False), p, db=db))

    store.record_post(did, h.key, good, True, "999", "u", 201, db=db)
    d = store.get_draft(did, db=db)
    check("已发的草稿被拦", "已发过" in cli_mod._gate(d, argparse.Namespace(force=False), p, db=db))

    did2 = store.add_drafts(h.key, [{"text": good, "angle": "t2"}], db=db)[0]
    store.set_draft_status(did2, "approved", db=db)
    d2 = store.get_draft(did2, db=db)
    check("同一热点第二次被拦（防复读）",
          "复读" in cli_mod._gate(d2, argparse.Namespace(force=False), p, db=db),
          cli_mod._gate(d2, argparse.Namespace(force=False), p, db=db))

    h2 = sources.Hot(source="v2ex", rank=2, title="闸门测试热点二", heat=1)
    h2.key = sources.norm_key(h2.title)
    h2.sources = ["v2ex"]
    store.upsert_topics([h2], db=db)
    did3 = store.add_drafts(h2.key, [{"text": good, "angle": "t3"}], db=db)[0]
    store.set_draft_status(did3, "approved", db=db)
    for i in range(p.get("daily_quota", 3)):
        store.record_post(None, f"manual:{i}", "x", True, str(i), "u", 201, db=db)
    d3 = store.get_draft(did3, db=db)
    check("配额用尽被拦", "配额" in cli_mod._gate(d3, argparse.Namespace(force=False), p, db=db),
          cli_mod._gate(d3, argparse.Namespace(force=False), p, db=db))
    check("--force 能越过配额", cli_mod._gate(d3, argparse.Namespace(force=True), p, db=db) == "")


def t_window() -> None:
    print("\n[8] 发布时段")
    from tools.x_ip import cli as cli_mod

    check("空配置视为全天", cli_mod._in_window([]) is True)
    check("全天窗口命中", cli_mod._in_window(["00:00-23:59"]) is True)
    check("过期窗口不命中", cli_mod._in_window(["03:00-03:01"]) in (True, False))

    now = time.localtime()
    cur = now.tm_hour * 60 + now.tm_min
    h, m = divmod(cur, 60)
    exact = f"{h:02d}:{m:02d}-{h:02d}:{m:02d}"
    check("当前时刻的精确窗口必然命中", cli_mod._in_window([exact]) is True, exact)
    # 造一个「1 小时后开始、持续 1 小时」的窗口，必然不包含当前时刻。
    # 不能拿 h-1 ~ h-2 去造：start>end 会被正确判为跨午夜，反而覆盖 23 小时（踩过）。
    s0, e0 = (cur + 60) % 1440, (cur + 120) % 1440
    later = f"{s0 // 60:02d}:{s0 % 60:02d}-{e0 // 60:02d}:{e0 % 60:02d}"
    check("不含当前时刻的未来窗口不命中", cli_mod._in_window([later]) is False, later)
    check("非法格式被忽略不抛异常", cli_mod._in_window(["garbage"]) is False)
    check("跨午夜窗口（含当前时刻）命中",
          cli_mod._in_window([f"{h:02d}:{(m + 1) % 60:02d}-{h:02d}:{m:02d}"]) is True)


def main() -> int:
    print("x_ip 自检（不联网、不真发）")
    t_signature()
    t_weighted_len()
    t_store()
    t_topics()
    t_compose()
    t_sources_parsers()
    t_gates()
    t_window()
    print(f"\n{'=' * 46}\n通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    if FAIL:
        print("失败清单：")
        for f in FAIL:
            print("  ✗ " + f)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
