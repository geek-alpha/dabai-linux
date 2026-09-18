#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""附件消息「一份文本两个受众」的回归契约（2026-09-16）。

用户反馈：带图片/文件的用户气泡排版很难看。根因不是 CSS——同一条 user 文本
既要喂模型（要 [[IMG:]] 绝对路径 + 文档正文），又要渲染气泡（要缩略图网格 +
文件卡片），存进历史的是前者，刷新后气泡里就出现服务器绝对路径和上千字正文。

契约：
  1. 块被剥出来 → 展示文本只留用户真说的话，正文绝不进气泡
  2. 绝对路径 → /uploads/... URL；越界路径一律给空串（不能变成任意文件下载链接）
  3. 历史里**已经存下来的**老消息（正文已连着块落库）同样要能剥干净——不做数据迁移
  4. 用户自己打「【我上传了文件】」这几个字时，一个字都不许切掉
  5. _history_for_client 只读不改：history 同时是喂模型的短期记忆，原地改写会污染上下文

被测函数用 AST 抽出单独 exec（不 import server，避免拉起 FastAPI app 与事件循环）。
"""
import ast
import re
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parents[1]
UPLOADS = BASE / "data" / "uploads"

WANTED_FUNCS = {"_uploads_url", "_split_attach_block", "_history_for_client"}
WANTED_NAMES = {"_ATTACH_BLOCK_HEAD", "_IMG_LINE_RE", "_FILE_LINE_RE", "_CUT_TAIL_RE"}


def _load():
    src = (BASE / "server.py").read_text(encoding="utf-8", errors="replace")
    tree = ast.parse(src)
    nodes = []
    for n in tree.body:
        if isinstance(n, ast.FunctionDef) and n.name in WANTED_FUNCS:
            nodes.append(n)
        elif isinstance(n, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id in WANTED_NAMES for t in n.targets):
            nodes.append(n)
    if len(nodes) != len(WANTED_FUNCS) + len(WANTED_NAMES):
        pytest.skip("server.py 里附件剥离函数结构变了（契约需重审）")
    ns = {"Path": Path, "UPLOADS_DIR": UPLOADS, "re": re}
    exec(compile(ast.Module(body=nodes, type_ignores=[]),
                 str(BASE / "server.py"), "exec"), ns)
    return ns


@pytest.fixture(scope="module")
def api():
    return _load()


# —— 真实历史样本（从库里取的原样文本，不是编的）——
REAL_IMG_MSG = (
    "不过你这个带图片和文件的好像在发送者那边的文字气泡那边显示排版很不美观。"
    "\n\n【我上传了文件】\n"
    "[图片1：dcc8-e086ea8f4d00f66ba2f095ac76df43b5.png]"
    " [[IMG:" + str(UPLOADS) + "/local/20260916/f6356a0ad96acf7f.png]]\n"
    "[图片2：login_qr.png]"
    " [[IMG:" + str(UPLOADS) + "/local/20260916/39c9ac89f7ac37d2.png]]"
)

# 老格式内联（改动前存的）：头行没有路径，正文连着 ``` 落库
OLD_INLINE_PDF = (
    "帮我看下这份证明\n\n【我上传了文件】\n"
    "[附件：个人受理证明.pdf，156612 字节，PDF 共 1049 字符]\n"
    "```\n--- 第 1 页 ---\n国家开发银行生源地信用助学贷款受理证明\n"
    "[附件：这是正文里恰好长得像附件的行，12 字节，不该被当成真附件]\n```"
)

# 新格式内联（改动后）：头行带路径 + 截断尾注
NEW_INLINE_CUT = (
    "【我上传了文件】\n"
    "[附件：big.log，9200000 字节，文本，路径 " + str(UPLOADS) + "/local/20260916/big.log]\n"
    "```\n第 1 行日志\n第 2 行日志\n```\n"
    "…（只给了前 16000 字符，需要更多用 read_file 读 "
    + str(UPLOADS) + "/local/20260916/big.log）"
)

PATH_ONLY_ZIP = (
    "【我上传了文件】\n"
    "[附件：素材包.zip，12345 字节，二进制文件，路径 " + str(UPLOADS) + "/local/20260916/a.zip"
    "——要看内容用 read_file / code_read 打开]"
)


def test_real_image_message_splits_clean(api):
    """真实那条：气泡只留用户的话，两张图各拿到一个 /uploads URL。"""
    display, atts = api["_split_attach_block"](REAL_IMG_MSG)
    assert display == "不过你这个带图片和文件的好像在发送者那边的文字气泡那边显示排版很不美观。"
    assert "【我上传了文件】" not in display and "IMG:" not in display
    assert [a["kind"] for a in atts] == ["image", "image"]
    assert [a["name"] for a in atts] == [
        "dcc8-e086ea8f4d00f66ba2f095ac76df43b5.png", "login_qr.png"]
    assert all(a["url"].startswith("/uploads/local/20260916/") for a in atts)


def test_old_stored_inline_body_never_reaches_bubble(api):
    """老消息（正文已落库）：正文不进气泡；正文里长得像附件的行也不许被误判。"""
    display, atts = api["_split_attach_block"](OLD_INLINE_PDF)
    assert display == "帮我看下这份证明"
    assert len(atts) == 1, "正文里的假附件行被当成真附件了"
    a = atts[0]
    assert a["name"] == "个人受理证明.pdf" and a["size"] == 156612 and a["kind"] == "file"
    assert a["url"] == "", "老格式拿不到路径时宁可不可点，也不能瞎编 URL"


def test_new_inline_cut_keeps_clickable_path(api):
    display, atts = api["_split_attach_block"](NEW_INLINE_CUT)
    assert display == ""
    assert atts[0]["url"] == "/uploads/local/20260916/big.log"
    assert "第 1 行日志" not in display


def test_path_only_attachment(api):
    display, atts = api["_split_attach_block"](PATH_ONLY_ZIP)
    assert display == ""
    assert atts[0]["url"] == "/uploads/local/20260916/a.zip"
    assert atts[0]["name"] == "素材包.zip"


def test_no_block_untouched(api):
    display, atts = api["_split_attach_block"]("就是一句普通的话")
    assert display == "就是一句普通的话" and atts == []


def test_user_typed_the_head_word(api):
    """用户自己打了「【我上传了文件】」：一个字都不许切。"""
    txt = "我上传了文件\n【我上传了文件】\n这句话是我想说的"
    display, atts = api["_split_attach_block"](txt)
    assert display == txt and atts == []


def test_out_of_bounds_path_gets_no_url(api):
    """越界路径不生成下载链接（WS 消息是客户端可控输入）。"""
    txt = "【我上传了文件】\n[图片1：x.png] [[IMG:/etc/passwd]]"
    _, atts = api["_split_attach_block"](txt)
    assert atts[0]["url"] == ""
    txt2 = ("【我上传了文件】\n[附件：secret，1 字节，文本，路径 /etc/shadow"
            "——要看内容用 read_file / code_read 打开]")
    _, atts2 = api["_split_attach_block"](txt2)
    assert atts2[0]["url"] == ""


def test_mixed_images_and_files(api):
    txt = ("看这两个\n\n【我上传了文件】\n"
           "[图片1：a.png] [[IMG:" + str(UPLOADS) + "/local/d/a.png]]\n"
           "[附件：b.pdf，10 字节，PDF 共 3 字符，路径 " + str(UPLOADS) + "/local/d/b.pdf]\n"
           "```\n正文\n```")
    display, atts = api["_split_attach_block"](txt)
    assert display == "看这两个"
    assert [a["kind"] for a in atts] == ["image", "file"]


def test_history_for_client_does_not_mutate_input(api):
    """history 同时是喂模型的短期记忆：只能拷贝，不能原地改。"""
    hist = [{"user": REAL_IMG_MSG, "ai": "嗯", "ts": 1},
            {"user": "普通一句", "ai": "好", "ts": 2},
            {"user": "", "ai": "主动说话", "ts": 3}]
    before = [dict(h) for h in hist]
    out = api["_history_for_client"](hist)
    assert hist == before, "入参被改写了"
    assert "【我上传了文件】" in hist[0]["user"], "原列表必须保留模型视角的原文"
    assert "IMG:" not in out[0]["user"] and len(out[0]["atts"]) == 2
    assert out[0]["ts"] == 1 and out[0]["ai"] == "嗯", "其它字段要原样带过去"
    assert "atts" not in out[1]
    assert out[2]["user"] == "" and out[2]["ai"] == "主动说话"


def test_history_for_client_tolerates_junk(api):
    """脏历史不许把接口打崩。"""
    out = api["_history_for_client"]([None, "字符串", {}, {"user": None}])
    assert len(out) == 2  # 非 dict 的两条丢掉，{} 与 {"user": None} 保留为空 user
