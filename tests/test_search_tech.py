# -*- coding: utf-8 -*-
"""极客信息源聚合（tech_impl）的纯函数与融合判据回归。

不打网络。测的都是实测踩过的坑：
- dev.to 官方 API 只支持 tag 检索，tag 命中 ≠ 内容相关（所以 auto 默认不含它）
- 同一个 URL 被 HN 与 StackExchange 同时引用时要收成一条，权威度取最高的那个（交叉印证）
- GitHub 未认证实测 403（缺 token 必须明确报，不能静默返回空结果）
- 某个源挂掉不能让整次检索看起来「没结果」
- 注册表 404（没这个包）属正常未命中，不该当错误报给用户
"""
from __future__ import annotations

import importlib.util
import sys
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "skills" / "search"))


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


M = _load(ROOT / "skills" / "search" / "tech_impl.py", "tech_impl_t")


# ---------------------------------------------------------------- URL 归并键
def test_url_key_strips_tracking_but_keeps_meaningful_query():
    assert M._url_key("https://www.Example.com/Post/?utm_source=hn&utm_medium=x") == "example.com/post"
    assert M._url_key("http://example.com/post") == "example.com/post"
    # ?id=123 是 HN 每条评论的身份，剥掉会让所有评论撞成一个键
    assert M._url_key("https://news.ycombinator.com/item?id=1") != M._url_key("https://news.ycombinator.com/item?id=2")
    assert M._url_key("") == ""


# ---------------------------------------------------------------- 时效
def test_age_str_buckets():
    today = date.today()
    assert M._age_str(today.isoformat()) == "今天"
    assert M._age_str((today - timedelta(days=5)).isoformat()) == "5天前"
    assert M._age_str((today - timedelta(days=90)).isoformat()) == "3个月前"
    assert M._age_str((today - timedelta(days=800)).isoformat()) == "2年前"
    assert M._age_str((today + timedelta(days=3)).isoformat()) == "未来日期"
    assert M._age_str("") == ""


def test_norm_date_pads_non_zero_filled_dates():
    assert M._norm_date("2026-7-1") == "2026-07-01"
    assert M._norm_date("2026-09-26T10:00:00Z") == "2026-09-26"
    assert M._norm_date("") == ""


def test_strip_html_removes_tags_and_entities():
    assert M._strip_html("<p>a &lt;b&gt;</p><script>x</script>") == "a <b>"
    assert M._strip_html("<pre><code>print(1)</code></pre>") == "print(1)"


# ---------------------------------------------------------------- 权威度分层
def test_item_tier_table_and_subsite_fallback():
    assert M._item("t", "https://a.com/1", "", "", "StackOverflow")["tier"] == "B"
    assert M._item("t", "https://a.com/2", "", "", "StackExchange/serverfault")["tier"] == "B"
    assert M._item("t", "https://a.com/3", "", "", "Hacker News")["tier"] == "C"
    assert M._item("t", "https://a.com/4", "", "", "PyPI")["tier"] == "A"


def test_item_meta_drops_empty_and_key_override():
    it = M._item("t", "https://a.com/1", "", "", "Hacker News", points=0, comments=3, lang="")
    assert it["meta"] == {"comments": 3}
    assert M._item("t", "https://a.com/1", "", "", "arXiv", key="10.1/x")["doi"] == "10.1/x"


def test_lang_of_detects_cjk():
    assert M._lang_of("注意力机制") == "zh"
    assert M._lang_of("attention mechanism") == "en"
    assert M._lang_of("attention", "zh") == "zh"


# ---------------------------------------------------------------- 交叉印证
def test_rrf_merges_same_url_across_sources_and_keeps_highest_tier():
    a = M._item("Same Page", "https://example.com/post?utm_source=x", "s1", "2026-01-01", "Hacker News")
    b = M._item("Same Page", "https://www.example.com/post/", "s2", "2026-01-02", "StackOverflow")
    pairs = M._rrf({"Hacker News": [a], "StackOverflow": [b]})
    assert len(pairs) == 1, "同一 URL 被两个源引用时必须收成一条"
    item, srcs, _score = pairs[0]
    assert set(srcs) == {"Hacker News", "StackOverflow"}
    ranks = [M._TIER_RANK[M._SRC_TIER[s]] for s in srcs]
    assert "ABC"[min(ranks)] == "B", "合并后权威度应取最高的那个源"


# ---------------------------------------------------------------- 源解析与降级
def test_search_tech_rejects_empty_query():
    assert "请提供检索关键词" in M.search_tech({})


