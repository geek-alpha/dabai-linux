/* ============================================================
 * DatingRelationshipModel — 恋爱模拟关系状态机与亲密度模型
 *
 * 提供完整的关系等级、亲密度指标、里程碑事件和行为约束系统。
 * 作为 RL 智能体的关系状态组件，所有行为决策由 RL agent
 * 使用本模型的指标作为状态向量进行控制。
 *
 * 核心设计原则：
 * - 不包含任何固定对话模板或 prompt 文本
 * - 行为约束通过掩码数组硬约束，不由 prompt 规则控制
 * - RL agent 将本模型的指标作为状态向量的一部分
 * ============================================================ */

// ==================== 关系等级定义 ====================

/** 关系等级枚举 */
export const RELATIONSHIP_LEVELS = Object.freeze({
  STRANGER: 0,
  ACQUAINTANCE: 1,
  FRIEND: 2,
  CLOSE_FRIEND: 3,
  ROMANTIC: 4,
  PARTNER: 5
});

/** 等级中文名称（按等级索引） */
export const LEVEL_NAMES = Object.freeze([
  '陌生人',   // 0
  '熟人',     // 1
  '朋友',     // 2
  '密友',     // 3
  '恋人',     // 4
  '伴侣'      // 5
]);

/** 等级英文名称映射 */
export const LEVEL_NAME_MAP = Object.freeze({
  0: 'STRANGER',
  1: 'ACQUAINTANCE',
  2: 'FRIEND',
  3: 'CLOSE_FRIEND',
  4: 'ROMANTIC',
  5: 'PARTNER'
});

// ==================== 动作定义 ====================

/** 所有可能的交互动作列表（索引 = 掩码位） */
export const ACTIONS = Object.freeze([
  'greet',               // 0:  打招呼
  'casual_chat',         // 1:  闲聊
  'compliment',          // 2:  赞美
  'deep_question',       // 3:  深度提问
  'share_feelings',      // 4:  分享感受
  'playful_tease',       // 5:  调皮调侃
  'flirt',               // 6:  调情
  'show_concern',        // 7:  表达关心
  'nickname',            // 8:  昵称称呼
  'declare_affection',   // 9:  表白好感
  'express_missing',     // 10: 表达思念
  'romance',             // 11: 浪漫举动
  'inside_joke',         // 12: 内部梗/默契玩笑
  'comfortable_silence', // 13: 舒适沉默
  'gift',                // 14: 赠送礼物
  'game_together',       // 15: 一起游戏
  'apology'              // 16: 道歉
]);

/** 动作总数 */
export const ACTION_COUNT = ACTIONS.length;

// ==================== 行为掩码（等级约束） ====================

/**
 * 各等级的行为限制掩码。
 * mask[level][actionIndex] = 1 表示允许，0 表示禁止。
 * RL agent 将掩码作为 validActions 参数，确保不选择违规动作。
 */
const BEHAVIOR_MASKS = Object.freeze([
  // Level 0 — STRANGER（陌生人）
  // 仅允许：打招呼、闲聊
  [1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],

  // Level 1 — ACQUAINTANCE（熟人）
  // 允许：打招呼、闲聊、赞美、一起游戏、道歉
  // 禁止：深度提问、分享感受、调侃、调情、关心、昵称、表白、思念、
  //       浪漫、内部梗、舒适沉默、送礼
  [1, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1],

  // Level 2 — FRIEND（朋友）
  // 允许：打招呼、闲聊、赞美、深度提问、分享感受、调侃、轻调情、送礼、游戏、道歉
  // 禁止：关心、昵称、表白、思念、浪漫、内部梗、舒适沉默
  [1, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1],

  // Level 3 — CLOSE_FRIEND（密友）
  // 允许：除表白、思念、浪漫、内部梗、舒适沉默外的所有
  [1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0, 1, 1, 1],

  // Level 4 — ROMANTIC（恋人）
  // 允许：除内部梗、舒适沉默外的所有
  [1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0, 1, 1, 1],

  // Level 5 — PARTNER（伴侣）
  // 允许：全部动作
  [1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1]
]);

