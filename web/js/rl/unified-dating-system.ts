/* ============================================================
 * UnifiedDatingSystem — RL统摄的统一恋爱养成Agent系统
 *
 * 整合 DatingRLSystem + EngagementRLAgent 为单一系统：
 * - 统一状态编码器（64维，覆盖所有游戏/非游戏模式）
 * - 统一动作空间（分层：高层策略+底层执行）
 * - 三层奖励函数（外源+好奇心内源+社会意识）
 * - 动态权重自适应（α, β, γ 随关系阶段自动调整）
 * - 好奇心引擎（ICM前向预测 + RND新颖性评分）
 * - 层级化Prompt体系（代替硬编码消息模板）
 * - CTDE训练架构
 *
 * 参考：DuplexPO / ProActor / ICM / RND / UnityMAS-O / MASPO
 * ============================================================ */

import { UnifiedRLAgent } from './unified-rl-agent.ts';
import { DatingRelationshipModel, ACTIONS as REL_ACTIONS, ACTION_COUNT } from './dating-relationship-model.ts';
import { TimePatternLearner, EventMemory } from './dating-time-event.ts';

// ==================== 常量 ====================

/** 统一状态编码维度 */
export const UNIFIED_STATE_DIM = 64;

/** 动作空间维度 */
export const ACTION_DIMS = {
  HIGH_LEVEL_PROACTIVITY: 0,   // 连续 [0,1] — 主动性等级
  HIGH_LEVEL_TIMING: 1,        // 连续 — 等待时间(s)
  HIGH_LEVEL_MODE_SWITCH: 2,   // 离散 0-8 — 模式切换
  HIGH_LEVEL_SCENARIO: 3,      // 离散 0-5 — 场景策略
  CONTENT_TYPE: 4,             // 离散 0-7 — 内容类型
  RELATION_ACTION: 5,          // 离散 0-16 — 关系动作
  ENGAGEMENT_ACTION: 6,        // 离散 0-10 — 互动行为
};

/** 内容类型枚举 */
export const CONTENT_TYPES = Object.freeze([
  'greeting',    // 0
  'sharing',     // 1
  'invitation',  // 2
  'humor',       // 3
  'empathy',     // 4
  'knowledge',   // 5
  'reminder',    // 6
  'silence',     // 7
]);

/** 模式枚举 */
export const MODE_TYPES = Object.freeze([
  'daily_companion',   // 0 日常陪伴
  'approach_game',     // 1 搭讪模式
  'date_game',         // 2 约会模式
  'knowledge_qa',      // 3 知识问答
  'emotional_support', // 4 情感支持
  'task_planning',     // 5 任务规划
  'creative_play',     // 6 创意互动
  'gift_system',       // 7 礼物系统
  'personal_growth',   // 8 个人成长
]);

/** 场景策略枚举 */
export const SCENARIO_STRATEGIES = Object.freeze([
  'explore',           // 0 探索模式（高中好奇）
  'deepen',            // 1 深化模式（建立深度连接）
  'maintain',          // 2 维护模式（保持关系温度）
  'repair',            // 3 修复模式（矛盾处理）
  'advance',           // 4 推进模式（关系升级）
  'respect',           // 5 尊重模式（低主动、高倾听）
]);

/** 动态权重默认值 */
const DEFAULT_WEIGHTS = { alpha: 0.5, beta: 0.3, gamma: 0.2 };

// ==================== 好奇心引擎 ====================

/**
 * 轻量级好奇心引擎（ICM-style）
 * 在前端用状态预测误差模拟内在好奇心奖励
 */
class CuriosityEngine {
  declare featureDim: number;
  declare _predictor: Map<number, number>;
  declare _seenStates: Set<number>;
  declare _stateCounts: Map<number, number>;
  declare _rndFeatures: Float64Array;

  constructor(featureDim = 16) {
    this.featureDim = featureDim;
    // 简易预测器：用线性层近似前向模型
    this._predictor = new Map(); // 状态特征 → 下一特征哈希
    this._seenStates = new Set();
    this._stateCounts = new Map();
    this._rndFeatures = new Float64Array(featureDim);
    // 随机种子
    for (let i = 0; i < featureDim; i++) {
      this._rndFeatures[i] = Math.random() * 2 - 1;
    }
  }

  /**
   * 计算内在好奇心奖励
   * @param {number[]} state - 当前状态向量
   * @param {number[]} nextState - 下一状态向量
   * @returns {number} 好奇心奖励值
   */
  computeIntrinsicReward(state, nextState) {
    // 1. 预测误差奖励 (ICM)
    const predError = this._computePredictionError(state, nextState);
    // 2. 新颖性奖励 (RND-style)
    const novelty = this._computeNovelty(nextState);
    // 3. 状态计数奖励（伪计数）
    const countBonus = this._computeCountBonus(nextState);
    return 0.5 * predError + 0.3 * novelty + 0.2 * countBonus;
  }

