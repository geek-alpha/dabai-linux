"""任务中心 —— 大白智能体指挥中枢的统一任务注册表。

把「大白」麾下的所有代码助手与外部智能体纳入同一个任务模型：
- dsh         ：DeepSeek Harness 里的 AI 智能体（经 harness_bridge，需用户确认）
- codex-tool  ：本地 codex / opencode 长任务（/cx /ai），同样需用户确认
- bg          ：后台 shell 长任务（/bg）
- steps       ：多步 shell 编排

每个任务有 id、通道、状态机、进度步骤、实时日志、结果与归属 ws。
确认闸门对所有智能体委派（dsh/codex/opencode）一视同仁：未确认前一律停在
confirming 状态，绝不执行，防止智能体擅自切换/误操作。
每次状态/进度/日志变化都推送 task_event 增量事件（默认广播给所有活跃连接，
见 set_broadcast），前端任务中心据此实时渲染（接近 DSH 网页会话的"任务/工具调用"视图）。
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections import deque
from typing import Any, Callable, Optional

logger = logging.getLogger("task_orchestrator")

# 任务状态机
STATUS_CONFIRMING = "confirming"   # 等待用户确认（dsh 任务）
STATUS_QUEUED = "queued"           # 排队等待执行（同一通道串行）
STATUS_RUNNING = "running"         # 执行中
STATUS_DONE = "done"               # 成功完成
STATUS_ERROR = "error"             # 失败
STATUS_CANCELLED = "cancelled"     # 用户取消/中断

TASK_TTL = 6 * 3600  # 终态任务保留 6 小时后清理

# 事件出口：server 启动时注入广播器（server.py 的 _BROADCAST_WS）。
# 原先 task.ws 是「归属连接」，事件只推给发起者——前端一刷新、或换个设备看，
# 就再也收不到进度，只能靠任务中心 2.5s / DSH 卡片 5s 的全量轮询兜底
# （每次 700ms 往返，手机上肉眼可见地钝）。单用户单系统，广播不会串台。
# 未注入时退回单播，独立使用本模块的测试不受影响。
_BROADCAST: Optional[Callable[[dict], Any]] = None


def set_broadcast(fn: Optional[Callable[[dict], Any]]) -> None:
    """注入事件广播器：接收一条待发消息，负责投递给所有活跃连接。"""
    global _BROADCAST
    _BROADCAST = fn

# ---------- 智能体目录（Agent Directory） ----------
# 唯一的"谁是谁"事实源：DSH / OpenCode / Codex / 后台命令 / 多步命令。
# 前端任务中心与大屏、后端任务快照都从这里取值，保证永不混淆。
AGENTS = {
    "dsh": {
        "name": "DSH 智能体",
        "icon": "🤖",
        "color": "#7c5cff",
        "desc": "DeepSeek Harness 里的 AI 智能体 —— 解决复杂任务的好帮手。"
                "自带 bash/文件/搜索/联网等独立工具集，适合跨系统、多步骤、需要深入调查的复杂任务。"
                "任务会先请你确认，执行过程可打开 DSH 网页（127.0.0.1:3080）实时查看。",
    },
    "opencode": {
        "name": "OpenCode",
        "icon": "✦",
        "color": "#00c2ff",
        "desc": "本机 AI 编码助手 —— 解决日常问题。日常的小需求、快速改文件、写简单脚本、跑测试等"
                "常规编码活儿交给它，本机直跑、速度快。执行前同样会先请你确认，防误操作。",
    },
    "codex": {
        "name": "Codex",
        "icon": "⚙️",
        "color": "#ffb84d",
        "desc": "本机 AI 编码助手 —— 攻坚顶级难题。算法难题、复杂重构、棘手 bug、性能优化等"
                "高难度编码挑战交给它，本机直跑。执行前同样会先请你确认，防误操作。",
    },
    "shell": {
        "name": "后台命令",
        "icon": "💻",
        "color": "#4ade80",
        "desc": "直接在电脑上运行的命令（/bg），适合下载、构建、训练等长耗时任务，可在任务中心查看输出。",
    },
    "steps": {
        "name": "多步命令",
        "icon": "🧭",
        "color": "#b39ddb",
        "desc": "按顺序执行的多条命令编排（由 LLM 拆解），适合需要先查后改的任务。",
    },
    "media": {
        "name": "子智能体（媒体看护）",
        "icon": "👷",
        "color": "#f472b6",
        "desc": "大白派出的媒体子智能体：独立负责一段播放（听歌/看视频）直到播完，"
                "播完自动向主智能体汇报；多个子智能体互不影响，可并行干活。",
    },
    "sub": {
        "name": "子智能体（通用任务）",
        "icon": "🧠",
        "color": "#60a5fa",
        "desc": "大白下发的通用子智能体：在后台独立执行交给它的复杂任务（LLM+工具），"
                "可同时大量分派、各干各的，完成自动向主智能体汇报。",
    },
    "longrun": {
        "name": "长跑引擎",
        "icon": "♾️",
        "color": "#a78bfa",
        "desc": "无人值守推进长期目标的后台引擎（systemd 常驻，独立进程）。"
                "一轮 = 从台账取一个 active 目标 → 执行它的 next 原子动作 → 用「台账有没有被更新」"
                "判定进展 → 落盘 → 睡。每轮都在任务中心留痕：可看当前轮次与上一轮产出，也可一键停止。",
    },
}

_TYPE_LABEL = {
    "dsh": "DSH 智能体",
    "codex-tool": "本地编码助手",
    "bg": "后台命令",
    "steps": "多步命令",
    "media-worker": "子智能体（媒体看护）",
    "sub-agent": "子智能体（通用任务）",
    "longrun": "长跑引擎",
}


class Task:
    __slots__ = (
        "id", "kind", "channel", "title", "brief", "status",
        "steps", "logs", "result", "error", "ws", "confirm",
        "extra", "created_at", "updated_at", "dsh_session_id",
    )

    def __init__(self, kind: str, title: str, ws, brief: str = "",
                 confirm: bool = False, channel: Optional[str] = None,
                 extra: Optional[dict] = None):
        self.id = "task-" + uuid.uuid4().hex[:10]
        self.kind = kind
        self.channel = channel or kind
        self.title = title[:120]
        # 任务内容（brief）用于任务中心展示、DSH 提交与 codex 委派回显，必须保留完整任务：
        # 原先 4000 字符截断会让长任务显示"不完整"，甚至把截断后的任务提交给 DSH 智能体。
        self.brief = brief[:50000]
        self.status = STATUS_CONFIRMING if confirm else STATUS_QUEUED
        self.steps: list[str] = []          # 有意义的里程碑（如"已提交 DSH""DSH 执行了 N 步"）
        self.logs: deque[str] = deque(maxlen=300)  # 原始日志行
        self.result = ""
        self.error = ""
        self.ws = ws                          # 归属连接（事件只推给发起者）
        self.confirm = confirm
        self.extra = extra or {}
        self.created_at = time.time()
        self.updated_at = time.time()
        self.dsh_session_id = ""

    # ---------- 快照（供前端渲染） ----------

    def snapshot(self, full: bool = False) -> dict:
        agent_meta = AGENTS.get(self.channel) or {
            "name": self.channel or self.kind,
            "icon": "•", "color": "#8c8ca0", "desc": "",
        }
        if full:
            return {
                "id": self.id, "kind": self.kind,
                "channel": self.channel, "title": self.title,
                "brief": self.brief, "status": self.status,
                "steps": list(self.steps), "logs": list(self.logs),
                "result": self.result, "error": self.error,
                "confirm": self.confirm, "extra": self.extra,
                "dsh_session_id": self.dsh_session_id,
                "agent": agent_meta,
                "created_at": int(self.created_at * 1000),
                "updated_at": int(self.updated_at * 1000),
            }
        return {
            "id": self.id, "kind": self.kind, "channel": self.channel,
            "title": self.title, "status": self.status,
            "steps": list(self.steps[-4:]), "result": self.result[:200],
            "error": self.error[:200], "confirm": self.confirm,
            # 过程持久可见：列表也带「日志总数 + 尾巴」，前端刷新（F5）后
            # 跑马灯/过程立刻有历史可渲染，并据此判断详情是否滞后需要补拉。
            "logs_count": len(self.logs),
            "logs_tail": list(self.logs)[-8:],
            "extra": self.extra,
            "dsh_session_id": self.dsh_session_id,
            "agent": agent_meta,
            "created_at": int(self.created_at * 1000),
            "updated_at": int(self.updated_at * 1000),
        }

    @property
    def label(self) -> str:
        return _TYPE_LABEL.get(self.kind, self.channel or self.kind)


class TaskOrchestrator:
    """统一任务注册表 + 事件推送 + DSH 桥接串行执行器。"""

    def __init__(self) -> None:
        self._tasks: dict[str, Task] = {}
        self._lock = asyncio.Lock()
        # DSH 任务串行队列（一个 DSH 会话同一时间只跑一个任务）
        self._dsh_queue: asyncio.Queue[str] = asyncio.Queue()
        self._runner_started = False

    # ---------- 注册 / 查询 ----------

    async def create(self, kind: str, title: str, ws, brief: str = "",
                     confirm: bool = False, channel: Optional[str] = None,
                     extra: Optional[dict] = None, auto_run: bool = True) -> Task:
        """登记一个任务。

        auto_run=False：调用方自带执行器与状态机（子智能体、流程镜像等），
        不要在创建时就把它标记为「执行中」——那样「还在排队」会被显示成「执行中」。
        """
        task = Task(kind, title, ws, brief, confirm, channel, extra)
        async with self._lock:
            self._tasks[task.id] = task
        # 需要确认的任务：立即推送确认事件；否则立刻入队
        if confirm:
            await self._push(task, {"event": "confirming"})
        else:
            self._enqueue_dispatch(task, auto_run)
        return task

    def get(self, task_id: str) -> Optional[Task]:
        return self._tasks.get(task_id)

    def list(self, limit: int = 50) -> list[dict]:
        tasks = sorted(self._tasks.values(), key=lambda t: t.created_at, reverse=True)
        return [t.snapshot(full=False) for t in tasks[:limit]]

    async def gc(self) -> None:
        now = time.time()
        stale = [tid for tid, t in self._tasks.items()
                 if t.status in (STATUS_DONE, STATUS_ERROR, STATUS_CANCELLED)
                 and now - t.updated_at > TASK_TTL]
        for tid in stale:
            self._tasks.pop(tid, None)

    def clear_finished(self) -> int:
        """批量清除所有终态任务（done/error/cancelled），返回清除数量。"""
        finished = [tid for tid, t in self._tasks.items()
                    if t.status in (STATUS_DONE, STATUS_ERROR, STATUS_CANCELLED)]
        for tid in finished:
            self._tasks.pop(tid, None)
        return len(finished)

    # ---------- 状态变更（每个变更都推送 task_event） ----------

    async def _push(self, task: Task, patch: dict) -> None:
        if _BROADCAST is None and task.ws is None:
            return
        payload = {
            "type": "task_event",
            "event": {
                "id": task.id,
                "channel": task.channel,
                "kind": task.kind,
                "title": task.title,
                **patch,
            },
        }
        try:
            if _BROADCAST is not None:
                await _BROADCAST(payload)
            else:
                await task.ws.send_json(payload)
        except Exception:
            pass  # 连接断开就静默（前端打开任务中心时会拉全量）

    async def set_status(self, task: Task, status: str) -> None:
        task.status = status
        task.updated_at = time.time()
        await self._push(task, {"event": "status", "status": status})

    async def add_step(self, task: Task, text: str) -> None:
        task.steps.append(text[:500])
        task.updated_at = time.time()
        await self._push(task, {"event": "step", "step": text[:500]})

    async def add_log(self, task: Task, line: str) -> None:
        if line:
            task.logs.append(line[:500])
        task.updated_at = time.time()
        await self._push(task, {"event": "log", "log": line[:500]})

    async def add_logs(self, task: Task, lines: list[str]) -> None:
        """批量追加日志并只推送一次 task_event（高频日志流不刷屏）。"""
        kept = [str(l)[:500] for l in lines if l]
        if not kept:
            return
        for l in kept:
            task.logs.append(l)
        task.updated_at = time.time()
        await self._push(task, {"event": "logs", "logs": kept})

    async def set_result(self, task: Task, result: str, status: str = STATUS_DONE) -> None:
        task.result = result[:20000]
        task.status = status
        task.updated_at = time.time()
        await self._push(task, {"event": "result", "status": status, "result": result[:20000]})

    async def set_error(self, task: Task, error: str) -> None:
        task.error = error[:2000]
        task.status = STATUS_ERROR
        task.updated_at = time.time()
        await self._push(task, {"event": "error", "status": STATUS_ERROR, "error": error[:2000]})

    # ---------- 用户控制 ----------

    async def confirm(self, task_id: str, approve: bool) -> Optional[Task]:
        task = self.get(task_id)
        if task is None or task.status != STATUS_CONFIRMING:
            return task
        if approve:
            await self.set_status(task, STATUS_QUEUED)
            self._enqueue_dispatch(task)
        else:
            await self.set_status(task, STATUS_CANCELLED)
        return task

    async def restore(self, task_id: str, kind: str, title: str, ws, brief: str = "",
                      channel: Optional[str] = None, extra: Optional[dict] = None) -> Optional[Task]:
        """重启/热重载后恢复任务：以原 task_id 注册，直接进入 running（不再确认/排队）。
        用于把仍在运行的 codex/opencode 独立进程接回任务中心。"""
        if task_id in self._tasks:
            return None
        task = Task(kind, title, ws, brief, confirm=False, channel=channel, extra=extra)
        task.id = task_id
        task.status = STATUS_RUNNING
        async with self._lock:
            self._tasks[task_id] = task
        return task

    async def cancel(self, task_id: str, cancel_fn: Optional[Callable[[Task], Any]] = None) -> Optional[Task]:
        task = self.get(task_id)
        if task is None or task.status in (STATUS_DONE, STATUS_ERROR, STATUS_CANCELLED):
            return task
        if cancel_fn:
            try:
                res = cancel_fn(task)
                if asyncio.iscoroutine(res):
                    await res
            except Exception as e:
                logger.warning("task cancel fn 失败: %s", e)
        was = task.status
        await self.set_status(task, STATUS_CANCELLED)
        if was != STATUS_CANCELLED:
            await self._push(task, {"event": "status", "status": STATUS_CANCELLED})
        return task

    # ---------- 分派 ----------

    def _enqueue_dispatch(self, task: Task, auto_run: bool = True) -> None:
        if task.kind == "dsh":
            self._dsh_queue.put_nowait(task.id)
        elif auto_run:
            # 非 dsh 任务：由调用方的执行器负责推进，这里先标记为运行中。
            # auto_run=False 的任务自带执行器与状态机：擅自置 running 会把
            # 「还在并发池排队」显示成「执行中」，且这个 ensure_future 与真正的
            # 状态更新存在竞态，可能在任务已完成后又把它改回 running。
            asyncio.ensure_future(self.set_status(task, STATUS_RUNNING))

    # ---------- DSH 串行执行器 ----------

    async def start_runner(self) -> None:
        """启动 DSH 任务串行执行器（server 启动时调用一次）。"""
        if self._runner_started:
            return
        self._runner_started = True
        asyncio.ensure_future(self._dsh_worker_loop())
        asyncio.ensure_future(self._gc_loop())

    async def _dsh_worker_loop(self) -> None:
        from harness_bridge import get_bridge, HarnessBridgeError
        from harness_bridge import CONFIG_FILE  # noqa: F401  仅触发配置存在检查
        bridge = get_bridge()
        while True:
            task_id = await self._dsh_queue.get()
            task = self.get(task_id)
            if task is None:
                continue
            try:
                await self.set_status(task, STATUS_RUNNING)
                await self.add_step(task, "已提交给 DSH 智能体…")
                cwd = (task.extra or {}).get("cwd")

                # submit_task 的 on_progress 在轮询线程里被回调：写入共享队列，
                # 这里用一个并发的 asyncio 任务把进度敏感地变成任务步骤（实时展示）
                prog_lines: list[str] = []
                done = asyncio.Event()

                async def drain_progress():
                    idx = 0
                    last_added = ""
                    while not done.is_set():
                        while idx < len(prog_lines):
                            line = prog_lines[idx]
                            idx += 1
                            # 相同文本不重复刷屏（DSH 轮询会反复给出同一句"执行中…"）
                            if line and line != last_added:
                                last_added = line
                                await self.add_step(task, line)
                        await asyncio.sleep(0.8)

                drainer = asyncio.ensure_future(drain_progress())
                try:
                    reply = await asyncio.to_thread(
                        bridge.submit_task, task.brief, cwd, None,
                        lambda text: prog_lines.append(text or ""),
                    )
                finally:
                    done.set()
                    drainer.cancel()
                task.dsh_session_id = ""
                await self.set_result(task, reply or "（DSH 智能体没有返回文字回复）")
            except HarnessBridgeError as e:
                await self.set_error(task, str(e))
            except Exception as e:
                await self.set_error(task, f"{e.__class__.__name__}: {e}")

    async def _gc_loop(self) -> None:
        while True:
            await asyncio.sleep(600)
            try:
                await self.gc()
            except Exception:
                pass


_orchestrator: Optional[TaskOrchestrator] = None


def get_orchestrator() -> TaskOrchestrator:
    global _orchestrator
    if _orchestrator is None:
        _orchestrator = TaskOrchestrator()
    return _orchestrator
