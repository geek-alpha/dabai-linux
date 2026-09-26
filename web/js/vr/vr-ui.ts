/* ============================================================
 * web/js/vr/vr-ui.ts —— VR 沉浸态信息层（toast + 字幕）
 * ------------------------------------------------------------
 * WebXR 沉浸会话里浏览器不渲染页面 DOM：#toast / #subtitle 戴上头显
 * 就彻底消失——所有 showToast 反馈静默、说话字幕没了。本模块把这两层
 * 在 VR 世界里重建，视觉参数逐项对齐 style.css：
 *   toast：rgba(0,0,0,0.85) 底 / #fff 字 / 圆角 12 / 14px / 2.5s 淡出
 *   字幕：#f0f0ff 字 / 15px / 居中 / text-shadow 0 2px 8px rgba(0,0,0,0.8)
 * （画布按 2 倍密度绘制，字号/圆角/阴影均取 CSS 值的 2 倍，视觉一致）
 *
 * 与非 VR 的差别只在空间形态：面板悬在头显前方 2m 的圆柱面上，yaw 带
 * 阻尼跟随（转头时略有滞后 → 视差与空间感），而不是贴在脸上。
 * 面板 depthTest:false + renderOrder 900，不被角色/大屏遮挡；
 * mesh.raycast 置空，不截胡手柄射线（否则戳角色会被它挡住）。
 * ============================================================ */
import * as THREE from 'three';
import type { AppKernel, VrUi } from '../types/app-kernel.js';

