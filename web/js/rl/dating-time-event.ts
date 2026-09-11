/**
 * 约会模拟强化学习 - 时间模式学习器与事件记忆系统
 *
 * 1. TimePatternLearner - 纯数据驱动，学习用户时间交互模式
 * 2. EventMemory - 短期（滑动窗口）+ 长期（重要事件）记忆管理
 */

// ============================================================
// Part 1: TimePatternLearner
// ============================================================

/**
 * 时间模式学习器
 * 从观测数据中学习用户的活跃时段、会话模式、响应速度等，
 * 全部基于指数移动平均(EMA)，无硬编码规则。
 */
export class TimePatternLearner {
  declare activeHours: Float64Array;
  declare sessionPatterns: {
    avgDuration: number;
    avgInterval: number;
    sessionCount: number;
    lastSessionStart: number;
    lastSessionEnd: number;
  };
  declare responseTime: { avgResponseMs: number; samples: number; lastResponseMs: number };
  declare dayOfWeek: Float64Array;
  declare moodByTimeOfDay: { morning: number; afternoon: number; evening: number; night: number };
  declare _responseSamples: number[];
  declare _maxSamples: number;
  declare _lastInteractionTime: number;
  declare _totalInteractions: number;

  constructor() {
    /** 24小时活跃度分布（归一化 0~1） */
    this.activeHours = new Float64Array(24);
    /** 会话模式 */
    this.sessionPatterns = {
      avgDuration: 0,      // 平均会话时长（ms）
      avgInterval: 0,      // 平均交互间隔（ms）
      sessionCount: 0,     // 会话总次数
      lastSessionStart: 0, // 上次会话开始
      lastSessionEnd: 0    // 上次会话结束
    };
    /** 响应时间统计（ms） */
    this.responseTime = {
      avgResponseMs: 0,
      samples: 0,
      lastResponseMs: 0
    };
    /** 星期活跃度分布（0~6） */
    this.dayOfWeek = new Float64Array(7);
    /** 时段情绪/参与度关联 */
    this.moodByTimeOfDay = {
      morning: 0,    // 6~12
      afternoon: 0,  // 12~18
      evening: 0,    // 18~22
      night: 0       // 22~6
    };

    this._responseSamples = [];       // 响应时间样本（百分位估算用）
    this._maxSamples = 100;
    this._lastInteractionTime = 0;
    this._totalInteractions = 0;
  }

  /** 记录一次交互 */
  recordInteraction(timestamp, responseTimeMs) {
    const date = new Date(timestamp);
    const hour = date.getHours();
    const day = date.getDay();
    const alpha = 0.15;

    // 更新当前小时活跃度
    this.activeHours[hour] += alpha * (1 - this.activeHours[hour]);
    // 衰减其他小时
    for (let i = 0; i < 24; i++) {
      if (i !== hour) this.activeHours[i] *= (1 - alpha * 0.1);
    }

    // 更新星期活跃度
    this.dayOfWeek[day] += alpha * (1 - this.dayOfWeek[day]);

    // 更新时段情绪
    const period = this._getPeriod(hour);
    this.moodByTimeOfDay[period] += alpha * (1 - this.moodByTimeOfDay[period]);

    // 更新响应时间
    if (responseTimeMs > 0) {
      this.responseTime.lastResponseMs = responseTimeMs;
      this.responseTime.samples++;
      const beta = 1 / Math.min(this.responseTime.samples, 20);
      this.responseTime.avgResponseMs += beta * (responseTimeMs - this.responseTime.avgResponseMs);
      this._responseSamples.push(responseTimeMs);
      if (this._responseSamples.length > this._maxSamples) this._responseSamples.shift();
    }

    // 更新交互间隔
    if (this._lastInteractionTime > 0) {
      const interval = timestamp - this._lastInteractionTime;
      if (this.sessionPatterns.sessionCount > 0) {
        const gamma = 1 / Math.min(this.sessionPatterns.sessionCount + 1, 20);
        this.sessionPatterns.avgInterval += gamma * (interval - this.sessionPatterns.avgInterval);
      } else {
        this.sessionPatterns.avgInterval = interval;
      }
    }

    this._lastInteractionTime = timestamp;
    this._totalInteractions++;
  }

  /** 记录会话开始 */
  recordSessionStart(timestamp) {
    this.sessionPatterns.lastSessionStart = timestamp;
    this.sessionPatterns.sessionCount++;
  }

