"""AI 感知引擎 —— 统一的 AI 环境感知与动态兴趣评估系统。

核心理念：
- AI 拥有"眼睛"和"好奇心"，自主发现环境中值得关注的事物
- 好奇心是兴奋累积引擎：互动越多越兴奋，发现越有趣越兴奋
- 每个物品有独立的好奇心分数（全局兴奋度 × 物品自身吸引力）
- 如果周围有AI极其好奇的东西 → 立刻不管不顾地扑过去
- 只有完整探索完一个 POI 时，好奇心才会下降约 1/4
- 兴趣评分多因子动态排序（频率衰减 + 时间恢复 + 距离 + 类型稀有度 + 随机）

职责：
- 维护环境状态与 POI 记忆（带时间戳 + 访问次数）
- 好奇心兴奋引擎（互动涨、发现涨、探索完才降）
- 多因子动态兴趣评分
- 生成探索序列（按综合兴趣排序）
"""

from __future__ import annotations

import math
import time
import random
import logging
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger("ai_perception")


# ==================== 数据结构 ====================

@dataclass
class PointOfInterest:
    """兴趣点 —— AI 在环境中发现的值得关注的事物。"""
    id: str
    poi_type: str                    # treasure / collectible / clue / npc / scenery / ...
    label: str
    x: float = 0
    z: float = 0
    distance: float = 999
    direction: str = ""
    is_collected: bool = False
    extra: dict = field(default_factory=dict)

    # 记忆追踪（跨快照持久化）
    visit_count: int = 0             # 访问次数
    first_seen_time: float = 0       # 首次发现时间
    last_visit_time: float = 0       # 上次访问时间
    total_exposure: float = 0        # 累计出现在视野中的时长（秒）

    # 动态评分（每次评分重新计算）
    type_score: float = 0.5          # 类型价值
    novelty_score: float = 0.5       # 新颖度
    distance_score: float = 0        # 距离效用
    recency_score: float = 0.5       # 最近访问激励
    total_score: float = 0           # 物品自身上下文无关的吸引力

    # 好奇心（结合全局兴奋度）
    ai_curiosity: float = 0          # AI 对这个特定 POI 的吸引力评分（= total_score）

    # 扑向追踪（重复扑向→大幅降低兴趣，需要很久恢复）
    pounce_count: int = 0            # AI 扑向这个物品的次数
    last_pounce_time: float = 0      # 上次扑向时间


@dataclass
class EnvironmentState:
    """AI 感知的环境状态快照。"""
    scene_type: str = "lobby"
    scene_bounds: tuple = (-10, -10, 10, 10)
    ai_x: float = 0; ai_z: float = 0; ai_facing: float = 0
    user_engaged: bool = False; user_last_active: float = 0; user_moving: bool = False

    # 用户（摄像机）位置 —— AI 对用户实际空间位置的参考
    user_x: float = 0; user_z: float = 0; user_facing: float = 0
    user_speed: float = 0
    user_known: bool = False          # 是否收到了用户位置数据
    user_distance: float = 999        # 用户与 AI 的直线距离（米）
    user_direction: str = ""          # 用户相对 AI 朝向的方位（如"右前方"）

    pois: list[PointOfInterest] = field(default_factory=list)
    nearby_objects: list[dict] = field(default_factory=list)
    map_data: Optional[dict] = None
    total_objects: dict[str, int] = field(default_factory=dict)
    snapshot_time: float = 0


# ==================== 好奇心兴奋引擎 ====================