def test_search_tech_unknown_source_is_reported_not_silently_dropped():
    out = M.search_tech({"query": "x", "sources": "maven,npmjs", "limit": 1})
    assert "没有可用的信息源" in out or "无法识别的源" in out


def test_auto_sources_excludes_devto():
    assert "devto" not in M._AUTO_SOURCES
    assert "academic" in M._AUTO_SOURCES


def test_search_tech_end_to_end_with_mocked_source():
    def fake(url, params=None, timeout=12.0, headers=None):
        if "hn.algolia" in url:
            return {"hits": [{"title": "T", "url": "https://x.com/a", "objectID": "1",
                              "points": 5, "num_comments": 2,
                              "created_at": "2026-09-01T00:00:00Z"}]}
        raise RuntimeError("HTTP 404")

    with patch.object(M, "_json_get", fake):
        out = M.search_tech({"query": "x", "sources": "hn", "limit": 1})
    assert "Hacker News" in out and "[C]" in out and "https://x.com/a" in out


def test_gather_one_source_failure_does_not_kill_the_rest():
    def fake_run(name, query, limit, days, sort, lang="", site="stackoverflow", **kw):
        if name == "gh":
            raise RuntimeError("缺 GITHUB_TOKEN")
        return [M._item("t", "https://a.com/1", "", "", "Hacker News")]

    with patch.object(M, "_run_source", fake_run):
        items, ok, errs = M._gather(["hn", "gh"], "q", 1, None, "relevance")
    assert len(items) == 1 and ok == ["hn(1)"]
    assert errs and "GITHUB_TOKEN" in errs[0]


def test_devto_uses_tag_not_keyword():
    seen = {}

    def fake(url, params=None, timeout=12.0, headers=None):
        seen.update(params or {})
        return []

    with patch.object(M, "_json_get", fake):
        M._src_devto("rust async runtime", 2, None, "relevance")
    assert seen.get("tag") == "rust", "dev.to 只支持 tag 检索，取首词当 tag"
    assert "q" not in seen


# ---------------------------------------------------------------- 注册表
def test_registry_info_validates_input_and_kind():
    assert "请提供包名" in M.registry_info({})
    assert "无法识别的 kind" in M.registry_info({"name": "x", "kind": "maven"})


def test_registry_404_is_treated_as_miss_not_error():
    def boom(*a, **k):
        raise RuntimeError("HTTP 404")

    with patch.object(M, "_json_get", boom):
        out = M.registry_info({"name": "definitely-not-a-package-xyz", "kind": "pypi"})
    assert "无命中" in out
    assert "404" not in out


def test_registry_pypi_formats_version_and_python_requirement():
    def fake(url, params=None, timeout=12.0, headers=None):
        return {"info": {"name": "requests", "version": "2.34.2", "summary": "Python HTTP for Humans.",
                         "requires_python": ">=3.10", "license_expression": "Apache-2.0",
                         "project_urls": {"Source": "https://github.com/psf/requests"}},
                "releases": {"2.34.2": [{"upload_time": "2026-05-14T19:25:26"}]}}

    with patch.object(M, "_json_get", fake):
        out = M.registry_info({"name": "requests", "kind": "pypi"})
    assert "2.34.2" in out and "Python >=3.10" in out and "Apache-2.0" in out
    assert "https://github.com/psf/requests" in out


# ---------------------------------------------------------------- tavily 源（域名判权威度）
def test_tier_of_url_judges_by_host():
    assert M._tier_of_url("https://docs.mem0.ai/introduction") == "A"  # docs. 前缀
    assert M._tier_of_url("https://kubernetes.io/docs/x") == "A"       # 官方域名白名单
    assert M._tier_of_url("https://en.wikipedia.org/wiki/X") == "B"
    assert M._tier_of_url("https://someblog.example/x") == "C"
    assert M._tier_of_url("") == "C"


def test_official_host_is_the_project_own_domain():
    # 白名单穷举不完厂商域名：查询对象自己拥有的域就是官方
    assert M._tier_of_url("https://mem0.ai/blog/x", "mem0 memory layer") == "A"
    assert M._tier_of_url("https://rust-lang.org/x", "rust async") == "A"
    # 停用词挡住误判：query 里的 the 不该把 theverge.com 抬成 A
    assert M._tier_of_url("https://theverge.com/x", "the best llm") == "C"


def test_official_domain_picks_registrable_domain():
    items = [M._item("t", "https://mem0.ai/blog/x", "", "", "Tavily"),
             M._item("t2", "https://docs.mem0.ai/x", "", "", "Tavily")]
    assert M._official_domain(items, "mem0") == "mem0.ai"
    assert M._official_domain([], "mem0") == ""
    assert M._official_domain(items, "unrelated thing") == ""


