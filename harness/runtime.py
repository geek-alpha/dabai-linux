"""Agent 监督运行时（AgentRuntime）—— 把「大白」的整条执行链路置于 harness 控制下。

harness 早期只管「扩展」（技能/插件）；本模块把控制面扩展到 Agent 本体：

- LLM 调用监督：所有 chat.completions.create 统一走 supervise_llm ——
  瞬时错误退避重试（复用 core.retry_async）、整体超时、调用计量、
  按提供方的熔断器（连续失败快速失败，冷却后半开探测自动恢复）；
- 工具执行监督：所有工具调用统一走 supervise_tool —— 超时、成功率/耗时
  计量、工具级熔断器（坏工具秒级让路，不拖垮对话轮）；
- 对话轮追踪：chat_stream 每一轮是一个 RunSpan（begin_run/end_run），
  在途运行、最近运行、耗时、轮数一目了然；
- 用量记账：record_usage 汇总 token 消耗（对话/游戏/记忆/决策分渠道）；
- 集中观测：snapshot() 输出全部健康数据，注入 /api/harness/status 与管理页。

设计原则（与 harness 一致）：监督层自身永远不能成为故障源——
本模块任何异常都被吞掉并降级为"直接执行"，绝不断大白的主流程。
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger("harness.runtime")

# 默认监督参数（settings.json 的 harness.runtime 段可覆盖）
DEFAULTS = {
    # LLM 调用：瞬时错误重试次数与退避基数（与 agent 原有行为一致）
    "llm_retries": 3,
    "llm_backoff": 1.0,
    # LLM 单次调用整体超时（秒）；流式调用包含整个流的建立阶段
    "llm_timeout": 180.0,
    # 工具调用默认超时（秒）；agent 的 TOOL_CALL_TIMEOUT 仍可按调用覆盖
    "tool_timeout": 30.0,
    # 熔断器：连续失败 N 次跳闸；冷却 M 秒后半开放一次探测
    "breaker_failures": 3,
    "breaker_cooldown": 60.0,
    # 并行执行同一轮多个工具调用的开关（高效模式）
    "parallel_tools": True,
}


class SupervisedBlockedError(RuntimeError):
    """熔断器开启导致的快速失败（拦截不是新失败样本，不再累计冷却）。"""


def _hit_rate(usage: dict) -> Optional[float]:
    """全渠道前缀缓存命中率（0-1）；无缓存口径数据时返回 None。"""
    hit = sum(u.get("cache_hit", 0) for u in usage.values())
    miss = sum(u.get("cache_miss", 0) for u in usage.values())
    if hit + miss <= 0:
        return None
    return round(hit / (hit + miss), 3)


class CircuitBreaker:
    """三态熔断器：closed（正常）→ open（快速失败）→ half-open（放行探测）。

    连续失败 failure_threshold 次跳闸；跳闸后在 cooldown 秒内直接拒绝调用，
    冷却结束进入半开态放行一次探测——成功即恢复，失败则重新计时。
    """

    def __init__(self, name: str, failure_threshold: int, cooldown: float):
        self.name = name
        self.failure_threshold = max(1, int(failure_threshold))
        self.cooldown = max(1.0, float(cooldown))
        self.failures = 0
        self.opened_at: Optional[float] = None  # 跳闸时刻；None = closed/half-open

    def allow(self) -> bool:
        """是否放行本次调用。open 态冷却结束自动转 half-open 并放行一次探测。"""
        if self.opened_at is None:
            return True
        if time.monotonic() - self.opened_at >= self.cooldown:
            self.opened_at = None  # half-open：放行探测
            return True
        return False

    def record(self, ok: bool) -> None:
        if ok:
            self.failures = 0
            self.opened_at = None
        else:
            self.failures += 1
            if self.failures >= self.failure_threshold:
                self.opened_at = time.monotonic()

    @property
    def state(self) -> str:
        if self.opened_at is None:
            return "closed" if self.failures == 0 else "degraded"
        if time.monotonic() - self.opened_at >= self.cooldown:
            return "half-open"
        return "open"

    def reset(self) -> None:
        self.failures = 0
        self.opened_at = None

    def info(self) -> dict:
        return {"name": self.name, "state": self.state,
                "failures": self.failures,
                "cooldown_left": (round(self.cooldown - (time.monotonic() - self.opened_at), 1)
                                  if self.opened_at is not None else 0)}


class _Meter:
    """单个调用点（LLM 渠道 / 工具）的累计计量。"""

    __slots__ = ("calls", "failures", "timeouts", "total_time", "max_time", "breaker")

    def __init__(self, breaker: CircuitBreaker):
        self.calls = 0
        self.failures = 0
        self.timeouts = 0
        self.total_time = 0.0
        self.max_time = 0.0
        self.breaker = breaker


class RunSpan:
    """一次对话轮（chat_stream 调用）的追踪上下文。"""

    __slots__ = ("run_id", "kind", "mode", "started_at", "rounds", "tool_calls", "ok")

    def __init__(self, run_id: str, kind: str, mode: str):
        self.run_id = run_id
        self.kind = kind          # chat / memory / decision ...
        self.mode = mode          # normal / simple ...
        self.started_at = time.monotonic()
        self.rounds = 0
        self.tool_calls = 0
        self.ok: Optional[bool] = None

    def info(self) -> dict:
        return {
            "run_id": self.run_id, "kind": self.kind, "mode": self.mode,
            "duration": round(time.monotonic() - self.started_at, 2),
            "rounds": self.rounds, "tool_calls": self.tool_calls,
            "ok": self.ok,
        }


class AgentRuntime:
    """大白 Agent 的监督运行时门面（挂在 Harness 上，全局单例）。"""

    def __init__(self, harness=None):
        self.harness = harness
        self._cfg = self._load_cfg()
        self._llm: dict[str, _Meter] = {}
        self._tools: dict[str, _Meter] = {}
        self._agents: dict[str, dict] = {}          # user_id -> {model, base_url, since}
        self._active_runs: dict[str, RunSpan] = {}  # run_id -> span
        self._recent_runs: deque = deque(maxlen=40)
        self._usage: dict[str, dict] = {}           # kind -> {prompt, completion, total}
        self._run_seq = 0

    # ---------- 配置 ----------

    def _load_cfg(self) -> dict:
        cfg = dict(DEFAULTS)
        try:
            import json
            from pathlib import Path
            base = Path(__file__).resolve().parent.parent
            p = base / "settings.json"
            if p.exists():
                with open(p, "r", encoding="utf-8") as f:
                    sc = json.load(f)
                rc = ((sc or {}).get("harness", {}) or {}).get("runtime", {}) or {}
                for k in DEFAULTS:
                    if k in rc:
                        cfg[k] = type(DEFAULTS[k])(rc[k])
        except Exception:
            pass
        return cfg

    def reload_cfg(self) -> dict:
        """热重载后刷新配置（新熔断器按新阈值建，已有计量保留）。"""
        self._cfg = self._load_cfg()
        return self._cfg

    # ---------- 生命周期 ----------

    def register_agent(self, user_id: str = "default", model: str = "", base_url: str = "") -> None:
        self._agents[str(user_id)] = {
            "user_id": str(user_id),
            "model": model,
            "base_url": base_url,
            "since": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        self._emit("runtime", f"Agent 已注册监督: {user_id} (model={model or '?'})")

    def unregister_agent(self, user_id: str = "default") -> None:
        self._agents.pop(str(user_id), None)

    # ---------- 对话轮追踪 ----------

    def begin_run(self, kind: str = "chat", mode: str = "normal") -> RunSpan:
        self._run_seq += 1
        span = RunSpan(f"run-{int(time.time())}-{self._run_seq}", kind, mode)
        self._active_runs[span.run_id] = span
        return span

    def end_run(self, span: Optional[RunSpan], ok: bool = True) -> None:
        if span is None:
            return
        span.ok = ok
        self._active_runs.pop(span.run_id, None)
        self._recent_runs.append(span.info())

    # ---------- LLM 调用监督 ----------

    def _llm_meter(self, kind: str) -> _Meter:
        m = self._llm.get(kind)
        if m is None:
            m = _Meter(CircuitBreaker(f"llm:{kind}",
                                      self._cfg["breaker_failures"],
                                      self._cfg["breaker_cooldown"]))
            self._llm[kind] = m
        return m

    async def supervise_llm(self, kind: str, factory: Callable[[], Awaitable[Any]],
                            timeout: Optional[float] = None) -> Any:
        """监督一次 LLM 调用：熔断 → 重试（瞬时错误）→ 超时 → 计量。

        kind 用于分渠道计量：chat / game / decision / character_line / memory ...
        熔断拦截抛 SupervisedBlockedError；真实失败原样抛出。
        """
        meter = self._llm_meter(str(kind))
        if not meter.breaker.allow():
            msg = (f"LLM 渠道 {kind} 已熔断（连续失败 {meter.breaker.failures} 次，"
                   f"约 {meter.breaker.info()['cooldown_left']}s 后自动恢复探测）")
            self._emit("error", msg)
            raise SupervisedBlockedError(msg)
        effective_timeout = float(timeout if timeout is not None else self._cfg["llm_timeout"])
        start = time.monotonic()
        try:
            from .core import retry_async
            result = await asyncio.wait_for(
                retry_async(factory,
                            attempts=int(self._cfg["llm_retries"]),
                            backoff=float(self._cfg["llm_backoff"])),
                timeout=effective_timeout,
            )
        except TimeoutError:
            meter.timeouts += 1
            meter.failures += 1
            meter.breaker.record(False)
            self._emit("error", f"LLM 渠道 {kind} 调用超时（>{effective_timeout}s）")
            raise
        except SupervisedBlockedError:
            raise
        except Exception as e:
            meter.failures += 1
            meter.breaker.record(False)
            self._emit("error", f"LLM 渠道 {kind} 调用失败: {e}")
            raise
        finally:
            elapsed = time.monotonic() - start
            meter.calls += 1
            meter.total_time += elapsed
            meter.max_time = max(meter.max_time, elapsed)
        meter.breaker.record(True)
        return result

    def record_usage(self, kind: str, prompt: int, completion: int, total: int,
                     cache_hit: int = 0, cache_miss: int = 0) -> None:
        """token 用量记账（按渠道累计，含前缀缓存命中口径，快照给管理页）。"""
        try:
            u = self._usage.setdefault(str(kind), {"prompt": 0, "completion": 0, "total": 0,
                                                    "calls": 0, "cache_hit": 0, "cache_miss": 0})
            u["prompt"] += int(prompt or 0)
            u["completion"] += int(completion or 0)
            u["total"] += int(total or 0)
            u["calls"] += 1
            u["cache_hit"] += int(cache_hit or 0)
            u["cache_miss"] += int(cache_miss or 0)
        except Exception:
            pass

    # ---------- 工具执行监督 ----------

    def _tool_meter(self, tool_name: str) -> _Meter:
        m = self._tools.get(tool_name)
        if m is None:
            m = _Meter(CircuitBreaker(f"tool:{tool_name}",
                                      self._cfg["breaker_failures"],
                                      self._cfg["breaker_cooldown"]))
            self._tools[tool_name] = m
        return m

    async def supervise_tool(self, tool_name: str, factory: Callable[[], Awaitable[str]],
                             timeout: Optional[float] = None) -> tuple:
        """监督一次工具执行：熔断 → 超时 → 计量。

        返回 (result_text, ok)：失败/超时/熔断不抛异常，
        以错误文案 + ok=False 返回（与 agent 原有错误协议一致）。"""
        meter = self._tool_meter(str(tool_name))
        if not meter.breaker.allow():
            msg = (f"工具 '{tool_name}' 已熔断（连续失败 {meter.breaker.failures} 次，"
                   f"约 {meter.breaker.info()['cooldown_left']}s 后自动恢复）")
            self._emit("error", msg)
            return msg, False
        start = time.monotonic()
        try:
            result = await asyncio.wait_for(
                factory(), timeout=float(timeout if timeout is not None else self._cfg["tool_timeout"]))
            meter.breaker.record(True)
            return result, True
        except TimeoutError:
            meter.timeouts += 1
            meter.failures += 1
            meter.breaker.record(False)
            msg = (f"工具 '{tool_name}' 执行超时（>{timeout or self._cfg['tool_timeout']}s）。"
                   "请勿原样重试同一命令：先检查原因（路径/网络/权限/是否真的需要这么久），"
                   "换参数、换工具，或把长任务交给后台任务系统，避免重复堆积执行。")
            self._emit("error", msg)
            return msg, False
        except Exception as e:
            meter.failures += 1
            meter.breaker.record(False)
            msg = f"工具 '{tool_name}' 执行失败: {e}"
            self._emit("error", msg)
            return msg, False
        finally:
            elapsed = time.monotonic() - start
            meter.calls += 1
            meter.total_time += elapsed
            meter.max_time = max(meter.max_time, elapsed)

    @property
    def parallel_tools(self) -> bool:
        return bool(self._cfg.get("parallel_tools", True))

    # ---------- 观测 / 管理 ----------

    def reset_breaker(self, name: str) -> bool:
        """手动复位熔断器（name 为 llm:chat / tool:xxx）。"""
        target = name.split(":", 1)
        if len(target) != 2:
            return False
        kind, key = target
        meter = (self._llm if kind == "llm" else self._tools).get(key)
        if meter is None:
            return False
        meter.breaker.reset()
        self._emit("runtime", f"熔断器 {name} 已手动复位")
        return True

    def snapshot(self) -> dict:
        """运行时健康快照（/api/harness/status 与管理页数据源）。"""
        def _meters(d: dict) -> dict:
            out = {}
            for k, m in d.items():
                out[k] = {
                    "calls": m.calls, "failures": m.failures, "timeouts": m.timeouts,
                    "avg_ms": round(m.total_time / m.calls * 1000, 1) if m.calls else 0,
                    "max_ms": round(m.max_time * 1000, 1),
                    "breaker": m.breaker.info(),
                }
            return out
        return {
            "agents": list(self._agents.values()),
            "llm": _meters(self._llm),
            "tools": _meters(self._tools),
            "usage": self._usage,
            "usage_total": {
                "prompt": sum(u["prompt"] for u in self._usage.values()),
                "completion": sum(u["completion"] for u in self._usage.values()),
                "total": sum(u["total"] for u in self._usage.values()),
                "cache_hit": sum(u.get("cache_hit", 0) for u in self._usage.values()),
                "cache_miss": sum(u.get("cache_miss", 0) for u in self._usage.values()),
            },
            "cache_hit_rate": _hit_rate(self._usage),
            "active_runs": [s.info() for s in self._active_runs.values()],
            "recent_runs": list(self._recent_runs)[-15:],
            "config": self._cfg,
        }

    def _emit(self, kind: str, message: str) -> None:
        """事件写入 harness 健康日志（无 harness 时静默）。"""
        try:
            if self.harness is not None:
                self.harness.record(kind, message)
        except Exception:
            pass
