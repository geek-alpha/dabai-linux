/* ============================================================
 * AI 自主控制器 —— AI 独立行动能力的前端执行器
 *
 * 核心理念：
 * - AI 的移动完全复用现有的 idle walk 系统（pickWalkPath / updateWalkTimer / advanceWalkSegment）
 * - 本控制器只做路径计算和状态管理，不管理任何位置、动画
 * - 与人操控共享同一套位置插值、身体旋转、手臂/腿部动画、弹跳逻辑
 *
 * 职责：
 * - 接收后端行为命令
 * - 调用 A* 寻路计算路径
 * - 将路径注入到 App.walkPath / idleWalkTarget 等现有变量中
 * - 监控行走完成并通知后端
 * ============================================================ */

import { AStarPathfinder } from './pathfinding.ts';

export class AIAutonomyController {
  declare App: any;

  declare pathfinder: AStarPathfinder;

  // 状态
  declare _aiMoving: boolean;
  declare _aiDrivenWalk: boolean;
  declare _aiBehavior: string | null;
  declare _aiTargetLabel: string;

  // 存储最近一次寻路目标，用于卡住时重新寻路
  declare _lastPathTarget: any;       // { x, z, label }
  declare _lastPathBehavior: any;
  declare _repathAttempts: number;    // 重新寻路尝试次数
  declare MAX_REPATH_ATTEMPTS: number; // 最多重新寻路次数

  // 游戏模式兼容
  declare _userControlling: boolean;
  declare _userInputActiveTime: number;
  declare USER_IDLE_BEFORE_AI_MOVE: number;

  // 冷却（大幅度缩短，AI 到目标后可立即前往下一个）
  declare _lastAIMoveTime: number;
  declare AI_MOVE_COOLDOWN: number;        // 2秒微冷却（防止同帧重复触发）
  declare _lastMoveFinishTime: number;
  declare AI_MIN_IDLE_AFTER_MOVE: number;  // 到目标后稍停1秒就出发

  // 环境数据
  declare _mapData: any;
  declare _mapType: string | null;

  constructor(app) {
    this.App = app;

    this.pathfinder = new AStarPathfinder();

    // 状态
    this._aiMoving = false;
    this._aiBehavior = null;
    this._aiTargetLabel = '';

    // 存储最近一次寻路目标，用于卡住时重新寻路
    this._lastPathTarget = null;       // { x, z, label }
    this._lastPathBehavior = null;
    this._repathAttempts = 0;          // 重新寻路尝试次数
    this.MAX_REPATH_ATTEMPTS = 3;      // 最多重新寻路次数

    // 游戏模式兼容
    this._userControlling = false;
    this._userInputActiveTime = 0;
    this.USER_IDLE_BEFORE_AI_MOVE = 5000;

    // 冷却（大幅度缩短，AI 到目标后可立即前往下一个）
    this._lastAIMoveTime = 0;
    this.AI_MOVE_COOLDOWN = 2000;        // 2秒微冷却（防止同帧重复触发）
    this._lastMoveFinishTime = 0;
    this.AI_MIN_IDLE_AFTER_MOVE = 1000;  // 到目标后稍停1秒就出发

    // 环境数据
    this._mapData = null;
    this._mapType = null;
  }

  // ==================== 命令接收 ====================

  receiveCommand(cmd) {
    if (!cmd || !cmd.behavior) return;
    console.log('[AI自主] 收到命令:', cmd.behavior, cmd);

    if (cmd.speak_text && this.App.sendAIAction) {
      this.App.sendAIAction(cmd.speak_text);
    }

    switch (cmd.behavior) {
      case 'go_to_poi':
      case 'immediate_action':
      case 'explore_chain': this._handleGoToPOI(cmd);  break;
      case 'wander':
      case 'anxious_wander': this._handleWander(cmd);  break;
      case 'idle_action':   this._handleIdleAction(cmd); break;
      case 'suggest_poi':   this._handleSuggestPOI(cmd); break;
    }
  }

  // ==================== 行为处理 ====================

