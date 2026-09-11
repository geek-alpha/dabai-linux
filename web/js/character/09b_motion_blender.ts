import type { AppKernel } from '../types/app-kernel.js';

export default (function init(App: AppKernel) {
  const { THREE } = App;
  /* ============================================================
   *  分层融合器 —— 情绪参数驱动现有动作系统
   *
   *  定位：情绪控制器的"执行层"。消费 emotion_controller 产出的
   *  EmotionParams，驱动 02_three_scene 的动作大类调度与
   *  08_expression_engine 的微动作系统：
   *  - 动作大类选择：按情绪权重（actionBias）加权随机，替代纯随机
   *  - 微动作选择：从情绪倾向的动作池（microPool）选取
   *  - 幅度缩放：按 arousal 缩放 motionOffsets（高唤醒 → 动作更大）
   *  - 速度缩放：按 arousal 叠加 MOTION_SPEED（高唤醒 → 动作更快）
   *
   *  通过包装 updateMotionSystem 挂入渲染循环，无需改动既有模块。
   * ============================================================ */

  // ==================== 情绪驱动动作大类选择 ====================
  // 覆盖 02_three_scene 的随机选择：按情绪权重加权随机
  App.pickNextActionType = function pickNextActionType() {
    const params = App.getEmotionParams();
    const bias = params.actionBias;
    const types = [App.ActionType.POSE, App.ActionType.WALK, App.ActionType.TURN, App.ActionType.DANCE];
    const keys = ['pose', 'walk', 'turn', 'dance'];
    let r = Math.random();
    for (let i = 0; i < types.length; i++) {
      r -= bias[keys[i]] || 0.25;
      if (r <= 0) return types[i];
    }
    return types[types.length - 1];
  };

  // ==================== 情绪驱动微动作选择 ====================
  // 覆盖 08_expression_engine 的随机选择：从情绪倾向的微动作池选取
  App._pickIdleMicro = function _pickIdleMicro() {
    const params = App.getEmotionParams();
    const pool = params.microPool;
    return pool[Math.floor(Math.random() * pool.length)];
  };

  // ==================== 每帧驱动 + 幅度/速度缩放 ====================
  // 包装 updateMotionSystem：先驱动情绪控制器，再按情绪参数缩放
  const origUpdateMotionSystem = App.updateMotionSystem;
  App.updateMotionSystem = function updateMotionSystem(dt: number) {
    // 先更新情绪（PAD 平滑 + 参数计算），供下方缩放使用
    if (App.updateEmotionController) App.updateEmotionController(dt);
    const params = App.getEmotionParams();
    // 速度系数叠加到全局慢速系数（原 MOTION_SPEED=0.6）
    App.MOTION_SPEED = 0.6 * params.speed;
    if (origUpdateMotionSystem) origUpdateMotionSystem(dt);
    // 幅度缩放：高唤醒 → 微动作幅度更大（重新钳制 45° 硬上限）
    const amp = params.amplitude;
    const off = App.motionOffsets;
    if (!off) return;
    const max = App.MOTION_MAX_RAD || 0.785;
    if (amp !== 1) {
      for (const bn in off) {
        const o = off[bn];
        o.x = THREE.MathUtils.clamp(o.x * amp, -max, max);
        o.y = THREE.MathUtils.clamp(o.y * amp, -max, max);
        o.z = THREE.MathUtils.clamp(o.z * amp, -max, max);
      }
    }
    // 姿态扩张：dominance 高（骄傲/生气）→ 挺胸、肩背打开、微抬下巴
    // 叠加在幅度缩放之后，保持克制（≤4.1°），与微动作共存不冲突
    const pe = params.postureExpansion;
    if (pe > 0.01) {
      const s = off.spine, c = off.chest, uc = off.upperChest, h = off.head;
      const la = off.leftUpperArm, ra = off.rightUpperArm;
      if (s) s.x = THREE.MathUtils.clamp(s.x - pe * 0.045, -max, max);
      if (c) c.x = THREE.MathUtils.clamp(c.x - pe * 0.03, -max, max);
      if (uc) uc.x = THREE.MathUtils.clamp(uc.x - pe * 0.03, -max, max);
      if (h) h.x = THREE.MathUtils.clamp(h.x - pe * 0.03, -max, max);
      if (la) la.z = THREE.MathUtils.clamp(la.z - pe * 0.09, -max, max);
      if (ra) ra.z = THREE.MathUtils.clamp(ra.z + pe * 0.09, -max, max);
    }
  };

  // 供外部手动触发（调试/事件驱动）
  App.updateEmotionBlender = function updateEmotionBlender(dt: number) {
    if (App.updateEmotionController) App.updateEmotionController(dt);
  };

  // 实测调参辅助：受控测量情绪姿态（停掉所有动作系统后读取实际骨骼旋转）
  App.measureEmotionPose = async function measureEmotionPose(emotion: string) {
    App.setEmotion(emotion, 1, 30);
    App.nextActionTimer = 99999;
    App.currentAction = null;
    App._motionActive = false;
    App._motionDef = null;
    App._motionHoldLeft = 0;
    App._idleMicroTimer = 0;
    App._idleMicroInterval = 99999;
    await new Promise(r => setTimeout(r, 1500));
    const b = App.vrmBones;
    const g = (o: any) => o ? {
      x: Math.round(o.rotation.x * 1000) / 1000,
      y: Math.round(o.rotation.y * 1000) / 1000,
      z: Math.round(o.rotation.z * 1000) / 1000
    } : null;
    const p = App.getEmotionParams();
    return {
      emotion,
      pe: Math.round(p.postureExpansion * 1000) / 1000,
      amp: Math.round(p.amplitude * 1000) / 1000,
      head: g(b.head), neck: g(b.neck), spine: g(b.spine), chest: g(b.chest),
      upperChest: g(b.upperChest),
      leftUpperArm: g(b.leftUpperArm), rightUpperArm: g(b.rightUpperArm)
    };
  };
});
