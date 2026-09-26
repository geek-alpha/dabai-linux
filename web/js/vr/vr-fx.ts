/* ============================================================
 * web/js/vr/vr-fx.ts —— VR 沉浸态特效层（欢呼 / 彩带 / 评级大字 / 频闪 / 震动）
 * ------------------------------------------------------------
 * WebXR 沉浸会话里浏览器不渲染页面 DOM：#holo-fx / #light-fx / #holo-cheer /
 * #holo-confetti / .lf-strobe 戴上头显就全部消失 —— 工具成功、暴击、升级在
 * VR 里变成「什么都没发生」。本模块把 41_holo_stage 的那套反馈在世界里重建，
 * 参数逐项对齐 style.css：
 *   彩带      34 片 / 7×12px / 5 色 / 落 104vh 用 1.6~3.0s / ±220px 横漂 / ±360° 转
 *   评级大字  en 52px（lv5 66px）+ cn 15px，1.1s 弹性入场；lv3 黄 lv4 橙 lv5 粉
 *   频闪      最多 3 次 / 460ms / 仅 4 级以上（光敏安全）
 *   震动      s|m|l = 2|4|6px 位移，0.34|0.46|0.62s
 *   底座      pulse 520ms / boom 1000ms，每一级都有 —— 投影仪在回应你
 *   鼓励      700ms 呼吸，不刺眼
 * 音效不重建：WebAudio 不依赖 DOM，41 的欢呼音在头显里本来就听得到。
 *
 * 与非 VR 的差别只在空间形态：彩带落在角色与你之间的真实空间、评级大字悬在
 * 视线前方 2.4m、震动换成「世界微抖 + 手柄触觉」（头显里晃相机是晕眩源，
 * 触觉才是 VR 里等价的那一下）。所有几何体 raycast 置空，不截胡手柄射线。
 * ============================================================ */
import * as THREE from 'three';
import type { AppKernel, VrFx } from '../types/app-kernel.js';

const FONT = '"Microsoft YaHei", "PingFang SC", "Noto Sans SC", sans-serif';

/* VR 特效层总开关：用户要求头显里不要任何特效（彩带 / 评级大字 / 频闪 /
 * 世界微抖），进 VR 只留功能面板（巨幕 / 对话屏 / 遥控条 / 工具栏）。
 * 关掉后 fx.active 恒为 false，每帧 update 首行即返回，零开销；要恢复改成 true。 */
const FX_ON_IN_VR = false;

/** 评级文案（与 41 的 CHEER_TIERS 同源；分级规则仍走 App.holo.level，不重造） */
const TIERS: { en: string; cn: string }[] = [
  { en: '', cn: '' },
  { en: 'NICE', cn: '稳' },
  { en: 'GOOD', cn: '漂亮' },
  { en: 'GREAT', cn: '干得漂亮' },
  { en: 'AMAZING', cn: '不可思议' },
  { en: 'UNBELIEVABLE', cn: '全场欢呼' }
];

/** 级数配色（.hc-tier.lv3/4/5 的 --hc-c） */
const TIER_RGB = ['', '', '', '255,213,79', '255,145,0', '255,82,168'];

/* 彩带：34 片、5 色、7×12px、3400ms 后回收（#holo-confetti） */
const CONFETTI_N = 34;
const CONFETTI_COLORS = ['#ffd54f', '#4ade80', '#00e5ff', '#ff6b9d', '#c084fc'];
const CONFETTI_W = 0.07;     // 7px（按「1000px ≈ 1m 观感」折算）
const CONFETTI_H = 0.12;     // 12px
const CONFETTI_FALL = 2.2;   // 104vh ≈ 从头顶落到脚下
const CONFETTI_SPREAD = 1.4; // 「满屏」在 VR 里的等价：半径 1.4m 的圆盘

