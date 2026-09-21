# -*- coding: utf-8 -*-
"""回归：数据库文件损坏（NOTADB）时 _get_db 必须丢掉坏连接重建。

曾经的写法是两个缺陷叠加，坏连接永远重建不了：
  ① 探活用 SELECT 1 —— 常量表达式，SQLite 不碰数据库文件就能回答，NOTADB 探不出来；
  ② except 只捕 OperationalError/ProgrammingError 子类，而 NOTADB 抛的是基类 DatabaseError。
"""

import sqlite3
import threading

import memory


def test_get_db_rebuilds_after_notadb(tmp_path, monkeypatch):
    broken = tmp_path / "broken.db"
    broken.write_bytes(b"this is definitely not a sqlite database" * 8)
    good = tmp_path / "good.db"

    monkeypatch.setattr(memory, "DB_PATH", good)
    monkeypatch.setattr(memory, "_db_local", threading.local())
    memory._db_local.conn = sqlite3.connect(str(broken))

    conn = memory._get_db()

    assert conn is not None
    assert conn.execute("SELECT count(*) FROM sqlite_master").fetchone()[0] == 0
    assert str(conn.execute("PRAGMA database_list").fetchone()[2]) == str(good)


def test_get_db_reuses_healthy_connection(tmp_path, monkeypatch):
    monkeypatch.setattr(memory, "DB_PATH", tmp_path / "good.db")
    monkeypatch.setattr(memory, "_db_local", threading.local())

    first = memory._get_db()

    assert memory._get_db() is first
