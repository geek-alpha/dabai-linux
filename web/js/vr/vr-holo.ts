/* ============================================================
 * web/js/vr/vr-holo.ts —— VR 沉浸态全息投影 + 舞台灯光
 * ------------------------------------------------------------
 * 41_holo_stage 的常驻层（#holo-fx / #light-fx）在 WebXR 沉浸会话里
 * 整层不可见：戴上头显，角色只是「站在空气里」——没有投影底座、没有光锥、
 * 没有追光与侧射，空间是死的。本模块把这两层在世界里重建。
 *
 * 参数逐项对齐 style.css（1px ≈ 0.002m，即 1000px 视口 ≈ 2m 视野）：
 *   #holo-fx  底座 46%×11%（3.6s 呼吸）/ 刻度环 53%×14%（14s 一圈）/
 *             光锥 42%×76%（底 56% 宽 → 顶 24% 宽，6.5s 呼吸）/
 *             扫描带 16% 高（4.6s，仅 thinking）/ 色差 左右 5% 红青分离 /
 *             噪点 3px 点阵 / 四角框标 26px / 边缘刻度 11px×42%（1px 线 / 8px）
 *   #light-fx 染色 0.7 / 追光 54%×94%（9.5s 呼吸）/ 双侧斜射 34%×120%
 *             （-15°↔7°、15°↔-7°，9.5s / 10.5s）/ 轮廓光 32%×64%（8s 呼吸）
 *   状态配色   与 #status-badge 同源（41 也是这么取的）：thinking #ffd54f /
 *             listening #ff6b9d / speaking #4ade80 / offline #82829f / idle #00e5ff
 *   速度倍率   thinking 0.55 / listening 0.75 / speaking 1.4 / offline 1.7
 *   欢呼亮度   --hf-br 1.7|2.1|2.6、--lf-br 1.5|1.9|2.3（ttl = 220 + lv*160）
 *
 * 与非 VR 的差别（都是头显里的正确做法，不是偷懒）：
 *   1. 常驻层一律静态或低频：色差 / 噪点 / 染色在 CSS 里本来就写着「静态」，
 *      全屏叠加在头显里一动就是晕眩源 —— 这点 CSS 注释自己说明了
 *   2. 扫描带做成绕角色的环形带而非屏幕横条，且只扫 thinking（照抄语义约定）
 *   3. 四角框标 + 边缘刻度跟随视线（它们本来就是「屏幕边框」），其余元素
 *      锚在角色身上（它们本来就是「舞台上的光」）
 *   4. 亮度倍率不做阶跃：CSS 是瞬时切变量，头显里瞬时跳亮很扎眼，末段 0.2s 收回
 * ============================================================ */
import * as THREE from 'three';
import type { AppKernel, VrHolo } from '../types/app-kernel.js';

/* VR 全息 / 舞台灯光层开关：投影底座、刻度环、光锥、扫描带、色差、噪点、
 * 染色球、追光、侧射、轮廓光、框标 —— 这些是角色在头显里的存在感与舞台
 * 氛围，保留（用户要关的是彩带/大字/频闪/抖动那类花哨特效，在 vr-fx 里）。
 * 关掉后 hl.active 恒为 false，每帧 update 首行即返回；要关改成 false。 */
const HOLO_ON_IN_VR = true;

/* 折算基准：视口 1000px ≈ 2m（1px = 0.002m）——CSS 的百分比按此换算成米 */
const PX = 0.002;
const VW = 1000, VH = 1000;
const pctW = (p: number) => VW * p / 100 * PX;
const pctH = (p: number) => VH * p / 100 * PX;

/* ---- #holo-fx ---- */
const BASE_W = pctW(46), BASE_H = pctH(11), BASE_BREATH = 3.6;   // .hf-base
const SPIN_W = pctW(53), SPIN_H = pctH(14), SPIN_DUR = 14;        // .hf-base-spin
const CONE_W = pctW(42), CONE_H = pctH(76);                       // .hf-cone
const CONE_RB = CONE_W * 0.28, CONE_RT = CONE_W * 0.12, CONE_DUR = 6.5;
const SCAN_H = pctH(16), SCAN_R = 0.5, SCAN_DUR = 4.6;            // .hf-scan
const SCAN_TOP = 2.10, SCAN_BOT = -0.05;                          // translateY -30% → 620%
/* ---- #light-fx ---- */
const SPOT_W = pctW(54), SPOT_H = pctH(94);                       // .lf-spot
const SPOT_RT = SPOT_W * 0.10, SPOT_RB = SPOT_W * 0.50, SPOT_DUR = 9.5;
const SWEEP_W = pctW(34), SWEEP_H = pctH(120);                    // .lf-sweep
const SWEEP_RB = SWEEP_W * 0.03, SWEEP_RT = SWEEP_W * 0.50, SWEEP_X = pctW(32);
const SWEEP_L_DUR = 9.5, SWEEP_R_DUR = 10.5, SWEEP_A = 15, SWEEP_B = 7;
const RIM_W = pctW(32) * 1.7, RIM_H = pctH(64) * 1.5;             // .lf-rim（放大到包住角色）
const RIM_DUR = 8, RIM_BACK = 0.30;
const WASH_R = 6, GRAIN_R = 3.2;                                  // 染色球 / 噪点球半径

