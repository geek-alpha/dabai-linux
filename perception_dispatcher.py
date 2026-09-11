"""统一感知路由调度器 —— 集中管理大厅感知事件的触发与分发。

将原本分散在 server.py WebSocket 消息循环中的：
- 感知消息的路由判断
- 冷却/防抖/保护逻辑
- trigger_text → LLM 调用决策
- behavior_cmd → 前端行为指令

统一到一个类中，方便管理、调试和扩展。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from enum import Enum
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    # dispatch() 的 autonomy 参数类型是 AutonomyHub。注解是字符串前向引用
    # （本文件有 from __future__ import annotations），运行时安全；
    # 但缺了这个导入，任何 get_type_hints() 调用都会 NameError。
    from ai_autonomy import AutonomyHub

logger = logging.getLogger("perception_dispatcher")


# ==================== 核心数据结构 ====================

class EventCategory(Enum):
    """感知事件类别"""
    PERIODIC = "periodic"            # 定期同步：位置/视野更新，综合触发
    BEHAVIOR_FEEDBACK = "behavior_feedback"  # 行为反馈：移动完成、到达POI
    SUMMON = "summon"                # 前端主动召唤


@dataclass
class DispatchResult:
    """统一调度结果 —— 告诉调用方应该执行什么操作。"""

    trigger_text: Optional[str] = None   # AI 说话触发文本（传给 LLM）
    behavior_cmd: Optional[dict] = None  # AI 行为指令（发给前端）
    should_speak: bool = False           # 是否启动 _kickoff_response
    category: Optional[EventCategory] = None  # 匹配的事件类别

    @property
    def has_any_action(self) -> bool:
        """是否有任何需要执行的动作"""
        return self.should_speak or self.behavior_cmd is not None


# ==================== 事件注册表 ====================

@dataclass
class EventRule:
    """单个事件的调度规则"""

    category: EventCategory
    should_speak: bool           # 是否触发 AI 说话
    needs_protection: bool       # 是否需要保护（AI说话时/冷却期内丢弃）
    needs_engine: bool           # 是否需要自主行为中枢实例
    cooldown_after_speak: float  # 说话后的冷却时间（秒）


# ── 事件注册表：消息 type → 调度规则 ──
# 新增事件只需在此添加一行即可，无需修改 server.py 消息循环。

EVENT_REGISTRY: dict[str, EventRule] = {
    # 定期状态同步（核心入口）→ 综合感知触发 + 行为决策
    "environment_snapshot": EventRule(
        category=EventCategory.PERIODIC,
        should_speak=True,
        needs_protection=True,
        needs_engine=True,
        cooldown_after_speak=30.0,
    ),
    # 行为反馈（移动完成 / 到达 POI）→ 记录 + 触发下一轮决策
    "ai_behavior_result": EventRule(
        category=EventCategory.BEHAVIOR_FEEDBACK,
        should_speak=True,
        needs_protection=True,
        needs_engine=True,
        cooldown_after_speak=3.0,
    ),
    # 前端主动召唤 → 不需要自主中枢（受保护：AI说话/回复冷却期内丢弃）
    "proactive": EventRule(
        category=EventCategory.SUMMON,
        should_speak=True,
        needs_protection=True,
        needs_engine=False,
        cooldown_after_speak=3.0,
    ),
}


# ==================== 调度器 ====================

class PerceptionDispatcher:
    """统一感知路由调度器。

    使用方式（在 server.py WebSocket 消息循环中）：

        result = dispatcher.dispatch(
            msg_type=mtype,
            msg=msg,                    # 完整的 WebSocket 消息 dict
            autonomy=state.autonomy,
            is_speaking=(state.active_task is not None and not state.active_task.done()),
            last_response_time=state.last_response_done,
            last_user_message_time=state.last_user_message_time,
            current_model=state.current_model,
            current_background=state.current_background,
            current_bgm=state.current_bgm,
        )
        if result.behavior_cmd:
            await safe_send_json(ws, result.behavior_cmd)
        if result.should_speak and result.trigger_text:
            await _kickoff_response(ws, result.trigger_text, history, state)
    """

    # ── 全局配置 ──
    USER_ENGAGED_WINDOW = 10.0   # 用户互动窗口（秒），用于判断 is_user_engaged
    USER_ACTIVE_GUARD = 60.0     # 用户活跃窗口（秒）：窗口内大厅自动行为不主动说话

    def __init__(self):
        self._history: list[dict] = []  # 最近调度记录（调试用）
        self._max_history = 50
        self._speak_skipped_global = 0   # 被全局主动闸门拦截的次数（统计）
        self._behavior_deg_applied = 0   # 冷落行为降级应用次数（统计）

    # ── 冷落行为降级 ──

    def _cold_shoulder_deg(self, rule: EventRule,
                           last_user_message_time: float) -> str:
        """冷落分段 → 行为降级档位（修复3：本地实时计算，不依赖心跳）。

        仅对自主行为类事件（BEHAVIOR_FEEDBACK / PERIODIC）生效：
        - fresh_activity（用户 10 分钟内互动）→ "normal" 恢复正常自主
        - away（5min-2h 未互动）→ "calm" 焦虑踱步（长时间乱走，象征焦虑/无聊）
        - gone（>2h 未互动）→ "freeze" 低频长时踱步（不主动说话）
        """
        if rule.category not in (EventCategory.BEHAVIOR_FEEDBACK,
                                 EventCategory.PERIODIC):
            return "normal"
        if last_user_message_time <= 0:
            return "normal"
        sec = time.time() - last_user_message_time
        if sec < 300:
            return "normal"
        if sec < 7200:
            return "calm"
        return "freeze"

    # ── 主入口 ──

    def dispatch(
        self,
        msg_type: str,
        msg: dict,
        autonomy: Optional["AutonomyHub"] = None,
        is_speaking: bool = False,
        last_response_time: float = 0,
        last_user_message_time: float = 0,
        current_model: Optional[str] = None,
        current_background: Optional[str] = None,
        current_bgm: Optional[str] = None,
        last_active_speak: float = 0,
        active_speak_cooldown: float = 0,
    ) -> DispatchResult:
        """统一调度入口。

        Args:
            msg_type: 消息类型（如 "environment_snapshot" 等）
            msg: 完整的 WebSocket 消息 dict
            autonomy: AutonomyHub 实例（proactive 以外都需要）
            is_speaking: AI 当前是否正在说话
            last_response_time: 上次回复完成的时间戳
            last_user_message_time: 用户最后发消息的时间戳
            current_model/background/bgm: 当前状态（proactive 用）
            last_active_speak: 上次全局主动说话的时间戳
            active_speak_cooldown: 全局主动说话冷却（秒）

        Returns:
            DispatchResult: 包含 trigger_text, behavior_cmd, should_speak 等
        """
        rule = EVENT_REGISTRY.get(msg_type)
        if rule is None:
            return DispatchResult()

        # ── 0. 前置全局主动说话闸门（廉价门）──
        # 主动类事件（定期快照/行为反馈/前端召唤）在全局主动说话冷却期内
        # 抑制说话。PERIODIC/SUMMON 直接丢弃（不执行决策链）；
        # BEHAVIOR_FEEDBACK 保留移动闭环（behavior_cmd 继续返回），
        # 仅抑制说话（AI 移动是后端驱动闭环，不能断）。
        gate_blocked = self._global_gate_blocked(
            rule, last_active_speak, active_speak_cooldown)
        if gate_blocked and rule.category != EventCategory.BEHAVIOR_FEEDBACK:
            return DispatchResult()

        # ── 0.5 冷落行为降级（RL 级联：被冷落时降低行为触发）──
        # 冷落分段实时计算（修复3：不依赖心跳往返，用本地时间戳直接判定）：
        #   away（5min-2h）→ 焦虑踱步模式（长时间乱走，象征焦虑/无聊）
        #   gone（>2h）    → 低频长时踱步（保持基本 idle，不主动说话）
        # 用户主动互动（fresh_activity）时解除降级，恢复正常自主。
        # 对自主行为类事件（行为反馈/定期快照）都同步档位到行为引擎，
        # 让大厅 AI 的"无聊 → 长时间乱走"由 RL 档位驱动。
        behavior_deg = self._cold_shoulder_deg(
            rule, last_user_message_time)
        if rule.category in (EventCategory.BEHAVIOR_FEEDBACK,
                             EventCategory.PERIODIC):
            engine_deg = getattr(autonomy, "set_behavior_degree", None)
            if engine_deg is not None:
                engine_deg(behavior_deg)
                self._behavior_deg_applied += 1

        # ── 1. 保护检查 ──
        if not self._passes_protection(rule, is_speaking, last_response_time):
            return DispatchResult()

        # ── 2. 路由到具体处理器 ──
        result = self._route(msg_type, msg, rule, autonomy,
                             last_user_message_time,
                             current_model, current_background, current_bgm)

        # ── 2.5 全局闸门抑制说话（BEHAVIOR_FEEDBACK 冷却期仍返回移动指令）──
        if gate_blocked and result.should_speak:
            result.should_speak = False
            result.trigger_text = None

        # ── 2.6 用户活跃窗口守卫（大厅自动闲聊抑制）──
        # 用户 1 分钟内有主动对话/回复 → 大厅环境快照/行为反馈的"自言自语"
        # 一律只保留行为指令（AI 仍可自主走动），抑制说话，避免"一直废话"。
        # 前端召唤属于外部触发，不受此限制（由 _apply_dispatch_result 的
        # external 判定放行）。
        if (result.should_speak and result.trigger_text
                and time.time() - last_user_message_time < self.USER_ACTIVE_GUARD
                and msg_type in ("environment_snapshot", "ai_behavior_result")):
            result.should_speak = False
            result.trigger_text = None

        # ── 3. 记录调度历史 ──
        self._record(msg_type, rule.category, result)

        return result

    # ── 保护检查 ──

    # 适用全局主动闸门的事件类别（非用户消息驱动的主动说话）
    _GLOBAL_GATE_CATEGORIES = {
        EventCategory.PERIODIC,
        EventCategory.BEHAVIOR_FEEDBACK,
        EventCategory.SUMMON,
    }

    def _global_gate_blocked(self, rule: EventRule,
                             last_active_speak: float,
                             active_speak_cooldown: float) -> bool:
        """前置全局主动说话闸门判断（廉价门）。

        主动类事件（定期快照 / 行为反馈 / 前端召唤）共用全局主动说话冷却
        （与 _kickoff_response 的 proactive 闸门同一冷却源）。冷却期内
        返回 True —— 调用方据此抑制说话（BEHAVIOR_FEEDBACK 保留移动闭环）
        或整链丢弃（PERIODIC / SUMMON）。
        """
        if rule.category not in self._GLOBAL_GATE_CATEGORIES:
            return False
        if active_speak_cooldown <= 0 or last_active_speak <= 0:
            return False  # 未启用冷却或从未主动说过 → 放行
        if time.time() - last_active_speak < active_speak_cooldown:
            self._speak_skipped_global += 1
            logger.debug(
                f"[调度器] 全局主动闸门拦截 {rule.category.value}: "
                f"冷却中 ({time.time() - last_active_speak:.0f}s)"
            )
            return True
        return False

    def _passes_protection(self, rule: EventRule, is_speaking: bool,
                           last_response_time: float) -> bool:
        """检查事件是否通过保护门。

        - AI 正在说话 → 被动事件丢弃
        - AI 刚说完在冷却期内 → 被动事件丢弃
        """
        if not rule.needs_protection:
            return True

        if is_speaking:
            logger.debug(f"[调度器] 跳过 {rule.category.value}: AI 正在说话")
            return False

        if last_response_time > 0:
            elapsed = time.time() - last_response_time
            if elapsed < rule.cooldown_after_speak:
                logger.debug(
                    f"[调度器] 跳过 {rule.category.value}: "
                    f"冷却中 ({elapsed:.1f}s < {rule.cooldown_after_speak}s)"
                )
                return False

        return True

    # ── 路由分发 ──

    def _route(self, msg_type, msg, rule, autonomy, last_user_msg_time,
               model, bg, bgm):
        """根据事件类型路由到对应处理器"""
        if rule.category == EventCategory.SUMMON:
            return self._handle_proactive(model, bg, bgm, autonomy)

        if not rule.needs_engine or autonomy is None:
            return DispatchResult()

        if rule.category == EventCategory.PERIODIC:
            user_engaged = time.time() - last_user_msg_time < self.USER_ENGAGED_WINDOW
            return self._handle_periodic(msg, autonomy, user_engaged)
        elif rule.category == EventCategory.BEHAVIOR_FEEDBACK:
            return self._handle_behavior_feedback(msg, autonomy)

        return DispatchResult()

    # ── 各事件处理器 ──

    def _handle_periodic(self, msg, autonomy, user_engaged):
        """定期同步：综合旧感知触发 + 新行为决策"""
        data = msg.get("data", {})
        trigger_text, behavior_cmd = autonomy.handle_autonomy_update(
            data, user_engaged=user_engaged,
        )

        result = DispatchResult(category=EventCategory.PERIODIC)
        if behavior_cmd:
            result.behavior_cmd = behavior_cmd
        if trigger_text:
            result.trigger_text = trigger_text
            result.should_speak = True
        return result

    def _handle_behavior_feedback(self, msg, autonomy):
        """行为反馈：移动完成 / 到达 POI → 记录 + 触发下一轮决策"""
        data = msg.get("data", {})
        event = data.get("event", "")
        poi_id = data.get("poi_id", "")

        if event == "reached_poi":
            autonomy.record_ai_exploration(poi_id)

        trigger_text, next_cmd = autonomy.handle_autonomy_update({}, user_engaged=False)

        result = DispatchResult(category=EventCategory.BEHAVIOR_FEEDBACK)
        if next_cmd:
            result.behavior_cmd = next_cmd
        if trigger_text:
            result.trigger_text = trigger_text
            result.should_speak = True
        return result

    def _handle_proactive(self, model, bg, bgm, autonomy=None):
        """前端主动召唤 AI 说话。"""
        parts = ["用户暂时没有说话"]
        if model:
            parts.append(f"，你当前的造型是 {model}")
        if bg:
            parts.append(f"，身处 {bg}")
        if bgm:
            parts.append(f"，正在播放背景音乐 {bgm}")
        # 用户（摄像机）位置 —— AI 对用户实际位置的空间参考
        if autonomy is not None:
            user_desc = autonomy.get_user_spatial_desc()
            if user_desc:
                parts.append(f"，{user_desc}")
        parts.append(
            "。你可以基于当前形象和场景主动说点什么、做个小动作，"
            "或者自然切换到新话题。保持简短自然，不要询问用户是否在线。"
        )
        return DispatchResult(
            trigger_text="".join(parts),
            should_speak=True,
            category=EventCategory.SUMMON,
        )

    # ── 工具方法 ──

    def _record(self, msg_type, category, result):
        """记录调度历史（用于调试）"""
        entry = {
            "time": time.time(),
            "type": msg_type,
            "category": category.value if category else "unknown",
            "speak": result.should_speak,
            "move": result.behavior_cmd is not None,
            "trigger": result.trigger_text[:80] if result.trigger_text else None,
        }
        self._history.append(entry)
        if len(self._history) > self._max_history:
            self._history = self._history[-self._max_history:]

    # ── 管理接口 ──

    def get_registered_events(self) -> list[str]:
        """获取所有已注册的事件类型"""
        return list(EVENT_REGISTRY.keys())

    def get_rule(self, event_type: str) -> Optional[EventRule]:
        """查询某个事件类型的调度规则"""
        return EVENT_REGISTRY.get(event_type)

    def update_rule(self, event_type: str, **kwargs):
        """动态更新某个事件的调度规则（热更新用）"""
        rule = EVENT_REGISTRY.get(event_type)
        if rule:
            for key, value in kwargs.items():
                if hasattr(rule, key):
                    setattr(rule, key, value)
            logger.info(f"[调度器] 更新规则 {event_type}: {kwargs}")

    def get_recent_history(self, n: int = 20) -> list[dict]:
        """获取最近 N 条调度记录"""
        return self._history[-n:]
