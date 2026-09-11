import { UnifiedRLAgent } from './unified-rl-agent.ts';

/* ============================================================
 *  ExpressionRLAgent —— RL 驱动的"AI 自我表达"智能体
 *
 *  定位：在统一约会系统(UnifiedDatingSystem, 决策"说什么")之外，
 *  新增一层视觉层表达智能体（决策"怎么表现"）：
 *  - 动作空间：12 类表情化身体表达（害羞低头/开心雀跃/沉思/
 *    惊讶偷看/委屈撒娇/俏皮眨眼/温柔注视……）
 *  - 每个动作 = 微动作序列 + 情绪覆盖 + 眼神控制（调用 08 引擎）
 *  - 状态编码：关系水平/好感/信任/亲密 + 互动节奏 + 情绪上下文
 *  - 奖励：用户响应(15s 内回消息)、互动提升、关系提升、
 *    新颖性(避免重复)、连贯性(打断说话惩罚)
 *  - 网络：复用 UnifiedRLAgent（Dueling Double DQN + NoisyNet
 *    + PER + N-step），storageKey='expression_rl_v1' 自动持久化
 * ============================================================ */

// 表达动作空间（顺序即动作索引）
export const EXPRESSION_ACTIONS = [
  { name: 'idle_gentle',   seq: ['head_tilt_l'],                                    emotion: 'calm',    gaze: 'cam',      energy: 'low'  },
  { name: 'happy_shine',   seq: ['happy_bounce'],                                  emotion: 'happy',   gaze: 'cam',      energy: 'high' },
  { name: 'shy_flutter',   seq: ['shy_look_down', { motion: 'shy_twist', hold: 0.3 }], emotion: 'shy',   gaze: 'shy',      energy: 'low'  },
  { name: 'think_deep',    seq: ['chin_touch'],                                    emotion: 'thoughtful', gaze: 'u',     energy: 'low'  },
  { name: 'surprise_peek', seq: ['surprise_flinch', { motion: 'head_turn_l', hold: 0.3 }], emotion: 'surprised', gaze: 'sidelong', energy: 'high' },
  { name: 'pout_coax',     seq: ['head_down'],                                     emotion: 'pout',    gaze: 'd',        energy: 'low'  },
  { name: 'excite_wave',   seq: ['excited_clap'],                                 emotion: 'excited', gaze: 'cam',      energy: 'high' },
  { name: 'calm_breath',   seq: ['sigh'],                                          emotion: 'calm',    gaze: 'd',        energy: 'low'  },
  { name: 'playful_wink',  seq: ['playful_wink'],                                  emotion: 'playful', gaze: 'cam',      energy: 'high' },
  { name: 'charm_look',    seq: ['love_gaze'],                                     emotion: 'love',    gaze: 'cam',      energy: 'low'  },
  { name: 'tired_sigh',    seq: ['stretch'],                                       emotion: 'tired',   gaze: 'u',        energy: 'low'  },
  { name: 'proud_stand',   seq: ['head_up', { motion: 'happy_bounce', hold: 0.3 }], emotion: 'proud',   gaze: 'cam',      energy: 'high' },
];

const STATE_DIM = 20;
const N_ACTIONS = EXPRESSION_ACTIONS.length;

// 每个动作最短间隔（秒）
const MIN_GAP = [8, 12, 12, 14, 15, 14, 16, 10, 14, 18, 20, 16];

export class ExpressionRLAgent {
  constructor(App) {
    this.App = App;
    this.enabled = false;
    this._initialized = false;

    this._rl = null;
    this._rlReady = false;

    // 决策节奏
    this._decisionTimer = 0;
    this._decisionInterval = 5 + Math.random() * 3;
    this._lastActionAt = 0;      // Date.now()
    this._lastActionIdx = -1;
    this._actionHistory = [];    // 最近动作（新颖性）
    this._totalActions = 0;

    // 待结算奖励
    this._pending = null;        // { action, state, at, engagementAt, spokeAtStart }
    this._pendingStores = 0;

    // 统计
    this.stats = {
      totalDecisions: 0,
      userResponses: 0,
      totalReward: 0,
      avgReward: 0,
      lastActionName: 'none',
    };

    // 会话追踪
    this._sessionMsgCount = 0;
    this._sessionStart = Date.now();
  }