/* 震动：s|m|l = 2|4|6px 位移，0.34|0.46|0.62s（#stage.holo-shake-*）
 * 每档是 [t, x, y] 关键帧，单位 px；1px ≈ 0.002m（1000px ≈ 2m 视野宽）。 */
const SHAKE_KEYS: number[][][] = [
  [[0, 0, 0], [0.25, -2, 1], [0.75, 2, -1], [1, 0, 0]],
  [[0, 0, 0], [0.2, -4, 2], [0.5, 4, -2], [0.8, -3, 1], [1, 0, 0]],
  [[0, 0, 0], [0.15, -6, 3], [0.4, 6, -3], [0.65, -5, 2], [0.88, 4, -2], [1, 0, 0]]
];
const SHAKE_DUR = [0.34, 0.46, 0.62];
const PX = 0.002;

/* 评级大字（.hc-tier）：画布 2 倍密度，1.1s 弹性入场 */
const TIER_CV = { w: 1024, h: 460 };
const TIER_WORLD_W = 1.7;
const TIER_DIST = 2.4;  // 悬在视线前方（米）
const TIER_UP = 0.45;   // CSS top:17% ≈ 眼平线上方
const TIER_ANIM = 1.1;  // hc-tier-in 1.1s
const TIER_LIFE = [0, 1100, 1100, 1100, 1500, 1800]; // showTier 的 life（ms）

/* 频闪：460ms 内 3 次，仅 4 级以上；强度压低（VR 里全屏白闪是晕眩源） */
const STROBE_TTL = 0.46;
const STROBE_TIMES = 3;
const STROBE_PEAK = 0.22;

/* 欢呼「整层亮一下」：ttl = 220 + lv*160（41 的 data-cheer） */
const glowTTL = (lv: number) => (220 + lv * 160) / 1000;

const DIM_TTL = 0.7; // hf-dim 700ms

type Panel = {
  cv: HTMLCanvasElement;
  ctx: CanvasRenderingContext2D;
  tex: THREE.CanvasTexture;
  mat: THREE.MeshBasicMaterial;
  mesh: THREE.Mesh;
};

type Piece = {
  mesh: THREE.Mesh;
  mat: THREE.MeshBasicMaterial;
  vx: number; vy: number; vz: number;
  sx: number; sy: number; sz: number;
  t: number; ttl: number;
  active: boolean;
};