  /** 记录会话结束，计算时长 */
  recordSessionEnd(timestamp) {
    if (this.sessionPatterns.lastSessionStart === 0) return;
    const duration = timestamp - this.sessionPatterns.lastSessionStart;
    this.sessionPatterns.lastSessionEnd = timestamp;
    if (this.sessionPatterns.sessionCount > 1) {
      const a = 1 / Math.min(this.sessionPatterns.sessionCount, 20);
      this.sessionPatterns.avgDuration += a * (duration - this.sessionPatterns.avgDuration);
    } else {
      this.sessionPatterns.avgDuration = duration;
    }
  }

  /** 获取某小时活跃度 0~1 */
  getActiveScore(hour) {
    if (hour < 0 || hour > 23) return 0;
    return Math.max(0, Math.min(1, this.activeHours[hour]));
  }

  /** 查找活跃度最高的小时 */
  getOptimalContactHour() {
    let best = 12, bestScore = -1;
    for (let i = 0; i < 24; i++) {
      if (this.activeHours[i] > bestScore) {
        bestScore = this.activeHours[i];
        best = i;
      }
    }
    return best;
  }

  /** 估算 p 百分位的响应时间（ms） */
  getResponseTimePercentile(p) {
    if (this._responseSamples.length === 0) return this.responseTime.avgResponseMs || 30000;
    const sorted = [...this._responseSamples].sort((a, b) => a - b);
    const idx = Math.ceil((p / 100) * sorted.length) - 1;
    return sorted[Math.max(0, Math.min(idx, sorted.length - 1))];
  }

  /** 判断当前是否应等待（用户可能休息/忙碌） */
  shouldWait(timestamp) {
    const hour = new Date(timestamp).getHours();
    const score = this.getActiveScore(hour);
    if (score < 0.15) return true;
    if (hour >= 23 || hour < 6) return score < 0.2;
    return false;
  }

  /** 编码时间上下文特征（9维，用于RL状态） */
  getTimeContextFeatures() {
    const now = Date.now();
    const d = new Date(now);
    const hour = d.getHours();
    const day = d.getDay();

    // 会话进度
    let sp = 0;
    if (this.sessionPatterns.lastSessionStart > 0 &&
        this.sessionPatterns.lastSessionEnd < this.sessionPatterns.lastSessionStart) {
      const elapsed = now - this.sessionPatterns.lastSessionStart;
      sp = this.sessionPatterns.avgDuration > 0
        ? Math.min(1, elapsed / this.sessionPatterns.avgDuration) : 0;
    }

    const tsi = this._lastInteractionTime > 0
      ? (now - this._lastInteractionTime) / 3600000 : 24;
    const asd = this.sessionPatterns.avgDuration / 3600000;
    const aiv = this.sessionPatterns.avgInterval / 3600000;
    const rtn = Math.min(1, this.responseTime.avgResponseMs / 60000);
    const opt = hour === this.getOptimalContactHour() ? 1 : 0;

    return [
      hour / 24,                    // 0. 当前小时归一化
      day / 7,                      // 1. 星期归一化
      this.getActiveScore(hour),    // 2. 当前活跃度
      sp,                           // 3. 会话进度
      Math.min(48, tsi),            // 4. 距上次交互（h），上限48h
      Math.min(2, asd),             // 5. 平均会话时长（h），上限2h
      Math.min(24, aiv),            // 6. 平均间隔（h），上限24h
      rtn,                          // 7. 响应时间归一化
      opt                           // 8. 是否最佳时段
    ];
  }

  serialize() {
    return {
      activeHours: Array.from(this.activeHours),
      sessionPatterns: { ...this.sessionPatterns },
      responseTime: { ...this.responseTime },
      dayOfWeek: Array.from(this.dayOfWeek),
      moodByTimeOfDay: { ...this.moodByTimeOfDay },
      _responseSamples: [...this._responseSamples],
      _lastInteractionTime: this._lastInteractionTime,
      _totalInteractions: this._totalInteractions
    };
  }

  deserialize(data) {
    if (!data) return;
    if (data.activeHours) this.activeHours = new Float64Array(data.activeHours);
    if (data.sessionPatterns) Object.assign(this.sessionPatterns, data.sessionPatterns);
    if (data.responseTime) Object.assign(this.responseTime, data.responseTime);
    if (data.dayOfWeek) this.dayOfWeek = new Float64Array(data.dayOfWeek);
    if (data.moodByTimeOfDay) Object.assign(this.moodByTimeOfDay, data.moodByTimeOfDay);
    if (data._responseSamples) this._responseSamples = [...data._responseSamples];
    if (data._lastInteractionTime !== undefined) this._lastInteractionTime = data._lastInteractionTime;
    if (data._totalInteractions !== undefined) this._totalInteractions = data._totalInteractions;
  }