  /** 记录经验以便后续学习 */
  recordTransition(state, nextState) {
    const key = this._hashState(state);
    const nextKey = this._hashState(nextState);
    this._predictor.set(key, nextKey);
    this._seenStates.add(nextKey);
    this._stateCounts.set(nextKey, (this._stateCounts.get(nextKey) || 0) + 1);
  }

  _computePredictionError(state, nextState) {
    const key = this._hashState(state);
    const predictedNextKey = this._predictor.get(key);
    if (!predictedNextKey) return 0.5; // 首次见 → 中等好奇
    const actualNextKey = this._hashState(nextState);
    // 汉明距离作为预测误差
    let diff = 0;
    for (let i = 0; i < state.length && i < 20; i++) {
      diff += Math.abs(state[i] - nextState[i]);
    }
    return Math.min(1.0, diff / state.length);
  }

  _computeNovelty(state) {
    const key = this._hashState(state);
    if (!this._seenStates.has(key)) return 0.8; // 全新状态→高好奇
    // 用RND风格：预测该状态在随机投影下的误差
    let proj = 0;
    for (let i = 0; i < Math.min(state.length, this.featureDim); i++) {
      proj += state[i] * this._rndFeatures[i];
    }
    const predicted = proj / Math.min(state.length, this.featureDim);
    // 越见过的状态预测误差越小
    const count = this._stateCounts.get(key) || 1;
    return Math.max(0, 0.5 - predicted * 0.3) / Math.min(count, 10);
  }

  _computeCountBonus(state) {
    const key = this._hashState(state);
    const count = this._stateCounts.get(key) || 0;
    // 伪计数奖励：越少访问越高
    return Math.max(0, 0.3 - 0.03 * count);
  }

  _hashState(state) {
    let h = 0;
    for (let i = 0; i < Math.min(state.length, 16); i++) {
      h = ((h << 5) - h) + Math.round(state[i] * 100);
      h |= 0;
    }
    return h;
  }
}

// ==================== 统一状态编码器 ====================

export class UnifiedStateEncoder {
  /**
   * 将分散的系统状态编码为64维统一向量
   * @param {Object} ctx - 上下文对象
   * @param {DatingRelationshipModel} ctx.relationship
   * @param {TimePatternLearner} ctx.timePattern
   * @param {EventMemory} ctx.eventMemory
   * @param {Object} ctx.userState - 用户状态（来自App）
   * @param {Object} ctx.internal - 内部状态（timer, session）
   * @returns {Float64Array} 64维归一化向量
   */
  static encode(ctx) {
    const s = new Float64Array(UNIFIED_STATE_DIM);
    const rel = ctx.relationship;
    const tp = ctx.timePattern;
    const mem = ctx.eventMemory;
    const usr = ctx.userState || {};
    const intl = ctx.internal || {};
    const now = Date.now();

    // ===== [0-9] 关系状态 =====
    const metrics = rel.getMetrics();
    s[0] = rel.getLevel() / 5;                          // 关系等级
    s[1] = metrics.intimacy / 100;                      // 亲密度
    s[2] = metrics.trust / 100;                         // 信任度
    s[3] = metrics.affection / 100;                     // 综合情感
    s[4] = Math.max(-1, metrics.interactionQuality / 50); // 互动质量
    s[5] = metrics.affectionTrend === 'rising' ? 1 :
           metrics.affectionTrend === 'declining' ? 0 : 0.5; // 趋势
    s[6] = rel.getAchievedMilestoneCount() / 9;          // 里程碑进度
    s[7] = Math.min(rel.milestones.total_interactions / 100, 1);
    s[8] = Math.min(rel.milestones.total_sessions / 50, 1);

    // ===== [9-15] 时间上下文 =====
    const tf = tp.getTimeContextFeatures();
    for (let i = 0; i < 9; i++) s[9 + i] = tf[i];

    // ===== [18-28] 记忆特征 =====
    const mf = mem.getMemoryFeatures();
    for (let i = 0; i < 12; i++) s[18 + i] = mf[i];

    // ===== [30-39] 用户实时状态 =====
    const secSinceMsg = (now - (usr.lastUserMessageTime || 0)) / 1000;
    const secSinceInteract = (now - (usr.lastUserInteractTime || 0)) / 1000;
    s[30] = usr.isListening ? 1 : 0;
    s[31] = usr.isSpeaking ? 1 : 0;
    s[32] = usr.isGameMode ? 1 : 0;
    s[33] = Math.min(1, secSinceMsg / 300);
    s[34] = Math.min(1, secSinceInteract / 120);
    s[35] = usr.isGyroEnabled ? 1 : 0;
    s[36] = usr.isLocked ? 1 : 0;
    s[37] = Math.min(1, (intl.sessionDuration || 0) / 3600);

    // ===== [38-44] 内部状态 =====
    s[38] = Math.min(1, (intl.actionTimer || 0) / 30);
    s[39] = intl.lastAction >= 0 ? intl.lastAction / 16 : 0.5;
    s[40] = Math.min(1, (now - (intl.lastProactiveTime || 0)) / 300000);
    const hour = new Date().getHours();
    s[41] = Math.sin(hour * Math.PI / 12);
    s[42] = Math.cos(hour * Math.PI / 12);
    s[43] = intl.proactiveRate || 0;
    s[44] = Math.min(1, (intl.sessionInteractionCount || 0) / 20);

    // ===== [45-49] 互动动力学 =====
    s[45] = Math.min(1, mem._recentTimestamps.length / 20);
    s[46] = Math.min(1, (intl.sessionInteractionCount || 0) / 20);
    s[47] = Math.min(1, secSinceInteract / 120);
    const attempts = intl.proactiveAttempts || 1;
    s[48] = (intl.proactiveSuccesses || 0) / attempts;
    s[49] = secSinceInteract < 1800 ? 1 : 0;

    // ===== [50-54] 好奇心状态 =====
    s[50] = Math.min(1, mem._totalEvents / 100);       // 事件总量
    s[51] = mem._recentTimestamps.length > 0 ? 
            Math.min(1, (now - mem._recentTimestamps[mem._recentTimestamps.length - 1]) / 3600000) : 1;
    s[52] = mem.longTerm.length / 20;                  // 长期记忆饱和度
    s[53] = mem.shortTerm.length / 20;                 // 短期记忆饱和度
    s[54] = intl.curiosityBias || 0.5;                 // 当前好奇偏置

    // ===== [55-59] 多模式融合 =====
    s[55] = usr.currentMode !== undefined ? usr.currentMode / 8 : 0.5;
    s[56] = usr.desiredMode !== undefined ? usr.desiredMode / 8 : 0.5;
    let activeScore = 0;
    try { activeScore = tp.getActiveScore(hour); } catch(e) { activeScore = 0.5; }
    s[57] = activeScore;
    s[58] = tp.shouldWait(now) ? 1 : 0;
    // [59] VR交互信号：上下/左右晃动强度（归一化0-1），供RL统一调度VR反馈时机
    const vr = usr.vrShake || {};
    const vrIntensity = Math.max(vr.upDown || 0, vr.leftRight || 0);
    s[59] = Math.min(1, vrIntensity / 100);

    // ===== [60-63] 随机扰动（防止过拟合） =====
    s[60] = Math.random() * 0.1;
    s[61] = Math.random() * 0.1;
    s[62] = Math.random() * 0.1;
    s[63] = Math.random() * 0.1;

    return s;
  }