// ==================== 等级阈值 ====================

/**
 * 各等级提升所需的指标阈值与判定模式。
 *
 * mode: 'or' 表示任一条件满足即可升级；
 *       'and' 表示所有条件必须同时满足。
 *
 * 键为提升后的等级，值为所需条件对象。
 */
const LEVEL_UP_THRESHOLDS = Object.freeze({
  1: { mode: 'or',  intimacy: 15, interactions: 5 },
  // 0 → 1：intimacy >= 15 OR total_interactions >= 5

  2: { mode: 'or',  intimacy: 30, sessions: 3, milestone: 'first_deep_conversation' },
  // 1 → 2：intimacy >= 30 OR sessions >= 3 OR first_deep_conversation achieved

  3: { mode: 'and', intimacy: 50, trust: 40, milestone: 'first_deep_conversation' },
  // 2 → 3：intimacy >= 50 AND trust >= 40 AND first_deep_conversation achieved

  4: { mode: 'and', intimacy: 70, trust: 60, affection: 50, milestone: 'first_flirt' },
  // 3 → 4：intimacy >= 70 AND trust >= 60 AND affection >= 50 AND first_flirt achieved

  5: { mode: 'and', intimacy: 85, trust: 80, affection: 75, milestone: 'bond_confirmed' }
  // 4 → 5：intimacy >= 85 AND trust >= 80 AND affection >= 75 AND bond_confirmed achieved
});

/**
 * 等级下降判定系数。
 * 当所有关键指标均未达到当前等级阈值的此比例时，触发降级判定。
 */
const LEVEL_DOWN_FACTOR = 0.6;

/**
 * 连续多少次降级检查失败后才实际降级。
 * 避免指标短暂波动导致的频繁升降。
 */
const LEVEL_DOWN_CONSECUTIVE_NEEDED = 3;

// ==================== 里程碑定义 ====================

/**
 * 里程碑数据定义。
 * bonus: 达成时获得的亲密度/信任奖励
 * triggerLevel: 该里程碑预期的触发等级（用于估算）
 */
const MILESTONE_DEFS = Object.freeze({
  first_greeting:          { bonus: { intimacy: 2,  trust: 1 },  triggerLevel: 0 },
  first_compliment:        { bonus: { intimacy: 3,  trust: 2 },  triggerLevel: 1 },
  first_game_together:     { bonus: { intimacy: 4,  trust: 3 },  triggerLevel: 1 },
  first_deep_conversation: { bonus: { intimacy: 8,  trust: 5 },  triggerLevel: 2 },
  first_flirt:             { bonus: { intimacy: 10, trust: 4 },  triggerLevel: 3 },
  first_affection:         { bonus: { intimacy: 12, trust: 6 },  triggerLevel: 3 },
  first_gift:              { bonus: { intimacy: 5,  trust: 4 },  triggerLevel: 2 },
  first_disagreement:      { bonus: { intimacy: -3, trust: -2 }, triggerLevel: 0 },
  bond_confirmed:          { bonus: { intimacy: 20, trust: 15 }, triggerLevel: 4 }
});

/** 里程碑中文名称 */
export const MILESTONE_NAMES = Object.freeze({
  first_greeting:          '初次打招呼',
  first_compliment:        '第一次被赞美',
  first_game_together:     '一起玩游戏',
  first_deep_conversation: '深度对话',
  first_flirt:             '第一次调情',
  first_affection:         '第一次表达好感',
  first_gift:              '送礼物',
  first_disagreement:      '第一次分歧',
  bond_confirmed:          '关系确认'
});

/** 里程碑默认初始状态（含计数型字段） */
const DEFAULT_MILESTONES = Object.freeze({
  first_greeting:          false,
  first_compliment:        false,
  first_game_together:     false,
  first_deep_conversation: false,
  first_flirt:             false,
  first_affection:         false,
  first_gift:              false,
  first_disagreement:      false,
  bond_confirmed:          false,
  days_known:              0,
  total_interactions:      0,
  total_sessions:          0
});