export default function initVrUi(App: AppKernel) {
  if (App.vrUi) return; // 幂等

  const FONT = '"Microsoft YaHei", "PingFang SC", "Noto Sans SC", sans-serif';
  const TOAST_MS = 2500;   // 与 14_toast 的 setTimeout 一致
  const FADE = 0.3;        // 与 CSS transition 0.3s 一致
  const DIST = 2.0;        // 面板到眼睛的距离（米）
  const YAW_DAMP = 7;      // yaw 跟随阻尼（越大越"贴脸"）
  const TOAST_CV = { w: 760, h: 160 };
  const SUB_CV = { w: 1100, h: 260 };
  const TOAST_WORLD_W = 1.5;
  const SUB_WORLD_W = 2.8;

  const ui = App.vrUi = {
    active: false,
    toastMesh: null,
    subMesh: null,
    show: () => {},
    hide: () => {},
    update: () => {}
  } as VrUi;

  /* ---------------- 状态（闭包内，不进 App 命名空间） ---------------- */
  let toastText = '';
  let toastAt = 0;
  let toastUntil = 0;
  let toastDrawn = '';
  let subText = '';
  let subDrawn = '';
  let subOp = 0;
  let smYaw = 0;
  let smInit = false;

  const fwd = new THREE.Vector3();
  const head = new THREE.Vector3();

  type Panel = {
    cv: HTMLCanvasElement;
    ctx: CanvasRenderingContext2D;
    tex: THREE.CanvasTexture;
    mat: THREE.MeshBasicMaterial;
    mesh: THREE.Mesh;
  };
  let toastP: Panel | null = null;
  let subP: Panel | null = null;

  /* ---------------- Canvas 工具 ---------------- */
  function roundRect(ctx: CanvasRenderingContext2D, x: number, y: number, w: number, h: number, r: number) {
    ctx.beginPath();
    ctx.moveTo(x + r, y);
    ctx.arcTo(x + w, y, x + w, y + h, r);
    ctx.arcTo(x + w, y + h, x, y + h, r);
    ctx.arcTo(x, y + h, x, y, r);
    ctx.arcTo(x, y, x + w, y, r);
    ctx.closePath();
  }

  function fitOne(ctx: CanvasRenderingContext2D, s: string, maxW: number, font: string): string {
    ctx.font = font;
    const t = String(s == null ? '' : s);
    if (ctx.measureText(t).width <= maxW) return t;
    let cut = t.length;
    while (cut > 1 && ctx.measureText(t.slice(0, cut) + '…').width > maxW) cut--;
    return t.slice(0, cut) + '…';
  }

  // 按宽度折行，最多 maxLines 行（超出时末行加省略号）
  function wrapLines(ctx: CanvasRenderingContext2D, s: string, maxW: number, maxLines: number): string[] {
    const all: string[] = [];
    let line = '';
    for (const ch of String(s == null ? '' : s)) {
      if (ch === '\n') { all.push(line); line = ''; continue; }
      const t = line + ch;
      if (line && ctx.measureText(t).width > maxW) { all.push(line); line = ch; }
      else line = t;
    }
    if (line) all.push(line);
    if (all.length <= maxLines) return all;
    const keep = all.slice(0, maxLines);
    keep[maxLines - 1] = keep[maxLines - 1].slice(0, Math.max(1, keep[maxLines - 1].length - 1)) + '…';
    return keep;
  }

  /* ---------------- 面板构建 ---------------- */
  function makePanel(cvW: number, cvH: number, worldW: number): Panel {
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
      depthTest: false,   // HUD 语义：永远读得到，不被角色/大屏遮挡
      side: THREE.DoubleSide
    });
    const mesh = new THREE.Mesh(new THREE.PlaneGeometry(worldW, worldW * cvH / cvW), mat);
    mesh.renderOrder = 900;
    mesh.visible = false;
    mesh.raycast = () => {}; // 不参与射线命中，避免挡住手柄戳角色
    return { cv, ctx, tex, mat, mesh };
  }

  function ensure() {
    if (toastP && subP) return;
    if (!App.scene) return;
    toastP = makePanel(TOAST_CV.w, TOAST_CV.h, TOAST_WORLD_W);
    subP = makePanel(SUB_CV.w, SUB_CV.h, SUB_WORLD_W);
    App.scene.add(toastP.mesh);
    App.scene.add(subP.mesh);
    ui.toastMesh = toastP.mesh;
    ui.subMesh = subP.mesh;
  }

  /* ---------------- 绘制 ---------------- */
  function drawToast() {
    if (!toastP) return;
    const { ctx, tex } = toastP;
    const W = TOAST_CV.w, H = TOAST_CV.h;
    ctx.clearRect(0, 0, W, H);
    const font = '28px ' + FONT;            // CSS 14px × 2 密度
    const txt = fitOne(ctx, toastText, W - 140, font);
    ctx.font = font;
    const bw = Math.min(W - 24, ctx.measureText(txt).width + 96); // padding 24px×2
    const bh = 96;
    const x = (W - bw) / 2, y = (H - bh) / 2;
    roundRect(ctx, x, y, bw, bh, 24);       // border-radius 12 × 2
    ctx.fillStyle = 'rgba(0,0,0,0.85)';
    ctx.fill();
    ctx.fillStyle = '#ffffff';
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.fillText(txt, W / 2, H / 2 + 1);
    ctx.textAlign = 'left';
    ctx.textBaseline = 'alphabetic';
    tex.needsUpdate = true;
  }

  function drawSub() {
    if (!subP) return;
    const { ctx, tex } = subP;
    const W = SUB_CV.w, H = SUB_CV.h;
    ctx.clearRect(0, 0, W, H);
    const font = '30px ' + FONT;            // CSS 15px × 2 密度
    ctx.font = font;
    const lines = wrapLines(ctx, subText, W - 80, 3);
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.shadowColor = 'rgba(0,0,0,0.8)';
    ctx.shadowBlur = 16;                    // text-shadow 8px × 2
    ctx.shadowOffsetY = 4;                  // 2px × 2
    ctx.fillStyle = '#f0f0ff';              // var(--text)
    const lh = 45;                          // 15px × line-height 1.5 × 2
    const y0 = H / 2 - ((lines.length - 1) * lh) / 2;
    for (let i = 0; i < lines.length; i++) ctx.fillText(lines[i], W / 2, y0 + i * lh);
    ctx.shadowColor = 'transparent';
    ctx.shadowBlur = 0;
    ctx.shadowOffsetY = 0;
    ctx.textAlign = 'left';
    ctx.textBaseline = 'alphabetic';
    tex.needsUpdate = true;
  }

  /* ---------------- 显示/隐藏 ---------------- */
  ui.show = function show() {
    ensure();
    ui.active = true;
    toastText = ''; toastUntil = 0; toastDrawn = '';
    subText = ''; subDrawn = ''; subOp = 0;
    smInit = false;
    if (toastP) { toastP.mesh.visible = false; toastP.mat.opacity = 0; }
    if (subP) { subP.mesh.visible = false; subP.mat.opacity = 0; }
  };

  ui.hide = function hide() {
    ui.active = false;
    if (toastP) toastP.mesh.visible = false;
    if (subP) subP.mesh.visible = false;
  };

  /* ---------------- 每帧：定位 + 淡入淡出 ---------------- */
  ui.update = function update(dt: number) {
    if (!ui.active) return;
    const hp = App._xrHeadPos;
    if (!hp) return;
    ensure();
    if (!toastP || !subP) return;

    // 头显水平前向（pitch 归零：低头/抬头不把面板拽进地面，只随 yaw 转）
    let hx = 0, hz = -1;
    const r = App.renderer;
    if (r && r.xr && r.xr.isPresenting) {
      try {
        const xrCam = r.xr.getCamera();
        xrCam.getWorldDirection(fwd);
        hx = fwd.x; hz = fwd.z;
      } catch (_) { /* 取不到就沿默认前向摆 */ }
    }
    let len = Math.hypot(hx, hz);
    if (len < 1e-3) { hx = 0; hz = -1; len = 1; }
    hx /= len; hz /= len;

    // yaw 阻尼跟随：转头时面板稍滞后 → 视差与空间感，而非贴在脸上
    const targetYaw = Math.atan2(hx, hz);
    if (!smInit) { smYaw = targetYaw; smInit = true; }
    else {
      let d = targetYaw - smYaw;
      while (d > Math.PI) d -= Math.PI * 2;
      while (d < -Math.PI) d += Math.PI * 2;
      smYaw += d * (1 - Math.exp(-dt * YAW_DAMP));
    }
    const px = hp.x + Math.sin(smYaw) * DIST;
    const pz = hp.z + Math.cos(smYaw) * DIST;
    head.set(hp.x, hp.y, hp.z);

    const now = performance.now() / 1000;

    // ---- toast：眼平线略上方（对应 CSS top:60px） ----
    const tm = toastP.mesh, tmat = toastP.mat;
    if (toastText && now < toastUntil + FADE) {
      if (toastDrawn !== toastText) { drawToast(); toastDrawn = toastText; }
      const inA = Math.min(1, (now - toastAt) / FADE);
      const outA = Math.min(1, (toastUntil + FADE - now) / FADE);
      tmat.opacity = Math.max(0, Math.min(inA, outA));
      tm.visible = true;
      tm.position.set(px, hp.y + 0.34, pz);
      tm.lookAt(head.x, head.y, head.z);
    } else if (tm.visible) {
      tm.visible = false;
      tmat.opacity = 0;
    }

    // ---- 字幕：眼平线略下方（对应 CSS bottom:16px） ----
    const sm = subP.mesh, smat = subP.mat;
    const wantSub = !!subText;
    if (wantSub && subDrawn !== subText) { drawSub(); subDrawn = subText; }
    subOp += ((wantSub ? 1 : 0) - subOp) * (1 - Math.exp(-dt * 10));
    if (!wantSub && subOp < 0.01) {
      subOp = 0;
      if (sm.visible) sm.visible = false;
    } else {
      smat.opacity = subOp;
      sm.visible = true;
      sm.position.set(px, hp.y - 0.42, pz);
      sm.lookAt(head.x, head.y, head.z);
    }
  };

  /* ---------------- 与非 VR 打通：包裹既有入口 ---------------- */
  // 1) toast / 字幕：VR 激活时同步到世界内面板（DOM 在 XR 会话里不可见）
  const _toast = App.showToast;
  App.showToast = function (text: string) {
    if (ui.active) {
      const t = String(text == null ? '' : text);
      if (t) {
        const now = performance.now() / 1000;
        toastText = t;
        toastAt = now;
        toastUntil = now + TOAST_MS / 1000;
      }
    }
    return _toast.apply(this, arguments as any);
  };

  const _sub = App.showSubtitle;
  App.showSubtitle = function (text: string) {
    if (ui.active) subText = String(text == null ? '' : text);
    return _sub.apply(this, arguments as any);
  };

  // 2) 进入/退出 VR 时显示/隐藏信息层
  const _enter = App.enterXrMode;
  App.enterXrMode = async function (...args: any[]) {
    const res = await _enter.apply(this, args);
    ui.show();
    return res;
  };
  const _exit = App.exitXrMode;
  App.exitXrMode = function (...args: any[]) {
    ui.hide();
    return _exit.apply(this, args);
  };

  App.updateVrUi = ui.update;
}