  /** 获取状态维度（静态） */
  static getDim() { return UNIFIED_STATE_DIM; }
}

// ==================== 统一动作空间 ====================

export class UnifiedActionSpace {
  /**
   * 将RL策略输出（5维连续/离散联合动作）映射到具体执行动作
   * @param {Object} rawAction - RL策略输出
   * @param {number} rawAction.proactivity - [0,1] 主动性
   * @param {number} rawAction.timing_delta - 等待时间(s)
   * @param {number} rawAction.mode_switch - 模式切换 0-8
   * @param {number} rawAction.content_type - 内容类型 0-7
   * @param {number} rawAction.relation_action - 关系动作 0-16
   * @param {number} rawAction.engagement_action - 互动行为 0-10
   * @returns {Object} 可执行计划
   */
  static resolve(rawAction, level, mode) {
    return {
      proactivity: Math.max(0, Math.min(1, rawAction.proactivity || 0.5)),
      timingDelta: Math.max(0, rawAction.timing_delta || 10),
      mode: rawAction.mode_switch !== undefined ? 
            Math.round(Math.min(8, Math.max(0, rawAction.mode_switch))) : (mode || 0),
      contentType: rawAction.content_type !== undefined ?
            Math.round(Math.min(7, Math.max(0, rawAction.content_type))) : 0,
      relationAction: rawAction.relation_action !== undefined ?
            Math.round(Math.min(16, Math.max(0, rawAction.relation_action))) : 0,
      engagementAction: rawAction.engagement_action !== undefined ?
            Math.round(Math.min(10, Math.max(0, rawAction.engagement_action))) : 0,
    };
  }

  /** 获取动作语义描述 */
  static describe(action) {
    const contentNames = ['问候', '分享', '邀约', '幽默', '共情', '知识', '提醒', '沉默'];
    const modeNames = ['日常陪伴', '搭讪模式', '约会模式', '知识问答', '情感支持',
                       '任务规划', '创意互动', '礼物系统', '个人成长'];
    const relNames = [
      '打招呼','闲聊','赞美','深度提问','分享感受','调侃','调情','关心',
      '昵称','表白','思念','浪漫','内部梗','舒适沉默','送礼','游戏','道歉'
    ];
    const engageNames = [
      '安静站立','随意走动','靠近用户','表达情绪','互动物品','赞美用户',
      '邀请游戏','跳舞表演','好奇探索','打招呼','坐下休息'
    ];

    return {
      proactivity: (action.proactivity * 100).toFixed(0) + '%主动',
      timing: action.timingDelta.toFixed(0) + '秒后行动',
      mode: modeNames[action.mode] || '日常陪伴',
      content: contentNames[action.contentType] || '问候',
      relationAction: relNames[action.relationAction] || '打招呼',
      engagementAction: engageNames[action.engagementAction] || '安静站立',
    };
  }
}