/** 亲密度的最大/最小值 */
const INTIMACY_MIN = -100;
const INTIMACY_MAX = 100;
const TRUST_MIN = 0;
const TRUST_MAX = 100;
const QUALITY_MIN = -50;
const QUALITY_MAX = 50;

/** 用于 affectionTrend 计算的滑动窗口大小 */
const TREND_WINDOW_SIZE = 5;

// ==================== 主类 ====================

/** 里程碑状态（boolean 型里程碑 + 计数型字段） */
interface MilestoneState {
  first_greeting: boolean;
  first_compliment: boolean;
  first_game_together: boolean;
  first_deep_conversation: boolean;
  first_flirt: boolean;
  first_affection: boolean;
  first_gift: boolean;
  first_disagreement: boolean;
  bond_confirmed: boolean;
  days_known: number;
  total_interactions: number;
  total_sessions: number;
}

export class DatingRelationshipModel {
  /**
   * 构造函数 — 无参数，所有状态初始化为默认值。
   */
  constructor() {
    this.reset();
  }

  declare level: number;
  declare metrics: { intimacy: number; trust: number; interactionQuality: number };
  declare milestones: MilestoneState;
  declare _affectionHistory: number[];
  declare _lowMetricsCount: number;

  // ---------------------------------------------------------------
  // 公开查询方法
  // ---------------------------------------------------------------

  /** @returns {number} 当前关系等级 (0–5) */
  getLevel() {
    return this.level;
  }

  /** @returns {string} 当前等级中文名称 */
  getLevelName() {
    return LEVEL_NAMES[this.level] || '未知';
  }

  /** @returns {string} 当前等级英文标识 */
  getLevelKey() {
    return LEVEL_NAME_MAP[this.level] || 'UNKNOWN';
  }

  /** @returns {number} 动作总数 */
  static getActionCount() {
    return ACTION_COUNT;
  }

  /** @returns {string[]} 所有动作名称列表（副本） */
  static getActions() {
    return [...ACTIONS];
  }

  /**
   * 获取指定动作的名称。
   * @param {number} index 动作索引
   * @returns {string} 动作名称，无效索引返回 'unknown'
   */
  static getActionName(index) {
    return ACTIONS[index] || 'unknown';
  }

  // ---------------------------------------------------------------
  // 指标获取
  // ---------------------------------------------------------------

  /**
   * 获取所有关系指标（含 computed 指标）。
   * @returns {Object} 完整指标快照
   */
  getMetrics() {
    const affection = this._calcAffection();
    const trend = this._calcAffectionTrend();
    return {
      intimacy:           this.metrics.intimacy,
      trust:              this.metrics.trust,
      interactionQuality: this.metrics.interactionQuality,
      affection:          affection,
      affectionTrend:     trend
    };
  }

  /**
   * 获取原始指标（不含 computed 值），
   * 供 RL agent 构建状态向量时使用。
   * @returns {Object} { intimacy, trust, interactionQuality }
   */
  getRawMetrics() {
    return { ...this.metrics };
  }

  /**
   * 获取完整的 RL 状态向量（等级 + 原始指标 + computed + 里程碑进度）。
   * 8 维归一化向量，供 RL agent 拼接状态使用。
   *
   * @returns {number[]} 归一化状态数组 [0..7]
   */
  getStateVector() {
    const m = this.getMetrics();
    const achieved = Object.entries(MILESTONE_DEFS)
      .filter(([name]) => this.milestones[name])
      .length;
    const total = Object.keys(MILESTONE_DEFS).length;

    return [
      this.level / 5,                              // [0] 等级 [0,1]
      (this.metrics.intimacy + 100) / 200,          // [1] 亲密度 [0,1]
      this.metrics.trust / 100,                     // [2] 信任度 [0,1]
      (this.metrics.interactionQuality + 50) / 100, // [3] 互动质量 [0,1]
      (m.affection + 100) / 200,                    // [4] 综合情感 [0,1]
      achieved / total,                             // [5] 里程碑比例 [0,1]
      Math.min(this.milestones.total_interactions / 100, 1), // [6] 互动次数归一化
      Math.min(this.milestones.total_sessions / 50, 1)      // [7] 会话数归一化
    ];
  }

