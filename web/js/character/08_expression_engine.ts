import type { AppKernel, BoneChannel, MotionDef } from '../types/app-kernel.js';

export default (function init(App: AppKernel) {
  const { THREE } = App;
  /* ============================================================
   *  表情动作引擎 —— 动作库 / 序列器 / 眼神控制 / 情绪覆盖
   *
   *  定位：在原有「姿态库(POSES)+动作调度器(随机大类)」之上，
   *  新增一层"微动作 + 连贯编排"系统：
   *  - MOTION_LIBRARY：转头/低头/抬头/点头/摇头/弯腰/侧身/耸肩/
   *    抱臂/叉腰/挥手/捂嘴/伸懒腰/叹气等 30+ 种微动作
   *  - 序列器：playMotion / playMotionSequence 支持优先级打断、
   *    动作间 hold 停顿、ease 混合过渡 —— 保证动作连贯自然
   *  - 眼神控制：视线目标(看相机/看左/看右/抬头/低头/移开/偷看/害羞)
   *    + 眼骨驱动 + 眨眼抑制(瞪大眼/惊讶)
   *  - 情绪覆盖：setEmotionOverlay 临时表情 + 非说话情绪嘴型
   *
   *  每帧由 updateMotionSystem 产出 motionOffsets(骨骼旋转偏移)，
   *  由 07_click_interact.js 的 animateModel 叠加到现有骨骼动画上。
   * ============================================================ */

  // ==================== 优先级 ====================
  App.MOTION_PRIORITY = { idle: 0, auto: 1, rl: 2, user: 3 };

  // ==================== 全局约束 ====================
  // 所有骨骼旋转偏移的硬上限：45 度（0.785 弧度），任何动作不得突破
  App.MOTION_MAX_RAD = 0.785;
  // 动作时间轴慢速系数：0.6 = 所有动作以 1.67 倍慢速播放
  App.MOTION_SPEED = 0.6;
  // 动作偏移平滑速率：0.12 ≈ 动作结束/被打断时较快柔和复位回中
  App.MOTION_SMOOTH = 0.12;

  // ==================== 微动作库 ====================
  // 通道值约定：
  //   数值            → 单次脉冲（0 → 峰值 → 0，attack/hold/release 包络）
  //   {amp, loops}    → 振荡（sin 波，loops 为往返次数），支持 blendIn/blendOut
  //   bone 名与 07_click_interact 骨骼缓存 App.vrmBones 一致
  App.MOTION_LIBRARY = {
    // ---- 头部微动作（head/neck 通道全部 ≤0.30≈17°，叠加 lookAt 后仍 <23°）----
    head_turn_l: { dur: 1.1, head: { y: -0.13 }, neck: { y: -0.03 } },
    head_turn_r: { dur: 1.1, head: { y: 0.13 }, neck: { y: 0.03 } },
    head_up: { dur: 1.8, head: { x: -0.11 }, neck: { x: -0.04 }, gaze: 'u' },
    head_down: { dur: 2.0, head: { x: 0.13 }, neck: { x: 0.04 }, gaze: 'd', hold: 0.25 },
    head_tilt_l: { dur: 1.0, head: { z: 0.13 }, gaze: 'cam' },
    head_tilt_r: { dur: 1.0, head: { z: -0.13 }, gaze: 'cam' },
    nod: { dur: 1.4, head: { x: { amp: 0.12, loops: 3 } } },
    nod_small: { dur: 0.9, head: { x: { amp: 0.08, loops: 2 } } },
    head_shake: { dur: 1.5, head: { y: { amp: 0.15, loops: 4 } } },
    glance_around: { dur: 1.8, head: { y: { amp: 0.14, loops: 2 } }, gaze: 'l' },
    look_away_side: { dur: 1.6, head: { y: 0.13, x: 0.03 }, gaze: 'sidelong', emotion: 'shy', hold: 0.3 },

    // ---- 躯干动作（前倾/后倒角度极克制：仅轻微表示，spine ≤0.10≈5.7°）----
    bow: { dur: 2.0, spine: { x: 0.10 }, chest: { x: 0.04 }, head: { x: 0.08 }, neck: { x: 0.03 }, hold: 0.35 },
    bow_small: { dur: 1.4, spine: { x: 0.06 }, head: { x: 0.05 }, hold: 0.15 },
    lean_forward: { dur: 1.8, spine: { x: 0.04 }, chest: { x: 0.02 }, head: { x: 0.03 }, hold: 0.2 },
    lean_back: { dur: 1.7, spine: { x: -0.04 }, head: { x: -0.02 } },
    side_turn_l: { dur: 1.4, spine: { y: 0.22 }, chest: { y: 0.10 }, head: { y: -0.10 }, gaze: 'l' },
    side_turn_r: { dur: 1.4, spine: { y: -0.22 }, chest: { y: -0.10 }, head: { y: 0.10 }, gaze: 'r' },
    shrug: { dur: 1.3, leftUpperArm: { x: -0.24, z: -0.12 }, rightUpperArm: { x: -0.24, z: 0.12 }, head: { x: -0.06 }, hold: 0.2 },
    arms_cross: { dur: 2.0, leftUpperArm: { x: 0.36 }, leftLowerArm: { x: -0.70 }, rightUpperArm: { x: 0.36 }, rightLowerArm: { x: -0.70 }, leftHand: { z: 0.16 }, rightHand: { z: -0.16 }, hold: 0.4 },
    hands_hips: { dur: 2.0, leftUpperArm: { x: 0.20, z: 0.40 }, leftLowerArm: { x: -0.66 }, rightUpperArm: { x: 0.20, z: -0.18 }, rightLowerArm: { x: -0.66 }, leftHand: { z: -0.30 }, rightHand: { z: 0.30 }, head: { x: -0.04 }, hold: 0.4 },

    // ---- 手部/手臂动作 ----
    // （原 wave 招手动作已删除：单臂大幅摆动观感奇怪，用户要求移除）
    chin_touch: { dur: 2.2, rightUpperArm: { x: 0.16 }, rightLowerArm: { x: -0.40 }, rightHand: { y: 0.10, z: -0.08 }, head: { x: -0.05, y: 0.06 }, emotion: 'thoughtful', hold: 0.5 },
    cover_mouth: { dur: 1.8, leftUpperArm: { x: 0.36 }, leftLowerArm: { x: -0.70 }, leftHand: { y: 0.24, z: -0.20 }, rightUpperArm: { x: 0.08 }, emotion: 'surprised', suppressBlink: 0.8, hold: 0.3 },
    // （原 point_self 指自己动作已删除：右臂抬起观感不佳，用户要求移除）
    hand_chest: { dur: 1.8, rightUpperArm: { x: 0.20 }, rightLowerArm: { x: -0.45 }, rightHand: { z: -0.06 }, leftUpperArm: { x: 0.06 }, emotion: 'love', hold: 0.4 },
    stretch: { dur: 2.4, leftUpperArm: { z: -0.14, x: -0.26 }, leftLowerArm: { x: 0.10 }, rightUpperArm: { z: 0.14, x: -0.26 }, rightLowerArm: { x: 0.10 }, chest: { x: -0.03 }, spine: { x: -0.05 }, head: { x: -0.08 }, emotion: 'calm', hold: 0.4 },
    sigh: { dur: 1.8, head: { x: 0.08 }, spine: { x: 0.04 }, chest: { x: 0.03 }, leftUpperArm: { x: 0.12 }, rightUpperArm: { x: 0.12 }, emotion: 'tired', hold: 0.3 },
    excited_clap: { dur: 1.5, leftUpperArm: { x: { amp: 0.18, loops: 4 } }, rightUpperArm: { x: { amp: 0.18, loops: 4 } }, leftLowerArm: { x: -0.20 }, rightLowerArm: { x: -0.20 }, emotion: 'excited' },
    happy_bounce: { dur: 1.6, spine: { x: { amp: -0.06, loops: 3 } }, chest: { x: { amp: -0.045, loops: 3 } }, leftUpperArm: { x: { amp: 0.06, loops: 3 } }, rightUpperArm: { x: { amp: 0.06, loops: 3 } }, emotion: 'happy' },
    shy_twist: { dur: 1.8, spine: { y: { amp: 0.10, loops: 3 } }, head: { x: 0.12 }, gaze: 'shy', emotion: 'shy' },
    surprise_flinch: { dur: 0.9, spine: { x: { amp: 0.07, loops: 1 } }, head: { x: { amp: -0.12, loops: 1 } }, emotion: 'surprised', mouth: 0.30, suppressBlink: 1.0 },
    think_glance: { dur: 2.0, head: { x: -0.12, y: 0.04 }, gaze: 'u', rightUpperArm: { x: 0.10 }, emotion: 'thoughtful', hold: 0.4 },
    shy_look_down: { dur: 1.6, head: { x: 0.14, y: -0.07 }, gaze: 'shy', emotion: 'shy', hold: 0.3 },
    love_gaze: { dur: 1.8, head: { z: 0.06, x: -0.06 }, gaze: 'cam', emotion: 'love', suppressBlink: 0.6, hold: 0.4 },
    playful_wink: { dur: 1.2, head: { z: 0.10, y: -0.08 }, gaze: 'cam', emotion: 'playful', wink: 0.5, hold: 0.25 }
  } as Record<string, MotionDef>;

  // 空闲时随机触发的"微小动作池"（低优先级，不加情绪，避免打扰）
  // 头部动作占比刻意降低（<50%），间隔拉长，避免"头部一直转、不停歇/不复位"的观感
  App.IDLE_MICRO_POOL = [
    'head_tilt_l', 'head_tilt_r', 'glance_around', 'nod_small',
    'look_away_side', 'shy_look_down', 'think_glance', 'head_up',
    'stretch', 'sigh', 'head_turn_l', 'head_turn_r'
  ];

  // ==================== 情绪 → 表情目标映射 ====================
  App.EMOTION_EXPR = {
    happy: { happy: 0.65, fun: 0.20 },
    excited: { happy: 0.70, surprised: 0.25 },
    shy: { happy: 0.20, relaxed: 0.30 },
    sad: { sad: 0.50 },
    pout: { sad: 0.35, happy: 0.10 },
    angry: { angry: 0.55 },
    surprised: { surprised: 0.65 },
    thoughtful: { thoughtful: 0.50, sad: 0.12 },
    calm: { relaxed: 0.45 },
    proud: { happy: 0.45 },
    tired: { sad: 0.25, relaxed: 0.30 },
    playful: { fun: 0.50, happy: 0.25 },
    love: { happy: 0.80, relaxed: 0.25 }
  };
  // 非说话时的情绪嘴型（数值映射到 exprNames.mouth，如惊讶微张嘴/委屈）
  App.EMOTION_MOUTH = { surprised: 0.30, excited: 0.15, pout: 0.22, sad: 0.10, angry: 0.12, love: 0.08 };

  // ==================== 运行时状态 ====================
  App.motionOffsets = null; // 每帧重建的骨骼旋转偏移 { bone: {x,y,z} }
  App.motionQueue = [];     // 待播放序列 [{ name|def, opts }]
  App._motionActive = false;
  App._motionName = '';
  App._motionDef = null;
  App._motionElapsed = 0;
  App._motionPriority = 0;
  App._motionHoldLeft = 0;  // 序列元素间的停顿
  App._motionItemHold = 0;
  App._lastLiveOffsets = null;  // 最近一帧"有动作"的偏移快照（hold 停顿期间冻结）
  App._motionSmooth = null;     // 偏移平滑缓冲（动作结束/被打断时柔和复位回中）
  App._motionCtx = {};      // 当前动作附带的 emotion/gaze/mouth/wink

  // 眼神
  App._gazeTarget = { x: 0, y: 0, weight: 0, until: 0 };
  App._gazeCur = { x: 0, y: 0 };
  App._gazeSideSign = 1;    // 移开视线的随机侧
  App._eyeBones = null;

  // 情绪覆盖
  App.emotionOverlay = null;
  App.emotionMouth = 0;

  // 眨眼抑制（瞪大眼）
  App._blinkSuppressUntil = 0;

  // 空闲微动作
  App._idleMicroTimer = 0;
  App._idleMicroInterval = 14 + Math.random() * 12;

  // ==================== 包络工具 ====================
  function smooth01(t: number) { return t <= 0 ? 0 : t >= 1 ? 1 : t * t * (3 - 2 * t); }

  /** 单次脉冲包络：attack → hold(峰值) → release → 0 */
  function pulseEnv(p: number, atk: number, hold: number, rel: number) {
    if (p < atk) return smooth01(p / atk);
    const plateau = atk + hold;
    if (p < plateau) return 1;
    const relEnd = plateau + rel;
    if (p < relEnd) return 1 - smooth01((p - plateau) / rel);
    return 0;
  }

  /** 计算单个通道在进度 p 处的值 */
  function evalChannel(chan: any, p: number, def: MotionDef | null) {
    if (chan == null) return 0;
    if (typeof chan === 'number') {
      const atk = 0.30, rel = 0.42;
      const hold = (def && def.hold) || 0;
      return chan * pulseEnv(p, atk, hold, rel);
    }
    // 振荡型 {amp, loops, blendIn?, blendOut?}
    const amp = chan.amp != null ? chan.amp : 0.15;
    const loops = chan.loops != null ? chan.loops : 1;
    const atk = chan.blendIn != null ? chan.blendIn : 0.18;
    const rel = chan.blendOut != null ? chan.blendOut : 0.30;
    const env = pulseEnv(p, atk, 0, rel);
    return Math.sin(p * loops * Math.PI * 2) * amp * env;
  }

  // ==================== 眼神控制 ====================
  const GAZE_OFFSETS: Record<string, { x: number; y: number } | null> = {
    cam: { x: 0, y: 0 },
    l: { x: 0.03, y: -0.24 },
    r: { x: 0.03, y: 0.24 },
    u: { x: -0.16, y: 0 },
    d: { x: 0.19, y: 0 },
    away: null,       // 随机侧，移开视线
    sidelong: { x: 0.05, y: 0.40 },
    shy: { x: 0.16, y: -0.10 }
  };

  /** 设置视线目标（duration 秒，0 = 无限直到被替换） */
  App.setGaze = function setGaze(target: string, weight?: number, duration?: number) {
    const off = GAZE_OFFSETS[target];
    if (!off) { App.clearGaze(); return; }
    if (target === 'away') {
      App._gazeSideSign = Math.random() < 0.5 ? -1 : 1;
    }
    App._gazeTarget = {
      x: off.x, y: off.y * (target === 'away' ? App._gazeSideSign : 1),
      weight: Math.min(1, weight || 1),
      until: duration ? performance.now() + duration * 1000 : 0
    };
  };

  App.clearGaze = function clearGaze() {
    App._gazeTarget = { x: 0, y: 0, weight: 0, until: 0 };
  };

  App.getGazeOffsets = function getGazeOffsets() {
    return { x: App._gazeCur.x, y: App._gazeCur.y };
  };

  /** 眨眼抑制：瞪大眼/惊讶时暂停普通眨眼 */
  App.suppressBlink = function suppressBlink(seconds?: number) {
    App._blinkSuppressUntil = Math.max(App._blinkSuppressUntil, performance.now() + (seconds || 1) * 1000);
  };
  App.blinkSuppressed = function blinkSuppressed() {
    return performance.now() < App._blinkSuppressUntil;
  };

  // ==================== 情绪覆盖 ====================
  App.setEmotionOverlay = function setEmotionOverlay(emotion: string, intensity?: number, duration?: number) {
    const map = App.EMOTION_EXPR[emotion];
    if (!map) { App.clearEmotionOverlay(); return; }
    const inten = Math.min(1, intensity == null ? 1 : intensity);
    const targets: Record<string, number> = {};
    for (const k in map) targets[k] = Math.min(1, (map[k] || 0) * inten);
    App.emotionOverlay = {
      emotion: emotion,
      until: performance.now() + (duration == null ? 3 : duration) * 1000,
      fadeMs: 600,
      targets: targets,
      mouth: (App.EMOTION_MOUTH[emotion] || 0) * inten
    };
  };

  App.clearEmotionOverlay = function clearEmotionOverlay() {
    App.emotionOverlay = null;
  };

  /** 供 07 updateExpressions 合并：返回按剩余时间衰减后的目标 {expr: value} */
  App.getEmotionOverlayTargets = function getEmotionOverlayTargets() {
    const ov = App.emotionOverlay;
    if (!ov) return null;
    const remain = ov.until - performance.now();
    if (remain <= 0) { App.emotionOverlay = null; return null; }
    const fade = Math.min(1, remain / ov.fadeMs);
    const out: Record<string, number> = {};
    for (const k in ov.targets) out[k] = (ov.targets[k] || 0) * fade;
    return out;
  };

  App.emotionOverlayActive = function emotionOverlayActive() {
    return !!(App.emotionOverlay && (App.emotionOverlay.until - performance.now()) > 0);
  };

  // ==================== 眼骨驱动 ====================
  App._ensureEyeBones = function _ensureEyeBones() {
    if (App._eyeBones) return App._eyeBones;
    App._eyeBones = { left: null, right: null };
    if (App.vrm && App.vrm.humanoid) {
      try {
        App._eyeBones!.left = App.vrm.humanoid.getNormalizedBoneNode('leftEye');
        App._eyeBones!.right = App.vrm.humanoid.getNormalizedBoneNode('rightEye');
      } catch (e) { /* 无眼骨 */ }
    }
    return App._eyeBones;
  };

  function applyEyeGaze() {
    const bones = App._ensureEyeBones();
    if (!bones || (!bones.left && !bones.right)) return;
    const gx = App._gazeCur.x * 0.8;
    const gy = App._gazeCur.y * 0.8;
    const cap = 0.14;
    const ex = THREE.MathUtils.clamp(gx, -cap, cap);
    const ey = THREE.MathUtils.clamp(gy, -cap, cap);
    if (bones.left) { bones.left.rotation.x = App.lerp(bones.left.rotation.x || 0, ex, 0.12); bones.left.rotation.y = App.lerp(bones.left.rotation.y || 0, ey, 0.12); }
    if (bones.right) { bones.right.rotation.x = App.lerp(bones.right.rotation.x || 0, ex, 0.12); bones.right.rotation.y = App.lerp(bones.right.rotation.y || 0, ey, 0.12); }
  }

  function resetEyeGaze() {
    const bones = App._ensureEyeBones();
    if (!bones || (!bones.left && !bones.right)) return;
    if (bones.left) { bones.left.rotation.x = 0; bones.left.rotation.y = 0; }
    if (bones.right) { bones.right.rotation.x = 0; bones.right.rotation.y = 0; }
  }

  // ==================== 序列器 ====================
  App.playMotion = function playMotion(name: string, opts?: any) {
    App.playMotionSequence([name], opts);
  };

  /**
   * 播放一连串动作（支持连贯编排）
   * @param {Array} seq 元素为动作名(string) 或 { motion, hold }（hold=播放完停顿秒数）
   * @param {Object} [opts] { priority:'idle'|'auto'|'rl'|'user', onDone, keepExpr }
   */
  App.playMotionSequence = function playMotionSequence(seq: any[], opts?: any) {
    if (!seq || seq.length === 0) return;
    opts = opts || {};
    const prio = App.MOTION_PRIORITY[opts.priority || 'idle'] != null
      ? App.MOTION_PRIORITY[opts.priority || 'idle']
      : App.MOTION_PRIORITY.rl;
    // 更低优先级打断当前高优先级动作 → 拒绝
    if (App._motionActive && prio < App._motionPriority) return;
    App.motionQueue = seq.map(s => typeof s === 'string' ? { motion: s } : s);
    App._motionHoldLeft = 0;
    App._motionPriority = prio;
    App._motionOnDone = opts.onDone || null;
    App._motionKeepExpr = !!opts.keepExpr;
    App._motionActive = true;
    App._startNextMotion();
  };

  App.interruptMotions = function interruptMotions() {
    App.motionQueue = [];
    App._motionActive = false;
    App._motionDef = null;
    App._motionName = '';
    App._motionHoldLeft = 0;
    App._motionCtx = {};
  };

  App._startNextMotion = function _startNextMotion() {
    // 跳过未知动作
    App._lastLiveOffsets = null;
    while (App.motionQueue.length > 0) {
      const item = App.motionQueue.shift()!;
      const def = typeof item.motion === 'string' ? App.MOTION_LIBRARY[item.motion] : item.motion;
      if (!def || typeof def !== 'object' || !def.dur) continue;
      App._motionDef = def;
      App._motionName = typeof item.motion === 'string' ? item.motion : '(inline)';
      App._motionElapsed = 0;
      App._motionItemHold = item.hold || 0;
      // 动作上下文（情绪/眼神/嘴型/眨眼抑制）
      App._motionCtx = {};
      if (def.emotion) {
        App.setEmotionOverlay(def.emotion, def.emotionIntensity || 1, Math.max(def.dur + 0.5, 2.5));
      }
      if (def.gaze) {
        App.setGaze(def.gaze, 1, def.dur + 0.8);
      }
      if (def.mouth) App._motionCtx.mouth = def.mouth;
      if (def.suppressBlink) App.suppressBlink(def.suppressBlink);
      if (def.wink) App._motionCtx.wink = def.wink;
      return;
    }
    // 队列为空
    App._motionActive = false;
    App._motionDef = null;
    const done = App._motionOnDone;
    App._motionOnDone = null;
    if (!App._motionKeepExpr) {
      App.clearEmotionOverlay();
    }
    App.clearGaze();
    if (done) done();
  };

  // ==================== 空闲微动作调度 ====================
  App._pickIdleMicro = function _pickIdleMicro() {
    const pool = App.IDLE_MICRO_POOL;
    return pool[Math.floor(Math.random() * pool.length)];
  };

  // ==================== 每帧主更新 ====================
  // 45° 硬钳制 + NaN 防护：任何非有限数值一律置 0（NaN 会经四元数传播导致
  // 骨骼 360° 打转且无法复位，必须在上游拦截）
  function clampOffsets() {
    const max = App.MOTION_MAX_RAD || 0.785;
    const off = App.motionOffsets!;
    for (const bn in off) {
      const o = off[bn];
      o.x = Number.isFinite(o.x) ? THREE.MathUtils.clamp(o.x, -max, max) : 0;
      o.y = Number.isFinite(o.y) ? THREE.MathUtils.clamp(o.y, -max, max) : 0;
      o.z = Number.isFinite(o.z) ? THREE.MathUtils.clamp(o.z, -max, max) : 0;
    }
  }

  // 快照"有动作"帧（供序列 hold 停顿冻结）
  function updateLiveSnapshot() {
    const off = App.motionOffsets!;
    let hasLive = false;
    for (const bn in off) {
      const o = off[bn];
      if (o.x || o.y || o.z) { hasLive = true; break; }
    }
    if (hasLive) {
      App._lastLiveOffsets = {};
      for (const bn in off) {
        const o = off[bn];
        App._lastLiveOffsets[bn] = { x: o.x, y: o.y, z: o.z };
      }
    }
  }

  // 平滑输出：目标突变（动作切换/结束/打断）时柔和过渡，最终自然复位到 0
  // NaN 防护：目标或缓冲出现非有限数一律按 0 处理（0 即复位目标），杜绝残留污染
  function smoothOffsets() {
    if (!App._motionSmooth) {
      App._motionSmooth = {};
      for (const bn in App.motionOffsets!) App._motionSmooth[bn] = { x: 0, y: 0, z: 0 };
    }
    const k = App.MOTION_SMOOTH || 0.10;
    for (const bn in App.motionOffsets!) {
      const t = App.motionOffsets![bn];
      const s = App._motionSmooth[bn];
      const tx = Number.isFinite(t.x) ? t.x : 0;
      const ty = Number.isFinite(t.y) ? t.y : 0;
      const tz = Number.isFinite(t.z) ? t.z : 0;
      const sx = App.lerp(s.x, tx, k);
      const sy = App.lerp(s.y, ty, k);
      const sz = App.lerp(s.z, tz, k);
      s.x = sx; s.y = sy; s.z = sz;
      t.x = Number.isFinite(sx) ? sx : 0;
      t.y = Number.isFinite(sy) ? sy : 0;
      t.z = Number.isFinite(sz) ? sz : 0;
    }
  }

  App.updateMotionSystem = function updateMotionSystem(dt: number) {
    // 复用偏移表对象（每帧清零）：避免每帧重建 ~13 个对象产生 GC 微卡顿
    // （VR 90Hz 下每帧分配会累积触发垃圾回收，导致角色"卡帧"）
    if (!App._motionOffsetsPool) {
      App._motionOffsetsPool = {
        spine: { x: 0, y: 0, z: 0 }, chest: { x: 0, y: 0, z: 0 }, upperChest: { x: 0, y: 0, z: 0 },
        neck: { x: 0, y: 0, z: 0 }, head: { x: 0, y: 0, z: 0 },
        hips: { x: 0, y: 0, z: 0 },
        leftUpperArm: { x: 0, y: 0, z: 0 }, rightUpperArm: { x: 0, y: 0, z: 0 },
        leftLowerArm: { x: 0, y: 0, z: 0 }, rightLowerArm: { x: 0, y: 0, z: 0 },
        leftHand: { x: 0, y: 0, z: 0 }, rightHand: { x: 0, y: 0, z: 0 },
        leftUpperLeg: { x: 0, y: 0, z: 0 }, rightUpperLeg: { x: 0, y: 0, z: 0 }
      };
    }
    const _pool = App._motionOffsetsPool;
    for (const k in _pool) { _pool[k].x = 0; _pool[k].y = 0; _pool[k].z = 0; }
    App.motionOffsets = _pool;

    // ---- 视线缓动（放缓，更柔和）----
    const gt = App._gazeTarget;
    if (gt.until && performance.now() > gt.until) {
      gt.weight = 0;
    }
    const tgtX = gt.x * gt.weight;
    const tgtY = gt.y * gt.weight;
    App._gazeCur.x = App.lerp(App._gazeCur.x, tgtX, 0.05);
    App._gazeCur.y = App.lerp(App._gazeCur.y, tgtY, 0.05);
    applyEyeGaze();

    // ---- 情绪嘴型（非说话时）----
    if (App.emotionOverlayActive() && App.currentState !== App.State.SPEAKING) {
      const ov = App.emotionOverlay!;
      const remain = ov.until - performance.now();
      const fade = Math.min(1, remain / ov.fadeMs);
      App.emotionMouth = App.lerp(App.emotionMouth || 0, (ov.mouth || 0) * fade, 0.09);
    } else {
      App.emotionMouth = App.lerp(App.emotionMouth || 0, 0, 0.16);
      if (App.emotionMouth < 0.005) App.emotionMouth = 0;
    }

    // 全局慢速时间轴：MOTION_SPEED=0.6 → 每真实秒只推进 0.6 秒动作时间（动作 1.67 倍慢速）
    const msDt = dt * (App.MOTION_SPEED || 1);

    // ---- 序列元素间的停顿（冻结姿态，避免跳变）----
    if (App._motionHoldLeft > 0) {
      App._motionHoldLeft -= msDt;
      if (App._motionHoldLeft <= 0) {
        App._motionHoldLeft = 0;
        App._motionDef = null;
        App._startNextMotion();
      } else if (App._lastLiveOffsets) {
        // 冻结峰值：重新应用最近一帧"有动作"的姿态偏移
        for (const bn in App._lastLiveOffsets) {
          const b = App.motionOffsets![bn];
          const h = App._lastLiveOffsets[bn];
          if (b && h) { b.x = h.x; b.y = h.y; b.z = h.z; }
        }
      }
    } else if (App._motionActive && App._motionDef) {
      App._motionElapsed += msDt;
      const def = App._motionDef!;
      const p = Math.min(1, App._motionElapsed / def.dur);

      // 行走/跳舞时只保留头部/躯干/眼神，避免与全身动画冲突
      const walkBlock = App.currentAction && App.currentAction.type === App.ActionType.WALK;
      const danceBlock = App.currentAction && App.currentAction.type === App.ActionType.DANCE;
      const off = App.motionOffsets!;
      const BONES = ['head', 'neck', 'spine', 'chest', 'upperChest', 'hips',
        'leftUpperArm', 'rightUpperArm', 'leftLowerArm', 'rightLowerArm',
        'leftHand', 'rightHand', 'leftUpperLeg', 'rightUpperLeg'];

      for (const bn of BONES) {
        const chan = def[bn];
        if (chan == null || !off[bn]) continue;
        if ((walkBlock || danceBlock) && bn !== 'head' && bn !== 'neck' && bn !== 'spine' && bn !== 'chest' && bn !== 'upperChest' && bn !== 'hips') continue;
        const ch = chan as BoneChannel;
        off[bn].x = evalChannel(ch.x, p, def);
        off[bn].y = evalChannel(ch.y, p, def);
        off[bn].z = evalChannel(ch.z, p, def);
      }

      // 俏皮眨眼：通过 wink 触发一次挤眼
      if (def.wink && App._motionElapsed < msDt * 2) {
        App.blinkType = Math.random() < 0.5 ? 'winkLeft' : 'winkRight';
        App.blinkPhase = 0;
        App.blinkDuration = 0.4;
        App.blinkTimer = 9999; // 立即触发
      }

      if (p >= 1) {
        if (App._motionItemHold > 0) {
          App._motionHoldLeft = App._motionItemHold;
          App._motionDef = null;
        } else {
          App._startNextMotion();
        }
      }
    } else {
      // ---- 空闲微动作调度（无 RL 主导时的基础生动性）----
      if (App.currentState === App.State.IDLE) {
        // RL 刚执行过动作时让位
        const rlRecently = App._expressionRL && App._expressionRL._lastActionAt &&
          (Date.now() - App._expressionRL._lastActionAt) < 8000;
        if (!rlRecently) {
          App._idleMicroTimer += dt;
          if (App._idleMicroTimer >= App._idleMicroInterval) {
            App._idleMicroTimer = 0;
            // 手势频率驱动：高唤醒情绪（兴奋/惊讶）微动作更频繁，低唤醒（平静/困倦）更稀疏
            const gf = (App.getEmotionParams && App.getEmotionParams().gestureFrequency) ?? 0.2;
            App._idleMicroInterval = 3 + (1 - gf) * 22;
            App.playMotion(App._pickIdleMicro(), { priority: 'idle' });
          }
        }
      }
    }

    // ---- 统一后处理：45° 钳制 → 快照 → 平滑复位 ----
    clampOffsets();
    updateLiveSnapshot();
    smoothOffsets();
  };

  // ==================== 工具：供外部查询 ====================
  App.motionSystemActive = function motionSystemActive() {
    return App._motionActive;
  };
  App.motionName = function motionName() {
    return App._motionName || '';
  };
});
