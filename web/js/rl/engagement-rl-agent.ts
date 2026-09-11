/**
 * @deprecated EngagementRLAgent 已弃用，将在未来版本中移除。
 * 请使用 UnifiedDatingSystem (./unified-dating-system.js) 替代。
 *
 * ============================================================
 * Engagement RL Agent — 薄兼容封装层
 *
 * 本文件作为向后兼容的薄封装层，所有核心逻辑委托给
 * UnifiedDatingSystem。保持外部 API 不变，确保已有调用方
 * （App.engagementRL、App.animate 等）无需修改。
 * ============================================================
 */

import { UnifiedDatingSystem, UnifiedActionSpace } from "./unified-dating-system.ts";

// ==================== 向后兼容的动作空间（11 个行为） ====================
export const ENGAGEMENT_ACTIONS = [
  'idle_stand',        // 0 安静站立（自然地存在）
  'wander',            // 1 随机漫步
  'approach_user',     // 2 靠近用户（走近相机）
  'show_emotion',      // 3 表达情绪（害羞/开心/好奇）
  'interact_object',   // 4 与环境物品互动
  'compliment',        // 5 赞美用户（文字+表情）
  'suggest_game',      // 6 邀请玩游戏
  'dance',             // 7 跳舞/表演
  'curious_explore',   // 8 好奇探索周围
  'greet_user',        // 9 主动打招呼
  'sit_rest',          // 10 坐下休息（放松姿态）
];

// ==================== 封装类 ====================

export class EngagementRLAgent {
  constructor(App) {
    this.App = App;

    // === 创建 UnifiedDatingSystem 作为核心引擎 ===
    try {
      this._unified = new UnifiedDatingSystem(App);
      this._initialized = true;
    } catch (e) {
      console.warn('[EngagementRL] 初始化失败（委派 UnifiedDatingSystem）:', e.message);
      this._initialized = false;
    }

    // === 向后兼容超参 ===
    this.STATE_SIZE = 32;
    this.N_ACTIONS = ENGAGEMENT_ACTIONS.length;

    // === 向后兼容运行时属性 ===
    this._lastActionTime = 0;
    this._actionCooldown = 4.0;       // 两次行为间最小间隔(秒)
    this._stateUpdateInterval = 1.0;  // 状态更新/决策间隔(秒)
    this._stateTimer = 0;
    this._currentActionId = -1;
    this._currentActionTimer = 0;
    this._actionDuration = {          // 各行为持续时间(秒)
      idle_stand: 3, wander: 4, approach_user: 3,
      show_emotion: 2.5, interact_object: 3.5,
      compliment: 2, suggest_game: 2.5, dance: 3.5,
      curious_explore: 4, greet_user: 2, sit_rest: 4,
    };

    // === 向后兼容用户互动追踪 ===
    this._lastUserMessageTime = 0;
    this._lastUserInteractTime = 0;
    this._userSpeakingCount = 0;
    this._engagementScore = 0.5;        // 0~1
    this._engagementLog = [];
    this._sessionStartTime = Date.now();
    this._consecutiveNoInteraction = 0;

    // === 向后兼容状态缓存 ===
    this._cachedState = null;
    this._cachedStateTime = 0;

    // === 向后兼容训练统计 ===
    this.stats = {
      totalDecisions: 0,
      userResponses: 0,
      avgEngagement: 0.5,
      sessionCount: 0,
    };

    // === 暴露底层 RL 引擎（供外部直接访问，如 getStats） ===
    this._rl = this._initialized ? this._unified._rl : null;
  }

  // ==================== 通知接口 ====================

  /** 通知：用户说话了（给外部调用） */
  notifyUserMessage() {
    this._lastUserMessageTime = Date.now();
    this._consecutiveNoInteraction = 0;
    this._unified?.notifyUserMessage('', 0);
  }

  /** 通知：用户进行了交互操作 */
  notifyUserInteraction() {
    this._lastUserInteractTime = Date.now();
    this._consecutiveNoInteraction = 0;
    this._unified?.notifyUserInteraction();
  }

  // ==================== 主循环 ====================

  /**
   * 主更新循环，每帧调用
   * 委托给 UnifiedDatingSystem，并提取 engagement action 执行
   */
  update(dt) {
    if (!this._initialized || !this._unified) return;

    // 1) 核心逻辑委托给统一系统
    this._unified.update(dt);

    // 2) 向后兼容：更新内部计时器
    this._stateTimer += dt;
    this._currentActionTimer += dt;

    // 3) 从统一系统提取 engagement action 并执行
    const resolved = this._unified._currentResolved;
    if (resolved && resolved.engagementAction !== undefined) {
      const newActionId = Math.min(resolved.engagementAction, this.N_ACTIONS - 1);
      if (newActionId !== this._currentActionId && newActionId >= 0) {
        // 新动作被选中
        this._currentActionId = newActionId;
        this._currentActionTimer = 0;
        this._lastActionTime = Date.now();
        this.stats.totalDecisions++;
        this._executeAction(newActionId);
      }
    }

    // 4) 动作过期检查
    if (this._currentActionId >= 0) {
      const name = ENGAGEMENT_ACTIONS[this._currentActionId];
      if (this._currentActionTimer >= (this._actionDuration[name] || 3)) {
        this._currentActionId = -1;
      }
    }

    // 5) 更新互动分数
    this._updateEngagementScore();
  }

