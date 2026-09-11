"""声明式 Flow DSL —— 借鉴 crewAI 的 @start / @listen 装饰器。

把「大白」的多步骤流程从命令式（手写 DAG）升级为声明式：
在类里用装饰器标注方法，方法之间的触发关系由装饰器自动推导，
运行时用 EventBus 串联，同步/异步方法混用无压力。

用法示例：
    class 调研流程(Flow):
        @start
        def 开始(self):
            return "需求：研究 crewAI"

        @listen(开始)
        def 搜索(self, 需求):
            return f"已搜索：{需求}"

        @listen(搜索)
        def 总结(self, 结果):
            return f"总结：{结果}"

    flow = 调研流程()
    flow.kickoff()   # 自动按依赖链跑完，返回最终结果

设计原则（与 harness 一致）：
- 装饰器只是语法糖，真正跑的是收集到的 FlowMethodDefinition；
- 单个方法抛异常只记日志并中止该分支，不拖垮其它分支；
- 不依赖 TaskSystem，纯 EventBus 驱动，轻量可独立使用。
"""
from __future__ import annotations

import asyncio
import inspect
import logging
from typing import Any, Callable, Dict, List, Optional, Union

logger = logging.getLogger("harness.flow_dsl")
_FLOW_CLASSES: Dict[str, type] = {}   # 类名 -> Flow 子类（TaskSystem handler 重启恢复用）


def _make_flow_handler(cls_name: str, method_name: str) -> Callable:
    """生成可持久化 handler：按类名重建 Flow 实例并调用指定方法。

    同步方法直接返回值；异步方法返回 coroutine（TaskSystem 会 await）。
    """
    def handler(payload: Any = None, **kw) -> Any:
        cls = _FLOW_CLASSES.get(cls_name)
        if cls is None:
            raise RuntimeError(f"Flow 类未登记: {cls_name}")
        flow = cls()
        m = flow._methods.get(method_name)
        if m is None:
            raise RuntimeError(f"Flow 方法不存在: {cls_name}.{method_name}")
        args = [payload] if payload is not None else []
        return m.func(*args) if not m.is_async else m.func(*args)
    return handler


class FlowMethodDefinition:
    """一个被装饰器标注的方法：do=实际动作，start/listen=触发条件。"""

    def __init__(self, func: Callable, start: bool = False,
                 listen: Optional[List[str]] = None):
        self.func = func
        self.name = func.__name__
        self.is_start = start
        self.listen = list(listen or [])   # 监听的事件名（方法名或显式事件名）
        self.is_async = asyncio.iscoroutinefunction(func)


def start(func: Callable) -> Callable:
    """标记方法为流程起点（无前置依赖，kickoff 时自动触发）。"""
    func.__flow_start__ = True
    return func


def listen(*triggers: Union[Callable, str]) -> Callable:
    """标记方法监听一个或多个触发源（方法对象或事件名字符串）。"""
    def decorator(func: Callable) -> Callable:
        names = []
        for t in triggers:
            if isinstance(t, str):
                names.append(t)
            elif callable(t):
                names.append(t.__name__)
            else:
                raise TypeError(f"listen 触发源必须是方法或字符串，收到 {t!r}")
        func.__flow_listen__ = names
        return func
    return decorator


class _MiniBus:
    """flow_dsl 自带的微型事件总线（不依赖 harness.core，独立可用）。

    与 harness EventBus 语义一致：subscribe/emit，异步 handler 丢事件循环，
    单个 handler 异常只记日志不拖垮其它。
    """

    def __init__(self):
        self._handlers: Dict[str, List[Callable]] = {}

    def subscribe(self, event_type: str, handler: Callable) -> None:
        self._handlers.setdefault(event_type, []).append(handler)

    def emit(self, event_type: str, payload: Any = None) -> None:
        for handler in list(self._handlers.get(event_type, [])):
            try:
                res = handler(event_type, payload)
                if asyncio.iscoroutine(res):
                    try:
                        asyncio.get_running_loop().create_task(res)
                    except RuntimeError:
                        pass
            except Exception as e:
                logger.warning("事件 %s 的订阅者异常: %s", event_type, e)
