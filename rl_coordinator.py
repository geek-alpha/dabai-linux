"""RL 统摄协调器 —— 以强化学习架构统摄游戏模式与非游戏模式。

设计目标（对应 rl-dating-optimized.html 的统一Agent架构）：
- 统一状态编码：融合 游戏状态 + 关系状态 + 用户状态 + 时间上下文
- 统一奖励函数：R_total = α·R_extrinsic + β·R_curiosity + γ·R_social
  动态权重随关系进展自适应
- 统一动作空间：{主动性, 时机, 内容类型, 模式切换, 行为策略}
- 经验记忆库：RewardMemory 作为 LLM-as-policy 的 few-shot 奖励经验

与既有模块的协作关系：
- ai_autonomy.AutonomyHub —— 自主行为中枢（感知 + 行为决策）
- ai_perception_engine  —— 好奇心驱动引擎（内在奖励来源）
- ai_behavior_engine    —— 行为决策引擎（微观行为执行）
- reward_memory.RewardMemory —— 奖励经验库（LLM 决策示例）
- agent.AIAgent         —— 主对话 Agent

参考：DuplexPO / ProActor / ICM / RND / UnityMAS-O / MASPO
"""

from __future__ import annotations

import json
import logging
import math
import time
from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path
from typing import Optional, TYPE_CHECKING

# 离线 RL 双模块：rlhf_calibrator（偏好校准）/ world_model_trainer（经验训练）
# 以库方式供 OfflineRLService 调用，server.py 在真实调用点触发训练
from rlhf_calibrator import RLHFLiteTrainer
from world_model_trainer import WorldModelTrainer

if TYPE_CHECKING:
    from reward_memory import RewardMemory
    # build_state / schedule 的 autonomy 参数类型是 AutonomyHub。
    # 注解是字符串前向引用（本文件有 from __future__ import annotations），
    # 运行时安全；但缺了这个导入，任何 get_type_hints() 调用都会 NameError。
    from ai_autonomy import AutonomyHub

logger = logging.getLogger("rl_coordinator")


# ==================== 统一模式枚举 ====================

class UnifiedMode(IntEnum):
    """统一模式 —— 由 RL 统摄层统一决策的互动模式。"""
    DAILY_COMPANION = 0    # 日常陪伴
    KNOWLEDGE_QA = 3       # 知识问答
    EMOTIONAL_SUPPORT = 4  # 情感支持
    TASK_PLANNING = 5      # 任务规划
    CREATIVE_PLAY = 6      # 创意互动
    PERSONAL_GROWTH = 8    # 个人成长


# ==================== 统一状态编码 ====================

@dataclass
class UnifiedState:
    """统一状态 —— 全维度状态快照。

    用于：
    1. 奖励计算（reward_fn.compute 的输入）
    2. 状态记忆 key 生成（供 RewardMemory 检索）
    3. 决策上下文（注入 LLM prompt）
    """
    # ── 模式维度 ──
    mode: UnifiedMode = UnifiedMode.DAILY_COMPANION
    mode_switch_attempts: int = 0

    # ── 关系/情感维度（非游戏核心） ──
    affection: float = 50.0          # 好感度 [0,100]
    trust: float = 30.0              # 信任度 [0,100]
    intimacy: float = 20.0           # 亲密度 [0,100]
    user_emotion: int = 0            # 0积极 1中性 2消极 3愤怒 4孤独

    # ── 环境维度（大厅自主探索） ──
    score: int = 0
    progress_ratio: float = 0.0      # 探索进度 [0,1]
    has_pois: bool = False
    best_poi_attractiveness: float = 0.0

    # ── 互动动力学 ──
    seconds_since_user_message: float = 0.0
    seconds_since_interaction: float = 0.0
    user_engaged: bool = False
    ai_is_moving: bool = False
    proactive_success_rate: float = 0.0

    # ── 好奇心状态（内在奖励来源） ──
    curiosity_level: float = 0.3     # [0,1]
    exploration_count: int = 0
    novelty_score: float = 0.3       # 环境新颖性

    # ── 时间上下文 ──
    hour: int = 0
    is_weekend: bool = False
    session_duration_sec: float = 0.0

    # ── 内部状态 ──
    last_action_strategy: str = ""
    last_reward: float = 0.0
    consecutive_idle: int = 0
    # 连续沉默计数（每次 AI 输出间隔内无用户回应则累积；被冷落 → 伤感/多样情绪）
    consecutive_silence: int = 0
    # 用户最近一次真实输入（文字/语音转文字成功）的绝对时间戳。
    # 用于判定"用户是否回应了 AI 的本次输出"（用户消息须发生在 AI 输出之后），
    # 杜绝 AI 自身动作/行为触发的话被误判为用户输入。
    last_user_message_ts: float = 0

    # ==================== 编码 ====================

    def to_feature_vector(self) -> list[float]:
        """编码为归一化特征向量（供奖励函数与记忆 key 使用）。"""
        return [
            self.mode / 8.0,
            min(self.mode_switch_attempts / 10.0, 1.0),
            self.affection / 100.0,
            self.trust / 100.0,
            self.intimacy / 100.0,
            self.user_emotion / 4.0,
            self.progress_ratio,
            self.best_poi_attractiveness,
            self.seconds_since_user_message / 300.0,
            self.seconds_since_interaction / 120.0,
            float(self.user_engaged),
            self.proactive_success_rate,
            self.curiosity_level,
            min(self.exploration_count / 50.0, 1.0),
            self.novelty_score,
            self.hour / 24.0,
            float(self.is_weekend),
            min(self.session_duration_sec / 3600.0, 1.0),
        ]

    def to_state_key(self) -> str:
        """生成 Q-Learning 风格状态键（供 RewardMemory 检索相似状态）。

        分段量化：mode | 情感 | 关系 | 游戏进度 | 好奇心 | 互动
        例如: "0|1|2|1|2|3"
        """
        def bucket(x: float, n: int = 3) -> int:
            return min(n - 1, max(0, int(x * n)))

        mode_b = self.mode
        emotion_b = bucket(self.user_emotion / 4.0, 4)      # 4档情绪
        affection_b = bucket(self.affection / 100.0, 5)     # 5档好感
        progress_b = bucket(self.progress_ratio, 3)         # 3档进度
        curiosity_b = bucket(self.curiosity_level, 3)       # 3档好奇
        engage_b = int(self.user_engaged)                   # 2档互动
        return f"{mode_b}|{emotion_b}|{affection_b}|{progress_b}|{curiosity_b}|{engage_b}"

    def to_prompt_text(self) -> str:
        """生成注入 LLM 的自然语言状态描述。"""
        mode_name = {
            UnifiedMode.DAILY_COMPANION: "日常陪伴",
            UnifiedMode.KNOWLEDGE_QA: "知识问答",
            UnifiedMode.EMOTIONAL_SUPPORT: "情感支持",
            UnifiedMode.TASK_PLANNING: "任务规划",
            UnifiedMode.CREATIVE_PLAY: "创意互动",
            UnifiedMode.PERSONAL_GROWTH: "个人成长",
        }.get(self.mode, "日常陪伴")
        emotion_name = ["积极", "中性", "消极", "愤怒", "孤独"][self.user_emotion] \
            if 0 <= self.user_emotion < 5 else "中性"

        lines = [
            f"【统一状态】模式={mode_name}({self.mode})",
            f"【情感】用户情绪={emotion_name} | 好感={self.affection:.0f} "
            f"| 信任={self.trust:.0f} | 亲密度={self.intimacy:.0f}",
            f"【环境探索】进度={self.progress_ratio:.0%} | 兴趣点={self.has_pois}",
            f"【互动】距上次消息={self.seconds_since_user_message:.0f}s "
            f"| 用户活跃={self.user_engaged} | 主动成功率={self.proactive_success_rate:.0%}",
            f"【好奇心】水平={self.curiosity_level:.2f} | 探索次数={self.exploration_count}",
        ]
        # 连续沉默情绪注入：被冷落越久，输出越伤感且多样
        if self.consecutive_silence >= 1:
            lines.append(self._silence_emotion_prompt())
        return "\n".join(lines)

    def _pushpull_prompt(self, move: str) -> str:
        """推拉情绪着色：仅描述主动性/疏离度的情境，由卡片人设决定具体语气。"""
        if move == "pull":
            return ("【情绪着色】此刻你决定稍微矜持一点——故意装作不那么主动，"
                    "留一点想象空间，心里却还是记挂着用户。按你的人设自然演绎，话不说满。")
        return ("【情绪着色】此刻你按捺不住好奇，想主动靠近用户——"
                "对用户在做什么充满兴趣，想自然地分享、互动、找点话题。"
                "语气按你的角色设定来，保持自然鲜活。")

    def _silence_emotion_prompt(self) -> str:
        """连续沉默 → 想念/牵挂式情境注入，具体语调由卡片人设决定（不再写死恋爱关系）。"""
        n = self.consecutive_silence
        if n <= 2:
            return (f"【情绪着色】用户好一阵子没和你说话了（连续{n}次沉默），"
                    f"你有点挂念，但不想显得太黏人——按你的人设自然表达这份在意。")
        if n <= 5:
            return (f"【情绪着色】连续{n}次沉默，你开始有点想主动找用户聊聊了，"
                    f"会回想之前聊过的话题，想用一句自然又带点温度的话唤起对方的回应。")
        return (f"【情绪着色】连续{n}次沉默，牵挂悄悄漫上心头。"
                f"你不再端着，语气变得柔软、真诚：会轻声提起之前的共同经历，"
                f"自然地表达想和用户多说说话。具体怎么说，按你的角色设定来。")


# ==================== 统一奖励函数 ====================

