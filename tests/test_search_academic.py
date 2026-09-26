# -*- coding: utf-8 -*-
"""学术科研引擎（academic_impl）的纯函数回归。

不打网络：只测可离线判定的判据——标识归一、页段解析、日期闸门、
跨源融合（DOI 键 + 标题签名二次合并）、字段互补与参数校验。
这些正是实测踩过的坑：arXiv 版没 DOI 导致同一篇被算成两条、
Crossref 有 2114 年脏数据、latest 纯时间排序把无关论文顶到最前。
"""
from __future__ import annotations

import importlib.util
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


M = _load(ROOT / "skills" / "search" / "academic_impl.py", "academic_impl_t")


# ---------------------------------------------------------------- 标识归一
def test_norm_ref_arxiv_id():
    assert M._norm_ref("2312.10997") == ("arxiv", "2312.10997")
    assert M._norm_ref("arXiv:2312.10997v5") == ("arxiv", "2312.10997v5")
    assert M._norm_ref("https://arxiv.org/abs/2312.10997") == ("arxiv", "2312.10997")
    assert M._norm_ref("arxiv 1706.03762") == ("arxiv", "1706.03762")


def test_norm_ref_doi():
    assert M._norm_ref("10.1145/3777378") == ("doi", "10.1145/3777378")
    assert M._norm_ref("https://doi.org/10.1145/3777378") == ("doi", "10.1145/3777378")
    assert M._norm_ref("DOI: 10.18653/v1/2024.eacl-demo.16.") == ("doi", "10.18653/v1/2024.eacl-demo.16")


def test_norm_ref_pdf_and_unknown():
    assert M._norm_ref("https://x.org/a/b.pdf")[0] == "pdf_url"
    assert M._norm_ref("https://example.com/page")[0] == "url"
    assert M._norm_ref("随便一段话")[0] == "unknown"


# ---------------------------------------------------------------- 页段解析
def test_parse_pages_default_and_all():
    assert M._parse_pages("", 21) == [0, 1, 2, 3, 4]
    assert M._parse_pages("", 3) == [0, 1, 2]
    assert M._parse_pages("all", 100) == list(range(60))  # 上限 60 页，防止一次拉爆上下文
    assert M._parse_pages("all", 7) == list(range(7))


def test_parse_pages_range_and_oob():
    assert M._parse_pages("3", 10) == [2]
    assert M._parse_pages("2-6", 10) == [1, 2, 3, 4, 5]
    assert M._parse_pages("8-99", 10) == list(range(7, 10))  # 右界夹到总页数
    assert M._parse_pages("99", 10) == []                    # 越界返回空，由调用方提示


# ---------------------------------------------------------------- 日期闸门
def test_date_ok_rejects_future_and_stale():
    assert M._date_ok({"date": "2114-01-01"}) is False        # 实测 Crossref 有 2114 年条目
    assert M._date_ok({"date": (date.today() + timedelta(days=200)).isoformat()}) is False
    assert M._date_ok({"date": date.today().isoformat()}) is True
    assert M._date_ok({"date": (date.today() - timedelta(days=30)).isoformat()}, days=7) is False
    assert M._date_ok({"date": (date.today() - timedelta(days=30)).isoformat()}, days=90) is True


def test_date_ok_passes_missing_date():
    assert M._date_ok({}) is True                             # 源没给日期不能当无关杀掉
    assert M._date_ok({"year": "2024"}) is True
    assert M._date_ok({"year": "2024"}, days=7) is False      # 有年无日 → 按年初算，会被 days 挡掉


# ---------------------------------------------------------------- 标题签名
def test_title_sig_case_insensitive():
    a = M._title_sig({"title": "Ragas: Automated Evaluation of Retrieval Augmented Generation"})
    b = M._title_sig({"title": "RAGAs — Automated Evaluation of Retrieval Augmented Generation"})
    assert a and a == b
    assert M._jaccard(a, b) == 1.0
    assert M._jaccard(a, M._title_sig({"title": "Totally Different Topic"})) < 0.2


