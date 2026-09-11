/* ============================================================
 * A* 寻路系统 —— AI 自主导航的核心
 *
 * 特性：
 * - 经典的 A* (A-star) 寻路算法
 * - 支持网格地图（迷宫）和开放世界（体素高度图）
 * - 4方向 / 8方向移动可选
 * - 对角线移动惩罚（避免穿墙角）
 * - 路径平滑化（减少锯齿，生成更自然的路径）
 * - 返回路径点列表供动画系统使用
 * ============================================================ */

export class PathNode {
  declare x: number;
  declare z: number;
  declare g: number;
  declare h: number;
  declare f: number;
  declare parent: PathNode | null;
  declare closed: boolean;
  declare opened: boolean;

  constructor(x, z, g = 0, h = 0, parent = null) {
    this.x = x;           // 网格坐标 X (col)
    this.z = z;           // 网格坐标 Z (row)
    this.g = g;           // 起点到此的实际代价
    this.h = h;           // 此到终点的启发式估算
    this.f = g + h;       // f = g + h
    this.parent = parent; // 父节点
    this.closed = false;  // 是否已关闭
    this.opened = false;  // 是否已开启
  }

  get key() {
    return `${this.x},${this.z}`;
  }
}

export class AStarPathfinder {
  declare allowDiagonal: boolean;
  declare diagonalPenalty: number;
  declare straightCost: number;
  declare MAX_ITERATIONS: number;
  declare SMOOTH_PASSES: number;

  constructor() {
    // 配置
    this.allowDiagonal = true;       // 是否允许对角线移动
    this.diagonalPenalty = 1.414;   // 对角线代价（√2）
    this.straightCost = 1.0;        // 直走代价
    this.MAX_ITERATIONS = 5000;     // 防止无限循环
    this.SMOOTH_PASSES = 2;         // 路径平滑迭代次数
  }

  /**
   * 在网格地图上寻路
   *
   * @param {number[][]} grid - 二维数组，0=可通行, 1=阻挡
   * @param {number} cols - 列数
   * @param {number} rows - 行数
   * @param {number} startX - 起点 col
   * @param {number} startZ - 起点 row
   * @param {number} endX - 终点 col
   * @param {number} endZ - 终点 row
   * @param {number} cellSize - 每格大小 (世界单位)
   * @param {Function} isWalkableFn - 可选，(fromCol, fromRow, toCol, toRow) => boolean，检查两格之间是否可通行
   * @returns {{x:number, z:number}[] | null} 路径点列表（世界坐标）或 null
   */
  findPathOnGrid(grid, cols, rows, startX, startZ, endX, endZ, cellSize = 1.0, isWalkableFn = null) {
    // 边界检查
    if (startX < 0 || startX >= cols || startZ < 0 || startZ >= rows) return null;
    if (endX < 0 || endX >= cols || endZ < 0 || endZ >= rows) return null;
    if (grid[endZ] && grid[endZ][endX] === 1) return null; // 终点不可达

    const openList = [];
    const nodeMap = new Map();

    const startNode = new PathNode(startX, startZ, 0, this._heuristic(startX, startZ, endX, endZ));
    openList.push(startNode);
    nodeMap.set(startNode.key, startNode);

    let iterations = 0;

    while (openList.length > 0 && iterations < this.MAX_ITERATIONS) {
      iterations++;

      // 找 f 最小的节点
      let bestIdx = 0;
      for (let i = 1; i < openList.length; i++) {
        if (openList[i].f < openList[bestIdx].f) bestIdx = i;
      }

      const current = openList.splice(bestIdx, 1)[0];
      current.closed = true;

      // 到达终点
      if (current.x === endX && current.z === endZ) {
        return this._reconstructPath(current, cellSize);
      }

      // 扩展邻居
      const neighbors = this._getNeighbors(current, grid, cols, rows, isWalkableFn);
      for (const [nx, nz, cost] of neighbors) {
        const nKey = `${nx},${nz}`;
        if (nodeMap.has(nKey) && nodeMap.get(nKey).closed) continue;

        const g = current.g + cost;
        const h = this._heuristic(nx, nz, endX, endZ);

        let neighbor = nodeMap.get(nKey);
        if (neighbor) {
          if (g < neighbor.g) {
            // 找到更好的路径
            neighbor.g = g;
            neighbor.f = g + neighbor.h;
            neighbor.parent = current;
          }
        } else {
          neighbor = new PathNode(nx, nz, g, h, current);
          neighbor.opened = true;
          nodeMap.set(nKey, neighbor);
          openList.push(neighbor);
        }
      }
    }

    // 没有找到路径
    if (iterations >= this.MAX_ITERATIONS) {
      console.warn('[Pathfinder] 达到最大迭代次数，无路径');
    }
    return null;
  }