  _handleGoToPOI(cmd) {
    if (!this._canAIMove()) return;
    const target = cmd.target;
    if (!target) return;

    const avatar = this.App.modelGroup;
    if (!avatar) return;
    const path = this._calculatePath(avatar.position.x, avatar.position.z, target.x, target.z);
    if (!path || path.length < 2) return;

    // 存储目标用于可能的重新寻路
    this._lastPathTarget = { x: target.x, z: target.z, label: target.label || '目标点' };
    this._lastPathBehavior = 'go_to_poi';
    this._repathAttempts = 0;

    this._aiTargetLabel = target.label || '目标点';
    this._aiBehavior = 'go_to_poi';
    this._injectWalkPath(path);
    console.log(`[AI自主] 前往 "${this._aiTargetLabel}" (${path.length} 个路径点)`);
  }

  _handleWander(cmd) {
    if (!this._canAIMove()) return;

    const wander = cmd.wander || {};
    const avatar = this.App.modelGroup;
    if (!avatar) return;

    const startX = avatar.position.x;
    const startZ = avatar.position.z;

    // ---- 多路点踱步（焦虑漫步 / 无聊长时间乱走）----
    // 后端围绕感知位置(center)生成路点，这里平移到实际位置附近，
    // 踱步紧跟角色当前位置，每段由 idle walk 系统平滑插值，绝不瞬移。
    const rawWaypoints = Array.isArray(wander.waypoints) ? wander.waypoints : null;
    if (rawWaypoints && rawWaypoints.length > 0) {
      const center = wander.center || { x: startX, z: startZ };
      const offX = startX - (center.x || 0);
      const offZ = startZ - (center.z || 0);
      const pts = [];
      for (const p of rawWaypoints) {
        const wx = (p.x || 0) + offX;
        const wz = (p.z || 0) + offZ;
        // 过滤过近/重复路点，防止原地碎步
        const last = pts[pts.length - 1];
        if (last && Math.hypot(wx - last.x, wz - last.z) < 0.6) continue;
        if (Math.hypot(wx - startX, wz - startZ) < 0.6) continue;
        pts.push({ x: wx, z: wz });
      }
      if (pts.length === 0) return;
      const path = [{ x: startX, z: startZ }].concat(pts);
      this._aiBehavior = wander.pacing ? 'anxious_wander' : 'wander';
      this._injectWalkPath(path);
      console.log(`[AI自主] ${wander.pacing ? '焦虑踱步' : '漫步'}: ${pts.length} 个路点`);
      return;
    }

    // ---- 单步漫步（常规）----
    // 幅度与游戏内移动一致（最多 6 单位），避免"原地打转"式的碎步
    const dist = Math.min(6.0, wander.distance || 2.0);
    const angle = wander.angle || Math.random() * Math.PI * 2;

    const path = [
      { x: startX, z: startZ },
      { x: startX + Math.cos(angle) * dist, z: startZ + Math.sin(angle) * dist },
    ];

    this._aiBehavior = 'wander';
    this._injectWalkPath(path);
    console.log(`[AI自主] 漫步: 距离${dist.toFixed(1)}m`);
  }

  _handleIdleAction(cmd) {
    this._aiBehavior = 'idle_action';
    if (this.App.nextActionTimer !== undefined) {
      this.App.nextActionTimer = 0.3;
    }
  }

  _handleSuggestPOI(cmd) {
    this._aiBehavior = 'suggest_poi';
  }

  // ==================== 路径注入 ====================