  /** 获取时间时段名称 */
  _getPeriod(hour) {
    if (hour >= 6 && hour < 12) return 'morning';
    if (hour >= 12 && hour < 18) return 'afternoon';
    if (hour >= 18 && hour < 22) return 'evening';
    return 'night';
  }
}


// ============================================================
// Part 2: EventMemory
// ============================================================

/** 一条记忆条目 */
interface MemoryEntry {
  type: string;
  timestamp: number;
  userSentiment: number;
  aiAction: any;
  reward: number;
  context: any;
  emotionalWeight: number;
}

/**
 * 事件记忆系统
 * 管理短期记忆（滑动窗口）和长期记忆（重要事件持久保留），
 * 自动检测显著性并升级记忆。
 */
export class EventMemory {
  declare capacityShortTerm: number;
  declare capacityLongTerm: number;
  declare shortTerm: MemoryEntry[];
  declare longTerm: MemoryEntry[];
  declare milestones: MemoryEntry[];
  declare _firstEventTime: number;
  declare _typeCounts: Record<string, number>;
  declare _totalEvents: number;
  declare _recentTimestamps: number[];

  /**
   * @param {number} shortCap - 短期记忆容量（默认20）
   * @param {number} longCap  - 长期记忆容量（默认100）
   */
  constructor(shortCap = 20, longCap = 100) {
    this.capacityShortTerm = shortCap;
    this.capacityLongTerm = longCap;
    this.shortTerm = [];       // 短期记忆（滑动窗口）
    this.longTerm = [];        // 长期记忆（重要事件）
    this.milestones = [];      // 关系里程碑
    this._firstEventTime = 0;
    this._typeCounts = {};
    this._totalEvents = 0;
    this._recentTimestamps = []; // 用于计算事件率
  }

  /**
   * 记录一条事件
   * @param {string}  type          - 事件类型
   * @param {number}  userSentiment - 用户情感 -1~1
   * @param {*}       aiAction      - AI 动作
   * @param {number}  reward        - 奖励值
   * @param {object}  [context]     - 上下文
   */
  record(type, userSentiment, aiAction, reward, context = {}) {
    const ts = Date.now();
    const ew = Math.abs(userSentiment) * 0.6 + Math.min(Math.abs(reward) / 10, 1) * 0.4;

    const entry = {
      type,
      timestamp: ts,
      userSentiment: Math.max(-1, Math.min(1, userSentiment)),
      aiAction,
      reward,
      context,
      emotionalWeight: ew
    };

    // 短期记忆
    this.shortTerm.push(entry);
    if (this.shortTerm.length > this.capacityShortTerm) this.shortTerm.shift();

    // 显著性检测 → 升入长期记忆
    if (this._isSignificant(entry)) {
      this.longTerm.push(entry);
      if (this.longTerm.length > this.capacityLongTerm) this.longTerm.shift();
    }

    // 里程碑单独存储
    if (type === 'milestone') this.milestones.push(entry);

    // 统计数据
    if (!this._firstEventTime) this._firstEventTime = ts;
    this._typeCounts[type] = (this._typeCounts[type] || 0) + 1;
    this._totalEvents++;

    // 时间戳记录（用于事件率）
    this._recentTimestamps.push(ts);
    const cutoff = ts - 3600000;
    this._recentTimestamps = this._recentTimestamps.filter(t => t > cutoff);
    if (this._recentTimestamps.length > 100) this._recentTimestamps.length = 100;
  }

  /**
   * 显著性检测（自动升级条件）
   */
  _isSignificant(e) {
    if (Math.abs(e.userSentiment) > 0.7) return true;     // 强烈情感
    if (e.reward > 5 || e.reward < -3) return true;       // 高影响奖励
    if (e.type === 'milestone') return true;               // 里程碑
    if (!this._typeCounts[e.type]) return true;            // 首次发生类型
    return false;
  }

  /** 获取最近 n 条事件 */
  getRecentEvents(n = 5) {
    return this.shortTerm.slice(-Math.min(n, this.shortTerm.length));
  }

  /** 获取重要记忆（情感权重>0.5） */
  getSignificantMemories() {
    return this.longTerm.filter(e => e.emotionalWeight > 0.5);
  }