def test_entity_terms_only_words_with_digits():
    assert M._entity_terms("mem0 memory layer") == ["mem0"]
    # 连字符不能把词拆开，否则 gpt-4 会被漏掉
    assert M._entity_terms("gpt-4 vs claude") == ["gpt-4"]
    assert M._entity_terms("k8s operator") == ["k8s"]
    # 不含数字词的 query 无从判断哪个词是实体，不做闸门
    assert M._entity_terms("rust async runtime") == []


def test_has_entity_normalizes_separators():
    it = M._item("Using GPT-4 for X", "", "", "", "Tavily")
    assert M._has_entity(it, ["gpt4"])
    assert M._has_entity(it, ["gpt-4"])
    assert not M._has_entity(M._item("Unrelated", "", "", "", "Tavily"), ["mem0"])


def test_tavily_source_second_round_limits_to_official_domain():
    calls = []

    def fake_request(method, path, payload=None, timeout=60.0):
        calls.append(dict(payload or {}))
        if payload.get("include_domains"):
            return {"results": [{"title": "Platform Overview",
                                 "url": "https://docs.mem0.ai/platform/overview",
                                 "content": "Mem0 platform docs", "score": 0.8}]}, ""
        return {"results": [{"title": "Introducing Mem0",
                             "url": "https://mem0.ai/blog/introducing-mem0",
                             "content": "Mem0 is a memory layer", "score": 0.9}]}, ""

    import tavily_impl
    with patch.object(tavily_impl, "_request", fake_request):
        items = M._src_tavily("mem0 memory layer", 3, None, "relevance")
    assert len(calls) == 2, "短 query 要补一次 documentation 查询"
    assert calls[1].get("include_domains") == ["mem0.ai"], "第二轮限定到第一轮识别出的官方域"
    urls = [i["url"] for i in items]
    assert "https://docs.mem0.ai/platform/overview" in urls
    assert all(i["tier"] == "A" for i in items), "官方域下的每一条都是 A"


def test_tavily_source_skips_second_round_for_long_query():
    calls = []

    def fake_request(method, path, payload=None, timeout=60.0):
        calls.append(dict(payload or {}))
        return {"results": []}, ""

    import tavily_impl
    with patch.object(tavily_impl, "_request", fake_request):
        M._src_tavily("how to build a rag pipeline in python", 3, None, "relevance")
    assert len(calls) == 1, "长 query 是自然语言，官方文档不是它要的东西，不花第二个 credit"


def test_search_tech_drops_word_collisions_when_query_has_entity():
    def fake_gather(names, query, limit, days, sort, lang="", site="stackoverflow", **kw):
        return [M._item("Mem0: memory layer", "https://mem0.ai", "", "", "Tavily"),
                M._item("Stream X-Machine", "https://en.wikipedia.org/wiki/X",
                        "model of computation", "", "Wikipedia")], ["hn(1)"], []

    with patch.object(M, "_gather", fake_gather):
        out = M.search_tech({"query": "mem0 memory layer", "sources": "hn"})
    assert "Mem0: memory layer" in out
    assert "Stream X-Machine" not in out, "词面撞车的条目要被滤掉"
    assert "滤掉 1 条" in out, "过滤要如实报出来，不能静默丢结果"


def test_search_tech_keeps_everything_when_query_has_no_entity():
    def fake_gather(names, query, limit, days, sort, lang="", site="stackoverflow", **kw):
        return [M._item("Async Rust in practice", "https://a.com/1", "runtime internals", "", "Hacker News"),
                M._item("Tokio internals", "https://a.com/2", "async runtime", "", "Hacker News")], ["hn(2)"], []

    with patch.object(M, "_gather", fake_gather):
        out = M.search_tech({"query": "rust async runtime", "sources": "hn"})
    assert "Async Rust in practice" in out and "Tokio internals" in out
    assert "滤掉" not in out


# ---------------------------------------------------------------- 官方文档源（GitHub homepage → llms.txt）
def test_official_from_github_skips_other_projects_homepage():
    # 搜 mem0 时 stars 最多的第一条是别的项目（claude-mem），它的 homepage 不能被采信
    items = [M._item("thedotmack/claude-mem", "https://github.com/x", "", "", "GitHub",
                     homepage="https://claude-mem.ai"),
             M._item("mem0ai/mem0", "https://github.com/mem0ai/mem0", "", "", "GitHub",
                     homepage="https://mem0.ai")]
    assert M._official_from_github(items, "mem0") == "mem0.ai"
    assert M._official_from_github(items, "unrelated thing") == ""


