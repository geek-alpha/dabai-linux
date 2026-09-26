/* ============================================================
 * 体素地形生成器 —— Minecraft 式方块高度场
 * ------------------------------------------------------------
 * 高度量化成整数层、每层一个立方体，所以地形天然带垂直面和台阶，
 * 而不是 heightfield 那种「格与格之间是斜坡」的光滑面。角色踩上去
 * 会绊、会卡、要抬腿 —— 这才是 PD 应付不了、策略能学出东西的地方。
 *
 * 高度来源是值噪声 fBm（多倍频），不是正弦叠加：正弦叠加会出现周期
 * 性山脊，策略能记住相位；值噪声每局都是新地形，学不出「地形表」。
 *
 * 出生格强制 0 层、中心 3×3 限制在 ±1 层：角色总是从能站的地方开始，
 * 站不起来是策略的问题，不是被地形埋了。
 * ============================================================ */

/** 格边长（米）—— 约等于脚长，再小就变成噪声而不是地形 */
export const CELL = 0.4;
/** 网格边长（格数）—— 11×11 = 4.4m 见方，够角色被推 2m 还站得住 */
export const N = 11;
/** 层高（米）：8cm 是「能踩上去的小台阶」，5 层 = 40cm 要抬腿跨 */
export const LAYER = 0.08;
/** 所有方块底面统一到这个深度：相邻块之间不会在侧面漏出缝隙把角色卡住 */
const DEEP = 0.9;
/** 最大层数绝对值（±5 层 = ±40cm） */
const MAX_LAYER = 5;

export const KIND_FLAT = 0;
export const KIND_SLOPE = 1;
export const KIND_VOXEL = 2;
export const KIND_NAMES = ['flat', 'slope', 'voxel'];

function clamp(v, lo, hi) { return v < lo ? lo : v > hi ? hi : v; }
function smoothstep(t) { return t * t * (3 - 2 * t); }

/** 32 位整数哈希 → [0,1)：Math.imul 保证不溢出成浮点丢精度 */
function hash2(ix, iz, seed) {
  let h = Math.imul(ix | 0, 374761393) ^ Math.imul(iz | 0, 668265263) ^ Math.imul(seed | 0, 1442695041);
  h = Math.imul(h ^ (h >>> 13), 1274126177);
  h ^= h >>> 16;
  return (h >>> 0) / 4294967296;
}

/** 值噪声：格点随机值 + 双线性插值，比 Perlin 少一半开销，做地形足够 */
function valueNoise(x, z, seed) {
  const ix = Math.floor(x), iz = Math.floor(z);
  const fx = smoothstep(x - ix), fz = smoothstep(z - iz);
  const a = hash2(ix, iz, seed);
  const b = hash2(ix + 1, iz, seed);
  const c = hash2(ix, iz + 1, seed);
  const d = hash2(ix + 1, iz + 1, seed);
  return (a + (b - a) * fx) * (1 - fz) + (c + (d - c) * fx) * fz;
}

/** 多倍频叠加：低频给大起伏，高频给小颗粒 */
function fbm(x, z, seed, octaves) {
  let sum = 0, amp = 1, freq = 1, norm = 0;
  for (let o = 0; o < octaves; o++) {
    sum += valueNoise(x * freq, z * freq, seed + o * 7919) * amp;
    norm += amp;
    amp *= 0.5;
    freq *= 2;
  }
  return sum / norm;
}

/**
 * 贪心矩形合并：等高相邻格拼成一块。
 * 121 个格子直接建 121 个体，物理步进和每局重建的开销都不必要；
 * 合并后通常只剩 30~50 块，形状完全一样。
 */
function greedyMerge(levels) {
  const used = new Uint8Array(N * N);
  const rects = [];
  for (let iz = 0; iz < N; iz++) {
    for (let ix = 0; ix < N; ix++) {
      const i = iz * N + ix;
      if (used[i]) continue;
      const lvl = levels[i];
      let w = 1;
      while (ix + w < N && !used[iz * N + ix + w] && levels[iz * N + ix + w] === lvl) w++;
      let h = 1;
      grow: while (iz + h < N) {
        for (let k = 0; k < w; k++) {
          const j = (iz + h) * N + ix + k;
          if (used[j] || levels[j] !== lvl) break grow;
        }
        h++;
      }
      for (let dz = 0; dz < h; dz++) {
        for (let dx = 0; dx < w; dx++) used[(iz + dz) * N + ix + dx] = 1;
      }
      rects.push({ ix, iz, w, h, lvl });
    }
  }
  return rects;
}

