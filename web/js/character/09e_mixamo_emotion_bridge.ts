import type { AppKernel } from '../types/app-kernel.js';

export default (function init(App: AppKernel) {
  /* ============================================================
   *  情绪 → Mixamo 动作桥接 —— 情绪变化自动触发对应全身动作
   *
   *  定位：情绪控制器与 Mixamo 动作库之间的"联动层"。
   *  - 情绪变化时自动选择对应情绪的待机/表达动作
   *  - 支持两种模式：idle-only（仅待机循环）和 expressive（情绪触发表达动作）
   *  - 动作优先级高于程序式微动作，可被打断
   *
   *  用法：
   *    App.enableMixamoEmotion(true)   // 开启情绪驱动
   *    App.setMixamoEmotionMode('idle') // 仅待机动作
   * ============================================================ */

  // ==================== 配置 ====================
  App._mixamoEmotionEnabled = false;
  App._mixamoEmotionMode = 'expressive'; // 'idle' | 'expressive'
  App._mixamoLastEmotion = 'neutral';
  App._mixamoEmotionCooldown = 0;

  // ==================== API ====================
  /**
   * 开启/关闭情绪驱动的 Mixamo 动作
   */
  App.enableMixamoEmotion = function enableMixamoEmotion(enabled: boolean) {
    App._mixamoEmotionEnabled = enabled;
    if (enabled) {
      // 立即应用当前情绪
      applyEmotionToMixamo(App.emotionSource);
    } else {
      App.stopMixamoClip();
    }
    console.log(`[Mixamo-Emotion] 情绪驱动: ${enabled ? '开启' : '关闭'}`);
  };

  /**
   * 设置情绪驱动模式
   * @param mode  'idle' = 仅待机循环动作 | 'expressive' = 情绪触发表达动作
   */
  App.setMixamoEmotionMode = function setMixamoEmotionMode(mode: string) {
    App._mixamoEmotionMode = mode;
  };

  // ==================== 核心逻辑 ====================
  /**
   * 将情绪应用到 Mixamo 动作
   */
  function applyEmotionToMixamo(emotion: string) {
    if (!App._mixamoEmotionEnabled) return;
    if (!App._animLibraryLoaded) return;
    if (emotion === App._mixamoLastEmotion) return;

    const now = performance.now();
    if (now < App._mixamoEmotionCooldown) return;
    App._mixamoEmotionCooldown = now + 1500; // 1.5s 冷却，避免频繁切换

    App._mixamoLastEmotion = emotion;

    if (App._mixamoEmotionMode === 'idle') {
      // 仅待机模式：切换到对应情绪的待机动作
      playIdleForEmotion(emotion);
    } else {
      // 表达模式：先播一个表达动作，播完回到程序式动画
      playExpressiveForEmotion(emotion);
    }
  }

  /**
   * 播放对应情绪的待机循环动作
   * 统一规则：idle 分类内“情绪匹配子池随机 → 整类随机”，与统一调度共用
   * pickLibraryActionByScene；情绪待机也是随机轮换而不是钉死一个动作。
   * 循环待机限时 4~8s（调度器还会在播够 2.5s 后随时轮换），
   * 绝不长时间保持同一个待机动作。
   */
  function playIdleForEmotion(emotion: string) {
    // 统一调度 API：idle 分类内随机挑一个在盘待机动作（优先情绪匹配）
    if (App.pickLibraryActionByScene) {
      const name = App.pickLibraryActionByScene('idle', emotion, { hold: [4, 8] });
      if (name) return;
      // 专属动作配置生效时：从允许的循环动作中随机选一个作为待机
      if (App.pickAllowedLoopClip) {
        const fallback = App.pickAllowedLoopClip(emotion);
        if (fallback) {
          App.playLibraryClip(fallback, { loop: true });
          return;
        }
      }
      return;
    }

    // 兜底（无统一调度 API 的旧环境）：固定查找 → idle_normal
    const config = App._animLibraryConfig;
    if (config && config.categories && config.categories['idle']) {
      const idleAnims = config.categories['idle'].animations;
      const match = idleAnims.find(a => a.emotion === emotion && App.isAnimAllowed(a.name));
      if (match && App.mixamoClips[match.name]) {
        App.playLibraryClip(match.name, { loop: true });
        return;
      }
      const normal = idleAnims.find(a => a.name === 'idle_normal' && App.isAnimAllowed(a.name));
      if (normal && App.mixamoClips[normal.name]) {
        App.playLibraryClip(normal.name, { loop: true });
        return;
      }
    }
    if (App.pickAllowedLoopClip) {
      const fallback = App.pickAllowedLoopClip(emotion);
      if (fallback) {
        App.playLibraryClip(fallback, { loop: true });
      }
    }
  }

  /**
   * 播放一个情绪表达动作，播完回到程序式动画
   */
  function playExpressiveForEmotion(emotion: string) {
    let clipName = App.playEmotionClip(emotion, { loop: false });

    // 专属动作配置生效时：情绪无匹配动作 → 从允许的动作中随机选一个表达
    if (!clipName && App.pickAllowedClipByEmotion) {
      clipName = App.pickAllowedClipByEmotion(emotion);
      if (clipName) App.playLibraryClip(clipName, { loop: false });
    }

    if (clipName) {
      // 单次表达动作：让它完整演完（duration）后立即释放控制权（取消动作混合，
      // 无尾收软着陆）。此层只在兜底出手——finished 事件极罕见丢失时确保仍能交权。
      // 兜底计时 = 完整演出时长：绝不中途掐短正在完整播放的动作。
      const info = App.mixamoClips[clipName];
      if (info?.clip?.duration) {
        setTimeout(() => {
          const stillMine = App._mixamoActiveClip === clipName && !App._mixamoTailActive;
          if (stillMine) App.stopMixamoClip();
        }, info.clip.duration * 1000);
      }
    }
    // 没有表达动作 → 保持程序式动画，不播放 Mixamo 待机
  }

  // ==================== 挂入情绪系统 ====================
  // 包装 setEmotion，在情绪变化时触发 Mixamo 动作
  const origSetEmotion = App.setEmotion;
  App.setEmotion = function setEmotion(emotion: string, intensity?: number, duration?: number, source?: string) {
    origSetEmotion(emotion, intensity, duration, source);
    // 延迟一帧，等情绪参数更新后再应用
    requestAnimationFrame(() => {
      applyEmotionToMixamo(emotion);
    });
  };

  // 包装 onReplyEmotion
  const origOnReplyEmotion = App.onReplyEmotion;
  App.onReplyEmotion = function onReplyEmotion(emotion: string) {
    origOnReplyEmotion(emotion);
    requestAnimationFrame(() => {
      applyEmotionToMixamo(emotion);
    });
  };
});