  /** @returns {number} 状态向量维度（固定 8） */
  static getStateSize() {
    return 8;
  }

  // ---------------------------------------------------------------
  // 指标更新
  // ---------------------------------------------------------------

  /**
   * 更新关系指标（由 RL agent 在每次交互后调用）。
   * 自动 clamp 各指标至合法范围、更新 affection 历史与趋势、
   * 递增互动计数、检查等级过渡。
   *
   * @param {number} deltaIntimacy  亲密度变化量
   * @param {number} deltaTrust     信任度变化量
   * @param {number} deltaQuality   互动质量变化量
   * @returns {Object} 更新后的指标快照（同 getMetrics()）
   */
  updateMetrics(deltaIntimacy, deltaTrust, deltaQuality) {
    // 应用变化并 clamp
    this.metrics.intimacy = clamp(
      this.metrics.intimacy + deltaIntimacy,
      INTIMACY_MIN, INTIMACY_MAX
    );
    this.metrics.trust = clamp(
      this.metrics.trust + deltaTrust,
      TRUST_MIN, TRUST_MAX
    );
    this.metrics.interactionQuality = clamp(
      this.metrics.interactionQuality + deltaQuality,
      QUALITY_MIN, QUALITY_MAX
    );

    // 递增互动计数
    this.milestones.total_interactions += 1;

    // 更新 affection 历史（用于趋势计算）
    this._affectionHistory.push(this._calcAffection());
    if (this._affectionHistory.length > TREND_WINDOW_SIZE) {
      this._affectionHistory.shift();
    }

    // 检查等级过渡
    this._checkLevelTransition();

    return this.getMetrics();
  }

  /**
   * 标记一次会话结束（每次对话/游戏 session 结束时调用）。
   * 更新会话计数并重新检查等级条件。
   */
  endSession() {
    this.milestones.total_sessions += 1;
    this._checkLevelTransition();
  }

  /**
   * 设置相识天数。
   * @param {number} days
   */
  setDaysKnown(days) {
    this.milestones.days_known = Math.max(0, Math.floor(days));
  }

  // ---------------------------------------------------------------
  // 里程碑系统
  // ---------------------------------------------------------------

  /**
   * 触发指定的里程碑。
   *
   * 若里程碑未达成，则标记为已达成、应用 bonus 奖励，
   * 然后检查是否因此触发等级提升。
   *
   * @param {string} name 里程碑名称（MILESTONE_DEFS 的键之一）
   * @returns {Object} { new: boolean, bonus: {intimacy, trust}, levelUp: boolean }
   */
  triggerMilestone(name) {
    const result = {
      new: false,
      bonus: { intimacy: 0, trust: 0 },
      levelUp: false
    };

    // 验证名称合法性（必须是 boolean 型里程碑）
    if (!(name in this.milestones) || typeof this.milestones[name] !== 'boolean') {
      return result;
    }

    // 已达成则跳过
    if (this.milestones[name]) {
      return result;
    }

    // 标记达成
    this.milestones[name] = true;
    result.new = true;

    // 应用奖励
    const def = MILESTONE_DEFS[name];
    if (def && def.bonus) {
      const b = def.bonus;
      this.metrics.intimacy = clamp(
        this.metrics.intimacy + (b.intimacy || 0),
        INTIMACY_MIN, INTIMACY_MAX
      );
      this.metrics.trust = clamp(
        this.metrics.trust + (b.trust || 0),
        TRUST_MIN, TRUST_MAX
      );
      result.bonus = { intimacy: b.intimacy || 0, trust: b.trust || 0 };
    }

    // 检查等级提升
    const beforeLevel = this.level;
    this._checkLevelTransition();
    result.levelUp = (this.level > beforeLevel);

    return result;
  }