# ---------------------------------------------------------------- 跨源融合
def test_rrf_merges_same_doi_and_complements_fields():
    a = {"title": "Graph RAG Survey", "doi": "10.1145/3777378", "source": "openalex", "cited": 145}
    b = {"title": "Graph Retrieval-Augmented Generation: A Survey",
         "doi": "https://doi.org/10.1145/3777378", "source": "crossref",
         "pdf": "https://x.org/y.pdf"}
    out = M._rrf({"openalex": [a], "crossref": [b]})
    assert len(out) == 1
    item, srcs, score = out[0]
    assert set(srcs) == {"openalex", "crossref"}
    assert item["cited"] == 145 and item["pdf"] == "https://x.org/y.pdf"
    assert score > 1 / 61  # 两源累积，高于任一单源


def test_rrf_merges_arxiv_and_publisher_by_title():
    arx = {"title": "Ragas: Automated Evaluation of Retrieval Augmented Generation",
           "source": "arxiv", "pdf": "https://arxiv.org/pdf/2309.15217v2"}
    pub = {"title": "RAGAs: Automated Evaluation of Retrieval Augmented Generation",
           "doi": "10.18653/v1/2024.eacl-demo.16", "source": "openalex", "cited": 481}
    out = M._rrf({"arxiv": [arx], "openalex": [pub]})
    assert len(out) == 1                       # 无 DOI 的 arXiv 版必须被标题签名并进来
    item, srcs, _ = out[0]
    assert set(srcs) == {"arxiv", "openalex"}
    assert item["cited"] == 481 and item["doi"] == "10.18653/v1/2024.eacl-demo.16"


def test_rrf_keeps_short_titles_apart():
    out = M._rrf({"arxiv": [{"title": "Graph RAG", "source": "arxiv"}],
                  "crossref": [{"title": "RAG System", "source": "crossref"}]})
    assert len(out) == 2                       # <4 实词不参与相似合并，避免误并


def test_rrf_ranks_multi_source_first():
    shared = {"title": "Shared Paper On Retrieval", "doi": "10.1/x", "source": "arxiv"}
    lone = {"title": "Lone Paper On Retrieval", "doi": "10.1/y", "source": "crossref"}
    out = M._rrf({"arxiv": [lone, shared], "openalex": [shared]})
    assert out[0][0]["doi"] == "10.1/x"


def test_norm_key_strips_doi_url():
    assert M._norm_key({"doi": "https://doi.org/10.1/AbC"}) == "doi:10.1/abc"
    assert M._norm_key({"title": "A B, C!"}) == M._norm_key({"title": "a b c"})
    assert M._norm_key({}) == ""


# ---------------------------------------------------------------- 摘要还原
def test_reconstruct_abstract_order():
    inv = {"world": [1], "hello": [0], "again": [2]}
    assert M._reconstruct_abstract(inv) == "hello world again"
    assert M._reconstruct_abstract(None) == ""


# ---------------------------------------------------------------- 参数校验
def test_search_paper_guards():
    assert "请提供" in M.search_paper({})
    assert "未知信息源" in M.search_paper({"query": "x", "source": "nope"})


def test_read_paper_guards():
    assert "请提供" in M.read_paper({})
    assert "无法识别" in M.read_paper({"ref": "随便一段话"})


def test_paper_cite_guards():
    assert "请提供" in M.paper_cite({})
    assert "% 无法识别" in M.paper_cite({"refs": "随便一段话"})


def test_handlers_registered():
    assert set(M.HANDLERS) == {"search_paper", "read_paper", "paper_cite"}


def test_fmt_papers_renders_links():
    item = {"title": "T", "year": "2024", "cited": 3, "authors": ["A", "B"],
            "doi": "10.1/x", "pdf": "https://x/p.pdf", "abstract": "abs"}
    text = M._fmt_papers([(item, ["arxiv", "openalex"], 0.03)], 5)
    assert "T" in text and "DOI: 10.1/x" in text and "https://x/p.pdf" in text
    assert "arxiv+openalex" in text
