"""Harness 任务系统（TaskSystem）—— 长任务 / 批量任务 / DAG 流程的统一执行与统摄。

对标的业界实践（Celery / Temporal / LangGraph 的核心子集），按大白的单体形态裁剪：

- **队列调度**：每队列独立 worker 池（并发上限）、任务优先级、启动限速（min_interval）、
  队列暂停/恢复，全部运行时可观测可控；
- **长任务（Flow）**：多步骤 DAG 流程。步骤支持 tool / llm / batch / handler 四类动作，
  步骤间结果可用 {{步骤id.result}} 模板引用；步骤级超时+重试+退避，流程级整体看门狗，
  失败策略 abort（中止后续）/ continue（独立分支继续跑）；
- **批量任务（Batch）**：一个工具并行 map 到 N 个条目，批内并发上限（信号量），
  单条失败不影响其它条目（部分失败容忍），结果按序聚合；
- **持久化与断点续跑**：流程/批量定义与各步骤完成状态实时落盘
  （harness_tasks.json，原子写）；服务重启 / 热重载自动重启后自动恢复未完成部分
  （已成功步骤的结果不重算）；
- **统摄**：全部任务/队列挂在 Harness 上，经 /api/harness/tasks* 与管理页统一查看、
  取消、重试、暂停；步骤执行复用 runtime 监督（熔断/超时/计量），LLM 步骤走
  runtime 的 plan 渠道 —— 任务系统自身也被上一层的监督运行时罩住。

调度模型（单事件循环，无跨线程共享）：
- 待执行任务存 per-queue 小顶堆（优先级, 序号, 就绪时间）；
- worker 协程循环取就绪任务执行（延迟重试的任务 ready_at 在未来，到点自动可取）；
- 流程/批量的协调器不占 worker 槽位（只是廉价协程），叶子动作才占；
- 子任务终态通过 parent 的 wake Event 唤醒协调器推进 DAG。
"""
from __future__ import annotations

import asyncio
import heapq
import itertools
import json
import logging
import os
import time
import traceback
import uuid
from collections import deque
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger("harness.tasks")

# 任务状态机：pending → running → succeeded / failed / cancelled
PENDING = "pending"
RUNNING = "running"
SUCCEEDED = "succeeded"
FAILED = "failed"
CANCELLED = "cancelled"
TERMINAL = (SUCCEEDED, FAILED, CANCELLED)

# 失败策略
POLICY_ABORT = "abort"          # 任一步骤失败 → 取消其余步骤，流程失败（默认）
POLICY_CONTINUE = "continue"    # 失败步骤的后续分支放弃，独立分支继续执行

DEFAULT_CFG = {
    "queues": {                       # 队列名 → {workers, min_interval}
        "default": {"workers": 4, "min_interval": 0.0},
        "flows": {"workers": 4, "min_interval": 0.0},
        "batch": {"workers": 6, "min_interval": 0.0},
    },
    "default_timeout": 600.0,         # 单个叶子动作默认超时（秒）
    "flow_timeout": 3600.0,           # 流程整体看门狗默认（秒）
    "max_attempts": 2,                # 叶子动作默认重试次数（含首次）
    "backoff": 2.0,                   # 重试退避基数（秒）：backoff * 2^(已试次数-1)
    "item_timeout": 120.0,            # 批量条目默认超时
    "batch_concurrency": 3,           # 批量默认批内并发
    "llm_wait_executor": 60.0,        # LLM 步骤等待执行器注册的最长秒数（重启窗口）
    "llm_budget": 40,                 # 单个流程 llm 步骤调用总量上限（防反思/循环失控）
    "reflection_cap": 3,              # 单个流程失败反思(重规划)次数上限
    "result_guard": True,             # 结果反查：工具正常返回错误文案 → 按失败处理
    "expect_verify": True,            # expect 语义校验：关键步骤结果由 LLM 复核是否答到点上
    "planner_critique": True,         # 规划器自评：规划后再过一遍批评/修订
    "history_cap": 300,               # 内存中保留的终态任务条数
    "journal_cap": 150,               # 落盘保留的 durable 任务条数
    "template_result_max": 4000,      # {{id.result}} 注入下游步骤时的截断长度
}

TEMPLATE_RE = None  # 懒加载 compiled 正则（{{stepid.result}}）


# 结果反查守卫：harness 系统自身生成的失败文案（工具返回"成功"但内容是错误信息）。
# 命中即按步骤失败处理（可重试/触发反思），不让垃圾结果流入下游步骤。
_RESULT_GUARD_PATTERNS = (
    "不存在或未加载", "不存在或未启用", "已禁用（可在", "工具不存在",
    "执行超时（>", "执行失败:", "处理器未注册",
)


class TaskSystemError(Exception):
    """任务系统参数/状态错误（提交时校验失败等）。"""


def _type_ok(v, t: str) -> bool:
    """JSON Schema 基本类型检查（宽松：未知类型放行）。"""
    if t == "string":
        return isinstance(v, str)
    if t == "integer":
        return isinstance(v, int) and not isinstance(v, bool)
    if t == "number":
        return isinstance(v, (int, float)) and not isinstance(v, bool)
    if t == "boolean":
        return isinstance(v, bool)
    if t == "array":
        return isinstance(v, list)
    if t == "object":
        return isinstance(v, dict)
    return True


def _extract_json(text: str):
    """提取文本中第一个配平的 JSON 对象（容忍 markdown 围栏与前后闲话）。"""
    if not isinstance(text, str):
        return None
    text = text.replace("```json", " ").replace("```", " ")
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start:i + 1])
                except Exception:
                    return None
    return None


def _short(v: Any, cap: int = 300) -> str:
    s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, default=str)
    return s if len(s) <= cap else s[:cap - 1] + "…"


def _levenshtein(a: str, b: str, cap: int = 3) -> int:
    """带早停的编辑距离（超过 cap 直接返回 cap+1，防长名 O(n·m) 全算）。"""
    a, b = str(a), str(b)
    if abs(len(a) - len(b)) > cap:
        return cap + 1
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        if min(cur) > cap:
            return cap + 1
        prev = cur
    return prev[-1]


class Task:
    """一个可调度单元：叶子动作 / 流程 / 批量 / 流程步骤 / 批量条目。"""

    __slots__ = ("id", "name", "kind", "queue", "priority", "state", "progress",
                 "status_text", "attempts", "max_attempts", "timeout",
                 "created_at", "started_at", "finished_at",
                 "result", "error", "durable", "action", "parent", "step_key",
                 "wake", "seq", "_exec_task")

    def __init__(self, id: str, name: str, kind: str, action: dict,
                 queue: str = "default", priority: int = 5,
                 timeout: Optional[float] = None, max_attempts: int = 1,
                 durable: bool = False, parent: Optional[str] = None,
                 step_key: str = ""):
        self.id = id
        self.name = name
        self.kind = kind          # task / flow / batch / step / item / handler-task
        self.queue = queue
        self.priority = priority  # 数值越小越先执行（1-9）
        self.state = PENDING
        self.progress = 0
        self.status_text = "排队中"
        self.attempts = 0
        self.max_attempts = max(1, int(max_attempts))
        self.timeout = timeout
        self.created_at = time.time()
        self.started_at: Optional[float] = None
        self.finished_at: Optional[float] = None
        self.result: Any = None
        self.error: str = ""
        self.durable = durable
        self.action = action      # 序列化友好的动作描述（不含协程）
        self.parent = parent      # 父任务 id（流程/批量）
        self.step_key = step_key  # 在父任务内的键（步骤 id / 条目下标）
        self.wake = asyncio.Event()
        self.seq = 0
        self._exec_task: Optional[asyncio.Task] = None

    # ---- 状态流转（集中在一处，便于埋点） ----

    def _set_state(self, state: str, error: str = "") -> None:
        self.state = state
        self.error = error or self.error
        if state in TERMINAL:
            self.finished_at = time.time()
            self.progress = 100 if state == SUCCEEDED else self.progress
        self.wake.set()

    def to_dict(self, brief: bool = False) -> dict:
        d = {
            "id": self.id, "name": self.name, "kind": self.kind,
            "queue": self.queue, "priority": self.priority, "state": self.state,
            "progress": self.progress, "status": self.status_text,
            "attempts": self.attempts, "max_attempts": self.max_attempts,
            "timeout": self.timeout,
            "created_at": self.created_at, "started_at": self.started_at,
            "finished_at": self.finished_at, "parent": self.parent,
            "step_key": self.step_key,
            "goal": str((self.action or {}).get("goal") or ""),
            "waiting_confirm": bool((self.action or {}).get("confirming")),
        }
        if not brief:
            d["error"] = self.error
            d["result"] = _shrink(self.result, deep=4)
            d["action"] = _shrink(self.action, deep=3)
        return d


def _shrink(v: Any, deep: int = 1) -> Any:
    """把结果裁剪成适合 API 返回的体积（长文本截断，深层结构折叠）。"""
    if isinstance(v, str):
        return v if len(v) <= 800 else v[:797] + "..."
    if isinstance(v, (int, float, bool)) or v is None:
        return v
    if isinstance(v, dict) and deep > 0:
        return {str(k): _shrink(x, deep - 1) for k, x in list(v.items())[:30]}
    if isinstance(v, (list, tuple)) and deep > 0:
        return [_shrink(x, deep - 1) for x in list(v)[:30]]
    return str(v)[:200]


class _QueueState:
    """队列运行态：worker 池 + 优先级堆 + 暂停/限速。"""

    def __init__(self, name: str, workers: int, min_interval: float):
        self.name = name
        self.workers = max(1, int(workers))
        self.min_interval = max(0.0, float(min_interval))
        self.paused = False
        self.heap: list = []            # (priority, seq, ready_at, task_id)
        self.running: dict[str, Task] = {}
        self.last_start: float = 0.0
        self.wakeup = asyncio.Event()
        self.worker_tasks: list[asyncio.Task] = []


