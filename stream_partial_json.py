"""流式工具参数的 partial JSON 解析。

提炼自 anthropic-sdk `lib/streaming/_messages.py` 的 accumulate 分支：流式到达的
工具参数是"解析到一半"的 JSON，缺的通常只是尾部右括号。生产版用 jiter 的
partial_mode（Rust），标准库没有等价物，这里用 raw_decode + 补闭合符逼近。

安全边界：partial 的结果只能用于诊断和展示，不能拿去执行。截断的 code_edit
参数直接执行会写出半个文件——状态必须由调用方显式判断，不许当 ok 用。
"""
from __future__ import annotations

import json
from typing import Any

OK = "ok"
PARTIAL = "partial"
BROKEN = "broken"

# 补闭合候选：缺一层右括号 / 缺引号+右括号 / 缺两层 / 数组收尾
_CLOSERS = ("}", "]", '"}', '"]', "}}", "}]")


def parse_partial_json(buf: str) -> tuple[Any, str]:
    """解析流式累积的 JSON 文本，返回 (值, 状态)。

    ok      —— 已闭合的完整 JSON；尾部多余标记（如 </tool_call>）忽略
    partial —— 补上闭合符才成立，说明流被截断，值只可信到已收到的键
    broken  —— 补闭合也救不回来，数据真损坏（值为 None）
    """
    text = buf.strip()
    if not text:
        return {}, OK

    decoder = json.JSONDecoder()
    try:
        value, _end = decoder.raw_decode(text)
    except json.JSONDecodeError:
        pass
    else:
        return value, OK

    for candidate in _partial_candidates(text):
        try:
            return decoder.raw_decode(candidate)[0], PARTIAL
        except json.JSONDecodeError:
            continue
    return None, BROKEN


def _partial_candidates(text: str) -> list[str]:
    out = [text + c for c in _CLOSERS]
    trimmed = text.rstrip().rstrip(",")
    if trimmed != text:
        out += [trimmed + c for c in _CLOSERS]
    return out
