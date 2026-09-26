/* ============================================================
 * web/js/vr/vr-ray.ts —— VR 射线锁定仲裁（跨面板，唯一真源）
 * ------------------------------------------------------------
 * 问题：每块面板（语音/对话/工具栏/HUD/音乐/视频）各自实现 hitTest，
 * 判定都是「射线先打中我这块矩形，再看落点在哪个按钮上」。于是：
 *   1) 面板外一圈是死区 —— 射线擦着面板边缘走，什么都不亮、点不中；
 *   2) 视线精度差 —— 头显里眼动/手柄抖动都远大于按钮尺寸；
 *   3) 多块面板各问各的 —— 射线同时指着两块时，谁先被问谁抢到。
 *
 * 做法：把「射线指向哪块面板」这一件事抽出来统一仲裁：
 *   - 判定基准从「命中矩形」换成「射线与面板平面的交点落在矩形外扩
 *     marginM 米内」—— 面板四周因此有一圈看不见的磁力带；
 *   - 面板内按钮仍由各面板自己挑（它知道自己的按钮表）；
 *   - 跨面板用「视线离面板中心的偏角」比大小，最小者锁定 ——
 *     射线只锁一块面板，高亮与触发都跟着它。
 *
 * 于是「锁定」与「触发」解耦：锁定=最近的那块面板的最近按钮（宽松），
 * 触发沿用锁定的结果，所见即所得。
 * ============================================================ */
import * as THREE from 'three';
import type { AppKernel, VrRay } from '../types/app-kernel.js';

/** 面板注册项：pick 只回答「射线指着我吗 + 指到哪个按钮」 */
export interface VrRayPanel {
  id: string;
  active: () => boolean;
  pick: (origin: THREE.Vector3, dir: THREE.Vector3, loose: boolean) => { id: string; score: number } | null;
  trigger: (id: string) => void;
}

export interface VrRayHit {
  panel: { id: string; trigger: (id: string) => void };
  id: string;
  score: number;
}

const _q = new THREE.Quaternion();
const _n = new THREE.Vector3();
const _c = new THREE.Vector3();
const _p = new THREE.Vector3();
const _l = new THREE.Vector3();
const _cd = new THREE.Vector3();
const _plane = new THREE.Plane();
const _ray = new THREE.Ray();

/* ---------------- 凝视（dwell）统一引擎参数 ---------------- */
/** 视线在同一目标上停多久算按了一下。4s：头显里视线扫过面板是常态，
 *  1.1s 会在「只是看一眼」时误触发；4s 只有真盯着某颗按钮才点得动。 */
const DWELL_MS = 4000;
/** 焦点抖动宽限（ms）：视线短暂滑开（面板微晃/眼动噪声）不清零，
 *  否则进度永远攒不满 —— 头显里没有绝对静止的视线。 */
const DWELL_GRACE = 260;
/** 最近一次触发的目标 key（任何来源）。视线没移开就不重复触发。 */
let firedKey = '';

/** 射线 × 面板平面。交点在「面板矩形外扩 marginM 米」内即算指向它。
 *  返回画布像素落点 + 锁定分（越小 = 视线越贴面板中心，跨面板仲裁用）。 */
export function panelPoint(
  mesh: THREE.Mesh | null,
  origin: THREE.Vector3,
  dir: THREE.Vector3,
  cvW: number,
  cvH: number,
  marginM: number
): { px: number; py: number; score: number } | null {
  if (!mesh || !mesh.geometry) return null;
  mesh.updateMatrixWorld();
  mesh.getWorldQuaternion(_q);
  _n.set(0, 0, 1).applyQuaternion(_q).normalize();
  // 只认正面：从背后/极侧面看过去不该锁（否则绕过面板时按钮突然高亮）
  if (_n.dot(dir) > -0.15) return null;
  mesh.getWorldPosition(_c);
  _plane.setFromNormalAndCoplanarPoint(_n, _c);
  _ray.set(origin, dir);
  if (!_ray.intersectPlane(_plane, _p)) return null;
  if (_p.distanceTo(origin) > 8) return null;
  const par = (mesh.geometry as any).parameters || {};
  const w = par.width || 1;
  const h = par.height || (w * cvH / cvW);
  _l.copy(_p);
  mesh.worldToLocal(_l);
  if (Math.abs(_l.x) > w / 2 + marginM || Math.abs(_l.y) > h / 2 + marginM) return null;
  const px = ((_l.x + w / 2) / w) * cvW;
  const py = (1 - (_l.y + h / 2) / h) * cvH;
  _cd.copy(_c).sub(origin).normalize();
  return { px, py, score: 1 - _cd.dot(dir) };
}