  /**
   * 将 A* 路径注入到 App 的 idle walk 系统中。
   * 此后所有移动、动画完全由现有的 updateWalkTimer + 07_click_interact 处理。
   */
  _injectWalkPath(path) {
    const root = this.App.modelGroup;
    if (!root || !path || path.length < 2) return;

    // 标记为 AI 驱动（阻止 updateWalkTimer 自动生成新路径）
    this.App._aiDrivenWalk = true;
    this._aiMoving = true;
    this._lastAIMoveTime = performance.now();

    // 关键：走路必须接管全身骨骼——先停掉正在播的 Mixamo 动作，
    // 否则 animateModel 里 `if (App._mixamoActiveClip)` 直接 return，
    // 程序式走路（applyFullBodyWalkAnimation）被跳过 → 腿僵
    if (this.App._mixamoActiveClip && this.App.stopMixamoClip) {
      this.App.stopMixamoClip(0.2);
    }

    // 设置路径（skip 第一个点，它是当前位置），并过滤退化路点（过近 → 原地碎步/极短段）
    const rawWp = path.slice(1);
    const pts = [];
    let lastPt = null;
    for (const p of rawWp) {
      const ref = pts.length === 0 ? root.position : lastPt;
      if (ref && Math.hypot(p.x - ref.x, p.z - ref.z) < 0.5) continue;
      pts.push(p);
      lastPt = p;
    }
    if (pts.length === 0) return;
    this.App.walkPath = pts;
    this.App.walkSegmentIndex = 0;
    this.App.currentAction = { type: this.App.ActionType.WALK };

    // 第一段
    this.App.idleWalkStart = { x: root.position.x, z: root.position.z };
    this.App.idleWalkTarget = this.App.walkPath[0];
    this.App.idleWalkProgress = 0;
    // 朝向角约定与全局一致（模型前方向 +Z，朝向角 = atan2(dx, dz)，同游戏模式 _applyPlayerMovement）
    this.App.walkFacingAngle = Math.atan2(
      this.App.idleWalkTarget.x - this.App.idleWalkStart.x,
      this.App.idleWalkTarget.z - this.App.idleWalkStart.z
    );
    const segLen = Math.hypot(
      this.App.idleWalkTarget.x - this.App.idleWalkStart.x,
      this.App.idleWalkTarget.z - this.App.idleWalkStart.z
    );
    // 速度取值：AI 驱动行走用 AI_LOBBY_WALK_SPEED（自然步行 1.6，避免像小跑）
    const walkSpeed = this.App.AI_LOBBY_WALK_SPEED || 1.6;
    // 去掉 2.5 进度/秒上限：恒定线速度；
    // max(0.5, segLen) 仅兜底超短段，避免除以极小段长
    this.App.idleWalkSpeed = walkSpeed / Math.max(0.5, segLen);

    // 注册完成回调（使用闭包保持 this 正确）
    const self = this;
    this.App._onAIWalkComplete = () => self._onWalkComplete();

    this._notifyBackendAIMoving();
  }

  /** 系统回调：路径全部走完 */
  _onWalkComplete() {
    this._aiMoving = false;
    this.App._aiDrivenWalk = false;
    this._aiTargetLabel = '';
    this._aiBehavior = null;
    this._lastMoveFinishTime = performance.now();
    this._lastPathTarget = null;
    this._lastPathBehavior = null;
    this._repathAttempts = 0;

    // 长冷却
    this.App.nextActionTimer = 4.0 + Math.random() * 6.0;

    this._notifyBackendMoveComplete();
    console.log('[AI自主] 移动完成');
  }

  /**
   * 中途遇到障碍时，从当前位置重新寻路到原始目标。
   * 由 game-mode-manager 在检测到 AI 长时间卡住时调用。
   * @param {any} gameInstance - 当前游戏实例（用于查地形）
   * @returns {boolean} 是否成功重新规划路径
   */
  repathFromCurrent(gameInstance) {
    if (!this._aiMoving || !this._lastPathTarget) return false;
    if (this._repathAttempts >= this.MAX_REPATH_ATTEMPTS) {
      console.warn('[AI自主] 重新寻路已达上限，放弃本次移动');
      this._forceReset();
      return false;
    }

    const avatar = this.App.modelGroup;
    if (!avatar) return false;

    const target = this._lastPathTarget;
    console.log(`[AI自主] 卡住检测，第${this._repathAttempts + 1}次重新寻路 → (${target.x.toFixed(1)}, ${target.z.toFixed(1)})`);

    // 从当前位置重新计算路径
    const path = this._calculatePath(avatar.position.x, avatar.position.z, target.x, target.z);

    if (!path || path.length < 2) {
      this._repathAttempts++;
      // 尝试放宽一步：直接用直线路径
      const directPath = this.pathfinder.findPathDirect(avatar.position.x, avatar.position.z, target.x, target.z);
      if (directPath && this._checkDirectPathWalkable(directPath, gameInstance)) {
        console.log('[AI自主] 使用直线路径作为降级方案');
        this._injectWalkPath(directPath);
        this._repathAttempts = 0; // 重置，让直线路径有机会到达
        return true;
      }
      console.warn(`[AI自主] 重新寻路失败 (第${this._repathAttempts}次)，目标可能不可达`);
      if (this._repathAttempts >= this.MAX_REPATH_ATTEMPTS) {
        this._forceReset();
      }
      return false;
    }

    this._repathAttempts = 0; // 寻路成功，重置计数
    this._aiTargetLabel = target.label || '目标点';
    this._injectWalkPath(path);
    console.log(`[AI自主] 重新寻路成功，新路径 ${path.length} 个点`);
    return true;
  }

