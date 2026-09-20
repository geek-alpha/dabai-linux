"""中途问用户：给模型一个「问完等答案」的真实通道。

确认卡（tool_gate）是「拦一下、等你点头」——工具没执行，模型下轮重试；
提问卡是「问一句、等你回答」——工具原地挂起，答案作为工具结果回灌，
模型带着答案继续同一轮干活，不用重述上下文、不用重试。

三条边界（宁可让模型自己继续，也绝不吊死一轮）：
  · 无人在线 / 超时 / 用户跳过 → 立刻返回可读说明，模型按默认继续；
  · 答案按 request_id 配对，未知 id 返回 False（过期卡片不误伤新提问）；
  · 结果跨线程投递（loop.call_soon_threadsafe）：工具执行线程与 HTTP 端点不同源。
"""

from __future__ import annotations

import asyncio
import logging
import threading
import uuid
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("ask_user")

PREFIX = "ask_user:"
DEFAULT_TIMEOUT = 300.0
MIN_TIMEOUT = 0.5      # 留出可测试空间（单测用 0.5s 验证超时路径）
MAX_TIMEOUT = 1800.0
MAX_OPTIONS = 6
MAX_OPTION_CHARS = 80

_LOCK = threading.Lock()
# rid -> (future, loop)
_PENDING: Dict[str, Tuple[Any, Any]] = {}
_BROADCAST = None


def set_broadcast(fn) -> None:
    """注入推送函数（server.py 复用确认卡通道）。返回在线前端数则用于判断送达。"""
    global _BROADCAST
    _BROADCAST = fn


def pending_count() -> int:
    """当前挂起等待回答的提问数（诊断用）。"""
    with _LOCK:
        return len(_PENDING)


def clean_options(options: Any) -> List[str]:
    """规范化选项：去空、去重保序、截断、封顶 MAX_OPTIONS。"""
    if isinstance(options, str):
        options = [options]
    out: List[str] = []
    for o in options or []:
        if o is None:      # JSON null 会变成字符串 "None" 挂成一个按钮，必须挡掉
            continue
        s = str(o).strip()
        if not s or s in out:
            continue
        out.append(s[:MAX_OPTION_CHARS])
        if len(out) >= MAX_OPTIONS:
            break
    return out


def clamp_timeout(value: Any) -> float:
    """等待秒数：非法/非正 → 默认；越界 → 夹到 [MIN, MAX]。"""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT
    if f <= 0:
        return DEFAULT_TIMEOUT
    return max(MIN_TIMEOUT, min(MAX_TIMEOUT, f))


def resolve(request_id: str, value: str) -> bool:
    """前端答案回填。未知/已过期的 request_id 返回 False。

    空串是合法答案，语义为「用户跳过」——比一直挂着等超时好。
    """
    rid = str(request_id or "")
    with _LOCK:
        entry = _PENDING.pop(rid, None)
    if not entry:
        return False
    fut, loop = entry
    text = str(value or "").strip()

    def _deliver() -> None:
        if not fut.done():
            fut.set_result(text)

    try:
        loop.call_soon_threadsafe(_deliver)
    except Exception as e:  # 事件循环已关闭等
        logger.warning("提问答案投递失败 %s: %s", rid, e)
        return False
    return True


async def _emit(rid: str, question: str, options: List[str]) -> Optional[int]:
    """推送提问卡。返回在线前端数；未知（None）表示无法判断，按已送达处理。"""
    fn = _BROADCAST
    if not fn:
        return 0
    try:
        res = fn({
            "type": "bridge_confirm",
            "request_id": rid,
            "task": question,
            "options": options,
            "ask": True,
            "task_id": rid,
        })
        if asyncio.iscoroutine(res):
            res = await res
        return res if isinstance(res, int) else None
    except Exception as e:
        logger.warning("提问卡推送失败: %s", e)
        return 0


_SKIP_HINT = ("按你判断的合理默认继续，并在回复里说明你选了哪个默认——"
              "不要重复提问同一件事。")


async def ask(question: str, options: Any = None, timeout: Any = None) -> str:
    """问用户并等待回答，返回给模型的工具结果文本。"""
    q = str(question or "").strip()
    if not q:
        return "❌ 提问失败：question 不能为空。把要问的话写进 question 参数。"
    opts = clean_options(options)
    secs = clamp_timeout(timeout)

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return ("❌ 提问失败：当前上下文没有事件循环，等不到回答。"
                "直接在回复里问用户。")

    rid = PREFIX + uuid.uuid4().hex[:10]
    fut = loop.create_future()
    with _LOCK:
        _PENDING[rid] = (fut, loop)
    try:
        sent = await _emit(rid, q, opts)
        if sent == 0:
            return ("⚠️ 提问没送达：当前没有在线的前端页面。"
                    "直接在回复里把问题写出来，别调用本工具。")
        try:
            answer = await asyncio.wait_for(fut, timeout=secs)
        except asyncio.TimeoutError:
            return f"⏰ 等了 {int(secs)} 秒用户没回答。别重复提问——{_SKIP_HINT}"
    finally:
        with _LOCK:
            _PENDING.pop(rid, None)

    if not answer:
        return f"用户跳过了这个问题（没给答案）。别重复提问——{_SKIP_HINT}"
    return f"用户回答：{answer}"
