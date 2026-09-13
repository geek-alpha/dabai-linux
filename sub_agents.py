# -*- coding: utf-8 -*-
"""通用子智能体（General Sub-Agents）—— 主智能体把任意复杂任务下发给独立子智能体，
与 DSH 的 subagent 分工模型对应，是对 media_workers 的一般化：

- 主智能体调用 sub_agent_spawn(task=...) → 这里登记一个子智能体（worker）并立即
  返回任务中心条目；worker 在后台跑自己的「LLM 思考 + 工具执行」循环，不阻塞主对话；
- 可同时大量分派：每个 worker 任务不同、互不影响；全局并发有上限（默认 4，
  settings.json -> agent.sub_max_concurrent 可调），超出并发自动排队执行；
- worker 的进度/日志/结果实时写入任务中心（channel=sub），完成后通过 report 回调
  反馈给主智能体（由 server 转成「子智能体汇报」，主智能体得知后向用户转述）；
- 工具：与主智能体共用 harness 技能/插件路由 + 本地工具兜底；
  子智能体工具列表里排除 sub_agent_* 自身，防止无限递归下发。

与媒体子智能体的区别：media_workers 靠前端播放事件驱动完成（等待类任务），
这里是自驱动的通用执行类任务（LLM+工具直到得出结果）。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger("sub_agents")

# 状态机
ST_QUEUED = "queued"
ST_RUNNING = "running"
ST_DONE = "done"
ST_ERROR = "error"
ST_CANCELLED = "cancelled"

_STATUS_LABEL = {
    ST_QUEUED: "排队中",
    ST_RUNNING: "执行中",
    ST_DONE: "已完成",
    ST_ERROR: "出错",
    ST_CANCELLED: "已取消",
}

MAX_CONCURRENT = 4          # 全局并发上限（超出排队），可经 settings.json -> agent.sub_max_concurrent 覆盖
MAX_ROUNDS = 0             # 单 worker 最大「思考+工具」轮次（0=不限制，与主智能体一致），可经 settings.json -> agent.sub_max_tool_rounds 覆盖
MAX_RUNTIME = 2 * 60 * 60       # 单 worker 最长运行时间（秒），防悬挂（默认 2 小时）
SUB_MAX_EST_TOKENS = 600000  # 单 worker 预估 token 预算（防烧钱），可经 settings.json -> agent.sub_max_est_tokens 覆盖
_MAX_WORKERS = 120          # 注册表上限

# 审计落盘：注册表在内存里，进程一重启就查不到 worker 干过什么（只剩产物文件 mtime 可反推）。
_HIST_FILE = Path(__file__).resolve().parent / "data" / "sub_agents.jsonl"
_HIST_MAX_BYTES = 2 * 1024 * 1024   # 超过就截断
_HIST_KEEP = 200                    # 截断/回灌保留的条数


def _hist_trim() -> None:
    """文件超限时只保留最近 _HIST_KEEP 行，防无限增长。"""
    try:
        if _HIST_FILE.stat().st_size <= _HIST_MAX_BYTES:
            return
        keep = _HIST_FILE.read_text(encoding="utf-8").splitlines()[-_HIST_KEEP:]
        _HIST_FILE.write_text("\n".join(keep) + "\n", encoding="utf-8")
    except Exception as e:
        logger.warning("[SubAgent] 审计截断失败: %s", e)


def _hist_record(worker, event: str, note: str = "") -> None:
    """把 worker 的一次状态变更追加进审计日志（data/sub_agents.jsonl）。

    失败只告警、不抛——审计绝不能成为执行链路的故障源。
    """
    try:
        rec = {
            "id": worker.id,
            "kind": worker.kind,
            "title": worker.title,
            "task": worker.task[:600],
            "profile": worker.profile,
            "status": worker.status,
            "status_label": worker.status_label,
            "event": event,
            "note": note[:300],
            "created_at": int(worker.created_at * 1000),
            "updated_at": int(worker.updated_at * 1000),
            "ts": time.time(),
            "result": worker.result[-1500:],
            "error": worker.error[:600],
        }
        _HIST_FILE.parent.mkdir(parents=True, exist_ok=True)
        with _HIST_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        _hist_trim()
    except Exception as e:
        logger.warning("[SubAgent] 审计落盘失败: %s", e)


def _max_rounds() -> int:
    """读取子智能体最大轮次（settings.json -> agent.sub_max_tool_rounds）。

    0 / 负数表示不限制（与主智能体 max_tool_rounds 语义一致），默认 MAX_ROUNDS=0。
    """
    try:
        from agent import load_config
        v = int(load_config().get("agent", {}).get("sub_max_tool_rounds", MAX_ROUNDS) or MAX_ROUNDS)
        return v if v > 0 else 0
    except Exception:
        return MAX_ROUNDS


def _max_concurrent() -> int:
    """全局并发上限（settings.json -> agent.sub_max_concurrent），默认 MAX_CONCURRENT=4。"""
    try:
        from agent import load_config
        v = int(load_config().get("agent", {}).get("sub_max_concurrent", MAX_CONCURRENT) or MAX_CONCURRENT)
        return v if v > 0 else MAX_CONCURRENT
    except Exception:
        return MAX_CONCURRENT


def _max_est_tokens() -> int:
    """单 worker 上下文 token 预算（settings.json -> agent.sub_max_est_tokens）。

    默认 SUB_MAX_EST_TOKENS（60 万），量的是**当前上下文**而不是累计消耗。
    """
    try:
        from agent import load_config
        v = int(load_config().get("agent", {}).get("sub_max_est_tokens", SUB_MAX_EST_TOKENS) or SUB_MAX_EST_TOKENS)
        return v if v > 0 else SUB_MAX_EST_TOKENS
    except Exception:
        return SUB_MAX_EST_TOKENS


# 上下文保留：最近多少条消息完整交给 LLM（约 30 轮工具往返）
_KEEP_RECENT_MESSAGES = 60
# 更早消息的字符上限：保留结论，砍掉大段工具输出
_OLD_MESSAGE_CAP = 600
# 单工具执行超时兜底（秒）；正常走 agent.tool_exec_config，与主智能体同口径
_TOOL_TIMEOUT_FALLBACK = 30.0


def _fit_context(messages: list) -> list:
    """给 LLM 的消息视图：system + 最近若干条完整 + 更早的压成片段。

    从前这里是 `messages[-120:]` 硬切：第 121 条之前的**全部**历史（含任务中途的
    关键发现、已确认的路径与结论）直接消失。长任务的典型症状就是「干到一半开始
    失忆」——反复重读同一批文件、重新探索走过的路，越干越慢还烧 token。
    这是最典型的吃力不讨好：省了 token，赔上的是整个任务的效率。

    现在不丢信息：更早的消息仍在（结论、路径、判断都留着），只把长内容压短。
    压缩只改 content 长度，不动 role / tool_calls 结构，function-calling 校验不受影响。
    """
    if len(messages) <= _KEEP_RECENT_MESSAGES + 1:
        return messages
    head = messages[0]
    older = messages[1:-_KEEP_RECENT_MESSAGES]
    out = [head]
    for m in older:
        c = str(m.get("content") or "")
        if len(c) > _OLD_MESSAGE_CAP:
            # 必须复制：直接改会污染原始历史，压缩就变成永久丢失
            m = dict(m)
            m["content"] = c[:_OLD_MESSAGE_CAP] + "…【已压缩】"
        out.append(m)
    out.extend(messages[-_KEEP_RECENT_MESSAGES:])
    return out


def _plan_batches(pending):
    """把本轮工具切成可安全并行的批次（harness.tool_sched.plan）。

    与主智能体共用同一个调度器：只读一批并行，写工具按资源键分桶、同键必分属不同批。
    调度器不可用/出错 → 返回 None，调用方退回串行（宁可慢，不可错）。
    """
    try:
        from harness.tool_sched import plan
        return plan(pending) or None
    except Exception as e:
        logger.warning("[SubAgent] 分批调度失败，本轮退回串行: %s", e)
        return None


def _get_cache():
    """只读工具结果缓存（harness.tool_sched）。拿不到就当没有缓存，绝不成为故障源。"""
    try:
        from harness.tool_sched import get_cache
        return get_cache()
    except Exception:
        return None


def _result_fp(result) -> str:
    """工具结果指纹。优先复用主智能体的实现，保证两处判据口径一致。

    各写一份迟早漂移（一边改了截断策略、另一边没改，判据就悄悄分叉），
    所以这里以 agent._result_fp 为单一来源，导入失败才用本地等价实现兜底。
    """
    try:
        from agent import _result_fp as _rf
        return _rf(result)
    except Exception:
        try:
            s = result if isinstance(result, str) else str(result)
            return hashlib.blake2b(s.encode("utf-8", "replace"),
                                   digest_size=8).hexdigest()
        except Exception:
            return "?"


# 死循环判据：连续 N 轮「调用相同 且 结果指纹也相同」才算原地打转。
# 只看调用会把合理轮询误杀——同一个进度查询工具连调 3 轮、结果
# 10% → 30% → 60% 明明在推进，却会被直接硬停（原实现正是如此）。
# 子智能体是后台任务、用户不在场，停错了要重新派，所以宁可多给一次机会。
LOOP_SAME_ROUNDS = 4
LOOP_HINT = (
    "【循环提醒】你连续几轮调用了相同的工具、且拿到完全相同的结果，"
    "说明这样下去不会有新信息。请立即换一种方法/工具/参数，"
    "或直接基于已有信息给出结论；如果确实卡住无法推进，就说明卡点后结束。"
)


def _note_round(recent_rounds: list, fps: list, results: dict, order) -> bool:
    """记录本轮「调用指纹 + 结果指纹」，返回是否已构成原地打转。

    单独成函数是为了可测：这是「误杀合理轮询」与「放过真死循环」之间唯一的
    分界线。它出错的方式很隐蔽——不报错，只是任务莫名中断（误杀）或者白烧
    几十轮（漏判），所以两边都必须有独立用例钉住。

    判据要求**调用和结果同时重复**：结果在变的重复调用是正常轮询
    （等生成进度、等构建完成），绝不能拦。
    """
    sigs = tuple(_result_fp(results.get(i)) for i in order)
    recent_rounds.append((tuple(tuple(x) for x in fps), sigs))
    del recent_rounds[:-LOOP_SAME_ROUNDS]
    if len(recent_rounds) >= LOOP_SAME_ROUNDS:
        return all(r == recent_rounds[0] for r in recent_rounds)
    return False


def _load_profile(pid: str) -> Optional[dict]:
    """读智能体档案。未指定 / 不存在 / 读盘失败 → None（退回全能力通用执行者）。

    档案是「增强」不是「依赖」：档案坏了、文件被删了，子智能体照样得能干活。
    """
    pid = str(pid or "").strip()
    if not pid:
        return None
    try:
        from agent_profiles import get_profile
        p = get_profile(pid)
        if p is None:
            logger.warning("[SubAgent] 档案 %s 不存在，退回全能力通用执行者", pid)
        return p
    except Exception as e:
        logger.warning("[SubAgent] 档案加载失败（%s），退回全能力通用执行者: %s", pid, e)
        return None


def _tool_timeout(tool_name: str) -> float:
    """单工具执行超时（秒）。与主智能体共用 agent.tool_exec_config，口径一致不漂移。"""
    try:
        from agent import tool_exec_config
        return float(tool_exec_config(tool_name).get("timeout") or _TOOL_TIMEOUT_FALLBACK)
    except Exception:
        return _TOOL_TIMEOUT_FALLBACK


# 单次 LLM 调用的硬超时（秒）：与 harness.runtime 默认 llm_timeout 一致。
# openai 客户端默认 600s，配上 3 次重试最坏能挂半小时——而子智能体全程占着
# 全局并发槽位（默认只有 4 个），一个卡住的 worker 等于四分之一产能停摆。
_LLM_TIMEOUT = 180.0


def _record_usage(resp) -> None:
    """把子智能体的 LLM 用量记进 harness 台账（渠道 sub_agent）。

    从前这里把 usage 直接丢了：主智能体每轮都记账，子智能体却完全不入账。
    子智能体是「可大量分派」的，消耗可能比主对话还大——不入账等于成本不可见，
    用户也就无从判断一次大规模并行到底值不值。记账失败绝不影响主流程。
    """
    try:
        u = getattr(resp, "usage", None)
        if u is None:
            return
        p = int(getattr(u, "prompt_tokens", 0) or 0)
        c = int(getattr(u, "completion_tokens", 0) or 0)
        t = int(getattr(u, "total_tokens", 0) or 0)
        if not (p or c or t):
            return
        from agent import _get_runtime
        runtime = _get_runtime()
        if runtime is None:
            return
        ch = int(getattr(u, "prompt_cache_hit_tokens", 0) or 0)
        cm = int(getattr(u, "prompt_cache_miss_tokens", 0) or 0)
        runtime.record_usage("sub_agent", p, c, t, cache_hit=ch, cache_miss=cm)
    except Exception:
        pass


SUB_SYSTEM = (
    "你是一个被主智能体派出的子智能体，负责独立完成一项具体任务。\n"
    "规则：\n"
    "1. 需要实时信息、查文件/网页、计算或执行操作时，调用可用工具完成，不要凭空编造；\n"
    # 2/3 原为两条（「互不依赖要并发」+「有依赖才串行」），是同一件事的正反面。
    # 合并后腾出的位置给了「先读齐再动手」——子智能体最常见的浪费是读一个改一个，
    # 每一步都等一次 LLM 往返。判据同样改成可操作的「写参数时要不要另一个的结果」。
    "2. 并行优先：写这个调用的参数时不需要另一个调用的结果，就把它们放在同一轮一起发"
    "（同时读多个文件、同时搜多个关键词、边跑测试边读下一个文件），系统会并行执行——"
    "一个一个发等于白等好几倍时间；只有参数依赖上一步结果时才必须分开；\n"
    "3. 先把要用的信息并发读齐再动手，不要读一个改一个；\n"
    "4. 读过的内容不要重复读（跨轮也一样）：结论留在上下文里直接引用，"
    "只有它刚被改过或要确认最新状态时才重读；\n"
    "5. 任务完成或无法继续时，停止调用工具，直接用简洁的中文输出最终结果/结论；\n"
    "6. 输出要可直接使用：结论、关键数字或文件路径，不要客套话；\n"
    "7. 你有与主智能体相同的全部工具能力（含技能说明书），长任务可连续多轮调用工具，"
    "没有轮数限制；遇到反爬/失败自动换思路重试，不要轻易放弃。"
)


class SubAgent:
    """一个通用子智能体（一次独立的「LLM+工具」自主执行委托）。"""

    __slots__ = (
        "id", "kind", "title", "task", "status", "logs", "result", "error",
        "ws", "owner", "extra", "created_at", "updated_at",
        "task_ref", "loop_task", "watchdog", "cancelled", "profile",
    )

    def __init__(self, task: str, title: str, ws, owner=None, extra: Optional[dict] = None,
                 profile: str = ""):
        self.id = "agent-" + uuid.uuid4().hex[:10]
        self.kind = "general"
        self.title = (title or task)[:120]
        self.task = task[:4000]
        self.status = ST_QUEUED
        self.logs: list[str] = []
        self.result = ""
        self.error = ""
        self.ws = ws
        self.owner = owner
        self.extra = extra or {}
        self.profile = str(profile or "").strip()   # 智能体档案 id（""=全能力通用执行者）
        self.created_at = time.time()
        self.updated_at = time.time()
        self.task_ref: Any = None        # 任务中心镜像 Task
        self.loop_task: Optional[asyncio.Task] = None
        self.watchdog: Optional[asyncio.Task] = None
        self.cancelled: bool = False

    @property
    def status_label(self) -> str:
        return _STATUS_LABEL.get(self.status, self.status)

    def snapshot(self, full: bool = False) -> dict:
        base = {
            "id": self.id,
            "kind": self.kind,
            "title": self.title,
            "task": self.task,
            "status": self.status,
            "status_label": self.status_label,
            "profile": self.profile,
            "created_at": int(self.created_at * 1000),
            "updated_at": int(self.updated_at * 1000),
        }
        if full:
            base.update({
                "logs": list(self.logs[-20:]),
                "result": self.result,
                "error": self.error,
                "extra": self.extra,
                "task_ref_id": self.task_ref.id if self.task_ref else "",
            })
        return base


class SubAgentManager:
    """通用子智能体注册表：spawn / 并发池 / 自主执行循环 / 汇报回调 + 任务中心镜像。"""

    def __init__(self) -> None:
        self._workers: dict[str, SubAgent] = {}
        self._lock = asyncio.Lock()
        self._sem = asyncio.Semaphore(_max_concurrent())
        self._report_handler: Optional[Callable[[SubAgent, str], Awaitable[Any]]] = None
        self._event_handler: Optional[Callable[[str, SubAgent], Awaitable[Any]]] = None
        self._client = None
        self._client_model = ""
        self._tools: Optional[list] = None
        self._tools_cache: dict[str, list] = {}   # 按档案 id 分组的过滤后工具集
        self._history: list[dict] = []            # 重启前跑过的 worker（每个 id 只留最后一次状态）
        self._load_history()

    def _load_history(self) -> None:
        """启动时回灌审计日志，让重启后仍能查到上一批 worker 干了什么。"""
        try:
            if not _HIST_FILE.exists():
                return
            latest: dict[str, dict] = {}
            lines = _HIST_FILE.read_text(encoding="utf-8").splitlines()[-_HIST_KEEP * 3:]
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                wid = rec.get("id")
                if wid:
                    latest[wid] = rec
            self._history = sorted(latest.values(), key=lambda r: r.get("ts", 0))
            logger.info("[SubAgent] 回灌审计历史 %d 条", len(self._history))
        except Exception as e:
            logger.warning("[SubAgent] 审计历史读取失败: %s", e)

    def set_report_handler(self, handler) -> None:
        """设置「worker 完成/出错 → 反馈主智能体」的汇报回调（server 注入）。"""
        self._report_handler = handler

    def set_event_handler(self, handler) -> None:
        """设置注册表事件回调（spawn/running/done/error/cancelled），供 UI 推送。"""
        self._event_handler = handler

    # ---------- 注册 / 查询 ----------

    async def spawn(self, ws, owner, task: str, title: str = "",
                    extra: Optional[dict] = None, profile: str = "") -> SubAgent:
        """派出一个通用子智能体（立即返回，后台自主执行，可大量并行分派）。

        profile：智能体档案 id（见 agent_profiles.py）——决定这个 worker 的系统提示词、
        可用技能/工具与可用模型。留空 = 全能力通用执行者（与旧行为完全一致）。
        """
        task = str(task or "").strip()
        if not task:
            raise ValueError("子智能体任务内容不能为空")
        worker = SubAgent(task, title, ws, owner, extra, profile)
        async with self._lock:
            self._workers[worker.id] = worker
            if len(self._workers) > _MAX_WORKERS:
                asyncio.ensure_future(self._sweep())
        await self._mirror_create(worker)
        # 立刻按真实状态同步任务中心：子智能体可能还在并发池排队（queued），
        # 不能因为镜像创建就显示成「执行中」；也让「任务中心能否反映后台真实进度」可验证。
        await self._mirror_set_status(worker, worker.status)
        await self._emit("spawn", worker)
        # 总体看护：超时自动取消，防悬挂
        worker.watchdog = asyncio.ensure_future(self._watchdog(worker.id))
        worker.loop_task = asyncio.ensure_future(self._run(worker))
        logger.info("[SubAgent] 派出《%s》→ [%s]", worker.title, worker.id)
        _hist_record(worker, "spawn")
        return worker

    def get(self, worker_id: str) -> Optional[SubAgent]:
        return self._workers.get(worker_id)

    def active(self) -> list[SubAgent]:
        return [w for w in self._workers.values()
                if w.status in (ST_QUEUED, ST_RUNNING)]

    def list(self, limit: int = 80) -> list[dict]:
        ordered = sorted(self._workers.values(),
                         key=lambda w: w.created_at, reverse=True)
        out = [w.snapshot(full=False) for w in ordered[:limit]]
        if len(out) < limit:
            seen = {w.id for w in self._workers.values()}
            for rec in reversed(self._history):      # 历史按时间正序存，取最近的排前面
                if len(out) >= limit:
                    break
                if rec.get("id") in seen:
                    continue
                out.append(dict(rec, restored=True))
        return out

    def active_text(self, limit: int = 8) -> str:
        """给主智能体看的「正在干活的子进程」摘要（注入每轮对话动态状态）。"""
        run = [w for w in self._workers.values() if w.status == ST_RUNNING]
        queued = [w for w in self._workers.values() if w.status == ST_QUEUED]
        if not run and not queued:
            return ""
        lines = [f"【子智能体运行中（{len(run)} 个执行，{len(queued)} 个排队）】"]
        for w in run[:limit]:
            lines.append(f"🧠 任务「{w.title}」[{w.id}] 执行中")
        for w in queued[:3]:
            lines.append(f"   ⏳（排队中）「{w.title}」[{w.id}]")
        return "\n".join(lines)

    # ---------- 异步并发池执行 ----------

    async def _run(self, worker: SubAgent) -> None:
        try:
            async with self._sem:            # 并发池：超出上限排队等待
                await self._set_status(worker, ST_RUNNING)
                result = await self._run_loop(worker)
                await self._finish(worker, ST_DONE, result=result)
        except asyncio.CancelledError:
            worker.cancelled = True
            await self._finish(worker, ST_CANCELLED, error="已被取消")
        except Exception as e:
            await self._finish(worker, ST_ERROR,
                               error=f"{e.__class__.__name__}: {e}")

    async def _run_loop(self, worker: SubAgent) -> str:
        prof = _load_profile(worker.profile)
        client, model = self._get_client(prof)
        tools = self._tool_defs(prof)
        # 与主智能体对齐：注入 harness 技能/插件说明书，让子智能体知道全部能力怎么用
        sys_content = SUB_SYSTEM
        active_skills: set = set()
        try:
            from agent_profiles import system_prompt_for, active_skills_for
            active_skills = set(active_skills_for(prof))
            role_prompt = system_prompt_for(prof)
            if role_prompt:
                sys_content += ("\n\n【你的角色与纪律（本次任务的专项要求，"
                                "与上面的通用规则冲突时以本节为准）】\n" + role_prompt)
        except Exception as e:
            logger.warning("[SubAgent] 档案提示词注入失败（忽略）: %s", e)
        try:
            from agent import get_harness_prompt_extras
            # 子智能体是新会话，无已激活技能 → 未激活的 on_demand 技能不逐条常驻，
            # 只汇总技能名清单，与主智能体「摘要与工具注册同步」保持一致。
            # 带档案时把该档案的技能视作「已激活」：说明书只注入相关技能，更聚焦也更省 token。
            extras = get_harness_prompt_extras(active_skills)
            if extras:
                sys_content += "\n\n【技能说明书（与主智能体相同）】\n" + extras
        except Exception:
            pass
        messages = [
            {"role": "system", "content": sys_content},
            {"role": "user", "content": worker.task},
        ]
        rounds = 0
        max_rounds = _max_rounds()          # 0 = 不限制（与主智能体一致）
        max_est_tokens = _max_est_tokens()
        est_tokens_used = 0
        # 死循环防护：记录每轮的「调用指纹 + 结果指纹」，两者都重复才算原地打转。
        # 只看调用会把合理轮询误杀（详见 LOOP_SAME_ROUNDS 处的说明）。
        recent_rounds: list = []
        loop_warned = False
        while max_rounds <= 0 or rounds < max_rounds:
            rounds += 1
            await self._mirror_log(worker, f"第 {rounds} 轮思考…")
            # 上下文视图：system + 最近 60 条完整 + 更早的压成片段（不再硬切丢弃）
            llm_messages = _fit_context(messages)
            # token 估算走 memory.estimate_tokens（与主智能体同一口径）。
            # 从前是每轮把整个消息列表 json.dumps 一遍再除以 3——120 条消息每轮全量
            # 序列化，纯浪费，而且对中文的估算偏差比正主还大。
            # 量的是「当前上下文」而不是累计消耗：累计会让长任务几轮就误判超预算提前停。
            try:
                from memory import estimate_tokens
                est_tokens_used = sum(
                    estimate_tokens(str(m.get("content") or "")) for m in llm_messages)
            except Exception:
                est_tokens_used = 0
            if est_tokens_used > max_est_tokens:
                await self._mirror_log(
                    worker, f"已超出预估 token 预算（约 {est_tokens_used}），提前停止")
                return (f"（子智能体已超出预估 token 预算（约 {est_tokens_used}），"
                        "为控制成本提前停止；请缩小任务范围或分批执行）")
            msg = await self._llm_call(client, model, llm_messages, tools)
            choice = msg.choices[0].message
            content = (choice.content or "").strip()
            tool_calls = choice.tool_calls or []

            if not tool_calls:
                return content or "（子智能体没有给出结论）"

            # 记录本轮工具调用（OpenAI 格式：assistant 消息必须带 tool_calls 原样回传）
            serialized = []
            fps = []
            for tc in tool_calls:
                serialized.append({
                    "id": tc.id or "",
                    "type": "function",
                    "function": {
                        "name": tc.function.name or "",
                        "arguments": tc.function.arguments or "{}",
                    },
                })
                fps.append((tc.function.name or "",
                            re.sub(r"\s+", "", tc.function.arguments or "{}")))
            messages.append({"role": "assistant", "content": content,
                             "tool_calls": serialized})

            # 同轮工具分批并行：只读一批并行跑，写按资源键串行（与主智能体同一调度器）。
            # 从前是一个一个 await——模型即使一轮要读 5 个文件，也要等 5 次串行往返；
            # 子智能体本来就是为了加速大型任务才存在，内部却比主智能体还慢。
            parsed: list = []
            for idx, tc in enumerate(tool_calls):
                raw = tc.function.arguments or "{}"
                try:
                    args = json.loads(raw) if raw.strip() else {}
                except Exception:
                    args = {}
                if not isinstance(args, dict):
                    args = {}
                parsed.append((idx, tc.function.name or "", args))

            results = await self._execute_calls(worker, parsed)

            # 结果必须按模型给出的原顺序回填，tool_call_id 一一对应
            for idx, tc in enumerate(tool_calls):
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id or "",
                    "content": results.get(idx, "（工具结果丢失）"),
                })

            # 原地打转检测：调用和结果都重复才算数。第一次只提醒（给模型自纠的
            # 机会），提醒后仍不收敛才停——子智能体是后台任务、用户不在场，
            # 停错了得重新派，所以宁可多给一次机会，也不轻易判死。
            if _note_round(recent_rounds, fps, results, range(len(tool_calls))):
                if not loop_warned:
                    loop_warned = True
                    await self._mirror_log(
                        worker, f"连续 {LOOP_SAME_ROUNDS} 轮调用相同且结果一致，"
                                "疑似原地打转，已提醒换思路")
                    messages.append({"role": "system", "content": LOOP_HINT})
                else:
                    await self._mirror_log(
                        worker, f"提醒后仍在重复（连续 {LOOP_SAME_ROUNDS} 轮无新信息），已停止")
                    return (f"（子智能体连续 {LOOP_SAME_ROUNDS} 轮调用相同的工具且返回"
                            "完全相同的结果，没有任何新信息，已自动停止；"
                            "请检查任务描述是否自相矛盾，或相关工具是否异常）")
        return f"（子智能体达到最大轮次 {max_rounds} 轮，未能完成全部意图）"

    async def _execute_calls(self, worker: SubAgent, parsed: list) -> dict:
        """分批执行同一轮的全部工具调用，返回 {下标: 结果文本}。

        只读一批并行、写工具按资源键串行（harness.tool_sched.plan），
        与主智能体同一套调度语义。

        单独成方法是为了可测：并行度这种东西必须能被测到——
        否则「以为并行了」和「真的并行了」没有任何区别。
        """
        batches = _plan_batches(parsed) or [[p] for p in parsed]
        results: dict = {}
        for batch in batches:
            if len(batch) == 1:
                i, n, a = batch[0]
                results[i] = await self._run_one_tool(worker, n, a)
                continue
            outs = await asyncio.gather(
                *(self._run_one_tool(worker, n, a) for _i, n, a in batch))
            for (i, _n, _a), out in zip(batch, outs):
                results[i] = out
        return results

    async def _run_one_tool(self, worker: SubAgent, name: str, args: dict) -> str:
        """执行单个工具：只读缓存 → 带超时执行 → 写操作推进缓存 epoch。

        与主智能体 _supervised_tool_stream 同一套语义（缓存 / 超时 / 失效），
        区别只在于子智能体没有流式通道，所以不产出心跳、只在任务中心记日志。

        超时是必须的：从前这里直接 await，一个卡住的工具能把整个子智能体拖到
        2 小时看门狗才被杀——期间这个 worker 一直占着并发槽位空转，
        而并发槽位是全局稀缺资源（默认只有 4 个）。
        """
        cache = _get_cache()
        if cache is not None:
            hit = cache.get(name, args)
            if hit is not None:
                await self._mirror_log(worker, f"命中缓存 {name} {str(args)[:50]}")
                return str(hit[0])[:8000]

        await self._mirror_log(worker, f"调用工具 {name} {str(args)[:60]}")
        timeout = _tool_timeout(name)
        ok = True
        try:
            result = await asyncio.wait_for(self._execute_tool(name, args), timeout=timeout)
        except (asyncio.TimeoutError, TimeoutError):
            result, ok = f"工具 {name} 执行超时（{timeout:.0f}s），已中止本次调用", False
        except asyncio.CancelledError:
            raise
        except Exception as e:
            result, ok = f"工具 {name} 执行失败：{e.__class__.__name__}: {e}", False

        if cache is not None:
            try:
                from harness.tool_sched import is_readonly
                if is_readonly(name):
                    cache.put(name, args, result, ok)
                else:
                    # 写操作：搜索结果类（无 path）的缓存立即失效
                    cache.invalidate()
            except Exception:
                pass

        text = str(result)[:8000]
        await self._mirror_log(worker, f"  {name} → {text[:120]}")
        return text

    async def _execute_tool(self, name: str, arguments: dict) -> str:
        # harness 技能/插件路由（与主智能体一致）
        try:
            from harness import get_harness
            result, source = await get_harness().execute_tool(name, arguments)
            if result is not None:
                return result
        except Exception as e:
            return f"工具执行失败：{e.__class__.__name__}: {e}"
        # 本地工具兜底
        try:
            from agent import execute_local_tool
            return await execute_local_tool(name, arguments)
        except Exception as e:
            return f"工具执行失败：{e.__class__.__name__}: {e}"

    async def _llm_call(self, client, model, messages, tools):
        """带瞬态重试的 LLM 调用（429/5xx/网络抖动退避重试，最多 3 次）。

        单次调用有硬超时：客户端默认 600s，加 3 次重试最坏能挂半小时，
        而子智能体全程占着全局并发槽位（默认 4 个）——卡住一个等于产能少 1/4。
        超时属于瞬时错误，retry_async 会正常退避重试。
        """
        # 兜底：保证 role=tool 消息带 tool_call_id（与主智能体同一规范函数），
        # 否则发给 OpenAI 兼容提供方会被 400（missing field tool_call_id）
        try:
            from agent import _normalize_tool_rounds
            messages = _normalize_tool_rounds(messages)
        except Exception:
            pass

        # 输出预算与主智能体同口径：子智能体的典型任务就是写文件/生成代码，
        # 硬编码 4096 很容易把长参数截断 → JSON 不完整 → 解析失败 → 白烧一整轮。
        try:
            from agent import _tool_max_tokens
            budget = int(_tool_max_tokens())
        except Exception:
            budget = 4096

        async def _one(max_tokens: int):
            return await asyncio.wait_for(
                client.chat.completions.create(
                    model=model, messages=messages, tools=tools, tool_choice="auto",
                    max_tokens=max_tokens, temperature=0.3, stream=False),
                timeout=_LLM_TIMEOUT)

        try:
            from harness.core import retry_async
            resp = await retry_async(lambda: _one(budget), attempts=3, backoff=2.0)
        except Exception as e:
            # 重试耗尽后的最后一搏：降 max_tokens 再试一次。
            # 必须留日志——否则「这次结论为什么只有半截」永远查不出来
            # （1024 tokens 很容易把结论截断，而失败原因被完全吞掉）。
            logger.warning("[SubAgent] LLM 调用重试耗尽（%s: %s），降级为单次小预算调用",
                           e.__class__.__name__, e)
            resp = await _one(1024)
        _record_usage(resp)
        return resp

    def _get_client(self, profile: Optional[dict] = None):
        from agent import _build_llm_client, load_config
        cfg = load_config()
        model = str(cfg.get("model") or "").strip()
        if self._client is None or self._client_model != model:
            self._client = _build_llm_client(
                str(cfg.get("base_url") or ""), str(cfg.get("api_key") or ""))
            self._client_model = model or "x-preview-f-free"
        # 档案可覆盖模型名（同一 base_url/api_key，只换模型）——
        # 比如给「调查员」配便宜快的模型，给「核心开发」配最强的模型
        override = str((profile or {}).get("model") or "").strip()
        return self._client, (override or self._client_model)

    def _tools_base(self) -> list:
        """全量工具（排除 sub_agent_* 自身，防无限递归下发）。"""
        if self._tools is None:
            try:
                from agent import load_local_tools
                self._tools = [
                    t for t in load_local_tools()
                    if not (t.get("function") or {}).get("name", "").startswith("sub_agent_")
                ]
            except Exception as e:
                logger.warning("[SubAgent] 工具列表加载失败: %s", e)
                self._tools = []
        return self._tools

    def _tool_defs(self, profile: Optional[dict] = None) -> list:
        """按智能体档案过滤的工具列表。

        缓存按档案 id 分组：不同档案的工具集不同，共用一份缓存会串味
        （「测试员」拿到「核心开发」的工具 = 档案形同虚设）。

        额外补一步：把白名单技能的完整 schema 直接读出来并进去——
        渐进披露下未激活技能的工具不在 collect_tool_specs() 里，不补就会把
        「技能白名单」变成「把技能禁用」，与档案本意完全相反。
        """
        key = str((profile or {}).get("id") or "")
        if key in self._tools_cache:
            return self._tools_cache[key]
        base = self._tools_base()
        if not profile:
            self._tools_cache[key] = base
            return base
        try:
            from agent_profiles import (resolve_tools, extra_skill_tool_specs,
                                        active_skills_for)
            merged = list(base)
            seen = {str(((t or {}).get("function") or {}).get("name") or "") for t in merged}
            for t in extra_skill_tool_specs(active_skills_for(profile), all_when_empty=True):
                n = str((t.get("function") or {}).get("name") or "")
                if n and n not in seen:
                    seen.add(n)
                    merged.append(t)
            self._tools_cache[key] = resolve_tools(profile, merged)
        except Exception as e:
            logger.warning("[SubAgent] 档案工具过滤失败，退回全量: %s", e)
            self._tools_cache[key] = base
        return self._tools_cache[key]

    # ---------- 取消 / 终态 ----------

    async def cancel(self, worker_id: str, reason: str = "") -> Optional[SubAgent]:
        """取消一个子智能体（排队中/执行中均可；静默，不汇报）。"""
        async with self._lock:
            worker = self.get(worker_id)
            if worker is None or worker.status in (ST_DONE, ST_ERROR, ST_CANCELLED):
                return worker
            if worker.loop_task is not None and not worker.loop_task.done():
                worker.loop_task.cancel()
            if worker.watchdog is not None:
                worker.watchdog.cancel()
            worker.cancelled = True
            _hist_record(worker, "cancel", reason)
            return worker

    async def _finish(self, worker: SubAgent, status: str, *,
                      result: str = "", error: str = "") -> None:
        worker.status = status
        worker.updated_at = time.time()
        if result:
            worker.result = result[:4000]
        if error:
            worker.error = error[:2000]
            worker.logs.append(f"✗ {error[:200]}")
        if worker.watchdog is not None:
            worker.watchdog.cancel()
            worker.watchdog = None
        _hist_record(worker, status)
        await self._mirror_finish(worker, status, result or error)
        await self._emit(status, worker)
        logger.info("[SubAgent] 「%s」→ %s", worker.title, worker.status_label)
        if status == ST_DONE:
            body = (result or "").strip()
            summary = body.split("\n")[0][:160] if body else "（无输出）"
            report = f"任务《{worker.title}》已完成：{summary}"
            # 只给首行摘要会导致主智能体「知道完成了，却不知道结论是什么」，
            # 还得回头查一次——把正文一并带上，一次汇报即可转述。
            if len(body) > len(summary):
                report += (f"\n—— 子智能体结论全文（截断至 1500 字；"
                           f"更长内容用 sub_agent_status(worker_id=\"{worker.id}\") 取）——\n"
                           + body[:1500])
        elif status == ST_ERROR:
            report = f"任务《{worker.title}》出错了：{error[:160]}"
        else:
            report = ""
        if report and self._report_handler is not None and worker.ws is not None:
            try:
                await self._report_handler(worker, report)
            except Exception as e:
                logger.warning("[SubAgent] 汇报回调出错: %s", e)

    async def _set_status(self, worker: SubAgent, status: str) -> None:
        worker.status = status
        worker.updated_at = time.time()
        _hist_record(worker, "status")
        await self._mirror_set_status(worker, status)
        await self._emit(status, worker)

    async def _emit(self, event: str, worker: SubAgent) -> None:
        if self._event_handler is None or worker.ws is None:
            return
        try:
            await self._event_handler(event, worker)
        except Exception as e:
            logger.warning("[SubAgent] 事件推送失败: %s", e)

    # ---------- 超时看护 ----------

    async def _watchdog(self, worker_id: str) -> None:
        try:
            await asyncio.sleep(MAX_RUNTIME)
            worker = self.get(worker_id)
            if worker is not None and worker.status in (ST_QUEUED, ST_RUNNING):
                if worker.loop_task is not None:
                    worker.loop_task.cancel()
                logger.warning("[SubAgent] 「%s」运行超时，自动取消", worker.title)
        except asyncio.CancelledError:
            pass

    # ---------- 任务中心镜像 ----------

    async def _mirror_create(self, worker: SubAgent) -> None:
        try:
            from task_orchestrator import get_orchestrator
            title = f"子智能体：「{worker.title}」"
            worker.task_ref = await get_orchestrator().create(
                kind="sub-agent",
                title=title,
                ws=worker.ws,
                brief=worker.task,
                confirm=False,
                channel="sub",
                extra={"worker_id": worker.id},
                auto_run=False,   # 状态由子智能体自己镜像，排队≠执行中
            )
        except Exception as e:
            worker.task_ref = None
            logger.warning("[SubAgent] 任务中心镜像创建失败: %s", e)

    async def _mirror_log(self, worker: SubAgent, text: str) -> None:
        if worker.task_ref is None:
            return
        try:
            from task_orchestrator import get_orchestrator
            worker.logs.append(text[:500])
            await get_orchestrator().add_log(worker.task_ref, text[:500])
        except Exception:
            pass

    async def _mirror_set_status(self, worker: SubAgent, status: str) -> None:
        if worker.task_ref is None:
            return
        try:
            from task_orchestrator import (
                get_orchestrator, STATUS_QUEUED, STATUS_RUNNING,
            )
            await get_orchestrator().set_status(
                worker.task_ref,
                STATUS_RUNNING if status == ST_RUNNING else STATUS_QUEUED,
            )
        except Exception:
            pass

    async def _mirror_finish(self, worker: SubAgent, status: str, tail: str) -> None:
        if worker.task_ref is None:
            return
        try:
            from task_orchestrator import (
                get_orchestrator, STATUS_DONE, STATUS_ERROR, STATUS_CANCELLED,
            )
            orch = get_orchestrator()
            if status == ST_DONE:
                await orch.set_result(worker.task_ref, tail or "子智能体已完成", STATUS_DONE)
            elif status == ST_ERROR:
                await orch.set_error(worker.task_ref, tail or "子智能体执行出错")
            else:
                await orch.set_status(worker.task_ref, STATUS_CANCELLED)
                if tail:
                    await orch.add_step(worker.task_ref, f"（{tail[:200]}）")
        except Exception as e:
            logger.warning("[SubAgent] 任务中心镜像更新失败: %s", e)

    async def _sweep(self) -> None:
        stale = [w for w in self._workers.values()
                 if w.status not in (ST_QUEUED, ST_RUNNING)]
        stale.sort(key=lambda w: w.updated_at, reverse=True)
        for w in stale[_MAX_WORKERS // 2:]:
            self._workers.pop(w.id, None)


_sub_agents: Optional[SubAgentManager] = None


def get_sub_agents() -> SubAgentManager:
    global _sub_agents
    if _sub_agents is None:
        _sub_agents = SubAgentManager()
    return _sub_agents