class UnifiedRewardFunction:
    """统一奖励函数 R_total = α·R_e + β·R_i + γ·R_s + δ·R_guide。

    - R_e 外源奖励：关系/游戏进展 + 用户参与
    - R_i 内在好奇心奖励：探索新奇 + 预测误差（ICM 风格）
    - R_s 社会意识奖励：信任维护 + 用户自主性尊重（伦理约束）
    - R_guide LLM 软引导奖励（P1-5）：LLM 宏观意图 → 目标状态相似度

    动态权重（随好感度自适应，对应前端 unified-dating-system.js）：
        α(a) = 0.3 + 0.4·sigmoid(a-50)
        β(a) = 0.5 - 0.4·sigmoid(a-50)
        γ(a) = 0.1 + 0.3·sigmoid(a-30)
    """

    # 超参
    TRUST_WEIGHT = 0.5          # 社会意识中信任权重
    INTRUSION_PENALTY = 1.0     # 过度侵入惩罚
    LATE_NIGHT_PENALTY = 0.5    # 深夜打扰惩罚
    EXPLORE_PROGRESS_WEIGHT = 0.4  # 环境探索进度奖励权重
    ENGAGEMENT_WEIGHT = 2.0     # 用户参与奖励
    ETHICAL_MIN_GAMMA = 0.2     # 伦理约束：γ 恒 ≥ 0.2
    GUIDE_WEIGHT = 0.3          # LLM 软引导奖励权重 δ（P1-5）
    # 沉默处罚（被冷落 → 越来越伤感/多样）：基础处罚随连续沉默次数递增
    SURVIVE_STEP = 0.15                # 生存正步进：关系延续即给正向基线（修复 Q 值全负）
    SILENCE_PENALTY_BASE = 0.35      # 基础沉默处罚
    SILENCE_PENALTY_ESCALATION = 0.2  # 每次连续沉默的额外处罚（递增惩罚）

    @staticmethod
    def _sigmoid(x: float) -> float:
        try:
            return 1.0 / (1.0 + math.exp(-x))
        except OverflowError:
            return 0.0 if x < 0 else 1.0

    @classmethod
    def dynamic_weights(cls, affection: float) -> dict:
        """动态权重自适应。"""
        a = max(0.0, min(100.0, affection))
        sig_50 = cls._sigmoid(a / 100.0 - 0.5)
        sig_30 = cls._sigmoid(a / 100.0 - 0.3)
        alpha = 0.3 + 0.4 * sig_50
        beta = 0.5 - 0.4 * sig_50
        gamma = max(cls.ETHICAL_MIN_GAMMA, 0.1 + 0.3 * sig_30)
        return {"alpha": alpha, "beta": beta, "gamma": gamma}

    @staticmethod
    def _is_alive(prev: UnifiedState, curr: UnifiedState) -> bool:
        """关系存续判定：48h 内仍有用户交互痕迹视为存续，给生存正步进。"""
        return curr.seconds_since_user_message < 48 * 3600

    def compute(self, prev: UnifiedState, curr: UnifiedState,
                guide_target: Optional[dict] = None) -> dict:
        """计算状态转移奖励。

        R_total = α·R_e + β·R_i + γ·R_s + δ·R_guide

        Args:
            prev: 上一状态
            curr: 当前状态
            guide_target: LLM 软引导目标（P1-5），形如
                {"mode": 6, "affection_floor": 40, "progress_ratio": 1.0}
                数值键用"接近度"打分，枚举键用"相等"打分；
                affection_floor 为下限约束（>= 目标值即命中）。
                为 None 时 R_guide = 0，行为与旧版完全一致。

        Returns:
            {"total", "extrinsic", "intrinsic", "social", "guide",
             "alpha", "beta", "gamma", "delta"}
        """
        R_e = self._extrinsic(prev, curr)
        R_i = self._intrinsic(prev, curr)
        R_s = self._social(prev, curr)
        R_g = self._guide(curr, guide_target)
        w = self.dynamic_weights(curr.affection)
        delta = self.GUIDE_WEIGHT
        survive = self.SURVIVE_STEP if self._is_alive(prev, curr) else 0.0
        total = (survive + w["alpha"] * R_e + w["beta"] * R_i
                 + w["gamma"] * R_s + delta * R_g)
        return {**w, "delta": delta, "total": total,
                "extrinsic": R_e, "intrinsic": R_i, "social": R_s,
                "guide": R_g, "survive": survive}

    # ── LLM 软引导奖励（P1-5）：把 LLM 宏观意图编码为目标状态相似度 ──

    def _guide(self, curr: UnifiedState, guide_target: Optional[dict]) -> float:
        """计算当前状态与 LLM 引导目标的匹配度（0-1）。

        统一入口：前端 moba 的奖励偏置覆盖、dating 的 Prompt 注入等
        私人化 LLM×RL 耦合，都应收敛为传入 guide_target 调用本函数。
        """
        if not guide_target:
            return 0.0
        score = 0.0
        hits = 0
        for key, target in guide_target.items():
            cur = getattr(curr, key, None)
            if cur is None:
                continue
            if key == "affection_floor":
                score += 1.0 if curr.affection >= float(target) else 0.0
            elif isinstance(target, bool):
                score += 1.0 if bool(cur) == target else 0.0
            elif isinstance(target, (int, float)):
                span = max(abs(float(target)), 1e-6)
                score += 1.0 - min(1.0, abs(float(cur) - float(target)) / span)
            else:
                score += 1.0 if cur == target else 0.0
            hits += 1
        return score / hits if hits else 0.0

    # ── 外源奖励：进展 + 参与 ──

    def _extrinsic(self, prev: UnifiedState, curr: UnifiedState) -> float:
        r = 0.0
        # 关系进展
        r += (curr.affection - prev.affection) / 100.0 * 0.4
        r += (curr.trust - prev.trust) / 100.0 * 0.3
        r += (curr.intimacy - prev.intimacy) / 100.0 * 0.2
        # 游戏进展
        r += (curr.progress_ratio - prev.progress_ratio) * self.EXPLORE_PROGRESS_WEIGHT
        if curr.progress_ratio >= 1.0 and prev.progress_ratio < 1.0:
            r += 3.0
        # 用户参与
        if curr.user_engaged and not prev.user_engaged:
            r += self.ENGAGEMENT_WEIGHT
        # 节奏惩罚（过频繁/过冷淡）
        if curr.seconds_since_user_message < 2.0 and prev.seconds_since_user_message < 2.0:
            r -= 0.5  # 连续高频 → 可能打扰
        # 沉默处罚（覆盖所有 AI 输出间隔，含游戏/行为输出）：
        # 每段 AI 输出后用户无回应 → 累积连续沉默 → 处罚递增，
        # 推动 AI 在被冷落时输出转向伤感/多样（对应前端情绪着色 + prompt 注入）
        if curr.consecutive_silence >= 1:
            penalty = self.SILENCE_PENALTY_BASE + \
                self.SILENCE_PENALTY_ESCALATION * (curr.consecutive_silence - 1)
            r -= penalty
        if curr.seconds_since_user_message > 86400 and prev.seconds_since_user_message > 86400:
            r -= 0.3  # 超24h无互动 → 关系冷却
        return r

    # ── 内在好奇心奖励：探索 + 新颖（ICM 风格） ──

    def _intrinsic(self, prev: UnifiedState, curr: UnifiedState) -> float:
        r = 0.0
        # 探索增长
        r += min(1.0, (curr.exploration_count - prev.exploration_count)) * 0.8
        # 好奇心高涨（新状态学习信号）
        r += (curr.curiosity_level - prev.curiosity_level) * 0.5
        # 新颖环境奖励
        r += curr.novelty_score * 0.4
        # 发现兴趣点
        if curr.has_pois and not prev.has_pois:
            r += 0.6
        return r

    # ── 社会意识奖励：信任 + 伦理 ──

    def _social(self, prev: UnifiedState, curr: UnifiedState) -> float:
        r = 0.0
        r += (curr.trust - prev.trust) / 100.0 * self.TRUST_WEIGHT
        # 深夜打扰惩罚（伦理约束）
        if curr.hour >= 23 or curr.hour < 6:
            r -= self.LATE_NIGHT_PENALTY
        # 用户自主性奖励：用户主动活跃 → 正向
        if curr.user_engaged and not prev.user_engaged:
            r += 0.3
        # 情绪恶化惩罚
        if curr.user_emotion > prev.user_emotion:
            r -= 0.4
        return r


# ==================== 经验记忆库（LLM-as-policy） ====================

