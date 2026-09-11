/* 全局类型补齐 —— 阶段 1（类型地基）
 * 覆盖现有 JS 里的非标准 window/navigator 用法（清单见 app-inventory.json）：
 *   - window._restoredScene：场景状态恢复缓存（09_websocket 写入/消费）
 *   - window._gameManager：游戏模式管理器实例（init-game-mode 写入）
 *   - window._animFrame：VR / 点击交互的动画帧句柄
 *   - window.webkitAudioContext：Safari 旧版 AudioContext
 *   - navigator.deviceMemory：Chrome 非标准，性能降级判断用
 * Navigator.xr 由 @types/webxr 提供（tsconfig 已显式引入 types）。 */

import type { ScenePersistState } from './app-kernel.js';

declare global {
  interface Window {
    /** 场景状态恢复缓存：restoreSceneState 写入，模型加载后 applySavedPositions 消费 */
    _restoredScene?: ScenePersistState;
    /** 游戏模式管理器实例 */
    _gameManager?: any;
    /** VR / 点击交互模块的 requestAnimationFrame 句柄 */
    _animFrame?: number;
    /** Safari 旧版 AudioContext */
    webkitAudioContext?: typeof AudioContext;
    /* ---- app.ts 末尾的调试暴露（Object.defineProperty getter） ---- */
    _App?: unknown;
    _GameManager?: unknown;
    _expressionRL?: unknown;
    _motionLib?: unknown;
    /* ---- 19_boot 的调试暴露（getter 动态读取 App 实例） ---- */
    _scene?: unknown;
    _camera?: unknown;
    _modelGroup?: unknown;
    _smoothRotY?: unknown;
    _currentAvatar?: unknown;
  }

  interface Navigator {
    /** Chrome 非标准：设备内存（GB），08_state_switch 用于低配降级 */
    deviceMemory?: number;
  }

  interface HTMLAudioElement {
    /** TTS 音频的 blob URL（10_tts_lipsync 挂载，08_state_switch 内存巡检释放） */
    _blobUrl?: string | null;
  }
}

export {};
