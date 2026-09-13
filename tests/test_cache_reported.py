#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""前缀缓存口径标记（cache_reported）的回归测试。

背景（2026-09-14）：turn_metrics 里 prompt_tokens 合计 126.0M，而
cache_hit + cache_miss 只有 118.4M，差 7.6M（6%）。查到缺口 100% 来自 22 个
「整轮 cache 全 0」的轮次：那些 provider 的 usage 对象没有
prompt_cache_hit_tokens / prompt_cache_miss_tokens 字段，被
getattr(u, 字段, 0) 静默兜底成 0。

于是 0 混了两个意思——「命中真的是 0」和「这家根本没这个口径」。成本模型拿
它算钱，两种情况下方向相反（本地推理本不该进账，被算成了全 miss 的高价）。

修法：_cache_field_present 单独判存在性，metrics 另记 cache_reported 一列。
数值读取（_read_cache）保持不变，避免影响已有成本统计的连续性。

本测试锁的就是这条语义：一旦有人为省事把存在性判据改回 getattr(..., 0)，
present 与「数值为 0」重新合流，这里必须报警。
"""
import sys
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

import agent  # noqa: E402


class _NoCacheField:
    """支持 prompt_tokens 但完全没有前缀缓存口径（Ollama 这类本地后端）。"""

    prompt_tokens = 8000
    completion_tokens = 100
    total_tokens = 8100


class _ZeroCacheField:
    """回传了缓存字段，且两个值都是 0（真·零命中，但口径存在）。"""

    prompt_cache_hit_tokens = 0
    prompt_cache_miss_tokens = 0


class _FullCacheField:
    prompt_cache_hit_tokens = 1234
    prompt_cache_miss_tokens = 56


class _HitOnly:
    prompt_cache_hit_tokens = 700


class _MissOnly:
    prompt_cache_miss_tokens = 700


class _NoneValued:
    """字段在、值为 None —— 判据认的是「有数值」，None 不携带信息。"""

    prompt_cache_hit_tokens = None
    prompt_cache_miss_tokens = None


# ---------- _cache_field_present：存在性判据 ----------

def test_未取到usage视为无口径():
    assert agent.AIAgent._cache_field_present(None) is False


def test_对象无缓存字段为无口径():
    assert agent.AIAgent._cache_field_present(_NoCacheField()) is False


def test_字段为零仍算有口径():
    """核心用例：0 是「命中为零」，不是「没这个字段」。"""
    assert agent.AIAgent._cache_field_present(_ZeroCacheField()) is True


def test_字段有值算有口径():
    assert agent.AIAgent._cache_field_present(_FullCacheField()) is True


def test_单侧字段命中即算有口径():
    """OR 语义：只回传 hit 或只回传 miss 的后端也要认出来。"""
    assert agent.AIAgent._cache_field_present(_HitOnly()) is True
    assert agent.AIAgent._cache_field_present(_MissOnly()) is True


def test_字段值为None视为无口径():
    """null 不携带数值，据此声明「这家有该口径」是过度自信；_read_cache 同样归零。"""
    assert agent.AIAgent._cache_field_present(_NoneValued()) is False


# ---------- _read_cache：数值读取行为不变 ----------

def test_读数值_无口径返回零零():
    assert agent.AIAgent._read_cache(_NoCacheField()) == (0, 0)


def test_读数值_零口径返回零零():
    assert agent.AIAgent._read_cache(_ZeroCacheField()) == (0, 0)


def test_读数值_有值原样返回():
    assert agent.AIAgent._read_cache(_FullCacheField()) == (1234, 56)


def test_读数值_None入参返回零零():
    assert agent.AIAgent._read_cache(None) == (0, 0)


def test_读数值_返回int():
    assert all(isinstance(x, int) for x in agent.AIAgent._read_cache(_FullCacheField()))


# ---------- 两者的分工：数值可以相同，口径必须能分开 ----------

def test_无口径与零命中数值相同但口径可区分():
    """这就是整个补丁存在的理由。

    两组数据的 _read_cache 完全一样（0, 0），若只看数值就必然把「本地后端
    没口径」误算成「全 miss 的高价调用」——成本模型方向反了。
    """
    no_field, zero_field = _NoCacheField(), _ZeroCacheField()
    assert agent.AIAgent._read_cache(no_field) == agent.AIAgent._read_cache(zero_field)
    assert agent.AIAgent._cache_field_present(no_field) is False
    assert agent.AIAgent._cache_field_present(zero_field) is True


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
