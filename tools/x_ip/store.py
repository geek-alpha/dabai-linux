"""状态库：选题 / 草稿 / 已发 三张表。

设计要点：
- topics.key 用 norm_key 做主键 → 同一条热点反复采集只更新不新增，天然防重复。
- drafts/posts 分开 → 一条草稿可能改多版、发失败可重试，发布记录独立留痕。
- 发布前查 posts.topic_key → 同一热点绝不发第二次（IP 最忌讳复读）。
"""

from __future__ import annotations

import os
import sqlite3
import time
from contextlib import contextmanager
from typing import Any

DEFAULT_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "x_ip.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS topics (
    key         TEXT PRIMARY KEY,
    title       TEXT NOT NULL,
    source      TEXT,
    sources     TEXT,
    rank        INTEGER DEFAULT 99,
    heat        INTEGER DEFAULT 0,
    score       REAL DEFAULT 0,
    url         TEXT,
    first_seen  REAL,
    last_seen   REAL,
    status      TEXT DEFAULT 'new'      -- new | drafted | posted | skipped
);
CREATE INDEX IF NOT EXISTS idx_topics_score ON topics(score DESC);

CREATE TABLE IF NOT EXISTS drafts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    topic_key   TEXT NOT NULL,
    text        TEXT NOT NULL,
    angle       TEXT,
    model       TEXT,
    created     REAL,
    status      TEXT DEFAULT 'pending'  -- pending | approved | rejected | posted
);
CREATE INDEX IF NOT EXISTS idx_drafts_status ON drafts(status);