  /**
   * 获取所有里程碑的达成状态（含计数型字段）。
   * @returns {Object} 里程碑状态快照副本
   */
  getMilestoneProgress() {
    return { ...this.milestones };
  }

  /**
   * 获取下一个尚未达成的里程碑的预计奖励总值（intimacy + trust）。
   * 用于 RL 奖励塑形（reward shaping），引导 agent 朝向下一里程碑。
   * @returns {number} 预估奖励值（所有里程碑已达成则返回 0）
   */
  getNextMilestoneBonus() {
    for (const [name, def] of Object.entries(MILESTONE_DEFS)) {
      if (!this.milestones[name]) {
        const b = def.bonus;
        return (b.intimacy || 0) + (b.trust || 0);
      }
    }
    return 0;
  }

  /**
   * 获取已达成里程碑的数量（仅 boolean 型）。
   * @returns {number}
   */
  getAchievedMilestoneCount() {
    return Object.entries(MILESTONE_DEFS)
      .filter(([name]) => this.milestones[name])
      .length;
  }

  /**
   * 获取里程碑总数（仅 boolean 型）。
   * @returns {number}
   */
  static getTotalMilestoneCount() {
    return Object.keys(MILESTONE_DEFS).length;
  }

  // ---------------------------------------------------------------
  // 等级变化
  // ---------------------------------------------------------------

  /**
   * 检查当前指标是否满足下一等级的提升条件。
   * @returns {boolean}
   */
  shouldLevelUp() {
    const thresholds = LEVEL_UP_THRESHOLDS[this.level + 1];
    if (!thresholds) return false;
    return this._checkThresholds(thresholds, thresholds.mode);
  }

  /**
   * 手动提升一级（通常在 shouldLevelUp() 返回 true 时调用）。
   * 已达最高等级 (5) 则无效果。
   * @returns {Object} { success, newLevel, levelKey }
   */
  levelUp() {
    if (this.level >= 5) {
      return { success: false, newLevel: this.level, levelKey: this.getLevelKey() };
    }
    this.level += 1;
    this._lowMetricsCount = 0;
    return { success: true, newLevel: this.level, levelKey: this.getLevelKey() };
  }

  /**
   * 手动降低一级（当指标持续过低时调用）。
   * 已达最低等级 (0) 则无效果。
   * @returns {Object} { success, newLevel, levelKey }
   */
  levelDown() {
    if (this.level <= 0) {
      return { success: false, newLevel: this.level, levelKey: this.getLevelKey() };
    }
    this.level -= 1;
    this._lowMetricsCount = 0;
    return { success: true, newLevel: this.level, levelKey: this.getLevelKey() };
  }

  // ---------------------------------------------------------------
  // 行为约束
  // ---------------------------------------------------------------

  /**
   * 获取当前等级下允许/禁止的动作掩码数组。
   * mask[i] === 1 表示动作 i 允许，0 表示禁止。
   *
   * RL agent 应将此数组传递给决策逻辑作为 validActions 参数，
   * 确保智能体不会选择当前关系等级下不允许的动作。
   *
   * @returns {number[]} 长度为 ACTION_COUNT 的 0/1 数组（副本）
   */
  getBehaviorRestrictions() {
    return [...BEHAVIOR_MASKS[this.level]];
  }

  /**
   * 检查指定动作在当前等级是否允许。
   * @param {number|string} action — 动作索引或名称
   * @returns {boolean}
   */
  isActionAllowed(action) {
    let idx;
    if (typeof action === 'number') {
      idx = action;
    } else {
      idx = ACTIONS.indexOf(action);
    }
    if (idx < 0 || idx >= ACTION_COUNT) return false;
    return BEHAVIOR_MASKS[this.level][idx] === 1;
  }

  /**
   * 获取当前等级下允许的所有动作索引列表。
   * @returns {number[]}
   */
  getAllowedActions() {
    const mask = BEHAVIOR_MASKS[this.level];
    const allowed = [];
    for (let i = 0; i < mask.length; i++) {
      if (mask[i] === 1) allowed.push(i);
    }
    return allowed;
  }