// ==================== 统一奖励函数 ====================

export class UnifiedRewardFunction {
  declare curiosity: CuriosityEngine;

  constructor() {
    this.curiosity = new CuriosityEngine();
  }

  /**
   * 计算统一奖励
   * R_total = α·R_extrinsic + β·R_curiosity + γ·R_social
   */
  compute(state, nextState, action, context: any = {}) {
    const affection = context.affection || 50;
    const weights = this._dynamicWeights(affection);

    const R_e = this._extrinsicReward(state, nextState, action, context);
    const R_i = this.curiosity.computeIntrinsicReward(
      Array.from(state).slice(0, 30),
      Array.from(nextState).slice(0, 30)
    );
    const R_s = this._socialReward(state, nextState, action, context);

    const total = weights.alpha * R_e + weights.beta * R_i + weights.gamma * R_s;

    this.curiosity.recordTransition(
      Array.from(state).slice(0, 30),
      Array.from(nextState).slice(0, 30)
    );

    return { total, extrinsic: R_e, intrinsic: R_i, social: R_s, ...weights };
  }

  /**
   * 动态权重：α(好感度) = 0.3+0.4·sigmoid(x-50)
   * β(好感度) = 0.5-0.4·sigmoid(x-50)
   * γ(好感度) = 0.1+0.3·sigmoid(x-30)
   */
  _dynamicWeights(affection) {
    const sig = (x) => 1 / (1 + Math.exp(-x));
    const a = affection / 100;
    return {
      alpha: 0.3 + 0.4 * sig(a - 0.5),
      beta: 0.5 - 0.4 * sig(a - 0.5),
      gamma: 0.1 + 0.3 * sig(a - 0.3),
    };
  }

  /** 外源奖励：关系进展 + 用户参与 + 任务完成 */
  _extrinsicReward(state, nextState, action, ctx) {
    let r = 0;
    // 关系进展奖励
    r += (nextState[1] - state[1]) * 0.4;   // 亲密度变化
    r += (nextState[2] - state[2]) * 0.3;   // 信任度变化
    r += (nextState[0] - state[0]) * 0.2;   // 等级变化
    // 用户参与奖励
    if (ctx.userResponded) r += 2.0;
    if (ctx.userEngaged) r += 1.5;
    // 互动节奏奖励
    if (ctx.responseTime > 5 && ctx.responseTime < 120) r += 0.2;
    if (ctx.responseTime > 3600) r -= 0.3;
    // 过频繁惩罚
    if (action.proactivity > 0.8 && !ctx.userResponded) r -= 1.0;
    // 行为适当性
    const relationLevel = Math.round(nextState[0] * 5);
    const masks = this._getBehaviorMask(relationLevel);
    if (action.relationAction >= 0 && action.relationAction < masks.length) {
      if (!masks[action.relationAction]) r -= 2.0;
    }
    return r;
  }

  /** 社会意识奖励：信任维护 + 用户自主性尊重 */
  _socialReward(state, nextState, action, ctx) {
    let r = 0;
    r += (nextState[2] - state[2]) * 0.5;  // 信任变化
    // 深夜惩罚
    const hour = new Date().getHours();
    if (hour >= 23 || hour < 6) r -= 0.5;
    // 过度侵入惩罚
    if (action.proactivity > 0.9 && nextState[30] < 0.5) r -= 1.0;
    // 用户自主奖励
    const prevAuto = state[30]; // 用户活跃标志
    const currAuto = nextState[30];
    if (currAuto > prevAuto) r += 0.3;  // 用户主动活跃
    return r;
  }

  _getBehaviorMask(level) {
    const MASKS = [
      [1,1,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0], // 0 陌生人
      [1,1,1,0,0,0,0,0,0,0,0,0,0,0,0,1,1], // 1 熟人
      [1,1,1,1,1,1,1,0,0,0,0,0,0,0,1,1,1], // 2 朋友
      [1,1,1,1,1,1,1,1,1,0,0,0,0,0,1,1,1], // 3 密友
      [1,1,1,1,1,1,1,1,1,1,1,1,0,0,1,1,1], // 4 恋人
      [1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1], // 5 伴侣
    ];
    return MASKS[level] || MASKS[0];
  }
}

// ==================== 层级化Prompt体系 ====================