  /**
   * 检查直线路径是否可行（沿路径采样高度差）
   */
  _checkDirectPathWalkable(path, gameInstance) {
    if (!gameInstance || !gameInstance._isWalkableBetween) return false;
    if (path.length < 2) return false;

    // 沿路径采样检查
    const samples = 10;
    for (let i = 0; i <= samples; i++) {
      const t = i / samples;
      const idx = Math.floor(t * (path.length - 1));
      const nextIdx = Math.min(idx + 1, path.length - 1);
      const a = path[idx];
      const b = path[nextIdx];
      const frac = (t - idx / (path.length - 1)) * (path.length - 1);
      const sx = a.x + (b.x - a.x) * Math.max(0, Math.min(1, frac));
      const sz = a.z + (b.z - a.z) * Math.max(0, Math.min(1, frac));
      const nsx = a.x + (b.x - a.x) * Math.max(0, Math.min(1, frac + 0.05));
      const nsz = a.z + (b.z - a.z) * Math.max(0, Math.min(1, frac + 0.05));
      if (!gameInstance._isWalkableBetween(sx, sz, nsx, nsz)) return false;
    }
    return true;
  }

  // ==================== 每帧更新 ====================

  /**
   * 不做位置/动画管理——只保底：如果路径断了但 _aiMoving 仍为 true，强制复位。
   */
  update(dt) {
    if (!this._aiMoving) return;

    // 保底：如果 App 当前不在 WALK 动作，说明状态已乱，清理
    if (!this.App.currentAction || this.App.currentAction.type !== this.App.ActionType.WALK) {
      this._forceReset();
      return;
    }
  }

  _forceReset() {
    console.warn('[AI自主] 保底复位');
    this._aiMoving = false;
    this.App._aiDrivenWalk = false;
    this.App.currentAction = null;
    this.App.walkPath = [];
    this.App.walkSegmentIndex = 0;
    this.App.idleWalkTarget = null;
    this.App.idleWalkProgress = 0;
    this.App._onAIWalkComplete = null;
    this._lastPathTarget = null;
    this._lastPathBehavior = null;
    this._repathAttempts = 0;
    this._lastMoveFinishTime = performance.now();
    this._notifyBackendMoveComplete();
  }

  // ==================== 寻路 ====================

  _calculatePath(startX, startZ, endX, endZ) {
    if (this._mapType === 'grid' && this._mapData) {
      const cells = this._mapData.cells;
      const rows = this._mapData.rows || 13;
      const cols = this._mapData.cols || 13;
      const cellSize = this._mapData.cell_size || 2.5;

      // 网格坐标偏移：第 0 格的世界坐标中心位于 -(cols-1)/2 * cellSize
      // 例如 13 格迷宫，第 6 格中心在 (0,0)
      const gridOriginW = (cols - 1) * cellSize / 2;
      const gridOriginH = (rows - 1) * cellSize / 2;

      const col = Math.round((startX + gridOriginW) / cellSize);
      const row = Math.round((startZ + gridOriginH) / cellSize);
      const endCol = Math.round((endX + gridOriginW) / cellSize);
      const endRow = Math.round((endZ + gridOriginH) / cellSize);

      const raw = this.pathfinder.findPathOnGrid(
        cells, cols, rows, col, row, endCol, endRow, cellSize
      );
      if (raw) {
        // _reconstructPath 返回 col*cellSize+cellSize/2，减去 cols*cellSize/2 归中到世界原点
        const worldOriginW = cols * cellSize / 2;
        const worldOriginH = rows * cellSize / 2;
        return raw.map(p => ({ x: p.x - worldOriginW, z: p.z - worldOriginH }));
      }
      // 目标超出网格范围 → 降级直线路径
    }
    if (this._mapType === 'heightmap' && this._mapData) {
      const raw = this.pathfinder.findPathOpenWorld(
        this._mapData, this._mapData.chunk_size || 16,
        this._mapData.render_distance_chunks || 3,
        startX, startZ, endX, endZ
      );
      if (raw) {
        // 同样减偏移
        const totalCells = (this._mapData.render_distance_chunks || 3) * 2 * (this._mapData.chunk_size || 16);
        const half = totalCells / 2;
        return raw.map(p => ({ x: p.x - half, z: p.z - half }));
      }
      // 目标超出地图范围 → 降级为直线路径，朝目标移动并逐步加载区块
      console.log(`[AI自主] 目标(${endX.toFixed(0)},${endZ.toFixed(0)})超出寻路网格，使用直线路径`);
    }
    return this.pathfinder.findPathDirect(startX, startZ, endX, endZ);
  }