  // ==================== 生命周期 ====================
  init() {
    if (this._initialized) return;
    this._initialized = true;
    try {
      this._rl = new UnifiedRLAgent({
        mode: 'expression',
        stateSize: STATE_DIM,
        nActions: N_ACTIONS,
        hiddenLayers: [48, 32],
        storageKey: 'expression_rl_v1',
        lr: 0.0008,
        gamma: 0.92,
        nStep: 2,
        useDistributional: false,
        useNoisy: true,
        noisyStd: 0.15,
        usePBT: false,
        replayCapacity: 4000,
        batchSize: 32,
      });
      // UnifiedRLAgent 构造时已异步加载持久化权重，稍后标记就绪
      setTimeout(() => { this._rlReady = true; }, 800);
    } catch (e) {
      console.warn('[ExpressionRL] 初始化失败:', e);
    }
  }

  start() { if (!this._initialized) this.init(); this.enabled = true; }
  stop() { this.enabled = false; this._pending = null; this._flushIfReady(); }
  toggle() { this.enabled ? this.stop() : this.start(); return this.enabled; }

  notifyUserMessage() {
    this._sessionMsgCount++;
  }

  // ==================== 状态读取 ====================
  _rel() {
    const ds = this.App._datingSystem;
    if (ds && ds.relationship) return ds.relationship;
    return null;
  }

  _engagementScore() {
    const ds = this.App._datingSystem;
    if (ds && typeof ds.getEngagementScore === 'function') return ds.getEngagementScore();
    if (this.App._engagementRL && typeof this.App._engagementRL._engagementScore === 'number') {
      return this.App._engagementRL._engagementScore;
    }
    return 0.5;
  }

  _encodeState() {
    const rel = this._rel();
    const m = rel ? rel.getMetrics() : null;
    const now = Date.now();
    const secSinceMsg = this.App._lastUserMessageTime ? (now - this.App._lastUserMessageTime) / 1000 : 9999;
    const secSinceAct = this._lastActionAt ? (now - this._lastActionAt) / 1000 : 9999;
    const secSinceInteract = this.App._lastUserInteractTime ? (now - this.App._lastUserInteractTime) / 1000 : 9999;
    const hour = new Date().getHours();
    const engagement = Math.max(0, Math.min(1, this._engagementScore()));

    const st = this.App.currentState;
    const stateCode = st === this.App.State.IDLE ? 0
      : st === this.App.State.LISTENING ? 1
      : st === this.App.State.SPEAKING ? 2 : 3;

    // 新颖性：最近 3 个动作中与上一次动作重复的比例（惩罚重复）
    const lastIdx = this._lastActionIdx;
    let novelty = 0;
    if (lastIdx >= 0 && this._actionHistory.length > 0) {
      let same = 0;
      for (const a of this._actionHistory) if (a === lastIdx) same++;
      novelty = same / Math.min(3, this._actionHistory.length);
    }

    const s = new Float64Array(STATE_DIM);
    s[0] = rel ? rel.getLevel() / 4 : 0;
    s[1] = m ? Math.max(0, Math.min(1, m.affection / 100)) : 0;
    s[2] = m ? Math.max(0, Math.min(1, m.trust / 100)) : 0;
    s[3] = m ? Math.max(0, Math.min(1, m.intimacy / 100)) : 0;
    s[4] = engagement;
    s[5] = stateCode / 3;
    s[6] = Math.min(1, secSinceMsg / 300);
    s[7] = Math.min(1, secSinceAct / 60);
    s[8] = Math.min(1, this._sessionMsgCount / 50);
    s[9] = hour / 24;
    // 时段 one-hot（晨/午/晚/夜）
    s[10] = hour >= 5 && hour < 12 ? 1 : 0;
    s[11] = hour >= 12 && hour < 18 ? 1 : 0;
    s[12] = hour >= 18 && hour < 23 ? 1 : 0;
    s[13] = hour >= 23 || hour < 5 ? 1 : 0;
    s[14] = novelty;
    s[15] = Math.min(1, secSinceInteract / 120);
    s[16] = this.stats.avgReward / 2;
    s[17] = 0;   // 保留位（原游戏模式标记，已恒为 0 以保持特征维度稳定）
    s[18] = Math.min(1, (now - this._sessionStart) / (30 * 60 * 1000));
    s[19] = secSinceMsg < 30 ? 1 : 0;
    return s;
  }

