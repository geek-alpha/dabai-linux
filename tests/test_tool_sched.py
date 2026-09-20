# -*- coding: utf-8 -*-
"""tool_sched 契约测试：分批并行 + 只读缓存。

为什么要单独钉死这份逻辑：
  分批是「只读并行 / 写按资源键串行」正确性的唯一凭据，主智能体与子智能体
  共用它切批次。但它只在工作区快照里存在、从没有单测——改坏任何一条
  （比如把 EXCLUSIVE 判定删了）全库测试仍然全绿，只有线上出现「同一文件
  并行编辑互相覆盖」才会被发现。宁可慢、不可错的边界必须由测试守。

对照纪律：学 Anthropic _dispatch_loop 后验证自家实现，发现大白批内真并行
（Anthropic 反而是单消费者串行），真正缺的是这份实现没被钉死。
"""

import os
import time

import pytest

from harness.tool_sched import (
    EXCLUSIVE,
    ReadCache,
    get_cache,
    has_write_conflict,
    is_readonly,
    plan,
    resource_key,
)

# 下标约定: [(下标, 工具名, 参数)]
RO_A = (0, "code_read", {"path": "/a.py"})
RO_B = (1, "code_read", {"path": "/b.py"})
RO_C = (2, "list_files", {"root": "/tmp"})
W_A = (3, "code_edit", {"file": "/a.py", "new": "x"})
W_B = (4, "code_edit", {"file": "/b.py", "new": "y"})
W_A2 = (5, "code_edit", {"file": "/a.py", "new": "z"})
SHELL = (6, "shell_run", {"command": "pytest"})


# ==================== plan: 批次切分 ====================

def test_empty_and_single():
    assert plan([]) == []
    assert plan([RO_A]) == [[RO_A]]


def test_readonly_all_parallel_in_one_batch():
    batches = plan([RO_A, RO_B, RO_C])
    assert len(batches) == 1
    assert {it[0] for it in batches[0]} == {0, 1, 2}


def test_readonly_always_first_batch():
    batches = plan([W_A, RO_A])
    assert batches[0] == [RO_A]
    assert W_A in batches[1]


def test_writers_same_path_never_same_batch():
    batches = plan([W_A, W_A2, RO_A])
    # 两个同 path 的写必须分属不同批
    wa_batch = next(i for i, b in enumerate(batches) if W_A in b)
    wa2_batch = next(i for i, b in enumerate(batches) if W_A2 in b)
    assert wa_batch != wa2_batch


def test_writers_different_paths_share_batch():
    batches = plan([W_A, W_B])
    for b in batches:
        if W_A in b:
            assert W_B in b  # 不同路径的写可以并行
            break
    else:
        pytest.fail("different-path writers should share a batch")


def test_exclusive_writer_alone():
    batches = plan([RO_A, SHELL])
    for b in batches:
        if SHELL in b:
            assert len(b) == 1  # 独占键不与任何人并行
            break
    else:
        pytest.fail("shell should be scheduled")


def test_each_item_appears_exactly_once():
    pending = [RO_A, RO_B, W_A, W_A2, SHELL]
    batches = plan(pending)
    flat = [it for b in batches for it in b]
    assert sorted(flat) == sorted(pending)


def test_plan_failure_degrades_to_serial(monkeypatch):
    def boom(tool_name, args):
        raise RuntimeError("resource_key exploded")

    monkeypatch.setattr("harness.tool_sched.resource_key", boom)
    pending = [RO_A, W_A, W_A2]
    batches = plan(pending)
    assert batches == [[it] for it in pending]  # 每项单独一批 = 完全串行


# ==================== resource_key: 资源键判定 ====================

def test_key_normalizes_path():
    # 绝对化归一：相对路径与绝对路径同文件同键（normcase 在 Linux 是 no-op，只验 abspath）
    assert resource_key("code_edit", {"file": "a.py"}) == resource_key(
        "code_edit", {"file": os.path.abspath("a.py")})