export class VoxelTerrain {
  /**
   * @param rng 环境提供的确定性随机（同一 seed 必须复现同一地形）
   * @param kind 地形类型（KIND_*），null = 随机
   * @param difficulty 0~1：起伏幅度与特征强度，用于课程学习
   * @param groundY 基准地面高度（脚底）
   */
  constructor(rng, kind = null, difficulty = 1, groundY = 0) {
    this.groundY = groundY;
    this.levels = new Int8Array(N * N);
    // difficulty 必须先赋值：_rollKind 依赖它决定地形类型分布
    this.difficulty = difficulty;
    this.kind = kind === null ? this._rollKind(rng) : kind;
    this.bodies = [];
    this.blockCount = 0;
    this._generate(rng);
  }

  /** 地形类型抽取。难度低时几乎全是平地 —— 课程学习的第一步 */
  _rollKind(rng) {
    const r = rng();
    const d = this.difficulty;
    const flatP = 1 - d * 0.7;
    const slopeP = 0.2 * d;
    if (r < flatP) return KIND_FLAT;
    if (r < flatP + slopeP) return KIND_SLOPE;
    return KIND_VOXEL;
  }

  get name() { return KIND_NAMES[this.kind]; }

  _generate(rng) {
    const c = (N - 1) >> 1;
    const d = this.difficulty;
    const seed = Math.floor(rng() * 0x7fffffff);
    const L = this.levels;

    if (this.kind === KIND_FLAT) {
      L.fill(0);
      return;
    }

    if (this.kind === KIND_SLOPE) {
      // 斜坡也量化成层：Minecraft 里的斜坡本来就是楼梯，脚掌有明确的落脚面
      const ang = rng() * Math.PI * 2;
      const dx = Math.cos(ang), dz = Math.sin(ang);
      const grade = (0.25 + rng() * 0.9) * d;
      for (let iz = 0; iz < N; iz++) {
        for (let ix = 0; ix < N; ix++) {
          const px = (ix - c) * CELL, pz = (iz - c) * CELL;
          L[iz * N + ix] = clamp(Math.round((px * dx + pz * dz) * grade / LAYER), -MAX_LAYER, MAX_LAYER);
        }
      }
      L[c * N + c] = 0;
      return;
    }

    /* --- 体素地形：噪声打底，再叠特征 --- */
    const span = 3.5 + d * 4.5;
    for (let iz = 0; iz < N; iz++) {
      for (let ix = 0; ix < N; ix++) {
        const n = fbm(ix * 0.42, iz * 0.42, seed, 3);
        let lvl = Math.round((n - 0.45) * span * 2);
        // 出生格必须与基准齐平，但紧邻一圈就放开：只把出生点做平的话，角色站在
        // 平地上不动，地形难度全在走不到的地方 —— PD 照样能站住，策略没活干。
        const ring = Math.max(Math.abs(ix - c), Math.abs(iz - c));
        if (ring === 0) lvl = 0;
        else if (ring === 1) lvl = clamp(lvl, -2, 2);
        else if (ring === 2) lvl = clamp(lvl, -4, 4);
        L[iz * N + ix] = clamp(lvl, -MAX_LAYER, MAX_LAYER);
      }
    }

    // 特征 1：台阶带 —— 沿随机方向切成 2~4 级，每级 1~3 层。整片平移而不是
    // 局部加减，所以台阶是连续的「梯田」，不是散落的坑包。
    if (rng() < 0.55) {
      const ang = rng() * Math.PI * 2;
      const dx = Math.cos(ang), dz = Math.sin(ang);
      const stepW = 1.2 + rng() * 1.6;
      const stepH = 1 + Math.floor(rng() * 2.5 * d);
      for (let iz = 0; iz < N; iz++) {
        for (let ix = 0; ix < N; ix++) {
          const i = iz * N + ix;
          const ring = Math.max(Math.abs(ix - c), Math.abs(iz - c));
          if (ring === 0) continue;
          const px = (ix - c) * CELL, pz = (iz - c) * CELL;
          const proj = px * dx + pz * dz;
          L[i] = clamp(L[i] + Math.floor(proj / stepW) * stepH, -MAX_LAYER, MAX_LAYER);
        }
      }
    }

    // 特征 2：深坑 —— 半径 1~2 格、下挖 3~5 层。坑壁是垂直的，掉进去
    // 只能靠抬腿撑出来，纯 PD 在这里会一直贴着壁抽搐。
    if (rng() < 0.5) {
      const r = 1 + Math.floor(rng() * 2);
      const px = c + Math.round((rng() - 0.5) * 4);
      const pz = c + Math.round((rng() - 0.5) * 4);
      const depth = 3 + Math.floor(rng() * 2.5 * d);
      for (let iz = 0; iz < N; iz++) {
        for (let ix = 0; ix < N; ix++) {
          const ring = Math.max(Math.abs(ix - c), Math.abs(iz - c));
          if (ring === 0) continue;
          if (Math.abs(ix - px) <= r && Math.abs(iz - pz) <= r) {
            const i = iz * N + ix;
            L[i] = clamp(L[i] - depth, -MAX_LAYER, MAX_LAYER);
          }
        }
      }
    }

    // 特征 3：高台 —— 半径 1 格的柱子，抬升 2~4 层。跨上去要抬腿，
    // 也是「被推时能不能踩住」的支点。
    if (rng() < 0.45) {
      const px = c + Math.round((rng() - 0.5) * 5);
      const pz = c + Math.round((rng() - 0.5) * 5);
      const rise = 2 + Math.floor(rng() * 2.5 * d);
      for (let iz = 0; iz < N; iz++) {
        for (let ix = 0; ix < N; ix++) {
          const ring = Math.max(Math.abs(ix - c), Math.abs(iz - c));
          if (ring === 0) continue;
          if (Math.abs(ix - px) <= 1 && Math.abs(iz - pz) <= 1) {
            const i = iz * N + ix;
            L[i] = clamp(L[i] + rise, -MAX_LAYER, MAX_LAYER);
          }
        }
      }
    }

    // 出生格必须与基准齐平：地形生成的所有特征都可能改到它，最后强行压回
    L[c * N + c] = 0;
  }

