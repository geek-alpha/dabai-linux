import type { AppKernel } from '../types/app-kernel.js';
import { AIAutonomyController } from './ai-autonomy-controller.ts';

/* ============================================================
 *  大厅 AI 自主行动控制器初始化（23_ai_autonomy）
 *
 *  从原 game-mode-manager 中拆出：控制器本体（AI 自主行动能力）与
 *  游戏无关，游戏引擎删除后由本模块单独装配，供大厅使用。
 *  - App.aiAutonomyController：09_websocket 收到 behavior_cmd 时投递
 *  - App.aiAutonomy：兼容旧引用名
 * ============================================================ */
export default (function init(App: AppKernel) {
  try {
    const ctrl = new AIAutonomyController(App);
    App.aiAutonomyController = ctrl;
    App.aiAutonomy = ctrl;
    console.log('[AIAutonomy] 大厅自主行动控制器已就绪');
  } catch (e) {
    console.warn('[AIAutonomy] 初始化失败:', (e as Error).message);
  }
});