  /**
   * 获取当前等级下禁止的所有动作索引列表。
   * @returns {number[]}
   */
  getBlockedActions() {
    const mask = BEHAVIOR_MASKS[this.level];
    const blocked = [];
    for (let i = 0; i < mask.length; i++) {
      if (mask[i] === 0) blocked.push(i);
    }
    return blocked;
  }

  // ---------------------------------------------------------------
  // 序列化 / 反序列化
  // ---------------------------------------------------------------

  /**
   * 导出完整状态（用于持久化存档，如 IndexedDB / localStorage）。
   * @returns {Object} 可 JSON 序列化的状态对象
   */
  serialize() {
    return {
      version: 1,
      level: this.level,
      metrics: { ...this.metrics },
      milestones: { ...this.milestones },
      affectionHistory: [...this._affectionHistory],
      lowMetricsCount: this._lowMetricsCount
    };
  }

  /**
   * 从序列化数据恢复完整状态。
   * 自动 clamp 各指标至合法范围，兼容部分缺失字段。
   * @param {Object} data — 由 serialize() 输出的数据
   */
  deserialize(data) {
    if (!data || typeof data !== 'object') return;

    this.level = clamp(data.level ?? 0, 0, 5);

    if (data.metrics) {
      this.metrics.intimacy = clamp(
        data.metrics.intimacy ?? 0, INTIMACY_MIN, INTIMACY_MAX
      );
      this.metrics.trust = clamp(
        data.metrics.trust ?? 0, TRUST_MIN, TRUST_MAX
      );
      this.metrics.interactionQuality = clamp(
        data.metrics.interactionQuality ?? 0, QUALITY_MIN, QUALITY_MAX
      );
    }

    if (data.milestones) {
      this.milestones = { ...DEFAULT_MILESTONES, ...data.milestones };
    }

    this._affectionHistory = Array.isArray(data.affectionHistory)
      ? data.affectionHistory.slice(-TREND_WINDOW_SIZE)
      : [];

    this._lowMetricsCount = data.lowMetricsCount ?? 0;
  }

  // ---------------------------------------------------------------
  // 重置
  // ---------------------------------------------------------------

  /** 将所有状态重置为初始默认值。 */
  reset() {
    this.level = 0;
    this.metrics = {
      intimacy: 0,
      trust: 0,
      interactionQuality: 0
    };
    this.milestones = { ...DEFAULT_MILESTONES };
    this._affectionHistory = [];
    this._lowMetricsCount = 0;
  }

  // ---------------------------------------------------------------
  // 内部方法
  // ---------------------------------------------------------------

  /**
   * 计算综合情感值：affection = intimacy * 0.4 + trust * 0.3 + interactionQuality * 0.3
   * @returns {number} 范围 [-100, 100]
   * @private
   */
  _calcAffection() {
    return clamp(
      this.metrics.intimacy * 0.4 +
      this.metrics.trust * 0.3 +
      this.metrics.interactionQuality * 0.3,
      INTIMACY_MIN, INTIMACY_MAX
    );
  }

  /**
   * 计算好感度短期趋势。
   * 基于 TREND_WINDOW_SIZE 滑动窗口内的采样值，将窗口分为
   * 前后两半，比较平均值之差来判断趋势方向。
   *
   * @returns {'rising' | 'stable' | 'declining'}
   * @private
   */
  _calcAffectionTrend() {
    const h = this._affectionHistory;
    if (h.length < 2) return 'stable';

    const half = Math.floor(h.length / 2);
    const firstAvg = h.slice(0, half).reduce((a, b) => a + b, 0) / half;
    const secondAvg = h.slice(half).reduce((a, b) => a + b, 0) / (h.length - half);

    const diff = secondAvg - firstAvg;
    const threshold = 1.5;

    if (diff > threshold) return 'rising';
    if (diff < -threshold) return 'declining';
    return 'stable';
  }