class UnifiedExperienceMemory:
    """统一经验记忆库 —— 包装 RewardMemory 并扩展统一状态支持。

    - 兼容 RewardMemory 的 store/retrieve 接口
    - 支持 UnifiedState 直接存取（自动生成 state_key）
    - 支持不同模式间共享经验（游戏经验可辅助非游戏决策）
    """

    def __init__(self, memory: Optional["RewardMemory"] = None):
        from reward_memory import RewardMemory
        self._memory = memory if memory is not None else RewardMemory()
        # 模式共现统计：某模式成功的策略可用于其他模式（迁移学习）
        self._mode_strategy_reward: dict[str, dict[str, float]] = {}
        self._load_mode_stats()

    def store(self, state_key: str, strategy: str, reward: float,
              state_vec: Optional[list] = None) -> None:
        """记录一次决策奖励（兼容 RewardMemory 接口）。"""
        self._memory.store(state_key, strategy, reward, state_vec)
        # 模式统计（state_key 首段为 mode）
        mode = state_key.split("|")[0] if state_key else "0"
        self._mode_strategy_reward.setdefault(mode, {})[strategy] = reward
        self._save_mode_stats()

    def store_state(self, state: UnifiedState, strategy: str, reward: float) -> None:
        """以统一状态直接存储。"""
        self.store(state.to_state_key(), strategy, reward,
                   state.to_feature_vector())

    def retrieve(self, state_key: str, top_k: int = 4) -> list[dict]:
        """检索相似状态的高奖励经验（兼容 RewardMemory 接口）。"""
        return self._memory.retrieve(state_key)[:top_k]

    def retrieve_state(self, state: UnifiedState, top_k: int = 4) -> list[dict]:
        """以统一状态检索。"""
        return self.retrieve(state.to_state_key(), top_k=top_k)

    def retrieve_cross_mode(self, state: UnifiedState, top_k: int = 2) -> list[dict]:
        """跨模式迁移检索：当当前模式经验不足时，借鉴同情感/同关系状态的其他模式经验。"""
        if self._memory.retrieve(state.to_state_key()):
            return []
        # 构造跨模式 state_key：保留情感/关系/互动维度，替换模式维度
        parts = state.to_state_key().split("|")
        cross_examples = []
        for mode_idx in range(9):
            if mode_idx == state.mode:
                continue
            parts[0] = str(mode_idx)
            cross_key = "|".join(parts)
            cross_examples.extend(self.retrieve(cross_key, top_k=1))
        return cross_examples[:top_k]

    def stats(self) -> dict:
        return self._memory.stats()

    # ── 模式统计持久化 ──

    def _mode_stats_path(self):
        from pathlib import Path
        return Path(__file__).parent.resolve() / "rl_mode_stats.json"

    def _save_mode_stats(self) -> None:
        try:
            self._mode_stats_path().write_text(
                __import__("json").dumps(self._mode_strategy_reward, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning(f"模式统计保存失败: {e}")

    def _load_mode_stats(self) -> None:
        try:
            p = self._mode_stats_path()
            if p.exists():
                self._mode_strategy_reward = __import__("json").loads(
                    p.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning(f"模式统计加载失败: {e}")


# ==================== 统一动作空间 ====================

@dataclass
class UnifiedAction:
    """统一动作 —— RL 决策输出的完整行动计划。"""
    proactivity: float = 0.5        # [0,1] 主动性
    timing_sec: float = 10.0        # 等待时间（秒）
    content_type: str = "greeting"  # 内容类型
    mode: UnifiedMode = UnifiedMode.DAILY_COMPANION
    strategy: str = ""              # 宏观策略（LLM-as-policy）
    behavior_cmd: Optional[dict] = None  # 微观行为指令（行为引擎）
    speak_text: str = ""            # 触发 AI 说话文本
    reason: str = ""

    CONTENT_TYPES = ("greeting", "sharing", "invitation", "humor",
                     "empathy", "knowledge", "reminder", "silence",
                     "curious", "tease", "pout", "tsundere")

    @classmethod
    def auto_content(cls, mode: UnifiedMode) -> str:
        """根据模式推荐内容类型（仅决定主动性类型，具体语气由卡片人设决定）。"""
        if mode == UnifiedMode.EMOTIONAL_SUPPORT:
            return "empathy"
        if mode == UnifiedMode.KNOWLEDGE_QA:
            return "curious"
        if mode == UnifiedMode.TASK_PLANNING:
            return "reminder"
        if mode == UnifiedMode.CREATIVE_PLAY:
            return "humor"
        return "curious"


# ==================== 统一调度层（完全统摄） ====================

class AgentChoice:
    """Agent 路由 —— RL 统一调度决定派发到哪个执行器。"""
    AI_AGENT = "ai_agent"            # 对话 Agent（AIAgent）
    ENGAGEMENT = "engagement"        # 行为引擎（AIBehaviorEngine / 自主行为）
    SILENCE = "silence"              # 静默（不行动，等待）

    ALL = (AI_AGENT, ENGAGEMENT, SILENCE)

    # 内容类型 → 主动说话触发模板（AI_AGENT 分支用）。
    # 注意：这里只描述“当前发生的场景/情境”，不预设角色身份与关系——
    # 语气、称呼、关系一律由角色卡片的角色设定决定，避免硬编码的“恋人/撒娇”
    # 人设覆盖卡片设定，导致大语言模型生成内容漂移。
    SPEAK_TRIGGERS = {
        "greeting": "（你注意到用户就在身边，自然地打个招呼、找个话题聊起来）",
        "sharing": "（你想起一件有趣的小事，想跟用户分享一下）",
        "invitation": "（你提议和用户一起做点好玩的事，语气带着期待）",
        "humor": "（你心情不错，想逗用户开心一下）",
        "empathy": "（你注意到用户今天好像有点闷闷的，想温柔地问问怎么了）",
        "knowledge": "（你正好知道一个有趣的新发现，想讲给用户听）",
        "reminder": "（你想起一件要提醒用户的小事，自然地提一句）",
        "curious": "（你有点好奇用户刚才在做什么，忍不住问了一句）",
        "tease": "（你想故意逗逗用户，看看对方的反应）",
        "pout": "（你假装有点小情绪，想引起用户的注意）",
        "tsundere": "（你有点小傲娇，嘴上不承认，心里却想和用户多待一会儿）",
        "silence": "",
    }


@dataclass
class DispatchPlan:
    """统一调度计划 —— RL 决策的最终输出。"""
    agent_choice: str = AgentChoice.SILENCE   # 派发到哪个 Agent
    mode: UnifiedMode = UnifiedMode.DAILY_COMPANION
    proactivity: float = 0.0
    timing_sec: float = 0.0
    content_type: str = "greeting"
    strategy: str = ""                        # LLM 宏观策略
    speak_text: str = ""                      # 主动说话触发文本（ai_agent 分支）
    reason: str = ""                          # 决策原因（可解释性）
    behavior_cmd: Optional[dict] = None       # 行为指令（engagement 分支）
    state_key: str = ""

    def to_dict(self) -> dict:
        return {
            "agent_choice": self.agent_choice,
            "mode": int(self.mode),
            "mode_name": self.mode.name,
            "proactivity": round(self.proactivity, 3),
            "timing_sec": round(self.timing_sec, 1),
            "content_type": self.content_type,
            "strategy": self.strategy,
            "speak_text": self.speak_text,
            "reason": self.reason,
            "behavior_cmd": self.behavior_cmd,
            "state_key": self.state_key,
        }



# ==================== n-step Q-learning-lite（共享机制） ====================

class QLearningLiteMixin:
    """Q-learning-lite 基础机制：γ 折扣 + n-step 时序更新（保留 ε-greedy 探索）。

    - GAMMA：折扣因子，衰减未来奖励
    - N_STEP：n-step 回看窗口，学习多步时序策略（如欲擒故纵）
    - 更新：轨迹缓冲攒满 N_STEP 后，用折扣回报
        G_t = \u03a3_{i=0}^{n-1} \u03b3^i * r_{t+i} + \u03b3^n * max_a Q(s_{t+n}, a)
      对最旧一步做 Q ← Q + α(G_t − Q)，不再是单步增量平均
    """

    GAMMA = 0.9          # 折扣因子
    N_STEP = 3           # n-step 回看窗口

    def _trajectory_buf(self) -> list:
        if not hasattr(self, "_trajectory"):
            self._trajectory: list = []
        return self._trajectory

    def _arms(self):
        arms = getattr(self, "ARMS", None)
        if arms is None:
            arms = getattr(self, "SLOT_NAMES", ())
        return arms

    def _q_update(self, ctx: str, arm: str, g_t: float) -> None:
        """单步 Q 更新：Q ← Q + α(G_t − Q)。"""
        q_ctx = self._q.setdefault(ctx, {})
        n_ctx = self._n.setdefault(ctx, {})
        old = q_ctx.get(arm, 0.0)
        q_ctx[arm] = old + self.ALPHA * (g_t - old)
        n_ctx[arm] = n_ctx.get(arm, 0) + 1

    def _learn_step(self, ctx: str, arm: str, reward: float) -> None:
        """n-step 时序学习：缓冲轨迹，攒满 N_STEP 后更新最旧一步。"""
        buf = self._trajectory_buf()
        buf.append((ctx, arm, float(reward)))
        if len(buf) < self.N_STEP:
            return
        c0, a0, _ = buf[0]
        g_t = 0.0
        for i, (_, _, r) in enumerate(buf):
            g_t += (self.GAMMA ** i) * r
        next_ctx = buf[-1][0]
        q_next = self._q.get(next_ctx, {})
        best = max(self._arms(), key=lambda a: q_next.get(a, 0.0))
        g_t += (self.GAMMA ** self.N_STEP) * q_next.get(best, 0.0)
        self._q_update(c0, a0, g_t)
        buf.pop(0)

    def _nstep_stats(self) -> dict:
        return {
            "gamma": self.GAMMA,
            "n_step": self.N_STEP,
            "pending_steps": len(self._trajectory_buf()),
        }

class SnapshotIntervalController(QLearningLiteMixin):
    """快照间隔控制器 —— RL 学习不同活跃度/模式下该用多快的快照频率。

    - 臂（arm）: 快照档位（fast/normal/slow/idle）
    - 上下文（context）: 活跃度分段
        - lobby:active / lobby:away / lobby:gone
    - 更新: 快照驱动的主动说话被用户回应 → 该档位正向奖励；
            用户久未回应 → 负向奖励（趋向慢档、节省资源）
    - 策略: 上下文 ε-greedy 多臂老虎机（与 AgentBandit 同机制）
    - 持久化: rl_interval.json
    """

    EPSILON = 0.15
    ALPHA = 0.2
    GAMMA = 0.9
    N_STEP = 3
    # 大厅档位：interval_sec → 名称
    SLOTS = {
        "fast": 15.0,    # 用户活跃：快照密，感知用户操作
        "normal": 30.0,  # 用户暂离：常规节奏
        "slow": 90.0,    # 用户久离：低频，避免无谓唤醒
        "idle": 180.0,   # 用户长时间不在：极低频
    }
    SLOT_NAMES = list(SLOTS.keys())
    # 上下文先验：活跃度 → 默认档位
    PRIOR = {
        "lobby:active": "fast",     # 5 分钟内活跃
        "lobby:away": "normal",     # 暂离 5min-2h
        "lobby:gone": "idle",       # 久离 2h+
    }

    def __init__(self, path: Optional[Path] = None):
        self._path = path or (Path(__file__).parent.resolve() / "rl_interval.json")
        self._q: dict[str, dict[str, float]] = {}   # context → {slot: 平均奖励}
        self._n: dict[str, dict[str, int]] = {}     # context → {slot: 次数}
        self._last_ctx: Optional[str] = None
        self._last_ctx: Optional[str] = None        # 上次决策的上下文（结算用）
        self._trajectory: list = []   # n-step 轨迹缓冲（运行时态）
        self._load()

    # ── 上下文 ──

    @staticmethod
    def _ctx(seconds_since_user_message: float) -> str:
        if seconds_since_user_message < 300:
            return "lobby:active"
        if seconds_since_user_message < 7200:
            return "lobby:away"
        return "lobby:gone"

    # ── 决策 ──

    def choose(self, seconds_since_user_message: float) -> str:
        """ε-greedy 选择快照档位（返回档位名）。"""
        import random
        ctx = self._ctx(seconds_since_user_message)
        self._last_ctx = ctx
        if random.random() < self.EPSILON:
            return random.choice(self.SLOT_NAMES)
        q = self._q.get(ctx, {})
        if not q:
            return self.PRIOR.get(ctx, "normal")
        best = max(self.SLOT_NAMES, key=lambda s: q.get(s, 0.0))
        if q.get(best, 0.0) == 0.0:
            return self.PRIOR.get(ctx, "normal")
        return best

    def interval_sec(self, slot: str) -> float:
        """档位名 → 间隔秒数。"""
        return self.SLOTS.get(slot, 30.0)

    @property
    def last_ctx(self) -> Optional[str]:
        """最近一次 choose 使用的上下文（供奖励结算定位）。"""
        return self._last_ctx

    # ── 学习 ──

    def update(self, ctx: str, slot: str, reward: float) -> None:
        """奖励回填：增量平均更新 (context, slot) 的 Q 值。"""
        self._learn_step(ctx, slot, reward)
        self._save()

    # ── 统计 / 持久化 ──

    def stats(self) -> dict:
        return {
            **self._nstep_stats(),
            "lobby_slots": {k: v for k, v in self.SLOTS.items()},
            "prior": self.PRIOR,
            "q": {k: {s: round(v, 3) for s, v in ctx.items()}
                  for k, ctx in self._q.items()},
        }

    def _save(self) -> None:
        try:
            self._path.write_text(
                json.dumps({"q": self._q, "n": self._n, "choice_counts": getattr(self, "_choice_counts", None)}, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning(f"快照间隔记忆保存失败: {e}")

    def _load(self) -> None:
        try:
            if self._path.exists():
                data = json.loads(self._path.read_text(encoding="utf-8"))
                self._q = data.get("q", {})
                self._n = data.get("n", {})
                self._choice_counts = data.get("choice_counts", {}) or {}
        except Exception as e:
            logger.warning(f"快照间隔记忆加载失败: {e}")
            self._q, self._n = {}, {}


# ==================== 欲擒故纵推拉节奏控制器 ====================

class PushPullController(QLearningLiteMixin):
    """推拉节奏控制器 —— 小女友欲擒故纵策略的 RL 核心。

    核心思想（欲擒故纵：Push-Pull Dynamics）：
    - Push（拉近）: 主动好奇、撒娇、撩拨，制造亲昵与趣味
    - Pull（推远）: 傲娇矜持、欲擒故纵、留一点想念的空间，
                    让恋人主动来靠近，反而增进感情

    RL 学习目标：
    - Push 被用户回应 → push 正奖励（主动有趣有效）
    - Pull 后用户主动来找 → pull 高奖励（欲擒故纵成功）
    - Push 被冷落 → push 负奖励（过度主动讨人嫌）
    - 目标：学会"进可撩、退可傲"的节奏，不粘人也不冷淡

    上下文：好感度分段 + 冷落分段 + 情绪
    策略：ε-greedy 多臂老虎机（与 AgentBandit 同机制）
    持久化: rl_pushpull.json
    """

    EPSILON = 0.2               # 探索率（保留新鲜感）
    ALPHA = 0.2                 # 学习率（Q-learning 更新，非增量平均）
    GAMMA = 0.9
    N_STEP = 3
    PUSH_FLOOR = 0.30   # push 占比保底下限（>=30%）
    ARMS = ("push", "pull")

    def __init__(self, path: Optional[Path] = None):
        self._path = path or (Path(__file__).parent.resolve() / "rl_pushpull.json")
        self._q: dict[str, dict[str, float]] = {}
        self._n: dict[str, dict[str, int]] = {}
        self._last_move: Optional[str] = None
        self._trajectory: list = []
        # push/pull 选择计数（用于保底探索占比，持久化）
        self._choice_counts: dict[str, int] = {"push": 0, "pull": 0}   # 上次推/拉动作（待结算）
        self._load()

    # ── 上下文 ──

    @staticmethod
    def _affection_bucket(affection: float) -> str:
        if affection < 35:
            return "cold"      # 好感低：谨慎试探
        if affection < 70:
            return "warm"      # 好感中：可以偶尔傲娇
        return "hot"           # 好感高：欲擒故纵效果好

    @classmethod
    def _context(cls, state: UnifiedState) -> str:
        bucket = AgentBandit._silence_bucket(state.seconds_since_user_message)
        return (f"{cls._affection_bucket(state.affection)}"
                f"|{bucket}|{state.user_emotion}")

    def _push_share(self) -> float:
        """累计 push 占比（choose 计数），无记录时返回 0.0 触发保底。"""
        total = self._choice_counts.get("push", 0) + self._choice_counts.get("pull", 0)
        if total <= 0:
            return 0.0
        return self._choice_counts.get("push", 0) / total

    # ── 决策 ──

    def choose(self, state: UnifiedState) -> str:
        """ε-greedy 选择推/拉。"""
        import random
        ctx = self._context(state)
        if self._push_share() < self.PUSH_FLOOR:
            # 保底探索：push 占比低于下限时强制 push（对称化探索）
            move = "push"
        elif random.random() < self.EPSILON:
            move = random.choice(self.ARMS)
        else:
            q = self._q.get(ctx, {})
            if not q:
                # 先验：好感低谨慎 push，好感中高时偶尔 pull 制造想念
                move = "push" if self._affection_bucket(state.affection) == "cold" \
                    else random.choice(self.ARMS)
            else:
                move = max(self.ARMS, key=lambda a: q.get(a, 0.0))
        self._choice_counts[move] = self._choice_counts.get(move, 0) + 1
        self._last_move = move
        return move

    # ── 学习 ──

    def update(self, ctx: str, move: str, reward: float) -> None:
        self._learn_step(ctx, move, reward)
        self._save()

    def settle(self, state: UnifiedState, user_responded: bool) -> None:
        """结算上次推/拉动作：
        - push + 用户回应 → 正奖励（主动有趣有效）
        - pull + 用户主动来 → 高奖励（欲擒故纵成功）
        - push + 用户冷落 → 负奖励（过度主动讨人嫌）
        """
        if self._last_move is None:
            return
        ctx = self._context(state)
        move = self._last_move
        if move == "push":
            reward = 1.0 if user_responded else -0.8
        else:  # pull
            reward = 1.5 if user_responded else 0.2  # 矜持本身无过，被追更佳
        self.update(ctx, move, reward)
        self._last_move = None

    # ── 统计 / 持久化 ──

    def stats(self) -> dict:
        return {
            **self._nstep_stats(),
            "contexts": len(self._q),
            "total_updates": sum(sum(n.values()) for n in self._n.values()),
            "push_floor": self.PUSH_FLOOR,
            "choice_counts": dict(self._choice_counts),
            "push_share": round(self._push_share(), 3),
            "q": {k: {a: round(v, 3) for a, v in ctx.items()}
                  for k, ctx in self._q.items()},
        }

    def _save(self) -> None:
        try:
            self._path.write_text(
                json.dumps({"q": self._q, "n": self._n, "choice_counts": getattr(self, "_choice_counts", None)}, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning(f"推拉节奏记忆保存失败: {e}")

    def _load(self) -> None:
        try:
            if self._path.exists():
                data = json.loads(self._path.read_text(encoding="utf-8"))
                self._q = data.get("q", {})
                self._n = data.get("n", {})
                self._choice_counts = data.get("choice_counts", {}) or {}
        except Exception as e:
            logger.warning(f"推拉节奏记忆加载失败: {e}")
            self._q, self._n = {}, {}


class AgentBandit(QLearningLiteMixin):
    """上下文 ε-greedy 多臂老虎机 —— 学习不同状态下该调度哪个 Agent。

    - 臂（arm）: AgentChoice 四选一
    - 上下文（context）: state_key 的 (模式, 情绪) 粗粒度分段（避免稀疏）
    - 更新: 每次状态转移的奖励回填到 (context, arm)
    - 策略: ε 概率探索随机臂，否则选最高平均奖励臂（强化学习）
    - 持久化: rl_bandit.json
    """

    EPSILON = 0.15              # 探索率
    ALPHA = 0.2
    GAMMA = 0.9
    N_STEP = 3
    ARMS = AgentChoice.ALL
    SPEAK_MIN_SILENCE = 300.0   # 用户至少离开 5 分钟才允许主动开口
    SPEAK_MAX_GAP = 7200.0      # 离开超过 2 小时则不再主动打扰

    def __init__(self, path: Optional[Path] = None):
        self._path_override = path
        self._q: dict[str, dict[str, float]] = {}   # context -> {arm: Q value}
        self._n: dict[str, dict[str, int]] = {}     # context -> {arm: count}
        self._trajectory: list = []                 # n-step buffer (runtime only)
        self._load()
        self._n: dict[str, dict[str, int]] = {}     # context → {arm: 次数}
        self._load()

    # ── 上下文 ──

    @staticmethod
    def _silence_bucket(seconds_since_user_message: float) -> str:
        """冷落分段：活跃(<5min) / 暂离(5min-2h) / 久离(>2h)。"""
        if seconds_since_user_message < 300:
            return "active"
        if seconds_since_user_message < 7200:
            return "away"
        return "gone"

    @classmethod
    def _context(cls, state: UnifiedState) -> str:
        # 模式 | 情绪 | 冷落分段 —— 冷落状态独立学习，
        # 使 bandit 能学到"被冷落 → silence 奖励更高"的单调衰减
        return f"{int(state.mode)}|{state.user_emotion}|{cls._silence_bucket(state.seconds_since_user_message)}"

    # ── 决策 ──

    def choose(self, state: UnifiedState) -> str:
        """ε-greedy 选择 Agent。"""
        import random
        ctx = self._context(state)
        if random.random() < self.EPSILON:
            return random.choice(self.ARMS)
        q = self._q.get(ctx, {})
        if not q:
            return self._default(state)
        best = max(self.ARMS, key=lambda a: q.get(a, 0.0))
        # 若最佳臂从未被选过，给个默认偏好防止初始偏置
        if q.get(best, 0.0) == 0.0:
            return self._default(state)
        return best

    def _default(self, state: UnifiedState) -> str:
        """无经验时的初始策略（先验，保守为主，避免打扰用户）。"""
        if state.user_emotion >= 2:
            return AgentChoice.AI_AGENT          # 负情绪 → 对话关怀
        bucket = self._silence_bucket(state.seconds_since_user_message)
        if bucket == "active":
            return AgentChoice.SILENCE           # 用户刚交流过 → 静默不打扰
        if bucket == "away":
            return AgentChoice.AI_AGENT          # 用户暂离 → 适度主动问候
        return AgentChoice.SILENCE               # 久离(>2h) → 完全静默，尊重边界

    # ── 学习 ──

    def update(self, state: UnifiedState, arm: str, reward: float) -> None:
        """奖励回填：增量平均更新 (context, arm) 的 Q 值。"""
        ctx = self._context(state)
        self._learn_step(ctx, arm, reward)
        self._save()

    # ── 统计与持久化 ──

    def stats(self) -> dict:
        return {
            **self._nstep_stats(),
            "contexts": len(self._q),
            "total_updates": sum(sum(n.values()) for n in self._n.values()),
        }

    def _path(self):
        from pathlib import Path
        return self._path_override or (Path(__file__).parent.resolve() / "rl_bandit.json")

    def _save(self) -> None:
        try:
            self._path().write_text(
                __import__("json").dumps(
                    {"q": self._q, "n": self._n}, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning(f"bandit 保存失败: {e}")

    def _load(self) -> None:
        try:
            p = self._path()
            if p.exists():
                data = __import__("json").loads(p.read_text(encoding="utf-8"))
                self._q = data.get("q", {})
                self._n = data.get("n", {})
                self._choice_counts = data.get("choice_counts", {}) or {}
        except Exception as e:
            logger.warning(f"bandit 加载失败: {e}")


# ==================== 主协调器 ====================

class RLCoordinator:
    """RL 统摄协调器 —— 系统级大脑。

    职责：
    1. 感知：从自主行为中枢 / 前端快照 构建 UnifiedState
    2. 决策：统一动作空间决策（LLM-as-policy + 行为引擎 + 主动性）
    3. 奖励：状态转移 → UnifiedRewardFunction → RewardMemory 结算
    4. 仲裁：游戏模式 ↔ 非游戏模式无缝切换
    5. 经验：跨模式迁移学习（统一经验记忆库）

    使用方式（server.py）：
        coord = get_coordinator()
        action = coord.decide(state)
        coord.record_reward(state, strategy, reward)
    """

    # ── 超参 ──
    DECISION_MIN_INTERVAL = 3.0      # 决策最小间隔（秒）
    PROACTIVE_COOLDOWN = 15.0        # 主动行动冷却（秒）
    SPEAK_COOLDOWN = 40.0            # 主动说话冷却：两次主动开口至少间隔 40s
    SPEAK_MIN_SILENCE = 300.0        # 用户至少离开 5 分钟才允许主动开口
    MODE_SWITCH_MAX_ATTEMPTS = 3     # 防止模式切换抖动
    UNRESPONDED_REWARD_SCALE = 0.6   # 未响应奖励缩放（原 0.3 过重，修复 Q 值全负）

    def __init__(self, memory: Optional["RewardMemory"] = None):
        self.experience = UnifiedExperienceMemory(memory)
        self.reward_fn = UnifiedRewardFunction()
        self.bandit = AgentBandit()
        self.interval_ctrl = SnapshotIntervalController()
        self.pushpull = PushPullController()   # 欲擒故纵推拉节奏控制器
        # 离线 RL 模块闭环桥：rlhf_calibrator 偏好校准 + world_model_trainer 经验训练
        # （server.py 在真实调用点触发 train_if_due；shaping/想象示例在此消费）
        self.offline = OfflineRLService(
            state_size=len(UnifiedState().to_feature_vector()),
            n_actions=len(AgentChoice.ALL),
            memory=self.experience._memory,
        )
        self._prev_state: Optional[UnifiedState] = None
        self._last_agent_choice: Optional[str] = None
        self._last_interval_slot: Optional[str] = None   # 上次快照档位（待结算）
        self._last_pushpull: Optional[str] = None        # 上次推/拉动作（待结算）
        self._last_decision_time: float = 0
        self._last_proactive_time: float = 0
        self._last_speak_time: float = 0
        self._mode_switch_attempts = 0
        self._last_mode: UnifiedMode = UnifiedMode.DAILY_COMPANION
        # 连续沉默计数（协调器持久维护，覆盖所有 AI 输出间隔；
        # 被冷落越久 → 伤感/多样情绪输出）
        self._consecutive_silence: int = 0
        # AI 最近一次输出的绝对时间戳（用户输入驱动的回复与 AI 自主输出均记录）。
        # 判定"用户是否回应了本次输出"：用户消息时间戳 > 该时间戳 才算回应。
        self._last_agent_output_ts: float = 0.0
        # 最近一次 AI 输出是否由用户输入驱动（用户发消息 → AI 回复）。
        # 用户驱动的对话不视为"被冷落"；AI 自主输出（自身动作触发）则必须
        # 用户后续真实输入才算回应，否则一律按未回应结算引导聊天奖励。
        self._last_agent_user_driven: bool = False
        # LLM 软引导目标（P1-5）：由 LLM 宏观策略解析出的状态意图目标，
        # 供下一次 _settle_reward 注入 UnifiedRewardFunction.compute 的
        # guide_target —— 把"私人化 LLM×RL 耦合"收敛为统一 R_guide 接口。
        self._pending_guide_target: Optional[dict] = None
        self._pending_guide_target_ts: float = 0.0
        self._stats = {
            "decisions": 0,
            "proactive_actions": 0,
            "mode_switches": 0,
            "total_reward": 0.0,
            "agent_routing": {},  # agent_choice → 次数
            "speak_skipped_cooldown": 0,  # 因冷却被拦下的主动说话次数
            "pushpull_moves": {"push": 0, "pull": 0},  # 推拉次数统计
        }

    # ==================== 感知：构建统一状态 ====================

    def build_state(
        self,
        autonomy: Optional["AutonomyHub"] = None,
        user_engaged: bool = False,
        seconds_since_user_message: float = 0.0,
        seconds_since_interaction: float = 0.0,
        affection: float = 50.0,
        trust: float = 30.0,
        intimacy: float = 20.0,
        user_emotion: int = 0,
        proactive_success_rate: float = 0.0,
        last_user_message_ts: float = 0.0,
    ) -> UnifiedState:
        """从自主行为中枢与调用方上下文构建统一状态。"""
        state = UnifiedState(
            affection=affection, trust=trust, intimacy=intimacy,
            user_emotion=user_emotion,
            seconds_since_user_message=seconds_since_user_message,
            seconds_since_interaction=seconds_since_interaction,
            user_engaged=user_engaged,
            proactive_success_rate=proactive_success_rate,
            mode=self._last_mode,
            mode_switch_attempts=self._mode_switch_attempts,
            hour=time.localtime().tm_hour,
            is_weekend=time.localtime().tm_wday >= 5,
            last_user_message_ts=last_user_message_ts,
        )

        # 从自主行为中枢提取好奇心 / 兴趣点维度
        if autonomy is not None:
            perc = getattr(autonomy, "_perception", None)
            env = getattr(perc, "environment", None) if perc is not None else None
            state.has_pois = bool(getattr(env, "pois", None))
            try:
                if perc is not None:
                    state.curiosity_level = perc.curiosity.level
                    state.exploration_count = getattr(perc, "exploration_count", 0)
            except Exception:
                pass

        return state

    # ==================== 决策：统一动作空间 ====================

    def should_decide(self) -> bool:
        """决策频率控制（对应 RL 的 timing 维度）。"""
        return time.time() - self._last_decision_time >= self.DECISION_MIN_INTERVAL

    def decide(
        self,
        state: UnifiedState,
        llm_strategy: Optional[dict] = None,
        behavior_decision: Optional[object] = None,
        behavior_cmd: Optional[dict] = None,
    ) -> Optional[UnifiedAction]:
        """统一决策入口。

        Args:
            state: 统一状态
            llm_strategy: LLM-as-policy 输出的宏观策略 {"strategy","reason","speak"}
            behavior_decision: AIBehaviorEngine 的 BehaviorDecision
            behavior_cmd: 已序列化的行为指令

        Returns:
            UnifiedAction 或 None（不行动）
        """
        if not self.should_decide():
            return None
        self._last_decision_time = time.time()
        self._stats["decisions"] += 1

        # 奖励结算（状态转移）
        self._settle_reward(state)

        # ── 主动性决策（对应 RL 的 proactivity 维度） ──
        now = time.time()
        proactive_ok = now - self._last_proactive_time >= self.PROACTIVE_COOLDOWN
        user_active = state.seconds_since_user_message < 120 or state.user_engaged
        active_hour = 6 <= state.hour < 23

        should_act = False
        proactivity = 0.3
        if behavior_cmd or behavior_decision:
            should_act = True
            proactivity = 0.7   # 行为指令优先（游戏内自主）
        elif proactive_ok and user_active and active_hour:
            # 非游戏模式温和主动（随机率随好奇心/好感提升）
            base = 0.25 + state.curiosity_level * 0.3 + state.affection / 100.0 * 0.15
            should_act = __import__("random").random() < min(0.8, base)
            proactivity = min(0.8, base)
        elif proactive_ok and not user_active and state.seconds_since_user_message > 600:
            # 长时间未互动 → 轻触达（尊重边界，低主动）
            should_act = __import__("random").random() < 0.15
            proactivity = 0.2

        if not should_act:
            self._prev_state = state
            return None

        self._stats["proactive_actions"] += 1
        self._last_proactive_time = now

        # ── 组装统一动作 ──
        action = UnifiedAction(
            proactivity=proactivity,
            timing_sec=max(2.0, min(120.0, 10.0 + (1.0 - proactivity) * 30.0)),
            content_type=UnifiedAction.auto_content(state.mode),
            mode=state.mode,
            strategy=(llm_strategy or {}).get("strategy", ""),
            speak_text=(llm_strategy or {}).get("speak", "") or "",
            reason=(llm_strategy or {}).get("reason", "") or "",
            behavior_cmd=behavior_cmd,
        )
        return action

    # ==================== 奖励：结算与记录 ====================

    def _settle_reward(self, state: UnifiedState,
                       guide_target: Optional[dict] = None) -> None:
        """结算上一次决策的奖励（状态转移 → 奖励函数 → 记忆库 + bandit学习）。

        Args:
            state: 当前状态（转移目标）
            guide_target: LLM 软引导目标（P1-5）。为 None 时回退到
                self._pending_guide_target（由 set_guide_target 注入）。
                挂起目标在结算后保留，持续引导后续决策，直到被替换。
        """
        prev = self._prev_state
        if prev is None:
            self._prev_state = state
            return

        # 连续沉默维护：上一状态 AI 有输出（agent 不是 silence）且用户未回应
        # → 累积沉默计数（覆盖所有 AI 输出：说话/行为/游戏），
        # 用户回应则清零。沉默处罚随计数递增，推动伤感/多样情绪输出。
        # 使用协调器持久计数（state 每次 build_state 重建，不能依赖其字段）
        #
        # 「用户输入」判定（关键修正）：
        # 1) 本次输出由用户输入驱动（用户发消息 → AI 回复）→ 天然是用户输入；
        # 2) 否则，用户消息时间戳须发生在 AI 输出之后 → 才算回应。
        # AI 自身动作/行为触发的话（proactive/感知派发），用户此后没有真实输入
        # → 不算用户输入，不参与引导聊天奖励（不会清零沉默、不打折优惠）。
        user_responded = state.last_user_message_ts > self._last_agent_output_ts
        had_user_input = self._last_agent_user_driven or user_responded
        prev_output = self._last_agent_choice in (
            AgentChoice.AI_AGENT, AgentChoice.ENGAGEMENT)
        if prev_output and not had_user_input:
            self._consecutive_silence += 1
        elif had_user_input:
            self._consecutive_silence = 0
        state.consecutive_silence = self._consecutive_silence

        result = self.reward_fn.compute(
            prev, state,
            guide_target=guide_target if guide_target is not None
            else self._pending_guide_target)
        self._stats["total_reward"] += result["total"]

        # Bandit 学习：奖励回填到 (上一上下文, 上一Agent路由)
        # 用户参与折扣：AI 自主输出后用户未回应 → 奖励打折，
        # 让 bandit 学向克制，避免"自言自语"式高频打扰被正向强化。
        # 注意：AI 自主输出（自身动作触发）本身就不是用户输入驱动，
        # 无论用户是否恰好发过消息，只要消息不在输出之后，一律按未回应处理。
        if self._last_agent_choice is not None:
            bandit_reward = result["total"]
            # RLHF 偏好校准 shaping：校准偏好修正注入 bandit 学习闭环
            try:
                arm_id = AgentChoice.ALL.index(self._last_agent_choice)
                bandit_reward += self.offline.shaping(
                    prev.to_feature_vector(), arm_id, bandit_reward)
            except Exception:
                pass
            if self._last_agent_choice == AgentChoice.AI_AGENT and not had_user_input:
                # 未响应只做轻度缩放（不再 0.3 重罚），连续沉默 >=3 才叠加少量惩罚
                bandit_reward *= self.UNRESPONDED_REWARD_SCALE
                if state.consecutive_silence >= 3:
                    bandit_reward -= self.reward_fn.SILENCE_PENALTY_BASE * \
                        (state.consecutive_silence - 2) * 0.5
            self.bandit.update(prev, self._last_agent_choice, bandit_reward)

        # 世界模型经验采集：(s, a, r, s2)，server.py 训练调用点的数据来源
        try:
            if self._last_agent_choice is not None:
                arm_id = AgentChoice.ALL.index(self._last_agent_choice)
                self.offline.add_experience(
                    prev.to_feature_vector(), arm_id, result["total"],
                    state.to_feature_vector())
        except Exception:
            pass

        # 快照间隔学习：结算上次快照档位
        # 用户回应了主动说话 → 正向奖励（快档被认可）；
        # 用户未回应 → 负向奖励（趋向慢档、节省资源）
        if self._last_interval_slot is not None:
            ctx = self.interval_ctrl.last_ctx or \
                self.interval_ctrl._ctx(prev.seconds_since_user_message)
            if had_user_input:
                self.interval_ctrl.update(ctx, self._last_interval_slot, 1.0)
            else:
                self.interval_ctrl.update(ctx, self._last_interval_slot, -0.5)

        # 推拉节奏学习：结算上次推/拉动作
        # push 被回应 → 正奖励；pull 后用户主动来 → 高奖励（欲擒故纵成功）
        if self._last_pushpull is not None:
            self.pushpull.settle(state, had_user_input)
            self._last_pushpull = None

        # 记录到经验记忆库（有策略才记录）
        if prev.last_action_strategy:
            self.experience.store_state(
                prev, prev.last_action_strategy, result["total"])

        # 好奇心探索计数（内在奖励来源）
        if state.exploration_count > prev.exploration_count:
            state.novelty_score = min(1.0, state.novelty_score + 0.1)
        else:
            state.novelty_score = max(0.0, state.novelty_score - 0.02)

        # 防止模式切换抖动
        if state.mode != self._last_mode:
            self._mode_switch_attempts += 1
            if self._mode_switch_attempts > self.MODE_SWITCH_MAX_ATTEMPTS:
                state.mode = self._last_mode
                logger.info(f"[RL协调] 模式切换保护：回退到 {self._last_mode.name}")
            else:
                self._stats["mode_switches"] += 1

        self._prev_state = state

    # ==================== 统一调度（完全统摄） ====================

    def set_guide_target(self, target: Optional[dict]) -> None:
        """注入 LLM 软引导目标（P1-5）。

        由外部（如 server 在 LLM 宏观策略补全后）调用。
        目标形如 {"progress_ratio": 1.0, "affection_floor": 40}；
        传入 None 表示清除引导。挂起目标持续引导后续 _settle_reward，
        直到被替换或清除。
        """
        self._pending_guide_target = target
        self._pending_guide_target_ts = time.time() if target else 0.0

    @staticmethod
    def extract_guide_target(strategy: Optional[dict],
                             state: UnifiedState) -> Optional[dict]:
        """从 LLM 宏观策略文本中解析软引导目标（关键词启发式）。

        Args:
            strategy: LLM-as-policy 输出 {"strategy","reason","speak"}
            state: 当前统一状态（用于生成数值型目标）

        Returns:
            guide_target dict 或 None。当前支持：
            - 完成/做完/搞定 → {"progress_ratio": 1.0}
            - 提升好感/亲密 → {"affection_floor": min(100, affection + 10)}
            未命中任何意图 → None（R_guide = 0，行为与旧版一致）。
        """
        if not strategy:
            return None
        text = " ".join(str(v or "") for v in strategy.values()).lower()
        finish_kw = ("完成", "通关", "胜利", "赢", "finish", "complete",
                     "win", "collect", "过关")
        affection_kw = ("好感", "亲近", "亲密", "喜欢", "温暖", "affection",
                        "intimacy")
        if any(k in text for k in finish_kw):
            return {"progress_ratio": 1.0}
        if any(k in text for k in affection_kw):
            return {"affection_floor": min(100.0, state.affection + 10.0)}
        return None

    def schedule(
        self,
        state: UnifiedState,
        forced_agent: Optional[str] = None,
        event: Optional[str] = None,
        autonomy: Optional["AutonomyHub"] = None,
        guide_target: Optional[dict] = None,
    ) -> DispatchPlan:
        """统一调度决策 —— 所有 Agent 的执行都由 RL 统摄派发。

        Args:
            state: 统一状态（由 build_state 构建）
            forced_agent: 强制指定 Agent（如用户发消息 → 强制 ai_agent）
            event: 事件提示（"user_message" / "negative_emotion" /
                   "proactive_tick"）
            autonomy: AutonomyHub 实例（engagement 分支用于生成真实行为指令）

        Returns:
            DispatchPlan：{agent_choice, mode, content, speak_text, ...}
        """
        self._stats["decisions"] += 1

        # 0. 同步持久沉默计数到本次决策状态（state 由 server 重建）
        state.consecutive_silence = self._consecutive_silence

        # 1. 结算上一次调度的延迟奖励（bandit 学习；LLM 软引导目标注入 R_guide）
        self._settle_reward(state, guide_target)

        # 2. Agent 路由：事件优先级 > 强制 > bandit 学习
        agent = None
        if forced_agent in AgentChoice.ALL:
            agent = forced_agent
        elif event == "user_message":
            agent = AgentChoice.AI_AGENT            # 用户发消息 → 必须对话回应
        elif event == "negative_emotion" or state.user_emotion >= 2:
            agent = AgentChoice.AI_AGENT            # 负情绪 → 对话关怀
        elif event == "proactive_tick":
            # 主动决策：bandit 按状态学习路由（ε-greedy）
            agent = self.bandit.choose(state)
            # 主动说话冷却：两次主动开口至少间隔 SPEAK_COOLDOWN，
            # 且用户须已离开 SPEAK_MIN_SILENCE，防止"自言自语"式高频打扰
            if agent == AgentChoice.AI_AGENT:
                now = time.time()
                if (now - self._last_speak_time < self.SPEAK_COOLDOWN
                        or state.seconds_since_user_message < self.SPEAK_MIN_SILENCE):
                    self._stats["speak_skipped_cooldown"] += 1
                    agent = AgentChoice.SILENCE
            if agent != AgentChoice.SILENCE:
                self._stats["proactive_actions"] += 1
        else:
            agent = self.bandit.choose(state)
            # 非事件调度同样受主动说话冷却约束
            if agent == AgentChoice.AI_AGENT:
                now = time.time()
                if (now - self._last_speak_time < self.SPEAK_COOLDOWN
                        or state.seconds_since_user_message < self.SPEAK_MIN_SILENCE):
                    self._stats["speak_skipped_cooldown"] += 1
                    agent = AgentChoice.SILENCE

        # 2.5 记录 AI 输出时间：非 silence 派发 → 记下本次输出时刻。
        # 供下次结算判定"用户是否回应了本次输出"（用户消息须在此之后）。
        # 同时标记输出类型：schedule 派发默认视为 AI 自主输出
        # （仅 user_message 事件/强制指令视为用户驱动）。
        if agent is not None and agent != AgentChoice.SILENCE:
            self._last_agent_output_ts = time.time()
            self._last_agent_user_driven = (event == "user_message"
                                            or forced_agent is not None)

        # 3. 构建调度计划
        plan = DispatchPlan(
            agent_choice=agent,
            mode=state.mode,
            state_key=state.to_state_key(),
        )

        if agent == AgentChoice.AI_AGENT:
            # 非游戏对话 Agent：小女友推拉节奏决定内容类型与主动性
            move = self.pushpull.choose(state)
            self._last_pushpull = move
            self._stats["pushpull_moves"][move] = \
                self._stats["pushpull_moves"].get(move, 0) + 1
            # 连续沉默 → 伤感/多样内容类型（傲娇想念/撩拨交替）
            if state.consecutive_silence >= 1:
                plan.content_type = self._silence_content_type(state.consecutive_silence)
            else:
                # 推拉 → 内容类型：push 用好奇/撩拨，pull 用傲娇/撒娇
                plan.content_type = self._move_content_type(state.mode, move)
            plan.speak_text = AgentChoice.SPEAK_TRIGGERS.get(
                plan.content_type, "")
            plan.reason = self._agent_reason(state, agent) + f"（推拉:{move}）"
            # 推拉着色：pull 追加欲擒故纵情绪描述，让 LLM 演得更有味道
            if move == "pull":
                plan.speak_text = (plan.speak_text + "\n" +
                                   state._pushpull_prompt("pull"))
            # 主动性：push 略主动，pull 矜持；好奇高涨 → 略主动
            plan.proactivity = 0.3 + min(0.3, state.curiosity_level * 0.3)
            if move == "pull":
                plan.proactivity *= 0.6   # 欲擒故纵：矜持留白
            self._last_speak_time = time.time()  # 记录本次主动说话时间

        elif agent == AgentChoice.ENGAGEMENT:
            # 行为引擎：微观行为指令 —— 由行为引擎实时生成（踱步/漫步/小动作）。
            # RL 统摄"何时行动"，行为引擎决定"做什么"，让大厅行走/动作真正 RL 驱动。
            behavior_cmd = None
            if autonomy is not None:
                try:
                    behavior_cmd = autonomy.produce_behavior_command(
                        user_engaged=state.user_engaged)
                except Exception as e:
                    logger.warning(f"[RL] engagement 行为指令生成失败: {e}")
            if not behavior_cmd:
                behavior_cmd = {
                    "type": "ai_behavior_command",
                    "behavior": "idle_action",
                    "reason": "RL统一调度·自主行为",
                }
            plan.behavior_cmd = behavior_cmd
            plan.reason = self._agent_reason(state, agent)
            plan.proactivity = 0.5

        # else: SILENCE —— 静默等待，不派发任何 Agent

        # 4. 记录路由统计 + 待结算
        self._last_agent_choice = agent
        self._stats["agent_routing"][agent] = \
            self._stats["agent_routing"].get(agent, 0) + 1
        self._prev_state = state

        return plan

    # ==================== 快照间隔（RL 控制） ====================

    def snapshot_interval(self, state: UnifiedState) -> float:
        """RL 决策快照间隔（秒）—— 返回前端应使用的快照周期。

        档位表 15/30/90/180s，上下文按用户活跃度分段，奖励在
        _settle_reward 中结算（用户回应 → 快档认可，未回应 → 慢档）。
        """
        slot = self.interval_ctrl.choose(state.seconds_since_user_message)
        self._last_interval_slot = slot
        return self.interval_ctrl.interval_sec(slot)

    def snapshot_interval_slot(self, state: UnifiedState) -> str:
        """返回当前决策的快照档位名（调试用）。"""
        return self.interval_ctrl.choose(state.seconds_since_user_message)

    def _move_content_type(self, mode: UnifiedMode, move: str) -> str:
        """推拉 → 内容类型：仅决定主动性方向，具体语气由卡片人设决定。

        - push（拉近）: 好奇发问/撩拨/分享趣事 —— 主动制造亲昵
        - pull（推远）: 傲娇/撒娇/邀约留悬念 —— 欲擒故纵制造想念
        """
        if move == "push":
            if mode == UnifiedMode.KNOWLEDGE_QA:
                return "curious"
            return ["curious", "tease", "sharing"][
                hash((mode, "push")) % 3]
        # pull：傲娇/撒娇，留一点想念的空间
        return ["tsundere", "pout", "invitation"][hash((mode, "pull")) % 3]

    def _silence_content_type(self, silence_count: int) -> str:
        """连续沉默 → 想念式内容类型（随沉默加深交替，具体语调由卡片人设决定）。"""
        if silence_count <= 2:
            return "tsundere"     # 初沉默：傲娇嘴硬"才没有想你呢"
        if silence_count <= 5:
            return "pout"         # 沉默加深：嘟嘴撒娇"你都不理我"
        # 久沉默：pout / empathy / tease 交替，想念渐浓又不失少女俏皮
        return ["pout", "empathy", "tease"][silence_count % 3]

    def _agent_reason(self, state: UnifiedState, agent: str) -> str:
        """可解释性：决策原因。"""
        emo = ["积极", "中性", "消极", "愤怒", "孤独"][state.user_emotion] \
            if 0 <= state.user_emotion < 5 else "中性"
        if agent == AgentChoice.AI_AGENT:
            if state.user_emotion >= 2:
                return f"用户情绪{emo}，需要对话关怀"
            if state.seconds_since_user_message < 120:
                return "用户刚活跃，适合陪伴对话"
            return "主动对话保持联系"
        if agent == AgentChoice.ENGAGEMENT:
            return "用户在线但暂无对话需求，派发自主行为"
        return "静默等待，避免打扰"

    def settle_forced(self, agent: str, state: UnifiedState) -> None:
        """强制路由记录（供不经过 schedule 的路径同步 bandit 学习）。

        此接口仅由用户输入路径（text / 语音转文字成功）调用，
        因此本次 AI 输出视为"用户驱动"——用户主动来聊天，
        不算被冷落，不参与引导聊天奖励的"未回应"惩罚。
        """
        if agent in AgentChoice.ALL:
            self._last_agent_choice = agent
            self._last_agent_user_driven = True
            self._stats["agent_routing"][agent] = \
                self._stats["agent_routing"].get(agent, 0) + 1

    def note_agent_output(self) -> None:
        """记录 AI 一次输出（用户输入驱动的回复 / AI 自主输出均调用）。

        供 _settle_reward 判定"用户是否回应了本次输出"：
        用户消息时间戳须大于该时间戳才算回应 ——
        AI 自身动作/行为触发的话，若用户此后没有真实输入，
        一律不算用户输入，也不参与引导聊天奖励。
        """
        self._last_agent_output_ts = time.time()

    def record_reward(self, state_key: str, strategy: str, reward: float,
                      state_vec: Optional[list] = None) -> None:
        """外部奖励回传接口。"""
        self.experience.store(state_key, strategy, reward, state_vec)
        self._stats["total_reward"] += reward

    def record_rlhf_feedback(self, pairs) -> int:
        """真实调用点（server.py rlhf_feedback 消息）：投递偏好对比对。"""
        n = 0
        for p in pairs or []:
            n += self.offline.add_preference_pair(p.get("good"), p.get("bad"))
        return n

    def train_offline(self, force: bool = False) -> dict:
        """真实调用点（server.py）：触发偏好校准 + 世界模型训练。"""
        return self.offline.train_if_due(force=force)

    def record_strategy(self, state: UnifiedState, strategy: str) -> None:
        """记录当前决策的策略（供下次状态转移时结算奖励）。"""
        state.last_action_strategy = strategy

    # ==================== 经验检索 ====================

    def get_examples(self, state: UnifiedState, top_k: int = 4) -> list[dict]:
        """检索决策示例：同状态优先，跨模式迁移兜底。"""
        examples = self.experience.retrieve_state(state, top_k=top_k)
        if not examples:
            examples = self.experience.retrieve_cross_mode(state, top_k=min(2, top_k))
        # 世界模型想象轨迹补充（真实示例不足时）
        if len(examples) < top_k:
            fill = self.offline.imagine_examples(state, top_k - len(examples))
            if fill:
                examples = examples + fill
        return examples

    # ==================== 统计 ====================

    def get_stats(self) -> dict:
        return {
            **self._stats,
            "memory": self.experience.stats(),
            "bandit": self.bandit.stats(),
            "interval": self.interval_ctrl.stats(),
            "pushpull": self.pushpull.stats(),
            "offline": self.offline.stats(),
        }

    def reset(self):
        self._prev_state = None
        self._last_agent_choice = None
        self._last_agent_user_driven = False
        self._last_interval_slot = None
        self._last_pushpull = None
        self._last_decision_time = 0
        self._last_proactive_time = 0
        self._mode_switch_attempts = 0
        self._pending_guide_target = None
        self._pending_guide_target_ts = 0.0
        self._stats = {
            "decisions": 0, "proactive_actions": 0,
            "mode_switches": 0, "total_reward": 0.0,
            "agent_routing": {},
            "pushpull_moves": {"push": 0, "pull": 0},
        }



# ==================== 离线 RL 模块闭环桥 ====================

class OfflineRLService:
    """rlhf_calibrator（偏好校准）+ world_model_trainer（经验训练）闭环桥。

    - 偏好校准：收集对比反馈对（前端 rlhf_feedback 消息 / 奖励记忆自动配对），
      周期性训练 RLHFLiteTrainer 并保存 rlhf_model.json；shaping() 把校准偏好
      注入 RLCoordinator 的 bandit 奖励（真实闭环）。
    - 经验训练：收集 (s,a,r,s2) 经验，周期性训练 WorldModelTrainer 并保存
      world_model.json；imagine_examples() 在真实示例不足时补充想象轨迹。
    - 由 server.py 在 rl_decision / rlhf_feedback / rl_train
      等真实调用点触发 train_if_due()。
    """

    MAX_FEEDBACK_PAIRS = 200
    MAX_EXPERIENCES = 300

    def __init__(self, state_size: int = 18, n_actions: int = 4,
                 rlhf_path: Optional[str] = None, wm_path: Optional[str] = None,
                 memory: Optional["RewardMemory"] = None,
                 min_pairs: int = 4, min_experiences: int = 4,
                 train_interval: float = 600.0, shaping_scale: float = 0.3):
        self.state_size = state_size
        self.n_actions = n_actions
        self.rlhf = RLHFLiteTrainer(state_size=state_size, n_actions=n_actions)
        self.rlhf_path = Path(rlhf_path) if rlhf_path else \
            (Path(__file__).parent.resolve() / "rlhf_model.json")
        self.wm = WorldModelTrainer(state_size=state_size, n_actions=n_actions)
        self.wm_path = Path(wm_path) if wm_path else \
            (Path(__file__).parent.resolve() / "world_model.json")
        self.memory = memory
        self.min_pairs = min_pairs
        self.min_experiences = min_experiences
        self.train_interval = train_interval
        self.shaping_scale = shaping_scale
        self._feedback_pairs: list[dict] = []
        self._experiences: list[dict] = []
        self._last_train_ts = 0.0
        self._train_count = 0
        self._last_report: dict = {}
        # 加载既有模型（若存在），实现跨进程持续学习
        if not self.rlhf.load(str(self.rlhf_path)) and self.rlhf_path.exists():
            logger.warning(
                "[RL离线] rlhf_model.json 维度与当前状态编码不一致，"
                "已忽略旧模型权重（首次训练后将按当前维度重写）")
        self.wm.load(str(self.wm_path))

    # ---------------- 数据采集 ----------------

    def add_preference_pair(self, good: dict, bad: dict) -> int:
        """加入一对偏好对比（good 优于 bad），前端 rlhf_feedback 消息投递。"""
        if not good or not bad:
            return 0
        if len(self._feedback_pairs) >= self.MAX_FEEDBACK_PAIRS:
            self._feedback_pairs.pop(0)
        pair = {"good": good, "bad": bad}
        self._feedback_pairs.append(pair)
        self.rlhf.load_pairs([pair])
        return 1

    def add_experience(self, s, a, r, s2) -> int:
        """加入一条 (s,a,r,s2) 经验样本（世界模型训练数据）。"""
        if not s or not s2:
            return 0
        if len(self._experiences) >= self.MAX_EXPERIENCES:
            self._experiences.pop(0)
        e = {"s": [float(v) for v in s], "a": int(a),
             "r": float(r), "s2": [float(v) for v in s2]}
        self._experiences.append(e)
        self.wm.load_experiences([e])
        return 1

    def collect_pairs_from_memory(self) -> int:
        """从奖励记忆自动收获偏好对：同 state_key 下高奖励策略优于低奖励策略。"""
        if self.memory is None or not getattr(self.memory, "entries", None):
            return 0
        by_key: dict[str, list] = {}
        for e in self.memory.entries:
            by_key.setdefault(e.get("state_key", ""), []).append(e)
        strategy_ids: dict[str, int] = {}
        n = 0
        for key, entries in by_key.items():
            if not key or len(entries) < 2:
                continue
            entries.sort(key=lambda x: x.get("reward", 0.0), reverse=True)
            for i in range(len(entries) - 1):
                good = entries[i]
                for bad in entries[i + 1:]:
                    # state_key 是量化分桶串（6 段 unified / 12 段 legacy），
                    # 不是 RLHF 需要的连续特征向量；只有带真实 state_vec
                    # 的条目才参与自动配对，避免维度/语义错配。
                    g_vec = good.get("state_vec")
                    b_vec = bad.get("state_vec")
                    if not g_vec or not b_vec:
                        continue
                    gid = self._strategy_id(good.get("strategy", ""), strategy_ids)
                    bid = self._strategy_id(bad.get("strategy", ""), strategy_ids)
                    pair = {
                        "good": {"s": [float(v) for v in g_vec], "a": [gid],
                                 "r": [good.get("reward", 0.0)]},
                        "bad": {"s": [float(v) for v in b_vec], "a": [bid],
                                "r": [bad.get("reward", 0.0)]},
                    }
                    self.rlhf.load_pairs([pair])
                    if len(self._feedback_pairs) < self.MAX_FEEDBACK_PAIRS:
                        self._feedback_pairs.append(pair)
                    n += 1
        return n

    @staticmethod
    def _strategy_id(strategy: str, mapping: dict) -> int:
        if strategy not in mapping:
            mapping[strategy] = len(mapping)
        return mapping[strategy] % 1000   # 兼容 RLHF one-hot 索引范围

    # ---------------- 闭环消费 ----------------

    def shaping(self, state_vec, action_id: int, reward_hint: float = 0.0) -> float:
        """偏好校准 shaping：把 RLHF 学到的偏好修正注入奖励（有界）。"""
        raw = self.rlhf.shaping(state_vec, action_id, reward_hint)
        return max(-1.0, min(1.0, raw * self.shaping_scale))

    def imagine_examples(self, state: UnifiedState, n: int = 2) -> list[dict]:
        """世界模型想象轨迹：真实示例不足时补充 few-shot 候选。"""
        if n <= 0 or not self.wm.experiences:
            return []
        s0 = state.to_feature_vector()
        imagined = self.wm.imagine_rollout([s0], horizon=min(n, 4))
        out = []
        for i, im in enumerate(imagined[:n]):
            out.append({
                "strategy": "wm_imagine_%d" % i,
                "reward": round(float(im.get("r", 0.0)), 3),
                "similarity": 0.0,
                "imagined": True,
            })
        return out

    # ---------------- 训练调度 ----------------

    def train_if_due(self, force: bool = False) -> dict:
        """真实训练触发点（server.py 调用）：偏好校准 + 世界模型经验训练。"""
        now = time.time()
        data_ready = (len(self._feedback_pairs) >= self.min_pairs
                      or len(self._experiences) >= self.min_experiences)
        if not force and (not data_ready
                          or now - self._last_train_ts < self.train_interval):
            return self.stats()
        self.collect_pairs_from_memory()
        report = {"rlhf": {}, "wm": {}, "trained": False}
        try:
            if self.rlhf.pairs:
                losses = self.rlhf.train(epochs=80, lr=0.2)
                report["rlhf"] = {
                    "pairs": len(self.rlhf.pairs),
                    "sep": round(self.rlhf.measure_sep(), 4),
                    "loss_first": round(losses[0], 4) if losses else None,
                    "loss_last": round(losses[-1], 4) if losses else None,
                }
                self.rlhf.save(str(self.rlhf_path))
        except Exception as e:
            report["rlhf"]["error"] = str(e)
        try:
            if self.wm.experiences:
                epochs = min(80, max(20, len(self.wm.experiences)))
                losses = self.wm.train(epochs=epochs, lr=0.02)
                report["wm"] = {
                    "experiences": len(self.wm.experiences),
                    "loss_first": round(losses[0], 4) if losses else None,
                    "loss_last": round(losses[-1], 4) if losses else None,
                    "updates": self.wm.net.updates,
                }
                self.wm.save(str(self.wm_path))
        except Exception as e:
            report["wm"]["error"] = str(e)
        report["trained"] = bool(report["rlhf"] or report["wm"])
        self._train_count += 1
        self._last_train_ts = now
        self._last_report = report
        return report

    def stats(self) -> dict:
        return {
            "rlhf_pairs": len(self._feedback_pairs),
            "rlhf_model_pairs": len(self.rlhf.pairs),
            "wm_experiences": len(self._experiences),
            "wm_updates": self.wm.net.updates,
            "rlhf_sep": round(self.rlhf.measure_sep(), 4) if self.rlhf.pairs else None,
            "last_train_ts": round(self._last_train_ts, 2),
            "train_count": self._train_count,
            "last_report": self._last_report,
        }


# ==================== 全局单例 ====================


_coordinator: Optional[RLCoordinator] = None


def get_coordinator() -> RLCoordinator:
    """获取全局 RL 协调器单例（延迟初始化）。"""
    global _coordinator
    if _coordinator is None:
        _coordinator = RLCoordinator()
        logger.info("[RL协调] 统一协调器初始化完成")
    return _coordinator