  /** 局终结算（每次新会话或模式切换） */
  endSession() {
    if (!this._initialized || !this._unified) return;
    this._unified.endSession();
    this.stats.sessionCount++;

    // 重置追踪
    this._engagementLog = [];
    this._lastUserMessageTime = Date.now();
    this._lastUserInteractTime = Date.now();
    this._currentActionId = -1;
    this._cachedState = null;
    this._stateTimer = 0;
  }

  // ==================== 调试与持久化 ====================

  /** 获取状态信息（旧版格式 + 统一系统扩展信息） */
  getDebugInfo() {
    const base = this._unified ? this._unified.getDebugInfo() : {};
    return {
      ...base,
      // 向后兼容字段
      engagementScore: this._engagementScore,
      currentAction: this._currentActionId >= 0 ? ENGAGEMENT_ACTIONS[this._currentActionId] : 'none',
      userSpeakingCount: this._userSpeakingCount,
      consecutiveNoInteraction: this._consecutiveNoInteraction,
      avgEngagement: this.stats.avgEngagement,
      totalDecisions: this.stats.totalDecisions,
      userResponses: this.stats.userResponses,
      // 委派标记
      _delegatedTo: 'UnifiedDatingSystem',
    };
  }

  /** 强制持久化 */
  async flush() {
    await this._unified?.flush();
  }

  // ==================== 旧版私有方法（向后兼容存根） ====================

  /** @deprecated 统一系统 UnifiedStateEncoder 负责状态编码 */
  _encodeState() {
    console.warn('[EngagementRL] _encodeState 已弃用，由 UnifiedDatingSystem 接管');
    return null;
  }

  /** @deprecated 统一系统 UnifiedRewardFunction 负责奖励计算 */
  _calculateReward() {
    return 0;
  }

  /** @deprecated 统一系统负责策略决策 */
  _decideAction() {}

  /** @deprecated 统一系统负责动作掩码 */
  _getValidActions() {
    return Array.from({ length: this.N_ACTIONS }, (_, i) => i);
  }

