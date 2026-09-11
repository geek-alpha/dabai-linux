// ============================================================
// 24_footstep_sfx.ts —— 走路脚步音效（全局）
// 音源：Mixkit "Crunchy footsteps loop"（免费免署名，Mixkit License）
// 机制：Audio 播放池轮换 + 按行走相位每半周期触发一步（左右脚交替）
// 接入：02_three_scene.js 的 applyFullBodyWalkAnimation（大厅漫步 / AI 驱动 /
//       大厅漫步 / AI 驱动移动统一经过该函数，因此"全局应用"）
// ============================================================
import type { AppKernel } from '../types/app-kernel.js';

export default function init_24_footstep_sfx(App: AppKernel) {
  // 绝对路径：服务端将 /static 挂载到 web 目录，文件位于 web/assets/sounds/ 下。
  // 必须用 /static/ 前缀，相对路径会解析成 /assets/... 导致 404 加载失败
  const SRC = '/static/assets/sounds/footstep_loop.mp3';
  const POOL_SIZE = 5;
  const pool: HTMLAudioElement[] = [];
  let idx = 0;
  let lastStepKey = -1;

  // ---------- 初始化：预创建 Audio 播放池 ----------
  App.initFootstepSFX = function initFootstepSFX() {
    if (pool.length) return true;
    try {
      for (let i = 0; i < POOL_SIZE; i++) {
        const a = new Audio();
        a.src = SRC;
        a.preload = 'auto';
        a.volume = 0; // 初始静音，解锁后按实际音量播放
        // 加载失败检测：路径错误/网络失败时立即在控制台给出明确提示
        a.addEventListener('error', () => {
          App._footstepSFXReady = false;
          console.warn('[FootstepSFX] 脚步音效加载失败，请确认文件存在: ' + SRC);
        }, { once: false });
        pool.push(a);
      }
      App._footstepSFXReady = true;
    } catch (e) {
      App._footstepSFXReady = false;
      console.warn('[FootstepSFX] 脚步音效初始化失败:', e);
    }
    return App._footstepSFXReady;
  };

  // ---------- 解锁：浏览器要求用户交互后才能出声 ----------
  // 首次点击/按键时静默播放一次以解锁音频策略；之后脚步随行走自然出声
  // 关键：必须遍历解锁【全部】池元素 —— Chrome 自动播放策略按元素拦截，
  // 只解锁 pool[0] 会导致轮换到 pool[1..4] 时 play() 被拒（无声）
  App.unlockFootstepSFX = function unlockFootstepSFX() {
    if (!pool.length) App.initFootstepSFX();
    if (!App._footstepSFXReady) return;
    for (const a of pool) {
      try {
        const p = a.play();
        if (p && p.catch) p.catch(() => {});
        a.pause();
        a.currentTime = 0;
      } catch (e) { /* ignore */ }
    }
    App._footstepUnlocked = true;
  };

  // ---------- 全局静音开关 ----------
  App.setFootstepMuted = function setFootstepMuted(muted: boolean) {
    App.footstepMuted = !!muted;
  };

  // ---------- 播放一步 ----------
  App.playFootstep = function playFootstep(vol?: number) {
    if (!App._footstepSFXReady || App.footstepMuted || !pool.length) return;
    const a = pool[idx % pool.length];
    idx++;
    try {
      a.pause();
      a.currentTime = 0;
      const v = (vol == null ? 0.5 : vol) * (App.sfxVolume == null ? 1 : App.sfxVolume);
      a.volume = Math.max(0.02, Math.min(1, v));
      a.playbackRate = 0.95 + Math.random() * 0.1; // 轻微随机音高，避免机械感
      const p = a.play();
      if (p && p.catch) {
        p.catch(() => {
          // 播放被拦截（如自动播放策略）：补齐解锁后重试一次
          if (!App._footstepUnlocked) {
            App.unlockFootstepSFX();
            a.play().catch(() => {});
          }
        });
      }
    } catch (e) { /* ignore */ }
  };

  // ---------- 相位驱动：每过 π（半步）触发一次，左右脚交替 ----------
  // 由 applyFullBodyWalkAnimation 在 walkActive 时每帧调用
  App.updateFootstepSFX = function updateFootstepSFX(phase: number, speedFactor?: number) {
    if (!App._footstepSFXReady) return;
    const key = Math.floor(phase / Math.PI);
    if (key !== lastStepKey) {
      lastStepKey = key;
      const s = speedFactor || 1;
      App.playFootstep(0.40 + Math.min(0.12, s * 0.05)); // 步速快时略响
    }
  };

  // ---------- 起步复位：停止后重新走，第一步必出声 ----------
  App.resetFootstepPhase = function resetFootstepPhase() {
    lastStepKey = -1;
  };

  // ---------- 首次用户交互自动解锁 ----------
  if (typeof window !== 'undefined') {
    const unlockOnce = function () {
      App.unlockFootstepSFX();
    };
    window.addEventListener('pointerdown', unlockOnce, { once: false });
    window.addEventListener('keydown', unlockOnce, { once: false });
  }

  // 立即初始化播放池（浏览器环境；Node 下 Audio 不存在则标记未就绪，不影响测试）
  App.initFootstepSFX();
}