class Flow:
    """声明式流程基类：收集 @start/@listen 方法，用 EventBus 串联执行。

    kickoff() 启动所有 start 方法；每个方法的结果以
    flow:<方法名> 事件广播，被 @listen 监听的方法自动接续。
    """

    def __init__(self):
        self._bus = _MiniBus()
        self._methods: Dict[str, FlowMethodDefinition] = {}
        self._results: Dict[str, Any] = {}
        self._collect()

    def _collect(self) -> None:
        """扫描类上所有被装饰器标注的方法，注册到事件总线。"""
        for name, member in inspect.getmembers(self, inspect.ismethod):
            if not hasattr(member, "__flow_start__") and \
               not hasattr(member, "__flow_listen__"):
                continue
            is_start = bool(getattr(member, "__flow_start__", False))
            listen_names = list(getattr(member, "__flow_listen__", []))
            self._methods[name] = FlowMethodDefinition(
                member, start=is_start, listen=listen_names)
            if is_start:
                self._bus.subscribe("flow:start", self._run_method)
            for ev in listen_names:
                self._bus.subscribe(f"flow:{ev}", self._run_method)

    async def _run_method(self, event_type: str, payload: Any) -> None:
        """事件处理器：找到对应方法并执行，结果再广播出去。"""
        ev_name = event_type.split(":", 1)[1] if ":" in event_type else event_type
        # start 事件触发所有 start 方法；普通事件按方法名精确匹配
        if ev_name == "start":
            targets = [m for m in self._methods.values() if m.is_start]
        else:
            targets = [m for m in self._methods.values() if ev_name in m.listen]
        for m in targets:
            try:
                args = self._collect_args(m, payload)
                res = m.func(*args) if not m.is_async else await m.func(*args)
                self._results[m.name] = res
                self._bus.emit(f"flow:{m.name}", res)
            except Exception as e:
                logger.warning("流程方法 %s 异常: %s", m.name, e)

    def _collect_args(self, m: FlowMethodDefinition, payload: Any) -> List[Any]:
        """按方法签名决定传参：无参方法不传，有参方法传上一个结果。"""
        sig = inspect.signature(m.func)
        params = [p for p in sig.parameters.values()
                  if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
        if not params:
            return []
        return [payload]

    def kickoff(self) -> Dict[str, Any]:
        """启动流程：触发所有 start 方法，返回各方法结果字典。

        同步入口：内部用 asyncio.run 驱动，异步方法也会被完整执行。
        若调用方已在事件循环中，请改用 kickoff_async。
        """
        asyncio.run(self._drive())
        return self._results

    async def _drive(self) -> None:
        """流程驱动协程：广播 start 事件，等待所有异步任务完成。"""
        self._bus.emit("flow:start", None)
        await asyncio.sleep(0)  # 给任务一点调度机会
        pending = [t for t in asyncio.all_tasks()
                   if t is not asyncio.current_task()]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    async def kickoff_async(self) -> Dict[str, Any]:
        """异步启动：在已有事件循环中调用，等待所有异步方法跑完。"""
        await self._drive()
        return self._results

    def results(self) -> Dict[str, Any]:
        """查看各方法执行结果。"""
        return dict(self._results)
    def __init_subclass__(cls, **kw):
        """子类定义时自动登记到 _FLOW_CLASSES（供 TaskSystem handler 重启恢复）。"""
        super().__init_subclass__(**kw)
        _FLOW_CLASSES[cls.__name__] = cls

    def submit_to_tasks(self, tasks, name: str = "", goal: str = "",
                        queue: str = "flows", durable: bool = True) -> str:
        """把本 Flow 提交给 TaskSystem 执行（断点续跑/失败反思/LLM 预算全生效）。

        每个方法注册为可持久化 handler（flow:<类名>:<方法名>），
        @listen 依赖自动转成步骤 deps，结果经 {{步骤id.result}} 模板传递。
        提交后由 TaskSystem 调度，重启后自动恢复未完成步骤。
        """
        cls_name = type(self).__name__
        steps = []
        for m in self._methods.values():
            handler_name = f"flow:{cls_name}:{m.name}"
            tasks.register_handler(handler_name, _make_flow_handler(cls_name, m.name))
            step = {"id": m.name, "action": "handler", "handler": handler_name, "args": {}}
            if m.is_start:
                step["deps"] = []
            else:
                step["deps"] = list(m.listen)
                if m.listen:
                    step["args"] = {"payload": f"{{{{{m.listen[0]}.result}}}}"}
            steps.append(step)
        return tasks.submit_flow(name or cls_name, steps, queue=queue,
                                 durable=durable, goal=goal or name or cls_name)
