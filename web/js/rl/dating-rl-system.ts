/* ============================================================
 * DatingRLSystem — 向后兼容的薄封装层
 *
 * @deprecated 此类已废弃，将在未来版本中移除。
 *             请使用 UnifiedDatingSystem 替代。
 *
 * 本文件保持 DatingRLSystem 的完整公开 API 不变，
 * 所有内部逻辑委托给 UnifiedDatingSystem。
 *
 * 统一架构说明：
 *   UnifiedDatingSystem 整合了原 DatingRLSystem（关系系统）、
 *   EngagementRLAgent（互动决策）、好奇心引擎（ICM/RND）、
 *   层级化Prompt体系，并以单一 RL 策略统摄游戏/非游戏模式。
 * ============================================================ */

import UnifiedDatingSystem from './unified-dating-system.ts';

// 兼容导出：RELATIONSHIP_LEVELS / ACTIONS 等常量由关系模型直接导出
export { RELATIONSHIP_LEVELS, ACTIONS, ACTION_COUNT } from './dating-relationship-model.ts';

// ==================== 兼容类 ====================

/**
 * DatingRLSystem — 薄封装，等价于 UnifiedDatingSystem
 *
 * 保留全部公开 API：
 *   - update(dt) / startSession() / endSession()
 *   - notifyUserMessage(text) / notifyUserInteraction()
 *   - triggerMilestone(name) / getDebugInfo()
 *   - flush() / load() / reset()
 *   - relationship / timePattern / eventMemory（子系统访问）
 */
export class DatingRLSystem extends UnifiedDatingSystem {}

export default DatingRLSystem;
