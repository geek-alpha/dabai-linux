"""AI 行为决策引擎 —— 决定 AI 自主行为。

新好奇心模型：
- 好奇心是兴奋累积：互动涨、发现涨、沟通涨
- 只有探索完一个 POI 才降 1/4
- 每个 POI 有独立的好奇心分数
- 某个 POI 好奇心超标 → 立刻不管不顾地扑过去（可打断当前聊天）

决策优先级：
1. 有"立刻行动"POI → 不管当前在干嘛，先扑过去（有冷却防抖）
2. 好奇心高涨 + 有 POI → 启动探索链
3. 好奇心中等 → 单次探索
4. 平静 → 漫步 / 小动作
5. 无 → 小动作

注：对话不再阻塞AI移动决策——AI可以边走边聊，移动持续进行。
"""

from __future__ import annotations

import json
import logging
import math
import time
import random
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Optional

from ai_perception_engine import AIPerceptionEngine, PointOfInterest

logger = logging.getLogger("ai_behavior")


# ==================== 行为类型 ====================

class BehaviorType(Enum):
    IMMEDIATE_ACTION = auto()   # 立刻扑过去（打断当前行为）
    CONTINUE_CHAT = auto()      # 继续聊天
    IDLE_ACTION = auto()        # 小动作
    WANDER = auto()             # 随机漫步
    GO_TO_POI = auto()          # 前往兴趣点
    EXPLORE_CHAIN = auto()      # 探索链模式
    ANXIOUS_WANDER = auto()     # 焦虑踱步（被冷落太久，长时间乱走，象征焦虑/无聊）


@dataclass
class BehaviorDecision:
    behavior: BehaviorType
    reason: str = ""
    target_poi: Optional[PointOfInterest] = None
    target_x: float = 0
    target_z: float = 0
    target_label: str = ""

    chain_targets: list[dict] = field(default_factory=list)
    wander_angle: float = 0
    wander_distance: float = 2.0
    waypoints: list[dict] = field(default_factory=list)  # 踱步路点序列（绝对坐标，绕 AI 位置生成）
    pacing: bool = False                                 # 是否焦虑踱步模式（长时间乱走）
    speak_text: str = ""
    speak_about_poi: bool = False
    confidence: float = 0.5
    timestamp: float = field(default_factory=time.time)


@dataclass
class DecisionContext:
    user_is_speaking: bool = False
    user_last_message_time: float = 0
    user_engaged: bool = False
    ai_is_moving: bool = False
    ai_idle_time: float = 0
    user_controlling: bool = False
    user_known: bool = False        # 是否掌握用户（摄像机）位置
    user_distance: float = 999      # 用户与 AI 的直线距离（米）
    user_direction: str = ""        # 用户相对 AI 朝向的方位（如"右前方"）
    has_pois: bool = False
    best_poi_attractiveness: float = 0    # 最佳物品吸引力评分
    poi_count: int = 0
    curiosity_level: float = 0.3
    chain_active: bool = False