  /**
   * 在开放世界寻路（基于体素高度图，始终在相近高度平面移动）
   *
   * 核心原则：
   * - 以起点高度为基准平面，只允许 ±MAX_STEP 高度范围内的格子
   * - 相邻格高度差超过 MAX_STEP 视为悬崖，不可跨越
   * - 水体、虚空不可通行
   *
   * @param {any} heightMap - { cells: [[{height: number, type: string}]] }
   * @param {number} chunkSize - 区块大小
   * @param {number} renderDistance - 渲染距离
   * @param {number} startWorldX - 起点世界 X
   * @param {number} startWorldZ - 起点世界 Z
   * @param {number} endWorldX - 终点世界 X
   * @param {number} endWorldZ - 终点世界 Z
   * @returns {{x:number, z:number}[] | null}
   */
  findPathOpenWorld(heightMap, chunkSize, renderDistance, startWorldX, startWorldZ, endWorldX, endWorldZ) {
    const MAX_STEP = 2;            // 最大高度差（格），超过视为悬崖不可跨越
    const MAX_PLANE_DRIFT = 3;     // 允许偏离起点平面的最大高度差

    const totalCells = renderDistance * 2 * chunkSize;
    const halfSize = totalCells / 2;
    const cellSize = 1.0;

    const gridCols = totalCells;
    const gridRows = totalCells;

    // 提取每个 cell 的高度
    const heights = [];
    for (let r = 0; r < gridRows; r++) heights.push(new Array(gridCols).fill(0));

    if (heightMap && heightMap.cells) {
      for (let r = 0; r < Math.min(gridRows, heightMap.cells.length); r++) {
        const cellRow = heightMap.cells[r];
        if (!cellRow) continue;
        for (let c = 0; c < Math.min(gridCols, cellRow.length); c++) {
          heights[r][c] = cellRow[c] ? (cellRow[c].height || 0) : 0;
        }
      }
    }

    // 世界坐标 → 网格坐标
    const worldToGrid = (wx, wz) => ({
      col: Math.round((wx + halfSize) / cellSize),
      row: Math.round((wz + halfSize) / cellSize),
    });

    const startGrid = worldToGrid(startWorldX, startWorldZ);
    const endGrid = worldToGrid(endWorldX, endWorldZ);

    // 起点高度（作为基准平面）
    const startHeight = (startGrid.row >= 0 && startGrid.row < gridRows &&
                         startGrid.col >= 0 && startGrid.col < gridCols)
      ? heights[startGrid.row][startGrid.col] : 0;

    // 构建可通行网格：基于高度平面的通行判断
    const grid = [];
    for (let r = 0; r < gridRows; r++) {
      const row = [];
      for (let c = 0; c < gridCols; c++) {
        let blocked = false;

        if (heightMap && heightMap.cells) {
          const cellRow = heightMap.cells[r];
          if (cellRow && cellRow[c]) {
            const cell = cellRow[c];
            // 水体 / 虚空 / 极高地形 → 不可通行
            if (cell.type === 'water' || cell.type === 'void' || cell.height > 50) {
              blocked = true;
            }
          }
        }

        // 偏离起点平面太远 → 不可通行（防止爬上高山或跌入深谷）
        if (!blocked) {
          const h = heights[r][c];
          if (Math.abs(h - startHeight) > MAX_PLANE_DRIFT) {
            blocked = true;
          }
        }

        row.push(blocked ? 1 : 0);
      }
      grid.push(row);
    }

    // 高度差可通行性检查函数：相邻两格高度不能差太多
    const isWalkableFn = (fromCol, fromRow, toCol, toRow) => {
      if (toRow < 0 || toRow >= gridRows || toCol < 0 || toCol >= gridCols) return false;
      const fromH = heights[fromRow][fromCol];
      const toH = heights[toRow][toCol];
      return Math.abs(fromH - toH) <= MAX_STEP;
    };

    return this.findPathOnGrid(
      grid, gridCols, gridRows,
      startGrid.col, startGrid.row,
      endGrid.col, endGrid.row,
      cellSize, isWalkableFn
    );
  }

  /**
   * 简化寻路：直线路径（无地图时使用）
   * @returns {{x:number, z:number}[]} - [起点, 终点]
   */
  findPathDirect(startX, startZ, endX, endZ) {
    return [
      { x: startX, z: startZ },
      { x: endX, z: endZ },
    ];
  }