class CuriosityDrive:
    """AI 的好奇心兴奋引擎。

    好奇心不是"需要被满足"的欲望，而是"不断累积"的兴奋能量。
    关键洞察：AI 存在"世界熟悉度"学习曲线——
    - 初来乍到 → 好奇心极重，什么都新鲜
    - 探索过大量事物后 → 逐渐淡定，不再一惊一乍
    - 越是见多识广，越需要更长时间/更多互动才能重新兴奋

    核心机制：
    - 互动 → 涨（但熟悉后涨幅衰减）
    - 发现 → 大涨（但熟悉后涨幅衰减）
    - 探索完 → 降 1/4，且地板随熟悉度下降
    - 每个 POI 有独立好奇心 = 兴奋度 × 吸引力 × 距离
    """

    def __init__(self):
        # 初始好奇心高——新世界充满未知
        self._level: float = 0.65
        self._last_update: float = time.time()

        # ===== 世界熟悉度 =====
        self._familiarity = WorldFamiliarity()

        # ===== 增长参数（基础值，会被熟悉度衰减） =====
        self.IDLE_GROWTH_RATE = 0.002          # 空闲每秒增长
        self.USER_INTERACTION_GROWTH = 0.08    # 用户互动一次
        self.DISCOVERY_GROWTH = 0.12           # 发现新东西
        self.NEARBY_INTERESTING_GROWTH = 0.04  # 扫描到有趣 POI

        # ===== 下降参数 =====
        self.EXPLORATION_DROP_RATIO = 0.25     # 探索完降 1/4

        # ===== 立即行动阈值（物品吸引力 > 此值 → 立刻扑过去）=====
        self.IMMEDIATE_ACTION_ATTRACTIVENESS = 0.55

        # 防抖
        self._last_immediate_action_time: float = 0
        self.IMMEDIATE_ACTION_COOLDOWN = 8.0

    # ==================== 属性 ====================

    @property
    def level(self) -> float:
        return self._level

    @property
    def familiarity(self):
        return self._familiarity

    # ==================== 增长触发（加熟悉度衰减）====================

    def _decay_multiplier(self) -> float:
        """熟悉度衰减系数 [0.3, 1.0]。

        熟悉度 0 时 = 1.0（全效增长）
        熟悉度 1 时 = 0.3（涨得极慢，需要大量互动）
        """
        return 1.0 - self._familiarity.familiarity * 0.7

    def _apply_growth(self, base_amount: float) -> float:
        """应用熟悉度衰减后的增长。返回实际增长的数值。"""
        actual = base_amount * self._decay_multiplier()
        old = self._level
        self._level = min(1.0, self._level + actual)
        return self._level - old

    def on_idle(self, dt: float):
        """空闲时缓慢自然增长（熟悉度衰减）。"""
        actual_rate = self.IDLE_GROWTH_RATE * self._decay_multiplier()
        self._level += actual_rate * dt
        self._level = min(self._level, 1.0)

    def on_user_interaction(self):
        """用户互动 → 涨（越熟悉涨越少）。"""
        gained = self._apply_growth(self.USER_INTERACTION_GROWTH)
        logger.debug(f"[Curiosity] 用户互动 → {self._level:.2f} (+{gained:.3f}) 熟悉度={self._familiarity.familiarity:.2f}")

    def on_discovery(self, what: str = ""):
        """发现新东西 → 大涨（越熟悉涨越少）。"""
        gained = self._apply_growth(self.DISCOVERY_GROWTH)
        logger.debug(f"[Curiosity] 发现({what}) → {self._level:.2f} (+{gained:.3f})")
        self._familiarity.mark_discovery()

    def on_nearby_interesting(self):
        """扫描到附近有 POI → 微涨。"""
        self._apply_growth(self.NEARBY_INTERESTING_GROWTH)

    # ==================== 下降触发（地板随熟悉度下降）====================

    @property
    def _current_floor(self) -> float:
        """好奇心下限：越熟悉越低。

        初始 0.65 → 熟悉 0.50 → 0.42 → 熟悉 1.0 → 0.18
        """
        return self._familiarity.curiosity_baseline

    def on_exploration_complete(self):
        """探索完一个 POI → 降 1/4，不会低于熟悉度地板。"""
        new_level = self._level * (1.0 - self.EXPLORATION_DROP_RATIO)
        self._level = max(self._current_floor, new_level)
        self._familiarity.mark_visit()
        logger.debug(f"[Curiosity] 探索完成 → {self._level:.2f} (地板={self._current_floor:.2f})")

    # ==================== 物品吸引力（决定探索哪个）====================

    # 物品吸引力阈值：物品吸引力超过此值 → 立刻扑过去
    IMMEDIATE_ACTION_ATTRACTIVENESS = 0.55

    def is_immediate_action(self, item_attractiveness: float) -> bool:
        """物品吸引力是否高到 AI 必须立刻扑过去？

        完全由物品自身吸引力决定，与 AI 好奇心层级无关。
        好奇心层级只控制 AI 有没有能力去探索。
        """
        if item_attractiveness < self.IMMEDIATE_ACTION_ATTRACTIVENESS:
            return False
        if time.time() - self._last_immediate_action_time < self.IMMEDIATE_ACTION_COOLDOWN:
            return False
        return True

    def mark_immediate_action(self):
        self._last_immediate_action_time = time.time()

    # ==================== 行为阈值（绝对值，不随基线浮动）====================

    @property
    def WANDER_THRESHOLD(self) -> float:
        """漫步阈值——好奇心超过此值才会主动走动。"""
        return 0.35

    @property
    def EXPLORE_THRESHOLD(self) -> float:
        """探索阈值——需要比漫步更兴奋。"""
        return 0.45

    @property
    def CHAIN_THRESHOLD(self) -> float:
        """探索链阈值——足够兴奋才连续探索多个 POI。"""
        return 0.55

    def should_wander(self) -> bool:
        return self._level >= self.WANDER_THRESHOLD

    def should_explore(self) -> bool:
        return self._level >= self.EXPLORE_THRESHOLD

    def should_chain_explore(self) -> bool:
        return self._level >= self.CHAIN_THRESHOLD

    def get_state_summary(self) -> str:
        fam = self._familiarity.familiarity
        if self._level >= 0.85: return "极度兴奋"
        if self._level >= 0.70: return "非常好奇"
        if self._level >= 0.55: return "兴致勃勃" if fam < 0.5 else "还算感兴趣"
        if self._level >= 0.40: return "有点兴趣" if fam < 0.5 else "一般般"
        if self._level >= 0.25: return "平静"
        return "见惯不惊" if fam > 0.6 else "冷淡"

    def reset(self):
        self._level = 0.65
        self._last_immediate_action_time = 0
        self._familiarity.reset()