CREATE TABLE IF NOT EXISTS posts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    draft_id    INTEGER,
    topic_key   TEXT,
    text        TEXT,
    tweet_id    TEXT,
    tweet_url   TEXT,
    http_status INTEGER,
    ok          INTEGER DEFAULT 0,
    error       TEXT,
    posted_at   REAL,
    raw         TEXT
);
CREATE INDEX IF NOT EXISTS idx_posts_topic ON posts(topic_key);
"""


@contextmanager
def conn(db: str = DEFAULT_DB):
    c = sqlite3.connect(db, timeout=15)
    c.row_factory = sqlite3.Row
    try:
        yield c
        c.commit()
    finally:
        c.close()


def init(db: str = DEFAULT_DB) -> None:
    with conn(db) as c:
        c.executescript(SCHEMA)


def upsert_topics(items: list[Any], db: str = DEFAULT_DB) -> dict[str, int]:
    """采集结果入库。返回 {新增, 更新, 总数}。"""
    init(db)
    now = time.time()
    added = updated = 0
    with conn(db) as c:
        for it in items:
            row = c.execute("SELECT key FROM topics WHERE key=?", (it.key,)).fetchone()
            if row:
                c.execute(
                    "UPDATE topics SET last_seen=?, rank=?, heat=?, score=?, sources=?"
                    " WHERE key=?",
                    (now, it.rank, it.heat, it.score(), ",".join(it.sources), it.key),
                )
                updated += 1
            else:
                c.execute(
                    "INSERT INTO topics(key,title,source,sources,rank,heat,score,url,"
                    "first_seen,last_seen,status) VALUES(?,?,?,?,?,?,?,?,?,?,'new')",
                    (
                        it.key,
                        it.title,
                        it.source,
                        ",".join(it.sources),
                        it.rank,
                        it.heat,
                        it.score(),
                        it.url,
                        now,
                        now,
                    ),
                )
                added += 1
    return {"added": added, "updated": updated, "total": added + updated}


def top_topics(limit: int = 15, statuses: tuple[str, ...] = ("new",), db: str = DEFAULT_DB) -> list[dict]:
    init(db)
    ph = ",".join("?" * len(statuses))
    with conn(db) as c:
        rows = c.execute(
            f"SELECT * FROM topics WHERE status IN ({ph}) ORDER BY score DESC LIMIT ?",
            (*statuses, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def get_topic(key: str, db: str = DEFAULT_DB) -> dict | None:
    init(db)
    with conn(db) as c:
        r = c.execute("SELECT * FROM topics WHERE key=?", (key,)).fetchone()
    return dict(r) if r else None


def set_topic_status(key: str, status: str, db: str = DEFAULT_DB) -> None:
    with conn(db) as c:
        c.execute("UPDATE topics SET status=? WHERE key=?", (status, key))


def add_drafts(topic_key: str, drafts: list[dict], model: str = "", db: str = DEFAULT_DB) -> list[int]:
    init(db)
    now = time.time()
    ids = []
    with conn(db) as c:
        for d in drafts:
            cur = c.execute(
                "INSERT INTO drafts(topic_key,text,angle,model,created,status)"
                " VALUES(?,?,?,?,?,'pending')",
                (topic_key, d["text"], d.get("angle", ""), model, now),
            )
            ids.append(cur.lastrowid)
        c.execute("UPDATE topics SET status='drafted' WHERE key=?", (topic_key,))
    return ids


def list_drafts(status: str = "pending", limit: int = 30, db: str = DEFAULT_DB) -> list[dict]:
    init(db)
    with conn(db) as c:
        rows = c.execute(
            "SELECT d.*, t.title AS topic_title, t.url AS topic_url FROM drafts d"
            " LEFT JOIN topics t ON t.key=d.topic_key"
            " WHERE d.status=? ORDER BY d.id DESC LIMIT ?",
            (status, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def get_draft(draft_id: int, db: str = DEFAULT_DB) -> dict | None:
    init(db)
    with conn(db) as c:
        r = c.execute(
            "SELECT d.*, t.title AS topic_title FROM drafts d"
            " LEFT JOIN topics t ON t.key=d.topic_key WHERE d.id=?",
            (draft_id,),
        ).fetchone()
    return dict(r) if r else None


def set_draft_status(draft_id: int, status: str, db: str = DEFAULT_DB) -> None:
    with conn(db) as c:
        c.execute("UPDATE drafts SET status=? WHERE id=?", (status, draft_id))


def update_draft_text(draft_id: int, text: str, db: str = DEFAULT_DB) -> None:
    with conn(db) as c:
        c.execute("UPDATE drafts SET text=? WHERE id=?", (text, draft_id))


def posted_topic_keys(db: str = DEFAULT_DB) -> set[str]:
    init(db)
    with conn(db) as c:
        rows = c.execute("SELECT DISTINCT topic_key FROM posts WHERE ok=1").fetchall()
    return {r["topic_key"] for r in rows}


def topics_with_open_drafts(db: str = DEFAULT_DB) -> set[str]:
    """已经有待审/已批准草稿的选题——再生成一次就是重复草稿。"""
    init(db)
    with conn(db) as c:
        rows = c.execute(
            "SELECT DISTINCT topic_key FROM drafts WHERE status IN ('pending','approved')"
        ).fetchall()
    return {r["topic_key"] for r in rows}


def count_posts_today(db: str = DEFAULT_DB) -> int:
    init(db)
    start = time.time() - (time.time() % 86400)
    with conn(db) as c:
        r = c.execute("SELECT COUNT(*) n FROM posts WHERE ok=1 AND posted_at>=?", (start,)).fetchone()
    return int(r["n"])


def record_post(
    draft_id: int | None,
    topic_key: str,
    text: str,
    ok: bool,
    tweet_id: str = "",
    tweet_url: str = "",
    http_status: int = 0,
    error: str = "",
    raw: str = "",
    db: str = DEFAULT_DB,
) -> int:
    init(db)
    with conn(db) as c:
        cur = c.execute(
            "INSERT INTO posts(draft_id,topic_key,text,tweet_id,tweet_url,http_status,"
            "ok,error,posted_at,raw) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                draft_id,
                topic_key,
                text,
                tweet_id,
                tweet_url,
                http_status,
                1 if ok else 0,
                error,
                time.time(),
                raw[:4000],
            ),
        )
        pid = cur.lastrowid
        if ok:
            if draft_id:
                c.execute("UPDATE drafts SET status='posted' WHERE id=?", (draft_id,))
            c.execute("UPDATE topics SET status='posted' WHERE key=?", (topic_key,))
    return pid


def stats(db: str = DEFAULT_DB) -> dict:
    init(db)
    with conn(db) as c:
        g = lambda q: c.execute(q).fetchone()[0]  # noqa: E731
        return {
            "topics": g("SELECT COUNT(*) FROM topics"),
            "topics_new": g("SELECT COUNT(*) FROM topics WHERE status='new'"),
            "drafts_pending": g("SELECT COUNT(*) FROM drafts WHERE status='pending'"),
            "posted_ok": g("SELECT COUNT(*) FROM posts WHERE ok=1"),
            "posted_fail": g("SELECT COUNT(*) FROM posts WHERE ok=0"),
            "posted_today": count_posts_today(db),
        }