  /**
   * 平滑路径：减少锯齿，生成更自然的行走轨迹
   * @param {{x:number, z:number}[]} path - 原始路径
   * @param {Function} isWalkable - (x, z) => boolean 可通行检测函数
   * @returns {{x:number, z:number}[]} 平滑后的路径
   */
  smoothPath(path, isWalkable = null) {
    if (!path || path.length <= 2) return path;

    let smoothed = [...path];

    for (let pass = 0; pass < this.SMOOTH_PASSES; pass++) {
      const result = [smoothed[0]];
      let i = 0;

      while (i < smoothed.length - 2) {
        const a = smoothed[i];
        const c = smoothed[i + 2];

        // 检查是否可以直接从 a 到 c（跳过中间点 b）
        if (!isWalkable || this._lineWalkable(a, c, isWalkable)) {
          result.push(c);
          i += 2;
        } else {
          result.push(smoothed[i + 1]);
          i += 1;
        }
      }

      // 确保终点在
      if (result[result.length - 1] !== smoothed[smoothed.length - 1]) {
        result.push(smoothed[smoothed.length - 1]);
      }

      smoothed = result;
    }

    return smoothed;
  }

  // ==================== 内部方法 ====================

  _heuristic(x1, z1, x2, z2) {
    // 曼哈顿距离（如果允许对角线）或 欧几里得距离
    if (this.allowDiagonal) {
      const dx = Math.abs(x1 - x2);
      const dz = Math.abs(z1 - z2);
      return this.straightCost * Math.max(dx, dz) + (this.diagonalPenalty - this.straightCost) * Math.min(dx, dz);
    }
    return this.straightCost * (Math.abs(x1 - x2) + Math.abs(z1 - z2));
  }

  _getNeighbors(node, grid, cols, rows, isWalkableFn = null) {
    const neighbors = [];
    const { x, z } = node;

    // 4方向
    const dirs4 = [[0, -1], [1, 0], [0, 1], [-1, 0]];

    for (const [dx, dz] of dirs4) {
      const nx = x + dx;
      const nz = z + dz;
      if (nx >= 0 && nx < cols && nz >= 0 && nz < rows && grid[nz][nx] === 0) {
        // 额外可通行性检查（如高度差）
        if (isWalkableFn && !isWalkableFn(x, z, nx, nz)) continue;
        neighbors.push([nx, nz, this.straightCost]);
      }
    }

    // 8方向对角线（带穿墙检测）
    if (this.allowDiagonal) {
      const diags = [
        [1, -1, 0, -1, 1, 0],   // 右上: 检查上和右不阻挡
        [1, 1, 0, 1, 1, 0],     // 右下
        [-1, 1, -1, 0, 0, 1],   // 左下
        [-1, -1, -1, 0, 0, -1], // 左上
      ];

      for (const [dx, dz, c1x, c1z, c2x, c2z] of diags) {
        const nx = x + dx;
        const nz = z + dz;
        if (nx >= 0 && nx < cols && nz >= 0 && nz < rows && grid[nz][nx] === 0) {
          // 检查不会穿墙角
          const c1pass = (x + c1x < 0 || x + c1x >= cols || z + c1z < 0 || z + c1z >= rows) ||
                         grid[z + c1z][x + c1x] === 0;
          const c2pass = (x + c2x < 0 || x + c2x >= cols || z + c2z < 0 || z + c2z >= rows) ||
                         grid[z + c2z][x + c2x] === 0;
          if (c1pass && c2pass) {
            // 额外可通行性检查，对角线需两端都可通过
            if (isWalkableFn) {
              if (!isWalkableFn(x, z, nx, nz)) continue;
              // 对角线的两个相邻格也要检查
              if (!isWalkableFn(x, z, x + c1x, z + c1z)) continue;
              if (!isWalkableFn(x, z, x + c2x, z + c2z)) continue;
            }
            neighbors.push([nx, nz, this.diagonalPenalty]);
          }
        }
      }
    }

    return neighbors;
  }

  _reconstructPath(node, cellSize) {
    const path = [];
    let current = node;
    while (current) {
      path.unshift({
        x: current.x * cellSize + cellSize / 2,
        z: current.z * cellSize + cellSize / 2,
      });
      current = current.parent;
    }
    return path;
  }

  _lineWalkable(a, b, isWalkable) {
    // 简单线性插值检查
    const steps = Math.max(Math.abs(b.x - a.x), Math.abs(b.z - a.z)) * 4;
    for (let i = 1; i < steps; i++) {
      const t = i / steps;
      const x = a.x + (b.x - a.x) * t;
      const z = a.z + (b.z - a.z) * t;
      if (!isWalkable(x, z)) return false;
    }
    return true;
  }
}

export default AStarPathfinder;