# ==================== 世界熟悉度 ====================

class WorldFamiliarity:
    """AI 对当前世界的熟悉程度。

    追踪 AI 总共探索了多少东西——越熟悉，越淡定。
    直接决定好奇心基线、增长衰减率。
    """

    def __init__(self):
        self._total_pois_discovered: int = 0    # 发现过的不同 POI 总数
        self._total_visits: int = 0              # 总探索访问次数
        self._unique_areas: set = set()          # 去过的不同区域

    @property
    def familiarity(self) -> float:
        """熟悉度 [0, 1]。

        0 = 全新世界，一切新鲜
        1 = 完全熟悉，见惯不惊

        Sigmoid 曲线：约 25 个 POI 后到达 50%，50 个到达 ~88%，100 个到达 ~99%
        """
        if self._total_pois_discovered <= 0:
            return 0.0
        raw = self._total_pois_discovered / 30.0
        return 1.0 / (1.0 + math.exp(-4.0 * (raw - 0.5)))

    @property
    def curiosity_baseline(self) -> float:
        """好奇心基线（熟悉度越高越低）。

        全新世界: 0.65
        熟悉 0.25: 0.53
        熟悉 0.50: 0.42
        熟悉 0.75: 0.30
        完全熟悉: 0.18
        """
        fam = self.familiarity
        return 0.65 - fam * 0.47

    def mark_discovery(self):
        """记录发现一个新的 POI 类型/实例。"""
        self._total_pois_discovered += 1

    def mark_visit(self):
        """记录一次探索访问。"""
        self._total_visits += 1

    def add_area(self, area_name: str):
        self._unique_areas.add(area_name)

    def get_summary(self) -> str:
        fam = self.familiarity
        if fam < 0.1: return "初来乍到，什么都新鲜"
        if fam < 0.3: return "正在熟悉这个世界"
        if fam < 0.5: return "已经见过不少东西了"
        if fam < 0.7: return "对这个世界很熟悉了"
        if fam < 0.9: return "差不多都见过了"
        return "这个世界的每个角落都一清二楚"

    def reset(self):
        self._total_pois_discovered = 0
        self._total_visits = 0
        self._unique_areas.clear()


# ==================== 动态兴趣评分器 ====================

