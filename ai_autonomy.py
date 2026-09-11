# -*- coding: utf-8 -*-
"""AI 自主行为中枢 —— 大厅里角色「自己动起来」的大脑。

从已删除的游戏引擎中抽出的**非游戏**部分：
- 好奇心感知（AIPerceptionEngine）：兴趣点、探索记忆、用户互动涨好奇
- 行为决策（AIBehaviorEngine）：踱步 / 漫步 / 小动作 / 走向兴趣点
- 冷落行为降级（RL 级联）：被冷落越久，自主行为越收敛（normal/calm/freeze）
- 用户空间位置描述：供主动说话时注入上下文

设计原则：这里只服务大厅（非游戏）场景，不持有任何游戏世界模型。
"""
from __future__ import annotations

import logging
import math
import time
from typing import Optional

from ai_behavior_engine import AIBehaviorEngine
from ai_perception_engine import AIPerceptionEngine

logger = logging.getLogger("ai_autonomy")


class AutonomyHub:
    """大厅 AI 自主行为中枢（感知 → 决策 → 行为指令）。"""

    def __init__(self):
        self._perception: Optional[AIPerceptionEngine] = None
        self._behavior: Optional[AIBehaviorEngine] = None
        self.behavior_degree: str = "normal"   # 冷落行为降级档位（RL 级联）
        self._last_autonomy_time: float = 0.0  # 上次自主决策时间（空闲时长依据）
        self._init_engines()

    # ==================== 引擎 ====================

    def _init_engines(self):
        """初始化感知与行为引擎。"""
        self._perception = AIPerceptionEngine()
        self._behavior = AIBehaviorEngine(self._perception)

    # ==================== 环境快照 → 行为指令 ====================

    def apply_environment_snapshot(
        self, data: dict, scene_type: str = "lobby",
        user_engaged: bool = False, user_is_speaking: bool = False,
    ) -> Optional[dict]:
        """应用环境快照到感知引擎，并返回 AI 行为命令。

        前端定期发送环境数据，后端评估后返回行为命令。

        Args:
            data: 环境快照数据
            scene_type: 场景类型（大厅固定 "lobby"）
            user_engaged: 用户是否正在互动
            user_is_speaking: 用户是否正在说话

        Returns:
            行为命令 dict 或 None（不需要行动）
        """
        if not self._perception:
            self._init_engines()

        perc = self._perception
        perc.apply_snapshot(data, scene_type=scene_type)

        ctx = self._behavior.build_context(
            user_is_speaking=user_is_speaking,
            user_engaged=user_engaged,
            user_last_message_time=time.time() if user_engaged else 0,
            ai_is_moving=False,
            ai_idle_time=time.time() - self._last_autonomy_time,
        )
        decision = self._behavior.decide(ctx)
        if not decision:
            return None

        cmd = self._behavior.decision_to_command(decision)
        if decision.speak_text:
            cmd = cmd or {}
            cmd["trigger_ai_speak"] = decision.speak_text
        return cmd

    def handle_autonomy_update(
        self, data: dict, user_engaged: bool = False,
    ) -> tuple[Optional[str], Optional[dict]]:
        """处理自主行为更新。

        Returns:
            (trigger_text, behavior_command) 元组
        """
        self._last_autonomy_time = time.time()
        behavior_cmd = self.apply_environment_snapshot(
            data, scene_type="lobby", user_engaged=user_engaged)

        trigger_text = None

        # 立即行动检查：某个兴趣点好奇心超标 → 立刻扑过去（可打断当前行为）
        if self._behavior and self._perception:
            immediate = self._behavior.check_immediate_action()
            if immediate:
                immediate_cmd = self._behavior.decision_to_command(immediate)
                if immediate_cmd:
                    logger.info(f"[AI自主] 立即行动! {immediate.reason}")
                    if immediate.speak_text:
                        trigger_text = immediate.speak_text
                    return trigger_text, immediate_cmd   # 立即行动优先

        # 行为决策要求 AI 说话 → 转为触发文本
        if behavior_cmd and behavior_cmd.get("trigger_ai_speak"):
            trigger_text = behavior_cmd.pop("trigger_ai_speak")

        return trigger_text, behavior_cmd

    def produce_behavior_command(self, user_engaged: bool = False) -> Optional[dict]:
        """RL 统一调度（engagement 分支）驱动：生成微观行为指令。

        大厅模式下，RL 协调器把路由决策（何时行动）交给行为引擎
        （做什么：踱步/漫步/小动作），让 AI 的行走与动作真正受 RL 统摄。
        """
        if not self._perception:
            self._init_engines()
        data = getattr(self._perception, "_last_snapshot_data", None) or {}
        cmd = self.apply_environment_snapshot(
            data, scene_type="lobby", user_engaged=user_engaged)
        if cmd:
            # RL 自主行为链路不附带说话（说话由 ai_agent 链路负责）
            cmd.pop("trigger_ai_speak", None)
        return cmd

    # ==================== 状态读写 ====================

    def record_ai_exploration(self, poi_id: str):
        """记录 AI 探索了某个兴趣点。"""
        if self._perception:
            self._perception.record_exploration(poi_id)

    def record_user_interaction(self):
        """记录用户互动 → AI 好奇心涨。"""
        if self._perception:
            self._perception.on_user_interaction()

    def set_behavior_degree(self, degree: str):
        """冷落行为降级（RL 级联）：被用户冷落时降低自主行为活跃度。

        - "normal"（用户 5 分钟内互动）→ 恢复正常自主探索
        - "calm"（5min-2h 未互动）→ 降频：只慢走/idle，不触发说话
        - "freeze"（>2h 未互动）→ 冻结：不主动移动探索，仅保持基本 idle
        """
        self.behavior_degree = degree
        if self._behavior:
            self._behavior.set_degree(degree)

    def get_curiosity_level(self) -> float:
        """获取当前好奇心水平。"""
        if self._perception:
            return self._perception.curiosity.level
        return 0.0

    def get_perception_summary(self) -> str:
        """获取感知摘要（用于调试）。"""
        if self._perception:
            return self._perception.get_perception_summary()
        return ""

    def get_user_spatial_desc(self) -> str:
        """用户（摄像机）相对 AI 的空间位置描述（供主动触发/LLM 上下文注入）。

        让 AI 拥有用户位置的实际参考，例如：
        "用户在你右前方约 3.2 米处，正看着你" / "用户就在你身边"。
        未收到用户位置时返回空字符串。
        """
        if not self._perception:
            return ""
        env = self._perception.environment
        if not env.user_known:
            return ""

        parts = []
        if env.user_distance < 1.5:
            parts.append("用户就在你身边")
        else:
            parts.append(f"用户在你{env.user_direction or '附近'}约 {env.user_distance:.1f} 米处")

        if env.user_facing:
            to_ai = math.atan2(env.ai_x - env.user_x, env.ai_z - env.user_z)
            diff = abs(to_ai - env.user_facing)
            if diff > math.pi:
                diff = abs(diff - 2 * math.pi)
            if diff < math.pi / 4:
                parts.append("，正看着你")
            elif diff > 3 * math.pi / 4:
                parts.append("，背对着你")
        return "".join(parts)