export default function initVrFx(App: AppKernel) {
  if (App.vrFx) return; // 幂等

  let cheerCount = 0;

  const fx = App.vrFx = {
    active: false,
    get count() { return cheerCount; },
    celebrate: () => {},
    confetti: () => {},
    shake: () => {},
    strobe: () => {},
    encourage: () => {},
    show: () => {},
    hide: () => {},
    update: () => {}
  } as VrFx;

  /* ---------------- 状态（闭包内，不进 App 命名空间） ---------------- */
  let root: THREE.Group | null = null;
  let baseRing: THREE.Mesh | null = null;    // .hf-base 底座光环
  let baseMat: THREE.MeshBasicMaterial | null = null;
  let cone: THREE.Mesh | null = null;        // .hf-cone 投影光锥
  let coneMat: THREE.MeshBasicMaterial | null = null;
  let tierP: Panel | null = null;            // .hc-tier 评级大字
  let flashMesh: THREE.Mesh | null = null;   // .lf-strobe 频闪
  let flashMat: THREE.MeshBasicMaterial | null = null;
  const pieces: Piece[] = [];                // #holo-confetti 彩带池

  let baseT = 0, baseTTL = 0, baseBoom = false; // 底座脉冲
  let glowT = 0, glowLv = 0;                    // 「整层亮一下」
  let tierT = 0;                                // 评级大字
  let flashT = 0;                               // 频闪
  let dimT = 0;                                 // 鼓励呼吸
  let shakeT = 0, shakeTTL = 0, shakeIdx = 0;   // 世界微抖
  let shakeBase: THREE.Vector3 | null = null;   // 抖动前的场景原点（必须复位）

  const head = new THREE.Vector3();
  const fwd = new THREE.Vector3();
  const tmpA = new THREE.Vector3();

  /* ---------------- 工具 ---------------- */
  // 场景锚点：角色脚下（拿不到角色时退到头显前下方）
  function anchor(out: THREE.Vector3): THREE.Vector3 {
    const mg = App.modelGroup;
    if (mg && mg.position) return out.set(mg.position.x, mg.position.y, mg.position.z);
    const hp = App._xrHeadPos;
    if (hp) return out.set(hp.x, hp.y - 1.6, hp.z - 1.2);
    return out.set(0, 0, 0);
  }

  // 手柄触觉：VR 里「震一下」的等价物（DOM 的舞台抖动在头显里没有落点）
  function haptic(intensity: number, ms: number) {
    try {
      const r: any = App.renderer;
      const session = r && r.xr && r.xr.getSession ? r.xr.getSession() : null;
      const srcs = (session && session.inputSources) || [];
      for (const s of srcs as any[]) {
        const act = s && s.gamepad && s.gamepad.hapticActuators && s.gamepad.hapticActuators[0];
        if (act && typeof act.pulse === 'function') act.pulse(intensity, ms);
      }
    } catch (_) { /* 触觉不可用不影响主流程 */ }
  }

  function makePanel(cvW: number, cvH: number, worldW: number, order: number): Panel {
    const cv = document.createElement('canvas');
    cv.width = cvW;
    cv.height = cvH;
    const ctx = cv.getContext('2d')!;
    const tex = new THREE.CanvasTexture(cv);
    tex.colorSpace = THREE.SRGBColorSpace;
    tex.minFilter = THREE.LinearFilter;
    const mat = new THREE.MeshBasicMaterial({
      map: tex,
      transparent: true,
      opacity: 0,
      depthWrite: false,
      depthTest: false,   // HUD 语义：不被角色 / 大屏遮挡
      side: THREE.DoubleSide
    });
    const mesh = new THREE.Mesh(new THREE.PlaneGeometry(worldW, worldW * cvH / cvW), mat);
    mesh.renderOrder = order;
    mesh.visible = false;
    mesh.raycast = () => {};  // 不截胡手柄射线
    return { cv, ctx, tex, mat, mesh };
  }

  /* ---------------- 场景对象 ---------------- */
  function ensure() {
    if (root || !App.scene) return;
    root = new THREE.Group();
    root.name = 'vr-fx';
    App.scene.add(root);

    // 底座光环：外径 1，靠 scale 做脉冲扩散（.hf-base.pulse / .boom）
    baseMat = new THREE.MeshBasicMaterial({
      color: 0x8fe6ff, transparent: true, opacity: 0, side: THREE.DoubleSide,
      blending: THREE.AdditiveBlending, depthWrite: false
    });
    baseRing = new THREE.Mesh(new THREE.RingGeometry(0.86, 1, 64), baseMat);
    baseRing.name = 'vr-fx-base';
    baseRing.rotation.x = -Math.PI / 2;
    baseRing.visible = false;
    baseRing.raycast = () => {};
    root.add(baseRing);

    // 投影光锥：光源在下方往上打（角色是「被投射出来」的）
    coneMat = new THREE.MeshBasicMaterial({
      color: 0x8fe6ff, transparent: true, opacity: 0, side: THREE.DoubleSide,
      blending: THREE.AdditiveBlending, depthWrite: false
    });
    cone = new THREE.Mesh(new THREE.CylinderGeometry(0.62, 0.12, 1.9, 28, 1, true), coneMat);
    cone.name = 'vr-fx-cone';
    cone.visible = false;
    cone.raycast = () => {};
    root.add(cone);

    // 评级大字
    tierP = makePanel(TIER_CV.w, TIER_CV.h, TIER_WORLD_W, 960);
    tierP.mesh.name = 'vr-fx-tier';
    root.add(tierP.mesh);

    // 频闪：贴着头显的一块大面
    flashMat = new THREE.MeshBasicMaterial({
      color: 0xffffff, transparent: true, opacity: 0, side: THREE.DoubleSide,
      blending: THREE.AdditiveBlending, depthWrite: false, depthTest: false
    });
    flashMesh = new THREE.Mesh(new THREE.PlaneGeometry(3.4, 2.6), flashMat);
    flashMesh.name = 'vr-fx-flash';
    flashMesh.renderOrder = 970;
    flashMesh.visible = false;
    flashMesh.raycast = () => {};
    root.add(flashMesh);

    // 彩带：34 片各自独立材质（每片淡出节奏不同，共享材质会互相盖掉 opacity）
    const geo = new THREE.PlaneGeometry(CONFETTI_W, CONFETTI_H);
    for (let i = 0; i < CONFETTI_N; i++) {
      const mat = new THREE.MeshBasicMaterial({
        color: new THREE.Color(CONFETTI_COLORS[i % CONFETTI_COLORS.length]),
        transparent: true, opacity: 1, depthWrite: false, side: THREE.DoubleSide
      });
      const mesh = new THREE.Mesh(geo, mat);
      mesh.name = 'vr-fx-confetti';
      mesh.visible = false;
      mesh.raycast = () => {};
      root.add(mesh);
      pieces.push({ mesh, mat, vx: 0, vy: 0, vz: 0, sx: 0, sy: 0, sz: 0, t: 0, ttl: 0, active: false });
    }
  }

  /* ---------------- 评级大字（.hc-tier） ---------------- */
  function drawTier(level: number) {
    if (!tierP) return;
    const { ctx, tex } = tierP;
    const W = TIER_CV.w, H = TIER_CV.h;
    const t = TIERS[level] || { en: '', cn: '' };
    const rgb = TIER_RGB[level] || '255,255,255';
    ctx.clearRect(0, 0, W, H);
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    const enSize = (level >= 5 ? 66 : 52) * 2;  // CSS 52px（lv5 66px）× 2 密度
    const enY = H * 0.40;
    (ctx as any).letterSpacing = '6px';         // letter-spacing 3px × 2
    ctx.font = '900 ' + enSize + 'px ' + FONT;
    ctx.fillStyle = '#ffffff';
    ctx.shadowColor = 'rgba(' + rgb + ',0.95)';
    ctx.shadowBlur = 36;                        // text-shadow 18px × 2
    ctx.fillText(t.en, W / 2, enY);
    ctx.shadowColor = 'rgba(' + rgb + ',0.6)';
    ctx.shadowBlur = 92;                        // 第二层 46px × 2
    ctx.fillText(t.en, W / 2, enY);
    (ctx as any).letterSpacing = '8px';         // 4px × 2
    ctx.font = '30px ' + FONT;                  // 15px × 2
    ctx.shadowColor = 'rgba(' + rgb + ',0.85)';
    ctx.shadowBlur = 28;                        // 14px × 2
    ctx.fillStyle = 'rgba(255,255,255,0.86)';
    ctx.fillText(t.cn, W / 2, enY + enSize * 0.6 + 14); // margin-top 7px × 2
    (ctx as any).letterSpacing = '0px';
    ctx.shadowColor = 'transparent';
    ctx.shadowBlur = 0;
    tex.needsUpdate = true;
  }

  /** hc-tier-in 关键帧：0% s0.55 → 18% s1.12 → 30% s1 → 78% 保持 → 100% 淡出上移 */
  function tierAnim(k: number): { s: number; o: number; dy: number } {
    if (k < 0.18) {
      const u = k / 0.18;
      return { s: 0.55 + 0.57 * u, o: u, dy: 0.024 * (1 - u) }; // 起点下移 12px
    }
    if (k < 0.3) {
      const u = (k - 0.18) / 0.12;
      return { s: 1.12 - 0.12 * u, o: 1, dy: 0 };
    }
    if (k < 0.78) return { s: 1, o: 1, dy: 0 };
    const u = (k - 0.78) / 0.22;
    return { s: 1 - 0.06 * u, o: 1 - u, dy: -0.036 * u }; // 终点上移 18px
  }

  /* ---------------- 彩带（#holo-confetti，5 级专用） ---------------- */
  function burstConfetti() {
    ensure();
    if (!root) return;
    const a = anchor(tmpA);
    for (let i = 0; i < pieces.length; i++) {
      const p = pieces[i];
      const ang = Math.random() * Math.PI * 2;
      const rad = Math.sqrt(Math.random()) * CONFETTI_SPREAD; // 「满屏」→ 半径 1.4m 圆盘
      const ttl = 1.6 + Math.random() * 1.4;                  // --d 1.6~3.0s
      const dx = (Math.random() - 0.5) * 0.5;                 // --dx ±220px ≈ ±0.25m
      const dz = (Math.random() - 0.5) * 0.4;
      const spin = ((Math.random() * 720 - 360) * Math.PI / 180) / ttl; // --rot ±360°
      p.t = 0;
      p.ttl = ttl;
      p.active = true;
      p.vx = dx / ttl;
      p.vz = dz / ttl;
      p.vy = -CONFETTI_FALL / ttl;
      p.sx = spin;
      p.sy = spin * (Math.random() - 0.5);
      p.sz = spin * (Math.random() - 0.5);
      p.mesh.position.set(
        a.x + Math.cos(ang) * rad,
        a.y + CONFETTI_FALL + Math.random() * 0.35, // top:-4% ≈ 头顶之上
        a.z + Math.sin(ang) * rad
      );
      p.mesh.rotation.set(Math.random() * 6.283, Math.random() * 6.283, Math.random() * 6.283);
      p.mat.opacity = 0;
      p.mesh.visible = true;
    }
  }

  /* ---------------- 触发（参数逐项对齐 41 的 celebrate / style.css） ---------------- */
  function pulseBase(level: number) {
    ensure();
    baseBoom = level >= 5;
    baseT = 0;
    baseTTL = baseBoom ? 1.0 : 0.52; // boom 1000ms / pulse 520ms
  }

  function doStrobe() {
    ensure();
    flashT = 1e-4; // >0 即开始（0 表示未触发）
  }

  function doShake(power = 1) {
    ensure();
    shakeIdx = Math.max(0, Math.min(2, Math.floor(power) - 1));
    shakeT = 0;
    shakeTTL = SHAKE_DUR[shakeIdx];
    const sc = App.scene;
    if (sc && !shakeBase) shakeBase = sc.position.clone();
    // 头显里晃相机是晕眩源，触觉才是 VR 里等价的那一下
    haptic(power >= 3 ? 0.6 : 0.4, power >= 3 ? 90 : 60);
  }

  function celebrate(level: number, _opts?: { combo?: number }) {
    const lv = Math.max(0, Math.min(5, Math.floor(level) || 0));
    if (lv <= 0) return;
    ensure();
    cheerCount += 1;
    // 整层亮一下：级别越高越亮越久（41 的 data-cheer ttl）
    glowLv = lv;
    glowT = glowTTL(lv);
    // 评级大字：3 级以上
    if (lv >= 3 && tierP) {
      tierT = 0;
      drawTier(lv);
      tierP.mat.opacity = 0;
      tierP.mesh.visible = true;
    }
    if (lv >= 4) {
      doStrobe();
      doShake(lv >= 5 ? 3 : 2);
    }
    pulseBase(lv); // 每一级都有：投影仪在回应你
    if (lv >= 5) burstConfetti();
  }

  function encourage() {
    ensure();
    dimT = DIM_TTL;
  }

  /* 公开 API 接线：内部触发也统一走 fx.* —— 外部层（vr-holo 等）包住它
   * 就能挂到同一条欢呼链上，不必各自再包一遍 onToolResult / arcade.pulse */
  fx.celebrate = celebrate;
  fx.confetti = burstConfetti;
  fx.shake = (p?: number) => doShake(p);
  fx.strobe = doStrobe;
  fx.encourage = encourage;

  /* ---------------- 每帧：定位 + 推进 ---------------- */
  fx.update = function update(dt: number) {
    if (!fx.active) return;
    ensure();
    if (!root) return;

    const hp = App._xrHeadPos;
    if (!hp) return;
    const r: any = App.renderer;
    let hx = 0, hz = -1;
    if (r && r.xr && r.xr.isPresenting) {
      try {
        r.xr.getCamera().getWorldDirection(fwd);
        hx = fwd.x;
        hz = fwd.z;
      } catch (_) { /* 取不到就沿默认前向摆 */ }
    }
    const len = Math.hypot(hx, hz) || 1;
    hx /= len;
    hz /= len;
    head.set(hp.x, hp.y, hp.z);
    const a = anchor(tmpA);

    // ---- 底座脉冲（.hf-base.pulse / .boom）：角色脚下 ----
    if (baseRing && baseMat) {
      if (baseT < baseTTL) {
        baseT += dt;
        const k = Math.min(1, baseT / baseTTL);
        const r0 = 0.3;
        const r1 = baseBoom ? 1.15 : 0.72;
        const s = r0 + (r1 - r0) * k;
        baseRing.position.set(a.x, a.y + 0.02, a.z);
        baseRing.scale.set(s, s, 1);
        baseMat.opacity = (1 - k) * (baseBoom ? 0.95 : 0.75);
        baseRing.visible = true;
      } else if (baseRing.visible) {
        baseRing.visible = false;
        baseMat.opacity = 0;
      }
    }

    // ---- 光锥：欢呼时「整层亮一下」（data-cheer）+ 失败鼓励的呼吸（hf-dim） ----
    if (cone && coneMat) {
      let op = 0;
      if (glowT > 0) {
        glowT = Math.max(0, glowT - dt);
        op = 0.05 + glowLv * 0.022; // 级别越高越亮
      }
      if (dimT > 0) {
        dimT = Math.max(0, dimT - dt);
        op = Math.max(op, 0.035 * Math.sin((1 - dimT / DIM_TTL) * Math.PI));
      }
      if (op > 0.001) {
        cone.position.set(a.x, a.y + 0.95, a.z);
        coneMat.opacity = op;
        cone.visible = true;
      } else if (cone.visible) {
        cone.visible = false;
        coneMat.opacity = 0;
      }
    }

    // ---- 评级大字（.hc-tier）：悬在视线前方 ----
    if (tierP) {
      if (tierT < TIER_ANIM && tierP.mesh.visible) {
        tierT += dt;
        const an = tierAnim(Math.min(1, tierT / TIER_ANIM));
        const m = tierP.mesh;
        m.position.set(hp.x + hx * TIER_DIST, hp.y + TIER_UP + an.dy, hp.z + hz * TIER_DIST);
        m.lookAt(head.x, head.y, head.z);
        m.scale.set(an.s, an.s, an.s);
        tierP.mat.opacity = an.o;
      } else if (tierP.mesh.visible) {
        tierP.mesh.visible = false;
        tierP.mat.opacity = 0;
      }
    }

    // ---- 频闪（.lf-strobe）：460ms 内 3 次，贴着头显 ----
    if (flashMesh && flashMat) {
      if (flashT > 0 && flashT < STROBE_TTL) {
        flashT += dt;
        const k = flashT / STROBE_TTL;
        const on = (k * STROBE_TIMES) % 1 < 0.45;
        flashMat.opacity = on ? STROBE_PEAK * (1 - k * 0.4) : 0;
        flashMesh.position.set(hp.x + hx * 1.2, hp.y, hp.z + hz * 1.2);
        flashMesh.lookAt(head.x, head.y, head.z);
        flashMesh.visible = true;
      } else if (flashMesh.visible) {
        flashMesh.visible = false;
        flashMat.opacity = 0;
      }
    }

    // ---- 世界微抖（#stage.holo-shake-*）：整个场景在震 ----
    if (shakeBase) {
      const sc = App.scene;
      if (shakeT < shakeTTL) {
        shakeT += dt;
        const k = Math.min(1, shakeT / shakeTTL);
        const keys = SHAKE_KEYS[shakeIdx];
        let i = 0;
        while (i < keys.length - 2 && k > keys[i + 1][0]) i += 1;
        const k1 = keys[i];
        const k2 = keys[i + 1];
        const span = k2[0] - k1[0] || 1;
        const u = Math.min(1, Math.max(0, (k - k1[0]) / span));
        const ox = (k1[1] + (k2[1] - k1[1]) * u) * PX;
        const oy = (k1[2] + (k2[2] - k1[2]) * u) * PX;
        if (sc) sc.position.set(shakeBase.x + ox, shakeBase.y + oy, shakeBase.z);
      } else {
        if (sc) sc.position.copy(shakeBase); // 必须复位：世界错位是不可逆的观感事故
        shakeBase = null;
        shakeTTL = 0;
      }
    }

    // ---- 彩带（#holo-confetti） ----
    for (let i = 0; i < pieces.length; i += 1) {
      const p = pieces[i];
      if (!p.active) continue;
      p.t += dt;
      if (p.t >= p.ttl) {
        p.active = false;
        p.mesh.visible = false;
        continue;
      }
      const k = p.t / p.ttl;
      p.mesh.position.x += p.vx * dt;
      p.mesh.position.y += p.vy * dt;
      p.mesh.position.z += p.vz * dt;
      p.mesh.rotation.x += p.sx * dt;
      p.mesh.rotation.y += p.sy * dt;
      p.mesh.rotation.z += p.sz * dt;
      // hc-confetti：8% 处 opacity 到 1，之后线性淡到 0
      p.mat.opacity = k < 0.08 ? k / 0.08 : 1 - (k - 0.08) / 0.92;
      if (p.mesh.position.y < a.y - 0.1) { // 落到脚底即回收（104vh 的终点）
        p.active = false;
        p.mesh.visible = false;
      }
    }
  };

  /* ---------------- 显示/隐藏 ---------------- */
  fx.show = function show() {
    ensure();
    fx.active = true;
    if (root) root.visible = true;
    baseT = 0;
    baseTTL = 0;
    glowT = 0;
    tierT = TIER_ANIM; // 首帧不显示残留大字
    flashT = 0;
    dimT = 0;
    shakeT = 0;
    shakeTTL = 0;
    shakeBase = null;
    if (baseRing) { baseRing.visible = false; if (baseMat) baseMat.opacity = 0; }
    if (cone) { cone.visible = false; if (coneMat) coneMat.opacity = 0; }
    if (tierP) { tierP.mesh.visible = false; tierP.mat.opacity = 0; }
    if (flashMesh) { flashMesh.visible = false; if (flashMat) flashMat.opacity = 0; }
    for (let i = 0; i < pieces.length; i += 1) {
      pieces[i].active = false;
      pieces[i].mesh.visible = false;
    }
  };

  fx.hide = function hide() {
    fx.active = false;
    // 世界抖动中途退出 VR：必须复位，否则整个场景永久偏移
    if (shakeBase && App.scene) App.scene.position.copy(shakeBase);
    shakeBase = null;
    shakeTTL = 0;
    if (root) root.visible = false;
  };

  /* ---------------- 与非 VR 打通：包裹既有出口 ----------------
   * 41_holo_stage 包的是 App.game.onToolResult / App.arcade.pulse，它调的是自己
   * 闭包里的 celebrate —— 所以只包 App.holo.* 抓不到暴击/升级。这里同样包那两个
   * 出口（本模块 init 在 41 之后，包在外层），再加 App.holo.* 抓手动调用。
   * 两边各做各的：DOM 层照旧，VR 层只在自己 active 时出手，不重复。
   */
  const H: any = App.holo;

  if (H && typeof H.celebrate === 'function') {
    const orig = H.celebrate;
    H.celebrate = function patched(level: number, opts?: { combo?: number }) {
      const res = orig.apply(this, arguments as any);
      try { if (fx.active) fx.celebrate(level, opts); } catch (_) { /* 特效层出错不影响主流程 */ }
      return res;
    };
  }

  if (H && typeof H.cheer === 'function') {
    const orig = H.cheer;
    H.cheer = function patched(input: any) {
      const lv = orig.apply(this, arguments as any);
      try {
        if (fx.active) {
          if (lv > 0) fx.celebrate(lv, { combo: input && input.combo });
          else fx.encourage();
        }
      } catch (_) { /* 同上 */ }
      return lv;
    };
  }

  const wrap = (name: string, run: (arg?: any) => void) => {
    if (!H || typeof H[name] !== 'function') return;
    const orig = H[name];
    H[name] = function patched(arg?: any) {
      const res = orig.apply(this, arguments as any);
      try { if (fx.active) run(arg); } catch (_) { /* 同上 */ }
      return res;
    };
  };
  wrap('confetti', () => fx.confetti());
  wrap('shake', (p) => fx.shake(p));
  wrap('strobe', () => fx.strobe());
  wrap('encourage', () => fx.encourage());

  if (App.game && typeof App.game.onToolResult === 'function') {
    const orig = App.game.onToolResult;
    App.game.onToolResult = function patched(el: HTMLElement, success: boolean, costMs: number) {
      const res = orig.apply(this, arguments as any);
      try {
        if (fx.active) {
          const snap = App.game && App.game.snapshot ? App.game.snapshot() : null;
          const combo = snap && Number.isFinite(snap.combo) ? Number(snap.combo) : 1;
          const lv = H && typeof H.level === 'function'
            ? H.level({ success, combo, grade: gradeOf(costMs) })
            : (success ? 1 : 0);
          if (lv > 0) fx.celebrate(lv, { combo });
          else fx.encourage();
        }
      } catch (_) { /* 欢呼层出错绝不能拖垮工具链 */ }
      return res;
    };
  }

  if (App.arcade && typeof App.arcade.pulse === 'function') {
    const orig = App.arcade.pulse;
    App.arcade.pulse = function patched(kind?: 'crit' | 'levelup' | 'tool') {
      const res = orig.apply(this, arguments as any);
      try {
        if (fx.active) {
          if (kind === 'crit') fx.celebrate(4);
          else if (kind === 'levelup') fx.celebrate(5);
        }
      } catch (_) { /* 同上 */ }
      return res;
    };
  }

  // 进入/退出 VR 时显示/隐藏（与 vr-ui / vr-hud 同一套挂法）
  const _enter = App.enterXrMode;
  App.enterXrMode = async function (...args: any[]) {
    const res = await _enter.apply(this, args);
    if (FX_ON_IN_VR) fx.show(); else fx.hide();
    return res;
  };
  const _exit = App.exitXrMode;
  App.exitXrMode = function (...args: any[]) {
    fx.hide();
    return _exit.apply(this, args);
  };

  App.updateVrFx = fx.update;
}

/* 与 41 / 34_game_fx 的评级阈值保持一致（这里只喂给 App.holo.level） */
function gradeOf(ms: number): 'S' | 'A' | 'B' | 'C' {
  if (!Number.isFinite(ms) || ms < 0) return 'C';
  if (ms <= 1500) return 'S';
  if (ms <= 5000) return 'A';
  if (ms <= 15000) return 'B';
  return 'C';
}