export class HierarchicalPromptSystem {
  /**
   * 根据RL策略输出和当前状态，生成三层Prompt
   * @param {Object} rlAction - RL策略输出
   * @param {Object} stateContext - 状态上下文
   * @returns {Object} { layer1, layer2, layer3 }
   */
  static generate(rlAction, stateContext) {
    const mode = stateContext.mode !== undefined ? MODE_TYPES[stateContext.mode] || 'daily_companion' : 'daily_companion';
    const level = stateContext.relationshipLevel || 0;
    const levelName = ['陌生人','熟人','朋友','密友','恋人','伴侣'][level] || '陌生人';
    const timeSinceLast = stateContext.secondsSinceLastMessage || 0;
    const userEmotion = ['积极','中性','消极','愤怒','孤独'][stateContext.userEmotion || 0] || '中性';

    // Layer 1: RL全局目标
    const layer1 = `【RL统摄层 - 不可覆盖】
当前策略：proactivity=${(rlAction.proactivity * 100).toFixed(0)}% | timing=${rlAction.timingDelta.toFixed(0)}s | mode=${mode}
奖励目标：R_total = α·R_e + β·R_i + γ·R_s
约束：用户自主性优先 | 禁止情感操控 | 自然语言优先`;

    // Layer 2: 场景策略
    const layer2 = `【场景策略层】
关系：${levelName}(Lv${level}) | 情绪：${userEmotion} | 上次消息：${timeSinceLast.toFixed(0)}s前
策略方向：${rlAction.proactivity > 0.7 ? '适度主动引导话题' : rlAction.proactivity > 0.4 ? '平稳交流' : '倾听为主，降低主动'}
模式适配：${mode === 'approach_game' ? '搭讪模式·保持轻松有趣' : 
            mode === 'date_game' ? '约会模式·营造浪漫氛围' :
            mode === 'emotional_support' ? '情感支持·共情优先' : '日常陪伴·自然流畅'}`;

    // Layer 3: 执行层
    const layer3 = `【执行层】
生成要求：
1. 使用自然口语化表达
2. 根据关系等级${levelName}调整亲密度
3. ${rlAction.contentType === 0 ? '自然问候，不做作' :
     rlAction.contentType === 1 ? '分享有趣的内容' :
     rlAction.contentType === 2 ? '温和邀约，不施加压力' :
     rlAction.contentType === 3 ? '适当幽默，不强行搞笑' :
     rlAction.contentType === 4 ? '共情回应，理解对方感受' :
     rlAction.contentType === 5 ? '分享知识或见解' :
     rlAction.contentType === 6 ? '温馨提醒或关心' : '沉默等待，不强迫对话'}
4. 禁止：提及RL/Agent/奖励等系统概念 | 同时发送超过1条消息 | 用户拒绝后坚持`;

    return { layer1, layer2, layer3 };
  }
}

// ==================== 主系统 ====================

export class UnifiedDatingSystem {
  declare App: any;
  declare _rl: UnifiedRLAgent;
  declare relationship: DatingRelationshipModel;
  declare timePattern: TimePatternLearner;
  declare eventMemory: EventMemory;
  declare rewardFn: UnifiedRewardFunction;
  declare _initialized: boolean;
  declare _lastUpdateTime: number;
  declare _sessionDuration: number;
  declare _actionTimer: number;
  declare _decisionInterval: number;
  declare _proactiveCooldown: number;
  declare _lastProactiveTime: number;
  declare _lastState: Float64Array | null;
  declare _lastRawAction: any | null;
  declare _lastResolved: any | null;
  declare _currentResolved: any | null;
  declare _vrFeedbackTimer: number;
  declare _vrFeedbackCooldown: number;
  declare _vrFeedbackPending: boolean;
  declare _vrFeedbackLastTime: number;
  declare _proactiveSuccesses: number;
  declare _proactiveAttempts: number;
  declare _sessionInteractionCount: number;
  declare _totalInteractions: number;
  declare _totalReward: number;
  declare _recentActions: number[];