def test_key_uses_any_path_alias():
    k1 = resource_key("code_edit", {"target": "/x.py"})
    k2 = resource_key("code_edit", {"path": "/x.py"})
    assert k1 == k2 == ("path", os.path.normcase("/x.py"))


def test_key_multiple_paths_sorted_tuple():
    k = resource_key("code_patch", {"files": "/z.py\n/a.py"})
    assert k == ("paths", (os.path.normcase("/a.py"), os.path.normcase("/z.py")))


def test_key_unknown_scope_is_exclusive():
    assert resource_key("shell_run", {"command": "ls"}) == EXCLUSIVE
    assert resource_key("code_edit", None) == EXCLUSIVE
    assert resource_key("code_edit", "not-a-dict") == EXCLUSIVE


# ==================== has_write_conflict: 是否有必须串行的写 ====================

def test_conflict_no_writers():
    assert has_write_conflict([RO_A, RO_B]) is False


def test_conflict_single_writer():
    assert has_write_conflict([RO_A, W_A]) is False


def test_conflict_same_key():
    assert has_write_conflict([W_A, W_A2]) is True


def test_conflict_exclusive_writer():
    assert has_write_conflict([RO_A, SHELL, W_A]) is True


def test_conflict_different_keys():
    assert has_write_conflict([W_A, W_B]) is False


# ==================== ReadCache: 只读结果缓存 ====================

def test_cache_only_success():
    c = ReadCache()
    c.put("code_read", {"path": "/a.py"}, "内容", False)
    assert c.get("code_read", {"path": "/a.py"}) is None


def test_cache_skips_non_cacheable():
    c = ReadCache()
    c.put("shell_run", {"command": "ls"}, "out", True)
    assert c.get("shell_run", {"command": "ls"}) is None


def test_cache_search_like_invalidated_by_epoch():
    c = ReadCache()
    c.put("code_search", {"query": "def foo"}, "hit", True)
    assert c.get("code_search", {"query": "def foo"}) == ("hit", True)
    c.invalidate()  # 写操作后无 path 条目立即失效
    assert c.get("code_search", {"query": "def foo"}) is None


def test_cache_file_like_invalidated_by_mtime(tmp_path):
    f = tmp_path / "f.py"
    f.write_text("v1", encoding="utf-8")
    c = ReadCache(ttl=300)
    c.put("code_read", {"path": str(f)}, "v1-content", True)
    assert c.get("code_read", {"path": str(f)}) == ("v1-content", True)
    # 外部改了文件 → mtime 签名变化 → 缓存自动失效，不会返回过期内容
    f.write_text("v2", encoding="utf-8")
    st = os.stat(f)
    os.utime(f, (st.st_atime + 5, st.st_mtime + 5))
    assert c.get("code_read", {"path": str(f)}) is None


def test_cache_epoch_starts_zero_and_advances():
    c = ReadCache()
    assert c.epoch == 0
    c.invalidate()
    assert c.epoch == 1


def test_cache_ttl_expiry():
    c = ReadCache(ttl=0.1)
    c.put("code_read", {"path": "/nope.py"}, "x", True)
    time.sleep(0.15)
    assert c.get("code_read", {"path": "/nope.py"}) is None


def test_cache_hit_stats():
    c = ReadCache()
    c.put("code_read", {"path": "/a.py"}, "x", True)
    assert c.get("code_read", {"path": "/a.py"}) == ("x", True)
    c.get("code_read", {"path": "/nope.py"})
    s = c.stats()
    assert s["hits"] == 1 and s["misses"] == 1
    assert s["hit_rate"] == 0.5


# ==================== 只读名单 ====================

def test_readonly_known_tools():
    assert is_readonly("code_read") is True
    assert is_readonly("read_lines") is True
    assert is_readonly("code_edit") is False
    assert is_readonly("shell_run") is False


def test_readonly_unknown_tool_conservative():
    assert is_readonly("brand_new_tool_999") is False  # 未知一律视为写


def test_singleton_cache_shared():
    assert get_cache() is get_cache()