  // ==================== 网络通知 ====================

  _notifyBackendAIMoving() {
    if (this.App.ws && this.App.ws.readyState === WebSocket.OPEN) {
      this.App.ws.send(JSON.stringify({
        type: 'ai_moving', moving: true,
        behavior: this._aiBehavior, target_label: this._aiTargetLabel,
      }));
    }
  }

  _notifyBackendMoveComplete() {
    if (this.App.ws && this.App.ws.readyState === WebSocket.OPEN) {
      this.App.ws.send(JSON.stringify({
        type: 'ai_behavior_result', data: { event: 'movement_complete' },
      }));
      this.App.ws.send(JSON.stringify({
        type: 'ai_moving', moving: false,
      }));
    }
  }

  // ==================== 状态查询 ====================

  _canAIMove() {
    const now = performance.now();
    if (now - this._lastAIMoveTime < this.AI_MOVE_COOLDOWN) return false;
    if (now - this._lastMoveFinishTime < this.AI_MIN_IDLE_AFTER_MOVE) return false;
    if (this._userControlling && now - this._userInputActiveTime < this.USER_IDLE_BEFORE_AI_MOVE) return false;
    return true;
  }

  // ==================== 公共接口 ====================

  setMapData(mapData, mapType) { this._mapData = mapData; this._mapType = mapType; }
  notifyUserControlling() { this._userControlling = true; this._userInputActiveTime = performance.now(); }
  notifyUserStopped() { this._userInputActiveTime = 0; }
  setUserControlling(active) { this._userControlling = active; if (active) this._userInputActiveTime = performance.now(); }

  /**
   * 游戏模式专用：获取 AI 驱动的移动向量（接入 _applyPlayerMovement 管道）。
   * 返回 null 表示 AI 没有在驱动移动或路径已走完。
   */
  getAIDrivenMoveVec(dt) {
    if (!this._aiMoving || !this._aiDrivenWalk) return null;
    const App = this.App;
    if (!App.idleWalkTarget || App.idleWalkProgress >= 1) return null;

    // smoothstep 插值，与 07_click_interact.js 一致
    const t = App.idleWalkProgress;
    const s = t * t * (3 - 2 * t);
    const tx = App.idleWalkStart.x + (App.idleWalkTarget.x - App.idleWalkStart.x) * s;
    const tz = App.idleWalkStart.z + (App.idleWalkTarget.z - App.idleWalkStart.z) * s;

    const avatar = App.modelGroup;
    if (!avatar) return null;
    const dx = tx - avatar.position.x;
    const dz = tz - avatar.position.z;
    const moving = Math.abs(dx) > 0.0001 || Math.abs(dz) > 0.0001;

    return { x: dx, z: dz, isMoving: moving, targetX: tx, targetZ: tz };
  }

  stopAIMovement() {
    if (this._aiMoving) {
      this._forceReset();
      console.log('[AI自主] 用户打断，AI 移动已停止');
    }
  }

  cleanup() {
    this._forceReset();
    this._mapData = null;
    this._mapType = null;
    this._userControlling = false;
  }
}

export default AIAutonomyController;