  /** 执行选中的动作（所有动作型行为统一说话概率，舞蹈不再特殊） */
  _executeAction(action) {
    const App = this.App;
    const actionName = ENGAGEMENT_ACTIONS[action];
    const ac = App.aiAutonomyController;

    // === 说话触发统一规则 ===
    // 动作型行为（站立/漫步/靠近/互动/探索/跳舞/休息）都以相同的概率触发 AI 说话
    // —— 触发说话的概率对所有动作均等，舞蹈不再是特殊的那一个。
    // 重要：这些文本是作为"用户提示词"（user message）发给 AI 的，
    // 必须用第二人称"你"明确指代 AI 自身的动作（如"你在跳舞"），
    // AI 才能认知"这是我在做什么"。不能用第一人称或无主语，
    // 否则 AI 会把这些动作误认为是用户做的。
    // "说话型"行为（表达情绪/赞美/邀约/打招呼）本身就是语言动作，保持必说。
    const ACTION_SPEAK_PROB = 0.5;  // 所有动作型行为触发说话的统一概率
    const ACTION_SPEAK_TEXTS = {
      idle_stand: '（你安静地站着，感受这一刻的宁静）',
      wander: '（你随意地走了走，活动了一下身体）',
      approach_user: '（你忍不住朝用户走近了几步，想离Ta近一点）',
      interact_object: '（你好奇地摆弄了一下身边的物品）',
      curious_explore: '（你的好奇心被勾起来了，想去周围看看有什么好玩的）',
      dance: '（你随着音乐轻轻摆动身体，跳起了舞）',
      sit_rest: '（你找了块舒服的地方坐下来休息）',
    };
    const trySpeakAction = (name) => {
      const text = ACTION_SPEAK_TEXTS[name];
      if (text && Math.random() < ACTION_SPEAK_PROB) {
        App.sendAIAction?.(text);
      }
    };

    switch (actionName) {
      case 'idle_stand':
        ac?.stopAIMovement?.();
        trySpeakAction('idle_stand');
        break;

      case 'wander':
        if (ac) {
          ac.receiveCommand({
            behavior: 'wander',
            wander: { distance: 2 + Math.random() * 2, angle: Math.random() * Math.PI * 2 }
          });
        }
        trySpeakAction('wander');
        break;

      case 'approach_user':
        // 走到相机附近（约 Z=-1 的位置）
        const avatar = App.modelGroup;
        if (avatar && ac) {
          const camPos = App.camera?.position;
          if (camPos) {
            ac.receiveCommand({
              behavior: 'go_to_poi',
              target: { x: camPos.x, z: camPos.z, label: '靠近用户' }
            });
          }
        }
        trySpeakAction('approach_user');
        break;

      case 'show_emotion':
        // 触发情绪表情动画（通过 VRM 表情系统）
        if (App.setBlendShape) {
          const emotions = ['happy', 'angry', 'sad', 'relaxed', 'surprised'];
          const emo = emotions[Math.floor(Math.random() * emotions.length)];
          App.setBlendShape(emo, 0.8);
          setTimeout(() => App.setBlendShape?.(emo, 0), 2000);
        }
        // 说话型动作：表达情绪本身就是语言动作，必说
        // @ts-ignore -- 原 JS 中 emo 仅在 if 块内声明，此处引用保持原作用域行为（运行时不变）
        App.sendAIAction?.('（你做出了' + ['开心的', '不满的', '难过的', '放松的', '惊讶的'][['happy','angry','sad','relaxed','surprised'].indexOf(emo)] + '表情）');
        break;

      case 'interact_object':
        // 与环境物品互动
        ac?.receiveCommand?.({ behavior: 'idle_action' });
        trySpeakAction('interact_object');
        break;

      case 'compliment':
        const compliments = [
          '你今天看起来心情不错！',
          '你选的这个背景和我的气质真搭',
          '和你聊天总是很开心~',
          '你真的很会照顾人',
          '每次见到你都有新的惊喜'
        ];
        const pick = compliments[Math.floor(Math.random() * compliments.length)];
        // 说话型动作：赞美本身就是语言动作，必说
        App.sendAIAction?.('（' + pick + '）');
        break;

      case 'suggest_game':
        const games = ['mario', 'moba', 'treasure_hunt'];
        const g = games[Math.floor(Math.random() * games.length)];
        // 说话型动作：邀约本身就是语言动作，必说
        App.sendAIAction?.('（想和我一起玩个游戏吗？' +
          (g === 'mario' ? '我们来玩马里奥跑酷！' :
           g === 'moba' ? '来一局 MOBA 对决！' : '去寻宝探探险吧！') + '）');
        break;

      case 'dance':
        if (App.setBlendShape) {
          App.setBlendShape('happy', 0.6);
          setTimeout(() => App.setBlendShape?.('happy', 0), 3000);
        }
        // 与漫步/靠近/探索等动作型行为一样：统一概率触发说话（第一人称"我在跳舞"）
        trySpeakAction('dance');
        break;

      case 'curious_explore':
        ac?.receiveCommand?.({
          behavior: 'explore_chain',
          chain_targets: [{ x: Math.random() * 4 - 2, z: Math.random() * 4 - 2, label: '好奇探索' }]
        });
        trySpeakAction('curious_explore');
        break;

      case 'greet_user':
        // 说话型动作：打招呼本身就是语言动作，必说
        // 用"你"指代 AI 自身，避免 AI 把"挥手打招呼"误认为用户做的
        App.sendAIAction?.('（你注意到用户的到来，开心地朝Ta挥手打招呼）');
        break;

      case 'sit_rest':
        ac?.stopAIMovement?.();
        trySpeakAction('sit_rest');
        break;
    }
  }

  /** @deprecated 内部追踪，保留以防外部直接调用 */
  _userResponsesDetected() {
    this._userSpeakingCount++;
    this.stats.userResponses++;
  }

  /** 更新互动分数（保留旧版实现以保持 stats._updateEngagementScore 一致性） */
  _updateEngagementScore() {
    const now = Date.now();
    const sinceMsg = (now - this._lastUserMessageTime) / 1000;
    const sinceInteract = (now - this._lastUserInteractTime) / 1000;

    // 衰减模型：短期互动活跃度
    const msgScore = Math.exp(-sinceMsg / 120);       // 2分钟衰减
    const interactScore = Math.exp(-sinceInteract / 60); // 1分钟衰减
    const raw = (msgScore * 0.6 + interactScore * 0.4);

    this._engagementScore = this._engagementScore * 0.95 + raw * 0.05;
    this._engagementLog.push(this._engagementScore);
    if (this._engagementLog.length > 200) this._engagementLog.shift();

    this.stats.avgEngagement = this._engagementLog.reduce((a, b) => a + b, 0) / this._engagementLog.length;
  }

  // ===== 类型声明（仅类型注解，无运行时副作用） =====
  declare App: any;
  declare _unified: any;
  declare _initialized: boolean;
  declare STATE_SIZE: number;
  declare N_ACTIONS: number;
  declare _lastActionTime: number;
  declare _actionCooldown: number;
  declare _stateUpdateInterval: number;
  declare _stateTimer: number;
  declare _currentActionId: number;
  declare _currentActionTimer: number;
  declare _actionDuration: Record<string, number>;
  declare _lastUserMessageTime: number;
  declare _lastUserInteractTime: number;
  declare _userSpeakingCount: number;
  declare _engagementScore: number;
  declare _engagementLog: number[];
  declare _sessionStartTime: number;
  declare _consecutiveNoInteraction: number;
  declare _cachedState: any;
  declare _cachedStateTime: number;
  declare stats: { totalDecisions: number; userResponses: number; avgEngagement: number; sessionCount: number };
  declare _rl: any;
}