/** 把一块面板包成注册项：外扩带用平面命中，按钮挑选交回面板自己 */
export function vrRayPanel(o: {
  id: string;
  active: () => boolean;
  mesh: () => THREE.Mesh | null;
  cvW: () => number;
  cvH: () => number;
  hit: (px: number, py: number, loose: boolean, inside: boolean) => string | null;
  trigger: (id: string) => void;
  /** 视线/确认键模式的外扩带（米）：0.24m 在 1.2~1.8m 视距下约 8~11°，
   *  视线擦着面板边缘也锁得住；再大就会在空处凭空点亮按钮。 */
  looseMargin?: number;
  /** 手柄精确点击模式的外扩带（米） */
  tightMargin?: number;
}): VrRayPanel {
  const looseM = o.looseMargin == null ? 0.24 : o.looseMargin;
  const tightM = o.tightMargin == null ? 0.03 : o.tightMargin;
  return {
    id: o.id,
    active: o.active,
    // 任何来源的触发（手柄/键盘/凝视）都记一笔：凝视引擎据此防连点 ——
    // 否则扳机点完、视线还停在原处，4s 后又被自动点一次
    trigger(id: string) {
      firedKey = o.id + ':' + id;
      o.trigger(id);
    },
    pick(origin, dir, loose) {
      const mesh = o.mesh();
      if (!mesh) return null;
      const cvW = o.cvW(), cvH = o.cvH();
      const pt = panelPoint(mesh, origin, dir, cvW, cvH, loose ? looseM : tightM);
      if (!pt) return null;
      // inside=false 表示落点在面板矩形外的磁力带里：交给面板自己决定要不要吸附
      const inside = pt.px >= 0 && pt.px <= cvW && pt.py >= 0 && pt.py <= cvH;
      const id = o.hit(pt.px, pt.py, loose, inside);
      return id ? { id, score: pt.score } : null;
    }
  };
}