  /**
   * @param {Object} App - 外部App引用
   * @param {Object} [options]
   */
  constructor(App, options = {}) {
    this.App = App;

    // 核心RL引擎（Rainbow DQN）
    this._rl = new UnifiedRLAgent({
      mode: 'unified_dating',
      stateSize: UNIFIED_STATE_DIM,
      nActions: 7,  // 7维动作头（proactivity, timing, mode, content, relation, engagement, scenario）
      hiddenLayers: [128, 96, 64],
      storageKey: 'unified_dating_v2',
      lr: 0.0005,
      gamma: 0.93,
      nStep: 3,
      useNoisy: true,
      noisyStd: 0.12,
      usePER: true,
      perAlpha: 0.6,
      autoTune: true,
      usePBT: true,
    });

    // 子系统
    this.relationship = new DatingRelationshipModel();
    this.timePattern = new TimePatternLearner();
    this.eventMemory = new EventMemory();
    this.rewardFn = new UnifiedRewardFunction();

    // 状态跟踪
    this._initialized = true;
    this._lastUpdateTime = Date.now();
    this._sessionDuration = 0;
    this._actionTimer = 0;
    this._decisionInterval = 3.0;
    this._proactiveCooldown = 15.0;
    this._lastProactiveTime = 0;
    this._lastState = null;
    this._lastRawAction = null;
    this._lastResolved = null;
    this._currentResolved = null;

    // VR反馈调度状态（RL统一调度VR晃动反馈时机）
    this._vrFeedbackTimer = 0;
    this._vrFeedbackCooldown = 10.0; // RL控制的基础反馈间隔(s)
    this._vrFeedbackPending = false; // 是否等待RL决策
    this._vrFeedbackLastTime = 0; // 内部时间差累计用

    // 统计
    this._proactiveSuccesses = 0;
    this._proactiveAttempts = 0;
    this._sessionInteractionCount = 0;
    this._totalInteractions = 0;
    this._totalReward = 0;
    this._recentActions = [];

    console.log('[UnifiedDating] 统一RL系统初始化完成, 状态维:', UNIFIED_STATE_DIM);
  }

  // ==================== 核心循环 ====================

  /** 每帧调用 */
  update(dt) {
    if (!this._initialized) return;
    const now = Date.now();
    this._actionTimer += dt;
    this._sessionDuration += dt;

    // 决策间隔
    if (this._actionTimer >= this._decisionInterval) {
      const remainder = this._actionTimer - this._decisionInterval;
      this._actionTimer = 0;
      this._step();
      this._actionTimer = Math.max(0, remainder);
    }

    // 被动衰减
    if (this._sessionDuration % 60 < dt) {
      const si = (now - (this.App._lastUserInteractTime || 0)) / 1000;
      if (si > 600) this.relationship.updateMetrics(-0.1 * dt, -0.05 * dt, -0.05 * dt);
    }
  }

  /**
   * RL统一调度VR晃动反馈（由VR侧每帧调用）：
   * 强度≥18时按RL策略节奏决定是否反馈，强度归零时通知安静。
   * 返回 null（不动作）或 { type:'up'|'left'|'stop', intensity }
   */
  dispatchVRFeedback(dt) {
    const App = this.App;
    const vs = App.vrShake;
    if (!vs) return null;
    const now = Date.now();
    // 内部用真实时间差累计（调用方不传dt也可工作）
    const elapsed = dt > 0 ? dt : Math.min((now - (this._vrFeedbackLastTime || now)) / 1000, 1);
    this._vrFeedbackLastTime = now;

    // RL调度纪律：AI正在说话/思考时不得打断自己，VR反馈挂起等待空闲
    const aiBusy = App.State && (App.currentState === App.State.SPEAKING || App.currentState === App.State.THINKING);

    // 维护活跃状态（供停止检测）
    if (vs.upDown > 0 || vs.leftRight > 0) {
      vs.lastActive = now;
      vs.stopNotified = false;
      this._vrFeedbackTimer += elapsed;
    } else {
      this._vrFeedbackTimer = 0;
      this._vrFeedbackPending = false;
    }

    // 强度≥18：RL调度反馈（冷却期满后按策略概率决定，间隔下限10s，强度越高越频繁）
    const vrIntensity = Math.max(vs.upDown || 0, vs.leftRight || 0);
    if (vrIntensity >= 18) {
      const cooldown = this._vrFeedbackCooldown * (1 - vrIntensity / 200); // 强度越高冷却越短
      if (this._vrFeedbackTimer >= cooldown) {
        this._vrFeedbackTimer = 0;
        const prob = 0.55 + (vrIntensity / 100) * 0.3 + (this.relationship.getMetrics().affection / 100) * 0.15;
        this._vrFeedbackPending = Math.random() < prob;
      }
      // 待决反馈在AI空闲时才发送；忙碌时保持挂起（不打断自己）
      if (this._vrFeedbackPending && !aiBusy) {
        this._vrFeedbackPending = false;
        if (vs.upDown >= 18 && vs.upDown >= vs.leftRight) {
          return { type: 'up', intensity: vs.upDown };
        }
        if (vs.leftRight >= 18) {
          return { type: 'left', intensity: vs.leftRight };
        }
      }
    }

    // 强度归零（停止滑动）：AI空闲时通知安静（忙碌时延后，不打断）
    if (vs.lastActive > 0 && !vs.stopNotified && now - vs.lastActive > 1500 && vs.upDown === 0 && vs.leftRight === 0 && !aiBusy) {
      vs.stopNotified = true;
      return { type: 'stop' };
    }

    return null;
  }