class AIBehaviorEngine:
    """AI 行为决策引擎。

    集成游戏策略系统：根据当前游戏类型分发到专属策略，
    策略返回 None 时回退到默认好奇心驱动的探索逻辑。
    """

    def __init__(self, perception: AIPerceptionEngine):
        self.perception = perception
        self._decision_history: list[BehaviorDecision] = []
        self._last_decision_time: float = 0

        self.MIN_DECISION_INTERVAL = 6.0
        self.MOVE_COOLDOWN = 8.0
        self.SPEAK_COOLDOWN = 5.0
        self._last_move_time: float = 0
        self._last_speak_time: float = 0

        self._consecutive_wanders: int = 0
        self._consecutive_idles: int = 0
        self._last_glance_time: float = 0   # 上次好奇张望时间
        self.MAX_CONSECUTIVE_WANDERS = 2    # 正常模式下允许连续多次漫步（更活泼）

        # 焦虑踱步（被冷落太久时长时间乱走，象征焦虑与无聊）
        self.ANXIOUS_MOVE_COOLDOWN = 8.0    # calm：踱步间隔 ~8s（几乎停不下来）
        self.FREEZE_WANDER_COOLDOWN = 40.0  # freeze：低频但单次踱步时长很长

        # 冷落行为降级档位（RL 级联，由 AutonomyHub.set_behavior_degree 设置）
        # "normal" 正常自主 / "calm" 降频 / "freeze" 冻结移动
        self.degree: str = "normal"

        # 探索链
        self._exploration_chain: list[str] = []
        self._chain_exclude: set = set()
        self._chain_visit_count: int = 0
        self.MAX_CHAIN_VISITS = 4

    def set_degree(self, degree: str):
        """冷落行为降级档位（RL 级联）：被用户冷落时降低自主行为活跃度。"""
        if degree not in ("normal", "calm", "freeze"):
            return
        self.degree = degree

    # ==================== 核心决策 ====================

    def decide(self, context: DecisionContext) -> Optional[BehaviorDecision]:
        """核心决策入口。

        优先级：
        1. 冷落降级门控（RL 级联：被用户冷落时降低自主行为活跃度）
        2. 好奇心驱动决策（探索/漫步/小动作）
        """
        now = time.time()
        # 冷落降级（RL 级联）：被冷落太久 → 焦虑踱步。
        # 踱步由 RL 驱动的多路点长时间乱走构成（象征焦虑与无聊），
        # 幅度/速度恒定，全程平滑插值、绝不瞬移；
        # 踱步不产生任何说话，避免打扰。freeze 踱步更稀疏但单次时长更长。
        if self.degree in ("freeze", "calm"):
            self._consecutive_wanders = 0
            decision = self._decide_anxious_wander()
            if decision:
                self._record(decision)
                return decision
            # 踱步冷却中 → 保持基本 idle（不主动移动/说话）
            if self._consecutive_idles >= 3:
                return None
            decision = self._decide_idle()
            if decision:
                self._record(decision)
                return decision
            return None
        min_interval = self.MIN_DECISION_INTERVAL
        if now - self._last_decision_time < min_interval:
            return None
        self._last_decision_time = now

        # ===== 好奇心驱动决策 =====

        # 规则 1：探索链继续
        if context.chain_active or self._chain_visit_count > 0:
            decision = self._decide_chain(context)
            if decision:
                self._record(decision)
                return decision

        # 规则 2：好奇心高 → 启动探索链
        if self.perception.curiosity.should_chain_explore():
            if context.has_pois and context.best_poi_attractiveness >= 0.35:
                decision = self._decide_start_chain(context)
                if decision:
                    self._record(decision)
                    return decision

        # 规则 3：好奇心中等 → 单次探索
        if self.perception.curiosity.should_explore():
            if context.has_pois and context.best_poi_attractiveness >= 0.25:
                decision = self._decide_explore(context)
                if decision:
                    self._record(decision)
                    return decision

        # 规则 3.5：好奇张望（用户在场时，先好奇地看看用户在做什么）
        # —— 比起远处的 POI，更好奇"用户在干什么"（语气按当前角色人设自然演绎）
        if context.user_engaged and self.perception.curiosity.should_explore():
            decision = self._decide_curious_glance()
            if decision:
                self._record(decision)
                return decision

        # 规则 3.6：掌握用户实际位置且距离较远 → 走向用户
        # —— 快照中的 user（摄像机）位置给了 AI 真实的空间参考：
        #    用户（FPV 走动时）离远了，就主动走过去陪在对方身边
        if (context.user_known and context.user_distance > 4.0
                and self.perception.curiosity.should_explore()):
            decision = self._decide_go_to_user(context)
            if decision:
                self._record(decision)
                return decision

        # ===== 规则 4：漫步 =====
        if self.perception.curiosity.should_wander():
            decision = self._decide_wander(context)
            if decision:
                self._record(decision)
                return decision

        # ===== 规则 5：小动作 =====
        if self._consecutive_idles < 3:
            decision = self._decide_idle()
            if decision:
                self._record(decision)
                return decision

        return None

    # ==================== 立即行动（在 snapshot 处理时调用，可打断）====================

    def check_immediate_action(self) -> Optional[BehaviorDecision]:
        """检查是否有需要立刻行动的事项。

        优先级：好奇心驱动的立即行动（POI 吸引力超标）
        """
        poi = self.perception.check_immediate_action()
        if not poi:
            return None

        if time.time() - self._last_move_time < 3.0:
            return None  # 刚移动完，稍等

        self._last_move_time = time.time()
        self._consecutive_wanders = 0

        return BehaviorDecision(
            behavior=BehaviorType.IMMEDIATE_ACTION,
            reason=f"立刻扑向: {poi.label} (吸引{poi.total_score:.2f})",
            target_poi=poi,
            target_x=poi.x, target_z=poi.z,
            target_label=poi.label,
            speak_text=f"（你的好奇心被完全点燃了！{poi.direction}有个{poi.label}，你完全无法抗拒。立刻表达兴奋并冲过去。）",
            speak_about_poi=True,
            confidence=0.95,
        )

    # ==================== 决策方法 ====================

    def _decide_start_chain(self, context: DecisionContext) -> Optional[BehaviorDecision]:
        chain = self.perception.get_exploration_chain(length=3)
        if not chain:
            return None

        self._chain_exclude = set()
        self._chain_visit_count = 0
        self._exploration_chain = []
        self._last_move_time = 0

        first = chain[0]
        return BehaviorDecision(
            behavior=BehaviorType.EXPLORE_CHAIN,
            reason=f"启动探索链: {first.label} + {len(chain) - 1}个后续目标",
            target_poi=first,
            target_x=first.x, target_z=first.z,
            target_label=first.label,
            chain_targets=[{"x": p.x, "z": p.z, "label": p.label, "id": p.id} for p in chain[1:]],
            speak_text=f"（你的好奇心越来越高。你对{first.direction}的{first.label}产生了浓厚兴趣，决定过去一探究竟。表达你的好奇并开始移动。）",
            speak_about_poi=True,
            confidence=0.75,
        )

    def _decide_chain(self, context: DecisionContext) -> Optional[BehaviorDecision]:
        if time.time() - self._last_move_time < self.MOVE_COOLDOWN:
            return None

        next_poi = self.perception.get_next_in_chain(self._chain_exclude)
        if not next_poi:
            self._reset_chain()
            return None

        if next_poi.distance < 1.5:
            self._chain_exclude.add(next_poi.id)
            self._chain_visit_count += 1
            return self._decide_chain(context)

        self._chain_visit_count += 1
        self._last_move_time = time.time()

        if self._chain_visit_count >= self.MAX_CHAIN_VISITS:
            self._reset_chain()
            return None

        return BehaviorDecision(
            behavior=BehaviorType.GO_TO_POI,
            reason=f"探索链 #{self._chain_visit_count}: {next_poi.label}",
            target_poi=next_poi,
            target_x=next_poi.x, target_z=next_poi.z,
            target_label=next_poi.label,
            speak_text=f"（探索完上一个，你的好奇心引着你来到{next_poi.label}跟前。对它发表看法。）",
            speak_about_poi=True,
            confidence=0.6,
        )

    def _decide_explore(self, context: DecisionContext) -> Optional[BehaviorDecision]:
        if time.time() - self._last_move_time < self.MOVE_COOLDOWN:
            return None

        best = self.perception.get_best_poi()
        if not best or best.distance > 25:
            return self._decide_wander(context)

        self._last_move_time = time.time()
        self._consecutive_wanders = 0

        return BehaviorDecision(
            behavior=BehaviorType.GO_TO_POI,
            reason=f"探索: {best.label} (吸引{best.total_score:.3f})",
            target_poi=best,
            target_x=best.x, target_z=best.z,
            target_label=best.label,
            speak_text=f"（你对{best.direction}约{best.distance:.0f}米处的{best.label}产生了一点好奇。用一句话表达，然后走过去。）",
            speak_about_poi=True,
            confidence=0.65,
        )

    def _decide_wander(self, context: DecisionContext) -> Optional[BehaviorDecision]:
        if time.time() - self._last_move_time < self.MOVE_COOLDOWN:
            return None
        if self._consecutive_wanders >= self.MAX_CONSECUTIVE_WANDERS:
            return self._decide_idle()

        self._last_move_time = time.time()
        self._consecutive_wanders += 1
        self._consecutive_idles = 0

        angle = random.uniform(0, 2 * 3.14159)
        # 漫步幅度与游戏内移动一致（2~4.5 单位），避免"原地打转"式的碎步
        distance = random.uniform(2.0, 4.5)

        return BehaviorDecision(
            behavior=BehaviorType.WANDER,
            reason=f"空闲漫步",
            wander_angle=angle,
            wander_distance=distance,
            confidence=0.55,
        )

    def _decide_anxious_wander(self) -> Optional[BehaviorDecision]:
        """焦虑踱步：被冷落太久时，长时间来回乱走，象征焦虑与无聊。

        生成一条沿随机轴线的往返踱步路线（6~10 个路点），
        每段幅度 2.5~4.5 单位、速度与游戏内移动一致；
        前端对每一段做平滑插值，全程无瞬间移动。
        踱步不携带任何说话文本（避免打扰被冷落的用户）。
        """
        cooldown = (self.FREEZE_WANDER_COOLDOWN if self.degree == "freeze"
                    else self.ANXIOUS_MOVE_COOLDOWN)
        if time.time() - self._last_move_time < cooldown:
            return None

        self._last_move_time = time.time()
        self._consecutive_wanders += 1
        self._consecutive_idles = 0

        env = self.perception.environment
        cx, cz = env.ai_x, env.ai_z
        # 踱步轴线（绕 AI 当前位置往返），幅度与游戏移动一致
        axis = random.uniform(0, 2 * 3.14159)
        radius = random.uniform(2.5, 4.5)
        count = random.randint(6, 10)
        waypoints: list[dict] = []
        for i in range(1, count + 1):
            side = 1 if i % 2 == 0 else -1
            leg = radius * random.uniform(0.7, 1.15)     # 每段幅度略随机
            jitter = random.uniform(-0.8, 0.8)           # 垂直方向小幅偏移 → 松散踱步
            wx = cx + side * leg * math.cos(axis) + jitter * math.cos(axis + math.pi / 2)
            wz = cz + side * leg * math.sin(axis) + jitter * math.sin(axis + math.pi / 2)
            waypoints.append({"x": round(wx, 2), "z": round(wz, 2)})

        return BehaviorDecision(
            behavior=BehaviorType.ANXIOUS_WANDER,
            reason="被冷落太久，焦虑地来回踱步（象征无聊与不安）",
            waypoints=waypoints,
            pacing=True,
            confidence=0.6,
        )

    # 好奇张望：好奇地看看用户在做什么（小动作 + 俏皮嘀咕；不预设恋爱关系，语气由人设决定）
    _CURIOUS_GLANCES = (
        "（你好奇地歪着头，偷偷瞄了一眼用户，小声嘀咕：'诶，你在做什么呀～'）",
        "（你像只好奇的小动物一样凑近两步，眨巴着眼睛想看清用户在做什么）",
        "（你假装四处张望，目光却总是不经意地飘向用户那边，嘴角悄悄翘起来）",
        "（你背着手踮起脚尖，伸长脖子想看看用户那边有什么好玩的）",
    )

    def _decide_curious_glance(self) -> Optional[BehaviorDecision]:
        if time.time() - self._last_glance_time < 20.0:
            return None
        self._last_glance_time = time.time()
        self._consecutive_wanders = 0
        self._consecutive_idles = 0
        return BehaviorDecision(
            behavior=BehaviorType.IDLE_ACTION,
            reason="好奇张望用户",
            speak_text=random.choice(self._CURIOUS_GLANCES),
            confidence=0.75,
        )

    def _decide_go_to_user(self, context: DecisionContext) -> Optional[BehaviorDecision]:
        """走向用户：AI 感知到用户（摄像机）的实际位置，主动走过去陪在Ta身边。

        复用 GO_TO_POI 移动闭环（前端 go_to_poi 会做 A* 寻路 + 平滑移动），
        目标点就是快照中 user 的坐标——AI 因此拥有了对用户的真实空间参考。
        """
        if time.time() - self._last_move_time < self.MOVE_COOLDOWN:
            return None
        env = self.perception.environment
        if not env.user_known:
            return None

        self._last_move_time = time.time()
        self._consecutive_wanders = 0
        self._consecutive_idles = 0
        dist = env.user_distance
        direc = env.user_direction or "那边"
        return BehaviorDecision(
            behavior=BehaviorType.GO_TO_POI,
            reason=f"走向用户（{direc} {dist:.1f}m）",
            target_x=env.user_x, target_z=env.user_z,
            target_label="用户身边",
            speak_text=(f"（你注意到用户在你{direc}约{dist:.1f}米处，"
                        f"决定主动走过去陪在对方身边。用一句话自然地表达想去找Ta。）"),
            confidence=0.7,
        )

    def _decide_idle(self) -> Optional[BehaviorDecision]:
        self._consecutive_idles += 1
        self._consecutive_wanders = 0
        return BehaviorDecision(
            behavior=BehaviorType.IDLE_ACTION,
            reason="小动作",
            confidence=0.8,
        )

    def _reset_chain(self):
        self._chain_exclude.clear()
        self._chain_visit_count = 0
        self._exploration_chain = []

    # ==================== POI 到达通知 ====================

    def notify_poi_reached(self, poi_id: str = ""):
        """前端通知：AI 到达 POI 附近。记录探索，降 1/4 好奇心。"""
        if poi_id:
            self._chain_exclude.add(poi_id)
        if self.perception:
            self.perception.record_exploration(poi_id)

    # ==================== 上下文 ====================

    def build_context(
        self,
        user_is_speaking: bool = False,
        user_last_message_time: float = 0,
        user_engaged: bool = False,
        ai_is_moving: bool = False,
        ai_idle_time: float = 0,
    ) -> DecisionContext:
        perc = self.perception
        env = perc.environment

        return DecisionContext(
            user_is_speaking=user_is_speaking,
            user_last_message_time=user_last_message_time,
            user_engaged=user_engaged,
            ai_is_moving=ai_is_moving,
            ai_idle_time=ai_idle_time,
            user_controlling=perc.user_controlling,
            user_known=env.user_known,
            user_distance=env.user_distance,
            user_direction=env.user_direction,
            has_pois=len(env.pois) > 0,
            best_poi_attractiveness=env.pois[0].total_score if env.pois else 0,
            poi_count=len(env.pois),
            curiosity_level=perc.curiosity.level,
            chain_active=bool(self._chain_exclude),
        )

    # ==================== 序列化 ====================

    def decision_to_command(self, decision: BehaviorDecision) -> dict | None:
        if decision.behavior == BehaviorType.CONTINUE_CHAT:
            return None

        cmd = {
            "type": "ai_behavior_command",
            "behavior": decision.behavior.name.lower(),
            "reason": decision.reason,
            "confidence": decision.confidence,
            "speak_text": decision.speak_text,
            "speak_about_poi": decision.speak_about_poi,
        }

        if decision.behavior in (BehaviorType.GO_TO_POI, BehaviorType.EXPLORE_CHAIN, BehaviorType.IMMEDIATE_ACTION):
            cmd["target"] = {
                "x": decision.target_x, "z": decision.target_z,
                "label": decision.target_label,
            }
            if decision.chain_targets:
                cmd["chain_targets"] = decision.chain_targets
        elif decision.behavior in (BehaviorType.WANDER, BehaviorType.ANXIOUS_WANDER):
            if decision.waypoints:
                # 多路点踱步：路点绕后端感知的 AI 位置（center）生成，
                # 前端将其平移到实际位置附近 → 踱步紧跟角色、全程平滑
                env = self.perception.environment
                cmd["wander"] = {
                    "waypoints": decision.waypoints,
                    "center": {"x": round(env.ai_x, 2), "z": round(env.ai_z, 2)},
                    "pacing": decision.pacing,
                }
            else:
                cmd["wander"] = {
                    "angle": decision.wander_angle,
                    "distance": decision.wander_distance,
                }

        return cmd

    def _record(self, decision: Optional[BehaviorDecision]):
        if decision:
            self._decision_history.append(decision)
            if len(self._decision_history) > 50:
                self._decision_history = self._decision_history[-30:]

    def reset(self):
        self._decision_history.clear()
        self._last_decision_time = 0
        self._last_move_time = 0
        self._last_speak_time = 0
        self._consecutive_wanders = 0
        self._consecutive_idles = 0
        self._reset_chain()