class TaskSystem:
    """任务系统门面：调度、流程、批量、持久化、恢复、控制。"""

    def __init__(self, harness):
        self.harness = harness
        self.base_dir = Path(getattr(harness, "base_dir", Path(".")))
        self.journal = self.base_dir / "harness_tasks.json"
        self._cfg = self._load_cfg()
        self._queues: dict[str, _QueueState] = {}
        self._tasks: dict[str, Task] = {}
        self._seq = itertools.count(1)
        self._history: deque = deque(maxlen=DEFAULT_CFG["history_cap"])
        self._coordinators: dict[str, asyncio.Task] = {}
        self._handlers: dict[str, Callable] = {}
        self._llm_executor: Optional[Callable] = None
        self._started = False
        self._saving = False
        self._stopping = False
        # 完成汇报队列：流程/批量到终态即入队，下一次 harness_task_list /
        # /api/harness/tasks 带回并清空 —— 模型无需反复轮询 status
        self._pending_reports: deque = deque(maxlen=20)

    # ==================== 配置 ====================

    def _load_cfg(self) -> dict:
        cfg = json.loads(json.dumps(DEFAULT_CFG))  # 深拷贝默认
        try:
            p = self.base_dir / "settings.json"
            if p.exists():
                with open(p, "r", encoding="utf-8") as f:
                    sc = json.load(f)
                tc = ((sc or {}).get("harness", {}) or {}).get("tasks", {}) or {}
                for k, v in tc.items():
                    if k == "queues" and isinstance(v, dict):
                        merged = dict(cfg["queues"])
                        for qn, qc in v.items():
                            merged[qn] = {**merged.get(qn, {"workers": 4, "min_interval": 0.0}),
                                         **(qc if isinstance(qc, dict) else {})}
                        cfg["queues"] = merged
                    elif k in cfg and not isinstance(cfg[k], dict):
                        try:
                            cfg[k] = type(cfg[k])(v)
                        except Exception:
                            pass
                # 队列字典内的类型规整
                for qn, qc in cfg["queues"].items():
                    qc["workers"] = max(1, int(qc.get("workers", 4)))
                    qc["min_interval"] = max(0.0, float(qc.get("min_interval", 0.0)))
        except Exception as e:
            logger.warning("加载 harness.tasks 配置失败，使用默认: %s", e)
        return cfg

    def reload_cfg(self) -> None:
        """热重载后刷新配置（worker 数变化对已起队列不回溯，新队列生效）。"""
        self._cfg = self._load_cfg()

    # ==================== 生命周期 ====================

    def ensure_started(self) -> None:
        """启动 worker 池并恢复持久化任务（幂等；需在事件循环内调用）。"""
        if self._started:
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return  # 无事件循环（导入期/CLI）：等首次异步调用再起
        self._started = True
        self._stopping = False
        for qn in self._cfg["queues"]:
            self._ensure_queue(qn)
        try:
            self._resume_persisted()
        except Exception as e:
            logger.warning("恢复持久化任务失败: %s", e)
        self._emit("task", f"任务系统就绪：队列 {list(self._cfg['queues'])}")

    def stop(self) -> None:
        """服务停止：取消 worker 与协调器，落盘 durable 状态（尽力而为，同步）。"""
        if not self._started:
            return
        self._stopping = True  # 抑制取消过程中协调器的过期落盘
        for q in self._queues.values():
            for w in q.worker_tasks:
                w.cancel()
            q.worker_tasks = []
        for c in self._coordinators.values():
            c.cancel()
        self._coordinators = {}
        # 运行中的 durable 任务回到 pending，等下次启动恢复
        for t in list(self._tasks.values()):
            if t.state == RUNNING and t.durable:
                t._set_state(PENDING)
                t.started_at = None
        self._started = False
        self._stopping = False
        try:
            self._save_journal()
        except Exception:
            pass

    def _ensure_queue(self, name: str) -> _QueueState:
        q = self._queues.get(name)
        if q is None:
            c = self._cfg["queues"].get(name, {"workers": 4, "min_interval": 0.0})
            q = _QueueState(name, c.get("workers", 4), c.get("min_interval", 0.0))
            self._queues[name] = q
        while len(q.worker_tasks) < q.workers:
            q.worker_tasks.append(
                asyncio.get_running_loop().create_task(self._worker_loop(q)))
        return q

    # ==================== 扩展点：处理器与 LLM 执行器 ====================

    def register_handler(self, name: str, fn: Callable) -> None:
        """注册具名处理器（可持久化任务的动作；重启后按名恢复）。"""
        self._handlers[str(name)] = fn

    def handler(self, name: str):
        """装饰器形式注册具名处理器。"""
        def deco(fn):
            self.register_handler(name, fn)
            return fn
        return deco

    def set_llm_executor(self, fn: Callable) -> None:
        """注册 LLM 步骤执行器（agent 初始化时注入；签名 async(system, prompt, **kw) -> str）。"""
        self._llm_executor = fn
        self._emit("task", "LLM 步骤执行器已注册（plan 渠道）")

    # ==================== 规划器（三言两语 → 结构化流程） ====================

    _PLANNER_SYSTEM = (
        "你是 AI 伙伴「大白」的任务规划器。把用户的简短请求规划成可执行的多步骤流程。"
        "只输出严格 JSON（不要 markdown 代码块、不要多余解释）。"
    )

    async def plan_flow(self, goal: str, hints: str = "", max_tokens: int = 1400,
                        critique: Optional[bool] = None) -> dict:
        """把用户的简短请求（往往三言两语）规划成结构化流程（只规划，不执行）。

        规划器经 plan 渠道 LLM 生成：先复述对任务的理解（目标/范围/交付物），
        再拆解为最小步骤；随后走与 submit_flow 相同的校验
        （工具名自动纠正 + 必填参数检查），返回给模型复核后提交。
        critique=None 时自动：≤2 步的简单计划跳过自评（省一次调用），更复杂的才自评。
        """
        goal = str(goal or "").strip()
        if not goal:
            raise TaskSystemError("goal 不能为空（请先复述用户的需求）")
        catalog = self._tool_catalog()
        skip = {"harness_flow_submit", "harness_flow_plan", "harness_batch_submit",
                "harness_task_cancel", "harness_task_retry"}
        lines = []
        for nm, fn in sorted(catalog.items()):
            if nm in skip:
                continue  # 规划业务动作，不规划任务系统自身
            desc = (fn.get("description") or "").strip().replace("\n", " ")
            lines.append(f"- {nm}: {desc[:90]}")
        tool_doc = "\n".join(lines) or "（无工具）"
        prompt = (
            f"用户的简短请求：{goal}\n"
            + (f"补充信息：{str(hints).strip()}\n" if str(hints or "").strip() else "")
            + "\n可用工具目录：\n" + tool_doc + "\n\n"
            + self._lessons_prompt()
            + "请规划一个多步骤流程：\n"
            "1. understanding：先用1-2句话复述你对任务的理解（目标、范围、最终交付什么给用户）；\n"
            "2. steps：最小必要步骤。步骤动作三选一：\n"
            "   - tool：直接调用目录中的工具，参数按工具描述如实填写，禁止编造工具名和参数；"
            "后续步骤依赖其产出的关键步骤建议加 \"expect\":\"该结果应满足什么\"（执行后自动校验）；\n"
            "   - llm：需要理解/加工/比较/决策时用（prompt 写清这一步要产出什么）；\n"
            "   - batch：同一工具作用于多条数据；\n"
            "3. 步骤有依赖用 deps 声明（无依赖的会自动并行）；后续步骤的 prompt/args "
            "里用 {{步骤id.result}} 引用前步结果；\n"
            "4. 一步能完成的不拆两步；工具能直接查到的信息不要让 llm 编造；最后一步应产出面向用户的结论；\n"
            "5. 危险动作（删除/覆盖/发送/花钱/对外发布）必须在该步骤上加 \"confirm\":\"危险性与影响说明\"，"
            "执行前会请用户批准；\n"
            "6. 【重要】关键信息缺失（对象不明/范围不清/用户偏好未知）时不要猜——"
            '输出 {"understanding":"当前理解","questions":["1-2个具体问题"],"steps":[]}，'
            "拿到答案后才能规划。\n"
            '信息充分时输出：{"understanding":"...","steps":[{"id":"s1","action":"tool",'
            '"tool":"...","args":{...}},{"id":"s2","action":"llm","prompt":"...","deps":["s1"]}]}'
        )
        raw = await self._exec_llm(self._PLANNER_SYSTEM, prompt,
                                   max_tokens=max_tokens, timeout=180)
        data = _extract_json(raw)
        if not isinstance(data, dict):
            raise TaskSystemError(f"规划器输出无法解析：{str(raw)[:200]}")
        understanding = str(data.get("understanding") or "").strip()
        questions = [str(q).strip() for q in (data.get("questions") or []) if str(q).strip()]
        if questions:
            # 结构化澄清：信息不足时不猜，把问题带回给模型去问用户
            return {"ok": True, "needs_clarification": True, "goal": goal,
                    "understanding": understanding, "questions": questions[:3],
                    "steps": [], "autofixed": [], "issues": [], "critique": "",
                    "hint": "关键信息缺失——先把 questions 问用户，拿到答案后带 hints 重新规划"}
        if not isinstance(data.get("steps"), list) or not data["steps"]:
            raise TaskSystemError(f"规划器输出无法解析为流程：{str(raw)[:200]}")
        try:
            norm = self._norm_steps(data["steps"])
        except TaskSystemError as e:
            return {"ok": False, "goal": goal, "understanding": understanding,
                    "steps": [], "autofixed": [],
                    "issues": [{"level": "error", "msg": str(e)}]}
        norm, autofixed, issues = self._validate_steps(norm)
        hard = [i["msg"] for i in issues if i.get("level") == "error"]

        # 审慎规划：自评一遍（风险/遗漏/更优工具），必要时修订。
        # 简单计划（≤2 步）默认跳过自评省一次调用；critique 显式指定时以指定为准。
        run_critique = critique if critique is not None else len(norm) > 2
        critique_note = ""
        if not hard and run_critique and self._cfg.get("planner_critique", True) and self._llm_executor is not None:
            crit = await self._critique_plan(goal, understanding, norm)
            if crit:
                critique_note = str(crit.get("notes") or "")[:300]
                if str(crit.get("verdict") or "") == "revise" and isinstance(crit.get("steps"), list):
                    try:
                        cnorm = self._norm_steps(crit["steps"])
                        cnorm, afx, ciss = self._validate_steps(cnorm)
                        if not any(i.get("level") == "error" for i in ciss):
                            norm = cnorm
                            autofixed = autofixed + \
                                [f"[自评修订] {a}" for a in afx]
                        # 修订版仍有硬错误则保留原方案（宁可稳）
                    except TaskSystemError:
                        pass
        return {
            "ok": not hard,
            "goal": goal,
            "understanding": understanding,
            "steps": norm,
            "autofixed": autofixed,
            "issues": issues,
            "critique": critique_note,
            "hint": ("规划已通过校验与自评，可直接 harness_flow_submit（把 understanding 放进 name，"
                     "goal 传用户原话）" if not hard else
                     "存在校验错误，请修正 steps 后再提交"),
        }

    async def _critique_plan(self, goal: str, understanding: str, steps: list) -> Optional[dict]:
        """规划自评：找风险/遗漏/更优解；verdict=revise 时给修订版步骤。失败返回 None。"""
        brief = "\n".join(
            f"- {s['id']}（{s['action']}" + (f" {s.get('tool', '')}" if s.get("tool") else "") + "）"
            + (f" deps={s['deps']}" if s.get("deps") else "")
            for s in steps)
        catalog = self._tool_catalog()
        names = "、".join(sorted(catalog.keys())[:60]) or "（无）"
        prompt = (
            f"【任务目标】{goal}\n【当前理解】{understanding}\n【当前计划】\n{brief}\n\n"
            f"【可用工具名】{names}\n\n"
            "请审慎自评这个计划：有没有更直接的路径？工具选得对吗？有没有遗漏必要步骤、"
            "多余的步骤、或应当先向用户确认的危险动作（删除/覆盖/发送/花钱类）？\n"
            '输出严格 JSON：{"verdict":"ok|revise","notes":"主要风险与改进点(1-3条)",'
            '"steps":[...](仅 revise 时给完整修订版)}\n'
            "规则：修订版必须能独立执行（含全部步骤）；拿不准就 verdict=ok 并在 notes 说明。"
        )
        try:
            raw = await self._exec_llm(self._PLANNER_SYSTEM, prompt,
                                       max_tokens=1200, timeout=180)
            d = _extract_json(raw)
            return d if isinstance(d, dict) else None
        except Exception:
            return None

    # ==================== 提交 ====================

    def _new_task(self, kind: str, name: str, action: dict, **kw) -> Task:
        tid = f"t{next(self._seq):06d}-{uuid.uuid4().hex[:6]}"
        t = Task(tid, name, kind, action, **kw)
        self._tasks[tid] = t
        return t

    def submit(self, name: str = "", factory: Optional[Callable[[], Awaitable]] = None,
               handler: Optional[str] = None, handler_args: Optional[dict] = None,
               queue: str = "default", priority: int = 5,
               timeout: Optional[float] = None, max_attempts: Optional[int] = None,
               durable: bool = False) -> str:
        """提交一个单动作任务（协程 factory 为进程内；handler 为可持久化动作）。"""
        if factory is None and handler is None:
            raise TaskSystemError("factory 与 handler 至少提供一个")
        if factory is not None:
            action = {"kind": "py"}
            t = self._new_task("task", name or "匿名任务", action, queue=queue,
                               priority=priority, timeout=timeout or self._cfg["default_timeout"],
                               max_attempts=max_attempts or 1, durable=False)
            t.action["coro"] = factory  # 进程内闭包，不可序列化
        else:
            action = {"kind": "handler", "name": str(handler),
                      "args": dict(handler_args or {})}
            t = self._new_task("task", name or f"handler:{handler}", action, queue=queue,
                               priority=priority, timeout=timeout or self._cfg["default_timeout"],
                               max_attempts=max_attempts or self._cfg["max_attempts"],
                               durable=durable)
        self._enqueue(t)
        return t.id

    def submit_flow(self, name: str, steps: list, queue: str = "flows",
                    priority: int = 5, timeout: Optional[float] = None,
                    max_attempts: Optional[int] = None, policy: str = POLICY_ABORT,
                    durable: bool = True, goal: str = "") -> str:
        """提交多步骤 DAG 流程（长任务）。steps 见 _norm_steps；返回任务 id。

        goal 是「用户为什么要做这件事」的一句话（用户原话最好）——会持久化，
        并自动注入每个 llm 步骤的上下文，防止长流程中途失忆。

        步骤动作四类：
        - {"action":"tool",  "tool":"music_search", "args":{...}}
        - {"action":"llm",   "system":"...", "prompt":"可引用 {{步骤id.result}}"}
        - {"action":"batch", "tool":"...", "items":[{args},...], "concurrency":3}
        - {"action":"handler","handler":"名称", "args":{...}}

        提交即校验：工具名必须存在（唯一高置信近似名自动纠正并记录），
        必填参数缺失/类型不符直接拒绝——错误在提交时暴露，绝不跑到一半才炸。
        """
        norm = self._norm_steps(steps)
        norm, autofixed, issues = self._validate_steps(norm)
        hard = [i["msg"] for i in issues if i.get("level") == "error"]
        if hard:
            raise TaskSystemError("步骤校验未通过：" + "；".join(hard))
        action = {
            "kind": "flow", "steps": norm,
            "goal": str(goal or name or "").strip(),
            "step_states": {s["id"]: {"state": PENDING, "attempts": 0,
                                      "result": None, "error": ""} for s in norm},
            "policy": POLICY_CONTINUE if str(policy) == POLICY_CONTINUE else POLICY_ABORT,
            "step_max_attempts": int(max_attempts or self._cfg["max_attempts"]),
            "autofixed": autofixed,
            "adaptive": True,   # 失败先反思重规划（reflection_cap 限制次数），无执行器时退回静态策略
            "llm_budget": int(self._cfg.get("llm_budget", 40)),
            "llm_launched": 0,
            "reflections": 0,
        }
        t = self._new_task("flow", name or "未命名流程", action, queue=queue,
                           priority=priority,
                           timeout=timeout or self._cfg["flow_timeout"],
                           max_attempts=1, durable=bool(durable))
        self._start_coordinator(t)
        if t.durable:
            self._save_journal()
        self._emit("task", f"提交流程 {t.id}「{t.name}」：{len(norm)} 步"
                   + (f"（自动纠正 {len(autofixed)} 处工具名）" if autofixed else ""))
        return t.id

    def submit_batch(self, name: str, tool: str, items: list,
                     concurrency: Optional[int] = None, queue: str = "batch",
                     priority: int = 5, item_timeout: Optional[float] = None,
                     max_attempts: Optional[int] = None, durable: bool = True) -> str:
        """提交批量任务：把 tool 并行 map 到 items（每个 item 是该工具的参数 dict）。

        与流程同样提交即校验：工具名自动纠正、必填参数/类型检查，错误当场拒绝。
        """
        if not isinstance(items, list) or not items:
            raise TaskSystemError("items 必须是非空列表")
        items = [it if isinstance(it, dict) else {"value": it} for it in items]
        norm, autofixed, issues = self._validate_steps(
            [{"id": f"item{i}", "action": "tool", "tool": str(tool), "args": it}
             for i, it in enumerate(items)])
        hard = [i["msg"] for i in issues if i.get("level") == "error"]
        if hard:
            raise TaskSystemError("批量校验未通过：" + "；".join(hard[:5]))
        tool = norm[0]["tool"]           # 可能已被自动纠正
        items = [s["args"] for s in norm]
        action = {
            "kind": "batch", "tool": str(tool), "items": items,
            "concurrency": max(1, int(concurrency or self._cfg["batch_concurrency"])),
            "item_timeout": float(item_timeout or self._cfg["item_timeout"]),
            "item_max_attempts": int(max_attempts or self._cfg["max_attempts"]),
            "item_states": {str(i): {"state": PENDING, "result": None, "error": ""}
                            for i in range(len(items))},
        }
        t = self._new_task("batch", name or f"批量:{tool}", action, queue=queue,
                           priority=priority,
                           timeout=max(self._cfg["flow_timeout"],
                                       self._cfg["item_timeout"] * len(items)),
                           max_attempts=1, durable=bool(durable))
        self._start_coordinator(t)
        if t.durable:
            self._save_journal()
        self._emit("task", f"提交批量 {t.id}「{t.name}」：{len(items)} 条，并发 {action['concurrency']}")
        return t.id

    # ---- 步骤规范化与校验 ----

    def _norm_steps(self, steps: list) -> list:
        global TEMPLATE_RE
        if TEMPLATE_RE is None:
            import re
            TEMPLATE_RE = re.compile(r"\{\{\s*([A-Za-z0-9_.\-\u4e00-\u9fff]+)\.result\s*\}\}")
        if not isinstance(steps, list) or not steps:
            raise TaskSystemError("steps 必须是非空列表")
        seen: set = set()
        norm = []
        for i, s in enumerate(steps):
            if not isinstance(s, dict):
                raise TaskSystemError(f"步骤 {i} 必须是对象")
            sid = str(s.get("id") or f"s{i}")
            if sid in seen:
                raise TaskSystemError(f"步骤 id 重复: {sid}")
            seen.add(sid)
            act = str(s.get("action") or "").strip().lower()
            item = {"id": sid,
                    "deps": [str(d) for d in (s.get("deps") or [])],
                    "action": act,
                    "timeout": s.get("timeout"),
                    "max_attempts": s.get("max_attempts"),
                    "confirm": str(s["confirm"]) if s.get("confirm") else "",
                    "expect": str(s["expect"]) if s.get("expect") else ""}
            if act == "tool":
                if not s.get("tool"):
                    raise TaskSystemError(f"步骤 {sid}: tool 动作缺少 tool")
                item["tool"] = str(s["tool"])
                item["args"] = dict(s.get("args") or {})
            elif act == "llm":
                if not s.get("prompt"):
                    raise TaskSystemError(f"步骤 {sid}: llm 动作缺少 prompt")
                item["system"] = str(s.get("system") or "")
                item["prompt"] = str(s["prompt"])
                item["max_tokens"] = int(s.get("max_tokens") or 800)
            elif act == "batch":
                if not s.get("tool") or not isinstance(s.get("items"), list) or not s["items"]:
                    raise TaskSystemError(f"步骤 {sid}: batch 动作需要 tool 与非空 items")
                item["tool"] = str(s["tool"])
                item["items"] = [it if isinstance(it, dict) else {"value": it}
                                 for it in s["items"]]
                item["concurrency"] = max(1, int(s.get("concurrency") or
                                                 self._cfg["batch_concurrency"]))
            elif act == "handler":
                if not s.get("handler"):
                    raise TaskSystemError(f"步骤 {sid}: handler 动作缺少 handler 名")
                item["handler"] = str(s["handler"])
                item["args"] = dict(s.get("args") or {})
            else:
                raise TaskSystemError(f"步骤 {sid}: 未知动作 {act!r}（支持 tool/llm/batch/handler）")
            norm.append(item)
        # 依赖存在性 + 环检测（Kahn）
        for s in norm:
            for d in s["deps"]:
                if d not in seen:
                    raise TaskSystemError(f"步骤 {s['id']} 依赖不存在的步骤 {d}")
        indeg = {s["id"]: len(s["deps"]) for s in norm}
        children: dict[str, list] = {s["id"]: [] for s in norm}
        for s in norm:
            for d in s["deps"]:
                children[d].append(s["id"])
        queue_ids = [k for k, v in indeg.items() if v == 0]
        visited = 0
        while queue_ids:
            n = queue_ids.pop()
            visited += 1
            for c in children[n]:
                indeg[c] -= 1
                if indeg[c] == 0:
                    queue_ids.append(c)
        if visited != len(norm):
            raise TaskSystemError("步骤依赖存在环，无法拓扑执行")
        return norm

    # ---- 提交时校验：工具存在性 + 必填参数 + 类型（错误前置到提交一刻） ----

    # ---- 跨任务经验库：把踩过的坑变成下一次规划的先验 ----

    def _lesson_file(self) -> Path:
        return self.base_dir / "harness_task_memory.json"

    def _lessons(self) -> list:
        try:
            with open(self._lesson_file(), "r", encoding="utf-8") as f:
                data = json.load(f)
            ls = data.get("lessons") if isinstance(data, dict) else None
            return [str(x) for x in ls][:60] if isinstance(ls, list) else []
        except Exception:
            return []

    def _remember_lesson(self, text: str) -> None:
        """记一条经验（去重、保最近 60 条、原子落盘）。失败静默——记忆不能拖垮执行。"""
        text = str(text or "").strip()
        if not text:
            return
        try:
            lessons = self._lessons()
            if text in lessons:
                return
            lessons.insert(0, text)
            lessons = lessons[:60]
            tmp = str(self._lesson_file()) + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"lessons": lessons}, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self._lesson_file())
            self._emit("task", f"经验入库：{text[:80]}")
        except Exception as e:
            logger.warning("经验入库失败: %s", e)

    def _lessons_prompt(self, cap: int = 8) -> str:
        """给规划器/反思器注入的历史经验段（无经验返回空串）。"""
        ls = self._lessons()[:cap]
        if not ls:
            return ""
        return "【历史经验（此前任务踩过的坑/成功路径）】\n" + \
            "\n".join(f"- {x[:90]}" for x in ls) + "\n\n"

    def _record_flow_lessons(self, job: Task) -> None:
        """流程终态时提炼经验：反思救回的路径 / 彻底失败的用法 / 批量失败模式。"""
        try:
            act = job.action or {}
            if act.get("kind") != "flow":
                return
            states = act.get("step_states") or {}
            steps = {s.get("id"): s for s in act.get("steps") or []}
            goal = str(act.get("goal") or job.name)[:40]
            if job.state == SUCCEEDED and int(act.get("reflections") or 0) > 0:
                self._remember_lesson(
                    f"目标『{goal}』曾有步骤失败，经反思修订后成功——类似失败优先尝试换工具/换参数绕过")
            for sid, st in states.items():
                if st.get("state") == FAILED:
                    sdef = steps.get(sid) or {}
                    tool = sdef.get("tool") or sdef.get("action")
                    args = json.dumps(sdef.get("args") or {}, ensure_ascii=False)[:60]
                    err = (st.get("error") or "")[:80]
                    self._remember_lesson(f"工具 {tool} 在参数 {args} 下失败: {err}")
        except Exception:
            pass

    def _record_batch_lessons(self, job: Task) -> None:
        try:
            act = job.action or {}
            if act.get("kind") != "batch":
                return
            r = job.result if isinstance(job.result, dict) else {}
            failed = int(r.get("failed") or 0)
            total = int(r.get("count") or 0)
            if failed and total and failed >= max(2, total // 3):
                first = next(iter((r.get("failures") or {}).values()), "")
                self._remember_lesson(
                    f"批量 {act.get('tool')} 大面积失败({failed}/{total}): {str(first)[:70]}")
        except Exception:
            pass

    def set_notifier(self, fn: Callable) -> None:
        """注册完成推送回调（server 注入：WebSocket 广播）。签名 async/sync (report: dict)。"""
        self._notifier = fn

    async def _notify(self, report: dict) -> None:
        fn = getattr(self, "_notifier", None)
        if fn is None:
            return
        try:
            r = fn(report)
            if asyncio.iscoroutine(r):
                await r
        except Exception:
            pass

    def _tool_catalog(self) -> dict:
        """工具目录：name → function 定义（来自 harness 技能/插件注册的全部工具）。"""
        try:
            cat = {}
            for spec in (self.harness.collect_tool_specs() or []):
                fn = (spec or {}).get("function") or {}
                nm = fn.get("name")
                if nm:
                    cat[str(nm)] = fn
            return cat
        except Exception:
            return {}

    def _validate_steps(self, steps: list) -> tuple:
        """校验（并尽力自动纠正）步骤里的工具引用与参数。

        返回 (steps, autofixed_notes, issues)：
        - 工具名拼错但与某个真实工具唯一高置信近似 → 自动纠正并记录；
        - 工具不存在（无法可靠纠正）/ 缺必填参数 / 类型不符 → level=error，
          由 submit_flow 拒绝提交——绝不带着已知错误跑一半才炸。
        """
        import difflib
        issues: list = []
        autofixed: list = []
        catalog = self._tool_catalog()
        if not catalog:
            issues.append({"level": "warn", "msg": "工具目录暂不可用，跳过工具校验"})
            return steps, autofixed, issues
        for s in steps:
            if s["action"] not in ("tool", "batch"):
                continue
            tn = s.get("tool")
            if tn not in catalog:
                # 自动纠正只信"编辑距离≤2 的唯一最近候选"（真实笔误），
                # 险些撞名的长怪名宁可报错让人工确认——绝不猜。
                cand = sorted(((_levenshtein(tn, nm), nm) for nm in catalog
                               if _levenshtein(tn, nm) <= 2))
                if cand and (len(cand) == 1 or cand[0][0] < cand[1][0]):
                    autofixed.append(f"步骤 {s['id']}: 工具名 '{tn}' → '{cand[0][1]}'")
                    self._remember_lesson(f"工具名常见笔误: {tn} → {cand[0][1]}")
                    s["tool"] = cand[0][1]
                    tn = cand[0][1]
                else:
                    close = difflib.get_close_matches(tn, list(catalog), n=3, cutoff=0.6)
                    msg = f"步骤 {s['id']}: 工具 '{tn}' 不存在"
                    msg += f"（你是不是想用：{'、'.join(close)}）" if close else "（先用 skill_help 查可用工具）"
                    issues.append({"level": "error", "msg": msg})
                    continue
            fn = catalog.get(tn) or {}
            params = fn.get("parameters") or {}
            props = params.get("properties") or {}
            required = list(params.get("required") or [])
            arg_list = ([("步骤 " + s["id"], s.get("args") or {})]
                        if s["action"] == "tool"
                        else [(f"步骤 {s['id']} 第{i}条", a)
                              for i, a in enumerate(s.get("items") or [])])
            for where, a in arg_list:
                missing = [k for k in required if k not in (a or {})]
                if missing:
                    issues.append({"level": "error",
                                   "msg": f"{where}: 缺少必填参数 {', '.join(missing)}（工具 {tn}）"})
                for k, v in (a or {}).items():
                    pt = (props.get(k) or {}).get("type")
                    if pt and not _type_ok(v, pt):
                        issues.append({"level": "error",
                                       "msg": f"{where}: 参数 {k} 应为 {pt} 类型（工具 {tn}）"})
        return steps, autofixed, issues

    # ==================== 调度核心 ====================

    def _enqueue(self, t: Task, delay: float = 0.0) -> None:
        q = self._ensure_queue(t.queue)
        t.seq = next(self._seq)
        t.state = PENDING
        t.status_text = "排队中" if delay <= 0 else f"{delay:.0f}s 后重试"
        heapq.heappush(q.heap, (t.priority, t.seq, time.monotonic() + max(0.0, delay), t.id))
        q.wakeup.set()

    def _ensure_coordinator_running(self, t: Task) -> None:
        cur = self._coordinators.get(t.id)
        if cur is None or cur.done():
            self._coordinators[t.id] = asyncio.get_running_loop().create_task(
                self._coordinate(t))

    def _start_coordinator(self, t: Task) -> None:
        t.state = RUNNING
        t.started_at = time.time()
        t.status_text = "协调中"
        self._ensure_coordinator_running(t)

    async def _worker_loop(self, q: _QueueState) -> None:
        while True:
            try:
                t = self._next_ready(q)
                if t is None:
                    try:
                        await asyncio.wait_for(q.wakeup.wait(), timeout=0.5)
                    except TimeoutError:
                        pass
                    q.wakeup.clear()
                    continue
                if q.paused:
                    # 暂停期间把任务放回（保持排队态），半秒后再看
                    heapq.heappush(q.heap, (t.priority, t.seq, 0.0, t.id))
                    await asyncio.sleep(0.5)
                    continue
                if q.min_interval > 0:
                    gap = q.min_interval - (time.monotonic() - q.last_start)
                    if gap > 0:
                        heapq.heappush(q.heap, (t.priority, t.seq, time.monotonic() + gap, t.id))
                        await asyncio.sleep(min(gap, 1.0))
                        continue
                    q.last_start = time.monotonic()
                await self._run_leaf(t, q)
            except asyncio.CancelledError:
                return
            except Exception as e:  # worker 永不死亡
                logger.warning("worker[%s] 异常: %s\n%s", q.name, e, traceback.format_exc())
                await asyncio.sleep(0.2)

    def _next_ready(self, q: _QueueState) -> Optional[Task]:
        """取出一个就绪任务（跳过已被取消/终态的堆内残留）。"""
        now = time.monotonic()
        while q.heap:
            pri, seq, ready_at, tid = q.heap[0]
            t = self._tasks.get(tid)
            if t is None or t.state in TERMINAL:
                heapq.heappop(q.heap)
                continue
            if ready_at > now:
                return None  # 最早的还没到点
            heapq.heappop(q.heap)
            if t.state == PENDING:
                return t
        return None

    async def _run_leaf(self, t: Task, q: _QueueState) -> None:
        t._set_state(RUNNING)
        t.started_at = t.started_at or time.time()
        t.attempts += 1
        q.running[t.id] = t
        # 执行体包成独立 asyncio.Task：外部 cancel() 能中断运行中的长动作
        t._exec_task = asyncio.get_running_loop().create_task(self._leaf_body(t))
        try:
            await t._exec_task
        except asyncio.CancelledError:
            # 子任务被取消（cancel() 调的是 _exec_task），worker 自身继续服务
            if t.state not in TERMINAL:
                t._set_state(CANCELLED, "已取消")
                t.status_text = "已取消"
        except Exception:
            pass  # _leaf_body 内部已归档为失败/重试
        finally:
            t._exec_task = None
            q.running.pop(t.id, None)
            self._after_terminal(t)

    async def _leaf_body(self, t: Task) -> None:
        t.status_text = f"执行中（第 {t.attempts} 次）"
        try:
            result = await asyncio.wait_for(
                self._execute(t),
                timeout=t.timeout or self._cfg["default_timeout"])
            # 结果反查：工具/技能以正常返回值夹带系统级错误文案 → 视为失败
            if self._cfg.get("result_guard", True) and isinstance(result, str) \
                    and any(p in result for p in _RESULT_GUARD_PATTERNS):
                await self._leaf_failed(t, f"工具返回了错误信息：{result[:120]}")
                return
            t.result = result
            t._set_state(SUCCEEDED)
            t.status_text = "已完成"
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            await self._leaf_failed(t, f"执行超时（>{t.timeout or self._cfg['default_timeout']}s）")
        except Exception as e:
            await self._leaf_failed(t, f"{e.__class__.__name__}: {e}")

    async def _leaf_failed(self, t: Task, msg: str) -> None:
        if t.attempts < t.max_attempts:
            delay = float(self._cfg["backoff"]) * (2 ** (t.attempts - 1))
            t._set_state(PENDING, msg)
            t.status_text = f"第 {t.attempts} 次失败，{delay:.0f}s 后重试"
            self._emit("task", f"任务 {t.id}（{t.name}）{msg}，{delay:.0f}s 后重试"
                       f"（{t.attempts}/{t.max_attempts}）")
            self._enqueue(t, delay)
        else:
            t._set_state(FAILED, msg)
            t.status_text = "失败（已耗尽重试）"
            self._emit("error", f"任务 {t.id}（{t.name}）最终失败: {msg}")

    # ---- 叶子动作执行 ----

    async def _execute(self, t: Task) -> Any:
        act = t.action or {}
        kind = act.get("kind")
        if kind == "py":
            return await act["coro"]()
        if kind == "handler":
            fn = self._handlers.get(act.get("name"))
            if fn is None:
                raise TaskSystemError(f"处理器未注册: {act.get('name')}")
            r = fn(**(act.get("args") or {}))
            if asyncio.iscoroutine(r):
                r = await r
            return r
        if kind == "tool":
            result = await self._exec_tool(str(act.get("tool")), dict(act.get("args") or {}),
                                           timeout=t.timeout)
            if act.get("expect"):
                await self._verify_expect(t, act["expect"], result)
            return result
        if kind == "llm":
            system = act.get("system") or ""
            if act.get("flow_parent"):
                parent = self._tasks.get(act["flow_parent"])
                if parent is not None:
                    system = (system + "\n\n" if system else "") + self._flow_context(parent)
            result = await self._exec_llm(system, act.get("prompt") or "",
                                          act.get("max_tokens") or 800, timeout=t.timeout)
            if act.get("expect"):
                await self._verify_expect(t, act["expect"], result)
            return result
        if kind == "batch_inline":
            return await self._exec_batch_inline(act, timeout=t.timeout)
        raise TaskSystemError(f"未知动作类型: {kind}")

    async def _exec_tool(self, tool: str, args: dict, timeout: Optional[float]) -> str:
        """工具步骤走 runtime 监督（熔断/超时/计量）——任务系统也被监督运行时罩住。"""
        def factory():
            async def call():
                res, _src = await self.harness.execute_tool(tool, args)
                if res is None:
                    raise TaskSystemError(f"工具不存在或未启用: {tool}")
                return res
            return call()
        rt = getattr(self.harness, "runtime", None)
        if rt is not None:
            res, ok = await rt.supervise_tool(tool, factory, timeout=timeout)
            if not ok:
                raise TaskSystemError(res)
            return res
        return await factory()

    async def _exec_llm(self, system: str, prompt: str, max_tokens: int,
                        timeout: Optional[float]) -> str:
        executor = self._llm_executor
        if executor is None:
            # 重启窗口内 agent 尚未初始化：等待注册而不是立刻失败（可等待时间可配）
            deadline = time.monotonic() + float(self._cfg["llm_wait_executor"])
            while self._llm_executor is None and time.monotonic() < deadline:
                await asyncio.sleep(1.0)
            executor = self._llm_executor
        if executor is None:
            raise TaskSystemError("LLM 执行器未注册（Agent 未初始化）")
        r = executor(system, prompt, max_tokens=max_tokens)
        if asyncio.iscoroutine(r):
            r = await r
        return str(r)

    # ---- 语义级结果校验（expect）：结果"成功返回"且不报错,但内容没答到点子上 ----

    _VERIFIER_SYSTEM = (
        "你是任务系统的结果校验器。判断给定结果是否满足期望，"
        '只输出严格 JSON：{"ok":true,"reason":"..."} 或 {"ok":false,"reason":"不满足的具体原因"}。'
    )

    async def _verify_expect(self, t: Task, expect: str, result: Any) -> None:
        """按步骤声明的 expect 语义校验结果；不满足则抛错（按步骤失败处理→可重试/反思）。

        校验本身是一次 plan 渠道小调用（150 token 上限），计入流程 LLM 预算；
        校验器输出解析失败时放行——宁可漏判不可误杀。
        """
        if not self._cfg.get("expect_verify", True) or self._llm_executor is None:
            return
        # 计入父流程 LLM 预算（校验也是 LLM 调用）
        parent = self._tasks.get(t.parent) if t.parent else None
        if parent is not None:
            parent.action["llm_launched"] = int(parent.action.get("llm_launched") or 0) + 1
        prompt = (
            f"【期望】{str(expect)[:300]}\n"
            f"【实际结果】{_short(result, 1500)}\n"
            "结果是否满足期望？只输出严格 JSON。"
        )
        try:
            raw = await self._exec_llm(self._VERIFIER_SYSTEM, prompt,
                                       max_tokens=150, timeout=60)
        except Exception as e:
            logger.warning("expect 校验调用失败，放行: %s", e)
            return
        d = _extract_json(raw)
        if isinstance(d, dict) and d.get("ok") is False:
            raise TaskSystemError(
                f"结果校验未通过：{str(d.get('reason') or '不满足期望')[:150]}（期望: {str(expect)[:80]}）")
        # ok=True 或解析失败 → 放行

    async def _exec_batch_inline(self, act: dict, timeout: Optional[float]) -> list:
        """流程内的 batch 步骤：gather + 信号量，结果按序返回。"""
        tool = str(act.get("tool"))
        items = act.get("items") or []
        sem = asyncio.Semaphore(max(1, int(act.get("concurrency") or 3)))

        async def one(i: int):
            async with sem:
                try:
                    return await self._exec_tool(tool, dict(items[i]), timeout=timeout)
                except Exception as e:
                    return f"[条目{i}失败] {e}"
        return list(await asyncio.gather(*(one(i) for i in range(len(items)))))

    # ==================== 流程 / 批量协调器（不占 worker 槽） ====================

    async def _coordinate(self, job: Task) -> None:
        try:
            if job.action.get("kind") == "flow":
                await self._run_flow(job)
            else:
                await self._run_batch_job(job)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            job._set_state(FAILED, f"协调器异常: {e}")
            self._emit("error", f"任务 {job.id} 协调器异常: {e}")
        finally:
            self._coordinators.pop(job.id, None)
            if job.kind in ("flow", "batch") and job.state in TERMINAL:
                report = {
                    "id": job.id, "name": job.name, "kind": job.kind,
                    "state": job.state, "status": job.status_text,
                    "goal": str(job.action.get("goal") or "")[:60],
                }
                self._pending_reports.append(report)
                # 跨任务经验提炼 + WebSocket 完成推送
                if job.kind == "flow":
                    self._record_flow_lessons(job)
                else:
                    self._record_batch_lessons(job)
                await self._notify(report)
            if job.durable:
                self._save_journal()

    def drain_reports(self, limit: int = 5) -> list:
        """取走并清空「已完成待汇报」队列（拉一次即清，不重复计费）。"""
        out = []
        while self._pending_reports and len(out) < limit:
            out.append(self._pending_reports.popleft())
        return out

    async def _run_flow(self, job: Task) -> None:
        act = job.action
        policy = act.get("policy", POLICY_ABORT)
        deadline = time.monotonic() + (job.timeout or self._cfg["flow_timeout"])
        children: dict[str, Task] = {}   # step_id -> child task
        WAITING = "waiting_confirm"

        # 恢复场景：上次运行中被标记 running 的步骤回 pending 重跑
        for st in act["step_states"].values():
            if st["state"] == RUNNING:
                st["state"] = PENDING

        def steps() -> list:
            return act["steps"]

        def states() -> dict:
            return act["step_states"]

        def step_terminal(s: dict) -> bool:
            return states()[s["id"]]["state"] in TERMINAL

        def deps_ok(s: dict) -> bool:
            return all(states()[d]["state"] == SUCCEEDED for d in s["deps"])

        def any_failed() -> bool:
            return any(states()[s["id"]]["state"] == FAILED for s in steps())

        while True:
            if job.state == CANCELLED:
                return  # cancel() 已置终态并取消了子任务
            # 同步子任务终态到步骤状态（_after_terminal 已同步过，这里兜底幂等）
            for sid, child in children.items():
                if sid in states() and child.state in TERMINAL and states()[sid]["state"] == RUNNING:
                    states()[sid]["state"] = child.state
                    states()[sid]["result"] = _shrink(child.result) if child.state == SUCCEEDED else states()[sid]["result"]
                    states()[sid]["error"] = child.error

            # ---- 失败决策：先反思（能不能换方法），再按策略传播 ----
            effective_policy = policy
            if any_failed():
                decision = await self._maybe_reflect(job)
                if decision == "revised":
                    children = {}  # 步骤表已重写，旧 children 引用作废（运行中的已保留在 states）
                elif decision == "abort":
                    effective_policy = POLICY_ABORT
                elif decision == "continue":
                    effective_policy = POLICY_CONTINUE
                elif decision is None and policy == POLICY_ABORT:
                    effective_policy = POLICY_ABORT
            if any_failed():
                if effective_policy == POLICY_ABORT:
                    for s in steps():
                        if states()[s["id"]]["state"] in (PENDING, WAITING):
                            states()[s["id"]]["state"] = CANCELLED
                            states()[s["id"]]["error"] = "前置失败，流程中止"
                    act.pop("confirming", None)
                    for child in children.values():
                        if child.state not in TERMINAL:
                            self.cancel(child.id)
                else:  # continue：只放弃失败分支的后续
                    changed = True
                    while changed:
                        changed = False
                        for s in steps():
                            if states()[s["id"]]["state"] == PENDING and \
                                    any(states()[d]["state"] in (FAILED, CANCELLED) for d in s["deps"]):
                                states()[s["id"]]["state"] = CANCELLED
                                states()[s["id"]]["error"] = "依赖失败，分支放弃"
                                changed = True

            # ---- 提交就绪步骤（模板解析在此刻做：依赖结果已就绪） ----
            confirmed = act.setdefault("confirmed", [])
            for s in steps():
                st = states()[s["id"]]
                if st["state"] != PENDING or not deps_ok(s):
                    continue
                # 审慎：声明了 confirm 的危险步骤先要用户批准
                if s.get("confirm") and s["id"] not in confirmed:
                    st["state"] = WAITING
                    act.setdefault("confirming", {})[s["id"]] = str(s["confirm"])
                    self._save_journal()
                    self._emit("task", f"流程 {job.id} 步骤 {s['id']} 等待用户确认：{s['confirm']}")
                    continue
                # 防失控：llm 步骤有总量预算
                if s["action"] == "llm":
                    launched = int(act.get("llm_launched") or 0)
                    budget = int(act.get("llm_budget") or self._cfg.get("llm_budget", 40))
                    if launched >= budget:
                        st["state"] = CANCELLED
                        st["error"] = f"LLM 预算耗尽（>{budget} 次调用）"
                        continue
                    act["llm_launched"] = launched + 1
                self._submit_step(job, s, st, children)

            done = sum(1 for s in steps() if step_terminal(s))
            job.progress = int(done * 100 / max(1, len(steps())))
            confirming = act.get("confirming") or {}
            job.status_text = (f"等待确认：{next(iter(confirming.values()))[:40]}"
                               if confirming else f"步骤 {done}/{len(steps())}")
            if all(step_terminal(s) for s in steps()):
                break

            remain = deadline - time.monotonic()
            if remain <= 0:
                for child in children.values():
                    if child.state not in TERMINAL:
                        self.cancel(child.id)
                job._set_state(FAILED, f"流程整体超时（>{job.timeout or self._cfg['flow_timeout']}s）")
                job.status_text = "整体超时"
                self._emit("error", f"流程 {job.id} 整体超时")
                return
            job.wake.clear()
            try:
                await asyncio.wait_for(job.wake.wait(), timeout=min(remain, 30.0))
            except TimeoutError:
                pass

        # 汇总
        failed = [s["id"] for s in steps() if states()[s["id"]]["state"] == FAILED]
        cancelled = [s["id"] for s in steps() if states()[s["id"]]["state"] == CANCELLED]
        job.result = {
            "steps": {s["id"]: {"state": states()[s["id"]]["state"],
                                "result": _shrink(states()[s["id"]]["result"]),
                                "error": states()[s["id"]]["error"]}
                      for s in steps()},
            "reflections": int(act.get("reflections") or 0),
        }
        if failed or (policy == POLICY_ABORT and cancelled):
            job._set_state(FAILED, f"失败步骤: {','.join(failed) or '无'}；"
                                   f"取消步骤: {','.join(cancelled) or '无'}")
            job.status_text = f"失败（{len(failed)} 步失败）"
        else:
            job._set_state(SUCCEEDED)
            job.status_text = "流程完成"
        self._emit("task", f"流程 {job.id}「{job.name}」结束：{job.state}")

    # ---- 失败反思：换方法重规划（审慎决策的核心） ----

    _REFLECT_SYSTEM = (
        "你是「大白」任务系统的故障决策器。一个多步骤流程中有步骤最终失败了，"
        "请基于目标与已有结果理性决策：能绕过就修订步骤（换工具/换参数/换路径），"
        "确实无法达成就放弃，失败分支无价值但其余有价值就继续。只输出严格 JSON。"
    )

    async def _maybe_reflect(self, job: Task) -> Optional[str]:
        """步骤失败后的反思决策。返回 'revised' | 'abort' | 'continue' | None(未启用/失败)。

        反思次数受 reflection_cap（默认 3）限制，防止修订循环；无 LLM 执行器时
        直接返回 None 走静态策略。修订会保留已成功/运行中的步骤与结果。
        """
        act = job.action
        if not act.get("adaptive", True):
            return None
        if self._llm_executor is None:
            return None
        cap = int(self._cfg.get("reflection_cap", 3))
        used = int(act.get("reflections") or 0)
        if used >= cap:
            return None
        states = act["step_states"]
        # 反思要看得深：失败步骤给足 600 字错误与部分产出（根因常藏在长输出里），
        # 成功步骤给 200 字摘要；再叠加跨任务经验库。
        overview = "\n".join(
            f"- {s['id']}（{s['action']}" + (f" {s.get('tool', '')}" if s.get("tool") else "") + f"）: "
            + states[s["id"]]["state"]
            + (f"｜结果: {_short(states[s['id']].get('result'), 200)}"
               if states[s["id"]]["state"] == SUCCEEDED and states[s["id"]].get("result") else "")
            + ((f"｜错误: {(states[s['id']].get('error') or '')[:600]}"
                + (f"｜失败前的部分产出: {_short(states[s['id']].get('result'), 200)}"
                   if states[s["id"]].get("result") else ""))
               if states[s["id"]]["state"] == FAILED else "")
            for s in act["steps"])
        prompt = (
            f"【任务目标】{act.get('goal') or job.name}\n"
            f"【流程现状】\n{overview}\n\n"
            + self._lessons_prompt()
            + "请决策（输出严格 JSON，不要 markdown）：\n"
            '{"action":"revise|abort|continue","reason":"...","steps":[...]}\n'
            "- revise：失败可绕过。steps 给出**全部未成功步骤**的新定义（已成功步骤自动保留、"
            "其 id 可被依赖；新步骤 deps 只能引用已成功或新步骤的 id；工具必须真实存在、"
            "必填参数齐全；一步能完成的不拆两步）；\n"
            "- abort：目标无法达成，放弃剩余；\n"
            "- continue：失败分支放弃，独立分支继续。"
        )
        try:
            raw = await self._exec_llm(self._REFLECT_SYSTEM, prompt,
                                       max_tokens=1200, timeout=180)
            data = _extract_json(raw)
        except Exception as e:
            self._emit("error", f"流程 {job.id} 反思调用失败，走静态策略: {e}")
            return None
        if not isinstance(data, dict):
            return None
        decision = str(data.get("action") or "").strip().lower()
        reason = str(data.get("reason") or "")[:200]
        if decision == "revise":
            err = self._apply_revision(job, data.get("steps") or [])
            if err is None:
                act["reflections"] = used + 1
                self._emit("task", f"流程 {job.id} 反思修订#{used + 1}：{reason}")
                self._save_journal()
                return "revised"
            self._emit("error", f"流程 {job.id} 反思修订被拒（{err}），重试反思或走静态策略")
            act["reflections"] = used + 1  # 无效修订也计数，防循环
            return None
        if decision in ("abort", "continue"):
            act["reflections"] = used + 1
            self._emit("task", f"流程 {job.id} 反思决策 {decision}：{reason}")
            return decision
        return None

    def _apply_revision(self, job: Task, new_steps_raw: list) -> Optional[str]:
        """把反思修订并入流程：保留已成功/运行中步骤，替换其余。返回 None=成功。"""
        act = job.action
        try:
            norm = self._norm_steps(new_steps_raw)
        except TaskSystemError as e:
            return f"修订步骤不合法: {e}"
        if not norm:
            return "修订步骤为空"
        norm, autofixed, issues = self._validate_steps(norm)
        if any(i.get("level") == "error" for i in issues):
            return issues[0]["msg"]
        states = act["step_states"]
        keep_ids = {sid for sid, st in states.items()
                    if st["state"] in (SUCCEEDED, RUNNING)}
        valid_ids = keep_ids | {s["id"] for s in norm}
        for s in norm:
            for d in s["deps"]:
                if d not in valid_ids:
                    return f"修订步骤 {s['id']} 依赖不存在的 {d}"
        act["steps"] = [s for s in act["steps"] if s["id"] in keep_ids] + norm
        for sid in list(states):
            if sid not in keep_ids:
                states.pop(sid)
        for s in norm:
            states[s["id"]] = {"state": PENDING, "attempts": 0,
                               "result": None, "error": ""}
        if autofixed:
            act["autofixed"] = list(act.get("autofixed") or []) + \
                [f"[反思#{int(act.get('reflections') or 0) + 1}] {a}" for a in autofixed]
        return None

    def _submit_step(self, job: Task, step: dict, st: dict, children: dict) -> None:
        sid = step["id"]
        act = {"kind": step["action"]}
        if step["action"] == "tool":
            act.update(tool=step["tool"],
                       args=self._resolve_templates(step.get("args") or {}, job),
                       expect=step.get("expect") or "")
        elif step["action"] == "llm":
            act.update(system=step.get("system") or "",
                       prompt=self._resolve_templates(step.get("prompt") or "", job),
                       max_tokens=step.get("max_tokens") or 800,
                       expect=step.get("expect") or "",
                       flow_parent=job.id)  # 执行时注入任务目标+已完成摘要（防中途失忆）
        elif step["action"] == "batch":
            act = {"kind": "batch_inline",
                   "tool": step["tool"],
                   "items": step.get("items") or [],
                   "concurrency": step.get("concurrency") or 3}
        elif step["action"] == "handler":
            act.update(name=step["handler"],
                       args=self._resolve_templates(step.get("args") or {}, job))
        child = self._new_task(
            "step", f"{job.name}·{sid}", act, queue=job.queue, priority=job.priority,
            timeout=float(step["timeout"]) if step.get("timeout") else None,
            max_attempts=int(step["max_attempts"]) if step.get("max_attempts")
            else int(job.action.get("step_max_attempts") or 2),
            durable=False, parent=job.id, step_key=sid)
        st["state"] = RUNNING
        children[sid] = child
        self._enqueue(child)

    def _flow_context(self, job: Task) -> str:
        """llm 步骤的全局上下文：任务目标 + 当前进度 + 已完成步骤结果摘要。

        长流程/重启续跑后，每一步仍知道自己为什么而做、前面做了什么——不失忆。
        """
        goal = (job.action.get("goal") or job.name or "").strip()
        states = job.action.get("step_states") or {}
        lines = [f"【任务目标】{goal}", f"【当前进度】{job.status_text}"]
        done = [f"- {sid}：{_short(st.get('result'), 300)}"
                for sid, st in states.items()
                if st.get("state") == SUCCEEDED and st.get("result")]
        if done:
            lines.append("【已完成步骤及结果摘要】\n" + "\n".join(done[-8:]))
        running = [sid for sid, st in states.items() if st.get("state") == RUNNING]
        if running:
            lines.append("【并行进行中的其他步骤】" + "、".join(running))
        lines.append("请基于以上上下文完成当前这一步，始终服务于任务目标。")
        return "\n".join(lines)

    def _resolve_templates(self, v: Any, job: Task) -> Any:
        """把 {{步骤id.result}} 替换为该步骤结果（截断保护；未知引用原样保留）。"""
        states = job.action.get("step_states", {})
        cap = int(self._cfg["template_result_max"])

        def sub(text: str) -> str:
            global TEMPLATE_RE
            if TEMPLATE_RE is None:
                import re
                TEMPLATE_RE = re.compile(r"\{\{\s*([A-Za-z0-9_.\-\u4e00-\u9fff]+)\.result\s*\}\}")
            def repl(m):
                sid = m.group(1)
                if sid not in states:
                    return m.group(0)  # 引用不存在（如批量条目场景）→ 原样保留
                r = states[sid].get("result")
                s = r if isinstance(r, str) else json.dumps(r, ensure_ascii=False, default=str)
                return s[:cap]
            return TEMPLATE_RE.sub(repl, text)

        if isinstance(v, str):
            return sub(v)
        if isinstance(v, dict):
            return {k: self._resolve_templates(x, job) for k, x in v.items()}
        if isinstance(v, list):
            return [self._resolve_templates(x, job) for x in v]
        return v

    async def _run_batch_job(self, job: Task) -> None:
        act = job.action
        items = act["items"]
        states = act["item_states"]
        sem = asyncio.Semaphore(int(act.get("concurrency") or 3))
        pending_ids: dict[str, str] = {}   # item idx(str) -> child task id

        # 恢复：running → pending
        for st in states.values():
            if st["state"] == RUNNING:
                st["state"] = PENDING

        def submit_item(idx: str):
            child = self._new_task(
                "item", f"{job.name}#{idx}",
                {"kind": "tool", "tool": act["tool"],
                 "args": self._resolve_templates(items[int(idx)], job)},
                queue=job.queue, priority=job.priority,
                timeout=act.get("item_timeout"), durable=False,
                parent=job.id, step_key=idx,
                max_attempts=act.get("item_max_attempts") or 2)
            states[idx]["state"] = RUNNING
            pending_ids[idx] = child.id
            self._enqueue(child)

        for i in [k for k, v in states.items() if v["state"] == PENDING]:
            submit_item(i)

        deadline = time.monotonic() + (job.timeout or self._cfg["flow_timeout"])
        while pending_ids:
            if job.state == CANCELLED:
                return  # cancel() 已置终态并取消了子任务
            remain = deadline - time.monotonic()
            if remain <= 0:
                for cid in pending_ids.values():
                    self.cancel(cid)
                job._set_state(FAILED, f"批量整体超时（>{job.timeout}s）")
                return
            job.wake.clear()
            try:
                await asyncio.wait_for(job.wake.wait(), timeout=min(remain, 30.0))
            except TimeoutError:
                pass
            for idx in list(pending_ids):
                cid = pending_ids[idx]
                child = self._tasks.get(cid)
                if child is not None and child.state in TERMINAL:
                    states[idx] = {"state": child.state,
                                   "result": _shrink(child.result),
                                   "error": child.error}
                    del pending_ids[idx]
                    done = sum(1 for v in states.values() if v["state"] in TERMINAL)
                    job.progress = int(done * 100 / len(items))
                    job.status_text = f"{done}/{len(items)}"
                    self._maybe_save(job)

        failures = {k: v["error"] for k, v in states.items() if v["state"] == FAILED}
        job.result = {"count": len(items),
                      "ok": len(items) - len(failures),
                      "failed": len(failures),
                      "results": [states[str(i)]["result"] for i in range(len(items))],
                      "failures": failures}
        if failures:
            job._set_state(FAILED, f"{len(failures)}/{len(items)} 条失败（结果含部分成功项）")
            job.status_text = f"部分失败（{len(failures)}/{len(items)}）"
        else:
            job._set_state(SUCCEEDED)
            job.status_text = f"全部完成（{len(items)} 条）"
        self._emit("task", f"批量 {job.id}「{job.name}」结束：{job.status_text}")

    def _after_terminal(self, t: Task) -> None:
        """叶子到达终态（成功/失败/取消）：同步回父任务状态、唤醒协调器、落盘检查点。

        注意：任务因重试回到 PENDING 时不算终态——绝不能把父流程的步骤状态
        改回 PENDING，否则协调器会重复提交同一步骤（工具被重复执行）。
        """
        if t.state not in TERMINAL:
            return
        if t.kind in ("task",):
            self._history.append(t.id)
        parent = self._tasks.get(t.parent) if t.parent else None
        if parent is not None:
            states_key = ("step_states"
                          if parent.action.get("kind") == "flow" else "item_states")
            states = parent.action.get(states_key) or {}
            st = states.get(t.step_key)
            if st is not None and st.get("state") == RUNNING:
                st["state"] = t.state
                st["result"] = _shrink(t.result) if t.state == SUCCEEDED else st.get("result")
                st["error"] = t.error
                st["attempts"] = t.attempts
            parent.wake.set()
            if parent.durable:
                self._maybe_save(parent)   # 先同步后落盘：断点精确到步骤
        elif t.durable:
            self._maybe_save(t)
        self._prune_memory()

    def _prune_memory(self) -> None:
        """内存里只留：活跃任务 + 最近历史；终态且不在历史窗口的移除。"""
        if len(self._tasks) <= DEFAULT_CFG["history_cap"] * 3:
            return
        keep = set(self._history)
        for t in self._tasks.values():
            if t.state not in TERMINAL or t.durable:
                keep.add(t.id)
        for tid in list(self._tasks):
            if tid not in keep:
                self._tasks.pop(tid, None)

    # ==================== 控制与查询 ====================

    def clear_finished(self) -> int:
        """批量清除所有终态任务（succeeded/failed/cancelled），返回清除数量。

        同时清理内存与持久化日志（harness_tasks.json），避免任务中心
        「清除已完成」后轮询刷新又恢复。
        """
        finished = [tid for tid, t in self._tasks.items()
                    if t.state in TERMINAL]
        for tid in finished:
            self._tasks.pop(tid, None)
        if finished:
            self._save_journal()
        return len(finished)

    def cancel(self, task_id: str) -> bool:
        t = self._tasks.get(str(task_id))
        if t is None or t.state in TERMINAL:
            return False
        if t.kind in ("flow", "batch"):
            # 取消所有子任务，协调器检测到 job 终态后自然退出
            for c in list(self._tasks.values()):
                if c.parent == t.id and c.state not in TERMINAL:
                    if c.state == RUNNING and c._exec_task:
                        c._exec_task.cancel()
                    else:
                        c._set_state(CANCELLED, "父任务已取消")
            t._set_state(CANCELLED, "已取消")
            t.status_text = "已取消"
            t.wake.set()
            if t.durable:
                self._save_journal()
            self._emit("task", f"任务 {t.id} 已取消")
            return True
        # 叶子：排队中直接置取消；运行中由 worker 持有的协程无法从这里直接拿到 —— 
        # 通过 _exec_task 引用取消（_run_leaf 会登记）
        if t.state == RUNNING and t._exec_task:
            t._exec_task.cancel()
            return True
        t._set_state(CANCELLED, "已取消")
        if t.parent:
            p = self._tasks.get(t.parent)
            if p:
                p.wake.set()
        return True

    def retry(self, task_id: str) -> bool:
        """重试终态任务：叶子重跑；流程/批量重置失败与取消的部分，保留成功步骤结果。"""
        t = self._tasks.get(str(task_id))
        if t is None or t.state not in TERMINAL:
            return False
        if t.kind in ("flow", "batch"):
            states_key = "step_states" if t.kind == "flow" else "item_states"
            for st in t.action.get(states_key, {}).values():
                if st["state"] in (FAILED, CANCELLED, "waiting_confirm"):
                    st["state"] = PENDING
                    st["error"] = ""
                    st["attempts"] = 0
            t.action.pop("confirming", None)
            t.action["confirmed"] = []      # 重试后危险步骤需重新确认
            t.action["reflections"] = 0     # 反思配额重置
            t.error = ""
            t._set_state(RUNNING)
            t.started_at = time.time()
            t.status_text = "重试中"
            self._ensure_coordinator_running(t)
            if t.durable:
                self._save_journal()
            self._emit("task", f"任务 {t.id} 已重试（保留已完成部分）")
            return True
        if t.action.get("kind") == "py":
            return False  # 进程内闭包不可复用
        t.attempts = 0
        t.error = ""
        self._enqueue(t)
        return True

    # ---- 确认门（危险步骤需用户批准） ----

    def approve_step(self, task_id: str, note: str = "") -> tuple:
        """批准当前等待确认的步骤。返回 (ok, message)。"""
        t = self._tasks.get(str(task_id))
        if t is None or t.kind != "flow":
            return False, "任务不存在"
        confirming = t.action.get("confirming") or {}
        if not confirming:
            return False, "没有等待确认的步骤"
        sid = next(iter(confirming))
        confirming.pop(sid)
        t.action.setdefault("confirmed", []).append(sid)
        st = t.action.get("step_states", {}).get(sid)
        if st is not None and st.get("state") == "waiting_confirm":
            st["state"] = PENDING
        t.wake.set()
        if t.durable:
            self._save_journal()
        self._emit("task", f"流程 {t.id} 步骤 {sid} 已获用户批准"
                   + (f"（{note[:80]}）" if note else ""))
        return True, f"已批准步骤 {sid}"

    def reject_step(self, task_id: str, note: str = "") -> tuple:
        """拒绝当前等待确认的步骤（该步骤取消，失败策略随之生效）。"""
        t = self._tasks.get(str(task_id))
        if t is None or t.kind != "flow":
            return False, "任务不存在"
        confirming = t.action.get("confirming") or {}
        if not confirming:
            return False, "没有等待确认的步骤"
        sid = next(iter(confirming))
        confirming.pop(sid)
        st = t.action.get("step_states", {}).get(sid)
        if st is not None and st.get("state") == "waiting_confirm":
            st["state"] = CANCELLED
            st["error"] = f"用户拒绝执行该步骤{(': ' + note[:120]) if note else ''}"
        t.wake.set()
        if t.durable:
            self._save_journal()
        self._emit("task", f"流程 {t.id} 步骤 {sid} 被用户拒绝")
        return True, f"已拒绝步骤 {sid}"

    def status(self, task_id: str) -> Optional[dict]:
        t = self._tasks.get(str(task_id))
        return t.to_dict() if t else None

    def list_tasks(self, state: Optional[str] = None, kind: Optional[str] = None,
                   limit: int = 20) -> list:
        out = []
        for t in sorted(self._tasks.values(),
                        key=lambda x: -(x.created_at)):
            if state and t.state != state:
                continue
            if kind and t.kind != kind:
                continue
            out.append(t.to_dict(brief=True))
            if len(out) >= limit:
                break
        return out

    def queue_stats(self) -> dict:
        stats = {}
        for name, q in self._queues.items():
            waiting = sum(1 for *_r, tid in q.heap
                          if (t := self._tasks.get(tid)) and t.state == PENDING)
            stats[name] = {
                "workers": q.workers, "paused": q.paused,
                "waiting": waiting, "running": len(q.running),
                "min_interval": q.min_interval,
            }
        return stats

    def pause_queue(self, name: str, paused: bool) -> bool:
        q = self._queues.get(str(name))
        if q is None:
            return False
        q.paused = bool(paused)
        q.wakeup.set()
        self._emit("task", f"队列 {name} 已{'暂停' if paused else '恢复'}")
        return True

    def summary(self) -> dict:
        """供 /api/harness/status 与管理页的任务总览。"""
        self.ensure_started()
        by_state: dict[str, int] = {}
        for t in self._tasks.values():
            if t.kind in ("flow", "batch", "task"):
                by_state[t.state] = by_state.get(t.state, 0) + 1
        jobs = [t.to_dict(brief=True) for t in self._tasks.values()
                if t.kind in ("flow", "batch")]
        jobs.sort(key=lambda x: (0 if x["state"] in (PENDING, RUNNING) else 1,
                                 -x["created_at"]))
        return {"queues": self.queue_stats(), "by_state": by_state,
                "jobs": jobs[:12]}

    # ==================== 持久化（断点续跑） ====================

    def _maybe_save(self, job: Task) -> None:
        # 大批量时降低落盘频率：每 8 条或终态必存
        if job.kind == "batch":
            done = sum(1 for v in job.action["item_states"].values()
                       if v["state"] in TERMINAL)
            if done % 8 != 0 and done != len(job.action["items"]):
                return
        self._save_journal()

    def _save_journal(self) -> None:
        if self._saving or self._stopping:
            return
        self._saving = True
        try:
            jobs = []
            for t in self._tasks.values():
                # 只持久化流程/批量（叶子步骤状态内嵌其中；进程内闭包不可恢复）
                if not t.durable or t.kind not in ("flow", "batch"):
                    continue
                rec = {"id": t.id, "name": t.name, "kind": t.kind, "queue": t.queue,
                       "priority": t.priority, "state": t.state,
                       "timeout": t.timeout, "max_attempts": t.max_attempts,
                       "created_at": t.created_at, "finished_at": t.finished_at,
                       "error": t.error, "action": _journal_action(t.action)}
                jobs.append(rec)
            # 只保留最近 journal_cap 条，且活跃的永不淘汰
            jobs.sort(key=lambda r: (0 if r["state"] not in TERMINAL else 1,
                                     -(r["created_at"] or 0)))
            jobs = jobs[: DEFAULT_CFG["journal_cap"]]
            tmp = str(self.journal) + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"version": 1, "saved_at": time.time(), "jobs": jobs},
                          f, ensure_ascii=False)
            os.replace(tmp, self.journal)
        except Exception as e:
            logger.warning("任务日志落盘失败: %s", e)
        finally:
            self._saving = False

    def _resume_persisted(self) -> None:
        if not self.journal.exists():
            return
        try:
            with open(self.journal, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            logger.warning("任务日志读取失败，忽略: %s", e)
            return
        resumed = 0
        for rec in (data or {}).get("jobs", []):
            try:
                action = rec.get("action") or {}
                kind = rec.get("kind")
                if kind not in ("flow", "batch") or action.get("kind") not in ("flow", "batch"):
                    continue  # py 闭包等不可恢复的跳过
                t = Task(rec["id"], rec.get("name") or "恢复任务", kind, action,
                         queue=rec.get("queue") or ("flows" if kind == "flow" else "batch"),
                         priority=int(rec.get("priority") or 5),
                         timeout=rec.get("timeout"), max_attempts=1, durable=True)
                t.created_at = rec.get("created_at") or time.time()
                t.error = rec.get("error") or ""
                prev = rec.get("state")
                self._tasks[t.id] = t
                if prev in TERMINAL:
                    t._set_state(prev)
                    t.finished_at = rec.get("finished_at")
                    # 终态但无 result 的从 step/item 状态重建摘要
                    t.result = _job_result_from_action(action)
                else:
                    self._start_coordinator(t)
                    resumed += 1
            except Exception as e:
                logger.warning("恢复任务 %s 失败: %s", rec.get("id"), e)
        if resumed:
            self._emit("task", f"已恢复 {resumed} 个未完成的持久化任务（断点续跑）")
            logger.info("[Tasks] 断点续跑：%d 个任务", resumed)

    # ==================== 事件 ====================

    def _emit(self, kind: str, message: str) -> None:
        try:
            if self.harness is not None:
                self.harness.record(kind, message)
        except Exception:
            pass


def _journal_action(action: dict) -> dict:
    """序列化动作（剔除协程等不可 JSON 化成员；step/item 状态原样保留）。"""
    if not isinstance(action, dict):
        return {}
    out = {}
    for k, v in action.items():
        if k == "coro" or callable(v):
            continue
        try:
            json.dumps(v, ensure_ascii=False)  # 探测可序列化性
            out[k] = v
        except Exception:
            out[k] = str(v)[:500]
    return out


def _job_result_from_action(action: dict) -> Any:
    """恢复终态任务时，从步骤/条目状态重建结果摘要。"""
    if "step_states" in action:
        return {"steps": {k: {"state": v.get("state"), "result": v.get("result"),
                              "error": v.get("error")}
                          for k, v in action["step_states"].items()}}
    if "item_states" in action:
        states = action["item_states"]
        items = action.get("items") or []
        failures = {k: v.get("error") for k, v in states.items() if v.get("state") == FAILED}
        return {"count": len(items), "ok": len(items) - len(failures),
                "failed": len(failures),
                "results": [states.get(str(i), {}).get("result") for i in range(len(items))],
                "failures": failures}
    return None