  // ==================== 奖励设计 ====================
  _settleReward() {
    if (!this._pending) return;
    const p = this._pending;
    const now = Date.now();
    const dtSec = (now - p.at) / 1000;

    let r = 0;
    // 1) 用户响应：动作后 15 秒内收到用户消息 → 强正奖励（表达被回应）
    if (this.App._lastUserMessageTime && (this.App._lastUserMessageTime - p.at) < 15000) {
      r += 1.2;
      this.stats.userResponses++;
    } else if (this.App._lastUserMessageTime && (this.App._lastUserMessageTime - p.at) < 30000) {
      r += 0.5;
    }
    // 2) 互动提升
    const engNow = this._engagementScore();
    if (engNow > p.engagementAt) r += 0.3 + 0.6 * (engNow - p.engagementAt);
    // 3) 关系提升
    const rel = this._rel();
    if (rel) {
      const lvlNow = rel.getLevel();
      if (lvlNow > p.relLevel) r += 0.4;
    }
    // 4) 连贯性：动作打断了 AI 说话 → 惩罚
    if (p.spokeAtStart) r -= 0.6;
    // 5) 新颖性：与最近动作重复 → 惩罚（鼓励多样性）
    if (this._actionHistory.indexOf(p.action) >= 0) r -= 0.45;
    // 6) 时机：空闲时的小动作更自然
    if (p.stateAtStart === this.App.State.IDLE && dtSec < 8) r += 0.1;
    // 7) 长时间无互动时，低能量动作更安全
    const secSinceMsg = this.App._lastUserMessageTime ? (now - this.App._lastUserMessageTime) / 1000 : 9999;
    if (secSinceMsg > 60 && EXPRESSION_ACTIONS[p.action].energy === 'low') r += 0.15;

    // 记录经验（state, action, reward, nextState）
    if (this._rl) {
      const nextState = this._encodeState();
      this._rl.store(p.state, p.action, r, nextState, false);
      this._pendingStores++;
      this.stats.totalReward += r;
      this.stats.avgReward = this.stats.avgReward * 0.97 + r * 0.03;
      if (this._pendingStores >= 3) {
        this._rl.train();
        this._pendingStores = 0;
      }
    }
    this._pending = null;
    return r;
  }

  // ==================== 动作执行 ====================
  _executeAction(idx) {
    const App = this.App;
    const act = EXPRESSION_ACTIONS[idx];
    App.playMotionSequence(act.seq, {
      priority: 'rl',
      keepExpr: true, // 动作结束后保留情绪覆盖，让表情自然回落
      onDone: () => { /* 表情由覆盖定时衰减 */ }
    });
    // 情绪 + 眼神（动作定义里已附带，这里兜底设置一次；时长覆盖动作全程+淡出）
    App.setEmotionOverlay(act.emotion, 1, 6);
    if (act.gaze) App.setGaze(act.gaze, 1, 5);

    this._lastActionAt = Date.now();
    this._lastActionIdx = idx;
    this._actionHistory.push(idx);
    if (this._actionHistory.length > 3) this._actionHistory.shift();
    this._totalActions++;
    this.stats.lastActionName = act.name;
    this.stats.totalDecisions++;

    // 记录待结算奖励
    this._pending = {
      action: idx,
      state: this._encodeState(),
      at: this._lastActionAt,
      engagementAt: this._engagementScore(),
      relLevel: this._rel() ? this._rel().getLevel() : 0,
      spokeAtStart: App.currentState === App.State.SPEAKING,
      stateAtStart: App.currentState,
    };
  }