  /** 某格顶面的绝对高度 */
  _levelTop(lvl) { return this.groundY + lvl * LAYER; }

  /** 点 (x,z) 脚下的地面高度。地形范围外是外圈基准地面，不是边界格 —— 否则
   *  站在边缘的角色会“感知”到一个不存在的台阶 */
  heightAt(x, z) {
    const half = (N * CELL) / 2;
    if (x < -half || x > half || z < -half || z > half) return this.groundY;
    const c = (N - 1) >> 1;
    let ix = Math.round(x / CELL) + c;
    let iz = Math.round(z / CELL) + c;
    ix = clamp(ix, 0, N - 1);
    iz = clamp(iz, 0, N - 1);
    return this._levelTop(this.levels[iz * N + ix]);
  }

  /** 建碰撞体：方块底面统一在 -DEEP，所以相邻格侧面永远有实体，不会漏缝 */
  build(RAPIER, world, friction) {
    this.dispose(world);
    const c = (N - 1) >> 1;
    const rects = greedyMerge(this.levels);
    for (const r of rects) {
      const top = r.lvl * LAYER;
      const height = top + DEEP;
      if (height <= 0.02) continue;
      const cx = (r.ix + (r.w - 1) / 2 - c) * CELL;
      const cz = (r.iz + (r.h - 1) / 2 - c) * CELL;
      const cy = this.groundY + top - height / 2;
      const body = world.createRigidBody(RAPIER.RigidBodyDesc.fixed().setTranslation(cx, cy, cz));
      world.createCollider(
        RAPIER.ColliderDesc.cuboid(r.w * CELL / 2, height / 2, r.h * CELL / 2).setFriction(friction),
        body
      );
      this.bodies.push(body);
    }

    // 外圈基准地面：地形外沿必须是「平的地面」，不能是虚空 —— 被推出去 2m
    // 就掉进 90cm 深坑爬不出来的话，评估出来的差异是地形边界造成的，不是控制能力。
    const half = (N * CELL) / 2;
    const OUT = 10;
    const ring = [
      [0, -(OUT + half) / 2, 2 * OUT, OUT - half],
      [0, (OUT + half) / 2, 2 * OUT, OUT - half],
      [-(OUT + half) / 2, 0, OUT - half, 2 * half],
      [(OUT + half) / 2, 0, OUT - half, 2 * half],
    ];
    for (const [px, pz, sx, sz] of ring) {
      const body = world.createRigidBody(
        RAPIER.RigidBodyDesc.fixed().setTranslation(px, this.groundY - 0.5, pz)
      );
      world.createCollider(RAPIER.ColliderDesc.cuboid(sx / 2, 0.5, sz / 2).setFriction(friction), body);
      this.bodies.push(body);
    }

    // 极低兜底：真掉出去了也别让物理无限下落（ActiveRagdoll 在 groundY-3 处判失败）
    const floor = world.createRigidBody(
      RAPIER.RigidBodyDesc.fixed().setTranslation(0, this.groundY - 3.5, 0)
    );
    world.createCollider(RAPIER.ColliderDesc.cuboid(20, 0.5, 20).setFriction(friction), floor);
    this.bodies.push(floor);
    this.blockCount = rects.length;
  }

  dispose(world) {
    for (const b of this.bodies) world.removeRigidBody(b);
    this.bodies.length = 0;
  }
}
