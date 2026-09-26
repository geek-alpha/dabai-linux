# -*- coding: utf-8 -*-
"""exa / tavily 引擎（原生 HTTP 版）的离线回归。

不打网络：把 requests 换掉，只验可离线判定的判据——key 发现顺序、请求体映射、
错误分流（401/429/4xx 各走哪条路）、research 异步轮询。这几处正是「写错一个参数名
只会收到一句 400」的地方，上线前必须钉死。

端点与鉴权口径来自 2026-09-26 的假 key 探针：api.exa.ai/{search,answer,findSimilar}
与 api.tavily.com/{search,extract,research} 全部 401（链路可达、鉴权层可达），鉴权头为 Bearer。
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SKILL_DIR = ROOT / "skills" / "search"
sys.path.insert(0, str(ROOT))
if str(SKILL_DIR) not in sys.path:
    sys.path.insert(0, str(SKILL_DIR))


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


KEYS = _load(SKILL_DIR / "keys_impl.py", "keys_impl_t")
EXA = _load(SKILL_DIR / "exa_impl.py", "exa_impl_t")
TAV = _load(SKILL_DIR / "tavily_impl.py", "tavily_impl_t")


class _Resp:
    def __init__(self, status, payload=None, text=""):
        self.status_code = status
        self._payload = payload
        self.text = text or json.dumps(payload or {}, ensure_ascii=False)

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class _FakeReq:
    """按 call 字典回调的假 requests：既能记调用，也能按 url/参数给不同响应。"""

    def __init__(self, handler):
        self.calls = []
        self._handler = handler

    def _call(self, method, url, payload, headers):
        call = {"method": method, "url": url, "json": payload, "headers": headers}
        self.calls.append(call)
        return self._handler(call)

    def post(self, url, json=None, headers=None, timeout=None, proxies=None):
        return self._call("POST", url, json, headers)

    def request(self, method, url, json=None, headers=None, timeout=None, proxies=None):
        return self._call(method, url, json, headers)


def _wire(monkeypatch, mod, handler, key="test-key"):
    fake = _FakeReq(handler)
    monkeypatch.setattr(mod, "requests", fake)
    monkeypatch.setattr(mod.web_impl, "_proxy_candidates", lambda url: [None])
    monkeypatch.setattr(mod.keys_impl, "find_key", lambda env: key)
    monkeypatch.setattr(mod.keys_impl, "key_source", lambda env: "测试来源")
    return fake


# ---------------------------------------------------------------- key 发现
def test_key_from_env(monkeypatch):
    monkeypatch.setenv("EXA_API_KEY", "exa-from-env")
    monkeypatch.setattr(KEYS, "secret_files", lambda: [])
    assert KEYS.find_key("EXA_API_KEY") == "exa-from-env"
    assert "环境变量" in KEYS.key_source("EXA_API_KEY")


def test_key_from_secret_file(monkeypatch, tmp_path):
    monkeypatch.delenv("EXA_API_KEY", raising=False)
    f = tmp_path / "secrets.env"
    f.write_text("# 手工变量区\nOTHER='x'\nEXA_API_KEY='exa-quoted'\n", encoding="utf-8")
    monkeypatch.setattr(KEYS, "secret_files", lambda: [str(f)])
    assert KEYS.find_key("EXA_API_KEY") == "exa-quoted"
    assert str(f) in KEYS.key_source("EXA_API_KEY")


def test_key_absent_returns_empty(monkeypatch, tmp_path):
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.setattr(KEYS, "secret_files", lambda: [str(tmp_path / "none.env")])
    assert KEYS.find_key("TAVILY_API_KEY") == ""
    assert KEYS.key_source("TAVILY_API_KEY") == "未配置"


def test_missing_key_msg_is_actionable():
    msg = KEYS.missing_key_msg("TAVILY_API_KEY", "https://app.tavily.com/home", "Tavily")
    assert "TAVILY_API_KEY" in msg
    assert "手工变量区" in msg and "export" in msg
    assert "https://app.tavily.com/home" in msg


# ---------------------------------------------------------------- exa
def test_exa_search_payload_and_render(monkeypatch):
    def handler(call):
        return _Resp(200, {"results": [
            {"title": "Attention Is All You Need", "url": "https://arxiv.org/abs/1706.03762",
             "publishedDate": "2017-06-12T00:00:00.000Z", "author": "Vaswani",
             "score": 0.91, "text": "The dominant sequence transduction models..."},
        ], "costDollars": {"total": 0.007}})

    fake = _wire(monkeypatch, EXA, handler)
    out = EXA.exa_search({"query": "transformer paper", "num": 999, "include_domains": "arxiv.org, aclanthology.org"})
    call = fake.calls[0]
    assert call["url"] == "https://api.exa.ai/search"
    assert call["json"]["numResults"] == 25                      # 上限夹紧
    assert call["json"]["includeDomains"] == ["arxiv.org", "aclanthology.org"]
    assert call["json"]["type"] == "auto"
    assert call["headers"]["Authorization"] == "Bearer test-key"
    assert call["headers"]["x-api-key"] == "test-key"            # 两种鉴权头都给，兼容上游切换
    assert "Attention Is All You Need" in out and "0.007" in out


def test_exa_search_without_key_returns_guide(monkeypatch):
    _wire(monkeypatch, EXA, lambda call: _Resp(200, {}), key="")
    out = EXA.exa_search({"query": "x"})
    assert "EXA_API_KEY" in out and "手工变量区" in out


def test_exa_auth_error_is_terminal(monkeypatch):
    fake = _wire(monkeypatch, EXA, lambda call: _Resp(401, {"error": "Invalid API key"}))
    out = EXA.exa_search({"query": "x"})
    assert "鉴权失败" in out and "测试来源" in out
    assert len(fake.calls) == 1                                  # 鉴权错不换代理重试


def test_exa_4xx_not_retried_but_5xx_retried(monkeypatch):
    fake = _wire(monkeypatch, EXA, lambda call: _Resp(400, {"error": "bad body"}))
    assert "拒绝请求" in EXA.exa_search({"query": "x"})
    assert len(fake.calls) == 1

    monkeypatch.setattr(EXA.web_impl, "_proxy_candidates", lambda url: [None, None, None])
    fake2 = _wire(monkeypatch, EXA, lambda call: _Resp(503, text="upstream down"))
    monkeypatch.setattr(EXA.web_impl, "_proxy_candidates", lambda url: [None, None, None])
    out2 = EXA.exa_search({"query": "x"})
    assert "所有代理链都试过" in out2 and len(fake2.calls) == 3


def test_exa_rate_limit_message(monkeypatch):
    _wire(monkeypatch, EXA, lambda call: _Resp(429, text="too many"))
    assert "限流" in EXA.exa_search({"query": "x"})


def test_exa_answer_and_similar(monkeypatch):
    def handler(call):
        if call["url"].endswith("/answer"):
            return _Resp(200, {"answer": "42", "citations": [{"title": "T", "url": "https://a.b"}]})
        return _Resp(200, {"results": [{"title": "similar page", "url": "https://c.d"}]})

    fake = _wire(monkeypatch, EXA, handler)
    out = EXA.exa_answer({"question": "答案是什么"})
    assert out.splitlines()[1] == "42" and "[1] T https://a.b" in out
    assert fake.calls[0]["json"] == {"query": "答案是什么", "text": True}

    out2 = EXA.exa_similar({"url": "https://c.d", "num": 3})
    assert fake.calls[1]["url"] == "https://api.exa.ai/findSimilar"
    assert fake.calls[1]["json"] == {"url": "https://c.d", "numResults": 3}
    assert "similar page" in out2


# ---------------------------------------------------------------- tavily
def test_tavily_search_payload_and_render(monkeypatch):
    def handler(call):
        return _Resp(200, {"answer": "梅西是阿根廷球员",
                           "results": [{"title": "Leo Messi", "url": "https://e.f",
                                        "published_date": "2026-01-02T00:00:00Z",
                                        "score": 0.88, "content": "Messi plays for Inter Miami"}],
                           "usage": {"credits": 1}})

    fake = _wire(monkeypatch, TAV, handler)
    out = TAV.tavily_search({"query": "who is Messi", "depth": "advanced", "time_range": "week",
                             "include_domains": "espn.com, fifa.com", "include_answer": True,
                             "max_results": 99})
    body = fake.calls[0]["json"]
    assert fake.calls[0]["url"] == "https://api.tavily.com/search"
    assert body["search_depth"] == "advanced" and body["time_range"] == "week"
    assert body["include_domains"] == ["espn.com", "fifa.com"]
    assert body["include_answer"] is True and body["max_results"] == 20   # 上限夹紧
    assert "直接答案：梅西是阿根廷球员" in out and "消耗 1 credits" in out
    assert fake.calls[0]["headers"]["Authorization"] == "Bearer test-key"


def test_tavily_search_ignores_bad_depth(monkeypatch):
    fake = _wire(monkeypatch, TAV, lambda call: _Resp(200, {"results": []}))
    TAV.tavily_search({"query": "x", "depth": "深度"})
    assert "search_depth" not in fake.calls[0]["json"]


def test_tavily_search_without_key_returns_guide(monkeypatch):
    _wire(monkeypatch, TAV, lambda call: _Resp(200, {}), key="")
    out = TAV.tavily_search({"query": "x"})
    assert "TAVILY_API_KEY" in out and "app.tavily.com" in out


def test_tavily_extract(monkeypatch):
    fake = _wire(monkeypatch, TAV, lambda call: _Resp(200, {
        "results": [{"url": "https://a.b", "raw_content": "正文内容"}],
        "failed_results": [{"url": "https://c.d", "error": "timeout"}],
    }))
    out = TAV.tavily_extract({"urls": ["https://a.b", "https://c.d"]})
    assert fake.calls[0]["json"]["urls"] == ["https://a.b", "https://c.d"]
    assert fake.calls[0]["json"]["format"] == "markdown"
    assert "成功 1 个，失败 1 个" in out and "正文内容" in out and "timeout" in out


def test_tavily_research_polls_until_completed(monkeypatch):
    state = {"n": 0}

    def handler(call):
        if call["method"] == "POST":
            return _Resp(200, {"request_id": "rid-1", "status": "pending"})
        state["n"] += 1
        if state["n"] == 1:
            return _Resp(200, {"request_id": "rid-1", "status": "pending"})
        return _Resp(200, {"request_id": "rid-1", "status": "completed",
                           "output": "研究报告正文",
                           "sources": [{"title": "S", "url": "https://s.t"}]})

    fake = _wire(monkeypatch, TAV, handler)
    monkeypatch.setattr(TAV.time, "sleep", lambda s: None)
    out = TAV.tavily_research({"topic": "AI 进展"})
    assert fake.calls[0]["json"]["input"] == "AI 进展"
    assert fake.calls[1]["url"] == "https://api.tavily.com/research/rid-1"
    assert "研究报告正文" in out and "[1] S https://s.t" in out
    assert state["n"] == 2                                        # pending 一次后才拿结果


def test_tavily_research_failed_status(monkeypatch):
    def handler(call):
        if call["method"] == "POST":
            return _Resp(200, {"request_id": "rid-2", "status": "pending"})
        return _Resp(200, {"request_id": "rid-2", "status": "failed", "error": "boom"})

    _wire(monkeypatch, TAV, handler)
    monkeypatch.setattr(TAV.time, "sleep", lambda s: None)
    out = TAV.tavily_research({"topic": "x"})
    assert "失败" in out and "boom" in out


def test_tavily_research_timeout_returns_request_id(monkeypatch):
    clock = {"t": 0.0}

    def handler(call):
        if call["method"] == "POST":
            return _Resp(200, {"request_id": "rid-3", "status": "pending"})
        return _Resp(200, {"request_id": "rid-3", "status": "pending"})

    _wire(monkeypatch, TAV, handler)
    monkeypatch.setattr(TAV.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(TAV.time, "sleep", lambda s: clock.__setitem__("t", clock["t"] + s))
    out = TAV.tavily_research({"topic": "x", "timeout": 30})
    assert "超时" in out and "rid-3" in out


def test_tavily_research_reuses_request_id(monkeypatch):
    fake = _wire(monkeypatch, TAV, lambda call: _Resp(200, {
        "request_id": "rid-9", "status": "completed", "output": "既有结果"}))
    out = TAV.tavily_research({"request_id": "rid-9"})
    assert len(fake.calls) == 1 and fake.calls[0]["method"] == "GET"
    assert "既有结果" in out


# ---------------------------------------------------------------- skill.py 接线
def _load_skill():
    if "skill" in sys.modules:
        return sys.modules["skill"]
    return _load(SKILL_DIR / "skill.py", "skill")


def test_skill_routes_exa_and_tavily_to_native_impls(monkeypatch):
    skill = _load_skill()
    sentinel = "SENTINEL"
    monkeypatch.setattr(skill.exa_impl, "exa_search", lambda a: sentinel)
    monkeypatch.setattr(skill.exa_impl, "exa_answer", lambda a: sentinel)
    monkeypatch.setattr(skill.exa_impl, "exa_similar", lambda a: sentinel)
    monkeypatch.setattr(skill.tavily_impl, "tavily_search", lambda a: sentinel)
    monkeypatch.setattr(skill.tavily_impl, "tavily_extract", lambda a: sentinel)
    monkeypatch.setattr(skill.tavily_impl, "tavily_research", lambda a: sentinel)
    for name in ("search_exa", "search_exa_answer", "search_exa_similar",
                 "search_tavily", "search_tavily_extract", "search_tavily_research"):
        assert skill.HANDLERS[name]({"query": "x", "url": "https://a.b", "topic": "x"}) == sentinel


def test_skill_no_longer_shells_out_to_tvly():
    src = (SKILL_DIR / "skill.py").read_text(encoding="utf-8")
    assert "shutil.which" not in src
    assert "tvly\"] + args" not in src


def test_skill_json_tools_all_have_handlers():
    cfg = json.loads((SKILL_DIR / "skill.json").read_text(encoding="utf-8"))
    skill = _load_skill()
    declared = {t["function"]["name"] for t in cfg["tools"]}
    missing = declared - set(skill.HANDLERS)
    assert not missing, f"skill.json 声明了但 HANDLERS 没实现：{missing}"
