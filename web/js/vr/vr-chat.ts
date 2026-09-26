/* ============================================================
 * web/js/vr/vr-chat.ts —— VR 对话大屏（#messages 的世界内镜像）
 * ------------------------------------------------------------
 * 戴上头显后 #chat-panel 随 DOM 一起消失（WebXR 沉浸会话不渲染页面），
 * 对话内容在 VR 里等于没有落点：只有 vr-ui 的 3 行字幕 + toast，
 * 用户看不到「我说过什么、她回过什么」。本模块把 #messages 镜像成
 * 角色身侧一块大屏。
 *
 * 空间形态（与 vr-hud 对称）：
 *   - vr-hud（状态 + 视频遥控）挂在角色右手侧，本模块挂左手侧，
 *     同一高度、同一朝向（永远正对用户头部），整场会话位置固定；
 *   - 面板 2.0m × 1.33m，摆在视线左前 55°、1.75m 处 —— 正视前方只扫得到
 *     边缘（不挡看角色），侧身/转头才正对它。屏幕要么在角色身后（被角色挡）、
 *     要么挡在脸前（看不到人），侧前方是唯一同时成立的位置。
 *   - 顶栏整条是收起/展开命中区（2.0m × 11cm，VR 里最好按的靶子），
 *     收起后变成一条细状态条，新消息在条上计数，不打扰看角色。
 *
 * 内容来源不抄 DOM 样式、直接读 DOM 文本（同 vr-toolbar 的做法）：
 *   - .msg.user → 「你」；.msg.ai / .msg.ai.turn → 角色名；
 *   - 回合气泡只取正文段 .turn-seg，思考行/工具块/状态行不进大屏；
 *   - 流式回复每 180ms 轮询一次签名，文本变长就重绘（90ms 节流）。
 * ============================================================ */
import * as THREE from 'three';
import type { AppKernel, VrChat } from '../types/app-kernel.js';
import { vrRayPanel } from './vr-ray.js';