  /** 编码记忆特征（12维，用于RL状态） */
  getMemoryFeatures() {
    const st = this.shortTerm;
    const n = st.length;
    const pos = st.filter(e => e.userSentiment > 0.3).length;
    const neg = st.filter(e => e.userSentiment < -0.3).length;
    const avgSent = n > 0 ? st.reduce((s, e) => s + e.userSentiment, 0) / n : 0;
    const diversity = Math.min(1, new Set(st.map(e => e.type)).size / 5);
    const last = st[n - 1];

    const typeMap = { message: 1, gift: 2, date: 3, compliment: 4, story: 5, question: 6, milestone: 7 };
    const lastType = last ? (typeMap[last.type] || 0) : 0;
    const lastSent = last ? last.userSentiment : 0;
    const lastRew = last ? Math.max(-1, Math.min(1, last.reward / 10)) : 0;
    const sigN = this.longTerm.length / 20;
    const daysSince = this._firstEventTime > 0 ? (Date.now() - this._firstEventTime) / 86400000 : 0;

    // 记忆一致性：情感标准差倒数（越小越稳定→1）
    let consistency = 0;
    if (n >= 2) {
      const v = st.reduce((s, e) => s + (e.userSentiment - avgSent) ** 2, 0) / n;
      const std = Math.sqrt(v);
      consistency = Math.min(1, std > 0 ? 0.5 / std : 1);
    } else if (n === 1) {
      consistency = 1;
    }

    return [
      pos / 5,                        // 0. 最近正面数
      neg / 5,                        // 1. 最近负面数
      avgSent,                         // 2. 最近平均情感
      diversity,                       // 3. 记忆多样性
      lastType / 7,                    // 4. 上次交互类型编码
      lastSent,                        // 5. 上次情感
      lastRew,                         // 6. 上次奖励归一化
      Math.min(1, sigN),              // 7. 重要记忆数
      Math.min(30, daysSince) / 30,   // 8. 距首次天数
      Math.min(1, this._totalEvents / 100), // 9. 总事件数
      Math.min(10, this._recentTimestamps.length) / 10, // 10. 近期事件率（次/h）
      consistency                     // 11. 记忆一致性
    ];
  }

  /** 随机召回升入长期记忆 */
  recallRandomMemory() {
    if (this.longTerm.length === 0) return null;
    return this.longTerm[Math.floor(Math.random() * this.longTerm.length)];
  }

  /** 按类型召回（长+短） */
  recallByType(type) {
    return [
      ...this.longTerm.filter(e => e.type === type),
      ...this.shortTerm.filter(e => e.type === type)
    ];
  }

  /** 按情感阈值召回（|sentiment| >= threshold） */
  recallWithSentiment(threshold) {
    return [
      ...this.longTerm.filter(e => Math.abs(e.userSentiment) >= threshold),
      ...this.shortTerm.filter(e => Math.abs(e.userSentiment) >= threshold)
    ];
  }

  /** 获取近期情感基线 */
  getEmotionalBaseline() {
    const r = this.getRecentEvents(10);
    return r.length > 0 ? r.reduce((s, e) => s + e.userSentiment, 0) / r.length : 0;
  }

  /** 获取情绪语境 */
  getMoodContext() {
    const b = this.getEmotionalBaseline();
    if (b > 0.2) return 'positive';
    if (b < -0.2) return 'negative';
    return 'neutral';
  }

  serialize() {
    return {
      capacityShortTerm: this.capacityShortTerm,
      capacityLongTerm: this.capacityLongTerm,
      shortTerm: [...this.shortTerm],
      longTerm: [...this.longTerm],
      milestones: [...this.milestones],
      _firstEventTime: this._firstEventTime,
      _typeCounts: { ...this._typeCounts },
      _totalEvents: this._totalEvents,
      _recentTimestamps: [...this._recentTimestamps]
    };
  }

  deserialize(data) {
    if (!data) return;
    if (data.capacityShortTerm !== undefined) this.capacityShortTerm = data.capacityShortTerm;
    if (data.capacityLongTerm !== undefined) this.capacityLongTerm = data.capacityLongTerm;
    if (data.shortTerm) this.shortTerm = [...data.shortTerm];
    if (data.longTerm) this.longTerm = [...data.longTerm];
    if (data.milestones) this.milestones = [...data.milestones];
    if (data._firstEventTime !== undefined) this._firstEventTime = data._firstEventTime;
    if (data._typeCounts) this._typeCounts = { ...data._typeCounts };
    if (data._totalEvents !== undefined) this._totalEvents = data._totalEvents;
    if (data._recentTimestamps) this._recentTimestamps = [...data._recentTimestamps];
  }

  /** 清空所有记忆 */
  clear() {
    this.shortTerm = [];
    this.longTerm = [];
    this.milestones = [];
    this._firstEventTime = 0;
    this._typeCounts = {};
    this._totalEvents = 0;
    this._recentTimestamps = [];
  }
}