/* ---- 状态配色与倍率（style.css #holo-fx / #light-fx 逐项同源） ---- */
type StateName = 'idle' | 'thinking' | 'listening' | 'speaking' | 'offline';
const STATE_RGB: Record<StateName, string> = {
  idle: '0,229,255',
  thinking: '255,213,79',
  listening: '255,107,157',
  speaking: '74,222,128',
  offline: '130,130,160'
};
const STATE_SPEED: Record<StateName, number> = {
  idle: 1, thinking: 0.55, listening: 0.75, speaking: 1.4, offline: 1.7
};
const C2 = '124,92,255';                                   // --hf-c2 / --lf-c2 补光色
const HF_BR = [1, 1.7, 1.7, 1.7, 2.1, 2.6];                // --hf-br
const LF_BR = [1, 1.5, 1.5, 1.5, 1.9, 2.3];                // --lf-br
const glowTTL = (lv: number) => (220 + lv * 160) / 1000;   // 41 的 data-cheer ttl
const BR_RELEASE = 0.2;                                    // 末段收回时间
const DIM_TTL = 0.7;                                       // hf-dim 700ms

/* 四角框标 + 边缘刻度：做成跟随视线的框（它们本来就是「屏幕边框」）。
 * 头显没有统一屏幕宽高比，所以框按 ±26° 的视场取方阵，不套 CSS 的百分比。 */
const FRAME_CV = { w: 1024, h: 1024 };
const FRAME_DIST = 2.6, FRAME_WORLD_W = 2.6;
const FRAME_INSET_X = 0.06, FRAME_INSET_Y = 0.05;
const FRAME_CORNER = 26, FRAME_TICK_W = 11, FRAME_TICK_H = 0.42, FRAME_YAW_DAMP = 7;

type Panel = {
  cv: HTMLCanvasElement;
  ctx: CanvasRenderingContext2D;
  tex: THREE.CanvasTexture;
  mat: THREE.MeshBasicMaterial;
  mesh: THREE.Mesh;
};