export default function initVrChat(App: AppKernel) {
  if (App.vrChat) return; // 幂等

  const FONT = '"Microsoft YaHei", "PingFang SC", "Noto Sans SC", sans-serif';
  const CV_W = 1080;
  const CV_H = 720;
  const WORLD_W = 2.2;         // 面板世界宽（米）：2.2m 宽 / 1.75m 距离 ≈ 64° 视角
  const VIEW_DIST = 1.75;      // 面板到用户的距离（米）：34px 正文在此距离上约 2.3° 视角
  const AZI = Math.PI * 68 / 180; // 摆位方位：视线左前 68°——正视前方扫不到它，侧身才正对（让开视线）
  const REACH = 3.0;           // 走远到此距离才重挂到身边（一次跳变，不跟随）
  const OFF_COS = 0.6;         // 绕到面板侧后方（夹角 >53°）才重挂，否则朝向锁死后只剩斜面板
  const TOP_Y = 3.15;          // 面板顶边高度（米）：面板高 1.47m，底边落在 1.68m（角色头顶之上）才不与角色叠
  const HEAD_H = 66;           // 顶栏高度（画布像素）
  const PAD = 28;
  const LH = 56;               // 正文行高（34px × 1.65，对齐网页版 .msg 的 1.68）
  const LH_LABEL = 40;         // 说话人标签行高（27px）
  const BLOCK_GAP = 20;        // 消息间距
  // 字号比面板像素密度，不跟物理尺寸走：面板放大后字反而要收小，否则笔画又粗又满
  // （40px 在 2.0m 面板上是 2.4° 视角，读起来像标题不像正文）
  const F_BODY = '34px ' + FONT;    // 正文：头显里字小了根本读不动
  const F_LABEL = '27px ' + FONT;   // 说话人标签
  const F_HEAD = '30px ' + FONT;    // 顶栏 / 收起态状态条
  const BAR_W = 640;           // 收起态状态条（画布像素）
  const BAR_H = 120;
  // 大屏控制带（展开态顶栏之下）：屏幕多大、放多远 —— VR 里最常调的两项。
  // 挂在这块面板上是因为它是头显里唯一随时找得到的常驻 UI；摇杆只能改距离，改不了大小。
  const CTRL_H = 76;           // 控制带高
  const CTRL_TOP = HEAD_H + 8; // 顶栏之下 8px
  const CTRL_GAP = 10;
  const CTRL_N = 7;            // 大小 − / 值 / 大小 + / 距离 − / 值 / 距离 + / 复位
  const CTRL_IDS = ['cfg:s-', 'cfg:sv', 'cfg:s+', 'cfg:d-', 'cfg:dv', 'cfg:d+', 'cfg:r'];
  function ctrlCellW() { return (CV_W - PAD * 2 - CTRL_GAP * (CTRL_N - 1)) / CTRL_N; }
  const POLL_MS = 180;         // DOM 轮询间隔
  const REDRAW_MS = 90;        // 重绘节流
  const NAME_FALLBACK = '大白';
  /** 底部语音带高度（画布像素）：vr-voice 提供；它还没初始化时按默认值留位 */
  function bandH() { return (App.vrVoice && App.vrVoice.bandH) || 140; }

  const chat = App.vrChat = {
    active: false,
    collapsed: false,
    mesh: null,
    tex: null,
    cv: null,
    ctx: null,
    count: 0,
    focusId: '',
    dirty: true,
    _ray: new THREE.Raycaster(),
    _v3: new THREE.Vector3(),
    _q: new THREE.Quaternion(),
    show: () => {},
    hide: () => {},
    markDirty: () => {},
    update: () => {},
    hitTest: () => null,
    trigger: () => {}
  } as VrChat;

  /* ---------------- 闭包状态 ---------------- */
  let sig = '';                // DOM 内容签名（变了才重绘）
  let sigAt = 0;
  let lastDraw = 0;
  let countAtCollapse = 0;     // 收起那一刻的条数（收起期间新增多少条）
  let aiName = NAME_FALLBACK;
  let nameAt = 0;
  let anchor: THREE.Vector3 | null = null;   // 世界固定摆位锚点（进场首帧定一次，位置与朝向都不再跟头）
  let anchorOffY = 0;                         // 锚定时的视野升降偏移（大屏之后只跟这个差值上下走）
  const lockDir = new THREE.Vector3(0, 0, -1); // 锚定瞬间的面板前向（世界系）：判断用户是否绕到侧后方
  const tmpDir = new THREE.Vector3();
  let ctrlSig = '';            // 大屏大小/距离签名（变了才重绘控制带）

  type Line = { kind: 'user' | 'ai' | 'sys'; text: string };

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

  /** 按宽度折行（CJK 逐字），最多 maxLines 行，超出末行加省略号 */
  function wrapLines(ctx: CanvasRenderingContext2D, s: string, maxW: number, maxLines: number): string[] {
    const all: string[] = [];
    let line = '';
    const src = String(s == null ? '' : s);
    for (let i = 0; i < src.length; i++) {
      const ch = src[i];
      if (ch === '\n') { all.push(line); line = ''; continue; }
      const t = line + ch;
      if (line && ctx.measureText(t).width > maxW) { all.push(line); line = ch; }
      else line = t;
      if (all.length > maxLines) break;
    }
    if (line && all.length <= maxLines) all.push(line);
    if (all.length <= maxLines) return all;
    const keep = all.slice(0, maxLines);
    keep[maxLines - 1] = keep[maxLines - 1].slice(0, Math.max(1, keep[maxLines - 1].length - 1)) + '…';
    return keep;
  }

  /* ---------------- 读 DOM：说话人 + 文本 ---------------- */
  function readName() {
    const now = performance.now();
    if (now - nameAt < 5000) return aiName;
    nameAt = now;
    const el = document.querySelector('.chat-head-label');
    const n = el ? String(el.textContent || '').split('·')[0].trim() : '';
    aiName = n || NAME_FALLBACK;
    return aiName;
  }

  /** 单条消息的可见文本：回合气泡只取正文段（思考行/工具块/状态行不进大屏） */
  function msgTextOf(n: HTMLElement): string {
    if (n.classList.contains('turn')) {
      let out = '';
      n.querySelectorAll('.turn-seg').forEach(s => {
        const t = (s.textContent || '').trim();
        if (t) out += (out ? '\n' : '') + t;
      });
      return out;
    }
    const body = n.querySelector('.msg-body');
    return String(((body || n) as HTMLElement).textContent || '').trim();
  }

  function readMessages(): Line[] {
    const el = App.messagesEl;
    if (!el) return [];
    const kids = el.children;
    const out: Line[] = [];
    for (let i = Math.max(0, kids.length - 40); i < kids.length; i++) {
      const n = kids[i] as HTMLElement;
      if (!n.classList || !n.classList.contains('msg')) continue;
      if (n.classList.contains('typing')) { out.push({ kind: 'ai', text: '正在输入…' }); continue; }
      const kind: Line['kind'] = n.classList.contains('user') ? 'user'
        : (n.classList.contains('system') ? 'sys' : 'ai');
      let text = msgTextOf(n);
      if (n.querySelector('.msg-attach.imgs')) text = '［图片］' + text;
      if (n.querySelector('.msg-files')) text = '［文件］' + text;
      text = text.replace(/[ \t]+/g, ' ').replace(/\n{2,}/g, '\n').trim();
      if (!text) text = '［卡片］';
      if (text.length > 800) text = text.slice(0, 800) + '…';
      out.push({ kind, text });
    }
    return out;
  }

  /** 内容签名：条数 + 末 3 条的类名与文本长度（流式 token 到达就会变） */
  function domSig(el: HTMLElement): string {
    const kids = el.children;
    const n = kids.length;
    let s = String(n);
    for (let i = Math.max(0, n - 3); i < n; i++) {
      const k = kids[i] as HTMLElement;
      s += '|' + (k.className || '') + ':' + String((k.textContent || '').length);
    }
    return s;
  }

  /* ---------------- 场景对象 ---------------- */
  function ensure() {
    if (chat.mesh) return;
    if (!App.scene) return;
    const cv = document.createElement('canvas');
    cv.width = CV_W;
    cv.height = CV_H;
    const ctx = cv.getContext('2d')!;
    const tex = new THREE.CanvasTexture(cv);
    tex.colorSpace = THREE.SRGBColorSpace;
    tex.minFilter = THREE.LinearFilter;
    const mat = new THREE.MeshBasicMaterial({
      map: tex,
      transparent: true,
      depthWrite: false,
      side: THREE.DoubleSide,
      polygonOffset: true,
      polygonOffsetFactor: -4
    });
    const mesh = new THREE.Mesh(new THREE.PlaneGeometry(WORLD_W, WORLD_W * CV_H / CV_W), mat);
    mesh.renderOrder = 500;   // 与 vr-hud 同层：世界内面板，压在大屏之上、工具栏之下
    mesh.visible = false;
    App.scene.add(mesh);
    chat.cv = cv;
    chat.ctx = ctx;
    chat.tex = tex;
    chat.mesh = mesh;
    chat.dirty = true;
  }

  /* ---------------- 绘制 ---------------- */
  /** 大屏控制带：大小 − / 值 / 大小 + · 距离 − / 值 / 距离 + · 复位 */
  function drawCtrl() {
    const ctx = chat.ctx;
    if (!ctx) return;
    const st = App._vrBoardCtl ? App._vrBoardCtl.get() : { dist: 0, scale: 1 };
    const labels = [
      '大小 −', '×' + st.scale.toFixed(1), '大小 +',
      '距离 −', st.dist.toFixed(1) + 'm', '距离 +', '复位'
    ];
    const cw = ctrlCellW();
    for (let i = 0; i < CTRL_N; i++) {
      const id = CTRL_IDS[i];
      const show = id === 'cfg:sv' || id === 'cfg:dv';   // 数值格：只显示，点击无效
      const x = PAD + i * (cw + CTRL_GAP);
      roundRect(ctx, x, CTRL_TOP, cw, CTRL_H, 16);
      ctx.fillStyle = show ? 'rgba(110,240,255,0.12)' : 'rgba(124,92,255,0.20)';
      ctx.fill();
      ctx.lineWidth = 3;
      ctx.strokeStyle = chat.focusId === id ? 'rgba(110,240,255,0.95)' : 'rgba(124,92,255,0.45)';
      ctx.stroke();
      ctx.font = F_HEAD;
      ctx.textAlign = 'center';
      ctx.textBaseline = 'middle';
      ctx.fillStyle = show ? '#6ef0ff' : '#f0f0ff';
      ctx.fillText(fitOne(ctx, labels[i], cw - 16, F_HEAD), x + cw / 2, CTRL_TOP + CTRL_H / 2 + 2);
    }
    ctx.textAlign = 'left';
    ctx.textBaseline = 'alphabetic';
  }

  function drawExpanded() {
    const ctx = chat.ctx;
    if (!ctx) return;
    ctx.clearRect(0, 0, CV_W, CV_H);

    roundRect(ctx, 2, 2, CV_W - 4, CV_H - 4, 30);
    ctx.fillStyle = 'rgba(8,10,22,0.66)';
    ctx.fill();
    ctx.lineWidth = 3;
    ctx.strokeStyle = 'rgba(124,92,255,0.42)';
    ctx.stroke();

    // 顶栏：整条可点（收起），画成可按的样子
    ctx.save();
    roundRect(ctx, 2, 2, CV_W - 4, HEAD_H, 30);
    ctx.clip();
    ctx.fillStyle = 'rgba(124,92,255,0.18)';
    ctx.fillRect(2, 2, CV_W - 4, HEAD_H);
    ctx.restore();
    ctx.font = F_HEAD;
    ctx.textBaseline = 'middle';
    ctx.textAlign = 'left';
    ctx.fillStyle = '#f0f0ff';
    ctx.fillText('对话 · ' + chat.count + ' 条', PAD, HEAD_H / 2 + 2);
    ctx.textAlign = 'right';
    ctx.fillStyle = 'rgba(240,240,255,0.7)';
    ctx.fillText('收起 ▾', CV_W - PAD, HEAD_H / 2 + 2);

    // 视线焦点环：看着屏幕按确认键 = 收起（只有确认键的设备全靠这条）
    if (chat.focusId === 'toggle') {
      const pulse = 0.5 + 0.5 * Math.sin(performance.now() / 260);
      roundRect(ctx, 2, 2, CV_W - 4, HEAD_H, 30);
      ctx.lineWidth = 5;
      ctx.strokeStyle = 'rgba(110,240,255,' + (0.5 + 0.4 * pulse).toFixed(2) + ')';
      ctx.stroke();
    }

    drawCtrl();

    // 消息：从最新往回排，装满内容区为止（自动跟最新，旧消息自然溢出）
    const areaTop = CTRL_TOP + CTRL_H + 14;
    const areaBot = CV_H - bandH() - PAD;   // 底部整条留给语音带
    const areaH = areaBot - areaTop;
    const lines = readMessages();
    ctx.font = F_BODY;
    type Block = { label: string; labelColor: string; color: string; rows: string[]; stream: boolean };
    const blocks: Block[] = [];
    let used = 0;
    for (let i = lines.length - 1; i >= 0; i--) {
      const m = lines[i];
      const rows = wrapLines(ctx, m.text, CV_W - PAD * 2 - 10, 6);
      const bh = LH_LABEL + rows.length * LH + BLOCK_GAP;
      if (used + bh > areaH && blocks.length) break;
      blocks.unshift({
        label: m.kind === 'user' ? '你' : (m.kind === 'sys' ? '系统' : aiName),
        labelColor: m.kind === 'user' ? '#a78bfa' : (m.kind === 'sys' ? '#8c8ca0' : '#6ef0ff'),
        color: m.kind === 'user' ? '#ffffff' : (m.kind === 'sys' ? '#a0a0b8' : '#e9e9f5'),
        rows,
        stream: m.kind === 'ai' && i === lines.length - 1 && (lastState === 'thinking' || lastState === 'speaking')
      });
      used += bh;
    }
    if (!blocks.length) {
      ctx.font = F_BODY;
      ctx.textAlign = 'center';
      ctx.fillStyle = 'rgba(240,240,255,0.45)';
      ctx.fillText('还没有对话 —— 说吧，我听着', CV_W / 2, CV_H / 2 + 20);
      ctx.textAlign = 'left';
      ctx.textBaseline = 'alphabetic';
      if (chat.tex) chat.tex.needsUpdate = true;
      return;
    }

    let y = areaBot - used;
    let curX = 0, curY = 0;
    for (let bi = 0; bi < blocks.length; bi++) {
      const b = blocks[bi];
      ctx.textAlign = 'left';
      ctx.textBaseline = 'middle';
      ctx.font = F_LABEL;
      ctx.fillStyle = b.labelColor;
      ctx.fillText(b.label, PAD, y + LH_LABEL / 2);
      y += LH_LABEL;
      ctx.font = F_BODY;
      ctx.fillStyle = b.color;
      for (let i = 0; i < b.rows.length; i++) {
        ctx.fillText(b.rows[i], PAD, y + LH / 2);
        if (b.stream && i === b.rows.length - 1) {
          curX = PAD + ctx.measureText(b.rows[i]).width + 6;
          curY = y + LH / 2;
        }
        y += LH;
      }
      y += BLOCK_GAP;
    }
    // 流式光标（正在生成回复：末尾闪烁竖条，等价屏幕版 .msg.ai.streaming::after）
    if (curX > 0) {
      ctx.fillStyle = '#7c5cff';
      ctx.fillRect(curX, curY - 16, 7, 32);
    }
    ctx.textAlign = 'left';
    ctx.textBaseline = 'alphabetic';
    if (chat.tex) chat.tex.needsUpdate = true;
  }

  function drawBar() {
    const ctx = chat.ctx;
    if (!ctx) return;
    ctx.clearRect(0, 0, CV_W, CV_H);
    const x = (CV_W - BAR_W) / 2;
    const y = (CV_H - BAR_H) / 2;
    roundRect(ctx, x, y, BAR_W, BAR_H, 30);
    ctx.fillStyle = 'rgba(8,10,22,0.72)';
    ctx.fill();
    ctx.lineWidth = 3;
    ctx.strokeStyle = 'rgba(124,92,255,0.5)';
    ctx.stroke();
    const fresh = Math.max(0, chat.count - countAtCollapse);
    ctx.font = F_HEAD;
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.fillStyle = fresh > 0 ? '#a78bfa' : '#f0f0ff';
    const txt = '对话 · ' + chat.count + ' 条' + (fresh > 0 ? ' · 新 ' + fresh + ' 条' : ' · 点开');
    ctx.fillText(fitOne(ctx, txt, BAR_W - 60, F_HEAD), x + BAR_W / 2, y + BAR_H / 2 + 2);
    if (chat.focusId === 'toggle') {
      const pulse = 0.5 + 0.5 * Math.sin(performance.now() / 260);
      roundRect(ctx, x - 6 - pulse * 4, y - 6 - pulse * 4, BAR_W + 12 + pulse * 8, BAR_H + 12 + pulse * 8, 34);
      ctx.lineWidth = 5;
      ctx.strokeStyle = 'rgba(110,240,255,' + (0.5 + 0.4 * pulse).toFixed(2) + ')';
      ctx.stroke();
    }
    ctx.textAlign = 'left';
    ctx.textBaseline = 'alphabetic';
    if (chat.tex) chat.tex.needsUpdate = true;
  }

  function draw() {
    if (chat.collapsed) { drawBar(); return; }
    drawExpanded();
    // 语音带贴在底部：与对话同一块面板，上面读、下面说
    const vvo = App.vrVoice;
    if (vvo && vvo.active && chat.ctx) vvo.drawBand(chat.ctx, CV_H - bandH());
  }

  /* ---------------- 显示/隐藏/每帧 ---------------- */
  chat.show = function show() {
    ensure();
    chat.active = true;
    chat.collapsed = false;
    chat.focusId = '';
    anchor = null;   // 每次进场重新定摆位（用户可能站在别处）
    chat.dirty = true;
    sig = '';
    sigAt = 0;
    countAtCollapse = 0;
    if (chat.mesh) chat.mesh.visible = true;
    if (App.vrVoice) App.vrVoice.show();
  };

  chat.hide = function hide() {
    chat.active = false;
    chat.focusId = '';
    anchor = null;
    if (chat.mesh) chat.mesh.visible = false;
    if (App.vrVoice) App.vrVoice.hide();
  };

  chat.markDirty = function markDirty() { chat.dirty = true; };

  chat.update = function update(dt: number) {
    if (!chat.active || !chat.mesh) return;
    const hp = App._xrHeadPos;
    if (!hp) return;
    const now = performance.now();

    const worldH = WORLD_W * CV_H / CV_W;

    // 锚点：进场首帧定一次 —— 从「用户→角色」方向再往用户左边转 55°、1.75m 处。
    // 位置与朝向都世界固定：跟头转的屏幕一转头就追着跑，读不成句，也点不中。
    if (anchor) {
      const tx = anchor.x - hp.x, tz = anchor.z - hp.z;
      const tl = Math.hypot(tx, tz) || 1;
      // 走远 / 绕到面板侧后方：重挂一次（跳变），否则只剩一块斜着的大屏
      if (tl > REACH || (tx / tl) * lockDir.x + (tz / tl) * lockDir.z < OFF_COS) anchor = null;
    }
    if (!anchor) {
      let yaw = Math.PI + AZI;   // 兜底：默认用户朝 -z
      const mc = App.modelGroup;
      if (mc) {
        const dx = mc.position.x - hp.x;
        const dz = mc.position.z - hp.z;
        if (Math.hypot(dx, dz) > 0.3) yaw = Math.atan2(dx, dz) + AZI;
      }
      anchor = new THREE.Vector3(hp.x + Math.sin(yaw) * VIEW_DIST, 0, hp.z + Math.cos(yaw) * VIEW_DIST);
      anchorOffY = App._xrEyeOffY || 0;
      // 朝向定死一次：每帧 lookAt 会让大屏随头部噪声微转，按钮/顶栏在空间里滑动
      chat.mesh.position.set(anchor.x, TOP_Y - worldH / 2, anchor.z);
      // 目标点与面板中心同高 → 只转水平角：面板垂直地面，不仰不俯（瞄眼睛会把它顶成上仰）
      chat.mesh.lookAt(hp.x, chat.mesh.position.y, hp.z);
      lockDir.set(0, 0, -1).applyQuaternion(chat.mesh.quaternion);
      lockDir.y = 0;
      lockDir.normalize();
      chat.dirty = true;
    }

    // 位置钉死：只跟「视野升降」的偏移量走（那是用户主动低头/抬头），
    // 头显微动与走动都不追 —— 会追的面板按钮在空间里滑，射线永远瞄不准
    chat.mesh.position.set(anchor.x, TOP_Y - worldH / 2 + (App._xrEyeOffY || 0) - anchorOffY, anchor.z);
    let gaze = '';
    const r = App.renderer;
    if (r && r.xr && r.xr.isPresenting) {
      try {
        if (App._xrEyeRay(chat._v3, tmpDir)) gaze = mine(chat._v3, tmpDir, true);
      } catch (_) { /* 取不到就只留手柄射线 */ }
    }
    if (gaze !== chat.focusId) { chat.focusId = gaze; chat.dirty = true; }
    // 语音带在这块画布上：焦点同步给它；带内容变了（电平/计时）也让宿主重绘
    const vvo = App.vrVoice;
    if (vvo) {
      vvo.focusId = gaze.slice(0, 2) === 'v:' ? gaze.slice(2) : '';
      if (vvo.tick()) chat.dirty = true;
    }

    // 控制带数值变化（本面板按钮与手柄摇杆都在改）→ 重绘
    const ctl = App._vrBoardCtl;
    if (ctl) {
      const st = ctl.get();
      const cs = st.dist.toFixed(1) + '/' + st.scale.toFixed(1);
      if (cs !== ctrlSig) { ctrlSig = cs; chat.dirty = true; }
    }

    // DOM 轮询（180ms）：内容签名变了才置脏，重绘再按 90ms 节流
    if (now - sigAt > POLL_MS) {
      sigAt = now;
      const el = App.messagesEl;
      if (el) {
        chat.count = el.querySelectorAll('.msg:not(.typing)').length;
        const s = domSig(el);
        if (s !== sig) { sig = s; chat.dirty = true; }
      }
      readName();
    }
    if (chat.dirty && now - lastDraw >= REDRAW_MS) {
      lastDraw = now;
      chat.dirty = false;
      draw();
    }
  };
  App.updateVrChat = chat.update;

  /* ---------------- 命中：顶栏（展开态）/ 状态条（收起态） ---------------- */
  /** 命中口径：面板矩形 + 外扩磁力带（vr-ray 统一仲裁，跨面板互斥） */
  const probe = vrRayPanel({
    id: 'chat',
    active: () => !!(chat.active && chat.mesh && chat.mesh.visible),
    mesh: () => chat.mesh,
    cvW: () => CV_W,
    cvH: () => CV_H,
    hit: (px, py, loose, inside) => {
      if (chat.collapsed) {
        const bx = (CV_W - BAR_W) / 2;
        const by = (CV_H - BAR_H) / 2;
        const near = px >= bx - 160 && px <= bx + BAR_W + 160 && py >= by - 160 && py <= by + BAR_H + 160;
        return near ? 'toggle' : null;
      }
      // 控制带优先于顶栏：整条都给 id（显示格给 'cfg:sv'/'cfg:dv'，trigger 里忽略）。
      // 必须排在顶栏前面 —— 顶栏的视线磁力带（loose 时下探 120px）会盖住整条控制带，
      // 看控制带会被当成「看顶栏」，4s 后把大屏收起来。
      const cy0 = CTRL_TOP - (loose ? 26 : 6);
      const cy1 = CTRL_TOP + CTRL_H + (loose ? 26 : 6);
      if (py >= cy0 && py <= cy1 && px >= PAD - 26 && px <= CV_W - PAD + 26) {
        const cw = ctrlCellW();
        const i = Math.floor((px - PAD) / (cw + CTRL_GAP));
        return CTRL_IDS[Math.max(0, Math.min(CTRL_N - 1, i))];
      }
      // 底部语音带优先：它就在这块画布上，按钮口径由 vr-voice 自己算，
      // 用 'v:' 前缀回传 —— 否则它的按钮 id 会和顶栏的 'toggle' 撞在一个命名空间
      const vvo = App.vrVoice;
      if (vvo && vvo.active && py >= CV_H - bandH() - (loose ? 30 : 0)) {
        const bid = vvo.hitBand(px, py - (CV_H - bandH()), loose, inside);
        if (bid) return 'v:' + bid;
      }
      // 展开态：顶栏是靶子，视线模式再给它下面一条磁力带 ——
      // 不能整屏都算：凝视引擎拿这个结果计时，看正文停 4s 就会把大屏收掉
      return py <= HEAD_H + (loose ? 120 : 10) ? 'toggle' : null;
    },
    trigger: (id) => chat.trigger(id)
  });
  App.vrRay?.register(probe);

  chat.hitTest = function hitTest(origin: THREE.Vector3, dir: THREE.Vector3, snap?: boolean) {
    const r = probe.pick(origin, dir, !!snap);
    return r ? r.id : null;
  };

  /** 本面板锁定：全局仲裁里锁到的是我，就返回按钮 id（跨面板互斥） */
  function mine(o: THREE.Vector3, d: THREE.Vector3, loose: boolean): string {
    if (App.vrRay) return App.vrRay.pickMine('chat', o, d, loose);
    const r = probe.pick(o, d, loose);
    return r ? r.id : '';
  }

  chat.trigger = function trigger(id: string) {
    if (!chat.active) return;
    // 'v:' 前缀 = 语音带上的按钮：转交 vr-voice，宿主只管把面板重绘一次
    if (id.slice(0, 2) === 'v:') {
      if (App.vrVoice) App.vrVoice.trigger(id.slice(2));
      chat.dirty = true;
      return;
    }
    if (id.slice(0, 4) === 'cfg:') {
      const ctl = App._vrBoardCtl;
      if (ctl) {
        const k = id.slice(4);
        if (k === 's-') ctl.step('scale', -0.2);
        else if (k === 's+') ctl.step('scale', 0.2);
        else if (k === 'd-') ctl.step('dist', -1.5);
        else if (k === 'd+') ctl.step('dist', 1.5);
        else if (k === 'r') ctl.reset();
      }
      chat.dirty = true;
      draw();   // 立刻反馈，不等节流
      return;
    }
    if (id !== 'toggle') return;
    chat.collapsed = !chat.collapsed;
    if (chat.collapsed) countAtCollapse = chat.count;
    chat.dirty = true;
    draw();   // 立刻反馈，不等节流
    App.showToast(chat.collapsed ? '对话大屏已收起 · 点状态条展开' : '对话大屏已展开');
  };

  /* ---------------- 与非 VR 打通 ---------------- */
  // 1) 进入/退出 VR（入口失败不建对象：这层服务的是头显里的观感，
  //    进不去时留在普通画面里就是一块多余的浮空板）
  const _enter = App.enterXrMode;
  App.enterXrMode = async function (...args: any[]) {
    const res = await _enter.apply(this, args);
    if (App.xrPresenting) chat.show();
    else chat.hide();
    return res;
  };
  const _exit = App.exitXrMode;
  App.exitXrMode = function (...args: any[]) {
    chat.hide();
    return _exit.apply(this, arguments as any);
  };

  // 2) AI 状态：思考/说话中给末条加流式光标
  let lastState = 'idle';
  const _setState = App.setState;
  if (_setState) {
    App.setState = function (s: any) {
      const r = _setState.apply(this, arguments as any);
      if (s !== lastState) {
        lastState = s;
        if (chat.active) chat.dirty = true;
      }
      return r;
    };
  }

  // 3) XR 手柄扳机：先命中顶栏/状态条，未命中回落到内层（工具栏 → 音乐面板 → 戳角色）
  const _selStart = App._onControllerSelectStart;
  App._onControllerSelectStart = function (e: any) {
    const c = e && e.target;
    if (chat.active) {
      // 视线焦点优先：看着屏幕按确认 = 收起/展开（只有确认键的设备全靠这条）
      if (chat.focusId) {
        // press 模式「按住说话」：按下即录，松手由 vr-voice 的 selectend 发送
        if (App.vrVoice && App.vrVoice.pressDown(chat.focusId.slice(2))) return;
        chat.trigger(chat.focusId);
        return;
      }
      if (c) {
        try {
          const o = chat._v3, d = new THREE.Vector3();
          if (App._xrCtrlRay(c, o, d, chat._q)) {
            const id = mine(o, d, true);
            if (id) { chat.trigger(id); return; }
          }
        } catch (_) { /* 取不到位姿就回落 */ }
      }
    }
    return _selStart.apply(this, arguments as any);
  };

  // 4) 蓝牙手柄/兜底按键：视中心射线同样先命中
  const _padClick = App._xrPadClick;
  App._xrPadClick = function () {
    if (chat.active && !this._xrGameMode && this.xrPresenting && this.renderer && this.renderer.xr) {
      try {
        const o = new THREE.Vector3(), d = new THREE.Vector3();
        if (App._xrEyeRay(o, d)) {
          const id = mine(o, d, true);
          if (id) { chat.trigger(id); return; }
          if (chat.focusId) { chat.trigger(chat.focusId); return; }
        }
      } catch (_) { /* 回落 */ }
    }
    return _padClick.apply(this, arguments as any);
  };
}