class InterestScorer:
    """多因子动态兴趣评分系统 —— 评估物品自身的吸引力（与 AI 兴奋度无关）。

    评分维度：
    1. 类型价值（25%）—— 宝藏 > 收集品 > 风景 + 频率衰减
    2. 新颖度（30%）—— 访问次数衰减 + 时间缓慢恢复
    3. 距离效用（20%）—— 最佳距离峰值
    4. 时间激励（15%）—— 久未访问重新变得有趣
    5. 随机鲜度（10%）—— 微小随机扰动
    """

    TYPE_BASE_SCORES = {
        "treasure": 0.95, "mystery": 0.90, "portal": 0.85,
        "collectible": 0.75, "clue": 0.70, "npc": 0.65,
        "animal": 0.60, "resource": 0.55, "door": 0.55,
        "scenery": 0.40, "user": 0.50, "wall": 0.05, "unknown": 0.50,
    }

    TYPE_MIN_SCORES = {
        "treasure": 0.40, "mystery": 0.35, "portal": 0.35,
        "collectible": 0.25, "clue": 0.25, "npc": 0.20,
        "animal": 0.20, "resource": 0.15, "door": 0.15,
        "scenery": 0.10, "user": 0.30, "wall": 0.02, "unknown": 0.20,
    }

    # 距离效用参数：探索型曲线——近处无聊，远处诱人（视野200）
    CLOSE_PENALTY_DIST = 10.0       # 10米以内 = "已经在这里了"，极低兴趣
    EXPLORE_RISE_DIST = 75.0        # 75米 = 吸引力接近峰值
    EXPLORE_PEAK_END = 150.0        # 150米内保持高吸引力
    MAX_EXPLORE_DIST = 200.0        # 200米外略降但仍保持兴趣

    FREQUENCY_SATURATION = 20
    FREQUENCY_DECAY_RATE = 3.0

    NOVELTY_RECOVERY_START = 180.0    # 3分钟后才开始恢复
    NOVELTY_FULL_RECOVERY = 900.0     # 15分钟完全恢复
    NOVELTY_DECAY_FACTOR = 1.2        # 新颖度衰减系数：越大旧物贬值越快

    RECENCY_HALF_LIFE = 180.0

    # 扑向疲劳：指数连续衰减，每次扑向都持续降低兴趣（喜新厌旧核心）
    POUNCE_DECAY_RATE = 0.7                   # 指数衰减系数：越大降得越快
    POUNCE_FATIGUE_RECOVERY_START = 300.0     # 5分钟后才开始恢复
    POUNCE_FATIGUE_FULL_RECOVERY = 1800.0     # 30分钟完全恢复

    @classmethod
    def calc_pounce_fatigue(cls, pounce_count: int, last_pounce_time: float, now: float) -> float:
        """计算扑向疲劳分数 (0, 1.0]。

        指数连续衰减——每次扑向都持续降低兴趣，永不饱和。
        fatigue = exp(-decay_rate * pounce_count)
        - 0 次扑向: 1.000（无惩罚）
        - 1 次扑向: 0.497（50%惩罚，新鲜感大幅下降）
        - 2 次扑向: 0.247（75%惩罚）
        - 3 次扑向: 0.122（已毫无吸引力）
        - 5 次扑向: 0.030（彻底厌倦）

        时间恢复：很久不扑向后慢慢恢复兴趣，但只恢复损失值的一半。
        """
        if pounce_count <= 0:
            return 1.0

        # 指数连续衰减：每次扑向都让兴趣成比例下降
        base = math.exp(-cls.POUNCE_DECAY_RATE * pounce_count)

        # 时间恢复：很久不扑向后慢慢恢复兴趣（只恢复损失值的一半）
        if last_pounce_time > 0:
            elapsed = now - last_pounce_time
            if elapsed > cls.POUNCE_FATIGUE_RECOVERY_START:
                recovery = min(1.0, (elapsed - cls.POUNCE_FATIGUE_RECOVERY_START) /
                                     (cls.POUNCE_FATIGUE_FULL_RECOVERY - cls.POUNCE_FATIGUE_RECOVERY_START))
                base = base + (1.0 - base) * recovery * 0.5

        return max(0.01, base)

    @classmethod
    def calc_type_value(cls, poi_type: str, type_frequency: int) -> float:
        base = cls.TYPE_BASE_SCORES.get(poi_type, 0.5)
        min_v = cls.TYPE_MIN_SCORES.get(poi_type, 0.15)
        if type_frequency <= 0:
            return base
        progress = min(1.0, type_frequency / cls.FREQUENCY_SATURATION)
        decay = 1.0 / (1.0 + math.exp(-cls.FREQUENCY_DECAY_RATE * (0.5 - progress) * 10))
        return min_v + (base - min_v) * decay

    @classmethod
    def calc_novelty(cls, visit_count: int, last_visit: float, now: float) -> float:
        """喜新厌旧：访问次数越多，新颖度连续暴跌（永不饱和）。

        novelty = 1 / (1 + visit_count * decay_factor)
        - 0 次访问: 1.000（完全新鲜）
        - 1 次访问: 0.455（已不新鲜了）
        - 2 次访问: 0.294（有点腻了）
        - 3 次访问: 0.217（明显失去兴趣）
        - 5 次访问: 0.143（基本无感）
        - 10 次访问: 0.077（彻底厌倦）

        时间可缓慢恢复一部分损失值。
        """
        if visit_count <= 0:
            return 1.0
        base = 1.0 / (1.0 + visit_count * cls.NOVELTY_DECAY_FACTOR)
        if last_visit > 0 and visit_count > 0:
            elapsed = now - last_visit
            if elapsed > cls.NOVELTY_RECOVERY_START:
                recovery = min(1.0, (elapsed - cls.NOVELTY_RECOVERY_START) /
                                     (cls.NOVELTY_FULL_RECOVERY - cls.NOVELTY_RECOVERY_START))
                base = base + (1.0 - base) * recovery * 0.4
        return max(0.03, base)

    @classmethod
    def calc_distance_utility(cls, distance: float) -> float:
        """探索型距离效用——鼓励 AI 去远处探索新事物。

        设计理念：AI 是探险家，不是守财奴。
        - 近处（≤2m）：已经在这里了，极低兴趣 (0.06)
        - 中距离（2-15m）：吸引力随距离线性上升 (0.06→0.88)
        - 远距离（15-30m）：高吸引力平台 (0.88→0.95)
        - 极远（30-40m）：轻微下降 (0.95→0.75)
        - 超远（40m+）：保持中等兴趣 (0.65)

        对比旧曲线（5m峰值0.25→1.0→暴跌至0.03）：
        新曲线让AI天生向往远处的未知，而不是围着身边转。
        """
        if distance <= cls.CLOSE_PENALTY_DIST:
            return 0.06  # 近处重罚
        elif distance <= cls.EXPLORE_RISE_DIST:
            t = (distance - cls.CLOSE_PENALTY_DIST) / (cls.EXPLORE_RISE_DIST - cls.CLOSE_PENALTY_DIST)
            return 0.06 + t * 0.82  # 0.06→0.88
        elif distance <= cls.EXPLORE_PEAK_END:
            t = (distance - cls.EXPLORE_RISE_DIST) / (cls.EXPLORE_PEAK_END - cls.EXPLORE_RISE_DIST)
            return 0.88 + t * 0.07  # 0.88→0.95
        elif distance <= cls.MAX_EXPLORE_DIST:
            t = (distance - cls.EXPLORE_PEAK_END) / (cls.MAX_EXPLORE_DIST - cls.EXPLORE_PEAK_END)
            return 0.95 - t * 0.20  # 0.95→0.75
        else:
            return 0.65  # 超远仍保持吸引力

    @classmethod
    def calc_recency_boost(cls, last_visit: float, now: float) -> float:
        if last_visit <= 0:
            return 1.0
        elapsed = now - last_visit
        return 1.0 - math.exp(-elapsed / cls.RECENCY_HALF_LIFE)

    @classmethod
    def calc_random_freshness(cls, poi_id: str) -> float:
        h = sum(ord(c) * (i + 1) for i, c in enumerate(poi_id))
        return (h % 100) / 100.0

    @classmethod
    def score_poi(cls, poi: PointOfInterest, type_frequency: int = 0, now: float = None) -> float:
        """多因子吸引力评分——决定 AI 会选哪个物品去探索（与 AI 兴奋度无关）。

        权重设计：喜新厌旧是最重要的驱动力，距离是探索引擎。
        - 新颖度（36%）：从未见过的东西最诱人，访问过就贬值
        - 扑向疲劳（22%）：重复扑向同一物品，兴趣连续下降
        - 距离效用（18%）：远处未知最诱人，近处已探索的无趣（探索引擎）
        - 类型价值（15%）：宝藏天生比墙壁有趣
        - 最近激励（5%）： 很久没去的地方重新有点吸引力
        - 随机扰动（4%）： 微小扰动打破僵局
        """
        if now is None:
            now = time.time()

        poi.type_score = cls.calc_type_value(poi.poi_type, type_frequency)
        poi.novelty_score = cls.calc_novelty(poi.visit_count, poi.last_visit_time, now)
        poi.distance_score = cls.calc_distance_utility(poi.distance)
        poi.recency_score = cls.calc_recency_boost(poi.last_visit_time, now)
        pounce_fatigue = cls.calc_pounce_fatigue(poi.pounce_count, poi.last_pounce_time, now)
        random_factor = cls.calc_random_freshness(poi.id)

        poi.total_score = (
            poi.novelty_score * 0.36 +      # 喜新厌旧核心
            pounce_fatigue * 0.22 +          # 重复扑向惩罚
            poi.distance_score * 0.18 +      # 探索引擎：远处诱人
            poi.type_score * 0.15 +          # 类型价值
            poi.recency_score * 0.05 +       # 最近激励
            random_factor * 0.04             # 随机扰动
        )
        return poi.total_score


