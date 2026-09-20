#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""侧任务（摘要生成 / 记忆提取）可选独立链路。

背景：摘要每 10 条消息一次、记忆提取每轮一次，都走主模型——高频但低价值。
配了 memory.aux_* 就分流到便宜模型，主对话链路不受影响；不配则与旧版逐字节一致。

契约：
  1. 未配置 aux → model 与 client 全是主链路（旧行为不变）
  2. 配了 aux → memory 渠道调用走 aux 的 model 与 client
  3. aux 失败 → 回退主链路把这次调用做完，并把 aux 停用（下次直接主链路）
     —— 模型名写错不该让摘要永久塌成关键词拼接
  4. 半配置（缺 model 或缺 client）→ 忽略 aux，绝不半启用
  5. _build_aux_llm：model 与 base_url 都给了才启用，密钥缺省沿用主链路
"""
import asyncio
import sys
import types
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

import memory as M  # noqa: E402

MSG = [{"role": "user", "content": "hi"}]


class _FakeCompletions:
    def __init__(self, results=()):
        self.calls = []
        self.results = list(results)

    async def create(self, **kw):
        self.calls.append(kw)
        r = self.results.pop(0) if self.results else "ok"
        if isinstance(r, Exception):
            raise r
        return r


def _client(results=()):
    return types.SimpleNamespace(
        chat=types.SimpleNamespace(completions=_FakeCompletions(results)))


class _FakeRuntime:
    async def supervise_llm(self, kind, fn):
        return await fn()


def _use_harness(monkeypatch, runtime=None):
    import harness
    monkeypatch.setattr(harness, "get_harness",
                        lambda: types.SimpleNamespace(runtime=runtime))


def _mem(main_client, aux=None, main_model="main-model"):
    m = M.ChatMemory(user_id="aux-test")
    m.set_llm_client(main_client, main_model, aux=aux)
    return m


def _call(m, model="main-model"):
    return asyncio.run(m._supervised_create(model=model, messages=MSG))


def test_默认不配aux时全走主链路(monkeypatch):
    _use_harness(monkeypatch, _FakeRuntime())
    main = _client()
    m = _mem(main)
    assert m._aux_client is None and m._aux_model is None
    assert _call(m) == "ok"
    assert len(main.chat.completions.calls) == 1
    assert main.chat.completions.calls[0]["model"] == "main-model"


def test_配了aux则侧任务走aux(monkeypatch):
    _use_harness(monkeypatch, _FakeRuntime())
    main, aux = _client(), _client()
    m = _mem(main, aux={"client": aux, "model": "cheap-model"})
    assert _call(m) == "ok"
    assert main.chat.completions.calls == [], "配了 aux 主链路不该被调用"
    assert aux.chat.completions.calls[0]["model"] == "cheap-model"


def test_harness不可用时aux仍生效(monkeypatch):
    _use_harness(monkeypatch, None)
    main, aux = _client(), _client()
    m = _mem(main, aux={"client": aux, "model": "cheap-model"})
    assert _call(m) == "ok"
    assert aux.chat.completions.calls[0]["model"] == "cheap-model"
    assert main.chat.completions.calls == []


def test_aux失败回退主链路并就地停用(monkeypatch):
    _use_harness(monkeypatch, _FakeRuntime())
    main = _client()
    aux = _client([RuntimeError("model typo-model not found")])
    m = _mem(main, aux={"client": aux, "model": "typo-model"})

    assert _call(m) == "ok", "aux 失败要把这次调用做完，不能直接掉进关键词降级"
    assert main.chat.completions.calls[0]["model"] == "main-model"
    assert m._aux_broken is True, "失败过的 aux 必须停用，否则每轮白付一次失败调用"

    assert _call(m) == "ok"
    assert len(aux.chat.completions.calls) == 1, "停用后不该再碰 aux"
    assert len(main.chat.completions.calls) == 2


def test_aux失败且主链路也失败则抛出(monkeypatch):
    """回退不是万能兜底：两条链路都挂时必须抛出，让上层走既有降级路径。"""
    _use_harness(monkeypatch, _FakeRuntime())
    main = _client([RuntimeError("main down")])
    aux = _client([RuntimeError("aux down")])
    m = _mem(main, aux={"client": aux, "model": "cheap-model"})
    try:
        _call(m)
    except RuntimeError as e:
        assert "main down" in str(e)
    else:
        raise AssertionError("两条链路都失败必须抛出")


def test_主链路失败时不吞异常(monkeypatch):
    """没配 aux 时行为与旧版一致：异常原样抛给 _llm_summarize 的 except。"""
    _use_harness(monkeypatch, _FakeRuntime())
    main = _client([RuntimeError("boom")])
    m = _mem(main)
    try:
        _call(m)
    except RuntimeError as e:
        assert "boom" in str(e)
    else:
        raise AssertionError("未配 aux 时不许吞异常")


def test_半个配置不启用aux():
    main, aux = _client(), _client()
    h1 = _mem(main, aux={"client": aux})
    assert (h1._aux_client, h1._aux_model) == (None, None), "缺 model 不许启用"
    h2 = _mem(main, aux={"model": "cheap"})
    assert (h2._aux_client, h2._aux_model) == (None, None), "缺 client 不许启用"
    h3 = _mem(main, aux={"client": aux, "model": "  "})
    assert (h3._aux_client, h3._aux_model) == (None, None), "空白 model 不许启用"


def test_set_llm_client会重置熔断位():
    main, aux = _client(), _client()
    m = _mem(main, aux={"client": aux, "model": "cheap"})
    m._aux_broken = True
    m.set_llm_client(main, "main-model", aux={"client": aux, "model": "cheap"})
    assert m._aux_broken is False, "换配置/重连后要给 aux 一次重新证明的机会"


def test_build_aux_llm缺字段不启用():
    import agent
    assert agent._build_aux_llm({}) is None
    assert agent._build_aux_llm({"memory": {"aux_model": "m"}}) is None, "只给 model 会在主端点报模型不存在"
    assert agent._build_aux_llm({"memory": {"aux_base_url": "http://x/v1"}}) is None
    assert agent._build_aux_llm({"memory": {"aux_model": "", "aux_base_url": ""}}) is None


def test_build_aux_llm密钥缺省沿用主链路():
    import agent
    cfg = {"api_key": "main-key",
           "memory": {"aux_model": "cheap", "aux_base_url": "http://localhost:9999/v1"}}
    got = agent._build_aux_llm(cfg)
    assert got is not None
    client, model = got
    assert model == "cheap"
    assert str(client.base_url).rstrip("/").endswith("9999/v1")


def test_build_aux_llm无任何密钥不启用(monkeypatch):
    import agent
    monkeypatch.setattr(agent, "_build_llm_client",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("无密钥不该建 client")))
    cfg = {"memory": {"aux_model": "cheap", "aux_base_url": "http://localhost:9999/v1"}}
    assert agent._build_aux_llm(cfg) is None


def test_agent侧任务链路按配置指纹缓存():
    import agent
    a = agent.AIAgent.__new__(agent.AIAgent)
    a._config = {"api_key": "k",
                 "memory": {"aux_model": "cheap", "aux_base_url": "http://localhost:9999/v1"}}
    a._aux_sig = None
    a._aux_cache = None
    first = a._aux_llm()
    assert first is not None and first["model"] == "cheap"
    assert a._aux_llm() is first, "同配置应复用同一个 client，不重建连接池"
    a._config["memory"]["aux_model"] = "cheaper"
    assert a._aux_llm() is not first, "配置变了必须重建"


def test_agent未配置侧任务链路返回None():
    import agent
    a = agent.AIAgent.__new__(agent.AIAgent)
    a._config = {}
    a._aux_sig = None
    a._aux_cache = None
    assert a._aux_llm() is None
