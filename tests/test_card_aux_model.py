#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""角色卡片声明侧任务模型（摘要 / 记忆提取）→ 覆盖全局 memory.aux_*。

契约：
  1. 卡片声明 aux_provider_id + aux_model → 用该供应商的地址与密钥
  2. 只声明 aux_model → 用本卡主模型端点（绝不沿用全局 aux_base_url：那是另一家的地址，
     拿它调卡片选的模型会报「模型不存在」）
  3. 卡片完全没声明 → 全局 memory.aux_* 原样保留（服务器级配置不被卡片清掉）
  4. aux_provider_id 指向已删除的供应商 → 退回本卡主模型端点，不炸
  5. 卡片只存「供应商 id + 模型名」，地址密钥一律取自全局注册表
"""
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

import agent as A  # noqa: E402

PROVIDERS = [
    {"id": "p-main", "name": "主供应商", "kind": "custom",
     "base_url": "https://main.example/v1", "api_key": "k-main", "default_model": "main-m"},
    {"id": "p-cheap", "name": "便宜供应商", "kind": "custom",
     "base_url": "https://cheap.example/v1", "api_key": "k-cheap", "default_model": "cheap-m"},
]

EMPTY_MEM = {"aux_model": "", "aux_base_url": "", "aux_api_key": ""}


def _cfg(mem=None):
    return {
        "base_url": "https://main.example/v1",
        "api_key": "k-main",
        "model": "main-m",
        "llm_providers": [dict(p) for p in PROVIDERS],
        "memory": dict(mem if mem is not None else EMPTY_MEM),
    }


def _patch(monkeypatch, card, mem=None):
    # 每次现造一份 cfg：load_config_for 会就地写 memory，共享同一 dict 会跨用例串味
    monkeypatch.setattr(A, "load_config", lambda: _cfg(mem))
    monkeypatch.setattr(A, "role_card_of", lambda uid: card)


def test_卡片声明供应商与模型时用该供应商端点(monkeypatch):
    _patch(monkeypatch, {"llm": {"provider_id": "p-main", "model": "main-m",
                                 "aux_provider_id": "p-cheap", "aux_model": "cheap-m"}})
    cfg = A.load_config_for("u1")
    assert cfg["memory"]["aux_model"] == "cheap-m"
    assert cfg["memory"]["aux_base_url"] == "https://cheap.example/v1"
    assert cfg["memory"]["aux_api_key"] == "k-cheap"


def test_只给模型时用本卡主模型端点且不沿用全局aux端点(monkeypatch):
    _patch(monkeypatch,
           {"llm": {"provider_id": "p-main", "model": "main-m", "aux_model": "cheap-m"}},
           mem={"aux_model": "stale-global", "aux_base_url": "https://stale.example/v1",
                "aux_api_key": "k-stale"})
    cfg = A.load_config_for("u1")
    assert cfg["memory"]["aux_model"] == "cheap-m"
    assert cfg["memory"]["aux_base_url"] == "https://main.example/v1"
    assert cfg["memory"]["aux_api_key"] == "k-main"


def test_卡片未声明aux时全局配置原样保留(monkeypatch):
    _patch(monkeypatch, {"llm": {"provider_id": "p-main", "model": "main-m"}},
           mem={"aux_model": "global-cheap", "aux_base_url": "https://g.example/v1",
                "aux_api_key": "k-g"})
    cfg = A.load_config_for("u1")
    assert cfg["memory"]["aux_model"] == "global-cheap"
    assert cfg["memory"]["aux_base_url"] == "https://g.example/v1"
    assert cfg["memory"]["aux_api_key"] == "k-g"


def test_供应商id失效时退回本卡主模型端点(monkeypatch):
    _patch(monkeypatch, {"llm": {"provider_id": "p-main", "model": "main-m",
                                 "aux_provider_id": "p-ghost", "aux_model": "cheap-m"}})
    cfg = A.load_config_for("u1")
    assert cfg["memory"]["aux_model"] == "cheap-m"
    assert cfg["memory"]["aux_base_url"] == "https://main.example/v1"
    assert cfg["memory"]["aux_api_key"] == "k-main"


def test_卡片没有llm段时不动全局(monkeypatch):
    _patch(monkeypatch, {"name": "空卡"})
    cfg = A.load_config_for("u1")
    assert cfg["memory"]["aux_model"] == ""


def test_清空aux模型即退回全局(monkeypatch):
    _patch(monkeypatch, {"llm": {"aux_provider_id": "p-cheap", "aux_model": ""}},
           mem={"aux_model": "global-cheap", "aux_base_url": "https://g.example/v1",
                "aux_api_key": "k-g"})
    cfg = A.load_config_for("u1")
    assert cfg["memory"]["aux_model"] == "global-cheap"
    assert cfg["memory"]["aux_base_url"] == "https://g.example/v1"


def test_卡片声明的aux能建出侧任务链路(monkeypatch):
    """配置落地到链路：光有 memory 字段不算数，_build_aux_llm 得真建出 client。"""
    _patch(monkeypatch, {"llm": {"provider_id": "p-main",
                                 "aux_provider_id": "p-cheap", "aux_model": "cheap-m"}})
    cfg = A.load_config_for("u1")
    got = A._build_aux_llm(cfg)
    assert got is not None, "卡片声明的 aux 应当能建出链路"
    client, model = got
    assert model == "cheap-m"
    assert "cheap.example" in str(client.base_url)


def test_归一化保留aux字段并去空白():
    import server
    out = server._normalize_card_llm({"provider_id": "p1", "model": "m1",
                                      "aux_provider_id": " p2 ", "aux_model": " cheap "})
    assert out["aux_provider_id"] == "p2"
    assert out["aux_model"] == "cheap"
    # 卡片不携带地址密钥：供应商注册表是唯一来源
    assert "base_url" not in out and "api_key" not in out


def test_归一化缺字段时给空串():
    import server
    out = server._normalize_card_llm({})
    assert out["aux_provider_id"] == "" and out["aux_model"] == ""