  /** 核心决策步骤 */
  _step() {
    const App = this.App;
    const now = Date.now();
    const secSinceMsg = (now - (App._lastUserMessageTime || 0)) / 1000;
    const secSinceProactive = (now - this._lastProactiveTime) / 1000;

    // 编码当前状态
    const state = this._encodeState();
    const rewardResult = this._lastState ? this.rewardFn.compute(
      this._lastState, state, this._lastRawAction || {},
      { affection: this.relationship.getMetrics().affection, userResponded: secSinceMsg < 10 }
    ) : { total: 0 };

    // 存储经验
    if (this._lastState && this._lastRawAction) {
      this._rl.store(
        Array.from(this._lastState),
        this._actionToDiscrete(this._lastRawAction),
        rewardResult.total,
        Array.from(state),
        false
      );
      this._rl.train();
    }

    // 更新关系指标
    this.relationship.updateMetrics(
      rewardResult.total * 0.02,
      rewardResult.total * 0.01,
      rewardResult.total * 0.03
    );

    // 决策是否主动行动
    let shouldAct = false;
    const hour = new Date().getHours();
    const isUserActive = secSinceMsg < 120 || (secSinceMsg < 300 && App.currentState === 'listening');
    const shouldProactive = this.timePattern.getActiveScore(hour) > 0.4;

    if (secSinceProactive > this._proactiveCooldown && (isUserActive || shouldProactive)) {
      shouldAct = Math.random() < 0.7;
    }

    if (shouldAct) {
      const action5d = this._rl.chooseAction(Array.from(state));
      this._lastRawAction = this._discreteToAction(action5d.action);
      this._currentResolved = UnifiedActionSpace.resolve(
        this._lastRawAction,
        this.relationship.getLevel(),
        App.currentMode || 0
      );
      this._proactiveAttempts++;
      this._lastProactiveTime = now;
    } else {
      this._lastRawAction = null;
      this._currentResolved = null;
    }

    this._lastState = state;
    this._lastResolved = this._currentResolved;
    if (this._currentResolved) {
      this._recentActions.push(this._currentResolved.contentType);
      if (this._recentActions.length > 10) this._recentActions.shift();
    }
  }

  /** 编码统一状态 */
  _encodeState() {
    const App = this.App;
    return UnifiedStateEncoder.encode({
      relationship: this.relationship,
      timePattern: this.timePattern,
      eventMemory: this.eventMemory,
      userState: {
        lastUserMessageTime: App._lastUserMessageTime,
        lastUserInteractTime: App._lastUserInteractTime,
        isListening: App.currentState === 'listening',
        isSpeaking: App.currentState === 'speaking',
        isGameMode: false,
        isLocked: App.lockMode,
        vrShake: App.vrShake, // VR晃动强度：RL统一调度VR反馈时机
        currentMode: App.currentMode !== undefined ? App.currentMode : 0,
      },
      internal: {
        sessionDuration: this._sessionDuration,
        actionTimer: this._actionTimer,
        lastAction: this._lastResolved ? this._lastResolved.relationAction : -1,
        lastProactiveTime: this._lastProactiveTime,
        proactiveSuccesses: this._proactiveSuccesses,
        proactiveAttempts: this._proactiveAttempts,
        sessionInteractionCount: this._sessionInteractionCount,
        curiosityBias: 0.5,
      },
    });
  }

  /** 将离散动作索引映射回多维动作 */
  _discreteToAction(discreteIdx) {
    const p = discreteIdx / 6;
    return {
      proactivity: Math.max(0, Math.min(1, p)),
      timing_delta: 5 + p * 60,
      mode_switch: Math.round(Math.min(8, p * 8)),
      content_type: Math.round(Math.min(7, p * 7)),
      relation_action: Math.round(Math.min(16, p * 16)),
      engagement_action: Math.round(Math.min(10, p * 10)),
    };
  }

  /** 将多维动作映射回离散索引 */
  _actionToDiscrete(action) {
    if (!action) return 0;
    return Math.round(
      (action.proactivity * 0.3 + action.timing_delta / 60 * 0.2 +
       action.mode_switch / 8 * 0.15 + action.content_type / 7 * 0.1 +
       action.relation_action / 16 * 0.15 + action.engagement_action / 10 * 0.1) * 6
    );
  }

  // ==================== 外部接口 ====================

  /** 获取当前执行的行动计划 */
  getCurrentPlan() {
    return this._currentResolved ? UnifiedActionSpace.describe(this._currentResolved) : null;
  }

  /** 获取上一个执行的行动计划 */
  getLastPlan() {
    return this._lastResolved ? UnifiedActionSpace.describe(this._lastResolved) : null;
  }

  /** 生成消息Prompt */
  getMessagePrompt() {
    const plan = this._currentResolved || this._lastResolved;
    if (!plan) return null;
    return HierarchicalPromptSystem.generate(
      this._lastRawAction || {},
      {
        mode: plan.mode,
        relationshipLevel: this.relationship.getLevel(),
        secondsSinceLastMessage: (Date.now() - (this.App._lastUserMessageTime || Date.now())) / 1000,
        userEmotion: 0,
      }
    );
  }