export default function initVrHolo(App: AppKernel) {
  if (App.vrHolo) return; // 幂等

  let state: StateName = 'idle';

  const hl = App.vrHolo = {
    active: false,
    get state() { return state; },
    setCheer: () => {},
    dim: () => {},
    show: () => {},
    hide: () => {},
    update: () => {}
  } as VrHolo;

  /* ---------------- 状态（闭包内，不进 App 命名空间） ---------------- */
  let root: THREE.Group | null = null;
  let wash: THREE.Mesh | null = null, washMat: THREE.MeshBasicMaterial | null = null;
  let grain: THREE.Mesh | null = null, grainMat: THREE.MeshBasicMaterial | null = null;
  let baseDisc: THREE.Mesh | null = null, baseMat: THREE.MeshBasicMaterial | null = null;
  let spinRing: THREE.Mesh | null = null, spinMat: THREE.MeshBasicMaterial | null = null;
  let cone: THREE.Mesh | null = null, coneMat: THREE.MeshBasicMaterial | null = null;
  let scan: THREE.Mesh | null = null, scanMat: THREE.MeshBasicMaterial | null = null;
  let spot: THREE.Mesh | null = null, spotMat: THREE.MeshBasicMaterial | null = null;
  let sweepL: THREE.Mesh | null = null, sweepR: THREE.Mesh | null = null;
  let sweepMatL: THREE.MeshBasicMaterial | null = null, sweepMatR: THREE.MeshBasicMaterial | null = null;
  let aberL: THREE.Mesh | null = null, aberR: THREE.Mesh | null = null;
  let aberMatL: THREE.MeshBasicMaterial | null = null, aberMatR: THREE.MeshBasicMaterial | null = null;
  let rim: THREE.Mesh | null = null, rimMat: THREE.MeshBasicMaterial | null = null;
  let frameP: Panel | null = null;
  let coneTex: THREE.CanvasTexture | null = null;
  let spotTex: THREE.CanvasTexture | null = null;
  let sweepTex: THREE.CanvasTexture | null = null;
  let washTex: THREE.CanvasTexture | null = null;

  let tAcc = 0;                       // 呼吸 / 自转相位（受速度倍率影响）
  let brT = 0, brHf = 1, brLf = 1;    // 欢呼亮度提升：剩余时间 + 倍率
  let dimT = 0;                       // 失败鼓励呼吸
  let smYaw = 0, smInit = false;      // 框标 yaw 阻尼跟随

  const head = new THREE.Vector3();
  const fwd = new THREE.Vector3();
  const tmpA = new THREE.Vector3();

  /* ---------------- 工具 ---------------- */
  // 场景锚点：角色脚下（拿不到角色时退到头显前下方），与 vr-fx 同一口径
  function anchor(out: THREE.Vector3): THREE.Vector3 {
    const mg = App.modelGroup;
    if (mg && mg.position) return out.set(mg.position.x, mg.position.y, mg.position.z);
    const hp = App._xrHeadPos;
    if (hp) return out.set(hp.x, hp.y - 1.6, hp.z - 1.2);
    return out.set(0, 0, 0);
  }

  /** 竖直渐变（stops 的位置按 CSS 语义给：fromTop=false 时 0 = 底部） */
  function vGrad(stops: [number, string][], fromTop = false): THREE.CanvasTexture {
    const cv = document.createElement('canvas');
    cv.width = 4;
    cv.height = 256;
    const ctx = cv.getContext('2d')!;
    const g = fromTop
      ? ctx.createLinearGradient(0, 0, 0, 256)
      : ctx.createLinearGradient(0, 256, 0, 0);
    for (const [p, c] of stops) g.addColorStop(Math.min(1, Math.max(0, p)), c);
    ctx.fillStyle = g;
    ctx.fillRect(0, 0, 4, 256);
    const tex = new THREE.CanvasTexture(cv);
    tex.colorSpace = THREE.SRGBColorSpace;
    return tex;
  }

  /** 径向渐变（0 = 中心，1 = 边缘） */
  function radialGrad(stops: [number, string][], size = 128): THREE.CanvasTexture {
    const cv = document.createElement('canvas');
    cv.width = cv.height = size;
    const ctx = cv.getContext('2d')!;
    const r = size / 2;
    const g = ctx.createRadialGradient(r, r, 0, r, r, r);
    for (const [p, c] of stops) g.addColorStop(Math.min(1, Math.max(0, p)), c);
    ctx.fillStyle = g;
    ctx.fillRect(0, 0, size, size);
    const tex = new THREE.CanvasTexture(cv);
    tex.colorSpace = THREE.SRGBColorSpace;
    return tex;
  }

  /** 全息噪点：3px 点阵、点半径 0.5px、白 0.55（.hf-grain 的 background-image） */
  function grainTex(): THREE.CanvasTexture {
    const cv = document.createElement('canvas');
    cv.width = cv.height = 48;
    const ctx = cv.getContext('2d')!;
    ctx.clearRect(0, 0, 48, 48);
    ctx.fillStyle = 'rgba(255,255,255,0.55)';
    for (let y = 0; y < 48; y += 3) {
      for (let x = 0; x < 48; x += 3) ctx.fillRect(x, y, 1, 1);
    }
    const tex = new THREE.CanvasTexture(cv);
    tex.colorSpace = THREE.SRGBColorSpace;
    tex.wrapS = tex.wrapT = THREE.RepeatWrapping;
    tex.repeat.set(10, 10);
    return tex;
  }

  /** 刻度环：1px 虚线（3px 实 3px 空）—— .hf-base-spin 的 border: 1px dashed */
  function dashRingTex(): THREE.CanvasTexture {
    const cv = document.createElement('canvas');
    cv.width = cv.height = 256;
    const ctx = cv.getContext('2d')!;
    ctx.clearRect(0, 0, 256, 256);
    ctx.strokeStyle = 'rgba(255,255,255,0.32)';
    ctx.lineWidth = 1;
    ctx.setLineDash([3, 3]);
    ctx.beginPath();
    ctx.ellipse(128, 128, 126, 126, 0, 0, Math.PI * 2);
    ctx.stroke();
    const tex = new THREE.CanvasTexture(cv);
    tex.colorSpace = THREE.SRGBColorSpace;
    return tex;
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
      map: tex, transparent: true, opacity: 1,
      depthWrite: false, depthTest: false,   // HUD 语义：不被角色 / 大屏遮挡
      side: THREE.DoubleSide
    });
    const mesh = new THREE.Mesh(new THREE.PlaneGeometry(worldW, worldW * cvH / cvW), mat);
    mesh.renderOrder = order;
    mesh.visible = false;
    mesh.raycast = () => {};  // 不截胡手柄射线
    return { cv, ctx, tex, mat, mesh };
  }

  /** 世界层材质：叠加混合 + 不写深度（挡住角色的背面由 depthTest 负责） */
  function glowMat(map?: THREE.Texture, opacity = 1): THREE.MeshBasicMaterial {
    return new THREE.MeshBasicMaterial({
      map, color: 0xffffff, transparent: true, opacity,
      side: THREE.DoubleSide, blending: THREE.AdditiveBlending, depthWrite: false
    });
  }

  /* ---------------- 场景对象（只在 show 时建：非 VR 一个对象都不建） ---------------- */
  function ensure() {
    if (root || !App.scene) return;
    root = new THREE.Group();
    root.name = 'vr-holo';
    App.scene.add(root);

    /* 舞台灯光：染色（空间基调） */
    washMat = glowMat(undefined, 0.7); // .lf-wash opacity: 0.7
    wash = new THREE.Mesh(new THREE.SphereGeometry(WASH_R, 20, 14), washMat);
    wash.name = 'vr-holo-wash';
    washMat.side = THREE.BackSide;     // 站在球内看内壁
    wash.raycast = () => {};
    root.add(wash);

    /* 舞台灯光：顶部追光（锥形，从上往下张开） */
    spotMat = glowMat(undefined, 0.72);
    spot = new THREE.Mesh(
      new THREE.CylinderGeometry(SPOT_RT, SPOT_RB, SPOT_H, 26, 1, true), spotMat);
    spot.name = 'vr-holo-spot';
    spot.raycast = () => {};
    root.add(spot);

    /* 舞台灯光：双侧斜射（从下往上散开，只有顶光画面是平的） */
    sweepMatL = glowMat(undefined, 1);
    sweepMatR = glowMat(undefined, 1);
    sweepL = new THREE.Mesh(new THREE.CylinderGeometry(SWEEP_RT, SWEEP_RB, SWEEP_H, 20, 1, true), sweepMatL);
    sweepR = new THREE.Mesh(new THREE.CylinderGeometry(SWEEP_RT, SWEEP_RB, SWEEP_H, 20, 1, true), sweepMatR);
    sweepL.name = 'vr-holo-sweep-l';
    sweepR.name = 'vr-holo-sweep-r';
    sweepL.raycast = () => {};
    sweepR.raycast = () => {};
    root.add(sweepL);
    root.add(sweepR);

    /* 舞台灯光：轮廓光（背后一圈极淡的光，把角色从背景里剥出来） */
    rimMat = glowMat(undefined, 0.5);
    rim = new THREE.Mesh(new THREE.PlaneGeometry(RIM_W, RIM_H), rimMat);
    rim.name = 'vr-holo-rim';
    rim.raycast = () => {};
    root.add(rim);

    /* 全息投影：底座椭圆光环 + 刻度环 */
    baseMat = glowMat(undefined, 0.72);
    baseDisc = new THREE.Mesh(new THREE.PlaneGeometry(1, 1), baseMat);
    baseDisc.name = 'vr-holo-base';
    baseDisc.rotation.x = -Math.PI / 2;
    baseDisc.raycast = () => {};
    root.add(baseDisc);

    spinMat = glowMat(dashRingTex(), 1);
    spinRing = new THREE.Mesh(new THREE.PlaneGeometry(1, 1), spinMat);
    spinRing.name = 'vr-holo-base-spin';
    spinRing.rotation.x = -Math.PI / 2;
    spinRing.raycast = () => {};
    root.add(spinRing);

    /* 全息投影：光锥（底宽顶收 —— 「被投射出来」的核心证据） */
    coneMat = glowMat(undefined, 0.7);
    cone = new THREE.Mesh(
      new THREE.CylinderGeometry(CONE_RT, CONE_RB, CONE_H, 28, 1, true), coneMat);
    cone.name = 'vr-holo-cone';
    cone.raycast = () => {};
    root.add(cone);

    /* 全息投影：逐行扫描（只扫 thinking；环形带而非屏幕横条） */
    scanMat = glowMat(undefined, 0);
    scan = new THREE.Mesh(
      new THREE.CylinderGeometry(SCAN_R, SCAN_R, SCAN_H, 28, 1, true), scanMat);
    scan.name = 'vr-holo-scan';
    scan.visible = false;
    scan.raycast = () => {};
    root.add(scan);

    /* 全息投影：色差（静态红青分离，贴在角色两侧） */
    aberMatL = glowMat(aberTex('left'), 0.6);   // .hf-aber opacity: 0.6
    aberMatR = glowMat(aberTex('right'), 0.6);
    aberL = new THREE.Mesh(new THREE.PlaneGeometry(0.5, 2.0), aberMatL);
    aberR = new THREE.Mesh(new THREE.PlaneGeometry(0.5, 2.0), aberMatR);
    aberL.name = 'vr-holo-aber-l';
    aberR.name = 'vr-holo-aber-r';
    aberL.raycast = () => {};
    aberR.raycast = () => {};
    root.add(aberL);
    root.add(aberR);

    /* 全息投影：噪点（静态点阵，只给质地，不给闪） */
    grainMat = glowMat(grainTex(), 0.1);        // .hf-grain opacity: 0.1
    grainMat.side = THREE.BackSide;
    grain = new THREE.Mesh(new THREE.SphereGeometry(GRAIN_R, 24, 16), grainMat);
    grain.name = 'vr-holo-grain';
    grain.raycast = () => {};
    root.add(grain);

    /* 四角框标 + 边缘刻度：跟随视线的框（屏幕边框在世界里的等价物） */
    frameP = makePanel(FRAME_CV.w, FRAME_CV.h, FRAME_WORLD_W, 940);
    frameP.mesh.name = 'vr-holo-frame';
    root.add(frameP.mesh);

    repaint();
    drawFrame();
  }

  /** 色差贴图：左红右青的极淡分离（.hf-aber 的 linear-gradient(90deg, ...)） */
  function aberTex(side: 'left' | 'right'): THREE.CanvasTexture {
    const cv = document.createElement('canvas');
    cv.width = 64;
    cv.height = 4;
    const ctx = cv.getContext('2d')!;
    const g = ctx.createLinearGradient(0, 0, 64, 0);
    if (side === 'left') {
      g.addColorStop(0, 'rgba(255,0,90,0.05)');   // 5%
      g.addColorStop(0.26, 'rgba(255,0,90,0)');
    } else {
      g.addColorStop(0.74, 'rgba(0,200,255,0)');
      g.addColorStop(1, 'rgba(0,200,255,0.05)');
    }
    ctx.fillStyle = g;
    ctx.fillRect(0, 0, 64, 4);
    const tex = new THREE.CanvasTexture(cv);
    tex.colorSpace = THREE.SRGBColorSpace;
    return tex;
  }

  /* ---------------- 状态色重绘（CSS 里 var() 是瞬时重解析，这里等价） ---------------- */
  function repaint() {
    if (!root) return;
    const rgb = STATE_RGB[state] || STATE_RGB.idle;

    // .hf-cone: linear-gradient(0deg, rgba(c,0.24), rgba(c,0.06) 48%, transparent 86%)
    coneTex = vGrad([
      [0, 'rgba(' + rgb + ',0.24)'],
      [0.48, 'rgba(' + rgb + ',0.06)'],
      [0.86, 'rgba(' + rgb + ',0)'],
      [1, 'rgba(' + rgb + ',0)']
    ]);
    if (coneMat) { coneMat.map = coneTex; coneMat.needsUpdate = true; }

    // .lf-spot: linear-gradient(180deg, rgba(c,0.20), rgba(c,0.05) 54%, transparent 86%)
    spotTex = vGrad([
      [0, 'rgba(' + rgb + ',0.20)'],
      [0.54, 'rgba(' + rgb + ',0.05)'],
      [0.86, 'rgba(' + rgb + ',0)'],
      [1, 'rgba(' + rgb + ',0)']
    ], true);
    if (spotMat) { spotMat.map = spotTex; spotMat.needsUpdate = true; }

    // .lf-sweep: linear-gradient(0deg, rgba(c2,0.30), rgba(c,0.09) 48%, transparent 82%)
    sweepTex = vGrad([
      [0, 'rgba(' + C2 + ',0.30)'],
      [0.48, 'rgba(' + rgb + ',0.09)'],
      [0.82, 'rgba(' + rgb + ',0)'],
      [1, 'rgba(' + rgb + ',0)']
    ]);
    if (sweepMatL) { sweepMatL.map = sweepTex; sweepMatL.needsUpdate = true; }
    if (sweepMatR) { sweepMatR.map = sweepTex; sweepMatR.needsUpdate = true; }

    // .lf-wash: 顶部 c2 0.13 → 44% 透明；中段 c 0.11 → 底部 0.06
    washTex = vGrad([
      [0, 'rgba(' + C2 + ',0.13)'],
      [0.44, 'rgba(' + C2 + ',0)'],
      [0.5, 'rgba(' + rgb + ',0.02)'],
      [0.62, 'rgba(' + rgb + ',0.11)'],
      [1, 'rgba(' + rgb + ',0.06)']
    ], true);
    if (washMat) { washMat.map = washTex; washMat.needsUpdate = true; }

    // .hf-base: radial-gradient(ellipse, rgba(c,0.40), rgba(c,0.11) 46%, transparent 74%)
    if (baseMat) {
      baseMat.map = radialGrad([
        [0, 'rgba(' + rgb + ',0.40)'],
        [0.46, 'rgba(' + rgb + ',0.11)'],
        [0.74, 'rgba(' + rgb + ',0)'],
        [1, 'rgba(' + rgb + ',0)']
      ]);
      baseMat.needsUpdate = true;
    }

    // .lf-rim: radial-gradient(ellipse, transparent 56%, rgba(c,0.15) 78%, transparent 93%)
    if (rimMat) {
      rimMat.map = radialGrad([
        [0, 'rgba(' + rgb + ',0)'],
        [0.56, 'rgba(' + rgb + ',0)'],
        [0.78, 'rgba(' + rgb + ',0.15)'],
        [0.93, 'rgba(' + rgb + ',0)'],
        [1, 'rgba(' + rgb + ',0)']
      ]);
      rimMat.needsUpdate = true;
    }

    // .hf-scan: linear-gradient(180deg, transparent, rgba(c,0.14) 44%, #fff 0.20 50%, ...)
    if (scanMat) {
      scanMat.map = vGrad([
        [0, 'rgba(' + rgb + ',0)'],
        [0.44, 'rgba(' + rgb + ',0.14)'],
        [0.5, 'rgba(255,255,255,0.20)'],
        [0.56, 'rgba(' + rgb + ',0.14)'],
        [1, 'rgba(' + rgb + ',0)']
      ]);
      scanMat.needsUpdate = true;
    }
  }

  /* ---------------- 四角框标 + 边缘刻度（.hf-frame / .hf-ticks） ---------------- */
  function drawFrame() {
    if (!frameP) return;
    const { ctx, tex } = frameP;
    const W = FRAME_CV.w, H = FRAME_CV.h;
    const rgb = STATE_RGB[state] || STATE_RGB.idle;
    // 画布像素 ↔ CSS 像素：画布 1px = FRAME_WORLD_W/W 米，CSS 1px = PX 米
    const k = PX * W / FRAME_WORLD_W;
    ctx.clearRect(0, 0, W, H);
    const mx = W * FRAME_INSET_X, my = H * FRAME_INSET_Y;   // inset 5% 6%
    const c = FRAME_CORNER * k;
    ctx.lineWidth = Math.max(1, 1 * k);
    ctx.strokeStyle = 'rgba(' + rgb + ',0.40)';
    // 四角各画两条边（.hf-frame i:nth-child(1..4) 只留朝内的两条）
    const corners: number[][] = [
      [mx, my, 1, 1], [W - mx, my, -1, 1], [mx, H - my, 1, -1], [W - mx, H - my, -1, -1]
    ];
    for (let i = 0; i < corners.length; i += 1) {
      const x = corners[i][0], y = corners[i][1];
      const sx = corners[i][2], sy = corners[i][3];
      ctx.beginPath();
      ctx.moveTo(x + sx * c, y);
      ctx.lineTo(x, y);
      ctx.lineTo(x, y + sy * c);
      ctx.stroke();
    }
    // 边缘刻度：左右各 11px 宽、42% 高，1px 线每 8px（repeating-linear-gradient）
    ctx.strokeStyle = 'rgba(' + rgb + ',0.38)';
    const tw = FRAME_TICK_W * k, th = H * FRAME_TICK_H, y0 = (H - th) / 2, step = 8 * k;
    ctx.beginPath();
    for (let y = y0; y <= y0 + th; y += step) {
      ctx.moveTo(0, y);
      ctx.lineTo(tw, y);
      ctx.moveTo(W - tw, y);
      ctx.lineTo(W, y);
    }
    ctx.stroke();
    tex.needsUpdate = true;
  }

  /* ---------------- 状态联动：与 41 同一个出口（#status-badge 的 class） ----------------
   * DOM 在 XR 会话里照样更新，只是不渲染 —— 所以读的还是同一份真相，
   * 两层不会各说各话。元素被换掉时自动重挂观察器。
   */
  let watched: HTMLElement | null = null;
  function stateOf(el: HTMLElement | null): StateName {
    const c = el ? el.className : '';
    if (c.includes('thinking')) return 'thinking';
    if (c.includes('listening')) return 'listening';
    if (c.includes('speaking')) return 'speaking';
    if (c.includes('active')) return 'idle';
    return 'offline';
  }
  function applyState() {
    const badge = document.getElementById('status-badge');
    const st = stateOf(badge);
    if (st !== state) {
      state = st;
      repaint();
      drawFrame();
    }
    if (badge && typeof MutationObserver !== 'undefined' && badge !== watched) {
      watched = badge;
      new MutationObserver(applyState).observe(badge, { attributes: true, attributeFilter: ['class'] });
    }
  }

  /* ---------------- 欢呼亮度 / 失败鼓励（与 41 的 data-cheer / hf-dim 同参） ---------------- */
  hl.setCheer = function setCheer(level: number) {
    const lv = Math.max(0, Math.min(5, Math.floor(level) || 0));
    if (lv <= 0) return;
    ensure();
    brHf = HF_BR[lv] || 1;
    brLf = LF_BR[lv] || 1;
    brT = glowTTL(lv);
  };

  hl.dim = function dim() {
    ensure();
    dimT = DIM_TTL;
  };

  /* ---------------- 每帧：定位 + 推进 ---------------- */
  hl.update = function update(dt: number) {
    if (!hl.active) return;
    ensure();
    if (!root) return;
    const hp = App._xrHeadPos;
    if (!hp) return;

    const a = anchor(tmpA);
    const sp = STATE_SPEED[state] || 1;
    tAcc += dt;

    // 0%,100% → 0；50% → 1（CSS 的 ease-in-out 呼吸）
    const breath = (dur: number) => 0.5 - 0.5 * Math.cos((tAcc * sp / dur) * Math.PI * 2);

    // 欢呼亮度：CSS 是瞬时切 --hf-br，头显里跳亮扎眼，末段 0.2s 收回
    let brHfMul = 1, brLfMul = 1;
    if (brT > 0) {
      brT = Math.max(0, brT - dt);
      const k = brT < BR_RELEASE ? brT / BR_RELEASE : 1;
      brHfMul = 1 + (brHf - 1) * k;
      brLfMul = 1 + (brLf - 1) * k;
    }
    // 失败鼓励：hf-dim 0% → 45% 0.4 → 100% 1（只压底座）
    let dimMul = 1;
    if (dimT > 0) {
      dimT = Math.max(0, dimT - dt);
      const k = 1 - dimT / DIM_TTL;
      dimMul = k < 0.45 ? 1 - 0.6 * (k / 0.45) : 0.4 + 0.6 * ((k - 0.45) / 0.55);
    }

    // ---- 投影底座（.hf-base 3.6s 呼吸 + 1.04 缩放） ----
    if (baseDisc && baseMat) {
      const b = breath(BASE_BREATH);
      const s = 1 + 0.04 * b;
      baseDisc.position.set(a.x, a.y + 0.01, a.z);
      baseDisc.scale.set(BASE_W * s, BASE_H * s, 1);
      baseMat.opacity = Math.min(1, (0.72 + 0.28 * b) * brHfMul * dimMul);
    }

    // ---- 底座刻度环（.hf-base-spin 14s 一圈，反向 = 机械在转） ----
    if (spinRing && spinMat) {
      spinRing.position.set(a.x, a.y + 0.02, a.z);
      spinRing.scale.set(SPIN_W, SPIN_H, 1);
      spinRing.rotation.z = -(tAcc * sp / SPIN_DUR) * Math.PI * 2;
      spinMat.opacity = Math.min(1, brHfMul);
    }

    // ---- 光锥（.hf-cone 6.5s 呼吸，opacity 0.7↔1 / scaleX 1↔1.05） ----
    if (cone && coneMat) {
      const b = breath(CONE_DUR);
      cone.position.set(a.x, a.y + 0.05 + CONE_H / 2, a.z);
      cone.scale.set(1 + 0.05 * b, 1, 1 + 0.05 * b);
      coneMat.opacity = Math.min(1, (0.7 + 0.3 * b) * brHfMul);
    }

    // ---- 逐行扫描（.hf-scan：4.6s 固定周期，一个周期 = 扫一次 + 静默一段；
    //      且只在 thinking —— 常驻扫描是没有语义的运动，只会抢注意力） ----
    if (scan && scanMat) {
      let op = 0, y = SCAN_TOP;
      if (state === 'thinking') {
        const k = (tAcc / SCAN_DUR) % 1;
        if (k < 0.05) op = 0.85 * (k / 0.05);
        else if (k < 0.42) { op = 0.85; y = SCAN_TOP + (SCAN_BOT - SCAN_TOP) * ((k - 0.05) / 0.37); }
        else if (k < 0.5) { op = 0.85 * (1 - (k - 0.42) / 0.08); y = SCAN_BOT; }
      }
      if (op > 0.01) {
        scan.visible = true;
        scan.position.set(a.x, a.y + y, a.z);
        scanMat.opacity = Math.min(1, op * brHfMul);
      } else if (scan.visible) {
        scan.visible = false;
        scanMat.opacity = 0;
      }
    }

    // ---- 顶部追光（.lf-spot 9.5s 呼吸，opacity 0.72↔1 / scaleY 1↔1.05） ----
    if (spot && spotMat) {
      const b = breath(SPOT_DUR);
      spot.position.set(a.x, a.y + 2.0 - SPOT_H / 2, a.z);
      spot.scale.set(1, 1 + 0.05 * b, 1);
      spotMat.opacity = Math.min(1, (0.72 + 0.28 * b) * brLfMul);
    }

    // ---- 双侧斜射（.lf-sweep：左 -15°→7° / 9.5s，右 15°→-7° / 10.5s，alternate） ----
    const ang = (u: number, sign: number) => (sign * (-SWEEP_A + (SWEEP_A + SWEEP_B) * u));
    if (sweepL && sweepMatL) {
      const u = breath(SWEEP_L_DUR);
      sweepL.position.set(a.x - SWEEP_X, a.y - 0.2 + SWEEP_H / 2, a.z);
      sweepL.rotation.z = -ang(u, 1) * Math.PI / 180;   // CSS rotate 顺时针为正，three 逆时针为正
      sweepMatL.opacity = Math.min(1, brLfMul);
    }
    if (sweepR && sweepMatR) {
      const u = breath(SWEEP_R_DUR);
      sweepR.position.set(a.x + SWEEP_X, a.y - 0.2 + SWEEP_H / 2, a.z);
      sweepR.rotation.z = -ang(u, -1) * Math.PI / 180;
      sweepMatR.opacity = Math.min(1, brLfMul);
    }

    // ---- 轮廓光（.lf-rim 8s 呼吸，opacity 0.5↔0.95 / scale 1↔1.04） ----
    if (rim && rimMat) {
      const b = breath(RIM_DUR);
      let dx = hp.x - a.x, dz = hp.z - a.z;
      const dl = Math.hypot(dx, dz) || 1;
      dx /= dl; dz /= dl;
      rim.position.set(a.x - dx * RIM_BACK, a.y + RIM_H / 2 - 0.1, a.z - dz * RIM_BACK);
      rim.rotation.set(0, Math.atan2(dx, dz), 0);   // 只绕 Y 朝向用户，不跟 pitch
      const s = 1 + 0.04 * b;
      rim.scale.set(s, s, 1);
      rimMat.opacity = Math.min(1, (0.5 + 0.45 * b) * brLfMul);
    }

    // ---- 色差（静态红青分离：贴在角色两侧，朝向用户；动起来才是全屏重合成） ----
    if (aberL && aberR) {
      const yaw = Math.atan2(hp.x - a.x, hp.z - a.z);
      const rx = Math.cos(yaw), rz = -Math.sin(yaw);   // 垂直于视线方向的右向量
      const ax = 0.45;
      aberL.position.set(a.x + rx * ax, a.y + 1.0, a.z + rz * ax);
      aberL.rotation.set(0, yaw, 0);
      aberR.position.set(a.x - rx * ax, a.y + 1.0, a.z - rz * ax);
      aberR.rotation.set(0, yaw, 0);
    }

    // ---- 氛围染色 / 噪点：套在用户身上的两个球（空间的基调是静止的） ----
    if (wash) wash.position.set(hp.x, hp.y, hp.z);
    if (grain) grain.position.set(hp.x, hp.y, hp.z);

    // ---- 四角框标 + 边缘刻度：跟随视线（yaw 阻尼，转头时略有滞后 = 空间感） ----
    if (frameP) {
      let hx = 0, hz = -1;
      const r: any = App.renderer;
      if (r && r.xr && r.xr.isPresenting) {
        try {
          r.xr.getCamera().getWorldDirection(fwd);
          hx = fwd.x;
          hz = fwd.z;
        } catch (_) { /* 取不到就沿默认前向摆 */ }
      }
      const len = Math.hypot(hx, hz) || 1;
      hx /= len; hz /= len;
      const targetYaw = Math.atan2(hx, hz);
      if (!smInit) { smYaw = targetYaw; smInit = true; }
      else {
        let d = targetYaw - smYaw;
        while (d > Math.PI) d -= Math.PI * 2;
        while (d < -Math.PI) d += Math.PI * 2;
        smYaw += d * (1 - Math.exp(-dt * FRAME_YAW_DAMP));
      }
      head.set(hp.x, hp.y, hp.z);
      frameP.mesh.position.set(hp.x + Math.sin(smYaw) * FRAME_DIST, hp.y, hp.z + Math.cos(smYaw) * FRAME_DIST);
      frameP.mesh.lookAt(head.x, head.y, head.z);
      frameP.mesh.visible = true;
    }
  };

  /* ---------------- 显示/隐藏 ---------------- */
  hl.show = function show() {
    ensure();
    hl.active = true;
    if (root) root.visible = true;
    tAcc = 0;
    brT = 0; brHf = 1; brLf = 1;
    dimT = 0;
    smInit = false;
    if (frameP) frameP.mesh.visible = false;
    if (scan) { scan.visible = false; if (scanMat) scanMat.opacity = 0; }
    applyState();   // 进入时立刻对齐当前状态色
  };

  hl.hide = function hide() {
    hl.active = false;
    if (root) root.visible = false;
  };

  /* ---------------- 与非 VR 打通：挂到 vr-fx 的欢呼链上 ----------------
   * vr-fx 的 celebrate / encourage 是所有欢呼（工具结果、暴击、升级、手动）
   * 的唯一出口，这里包一层就能拿到每一次「整层亮一下」，不必各自再包一遍
   * onToolResult / arcade.pulse。本模块 init 在 vr-fx 之后，包在外层。
   */
  const vfx: any = App.vrFx;
  if (vfx && typeof vfx.celebrate === 'function') {
    const orig = vfx.celebrate;
    vfx.celebrate = function patched(level: number, opts?: { combo?: number }) {
      const res = orig.apply(this, arguments as any);
      try { if (hl.active) hl.setCheer(level); } catch (_) { /* 亮度层出错不拖垮欢呼链 */ }
      return res;
    };
  }
  if (vfx && typeof vfx.encourage === 'function') {
    const orig = vfx.encourage;
    vfx.encourage = function patched() {
      const res = orig.apply(this, arguments as any);
      try { if (hl.active) hl.dim(); } catch (_) { /* 同上 */ }
      return res;
    };
  }

  // 进入/退出 VR 时显示/隐藏（与 vr-ui / vr-hud / vr-fx 同一套挂法）
  const _enter = App.enterXrMode;
  App.enterXrMode = async function (...args: any[]) {
    const res = await _enter.apply(this, args);
    // 只有真的进了沉浸会话才亮：进不去（不支持 / 被拒 / 超时）时这层会把整个画面
    // 染青（染色球半径 6m，普通相机就在球里），而它服务的是头显里的观感
    if (HOLO_ON_IN_VR && App.xrPresenting) hl.show();
    else hl.hide();
    return res;
  };
  const _exit = App.exitXrMode;
  App.exitXrMode = function (...args: any[]) {
    hl.hide();
    return _exit.apply(this, args);
  };

  App.updateVrHolo = hl.update;
  applyState();
}
