import type { AppKernel, EmotionParams, PADState } from '../types/app-kernel.js';

export default (function init(App: AppKernel) {
  const { THREE } = App;
  /* ============================================================
   *  情绪控制器 —— PAD 情绪模型 / 情绪源接入 / 情绪→动作参数
   *
   *  定位：情绪驱动动作系统的"大脑"。把离散情绪标签（LLM 语义、
   *  语音情绪、场景事件、用户指令）映射到 PAD 三维连续空间
   *  （愉悦 pleasure / 唤醒 arousal / 支配 dominance，各 ∈ [-1,1]），
   *  再计算驱动动作系统的参数（幅度/速度/姿态扩张/手势频率/动作大类权重）。
   *
   *  PAD 值每帧由 updateEmotionController 平滑过渡（帧率无关），
   *  情绪结束后自动回落中性。表情覆盖复用 08_expression_engine 的
   *  setEmotionOverlay，动作调度由 09b_motion_blender 消费。
   * ============================================================ */

  // ==================== 情绪 → PAD 映射表 ====================
  // 与 08_expression_engine 的 EMOTION_EXPR 情绪标签保持一致
  App.EMOTION_PAD = {
    happy:      { pleasure: 0.80, arousal: 0.50, dominance: 0.30 },
    excited:    { pleasure: 0.90, arousal: 0.90, dominance: 0.40 },
    shy:        { pleasure: 0.30, arousal: 0.20, dominance: -0.60 },
    sad:        { pleasure: -0.70, arousal: -0.30, dominance: -0.40 },
    pout:       { pleasure: -0.40, arousal: 0.10, dominance: -0.20 },
    angry:      { pleasure: -0.80, arousal: 0.80, dominance: 0.60 },
    surprised:  { pleasure: 0.10, arousal: 0.90, dominance: -0.10 },
    thoughtful: { pleasure: 0.00, arousal: -0.40, dominance: 0.00 },
    calm:       { pleasure: 0.40, arousal: -0.60, dominance: 0.10 },
    proud:      { pleasure: 0.60, arousal: 0.30, dominance: 0.80 },
    tired:      { pleasure: -0.30, arousal: -0.80, dominance: -0.50 },
    playful:    { pleasure: 0.80, arousal: 0.60, dominance: 0.20 },
    love:       { pleasure: 0.90, arousal: 0.40, dominance: 0.20 },
    neutral:    { pleasure: 0.00, arousal: 0.00, dominance: 0.00 }
  } as Record<string, PADState>;

  // ==================== 情绪 → 微动作池 ====================
  // 动作名与 08_expression_engine 的 MOTION_LIBRARY 一致
  App.EMOTION_MICRO_POOL = {
    happy:      ['happy_bounce', 'head_tilt_l', 'head_tilt_r', 'excited_clap', 'nod_small'],
    excited:    ['excited_clap', 'happy_bounce', 'playful_wink', 'nod', 'head_turn_l'],
    shy:        ['shy_twist', 'shy_look_down', 'look_away_side', 'head_tilt_l', 'head_down'],
    sad:        ['sigh', 'head_down', 'look_away_side', 'lean_back', 'head_tilt_l'],
    pout:       ['head_turn_l', 'head_turn_r', 'look_away_side', 'sigh', 'head_down'],
    angry:      ['arms_cross', 'side_turn_l', 'side_turn_r', 'head_turn_l', 'head_turn_r'],
    surprised:  ['surprise_flinch', 'head_up', 'cover_mouth', 'head_turn_l', 'head_turn_r'],
    thoughtful: ['think_glance', 'chin_touch', 'head_up', 'glance_around', 'head_tilt_r'],
    calm:       ['head_tilt_l', 'head_tilt_r', 'nod_small', 'stretch', 'glance_around'],
    proud:      ['hands_hips', 'head_up', 'arms_cross', 'head_tilt_r', 'stretch'],
    tired:      ['sigh', 'lean_back', 'head_down', 'stretch', 'head_tilt_l'],
    playful:    ['playful_wink', 'happy_bounce', 'head_tilt_l', 'head_tilt_r', 'nod'],
    love:       ['love_gaze', 'hand_chest', 'head_tilt_l', 'head_tilt_r', 'nod_small'],
    neutral:    ['head_tilt_l', 'head_tilt_r', 'glance_around', 'nod_small', 'stretch']
  } as Record<string, string[]>;

  // ==================== 运行时状态 ====================
  App.pad = { pleasure: 0, arousal: 0, dominance: 0 };
  App.padTarget = { pleasure: 0, arousal: 0, dominance: 0 };
  App.emotionSource = 'neutral';
  App.emotionParams = null;
  App._emotionHoldUntil = 0;
  App._emotionFadeMs = 800;

  // ==================== 情绪源接入 ====================
  /**
   * 设置情绪（离散标签）：同时驱动 PAD 与现有表情覆盖
   * @param emotion  情绪标签（happy/sad/angry/...）
   * @param intensity 强度 0~1
   * @param duration  持续时间（秒），结束后回落中性
   * @param source    情绪来源（llm/voice/scene/user）
   */
  App.setEmotion = function setEmotion(emotion: string, intensity?: number, duration?: number, source?: string) {
    const pad = App.EMOTION_PAD[emotion] || App.EMOTION_PAD.neutral;
    const inten = Math.min(1, intensity == null ? 1 : intensity);
    App.setPAD(pad.pleasure * inten, pad.arousal * inten, pad.dominance * inten, duration);
    App.emotionSource = emotion;
    // 表情覆盖复用现有引擎（非说话时情绪嘴型/表情）
    if (App.setEmotionOverlay) App.setEmotionOverlay(emotion, inten, duration == null ? 3 : duration);
  };

  /** 直接设置 PAD 连续值（供程序化/自定义情绪源使用） */
  App.setPAD = function setPAD(pleasure: number, arousal: number, dominance: number, duration?: number) {
    App.padTarget = {
      pleasure: THREE.MathUtils.clamp(pleasure, -1, 1),
      arousal: THREE.MathUtils.clamp(arousal, -1, 1),
      dominance: THREE.MathUtils.clamp(dominance, -1, 1)
    };
    App._emotionHoldUntil = duration ? performance.now() + duration * 1000 : 0;
  };

  /** LLM 语义情绪钩子：由回复链路在拿到情绪标签时调用 */
  App.onReplyEmotion = function onReplyEmotion(emotion: string) {
    App.setEmotion(emotion, 1, 6, 'llm');
  };

  // ==================== 回复文本情绪检测 ====================
  // 轻量关键词分类器：从 AI 回复文本推断情绪标签（后端未下发情绪标签时的兜底）
  const EMOTION_KEYWORDS: Record<string, string[]> = {
    happy: ['开心', '高兴', '哈哈', '太好了', '真棒', '喜欢', '好耶', '棒极了', '开心极了', '真不错'],
    excited: ['兴奋', '激动', '哇', '太棒了', '超棒', '疯狂', '热血', '燃', '耶', '好激动'],
    sad: ['难过', '伤心', '悲伤', '哭', '呜呜', '遗憾', '失落', '心痛', '难过极了', '委屈'],
    angry: ['生气', '愤怒', '气死', '可恶', '讨厌', '烦', '火大', '气人', '气坏了'],
    surprised: ['惊讶', '吃惊', '哇塞', '天哪', '不会吧', '居然', '竟然', '震惊', '没想到'],
    shy: ['害羞', '不好意思', '脸红', '难为情', '羞涩', '怪不好意思'],
    thoughtful: ['思考', '想想', '琢磨', '考虑', '也许', '可能', '或许', '让我想想'],
    tired: ['累', '疲惫', '困', '乏', '没劲', '唉', '好累'],
    calm: ['平静', '淡定', '放松', '安心', '放心', '别急'],
    love: ['爱你', '喜欢你', '想你', '亲爱的', '宝贝', '心动', '好喜欢'],
    playful: ['调皮', '逗你', '开玩笑', '嘻嘻', '嘿嘿', '逗你玩'],
    proud: ['骄傲', '自豪', '厉害吧', '我可是', '当然啦']
  };
  App._replyEmotionDone = false;

  /** 从回复文本检测情绪标签（无匹配返回 null） */
  App.detectReplyEmotion = function detectReplyEmotion(text: string): string | null {
    if (!text) return null;
    let best = '';
    let bestScore = 0;
    for (const emotion in EMOTION_KEYWORDS) {
      let score = 0;
      for (const kw of EMOTION_KEYWORDS[emotion]) {
        if (text.includes(kw)) score += kw.length;
      }
      if (score > bestScore) { bestScore = score; best = emotion; }
    }
    // 至少命中 2 个字符长度的关键词才判定（避免单字误触发）
    return bestScore >= 2 ? best : null;
  };

  // ==================== 每帧更新 ====================
  App.updateEmotionController = function updateEmotionController(dt: number) {
    // 超时回落中性
    if (App._emotionHoldUntil && performance.now() > App._emotionHoldUntil) {
      App.padTarget = { pleasure: 0, arousal: 0, dominance: 0 };
      App._emotionHoldUntil = 0;
      if (App.emotionSource !== 'neutral') {
        App.emotionSource = 'neutral';
        if (App.clearEmotionOverlay) App.clearEmotionOverlay();
      }
    }
    // 帧率无关平滑（时间常数 ~0.25s，接近项目 lerp 0.06~0.12 惯例）
    const k = 1 - Math.exp(-dt * 4);
    App.pad.pleasure = App.lerp(App.pad.pleasure, App.padTarget.pleasure, k);
    App.pad.arousal = App.lerp(App.pad.arousal, App.padTarget.arousal, k);
    App.pad.dominance = App.lerp(App.pad.dominance, App.padTarget.dominance, k);
    App.emotionParams = computeEmotionParams();
  };

  // ==================== PAD → 动作参数 ====================
  function computeEmotionParams(): EmotionParams {
    const p = App.pad;
    const arousal = Math.max(0, p.arousal);      // 0~1
    const dominance = Math.max(0, p.dominance);  // 0~1
    const pleasure = p.pleasure;

    // 幅度：唤醒度高 → 动作幅度大（中性 0.75，高唤醒 1.5）
    const amplitude = 0.75 + arousal * 0.75;
    // 速度：唤醒度高 → 动作速度快（中性 0.75，高唤醒 1.4）
    const speed = 0.75 + arousal * 0.65;
    // 姿态扩张：支配度高 → 姿态扩张
    const postureExpansion = dominance;
    // 手势频率：唤醒度高 → 手势频繁
    const gestureFrequency = 0.2 + arousal * 0.8;

    // 动作大类权重（pose/walk/turn/dance）
    const actionBias: Record<string, number> = {
      pose: 0.25 + (1 - arousal) * 0.35 + dominance * 0.10,
      walk: 0.25 + arousal * 0.20,
      turn: 0.25 - Math.abs(pleasure) * 0.10,
      dance: 0.25 + arousal * 0.35 + (pleasure > 0 ? 0.15 : -0.10)
    };
    // 归一化（负值钳 0）
    const sum = actionBias.pose + actionBias.walk + actionBias.turn + actionBias.dance;
    for (const k in actionBias) {
      actionBias[k] = Math.max(0, actionBias[k] / sum);
    }

    const pool = App.EMOTION_MICRO_POOL[App.emotionSource] || App.EMOTION_MICRO_POOL.neutral;
    return {
      amplitude,
      speed,
      postureExpansion,
      gestureFrequency,
      actionBias,
      microPool: pool,
      dominantEmotion: App.emotionSource
    };
  }

  App.getEmotionParams = function getEmotionParams(): EmotionParams {
    return App.emotionParams || computeEmotionParams();
  };
});