# ==================== AI 感知引擎 ====================

class AIPerceptionEngine:
    """AI 统一感知引擎。

    整合多因子兴趣评分、POI 记忆、好奇心兴奋引擎、探索序列四大子系统。
    """

    def __init__(self):
        self.environment = EnvironmentState()

        # POI 记忆
        self._poi_memory: dict[str, PointOfInterest] = {}
        self._poi_visit_log: dict[str, list[float]] = {}
        self._exposure_counts: dict[str, int] = {}
        self._recently_visited: list[str] = []

        # 好奇心兴奋引擎（新模型：互动涨，探索完才降）
        self.curiosity = CuriosityDrive()

        # 状态
        self._last_update_time: float = time.time()
        self._snapshot_count: int = 0
        self.user_controlling: bool = False

        # 原始快照数据（供行为决策访问环境细节）
        self._last_snapshot_data: dict = {}
        self._env_progress: dict = {}

        # 立即行动防重复：追踪当前正在前往的目标
        self._current_immediate_target_id: str = ""

        # 探索序列
        self._exploration_sequence: list[PointOfInterest] = []
        self._current_chain_index: int = 0
        self._chain_explored_count: int = 0     # 当前链已探索数量

    # ==================== 快照应用 ====================

    @staticmethod
    def _rel_direction(dx: float, dz: float, facing: float) -> str:
        """计算目标相对 AI 朝向的方位描述（正前方/左前方/右侧/…）。

        坐标系约定：世界偏航角 = atan2(dx, dz)，forward = (sinθ, 0, cosθ)，
        与前端 computeBodyFaceCam / smoothRotY 的约定一致。
        """
        ang = math.atan2(dx, dz) - facing
        while ang > math.pi:
            ang -= 2 * math.pi
        while ang < -math.pi:
            ang += 2 * math.pi
        a = abs(ang)
        if a < math.pi / 8:
            return "正前方"
        if a < 3 * math.pi / 8:
            return "右前方" if ang > 0 else "左前方"
        if a < 5 * math.pi / 8:
            return "右侧" if ang > 0 else "左侧"
        if a < 7 * math.pi / 8:
            return "右后方" if ang > 0 else "左后方"
        return "正后方"

    def apply_snapshot(self, data: dict, scene_type: str = "lobby"):
        """应用环境快照。"""
        now = time.time()
        dt = now - self._last_update_time
        self._last_update_time = now
        self._snapshot_count += 1

        env = self.environment
        env.scene_type = scene_type
        env.snapshot_time = now

        # 存储原始快照（供行为决策访问环境细节）
        self._last_snapshot_data = data
        if isinstance(data.get("progress"), dict):
            self._env_progress = data["progress"]

        # AI 位置
        player = data.get("player", {})
        if player:
            env.ai_x = player.get("x", env.ai_x)
            env.ai_z = player.get("z", env.ai_z)
            env.ai_facing = player.get("facing", env.ai_facing)

        # 用户（摄像机）位置 —— AI 对用户实际位置的参考
        # 前端快照中的 user 即用户在场景中的第一人称位置（FPV 时可脱离角色自由走动）
        user = data.get("user")
        if user and isinstance(user, dict):
            env.user_x = user.get("x", env.user_x)
            env.user_z = user.get("z", env.user_z)
            env.user_facing = user.get("facing", env.user_facing)
            env.user_speed = user.get("speed", 0)
            env.user_known = True
            udx = env.user_x - env.ai_x
            udz = env.user_z - env.ai_z
            env.user_distance = math.hypot(udx, udz)
            env.user_direction = self._rel_direction(udx, udz, env.ai_facing)
            env.user_moving = env.user_moving or env.user_speed > 0.1
        else:
            env.user_known = False

        # 用户状态：角色(玩家)移动或摄像机(用户)移动都视为用户在动
        env.user_moving = (player.get("speed", 0) > 0.1
                           or (env.user_known and env.user_speed > 0.1))
        env.user_engaged = data.get("user_engaged", env.user_engaged)
        if env.user_engaged:
            env.user_last_active = now

        # 地图
        map_data = data.get("map")
        if map_data:
            env.map_data = map_data
            if map_data.get("type") == "grid":
                cols = map_data.get("cols", 13)
                rows = map_data.get("rows", 13)
                cs = map_data.get("cell_size", 2.5)
                env.scene_bounds = (-cols * cs / 2, -rows * cs / 2, cols * cs / 2, rows * cs / 2)

        # 兴趣点
        self._update_pois(data, scene_type, now, dt)

        # 生成探索序列 + 计算每个 POI 的好奇心
        self._rank_exploration_sequence(now)

        # 好奇心自然增长
        is_idle = not env.user_engaged and not env.user_moving
        if is_idle:
            self.curiosity.on_idle(dt)

    def _update_pois(self, data: dict, scene_type: str, now: float, dt: float):
        """提取并更新兴趣点。"""
        env = self.environment
        current_ids = set()
        ai_x, ai_z = env.ai_x, env.ai_z

        def _upsert_poi(poi_id, poi_type, label, x, z, dist, direction, extra=None):
            current_ids.add(poi_id)
            if poi_id in self._poi_memory:
                poi = self._poi_memory[poi_id]
                poi.distance = dist; poi.x = x; poi.z = z
                poi.direction = direction
                poi.is_collected = extra.get("collected", False) if extra else False
                poi.total_exposure += dt
            else:
                poi = PointOfInterest(
                    id=poi_id, poi_type=poi_type, label=label,
                    x=x, z=z, distance=dist, direction=direction,
                    first_seen_time=now, extra=extra or {},
                )
                self._poi_memory[poi_id] = poi
                # 发现新的 POI → 好奇心涨
                self.curiosity.on_discovery(f"新{poi_type}:{label}")
            return poi

        for obj in data.get("nearby", []):
            obj_type = obj.get("type", "unknown")
            if obj_type == "wall":
                continue
            # 用位置生成稳定ID，确保同一物体跨快照追踪（扑向疲劳等状态不会丢失）
            obj_id = obj.get("id") or f"{obj_type}_{obj.get('x',0):.0f}_{obj.get('z',0):.0f}"
            dist = obj.get("distance", 999)
            dx = obj.get("x", ai_x + dist * math.cos(env.ai_facing))
            dz = obj.get("z", ai_z + dist * math.sin(env.ai_facing))
            _upsert_poi(obj_id, obj_type,
                       obj.get("name", obj.get("id", obj_type)),
                       dx, dz, dist, obj.get("direction", ""),
                       {"color": obj.get("color", ""), "collected": obj.get("collected", obj.get("found", False))})

        for obj_type, obj_list in data.get("objects", {}).items():
            for obj in obj_list:
                ox, oz = obj.get("x", 0), obj.get("z", 0)
                # 用位置生成稳定ID
                obj_id = obj.get("id") or f"{obj_type}_{ox:.0f}_{oz:.0f}"
                _upsert_poi(obj_id, obj_type,
                           obj.get("name", obj.get("id", obj_type)),
                           ox, oz, math.hypot(ox - ai_x, oz - ai_z), "")

        # 更新类型出现频率
        type_counts: dict[str, int] = {}
        for pid in current_ids:
            if pid in self._poi_memory:
                t = self._poi_memory[pid].poi_type
                type_counts[t] = type_counts.get(t, 0) + 1
        for t, count in type_counts.items():
            self._exposure_counts[t] = max(self._exposure_counts.get(t, 0), count)

        # 构建活跃 POI 列表
        env.pois = []
        for pid in current_ids:
            if pid in self._poi_memory:
                poi = self._poi_memory[pid]
                if poi.is_collected and poi.visit_count > 0:
                    continue
                type_freq = self._exposure_counts.get(poi.poi_type, 0)
                InterestScorer.score_poi(poi, type_freq, now)
                # 物品吸引力 = total_score（好奇心层级不再乘入，完全解耦）
                poi.ai_curiosity = poi.total_score
                env.pois.append(poi)

        # 排序（按物品吸引力降序——喜新厌旧主导）
        env.pois.sort(key=lambda p: p.total_score, reverse=True)

        # 有高分物品 → 好奇心微涨
        if env.pois and env.pois[0].total_score >= 0.45:
            self.curiosity.on_nearby_interesting()

        obj_counts: dict[str, int] = {}
        for poi in env.pois:
            obj_counts[poi.poi_type] = obj_counts.get(poi.poi_type, 0) + 1
        env.total_objects = obj_counts

    def _rank_exploration_sequence(self, now: float):
        """生成按物品吸引力降序的探索序列。"""
        self._exploration_sequence = sorted(
            self.environment.pois,
            key=lambda p: p.total_score,
            reverse=True,
        )
        self._current_chain_index = 0

    # ==================== 好奇心接口 ====================

    def on_user_interaction(self):
        """用户互动 → 好奇心涨。"""
        self.curiosity.on_user_interaction()

    # ==================== 立即行动检查 ====================

    def check_immediate_action(self) -> Optional[PointOfInterest]:
        """检查是否有物品吸引力极高，AI 必须立刻扑过去。

        完全由物品自身吸引力决定（喜新厌旧+类型价值+距离等），
        与 AI 好奇心层级无关。好奇心层级只控制 AI 有没有能力探索。

        防重复：同一目标不会重复触发，直到 AI 到达（record_exploration 清除追踪）
        或 POI 从记忆中消失（已收集/不再出现在视野中）。
        扑向计数不在此处记录——只在 record_exploration（AI 实际到达）时计数。
        """
        # 检查当前追踪目标是否仍在 POI 记忆中（可能已被收集或移出视野）
        if self._current_immediate_target_id and self._current_immediate_target_id not in self._poi_memory:
            self._current_immediate_target_id = ""

        for poi in self._exploration_sequence:
            # 跳过当前正在前往的目标（防止重复触发同一POI）
            if poi.id == self._current_immediate_target_id:
                continue
            if self.curiosity.is_immediate_action(poi.total_score):
                self.curiosity.mark_immediate_action()
                self._current_immediate_target_id = poi.id
                return poi
        return None

    # ==================== 探索序列接口 ====================

    def get_top_pois(self, limit: int = 5, min_attractiveness: float = 0.15) -> list[PointOfInterest]:
        """获取 AI 当前最感兴趣的 N 个点（按物品吸引力排序）。"""
        return [p for p in self._exploration_sequence if p.total_score >= min_attractiveness][:limit]

    def get_best_poi(self) -> Optional[PointOfInterest]:
        """获取 AI 当前最感兴趣的点。"""
        top = self.get_top_pois(limit=1)
        return top[0] if top else None

    def get_next_in_chain(self, exclude_ids: set = None) -> Optional[PointOfInterest]:
        """获取探索链中下一个 POI。"""
        exclude = exclude_ids or set()
        for i, poi in enumerate(self._exploration_sequence):
            if poi.id not in exclude:
                self._current_chain_index = i
                return poi
        return None

    def get_exploration_chain(self, length: int = 3) -> list[PointOfInterest]:
        """获取前 N 个探索目标。"""
        return self._exploration_sequence[:length]

    def get_perception_summary(self) -> str:
        """人类可读感知摘要。"""
        env = self.environment
        c = self.curiosity
        lines = [
            f"位置: ({env.ai_x:.1f}, {env.ai_z:.1f})  场景: {env.scene_type}",
            f"状态: {c.get_state_summary()}  兴奋度: {c.level:.2f}  地板: {c._current_floor:.2f}",
            f"世界: {c.familiarity.get_summary()}  熟悉度: {c.familiarity.familiarity:.2f}  (发现{c.familiarity._total_pois_discovered}个POI, 探索{c.familiarity._total_visits}次)",
        ]
        if env.user_known:
            motion = "移动中" if env.user_speed > 0.1 else "静止"
            lines.append(
                f"用户位置: ({env.user_x:.1f}, {env.user_z:.1f})  "
                f"距离: {env.user_distance:.1f}m {env.user_direction}  {motion}"
            )
        top = self.get_top_pois(5)
        if top:
            lines.append("▼ 吸引力排行（喜新厌旧）:")
            for i, poi in enumerate(top):
                im = "⚡立刻!" if self.curiosity.is_immediate_action(poi.total_score) else ""
                lines.append(
                    f"  {i + 1}. {poi.label}({poi.poi_type}) "
                    f"{poi.distance:.1f}m [吸引{poi.total_score:.3f}] "
                    f"(新{poi.novelty_score:.2f} 疲{InterestScorer.calc_pounce_fatigue(poi.pounce_count, poi.last_pounce_time, time.time()):.2f}) {im}"
                )
        return "\n".join(lines)

    # ==================== 环境数据访问 ====================

    def get_env_objects(self) -> dict:
        """获取最近快照中的环境对象数据。"""
        return self._last_snapshot_data.get("objects", {}) or {}

    def get_env_progress(self) -> dict:
        """获取环境进度数据。"""
        return self._env_progress or {}

    def get_player_state(self) -> dict:
        """获取玩家状态（位置、血量、等级等）。"""
        return self._last_snapshot_data.get("player", {}) or {}

    def get_recent_events(self, limit: int = 5) -> list[dict]:
        """获取最近的游戏事件。"""
        return self._last_snapshot_data.get("recent_events", []) or []

    # ==================== 探索记录 ====================

    def record_exploration(self, poi_id: str):
        """记录 AI 探索了某个 POI。

        每探索完一个 POI → 好奇心降 1/4（降的是兴奋度，每个 POI 自身吸引力不变）。
        探索也计入扑向次数，重复探索同一物品兴趣大幅降低。
        """
        now = time.time()

        # 清除立即行动追踪（已到达目标）
        if poi_id == self._current_immediate_target_id:
            self._current_immediate_target_id = ""

        if poi_id not in self._poi_visit_log:
            self._poi_visit_log[poi_id] = []
        self._poi_visit_log[poi_id].append(now)

        if poi_id in self._poi_memory:
            poi = self._poi_memory[poi_id]
            poi.visit_count += 1
            poi.last_visit_time = now
            # 探索也计入扑向（重复探索同一物品 → 兴趣大幅降低）
            self._record_pounce(poi)

        self.curiosity.on_exploration_complete()

        self._recently_visited.append(poi_id)
        if len(self._recently_visited) > 30:
            self._recently_visited = self._recently_visited[-15:]

        self._rank_exploration_sequence(now)

    def _record_pounce(self, poi: PointOfInterest):
        """记录 AI 扑向/探索了一个 POI，增加扑向疲劳。"""
        now = time.time()
        poi.pounce_count += 1
        poi.last_pounce_time = now
        logger.info(
            f"[Pounce] {poi.label}({poi.poi_type}) 扑向#{poi.pounce_count} → "
            f"疲劳惩罚={InterestScorer.calc_pounce_fatigue(poi.pounce_count, poi.last_pounce_time, now):.2f}"
        )

    def reset(self):
        self.environment = EnvironmentState()
        self._poi_memory.clear()
        self._poi_visit_log.clear()
        self._exposure_counts.clear()
        self._recently_visited.clear()
        self._exploration_sequence = []
        self._current_immediate_target_id = ""
        self.curiosity.reset()
        self._snapshot_count = 0
        self._last_update_time = time.time()
