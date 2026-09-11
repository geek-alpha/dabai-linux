"""DeepSeek Harness（DSH）桥接客户端 —— 让「大白」的智能体能够驱动 DSH 里的 AI 智能体。

通过 DSH Web 壳子的 /api RPC 网关（http://127.0.0.1:3080）：
- 每个桥接任务复用一个持久会话（session.create + session.prompt），
  任务文本以用户消息进入 DSH 会话，DSH 智能体像任何其他会话一样执行工具并回复；
- 轮询 session.history 取回完成后的回复文本（无需监听 WebSocket，稳定简单）；
- 用户的「确认」在 dabai 前端完成（见 server.py 的 /api/bridge/confirm）——
  本模块只负责真正执行与取回结果。

协议参考 DSH 源码（packages/host/apiproxy/src/api/）：
POST /api/<method>  body = {"type":"client-request","rpcId":"<uuid>","method":"<method>","payload":{...}}
响应 = {"type":"server-response","rpcId":"...","result":{"ok":true,"value":...}|{"ok":false,"error":{...}}}
Content-Type 必须为 application/json；Host 必须为 loopback（信任围栏）。
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional

try:
    import requests
    _HAS_REQUESTS = True
except ImportError:  # pragma: no cover
    _HAS_REQUESTS = False

logger = logging.getLogger("harness_bridge")

BASE_DIR = Path(__file__).parent.resolve()
CONFIG_FILE = BASE_DIR / "harness_bridge.json"

DEFAULT_CONFIG = {
    # DSH Web 壳子地址（本地回环，信任围栏要求 loopback Host）
    "dsh_base_url": "http://127.0.0.1:3080",
    # 桥接会话的工作目录（DSH 智能体在这个目录下执行任务）
    "cwd": str(BASE_DIR),
    # DSH 会话使用的 agentPreset（'code' 提供 bash/文件工具；失败时自动降级为默认组合）
    "agent_preset": "code",
    # 是否需要用户确认后才执行（默认 true —— 安全要求）
    "require_confirm": True,
    # 轮询间隔（秒）
    "poll_interval": 1.5,
    # 兼容旧配置的「任务超时」：作为 stall_timeout 的旧别名保留
    "timeout": 600,
    # 动态超时（基于“最近活动时间”）：连续这么久没有出现任何新事件/新文本、
    # 且没有进行中的工具调用时，才判定任务停滞并中断；
    # 只要 DSH 还在产出新事件/新文本，就持续等待，不再按固定总时长掐断
    "stall_timeout": 600,
    # 工具调用进行中（事件流尾部为 tool/call）的停滞容忍时间（秒）：
    # 工具执行期间可能长时间不产生中间事件，给更宽的等待窗口
    "tool_stall_timeout": 3600,
    # 判定"回复稳定"的安静时间（秒）：最后一条消息之后多久没有新活动视为完成
    "settle_seconds": 5.0,
    # 每个任务使用全新会话（默认 true）：避免长上下文堆积造成上下文压力；
    # 设为 false 恢复按 cwd 复用会话的旧行为。
    "fresh_session_per_task": True,
    # 已创建会话的持久映射 {cwd: sessionId}（仅记录最近一次任务用，供取消）
    "session_ids": {},
}


def _dsh_task_payload(task: str) -> str:
    """DSH 任务同样注入：清理安全守则 + 复杂任务规范 + 续接指引。"""
    try:
        from codex_runner import (
            _cleanup_safety_appendix,
            _task_spec_appendix, _continuation_appendix,
        )
        return (task + _cleanup_safety_appendix(task)
                + _task_spec_appendix(task)
                + _continuation_appendix(task))
    except Exception:
        return task


_HARNESS_RPC_TIMEOUT = 20  # 单次 RPC 超时（秒）


class HarnessBridgeError(Exception):
    """桥接错误（DSH 不可达 / RPC 失败 / 任务失败）。"""


def _load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                saved = json.load(f)
            if isinstance(saved, dict):
                for k in DEFAULT_CONFIG:
                    if k in saved:
                        cfg[k] = saved[k]
        except Exception as e:
            logger.warning("加载 harness_bridge.json 失败: %s", e)
    if not isinstance(cfg.get("session_ids"), dict):
        cfg["session_ids"] = {}
    return cfg


def _save_config(cfg: dict) -> None:
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning("保存 harness_bridge.json 失败: %s", e)


class HarnessBridge:
    """DSH RPC 客户端与任务执行器（调用方串行使用即可）。"""

    def __init__(self, base_url: Optional[str] = None) -> None:
        if not _HAS_REQUESTS:
            logger.error("缺少 requests 库，桥接不可用")
        self._file_cfg = _load_config()
        if base_url:
            self._file_cfg["dsh_base_url"] = base_url.rstrip("/")
        self.base_url = self._file_cfg["dsh_base_url"]
        self._session_ids: dict[str, str] = dict(self._file_cfg.get("session_ids", {}))
        self._lock = None  # 调用方（server.py）用 asyncio 队列保证串行

    # ---------- 底层 RPC ----------

    def _rpc(self, method: str, payload: dict, timeout: int = _HARNESS_RPC_TIMEOUT) -> Any:
        """发一次 client-request，返回 RPC 成功后的 value；失败抛 HarnessBridgeError。"""
        if not _HAS_REQUESTS:
            raise HarnessBridgeError("缺少 requests 库，无法连接 DSH")
        body = {
            "type": "client-request",
            "rpcId": "bridge-" + uuid.uuid4().hex[:16],
            "method": method,
            "payload": payload,
        }
        url = self.base_url + "/api/" + method
        try:
            resp = requests.post(url, json=body, timeout=timeout,
                                 headers={"Content-Type": "application/json"})
        except Exception as e:
            raise HarnessBridgeError(
                "无法连接 DSH（" + self.base_url + "）：" + e.__class__.__name__ + ": " + str(e) +
                "。请确认 DeepSeek Harness 的 Web 界面（dsh web）正在运行。"
            ) from e
        if resp.status_code != 200:
            raise HarnessBridgeError("DSH RPC HTTP " + str(resp.status_code) + ": " + resp.text[:200])
        try:
            msg = resp.json()
        except Exception as e:
            raise HarnessBridgeError("DSH 返回非 JSON: " + resp.text[:200]) from e
        if not isinstance(msg, dict) or msg.get("type") != "server-response":
            raise HarnessBridgeError("DSH 返回异常报文: " + str(msg)[:200])
        result = msg.get("result") or {}
        if not result.get("ok"):
            err = result.get("error") or {}
            raise HarnessBridgeError("DSH 拒绝 " + method + ": " + str(err.get("message", str(err)))[:300])
        return result.get("value")

    def ping(self) -> bool:
        """探测 DSH 是否可达（只读 session.list）。"""
        try:
            self._rpc("session.list", {})
            return True
        except Exception:
            return False

    # ---------- 会话管理 ----------

    def ensure_session(self, cwd: str, fresh: bool = False) -> str:
        """返回用于执行任务的 DSH 会话。

        fresh=True（每任务新会话）：总是创建全新会话并把映射指向它——
        旧会话留在 DSH 侧不参与新任务，从根本上避免长上下文堆积；
        fresh=False：按 cwd 复用已建会话（兼容旧行为）。
        """
        cwd = str(cwd or self._file_cfg.get("cwd") or BASE_DIR)
        key = os.path.normcase(os.path.abspath(cwd))
        if not fresh:
            if key in self._session_ids:
                sid = self._session_ids[key]
                try:
                    self._rpc("session.list", {})
                    return sid
                except Exception:
                    pass  # 列表失败只是探测，不阻断复用
        session_id = "session-bridge-" + uuid.uuid4().hex[:12]
        payload: dict[str, Any] = {"cwd": cwd, "sessionId": session_id}
        preset = self._file_cfg.get("agent_preset")
        if preset:
            payload["agentPreset"] = preset
        try:
            value = self._rpc("session.create", payload)
        except HarnessBridgeError as e:
            # 预设不存在等错误 → 去掉预设重试（部署可能没有该组合）
            if "preset" in str(e).lower():
                logger.warning("agentPreset=%s 创建失败，回退默认组合: %s", preset, e)
                value = self._rpc("session.create", {"cwd": cwd, "sessionId": session_id})
            else:
                raise
        sid = (value or {}).get("sessionId") or session_id
        self._session_ids[key] = sid
        self._file_cfg["session_ids"] = dict(self._session_ids)
        _save_config(self._file_cfg)
        logger.info("[Bridge] DSH 桥接会话就绪: %s (cwd=%s, fresh=%s)", sid, cwd, fresh)
        return sid

    # ---------- 任务执行 ----------

    @staticmethod
    def _assistant_text(events: list) -> str:
        """从 history events 中提取「最后一条」assistant/message 的纯文本内容。

        一个任务往往产生多条 assistant/message（模型分步生成），
        只有最后一条才是本轮任务的最终答复。
        """
        last_text = ""
        for entry in events or []:
            ev = (entry or {}).get("event") or {}
            if ev.get("type") != "assistant/message":
                continue
            data = ev.get("data") or {}
            content = data.get("message", {}).get("content") or []
            text = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text" and block.get("text"):
                    text.append(block["text"])
            if text:
                last_text = "\n".join(text)
        return last_text

    def submit_task(self, task: str, cwd: Optional[str] = None,
                    timeout: Optional[int] = None,
                    on_progress=None) -> str:
        """把一个任务发给 DSH 智能体，等待其完成并返回最终回复文本。

        on_progress(status_text) 在轮询期间被回调（可选，便于上层展示进度）。
        超时采用“最近活动时间”动态判定：任何新事件/新文本都会刷新活动时间，
        只有连续 stall_timeout 秒没有任何进展（且无进行中工具调用）才判定停滞超时；
        只要任务持续产出，就无限等待，不再受固定总时长限制。
        """
        if not task or not str(task).strip():
            raise HarnessBridgeError("任务内容为空")
        # timeout 参数/旧配置 timeout 均作为 stall_timeout 的别名保留
        stall_timeout = float(
            timeout if timeout is not None
            else (self._file_cfg.get("stall_timeout")
                  or self._file_cfg.get("timeout") or 600)
        )
        tool_stall_timeout = float(
            self._file_cfg.get("tool_stall_timeout")
            or max(stall_timeout * 6, 3600)
        )
        cwd = str(cwd or self._file_cfg.get("cwd") or BASE_DIR)
        # 每任务新会话（fresh_session_per_task，默认开启）：
        # 新任务总是从干净上下文开始，避免多任务堆积导致上下文压力；
        # 关闭时恢复按 cwd 复用会话，让记忆跨任务延续。
        fresh = bool(self._file_cfg.get("fresh_session_per_task", True))
        sid = self.ensure_session(cwd, fresh=fresh)

        # 1) 记录当前日志末尾 seq，作为本次任务的起点
        hist = self._rpc("session.history", {"sessionId": sid, "maxMessages": 30})
        base_seq = -1
        for entry in (hist or {}).get("events") or []:
            seq = ((entry or {}).get("event") or {}).get("seq")
            if isinstance(seq, int) and seq > base_seq:
                base_seq = seq

        # 2) 发送任务（作为一条用户消息进入 DSH 会话）
        prompt = (
            "你是 DeepSeek Harness（DSH）里的 AI 智能体，正在协助虚拟角色「大白」和它的用户。\n"
            "用户/角色请求的任务如下（工作目录：" + cwd + "）：\n"
            "------\n" + _dsh_task_payload(str(task)).strip() + "\n------\n"
            "请实际执行并调查，然后用简洁的中文汇报：你做了什么、结果/发现、有哪些需要注意的地方。"
            "你的回复会原样回传给「大白」和用户。不要改动与任务无关的文件。"
        )
        self._rpc("session.prompt", {
            "sessionId": sid,
            "mode": "queue",
            "content": [{"type": "text", "text": prompt}],
        })

        # 3) 轮询直到完成：turn/end 是最可靠的完成信号；没有 turn/end 时，
        #    必须同时满足「已产出文字回复 + 事件流尾部是 assistant/message +
        #    事件流整体静默 settle 秒」才判定完成。只看"文本静默"会在模型
        #    输出完一段文字后继续调用工具时误判完成（任务中心提前显示已完成）。
        #    超时不做固定总时长限制：任何新事件/新文本都会刷新 last_activity，
        #    只有连续 stall_timeout 秒没有任何进展（且无进行中工具调用）才判停滞超时，
        #    避免长时间运行但持续有进展的任务被固定时限误杀。
        last_activity = time.monotonic()  # 最近一次出现活动（新事件/新文本）的时间
        quiet_since = None            # 事件流最后一次出现新事件的时间（任意类型都算）
        last_event_count = 0          # 上次轮询时的 fresh 事件数
        last_text = ""
        saw_reply = False             # 是否已收到 assistant 文字回复
        interval = float(self._file_cfg.get("poll_interval", 1.5))
        settle = float(self._file_cfg.get("settle_seconds", 5.0))
        while True:
            time.sleep(interval)
            try:
                hist = self._rpc("session.history", {"sessionId": sid, "maxMessages": 60})
            except HarnessBridgeError:
                continue
            events = (hist or {}).get("events") or []
            # 只统计本次任务（seq > base_seq）之后的事件，避免把历史回复一起带上
            fresh = [entry for entry in events
                     if isinstance(((entry or {}).get("event") or {}).get("seq"), int)
                     and ((entry or {}).get("event") or {}).get("seq") > base_seq]
            # 实际进度：DSH 智能体已执行的工具调用次数（"有效进程"信号）
            tool_steps = sum(1 for entry in fresh
                             if ((entry or {}).get("event") or {}).get("type") == "tool/call")
            # 事件流尾部类型：尾部还是 tool/call 等说明工具正在执行，绝不能判定完成
            tail_type = ""
            if fresh:
                tail_type = ((fresh[-1] or {}).get("event") or {}).get("type") or ""
            for entry in fresh:
                ev = (entry or {}).get("event") or {}
                if ev.get("type") == "turn/end":
                    text = self._assistant_text(fresh).strip()
                    if text:
                        last_text = text
                    if on_progress:
                        on_progress("完成")
                    return last_text or "（DSH 智能体没有返回文字回复）"
            cur_text = self._assistant_text(fresh).strip()
            if cur_text:
                if cur_text != last_text:
                    last_text = cur_text
                    # 文本继续变化同样是活动：刷新活动时间与静默计时，
                    # 避免“文本仍在增长但事件数不变”时被误判完成/停滞
                    now = time.monotonic()
                    last_activity = now
                    quiet_since = now
                    if on_progress:
                        on_progress("DSH 智能体正在生成回复…")
                saw_reply = True
            # 任意类型的新事件（assistant 消息/工具调用/工具结果…）都代表 DSH 还在工作，
            # 刷新最近活动时间与静默计时；只有整体事件流安静下来才可能真正收尾
            if len(fresh) != last_event_count:
                last_event_count = len(fresh)
                now = time.monotonic()
                quiet_since = now
                last_activity = now
            if (saw_reply and tail_type == "assistant/message"
                    and quiet_since is not None
                    and time.monotonic() - quiet_since >= settle):
                return last_text or "（DSH 智能体没有返回文字回复）"
            if on_progress:
                on_progress(("执行中 · DSH 已执行 " + str(tool_steps) + " 步工具调用…")
                            if tool_steps else "执行中 · DSH 正在分析任务…")
            # 动态超时判定：只按“最近活动时间”走，不设固定总时长。
            # 工具调用进行中（尾部仍是 tool/call）时给更宽的停滞容忍窗口，
            # 避免工具长时间执行但未产生中间事件时被误杀。
            now = time.monotonic()
            tool_in_flight = tail_type == "tool/call"
            limit = tool_stall_timeout if tool_in_flight else stall_timeout
            if now - last_activity > limit:
                msg = ("任务停滞超时（连续 " + str(int(limit)) + "s 无新活动"
                       + ("，工具调用无进展" if tool_in_flight else "")
                       + "）：DSH 智能体最近一次活动后未再产出任何进展")
                raise HarnessBridgeError(msg)

    def cancel(self, cwd: Optional[str] = None) -> bool:
        """中断当前桥接会话的进行中任务（尽力而为）。"""
        cwd = str(cwd or self._file_cfg.get("cwd") or BASE_DIR)
        sid = self._session_ids.get(os.path.normcase(os.path.abspath(cwd)))
        if not sid:
            return False
        try:
            self._rpc("session.cancel", {"sessionId": sid})
            return True
        except Exception:
            return False

    def config_view(self) -> dict:
        return {
            "base_url": self.base_url,
            "cwd": self._file_cfg.get("cwd"),
            "agent_preset": self._file_cfg.get("agent_preset"),
            "require_confirm": bool(self._file_cfg.get("require_confirm", True)),
            "reachable": self.ping(),
        }


_bridge: Optional[HarnessBridge] = None


def get_bridge() -> HarnessBridge:
    global _bridge
    if _bridge is None:
        _bridge = HarnessBridge()
    return _bridge