  /**
   * 检查当前状态是否满足一组阈值条件。
   *
   * 支持的字段：
   *   intimacy, trust, affection — 连续指标对比
   *   interactions, sessions     — 里程碑计数对比
   *   milestone                  — 要求特定里程碑已达成
   *
   * @param {Object} thresholds  阈值对象
   * @param {string} mode        'and' | 'or'
   * @returns {boolean}
   * @private
   */
  _checkThresholds(thresholds, mode = 'and') {
    const results = [];

    if (thresholds.intimacy !== undefined) {
      results.push(this.metrics.intimacy >= thresholds.intimacy);
    }
    if (thresholds.trust !== undefined) {
      results.push(this.metrics.trust >= thresholds.trust);
    }
    if (thresholds.affection !== undefined) {
      results.push(this._calcAffection() >= thresholds.affection);
    }
    if (thresholds.interactions !== undefined) {
      results.push(this.milestones.total_interactions >= thresholds.interactions);
    }
    if (thresholds.sessions !== undefined) {
      results.push(this.milestones.total_sessions >= thresholds.sessions);
    }
    if (thresholds.milestone !== undefined) {
      results.push(this.milestones[thresholds.milestone] === true);
    }

    if (results.length === 0) return false;

    return mode === 'or' ? results.some(Boolean) : results.every(Boolean);
  }

  /**
   * 等级过渡核心逻辑。
   *
   * 等级提升：根据各等级定义的模式（'and' | 'or'）逐级向上检查，
   * 满足条件即升级，并继续检查是否可连续升级。
   *
   * 等级下降：当所有关键指标均持续低于当前等级阈值的 60% 时，
   * 下降一级。需要连续多次检查确认以平滑波动。
   *
   * 在 updateMetrics / triggerMilestone / endSession 后自动调用。
   * @private
   */
  _checkLevelTransition() {
    // ---- 等级提升 ----
    // 从当前等级开始向上逐级检查（可连续升级）
    let canLevelUp = true;
    while (canLevelUp && this.level < 5) {
      const nextLevel = this.level + 1;
      const thresholds = LEVEL_UP_THRESHOLDS[nextLevel];
      if (!thresholds) break;

      if (this._checkThresholds(thresholds, thresholds.mode)) {
        this.level = nextLevel;
        this._lowMetricsCount = 0;  // 升级后重置降级计数
        // 继续检查下一级
      } else {
        canLevelUp = false;
      }
    }

    // ---- 等级下降 ----
    if (this.level > 0) {
      const current = LEVEL_UP_THRESHOLDS[this.level];
      if (current) {
        // 构建降级条件：所有可比较指标均低于 60% 阈值
        const downCheck = {};
        for (const [key, val] of Object.entries(current)) {
          if (key === 'mode' || key === 'milestone') continue;
          if (typeof val === 'number') {
            downCheck[key] = val * LEVEL_DOWN_FACTOR;
          }
        }

        // 降级采用 AND 模式：所有指标都必须低于阈值才考虑降级
        const shouldDown = Object.keys(downCheck).length > 0 &&
          !this._checkThresholds(downCheck, 'and');

        if (shouldDown) {
          this._lowMetricsCount += 1;
          if (this._lowMetricsCount >= LEVEL_DOWN_CONSECUTIVE_NEEDED) {
            this.level = Math.max(0, this.level - 1);
            this._lowMetricsCount = 0;
          }
        } else if (this._lowMetricsCount > 0) {
          // 指标有所恢复，逐步递减计数
          this._lowMetricsCount = Math.max(0, this._lowMetricsCount - 1);
        }
      }
    }
  }
}

// ==================== 工具函数 ====================

/**
 * 将数值限制在指定闭区间内。
 * @param {number} val
 * @param {number} min
 * @param {number} max
 * @returns {number}
 */
function clamp(val, min, max) {
  return Math.max(min, Math.min(max, val));
}

// ==================== 默认导出 ====================

export default DatingRelationshipModel;