  /** 用户消息通知 */
  notifyUserMessage(text, sentiment) {
    const now = Date.now();
    const lastMsgTime = this.App._lastUserMessageTime || now;
    this.timePattern.recordInteraction(now, now - lastMsgTime);
    this.eventMemory.record('user_message', sentiment || 0, null, 0,
      { text: text ? text.substring(0, 50) : '', timestamp: now });
    this._sessionInteractionCount++;
    this._totalInteractions++;
  }

  /** 用户交互通知 */
  notifyUserInteraction() {
    this.timePattern.recordInteraction(Date.now(), 0);
  }

  /** 里程碑触发 */
  triggerMilestone(name) {
    const result = this.relationship.triggerMilestone(name);
    if (result.new) {
      const bonus = (result.bonus.intimacy || 0) + (result.bonus.trust || 0);
      this._totalReward += bonus * 0.5;
      this.eventMemory.record('milestone', 0.8, null, bonus,
        { milestone: name, timestamp: Date.now() });
    }
    return result;
  }

  /** 会话管理 */
  startSession() {
    this._sessionDuration = 0;
    this._sessionInteractionCount = 0;
    this.timePattern.recordSessionStart(Date.now());
  }

  endSession() {
    this.timePattern.recordSessionEnd(Date.now());
    this.relationship.endSession();
    this._rl.endEpisode(this._totalReward, {
      win: this.relationship.getLevel() >= 4,
      level: this.relationship.getLevel(),
    } as any);
    if (this._lastState && this._lastRawAction) {
      const finalState = this._encodeState();
      this._rl.store(Array.from(this._lastState), this._actionToDiscrete(this._lastRawAction), 0, Array.from(finalState), true);
      this._rl.train();
    }
  }

  // ==================== 调试与持久化 ====================

  getDebugInfo() {
    const plan = this.getCurrentPlan();
    return {
      relationship: {
        level: this.relationship.getLevel(),
        levelName: this.relationship.getLevelName(),
        metrics: this.relationship.getMetrics(),
        milestones: this.relationship.getMilestoneProgress(),
      },
      timePattern: {
        activeHours: Array.from(this.timePattern.activeHours),
        avgResponseMs: this.timePattern.responseTime.avgResponseMs,
        sessionCount: this.timePattern.sessionPatterns.sessionCount,
      },
      eventMemory: {
        shortTermCount: this.eventMemory.shortTerm.length,
        longTermCount: this.eventMemory.longTerm.length,
        totalEvents: this.eventMemory._totalEvents,
      },
      rl: {
        stats: this._rl.getStats(),
        hyper: { lr: (this._rl as any).lr, gamma: (this._rl as any).gamma },
      },
      currentPlan: plan,
      session: {
        duration: this._sessionDuration,
        interactions: this._sessionInteractionCount,
        totalReward: this._totalReward,
        proactiveRate: this._proactiveAttempts > 0 ?
          (this._proactiveSuccesses / this._proactiveAttempts).toFixed(2) : 0,
      },
    };
  }

  async flush() {
    try {
      await this._rl.flush();
      const data = {
        version: 2,
        relationship: this.relationship.serialize(),
        timePattern: this.timePattern.serialize(),
        eventMemory: this.eventMemory.serialize(),
        stats: {
          totalInteractions: this._totalInteractions,
          totalReward: this._totalReward,
          proactiveSuccesses: this._proactiveSuccesses,
          proactiveAttempts: this._proactiveAttempts,
        },
      };
      localStorage.setItem('unified_dating_v2', JSON.stringify(data));
    } catch (e) {
      console.warn('[UnifiedDating] flush error:', e.message);
    }
  }

  async load() {
    try {
      const raw = localStorage.getItem('unified_dating_v2');
      if (!raw) return false;
      const data = JSON.parse(raw);
      if (data.relationship) this.relationship.deserialize(data.relationship);
      if (data.timePattern) this.timePattern.deserialize(data.timePattern);
      if (data.eventMemory) this.eventMemory.deserialize(data.eventMemory);
      if (data.stats) Object.assign(this, data.stats);
      return true;
    } catch (e) {
      console.warn('[UnifiedDating] load error:', e.message);
      return false;
    }
  }

  reset() {
    this.relationship.reset();
    this.timePattern = new TimePatternLearner();
    this.eventMemory = new EventMemory();
    this._rl.reset();
    this._sessionDuration = 0;
    this._actionTimer = 0;
    this._lastProactiveTime = 0;
    this._lastState = null;
    this._lastRawAction = null;
    this._lastResolved = null;
    this._currentResolved = null;
    this._proactiveSuccesses = 0;
    this._proactiveAttempts = 0;
    this._sessionInteractionCount = 0;
    this._totalInteractions = 0;
    this._totalReward = 0;
    this._recentActions = [];
  }
}

export default UnifiedDatingSystem;
