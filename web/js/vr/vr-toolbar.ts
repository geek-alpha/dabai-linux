/* ============================================================
 * web/js/vr/vr-toolbar.ts —— VR 舞台工具栏（#stage-tools 的世界内重建）
 * ------------------------------------------------------------
 * 戴上头显后 body.vr-active 把 #stage-tools 整个 display:none（style.css:3435），
 * 而 WebXR 沉浸会话里浏览器不渲染 DOM —— 工具栏在 VR 里等于不存在，
 * 用户除了退出 VR 什么也点不了。本模块把那一列按钮在 VR 世界里重建。
 *
 * 「几乎一致」的做法是**不抄样式、直接读 DOM**：
 *   - 按钮清单、顺序、图标 path、active 态、fps 文本全部从 #stage-tools-ring
 *     实时读取 —— 权限（data-admin-only）、锁屏（body.locked 只留锁屏键）
 *     等既有可见性规则自动生效，不需要在这里重写一遍。
 *   - 图标用 Path2D 直接吃 DOM 里的 svg path 的 d，视觉与原按钮逐像素同源。
 *   - 配色/尺寸照 style.css 的 .stage-tool-btn：34px 圆、rgba(0,0,0,0.4) 底、
 *     1px rgba(255,255,255,0.12) 边、hover rgba(124,92,255,0.4) + accent 边、
 *     active rgba(124,92,255,0.5) + accent 边 + 白字。
 *     （画布按 2 倍密度绘制：直径 68px、边框 2px、图标 34px）
 *
 * 与屏幕版的唯一差别是空间形态，这是头显里不得不变的：
 *   1) 不做 4 个一屏的滚轮 —— VR 里面板高度不受屏幕限制，19 颗按钮一次排开，
 *      省掉翻页心智；
 *   2) 面板悬在头显右前方（前向偏右 30°、1.25m），yaw 带阻尼跟随，转头能追上来；
 *   3) 屏幕版靠鼠标 hover 出 tooltip，VR 里没有光标 —— 射线指到哪颗按钮，
 *      面板左侧那栏就显示它的 title（等价于屏幕版底部的 .stage-tools-label）。
 *
 * 交互接线复用 vr-hud 的模式：包裹 App._onControllerSelectStart / App._xrPadClick，
 * 先命中工具栏，未命中才回落到 vr-hud 面板、再回落到戳角色。
 * 本模块必须在 vr-hud 之后 init（外层包裹 = 优先命中）。
 *
 * 点击落到哪：能立即生效的直接代理 DOM 按钮 click()（重置视角/移动/帧率/
 * 沉浸/锁屏/VR），已在 VR 里有对应物的接过去（视频 → vr-hud 遥控）；
 * 仍是纯 DOM 弹窗的那几个（角色卡片/背景/音乐/会话历史…）面板在头显里
 * 看不见，点击后明确告知，不留「点了没反应」的黑洞。
 * ============================================================ */
import * as THREE from 'three';
import type { AppKernel, VrToolbar, VrToolbarItem, VrToolbarIcon } from '../types/app-kernel.js';
import { vrRayPanel } from './vr-ray.js';