def test_official_from_github_skips_hosting_and_empty_homepage():
    # a.github.io 的注册域是 github.io：托管平台域太宽，当 include_domains 会放进整个平台
    items = [M._item("alpha/beta", "https://github.com/alpha/beta", "", "", "GitHub",
                     homepage="https://alpha.github.io/docs")]
    assert M._official_from_github(items, "alpha") == ""
    assert M._official_from_github([M._item("x/y", "https://github.com/x/y", "", "", "GitHub")], "x") == ""


def test_official_from_github_accepts_docs_subdomain():
    # langchain 的 homepage 直接是 docs 域，要按注册域收
    items = [M._item("langchain-ai/langchain", "https://github.com/langchain-ai/langchain", "", "",
                     "GitHub", homepage="https://docs.langchain.com/langchain/")]
    assert M._official_from_github(items, "langchain") == "langchain.com"


def test_llms_entries_parses_links_sections_and_drops_tag():
    text = ("# Mem0\n\n> intro\n\n## Install\n\n"
            "- [Install](https://docs.mem0.ai/install) [Both]: pip install\n\n"
            "## Community & Support\n\n- [Discord](https://docs.mem0.ai/community) [Both]: chat\n")
    got = M._llms_entries(text)
    assert [e["title"] for e in got] == ["Install", "Discord"]
    assert got[0]["desc"] == "pip install", "分区标记 [Both]: 要从描述里剥掉"
    assert got[0]["section"] == "Install" and got[0]["sidx"] == 1
    assert got[1]["sidx"] == 2
    assert M._llms_entries("") == []


def test_src_official_needs_official_domain():
    assert M._src_official("", "mem0", 3) == []


def test_src_official_reports_when_site_has_no_llms_txt():
    def boom(url, **kw):
        raise RuntimeError("HTTP 404")

    with patch.object(M, "_get", boom):
        try:
            M._src_official("vllm.ai", "vllm", 3)
            raise AssertionError("站点没提供 llms.txt 时要报错，不能静默返回空")
        except RuntimeError as e:
            assert "llms.txt 不可用" in str(e)
            assert "docs.vllm.ai" in str(e), "两个候选都要试过再报错"


def test_src_official_ranks_getting_started_and_entry_pages_first():
    text = ("## Community & Support\n"
            "- [Discord](https://docs.mem0.ai/community) [Both]: chat\n"
            "## Getting Started\n"
            "- [Introduction](https://docs.mem0.ai/introduction) [Both]: one-page overview\n"
            "- [Platform Quickstart](https://docs.mem0.ai/platform/quickstart) [Platform]: first integration\n")

    class R:
        content = text.encode("utf-8")

    with patch.object(M, "_get", lambda url, **kw: R()):
        items = M._src_official("mem0.ai", "mem0", 2)
    assert [i["url"] for i in items] == ["https://docs.mem0.ai/introduction",
                                         "https://docs.mem0.ai/platform/quickstart"]
    assert all(i["tier"] == "A" for i in items), "官方文档是一手"
    assert items[0]["meta"]["section"] == "Getting Started"


def test_src_official_tries_docs_subdomain_before_main_domain():
    seen = []

    class R:
        content = b"- [X](https://docs.mem0.ai/x) [Both]: y"

    def fake_get(url, **kw):
        seen.append(url)
        return R()

    with patch.object(M, "_get", fake_get):
        M._src_official("mem0.ai", "mem0", 1)
    assert seen == ["https://docs.mem0.ai/llms.txt"], "文档子域优先：主域 llms.txt 常是营销索引"


def test_search_tech_feeds_github_homepage_domain_to_official_source():
    seen = {}

    def fake_gh(query, limit, days, sort):
        return [M._item("mem0ai/mem0", "https://github.com/mem0ai/mem0", "", "", "GitHub",
                        homepage="https://mem0.ai")]

    def fake_gather(names, query, limit, days, sort, lang="", site="stackoverflow", official=""):
        seen["names"], seen["official"] = list(names), official
        return [], ["official(0)"], []

    with patch.object(M, "_src_github", fake_gh), patch.object(M, "_gather", fake_gather):
        out = M.search_tech({"query": "mem0", "sources": "gh,official"})
    assert seen["official"] == "mem0.ai"
    assert "gh" not in seen["names"], "gh 已预跑过，不能再打一次 API"
    assert "gh(1)" in out, "预跑拿到的 GitHub 结果照样要并进输出"
