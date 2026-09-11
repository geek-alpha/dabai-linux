# -*- coding: utf-8 -*-
"""媒体子智能体（Media Sub-Agents）—— 大白把「播放并在结束后汇报」的长耗时任务
交给独立子智能体全程负责，与 DSH 的 subagent 分工模型对应：

- 主智能体（大白）调用带 watch 的播放工具（music_play / video_play 的 watch=true）
  → server 在这里 spawn 一个媒体子智能体（worker）并登记到任务中心；
- worker 独立存在：有独立 id/状态机（playing → done/error/cancelled）、独立任务条目，
  不阻塞主智能体的对话循环；听歌看护、视频看护、不同连接之间的 worker 互不影响；
- 前端把「播完 / 停止 / 出错」等事件回报给 server → 对应 worker 进入终态；
- worker 进入 done/error 时通过 report 回调把结果反馈给主智能体（主智能体由
  此知道有哪些子进程干完了活）；cancelled 为静默终态（用户/设备主动停止，不打扰）。

主智能体视角：
- 随时可用 media_workers_list / media_worker_status 查看有哪些子进程在干活；
- 每轮对话的「当前状态」注入里也会列出正在运行的子智能体（agent.py 动态状态）。
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger("media_workers")

# 状态机
ST_PLAYING = "playing"
ST_PAUSED = "paused"
ST_DONE = "done"
ST_ERROR = "error"
ST_CANCELLED = "cancelled"

_ACTIVE = (ST_PLAYING, ST_PAUSED)

# 子智能体目录：团队名册（呈现用；任务中心通道名 = media）
KIND_META = {
    "music":    {"label": "听歌看护", "icon": "🎵", "desc": "负责一首在线歌曲从开播到播完，播完自动向主智能体汇报。"},
    "playlist": {"label": "歌单看护", "icon": "🎧", "desc": "负责一个歌单按顺序播完，全部播完后自动向主智能体汇报。"},
    "video":    {"label": "视频看护", "icon": "🎬", "desc": "负责一部视频从开播到播完，播完自动向主智能体汇报。"},
}

_STATUS_LABEL = {
    ST_PLAYING: "播放中",
    ST_PAUSED: "已暂停",
    ST_DONE: "已完成",
    ST_ERROR: "出错",
    ST_CANCELLED: "已结束",
}

# 注册表上限：超出时优先丢弃最老的终态 worker（防止无限增长）
_MAX_WORKERS = 80
_WATCHDOG_MAX = 6 * 3600   # 看护最长时间：超过即视为异常，静默取消


class MediaWorker:
    """一个媒体子智能体（一次「播放 + 盯到结束 + 汇报」的委托）。"""

    __slots__ = (
        "id", "kind", "title", "brief", "status", "result", "error",
        "ws", "owner", "extra", "created_at", "updated_at", "task", "watchdog",
    )

    def __init__(self, kind: str, title: str, ws, owner=None, brief: str = "",
                 extra: Optional[dict] = None):
        self.id = "agent-" + uuid.uuid4().hex[:10]
        self.kind = kind
        self.title = title[:120]
        self.brief = brief[:500]
        self.status = ST_PLAYING
        self.result = ""
        self.error = ""
        self.ws = ws                      # 归属连接（事件/汇报投递对象）
        self.owner = owner                # 不透明所有者（server 传入 WSState，汇报时取回）
        self.extra = extra or {}
        self.created_at = time.time()
        self.updated_at = time.time()
        self.task: Any = None             # 任务中心镜像 Task（task_orchestrator）
        self.watchdog: Optional[asyncio.Task] = None

    @property
    def meta(self) -> dict:
        return KIND_META.get(self.kind, {"label": self.kind, "icon": "•", "desc": ""})

    @property
    def kind_label(self) -> str:
        return self.meta["label"]

    @property
    def status_label(self) -> str:
        return _STATUS_LABEL.get(self.status, self.status)

    def snapshot(self, full: bool = False) -> dict:
        base = {
            "id": self.id,
            "kind": self.kind,
            "kind_label": self.kind_label,
            "icon": self.meta["icon"],
            "title": self.title,
            "status": self.status,
            "status_label": self.status_label,
            "created_at": int(self.created_at * 1000),
            "updated_at": int(self.updated_at * 1000),
        }
        if full:
            base.update({
                "brief": self.brief,
                "result": self.result,
                "error": self.error,
                "extra": self.extra,
                "task_id": self.task.id if self.task else "",
            })
        return base


class MediaWorkerManager:
    """媒体子智能体注册表：spawn / 查询 / 终态转换 + 汇报回调 + 任务中心镜像。"""

    def __init__(self) -> None:
        self._workers: dict[str, MediaWorker] = {}
        self._lock = asyncio.Lock()
        self._report_handler: Optional[Callable[[MediaWorker, str], Awaitable[Any]]] = None
        self._event_handler: Optional[Callable[[str, MediaWorker], Awaitable[Any]]] = None

    # ---------- 注入（server 启动时调用） ----------

    def set_report_handler(self, handler) -> None:
        """设置「worker 进入反馈终态（done/error）→ 通知主智能体」的回调。

        handler(worker, message) 是 async 函数，由 server 注入（内部转成
        _kickoff_response 让主智能体收到子智能体的汇报）。
        """
        self._report_handler = handler

    def set_event_handler(self, handler) -> None:
        """设置注册表事件回调（spawn/status/done/error/cancelled），供 UI 推送。"""
        self._event_handler = handler

    # ---------- 注册 / 查询 ----------

    async def spawn(self, ws, owner, kind: str, title: str, brief: str = "",
                    extra: Optional[dict] = None, replace: bool = True) -> MediaWorker:
        """派出一个媒体子智能体。

        replace=True（默认）：同连接上同类型还在跑的老 worker 自动静默收尾——
        现实里一个连接只有一个 <audio> / 一个大屏，同类型不可能同时播两部，
        新委托取代旧委托是正确的语义；不同类型（听歌 vs 看视频）互不影响。
        """
        kind = kind if kind in KIND_META else "music"
        async with self._lock:
            if replace:
                for w in list(self._workers.values()):
                    if (w.ws is ws and w.kind == kind and w.status in _ACTIVE):
                        await self._finish_internal(w, ST_CANCELLED, reason="被新的同类型播放取代")
            worker = MediaWorker(kind, title, ws, owner, brief, extra)
            self._workers[worker.id] = worker
            if len(self._workers) > _MAX_WORKERS:
                asyncio.ensure_future(self.sweep())
        await self._mirror_create(worker)
        await self._emit("spawn", worker)
        logger.info("[MediaWorker] 派出 %s 《%s》 [%s]",
                    worker.kind_label, worker.title, worker.id)
        return worker

    def get(self, worker_id: str) -> Optional[MediaWorker]:
        return self._workers.get(worker_id)

    def active(self) -> list[MediaWorker]:
        return [w for w in self._workers.values() if w.status in _ACTIVE]

    def list(self, limit: int = 50) -> list[dict]:
        ordered = sorted(self._workers.values(),
                         key=lambda w: w.created_at, reverse=True)
        return [w.snapshot(full=False) for w in ordered[:limit]]

    def active_text(self, limit: int = 6) -> str:
        """给主智能体看的「正在干活的子进程」摘要（注入每轮对话动态状态）。"""
        act = self.active()
        if not act:
            return ""
        lines = [f"【子智能体运行中（{len(act)} 个）】"]
        for w in act[:limit]:
            lines.append(f"{w.meta['icon']} {w.kind_label}《{w.title}》"
                         f"[{w.id}] {w.status_label}")
        return "\n".join(lines)

    # ---------- 事件推送 ----------

    async def _emit(self, event: str, worker: MediaWorker) -> None:
        if self._event_handler is None or worker.ws is None:
            return
        try:
            await self._event_handler(event, worker)
        except Exception as e:
            logger.warning("[MediaWorker] 事件推送失败: %s", e)

    # ---------- 终态转换 ----------

    async def start_watchdog(self, worker_id: str, deadline: Optional[float] = None) -> None:
        """给 worker 挂上超时看护：超过期限还没收到播完信号 → 静默取消。

        deadline 建议 = 预估时长 + 宽限；不给则按类型默认（防前端事件丢失悬挂）。
        """
        worker = self.get(worker_id)
        if worker is None:
            return
        if deadline is None:
            deadline = time.time() + _WATCHDOG_MAX
        worker.watchdog = asyncio.ensure_future(self._watchdog_loop(worker_id, deadline))

    async def complete(self, worker_id: str, message: str,
                       detail: Optional[dict] = None) -> Optional[MediaWorker]:
        """子智能体干完活：进入 done 并反馈主智能体（汇报消息由调用方组装）。"""
        async with self._lock:
            worker = self.get(worker_id)
            if worker is None or worker.status not in _ACTIVE:
                return worker
            await self._finish_internal(worker, ST_DONE, result=message, detail=detail,
                                        report=message)
        return worker

    async def fail(self, worker_id: str, error: str) -> Optional[MediaWorker]:
        async with self._lock:
            worker = self.get(worker_id)
            if worker is None or worker.status not in _ACTIVE:
                return worker
            report = f"《{worker.title}》{worker.kind_label}失败：{error}"
            await self._finish_internal(worker, ST_ERROR, error=error, report=report)
        return worker

    async def cancel(self, worker_id: str, reason: str = "") -> Optional[MediaWorker]:
        """静默收尾（用户停止播放/被取代/超时），不打扰主智能体。"""
        async with self._lock:
            worker = self.get(worker_id)
            if worker is None or worker.status not in _ACTIVE:
                return worker
            await self._finish_internal(worker, ST_CANCELLED, reason=reason)
        return worker

    async def _finish_internal(self, worker: MediaWorker, status: str, *,
                               result: str = "", error: str = "",
                               reason: str = "", report: str = "",
                               detail: Optional[dict] = None) -> None:
        worker.status = status
        worker.updated_at = time.time()
        if result:
            worker.result = result[:2000]
        if error:
            worker.error = error[:2000]
        if worker.watchdog is not None:
            worker.watchdog.cancel()
            worker.watchdog = None
        # 任务中心镜像：同步终态
        await self._mirror_finish(worker, status, result or error or reason, detail)
        await self._emit(status, worker)
        logger.info("[MediaWorker] %s 《%s》→ %s", worker.kind_label,
                    worker.title, _STATUS_LABEL.get(status, status))
        if report and self._report_handler is not None and worker.ws is not None:
            try:
                await self._report_handler(worker, report)
            except Exception as e:
                logger.warning("[MediaWorker] 汇报回调出错: %s", e)

    # ---------- 超时看护 ----------

    async def _watchdog_loop(self, worker_id: str, deadline: float) -> None:
        try:
            while True:
                remain = deadline - time.time()
                if remain <= 0:
                    break
                await asyncio.sleep(min(remain, 15.0))
            await self.cancel(worker_id, reason="超时未收到播完信号（自动收尾）")
        except asyncio.CancelledError:
            pass

    # ---------- 任务中心镜像（task_orchestrator） ----------

    async def _mirror_create(self, worker: MediaWorker) -> None:
        try:
            from task_orchestrator import get_orchestrator
            title = f"{worker.kind_label}：《{worker.title}》"
            worker.task = await get_orchestrator().create(
                kind="media-worker",
                title=title,
                ws=worker.ws,
                brief=worker.brief or title,
                confirm=False,
                channel="media",
                extra={"worker_id": worker.id, "worker_kind": worker.kind},
            )
            from task_orchestrator import get_orchestrator as _go
            await _go().add_step(worker.task, f"子智能体 [{worker.id}] 已派出，开始 {worker.kind_label}")
        except Exception as e:
            worker.task = None
            logger.warning("[MediaWorker] 任务中心镜像创建失败: %s", e)

    async def _mirror_finish(self, worker: MediaWorker, status: str, tail: str,
                             detail: Optional[dict] = None) -> None:
        task = worker.task
        if task is None:
            return
        try:
            from task_orchestrator import (
                get_orchestrator, STATUS_DONE, STATUS_ERROR, STATUS_CANCELLED,
            )
            orch = get_orchestrator()
            if status == ST_DONE:
                await orch.set_result(task, tail or "子智能体已完成看护", STATUS_DONE)
            elif status == ST_ERROR:
                await orch.set_error(task, tail or "子智能体看护出错")
            else:
                await orch.set_status(task, STATUS_CANCELLED)
                if tail:
                    await orch.add_step(task, f"（{tail}）")
        except Exception as e:
            logger.warning("[MediaWorker] 任务中心镜像更新失败: %s", e)

    async def sweep(self) -> None:
        """注册表裁剪：终态 worker 保留最近 _MAX_WORKERS 条（老的丢弃）。"""
        stale = []
        for w in self._workers.values():
            if w.status not in _ACTIVE:
                stale.append(w)
        stale.sort(key=lambda w: w.updated_at, reverse=True)
        for w in stale[_MAX_WORKERS // 2:]:
            self._workers.pop(w.id, None)


_media_workers: Optional[MediaWorkerManager] = None


def get_media_workers() -> MediaWorkerManager:
    """媒体子智能体管理器单例（进程内唯一注册表）。"""
    global _media_workers
    if _media_workers is None:
        _media_workers = MediaWorkerManager()
    return _media_workers