export default function initVrRay(App: AppKernel) {
  if (App.vrRay) return; // 幂等

  const panels: VrRayPanel[] = [];

  const ray: VrRay = (App.vrRay = {
    panels,
    dwell: { panelId: '', id: '', p: 0 },
    register(p: VrRayPanel) {
      if (!p || panels.indexOf(p) >= 0) return;
      panels.push(p);
    },
    pick(origin: THREE.Vector3, dir: THREE.Vector3, loose: boolean): VrRayHit | null {
      let best: VrRayHit | null = null;
      for (const p of panels) {
        let r: { id: string; score: number } | null = null;
        try {
          if (!p.active()) continue;
          r = p.pick(origin, dir, loose);
        } catch (_) { /* 单块面板异常不该拖垮整条射线 */ }
        if (r && (!best || r.score < best.score - 1e-4)) best = { panel: p, id: r.id, score: r.score };
      }
      return best;
    },
    pickMine(id: string, origin: THREE.Vector3, dir: THREE.Vector3, loose: boolean): string {
      const h = ray.pick(origin, dir, loose);
      return h && h.panel.id === id ? h.id : '';
    },
    // 凝视计时（实现在下面，函数声明提升）
    update: (now: number) => dwellUpdate(now)
  });

  /* ---------------- 凝视点击：一处实现，所有面板自动获得 ----------------
   * 为什么放在仲裁器而不是各面板：凝视要用「跨面板互斥的锁定结果」计时，
   * 面板自己只能看到自己 —— 于是每加一块面板就要重写一遍计时/宽限/防连点，
   * 还会出现两块面板同时攒进度。放在这里，面板只需实现 trigger。 */
  const dwell = ray.dwell;
  const _o = new THREE.Vector3();
  const _d = new THREE.Vector3();
  let key = '';        // 正在计时的目标 `${panelId}:${id}`
  let since = 0;       // 计时起点
  let lostAt = 0;      // 焦点丢失时刻（宽限期内不清零）

  /* 凝视进度环：一圈细弧贴在视线前方，只在攒进度时出现。
   * 无目标时整个 mesh 隐藏 —— 视线里不常驻准星，画面才干净。 */
  const RING_CV = 128;
  const RING_DIST = 0.55;     // 环到眼睛的距离（米）：在面板（1.0~1.8m）前面，不遮内容
  const RING_WORLD = 0.07;    // 世界尺寸 ≈ 7° 视角，够看清不刺眼
  let ringMesh: THREE.Mesh | null = null;
  let ringTex: THREE.CanvasTexture | null = null;
  let ringMat: THREE.MeshBasicMaterial | null = null;
  let ringCtx: CanvasRenderingContext2D | null = null;
  let ringDrawn = -1;

  function ensureRing() {
    if (ringMesh || !App.scene) return;
    const cv = document.createElement('canvas');
    cv.width = RING_CV; cv.height = RING_CV;
    ringCtx = cv.getContext('2d');
    ringTex = new THREE.CanvasTexture(cv);
    ringTex.colorSpace = THREE.SRGBColorSpace;
    ringMat = new THREE.MeshBasicMaterial({
      map: ringTex, transparent: true, depthWrite: false,
      depthTest: false, side: THREE.DoubleSide
    });
    ringMesh = new THREE.Mesh(new THREE.PlaneGeometry(RING_WORLD, RING_WORLD), ringMat);
    ringMesh.renderOrder = 950;   // 盖在面板之上，但只有攒进度时才可见
    ringMesh.visible = false;
    ringMesh.raycast = () => {};  // 不参与命中，免得挡住手柄射线
    App.scene.add(ringMesh);
  }

  function drawRing(p: number) {
    if (!ringCtx || !ringTex) return;
    const c = ringCtx, S = RING_CV, c0 = S / 2, r = S * 0.34;
    c.clearRect(0, 0, S, S);
    c.lineCap = 'round';
    c.beginPath(); c.arc(c0, c0, r, 0, Math.PI * 2);
    c.lineWidth = 7; c.strokeStyle = 'rgba(255,255,255,0.14)'; c.stroke();
    c.beginPath(); c.arc(c0, c0, r, -Math.PI / 2, -Math.PI / 2 + Math.PI * 2 * p);
    c.lineWidth = 9; c.strokeStyle = 'rgba(110,240,255,0.95)'; c.stroke();
    c.beginPath(); c.arc(c0, c0, 5, 0, Math.PI * 2);
    c.fillStyle = 'rgba(110,240,255,0.75)'; c.fill();
    ringTex.needsUpdate = true;
    ringDrawn = p;
  }

  function showRing(p: number) {
    if (p <= 0.02) {
      if (ringMesh && ringMesh.visible) ringMesh.visible = false;
      ringDrawn = -1;
      return;
    }
    ensureRing();
    if (!ringMesh || !ringMat) return;
    // 按 2% 一档重绘：每帧重传整张纹理会白白吃掉头显的带宽
    if (ringDrawn < 0 || Math.abs(p - ringDrawn) > 0.02) drawRing(p);
    ringMat.opacity = Math.min(1, 0.25 + p);
    ringMesh.visible = true;
    ringMesh.position.copy(_o).addScaledVector(_d, RING_DIST);
    ringMesh.lookAt(_o);
  }

  /** 凝视触发的手感反馈：头显里「震一下」是唯一能替代按钮回弹的通道 */
  function haptic() {
    try {
      const r: any = App.renderer;
      const session = r && r.xr && r.xr.getSession ? r.xr.getSession() : null;
      const srcs = (session && session.inputSources) || [];
      for (const s of srcs as any[]) {
        const act = s && s.gamepad && s.gamepad.hapticActuators && s.gamepad.hapticActuators[0];
        if (act && typeof act.pulse === 'function') act.pulse(0.35, 45);
      }
    } catch (_) { /* 触觉不可用不影响凝视点击 */ }
  }

  /** 每帧一次（07_click_interact 帧循环调用）：
   *  视线 → 仲裁锁定 → 计时 → 满 DWELL_MS 触发。所有面板共用这一条路。 */
  function dwellUpdate(now: number) {
    const r = App.renderer;
    const on = !!(r && r.xr && r.xr.isPresenting && App._xrEyeRay);
    let hit: VrRayHit | null = null;
    if (on) {
      try { if (App._xrEyeRay(_o, _d)) hit = ray.pick(_o, _d, true); } catch (_) { hit = null; }
    }
    if (!hit) {
      if (key) {
        // 刚触发过的目标：视线一移开就重新武装 + 重新计时 ——
        // 否则把眼看回去会拿旧起点当场再点一次
        if (firedKey === key) { firedKey = ''; since = now; }
        if (!lostAt) lostAt = now;
        if (now - lostAt > DWELL_GRACE) {
          key = ''; lostAt = 0;
          dwell.panelId = ''; dwell.id = ''; dwell.p = 0;
        }
      }
      showRing(key ? dwell.p : 0);
      return;
    }
    const k = hit.panel.id + ':' + hit.id;
    if (lostAt) {
      if (now - lostAt > DWELL_GRACE) since = now;   // 滑开久了：重新计时，不把空档算进去
      lostAt = 0;
    }
    if (k !== key) {
      firedKey = '';                                  // 换目标 = 重新武装
      key = k; since = now;
      dwell.panelId = hit.panel.id; dwell.id = hit.id; dwell.p = 0;
      showRing(0);
      return;
    }
    if (firedKey === k) { dwell.p = 0; showRing(0); return; }   // 刚触发过：视线没移开就不点第二次
    const p = (now - since) / DWELL_MS;
    if (p >= 1) {
      firedKey = k;
      dwell.p = 0;
      haptic();
      try { hit.panel.trigger(hit.id); } catch (_) { /* 单块面板异常不该打断凝视循环 */ }
      showRing(0);
      return;
    }
    dwell.p = p;
    showRing(p);
  }

  // 各面板 hook 的统一入口：拿到锁定的面板+按钮，直接触发
  App._vrRayPick = function (origin: THREE.Vector3, dir: THREE.Vector3, loose?: boolean) {
    return ray.pick(origin, dir, loose !== false);
  };

  // 帧循环入口：凝视计时与触发（07_click_interact 每帧调一次）
  App.updateVrRay = dwellUpdate;
}