export default function initVrToolbar(App: AppKernel) {
  if (App.vrToolbar) return; // 幂等：避免重复初始化/重复包裹

  const FONT = '"Microsoft YaHei", "PingFang SC", "Noto Sans SC", sans-serif';

  /* ---------------- 几何常量（CSS px × 2 密度） ---------------- */
  const CV_BTN = 68;            // .stage-tool-btn 34px × 2
  const CV_GAP = 8;             // 间隙 4px × 2
  const CV_PAD = 12;
  const LABEL_W = 260;          // 左侧 title 栏（对应屏幕版 .stage-tools-label）
  const CV_W = LABEL_W + CV_BTN + CV_PAD * 2;
  const LABEL_H = 44;
  const ICON_PX = 34;           // .stage-tool-btn svg 17px × 2
  const BTN_WORLD = 0.052;      // 按钮世界直径（米）—— 1.1m 处约 2.7° 视角（原 0.04m 在头显里太小）
  const S = BTN_WORLD / CV_BTN; // 画布像素 → 米
  const DIST = 0.78;            // 常态：贴手近场 0.78m（与 2m+ 处的视频遥控面板拉开深度差，一眼分得清）
  const YAW_OFF = Math.PI * 102 / 180; // 常态锚点方位：右后 102° —— 整条右侧 0~70° 让给视频遥控面板与正前方点播/音乐，侧边栏退到身后
  const DOCK_GAP = 0.16;        // 挂到点播面板右沿时的缝隙（米）：1.25m 处两块板子边缘差 2.6°（约 57px），不会互相挡
  const DOCK_SCALE = 1.2;       // 挂靠时放大一点：面板在 1.25m，比常态 0.78m 远 1.6 倍，不补的话按钮只剩 2.4°、头显里描不准（补到 2.9°；再大就顶出 FOV 右缘 —— 1.2 时外缘已到 46.6°）
  const REACH = 1.7;            // 走远超过这个水平距离才重挂到身边（一次跳变）
  const OFF_COS = 0.6;          // 用户绕到面板侧后方（夹角 >53°）才重挂，否则朝向锁死后只剩斜面板
  const MENU_DIST = 0.9;        // 居中菜单：面板拉到正前方 0.9m
  const MENU_SCALE = 1.35;      // 居中菜单：整块放大（视线对准即可，不必精确命中）
  const MENU_COLS = 2;          // 居中菜单：两列，20 颗按钮不拉成一根长条
  const GAZE_SNAP = 1.6;        // 视线吸附半径（× 按钮尺寸）：视线落在面板上即选中最近一颗
  const GAZE_SNAP_MENU = 2.6;   // 居中菜单里放宽（按钮更大、更靠中心）
  const YAW_DAMP = 10;          // 居中菜单的 yaw 跟随阻尼：比信息层跟得紧，否则按钮会晃出手柄
  const HOVER_SLOP = 6;         // 命中放宽（画布像素），VR 里手抖

  /* 仍是纯 DOM 弹窗的按钮：VR 里点了会开一个看不见的面板，明确告知 */
  const DOM_ONLY: Record<string, string> = {
    'role-card-btn': '角色卡片',
    'llm-provider-btn': '模型供应商',
    'bg-btn': '背景场景',
    'fpv-btn': '机位调整',
    'cam-settings-btn': '相机设置',
    'workspace-btn': '工作区',
    'session-btn': '对话历史'
  };

  const tb = App.vrToolbar = {
    active: false,
    collapsed: false,          // VR 里默认展开（屏幕版默认收缩，头显里收起来就没入口了）
    mesh: null,
    tex: null,
    cv: null,
    ctx: null,
    items: [],
    hoverId: '',
    focusId: '',
    menuOpen: false,
    flash: null,
    _ray: new THREE.Raycaster(),
    _v3: new THREE.Vector3(),
    _q: new THREE.Quaternion(),
    show: () => {},
    hide: () => {},
    markDirty: () => {},
    update: () => {},
    hitTest: () => null,
    trigger: () => {},
    toggleMenu: () => {}
  } as VrToolbar;

  let anchor: THREE.Vector3 | null = null; // 常态世界锚点（进场首帧定一次，位置与朝向都不再跟头）
  // 软锚定参数：钉在世界里不随头部噪声抖，走动/升降偏出死区就按限速缓缓追上
  const FOLLOW_DEAD = 0.45;                 // 死区（米）：小于它面板纹丝不动
  const FOLLOW_GAIN = 1.4;                  // 跟随增益：偏得越多追得越快
  const FOLLOW_MAX = 2.2;                   // 跟随限速（米/秒）
  let fdirX = 0, fdirZ = -1;                // 锚定时的水平方向（软跟随基准）
  const idealPos = new THREE.Vector3();     // 复用的理想位置（VR 每帧禁止 new）
  const dockPos = new THREE.Vector3();      // 挂靠点播面板右沿时的理想位置（同上，禁止 new）
  const lockDir = new THREE.Vector3(0, 0, -1); // 锚定瞬间的面板前向（世界系）：判断用户是否绕到侧后方
  let smYaw = 0;
  let smInit = false;
  let dirty = true;
  let sigAt = 0;
  let lastSig = '';
  const fwd = new THREE.Vector3();
  const head = new THREE.Vector3();

  /* ---------------- 从 DOM 采集按钮（不抄样式，直接读现状） ---------------- */
  // 只按按钮自身的 computed display 过滤：
  //   - 父容器 #stage-tools 在 VR 里是 display:none，但 computed display 不受祖先影响，
  //     子按钮仍是 flex —— 若按 offsetWidth/visibility 判断会误判成全空；
  //   - body.locked 的 .wheel-track > *:not(#lock-mode-btn){display:none!important}
  //     打在按钮自己身上，能被读到。
  function readIcon(btn: HTMLElement): { icons: VrToolbarIcon[]; vw: number; vh: number } {
    const icons: VrToolbarIcon[] = [];
    let vw = 24, vh = 24;
    btn.querySelectorAll('svg').forEach(svg => {
      const vb = (svg.getAttribute('viewBox') || '0 0 24 24').trim().split(/[\s,]+/).map(Number);
      if (vb.length === 4 && vb[2] > 0 && vb[3] > 0) { vw = vb[2]; vh = vb[3]; }
      svg.querySelectorAll('path').forEach(p => {
        const d = p.getAttribute('d');
        if (!d) return;
        const fillAttr = p.getAttribute('fill');
        const strokeAttr = p.getAttribute('stroke');
        // 描边型图标（stage-tools-toggle 的三横线/叉）：fill=none + stroke=currentColor
        const mode: 'fill' | 'stroke' =
          (strokeAttr && strokeAttr !== 'none' && (!fillAttr || fillAttr === 'none')) ? 'stroke' : 'fill';
        const m = /translate\(\s*(-?[\d.]+)[\s,]+(-?[\d.]+)/.exec(p.getAttribute('transform') || '');
        icons.push({
          path: new Path2D(d),
          mode,
          sw: parseFloat(p.getAttribute('stroke-width') || '1.8') || 1.8,
          tx: m ? Number(m[1]) : 0,
          ty: m ? Number(m[2]) : 0
        });
      });
    });
    return { icons, vw, vh };
  }

  type Raw = { id: string; title: string; icons: VrToolbarIcon[]; vw: number; vh: number; text: string; active: boolean };

  function readDom(): Raw[] {
    const ring = document.getElementById('stage-tools-ring');
    if (!ring) return [];
    const out: Raw[] = [];
    ring.querySelectorAll<HTMLButtonElement>('.stage-tool-btn').forEach(b => {
      if (!b.id) return;
      if (getComputedStyle(b).display === 'none') return;   // 权限隐藏 / 锁屏只留锁屏键
      const labelEl = b.querySelector('.fps-label');
      const { icons, vw, vh } = readIcon(b);
      out.push({
        id: b.id,
        title: b.getAttribute('title') || b.id,
        icons,
        vw,
        vh,
        // 文本型按钮（#fps-btn 的 #fps-label 显示 60/30/20）
        text: labelEl ? (labelEl.textContent || '').trim() : '',
        active: b.classList.contains('active')
      });
    });
    return out;
  }

  // 状态签名：按钮集合 + 顺序 + active + fps 文本 + 锁屏。变了才重建（0.25s 轮询）
  function signature(list: Raw[]): string {
    return list.map(r => r.id + (r.active ? '*' : '') + ':' + r.text).join('|') +
      '|' + (document.body.classList.contains('locked') ? 'L' : '');
  }

  /* ---------------- 布局：按钮表 + 画布尺寸 ---------------- */
  const TOGGLE: Raw = {
    id: '__toggle',
    title: '收起工具栏',
    icons: [],
    vw: 24,
    vh: 24,
    text: '',
    active: false
  };

  // 常驻第二颗：居中/归位。侧边面板在头显里够不着时，先把它叫到眼前再操作
  const MENU: Raw = {
    id: '__menu',
    title: '面板移到正前方',
    icons: [],
    vw: 24,
    vh: 24,
    text: '居中',
    active: false
  };

  function layout(list: Raw[]) {
    // 首颗 = 收起/展开（对应屏幕版的 #stage-tools-toggle），锁屏时不给（只留锁屏键）；
    // 第二颗 = 居中/归位（头显里把面板叫到眼前的操作入口）
    const locked = document.body.classList.contains('locked');
    const rows: Raw[] = locked ? list : (tb.collapsed ? [TOGGLE] : [TOGGLE, MENU].concat(list));
    const cols = (tb.menuOpen && !tb.collapsed && !locked) ? MENU_COLS : 1;
    const perCol = Math.max(1, Math.ceil(rows.length / cols));
    const cvW = LABEL_W + cols * CV_BTN + (cols - 1) * CV_GAP + CV_PAD * 2;
    const cvH = CV_PAD * 2 + LABEL_H + perCol * CV_BTN + (perCol - 1) * CV_GAP;
    const cv = tb.cv!;
    if (cv.width !== cvW || cv.height !== cvH) {
      cv.width = cvW;
      cv.height = cvH;
      if (tb.mesh) {
        (tb.mesh.geometry as THREE.PlaneGeometry).dispose();
        tb.mesh.geometry = new THREE.PlaneGeometry(cvW * S, cvH * S);
      }
    }
    tb.items = rows.map((r, i) => {
      const col = Math.floor(i / perCol);
      const row = i - col * perCol;
      return {
        id: r.id,
        title: r.id === '__toggle' ? (tb.collapsed ? '展开工具栏' : '收起工具栏')
          : r.id === '__menu' ? (tb.menuOpen ? '面板归位到侧边' : '面板移到正前方')
            : r.title,
        icons: r.icons,
        vw: r.vw,
        vh: r.vh,
        text: r.id === '__menu' ? (tb.menuOpen ? '侧边' : '居中') : r.text,
        active: r.id === '__menu' ? tb.menuOpen : r.active,
        x: LABEL_W + CV_PAD + col * (CV_BTN + CV_GAP),
        y: CV_PAD + row * (CV_BTN + CV_GAP),
        w: CV_BTN,
        h: CV_BTN
      } as VrToolbarItem;
    });
  }

  /* ---------------- 场景构建 ---------------- */
  function ensureScene() {
    if (tb.mesh) return;
    const cv = document.createElement('canvas');
    cv.width = CV_W;
    cv.height = CV_PAD * 2 + LABEL_H + CV_BTN;
    tb.cv = cv;
    tb.ctx = cv.getContext('2d');

    const tex = new THREE.CanvasTexture(cv);
    tex.colorSpace = THREE.SRGBColorSpace;
    tex.minFilter = THREE.LinearFilter;
    tb.tex = tex;

    const mat = new THREE.MeshBasicMaterial({
      map: tex,
      transparent: true,
      depthWrite: false,
      side: THREE.DoubleSide,
      polygonOffset: true,
      polygonOffsetFactor: -4
    });
    const mesh = new THREE.Mesh(new THREE.PlaneGeometry(cv.width * S, cv.height * S), mat);
    mesh.renderOrder = 700;   // 压在 vr-hud(500) 之上、vr-ui 信息层(900) 之下
    mesh.visible = false;
    App.scene!.add(mesh);
    tb.mesh = mesh;
    rebuild();
  }

  function rebuild() {
    layout(readDom());
    dirty = true;
  }

  /* ---------------- 绘制 ---------------- */
  function drawBtn(ctx: CanvasRenderingContext2D, it: VrToolbarItem, now: number) {
    const cx = it.x + it.w / 2;
    const cy = it.y + it.h / 2;
    const hovered = tb.hoverId === it.id;
    const flashed = !!(tb.flash && tb.flash.id === it.id && now < tb.flash.until);
    const hot = hovered || flashed;
    // hover 放大 1.08（对应屏幕版 .wheel-front 的 scale(1.15)，VR 里收敛一点防误触）
    const r = (it.w / 2 - 1) * (hovered ? 1.08 : 1);

    ctx.beginPath();
    ctx.arc(cx, cy, r, 0, Math.PI * 2);
    ctx.fillStyle = flashed ? 'rgba(124,92,255,0.62)'
      : it.active ? 'rgba(124,92,255,0.5)'
        : hovered ? 'rgba(124,92,255,0.4)'
          : 'rgba(0,0,0,0.4)';
    ctx.fill();
    ctx.lineWidth = 2;   // border 1px × 2
    ctx.strokeStyle = (flashed || it.active || hovered) ? '#7c5cff' : 'rgba(255,255,255,0.12)';
    ctx.stroke();

    const col = (flashed || it.active || hovered) ? '#ffffff' : '#f0f0ff';
    if (it.text) {
      ctx.font = 'bold 26px ' + FONT;
      ctx.fillStyle = col;
      ctx.textAlign = 'center';
      ctx.textBaseline = 'middle';
      ctx.fillText(it.text, cx, cy + 1);
      ctx.textAlign = 'left';
      ctx.textBaseline = 'alphabetic';
    } else {
      const k = ICON_PX / it.vw;
      ctx.save();
      ctx.translate(cx, cy);
      ctx.scale(k, k);
      ctx.translate(-it.vw / 2, -it.vh / 2);
      for (const ic of it.icons) {
        ctx.save();
        ctx.translate(ic.tx, ic.ty);
        if (ic.mode === 'stroke') {
          ctx.lineWidth = ic.sw;
          ctx.lineJoin = 'round';
          ctx.lineCap = 'round';
          ctx.strokeStyle = col;
          ctx.stroke(ic.path);
        } else {
          ctx.fillStyle = col;
          ctx.fill(ic.path);
        }
        ctx.restore();
      }
      ctx.restore();
    }

    // 视线焦点环：只有确认键的设备靠它知道「按下去会点谁」（呼吸亮环，射线 hover 之外单独一层）
    if (tb.focusId === it.id) {
      const pulse = 0.5 + 0.5 * Math.sin(now / 260);
      ctx.beginPath();
      ctx.arc(cx, cy, r + 7 + pulse * 5, 0, Math.PI * 2);
      ctx.lineWidth = 5;
      ctx.strokeStyle = 'rgba(110,240,255,' + (0.5 + 0.4 * pulse).toFixed(2) + ')';
      ctx.stroke();
    }
  }

  // 收起态的 toggle 图标（屏幕版用 svg，收起态给的是叉、展开态是三横线）
  function drawToggleIcon(ctx: CanvasRenderingContext2D, it: VrToolbarItem, now: number) {
    if (it.icons.length) { drawBtn(ctx, it, now); return; }
    // 兜底：DOM 没给图标时手绘三横线 / 叉
    const cx = it.x + it.w / 2, cy = it.y + it.h / 2;
    drawBtn(ctx, it, now);
    ctx.strokeStyle = '#f0f0ff';
    ctx.lineWidth = 4;
    ctx.lineCap = 'round';
    ctx.beginPath();
    if (tb.collapsed) {
      ctx.moveTo(cx - 14, cy - 14); ctx.lineTo(cx + 14, cy + 14);
      ctx.moveTo(cx + 14, cy - 14); ctx.lineTo(cx - 14, cy + 14);
    } else {
      ctx.moveTo(cx - 15, cy - 9); ctx.lineTo(cx + 15, cy - 9);
      ctx.moveTo(cx - 15, cy); ctx.lineTo(cx + 15, cy);
      ctx.moveTo(cx - 15, cy + 9); ctx.lineTo(cx + 15, cy + 9);
    }
    ctx.stroke();
  }

  function roundRect(ctx: CanvasRenderingContext2D, x: number, y: number, w: number, h: number, r: number) {
    ctx.beginPath();
    ctx.moveTo(x + r, y);
    ctx.arcTo(x + w, y, x + w, y + h, r);
    ctx.arcTo(x + w, y + h, x, y + h, r);
    ctx.arcTo(x, y + h, x, y, r);
    ctx.arcTo(x, y, x + w, y, r);
    ctx.closePath();
  }

  function draw() {
    if (!tb.ctx || !tb.cv) return;
    const ctx = tb.ctx;
    const W = tb.cv.width, H = tb.cv.height;
    ctx.clearRect(0, 0, W, H);
    const now = performance.now();

    for (const it of tb.items) {
      if (it.id === '__toggle') drawToggleIcon(ctx, it, now);
      else drawBtn(ctx, it, now);
    }

    // 左侧 title 栏（等价屏幕版底部的 .stage-tools-label）：显示射线/视线指着的按钮名
    const hit = tb.items.find(i => i.id === (tb.focusId || tb.hoverId));
    const label = hit ? hit.title : (tb.menuOpen ? '视线对准按钮 → 按确认键' : '工具');
    ctx.font = (tb.menuOpen && !hit ? '20px ' : '24px ') + FONT;
    ctx.textAlign = 'right';
    ctx.textBaseline = 'middle';
    const ty = CV_PAD + LABEL_H / 2 + 4;
    const tw = ctx.measureText(label).width + 32;
    const bx = Math.max(4, LABEL_W - tw);
    roundRect(ctx, bx, CV_PAD + 4, LABEL_W - bx, LABEL_H - 8, 12);
    ctx.fillStyle = hit ? 'rgba(124,92,255,0.35)' : 'rgba(0,0,0,0.5)';
    ctx.fill();
    ctx.lineWidth = 2;
    ctx.strokeStyle = hit ? '#7c5cff' : 'rgba(124,92,255,0.3)';
    ctx.stroke();
    ctx.fillStyle = 'rgba(255,255,255,0.92)';
    ctx.fillText(label, LABEL_W - 16, ty);
    ctx.textAlign = 'left';
    ctx.textBaseline = 'alphabetic';

    if (tb.tex) tb.tex.needsUpdate = true;
  }

  /* ---------------- 对外：显示/隐藏/更新 ---------------- */
  tb.show = function show() {
    ensureScene();
    tb.active = true;
    tb.collapsed = false;
    tb.hoverId = '';
    tb.focusId = '';
    tb.menuOpen = false;
    tb.flash = null;
    smInit = false;
    anchor = null;   // 每次进场重新定摆位（用户可能站在别处）
    lastSig = '';
    rebuild();
    if (tb.mesh) tb.mesh.visible = true;
  };

  tb.hide = function hide() {
    tb.active = false;
    tb.hoverId = '';
    tb.focusId = '';
    anchor = null;
    if (tb.mesh) tb.mesh.visible = false;
  };

  tb.markDirty = function markDirty() { dirty = true; };

  /** 居中菜单：把面板从侧边叫到正前方（两列、放大），视线对准 + 确认键就能操作 */
  tb.toggleMenu = function toggleMenu() {
    tb.menuOpen = !tb.menuOpen;
    tb.focusId = '';
    rebuild();
    App.showToast(tb.menuOpen ? '面板已移到正前方 · 视线对准按钮按确认键' : '面板已归位侧边');
  };

  /* ---------------- 命中测试：射线 → 按钮 id ---------------- */
  /** 视线吸附：离交点最近的按钮（不限按钮矩形，半径内都算）—— 头显里视线不需要精确对准 */
  function nearestItem(px: number, py: number, snapR?: number): string {
    let best = '';
    let bestD = Infinity;
    for (const it of tb.items) {
      const dx = Math.max(it.x - px, 0, px - (it.x + it.w));
      const dy = Math.max(it.y - py, 0, py - (it.y + it.h));
      const d = Math.hypot(dx, dy);
      if (d < bestD) { bestD = d; best = it.id; }
    }
    const lim = snapR == null ? CV_BTN * (tb.menuOpen ? GAZE_SNAP_MENU : GAZE_SNAP) : snapR;
    return bestD <= lim ? best : '';
  }

  /** 命中口径：面板矩形 + 外扩磁力带（vr-ray 统一仲裁，跨面板互斥） */
  const probe = vrRayPanel({
    id: 'toolbar',
    active: () => !!(tb.active && tb.mesh && tb.mesh.visible && tb.cv),
    mesh: () => tb.mesh,
    cvW: () => (tb.cv ? tb.cv.width : 0),
    cvH: () => (tb.cv ? tb.cv.height : 0),
    hit: (px, py, loose, inside) => {
      for (const it of tb.items) {
        if (px >= it.x - HOVER_SLOP && px <= it.x + it.w + HOVER_SLOP &&
          py >= it.y - HOVER_SLOP && py <= it.y + it.h + HOVER_SLOP) return it.id;
      }
      if (!loose) return null;
      return nearestItem(px, py, inside ? undefined : 260) || null;
    },
    trigger: (id) => tb.trigger(id)
  });
  App.vrRay?.register(probe);

  /** 本面板锁定：全局仲裁里锁到的是我，就返回按钮 id（跨面板互斥） */
  function mine(o: THREE.Vector3, d: THREE.Vector3, loose: boolean): string {
    if (App.vrRay) return App.vrRay.pickMine('toolbar', o, d, loose);
    const r = probe.pick(o, d, loose);
    return r ? r.id : '';
  }

  tb.hitTest = function hitTest(origin: THREE.Vector3, dir: THREE.Vector3, snap?: boolean) {
    const r = probe.pick(origin, dir, !!snap);
    return r ? r.id : null;
  };

  /* ---------------- 命中动作 ---------------- */
  tb.trigger = function trigger(id: string) {
    if (!tb.active) return;
    tb.flash = { id, until: performance.now() + 260 };
    dirty = true;

    if (id === '__menu') {
      tb.toggleMenu();
      return;
    }
    if (id === '__toggle') {
      tb.collapsed = !tb.collapsed;
      rebuild();
      return;
    }
    // gyro-btn 在屏幕版是「进入 VR」；VR 里它就是出口（同一位置同一图标）
    if (id === 'gyro-btn') {
      if (App.exitXrMode) App.exitXrMode();
      return;
    }
    // 屏幕版 immerse-btn = 隐藏所有按钮；VR 里等价于收起这一列
    if (id === 'immerse-btn') {
      tb.collapsed = true;
      rebuild();
      App.showToast('工具栏已收起 · 点左上 ☰ 再展开');
      return;
    }
    // 在线视频：VR 里已有对应物（vr-video 面板：搜索/热门/收藏/历史），不要走 DOM
    if (id === 'video-btn') {
      if (App.vrVideo) {
        // 点播与音乐都占正前方 ±26° 扇区（1.45m / 1.25m），同时开必然完全重叠 → 互斥
        if (!App.vrVideo.open && App.vrMusic && App.vrMusic.open) App.vrMusic.toggle();
        App.vrVideo.toggle();
      } else if (App.vrHud) App.vrHud.trigger('toggleVideo');
      else App.showToast('视频面板尚未就绪');
      return;
    }
    // 在线音乐：VR 里已有对应物（vr-music 面板），不要走 DOM
    if (id === 'music-btn') {
      if (App.vrMusic) {
        if (!App.vrMusic.open && App.vrVideo && App.vrVideo.open) App.vrVideo.toggle();
        App.vrMusic.toggle();
      } else App.showToast('音乐面板尚未就绪');
      return;
    }
    const el = document.getElementById(id);
    if (el) el.click();
    if (DOM_ONLY[id]) {
      App.showToast(DOM_ONLY[id] + '面板还没在 VR 里重建 · 先退出 VR 操作');
    }
  };

  /* ---------------- 手柄/射线：hover 高亮 ---------------- */
  const _o = new THREE.Vector3();
  const _d = new THREE.Vector3();
  function rayOf(c: any): boolean {
    return !!(App._xrCtrlRay && App._xrCtrlRay(c, _o, _d, tb._q));
  }

  function updateHover() {
    let id = '';
    const ctrls = App._xrControllers || [];
    for (let i = 0; i < ctrls.length; i++) {
      if (!rayOf(ctrls[i])) continue;
      const h = mine(_o, _d, true);
      if (h) { id = h; break; }
    }
    // 无 XR 控制器（手机 + 蓝牙手柄）：视中心射线当光标，否则手柄用户全程没有悬停反馈
    if (!id && !ctrls.length && App._readStdGamepad) {
      const pad = App._readStdGamepad();
      if (pad && pad.connected && App.renderer && App.renderer.xr && App.renderer.xr.isPresenting) {
        try {
          if (App._xrEyeRay(_o, _d)) {
            const h = mine(_o, _d, true);
            if (h) id = h;
          }
        } catch (_) {}
      }
    }
    if (id !== tb.hoverId) { tb.hoverId = id; dirty = true; }
  }

  /** 视线聚焦：头显射线落在面板上 → 选中最近的按钮（只有确认键的设备全靠这条） */
  function updateGaze() {
    let id = '';
    const r = App.renderer;
    if (r && r.xr && r.xr.isPresenting) {
      try {
        if (App._xrEyeRay(_o, _d)) id = mine(_o, _d, true);
      } catch (_) { /* 取不到就只留手柄射线 hover */ }
    }
    if (id !== tb.focusId) { tb.focusId = id; dirty = true; }
  }

  /* ---------------- 每帧：跟随定位 + 状态轮询 + 重绘 ---------------- */
  tb.update = function update(dt: number) {
    if (!tb.active || !tb.mesh) return;
    const hp = App._xrHeadPos;
    if (!hp) return;

    // 状态轮询（0.25s）：按钮集合/顺序/active/fps 文本/锁屏 变了就重建
    const now = performance.now();
    if (now - sigAt > 250) {
      sigAt = now;
      const list = readDom();
      const sig = signature(list) + '|' + (tb.collapsed ? 'C' : '');
      if (sig !== lastSig) { lastSig = sig; rebuild(); }
    }

    // 头显水平前向（pitch 归零：低头抬头不把面板拽进地面）
    let hx = 0, hz = -1;
    const r = App.renderer;
    if (r && r.xr && r.xr.isPresenting) {
      try {
        r.xr.getCamera().getWorldDirection(fwd);
        hx = fwd.x; hz = fwd.z;
      } catch (_) { /* 取不到就沿默认前向摆 */ }
    }
    let len = Math.hypot(hx, hz);
    if (len < 1e-3) { hx = 0; hz = -1; len = 1; }
    hx /= len; hz /= len;

    // 常态：世界固定 —— 进场首帧定一次锚点（用户→角色方向偏右 YAW_OFF），位置与朝向都不再动。
    // 位置跟头跑点不中，朝向每帧 lookAt 会让按钮随头部追踪噪声滑动，同样瞄不住。
    // 居中菜单：主动召唤，仍拉到视线正前方（此刻用户不动头，跟随才顺手）。
    // 点播面板开着 → 侧边栏挂它右沿（同平面、右侧留缝）：用户要「侧边栏在视频点播栏右边」。
    // 读它的位姿而不是自己算方位：面板挪到哪它跟到哪，两块板子永不重叠；面板收起就回常态锚点。
    const dock = (!tb.menuOpen && App.vrVideo && App.vrVideo.open &&
      App.vrVideo.mesh && App.vrVideo.mesh.visible) ? App.vrVideo.mesh : null;
    if (dock) {
      const gp = (dock.geometry as any).parameters || {};
      const vW = gp.width || 1.4;
      const myW = (tb.cv ? tb.cv.width : 0) * S * DOCK_SCALE;
      anchor = null;             // 回常态时重新定摆位，不继承挂靠位置
      tb.mesh.scale.setScalar(DOCK_SCALE);
      tb.mesh.quaternion.copy(dock.quaternion);
      dockPos.set(vW / 2 + DOCK_GAP + myW / 2, 0, 0)
        .applyQuaternion(dock.quaternion)
        .add(dock.position);
      tb.mesh.position.copy(dockPos);
    } else if (!tb.menuOpen) {
      if (anchor) {
        const tx = anchor.x - hp.x, tz = anchor.z - hp.z;
        const tl = Math.hypot(tx, tz) || 1;
        // 走远 / 绕到面板侧后方：重挂一次（跳变），否则只剩一块斜着的面板
        if (tl > REACH || (tx / tl) * lockDir.x + (tz / tl) * lockDir.z < OFF_COS) anchor = null;
      }
      if (!anchor) {
        const mc = App.modelGroup;
        let yaw = Math.atan2(hx, hz) - YAW_OFF;   // 兜底：头显前向偏右
        if (mc) {
          const dx = mc.position.x - hp.x;
          const dz = mc.position.z - hp.z;
          if (Math.hypot(dx, dz) > 0.3) yaw = Math.atan2(dx, dz) - YAW_OFF;
        }
        fdirX = Math.sin(yaw); fdirZ = Math.cos(yaw);
        anchor = new THREE.Vector3(hp.x + fdirX * DIST, hp.y + (App._xrEyeOffY || 0) - 0.08, hp.z + fdirZ * DIST);
        tb.mesh.position.copy(anchor);
        tb.mesh.lookAt(hp.x, hp.y - 0.08, hp.z);
        lockDir.set(0, 0, -1).applyQuaternion(tb.mesh.quaternion);
      }
      // 软锚定：死区内不动（不晃、按钮不滑），偏出死区按限速缓缓追上（走动/升降不丢面板）
      idealPos.set(hp.x + fdirX * DIST, hp.y + (App._xrEyeOffY || 0) - 0.08, hp.z + fdirZ * DIST);
      const dd = anchor.distanceTo(idealPos);
      if (dd > FOLLOW_DEAD) {
        const v = Math.min(dd * FOLLOW_GAIN, FOLLOW_MAX);
        anchor.lerp(idealPos, Math.min(1, (v * dt) / dd));
      }
      tb.mesh.position.copy(anchor);
      tb.mesh.scale.setScalar(1);
    } else {
      const targetYaw = Math.atan2(hx, hz);
      if (!smInit) { smYaw = targetYaw; smInit = true; }
      else {
        let d = targetYaw - smYaw;
        while (d > Math.PI) d -= Math.PI * 2;
        while (d < -Math.PI) d += Math.PI * 2;
        smYaw += d * (1 - Math.exp(-dt * YAW_DAMP));
      }
      const px = hp.x + Math.sin(smYaw) * MENU_DIST;
      const pz = hp.z + Math.cos(smYaw) * MENU_DIST;
      head.set(hp.x, hp.y, hp.z);
      tb.mesh.position.set(px, hp.y + (App._xrEyeOffY || 0) - 0.05, pz);
      tb.mesh.scale.setScalar(MENU_SCALE);
      tb.mesh.lookAt(head.x, head.y, head.z);
    }

    updateGaze();
    updateHover();
    if (dirty) { dirty = false; draw(); }
  };
  App.updateVrToolbar = tb.update;

  /* ---------------- 与非 VR 打通：包裹既有入口 ---------------- */
  // 1) 进入/退出 VR 时显示/隐藏（游戏模式的 VR 由游戏自理：沿用 vr-hud 的判定）
  const _enter = App.enterXrMode;
  App.enterXrMode = async function (...args: any[]) {
    const res = await _enter.apply(this, args);
    tb.show();
    return res;
  };
  const _exit = App.exitXrMode;
  App.exitXrMode = function (...args: any[]) {
    tb.hide();
    return _exit.apply(this, args);
  };

  // 2) XR 手柄扳机：先命中工具栏，未命中回落到 vr-hud 面板、再回落到戳角色
  const _selStart = App._onControllerSelectStart;
  App._onControllerSelectStart = function (e: any) {
    const c = e && e.target;
    if (tb.active) {
      // 视线焦点优先：确认键只管「我正在看的那颗」，射线打不准也能操作
      if (tb.focusId) { tb.trigger(tb.focusId); return; }
      if (c && rayOf(c)) {
        const id = mine(_o, _d, true);
        if (id) { tb.trigger(id); return; }
      }
    }
    return _selStart.apply(this, arguments);
  };

  // 3) 蓝牙手柄/兜底按键：视中心射线同样先命中工具栏
  const _padClick = App._xrPadClick;
  App._xrPadClick = function () {
    if (tb.active && !this._xrGameMode && this.xrPresenting && this.renderer && this.renderer.xr) {
      try {
        if (App._xrEyeRay(_o, _d)) {
          const id = mine(_o, _d, true);
          if (id) { tb.trigger(id); return; }
          if (tb.focusId) { tb.trigger(tb.focusId); return; }
        }
      } catch (_) {}
    }
    return _padClick.apply(this, arguments);
  };

  // 4) 帧率按钮点完 / 锁屏切换 → 下一轮轮询自会重建，这里只保证立刻重绘
  const _cycle = App.cyclePerfTier;
  if (_cycle) {
    App.cyclePerfTier = function () {
      const r = _cycle.apply(this, arguments);
      tb.markDirty();
      return r;
    };
  }
}