  // ==================== 主循环 ====================
  update(dt) {
    if (!this.enabled || !this._rl || !this._rlReady) return;
    const App = this.App;

    // 结算上轮奖励（动作完成/超时）
    if (this._pending) {
      const dtSec = (Date.now() - this._pending.at) / 1000;
      if (dtSec > 12 || (dtSec > 4 && App.currentState === App.State.SPEAKING)) {
        this._settleReward();
      }
    }

    this._decisionTimer += dt;
    if (this._decisionTimer < this._decisionInterval) return;
    this._decisionTimer = 0;
    // 决策间隔拉长：给动作留足"播放+复位"时间，避免频繁切换导致头部不停转动
    this._decisionInterval = 8 + Math.random() * 6;

    // 决策条件：非游戏模式、非说话中、无更高/同级动作进行中、冷却完成
    if (App.currentState === App.State.SPEAKING) return;
    if (App.motionSystemActive && App.motionSystemActive()) return;
    if (this._lastActionAt && (Date.now() - this._lastActionAt) < MIN_GAP[this._lastActionIdx] * 1000) return;
    // 模型未加载时给一点随机探索（新用户也能看到动作）
    const eps = 0.08 + 0.52 * Math.exp(-this._totalActions / 120);
    if (Math.random() < eps) {
      const pick = Math.floor(Math.random() * N_ACTIONS);
      this._executeAction(pick);
      return;
    }
    // 45% 概率选择"安静动作"（幅度小、易复位），让角色有稳定的休息时刻，
    // 观感上头部能回到注视用户的中位（复位）
    const quiet = [0, 7, 3, 10, 9]; // idle_gentle / calm_breath / think_deep / tired_sigh / charm_look
    if (Math.random() < 0.45) {
      this._executeAction(quiet[Math.floor(Math.random() * quiet.length)]);
      return;
    }
    const res = this._rl.chooseAction(this._encodeState());
    if (res && res.action != null) this._executeAction(res.action);
  }

  // ==================== 持久化与调试 ====================
  async _flushIfReady() {
    if (this._rl) { try { await this._rl.flush(); } catch (e) { /* ignore */ } }
  }

  async flush() { await this._flushIfReady(); }

  getDebugInfo() {
    const q = [];
    if (this._rlReady && this._rl) {
      try { q.push(...this._rl.getQValues(this._encodeState())); } catch (e) { /* ignore */ }
    }
    return {
      enabled: this.enabled,
      lastAction: this.stats.lastActionName,
      lastActionAt: this._lastActionAt ? new Date(this._lastActionAt).toLocaleTimeString() : '-',
      totalActions: this._totalActions,
      totalDecisions: this.stats.totalDecisions,
      userResponses: this.stats.userResponses,
      avgReward: +this.stats.avgReward.toFixed(3),
      totalReward: +this.stats.totalReward.toFixed(2),
      sessionMsgCount: this._sessionMsgCount,
      qValues: q.length ? q.map(v => +v.toFixed(3)) : [],
    };
  }

  // ===== 类型声明（仅类型注解，无运行时副作用） =====
  declare App: any;
  declare enabled: boolean;
  declare _initialized: boolean;
  declare _rl: any;
  declare _rlReady: boolean;
  declare _decisionTimer: number;
  declare _decisionInterval: number;
  declare _lastActionAt: number;
  declare _lastActionIdx: number;
  declare _actionHistory: number[];
  declare _totalActions: number;
  declare _pending: any;
  declare _pendingStores: number;
  declare stats: { totalDecisions: number; userResponses: number; totalReward: number; avgReward: number; lastActionName: string };
  declare _sessionMsgCount: number;
  declare _sessionStart: number;
}

export default ExpressionRLAgent;
