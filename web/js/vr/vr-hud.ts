/* ============================================================
 * web/js/vr/vr-hud.ts —— VR 世界内迷你状态面板 + 视频遥控（模块 32）
 * ------------------------------------------------------------
 * 手机 + VR 眼镜的 WebXR 沉浸会话里，手机屏幕只显示立体 3D 画面，
 * 网页 DOM 完全不可见。本模块只保留 VR 里真正有用的东西：
 *
 *   状态徽章：AI 状态（在线/思考中/聆听中/说话中，同步 setState）
 *   🎬 按钮：展开/收起「大白影院」视频遥控面板
 *   右上角 ✕：退出 VR（安全出口，射线/手柄 A 键可触发）
 *
 * 视频遥控面板（2026-08-26 新增）——VR 里直接控制大屏影院：
 *   - 视频（含 AI 语音点播的）在 VR 大屏上播放时面板自动展开，
 *     用户手动收起后同一部片内不再打扰，停播后重新 armed；
 *   - 播放/暂停、快退/快进 10 秒、下一部（连播队列弹出）；
 *   - 进度条点按跳转（direct 走 Range；relay 由服务端 ?ss= 模拟），
 *     直播流/无时长自动禁用并提示；
 *   - 音量增减 / 静音；
 *   - 📋 片单：浏览收藏夹，点选即点播（换片重解析自动上大屏）。
 *   播放控制走 videoBoardControl、状态节流拉取 videoBoardGetState
 *   （30_task_big_screen），与网页端「在线视频」面板完全同一套管线。
 *
 * 交互（先命中面板按钮才消费，其余射线/按键回落到原有戳角色逻辑）：
 *   - XR 手柄：手柄射线命中按钮 → 触发
 *   - 蓝牙手柄/无手柄：A/B/X/Y 键视中心射线命中按钮 → 触发
 *
 * 收起时是固定在 AI 角色右侧的迷你状态条（侧向进入 VR 时由 webxr-vr 确定：
 * App._vrHudSide，位置整场会话不变）；展开时钉在「波波点播」面板正上方
 * （读它的 mesh 位姿，随点播面板一起走，不跟头），朝向与它同一平面。
 * 仅 WebXR 沉浸（非游戏模式）显示。
 * ============================================================ */
import * as THREE from 'three';
import type { AppKernel, VrHud, VideoItem } from '../types/app-kernel.js';
import { vrRayPanel } from './vr-ray.js';

export default function initVrHud(App: AppKernel) {
  if (App.vrHud) return; // 幂等：避免重复初始化/重复包裹

  const HUD_W = 480;
  const HUD_H = 372;
  const HUD_WORLD_W = 1.1;              // 面板世界宽（米）
  const LIST_ROWS = 4;                  // 片单每页行数
  const ST_PULL_MS = 400;               // 播放状态节流拉取
  const PROG_REDRAW_MS = 500;           // 播放中进度条重绘间隔
  const FONT = '"Microsoft YaHei", "PingFang SC", "Noto Sans SC", sans-serif';

  const STATE_LABEL: Record<string, string> = { idle: '在线', thinking: '思考中', listening: '聆听中', speaking: '说话中' };
  const STATE_COLOR: Record<string, string> = { idle: '#4ade80', thinking: '#fbbf24', listening: '#38bdf8', speaking: '#f87171' };

  // 进度条命中区（画布像素坐标；点按位置 → 跳转分数）
  const SEEK_AREA = { x: 20, y: 276, w: 440, h: 54 };

  /* ---------------- 面板对象 ---------------- */
  const hud = App.vrHud = {
    active: false,
    mesh: null,
    tex: null,
    cv: null,
    ctx: null,
    buttons: [],
    flash: null,       // { id, until } 按钮触发后的高亮反馈
    dirty: true,
    videoOpen: false,  // 视频遥控面板展开中
    listOpen: false,   // 片单视图展开中
    // 上一次快照（变化才重绘）
    _state: 'idle',
    _ray: new THREE.Raycaster(),
    _v3: new THREE.Vector3(),
    _q: new THREE.Quaternion()
  } as VrHud;

  /* ---------------- 视频遥控状态 ---------------- */
  let vst: any = null;          // videoBoardGetState 节流缓存
  let vstAt = 0;
  let vstSig = '';              // 状态签名（变化才重绘）
  let lastProgAt = 0;           // 播放中进度条节流重绘
  let autoExpanded = false;     // 本部片已自动展开过（不再打扰手动收起的用户）
  let userCollapsed = false;    // 用户手动收起（同一部片内禁止再自动展开）
  let listItems: any[] = [];    // 片单条目（统一成 VideoItem + _cat 副标题）
  let listSrc: 'hot' | 'fav' | 'hist' = 'hot';  // 片单来源：热门 / 收藏 / 历史
  let listPage = 0;
  let listLoading = false;
  let lockExpand: boolean | null = null;  // 朝向锁定的展开态（null = 还没定过）
  let hudOffY = 0;                        // 显示时的视野升降偏移（面板之后只跟这个差值上下走）
  const lockDir = new THREE.Vector3(0, 0, -1); // 锁定瞬间的面板前向（世界系）

  /* 展开态摆位：钉在「波波点播」面板正上方（读它的 mesh 位姿，世界锚定）。
   * 只有点播面板不存在时才回退到「视线前方 1.85m」的旧摆法。 */
  const REMOTE_GAP = 0.06;    // 与点播面板上沿的缝隙（米）
  const REMOTE_DIST = 1.85;   // 兜底：视线正前方距离（米），点播面板不可用时才用
  const REMOTE_UP = 0.93;     // 兜底：中心抬升（米）
  const REMOTE_DEAD = 0.10;   // 软锚定死区：此范围内不跟（不晃、按钮不滑）
  const REMOTE_GAIN = 1.8;
  const REMOTE_MAX = 1.2;
  let hudAnchor: THREE.Vector3 | null = null;  // 展开态软锚点（null = 下一帧重挂）
  const hudIdeal = new THREE.Vector3();
  const hudFwd = new THREE.Vector3();

  /* ---------------- Canvas 绘制工具 ---------------- */
  function roundRect(ctx: CanvasRenderingContext2D, x: number, y: number, w: number, h: number, r: number) {
    ctx.beginPath();
    ctx.moveTo(x + r, y);
    ctx.arcTo(x + w, y, x + w, y + h, r);
    ctx.arcTo(x + w, y + h, x, y + h, r);
    ctx.arcTo(x, y + h, x, y, r);
    ctx.arcTo(x, y, x + w, y, r);
    ctx.closePath();
  }

  function fitTo(ctx: CanvasRenderingContext2D, s: string, maxW: number, font: string): string {
    ctx.font = font;
    const t = String(s == null ? '' : s);
    if (ctx.measureText(t).width <= maxW) return t;
    let cut = t.length;
    while (cut > 1 && ctx.measureText(t.slice(0, cut) + '…').width > maxW) cut--;
    return t.slice(0, cut) + '…';
  }

  function fmtT(s: number): string {
    s = Math.max(0, Math.floor(s || 0));
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), ss = s % 60;
    return h ? h + ':' + String(m).padStart(2, '0') + ':' + String(ss).padStart(2, '0')
             : m + ':' + String(ss).padStart(2, '0');
  }

  function videoBadge(st: any): { text: string; color: string } {
    if (!st || !st.active) return { text: '未播放', color: '#8c8ca0' };
    if (st.dead) return { text: '播放失败', color: '#ff6b6b' };
    if (st.recovering) return { text: '自动恢复中', color: '#fbbf24' };
    if (!st.ready) return { text: '载入中', color: '#38bdf8' };
    if (st.paused) return { text: '已暂停', color: '#ffd54f' };
    return { text: '播放中', color: '#4ade80' };
  }

  /* ---------------- 按钮表（画布像素坐标，随模式重建） ---------------- */
  function buildButtons() {
    // 顶栏常驻：🎬 展开视频遥控 + ✕ 退出
    const btns = [
      { id: 'toggleVideo', x: HUD_W - 96, y: 13, w: 38, h: 38 },
      { id: 'exit', x: HUD_W - 50, y: 13, w: 38, h: 38 }
    ];
    if (!hud.videoOpen) return btns;
    if (hud.listOpen) {
      // 片单视图：来源页签 + 翻页 + 返回 + 条目行
      btns.push({ id: 'srcHot', x: 20, y: 112, w: 136, h: 32 });
      btns.push({ id: 'srcFav', x: 172, y: 112, w: 136, h: 32 });
      btns.push({ id: 'srcHist', x: 324, y: 112, w: 136, h: 32 });
      btns.push({ id: 'pageUp', x: 288, y: 84, w: 44, h: 30 });
      btns.push({ id: 'pageDown', x: 338, y: 84, w: 44, h: 30 });
      btns.push({ id: 'backCtrl', x: 396, y: 84, w: 60, h: 30 });
      for (let i = 0; i < LIST_ROWS; i++) {
        btns.push({ id: 'item-' + (listPage * LIST_ROWS + i), x: 20, y: 152 + i * 50, w: 440, h: 46 });
      }
      return btns;
    }
    // 控制视图：两行大按钮
    const xs = [20, 133, 246, 359];
    ['back10', 'togglePlay', 'fwd10', 'next'].forEach((id, i) =>
      btns.push({ id, x: xs[i], y: 126, w: 101, h: 64 }));
    ['vol-', 'vol+', 'mute', 'list'].forEach((id, i) =>
      btns.push({ id, x: xs[i], y: 198, w: 101, h: 64 }));
    return btns;
  }

  /* ---------------- 场景构建 ---------------- */
  function ensureScene() {
    if (hud.mesh) return;
    const cv = document.createElement('canvas');
    cv.width = HUD_W;
    cv.height = HUD_H;
    hud.cv = cv;
    hud.ctx = cv.getContext('2d');

    const tex = new THREE.CanvasTexture(cv);
    tex.colorSpace = THREE.SRGBColorSpace;
    tex.minFilter = THREE.LinearFilter;
    hud.tex = tex;

    const aspect = HUD_H / HUD_W;
    const geo = new THREE.PlaneGeometry(HUD_WORLD_W, HUD_WORLD_W * aspect);
    const mat = new THREE.MeshBasicMaterial({
      map: tex,
      transparent: true,
      depthWrite: false,
      side: THREE.DoubleSide,
      polygonOffset: true,
      polygonOffsetFactor: -4
    });
    const mesh = new THREE.Mesh(geo, mat);
    mesh.renderOrder = 500;
    mesh.visible = false;
    App.scene!.add(mesh);
    hud.mesh = mesh;
    hud.buttons = buildButtons();
    hud.dirty = true;
  }

  /* ---------------- 绘制：通用按钮 ---------------- */
  function btnById(id: string) {
    return hud.buttons.find(b => b.id === id);
  }

  // 顶栏小图标按钮（🎬 / ✕ / 翻页）
  function drawIconBtn(ctx: CanvasRenderingContext2D, id: string, label: string, now: number, active: boolean,
                       flashFill?: string, flashStroke?: string) {
    const b = btnById(id);
    if (!b) return;
    const flashed = hud.flash && hud.flash.id === id && now < hud.flash.until;
    roundRect(ctx, b.x, b.y, b.w, b.h, 10);
    ctx.fillStyle = flashed ? (flashFill || 'rgba(0,229,255,0.32)')
      : active ? 'rgba(0,229,255,0.22)' : 'rgba(8, 12, 24, 0.55)';
    ctx.fill();
    ctx.lineWidth = flashed ? 2.5 : 1.5;
    ctx.strokeStyle = flashed ? (flashStroke || 'rgba(0,229,255,0.9)') : 'rgba(255,255,255,0.35)';
    ctx.stroke();
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.font = 'bold 19px ' + FONT;
    ctx.fillStyle = flashed || active ? '#ffffff' : 'rgba(232,246,255,0.85)';
    ctx.fillText(label, b.x + b.w / 2, b.y + b.h / 2 + 1);
    ctx.textAlign = 'left';
    ctx.textBaseline = 'alphabetic';
  }

  // 片单来源页签（热门 / 收藏 / 历史）：头显里没键盘，能点的入口就是选片的主要途径
  function drawTab(ctx: CanvasRenderingContext2D, id: string, label: string, now: number, active: boolean) {
    const b = btnById(id);
    if (!b) return;
    const flashed = !!(hud.flash && hud.flash.id === id && now < hud.flash.until);
    roundRect(ctx, b.x, b.y, b.w, b.h, 10);
    ctx.fillStyle = (flashed || active) ? 'rgba(0,229,255,0.22)' : 'rgba(255,255,255,0.06)';
    ctx.fill();
    ctx.lineWidth = (flashed || active) ? 2 : 1.2;
    ctx.strokeStyle = (flashed || active) ? 'rgba(0,229,255,0.9)' : 'rgba(255,255,255,0.2)';
    ctx.stroke();
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.font = '600 18px ' + FONT;
    ctx.fillStyle = (flashed || active) ? '#ffffff' : 'rgba(200,215,235,0.8)';
    ctx.fillText(label, b.x + b.w / 2, b.y + b.h / 2 + 1);
    ctx.textAlign = 'left';
    ctx.textBaseline = 'alphabetic';
  }

  // 遥控大按钮（控制行 / 片单行）
  function drawBigBtn(ctx: CanvasRenderingContext2D, b: { id: string; x: number; y: number; w: number; h: number },
                      label: string, now: number, small?: boolean) {
    const flashed = hud.flash && hud.flash.id === b.id && now < hud.flash.until;
    roundRect(ctx, b.x, b.y, b.w, b.h, 12);
    ctx.fillStyle = flashed ? 'rgba(0,229,255,0.32)' : 'rgba(255,255,255,0.07)';
    ctx.fill();
    ctx.lineWidth = flashed ? 2.5 : 1.2;
    ctx.strokeStyle = flashed ? 'rgba(0,229,255,0.9)' : 'rgba(255,255,255,0.22)';
    ctx.stroke();
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.font = (small ? '600 19px ' : 'bold 24px ') + FONT;
    ctx.fillStyle = flashed ? '#ffffff' : '#e8f2ff';
    ctx.fillText(label, b.x + b.w / 2, b.y + b.h / 2 + 1);
    ctx.textAlign = 'left';
    ctx.textBaseline = 'alphabetic';
  }

  /* ---------------- 绘制：顶栏状态条（常驻） ---------------- */
  function drawTopBar(ctx: CanvasRenderingContext2D, now: number) {
    const H = 64;
    roundRect(ctx, 8, 8, HUD_W - 16, H - 16, (H - 16) / 2);
    ctx.fillStyle = 'rgba(10, 14, 28, 0.88)';
    ctx.fill();
    ctx.lineWidth = 2.5;
    ctx.strokeStyle = 'rgba(0, 229, 255, 0.55)';
    ctx.stroke();

    // AI 状态徽章
    const st = hud._state;
    ctx.textAlign = 'left';
    ctx.textBaseline = 'middle';
    ctx.beginPath();
    ctx.arc(30, H / 2, 7, 0, Math.PI * 2);
    ctx.fillStyle = STATE_COLOR[st] || '#4ade80';
    ctx.fill();
    ctx.font = 'bold 21px ' + FONT;
    ctx.fillStyle = '#f2f6ff';
    ctx.fillText(STATE_LABEL[st] || '在线', 46, H / 2 + 1);

    // 视频迷你状态（有播放时）：📺 播放中/已暂停…
    if (vst && vst.active) {
      const badge = videoBadge(vst);
      ctx.font = '600 17px ' + FONT;
      ctx.fillStyle = badge.color;
      ctx.fillText('📺 ' + badge.text, 152, H / 2 + 1);
    }

    drawIconBtn(ctx, 'toggleVideo', '🎬', now, !!hud.videoOpen);
    drawIconBtn(ctx, 'exit', '✕', now, false, 'rgba(255,77,79,0.4)', '#ff4d4f');
  }

  /* ---------------- 绘制：视频控制视图 ---------------- */
  function drawRowButtons(ctx: CanvasRenderingContext2D, now: number) {
    const playing = !!(vst && vst.active && !vst.paused);
    const labels: Record<string, string> = {
      back10: '⏪ 10s',
      togglePlay: playing ? '⏸' : '▶',
      fwd10: '⏩ 10s',
      next: '⏭ 下一部',
      'vol-': '🔉 −',
      'vol+': '🔊 ＋',
      mute: (vst && vst.muted) ? '🔇' : '🔊',
      list: '📋 片单'
    };
    for (const id of ['back10', 'togglePlay', 'fwd10', 'next', 'vol-', 'vol+', 'mute', 'list']) {
      const b = btnById(id);
      if (b) drawBigBtn(ctx, b, labels[id], now);
    }
  }

  function drawProgress(ctx: CanvasRenderingContext2D, st: any) {
    const sa = SEEK_AREA;
    const barY = sa.y + 12, barH = 18;
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    if (st && st.active && st.seekable && st.duration > 0) {
      const frac = Math.min(1, Math.max(0, (st.currentTime || 0) / st.duration));
      roundRect(ctx, sa.x, barY, sa.w, barH, barH / 2);
      ctx.fillStyle = 'rgba(255,255,255,0.12)';
      ctx.fill();
      if (frac > 0) {
        roundRect(ctx, sa.x, barY, Math.max(barH, sa.w * frac), barH, barH / 2);
        const g = ctx.createLinearGradient(sa.x, 0, sa.x + sa.w, 0);
        g.addColorStop(0, '#00e5ff');
        g.addColorStop(1, '#7c5cff');
        ctx.fillStyle = g;
        ctx.fill();
      }
      ctx.beginPath();
      ctx.arc(sa.x + sa.w * frac, barY + barH / 2, 11, 0, Math.PI * 2);
      ctx.fillStyle = '#ffffff';
      ctx.fill();
      ctx.font = '600 18px ' + FONT;
      ctx.fillStyle = '#e8f2ff';
      ctx.fillText(fmtT(st.currentTime || 0) + ' / ' + fmtT(st.duration), sa.x + sa.w / 2, sa.y + 48);
    } else {
      roundRect(ctx, sa.x, barY, sa.w, barH, barH / 2);
      ctx.fillStyle = 'rgba(255,255,255,0.08)';
      ctx.fill();
      ctx.font = '500 17px ' + FONT;
      ctx.fillStyle = 'rgba(160,180,210,0.7)';
      ctx.fillText(st && st.active ? '— 直播流 · 暂不支持拖动 —' : '— 没有正在播放的视频 —',
        sa.x + sa.w / 2, sa.y + 48);
    }
    ctx.textAlign = 'left';
    ctx.textBaseline = 'alphabetic';
  }

  function drawControls(ctx: CanvasRenderingContext2D, now: number) {
    // 面板底
    roundRect(ctx, 8, 76, HUD_W - 16, HUD_H - 86, 18);
    ctx.fillStyle = 'rgba(8, 12, 26, 0.88)';
    ctx.fill();
    ctx.lineWidth = 2;
    ctx.strokeStyle = 'rgba(0, 229, 255, 0.5)';
    ctx.stroke();

    ctx.textBaseline = 'middle';
    if (!vst || !vst.active) {
      // 无播放：引导点播（片单按钮仍可用）
      ctx.textAlign = 'center';
      ctx.font = 'bold 24px ' + FONT;
      ctx.fillStyle = '#e8f2ff';
      ctx.fillText('大白影院 · 未在播放', HUD_W / 2, 108);
      ctx.font = '500 18px ' + FONT;
      ctx.fillStyle = 'rgba(160,180,210,0.85)';
      ctx.fillText('点「📋 片单」从收藏里选一部', HUD_W / 2, 142);
      drawRowButtons(ctx, now);
      drawProgress(ctx, vst);
      ctx.font = '500 16px ' + FONT;
      ctx.fillStyle = 'rgba(160,180,210,0.75)';
      ctx.fillText('或对大白说「播放○○」语音点播', HUD_W / 2, 350);
      ctx.textAlign = 'left';
      ctx.textBaseline = 'alphabetic';
      return;
    }

    // 标题 + 播放状态徽章
    const badge = videoBadge(vst);
    ctx.textAlign = 'left';
    ctx.font = 'bold 22px ' + FONT;
    ctx.fillStyle = '#ffffff';
    ctx.fillText('📺 ' + fitTo(ctx, String(vst.title || '正在播放…'), 300, 'bold 22px ' + FONT), 24, 104);
    roundRect(ctx, 344, 86, 112, 32, 16);
    ctx.fillStyle = 'rgba(255,255,255,0.08)';
    ctx.fill();
    ctx.font = '600 17px ' + FONT;
    ctx.fillStyle = badge.color;
    ctx.textAlign = 'center';
    ctx.fillText(badge.text, 400, 103);

    drawRowButtons(ctx, now);
    drawProgress(ctx, vst);
    ctx.font = '500 16px ' + FONT;
    ctx.fillStyle = 'rgba(160,180,210,0.75)';
    ctx.fillText('扳机/A键点击 · 进度条点按跳转 · 对大白说「播放○○」换片', HUD_W / 2, 350);
    ctx.textAlign = 'left';
    ctx.textBaseline = 'alphabetic';
  }

  /* ---------------- 绘制：片单视图（收藏点播） ---------------- */
  function drawList(ctx: CanvasRenderingContext2D, now: number) {
    roundRect(ctx, 8, 76, HUD_W - 16, HUD_H - 86, 18);
    ctx.fillStyle = 'rgba(8, 12, 26, 0.88)';
    ctx.fill();
    ctx.lineWidth = 2;
    ctx.strokeStyle = 'rgba(124, 92, 255, 0.5)';
    ctx.stroke();

    ctx.textBaseline = 'middle';
    ctx.textAlign = 'left';
    const SRC_TITLE: Record<string, string> = { hot: '🔥 热门推荐', fav: '📋 我的收藏', hist: '🕘 观看历史' };
    ctx.font = 'bold 22px ' + FONT;
    ctx.fillStyle = listSrc === 'hot' ? '#ffb26b' : listSrc === 'fav' ? '#b39ddb' : '#7fd1c8';
    ctx.fillText(SRC_TITLE[listSrc], 24, 100);

    drawTab(ctx, 'srcHot', '🔥 热门', now, listSrc === 'hot');
    drawTab(ctx, 'srcFav', '📋 收藏', now, listSrc === 'fav');
    drawTab(ctx, 'srcHist', '🕘 历史', now, listSrc === 'hist');

    const pages = Math.max(1, Math.ceil(listItems.length / LIST_ROWS));
    ctx.font = '600 17px ' + FONT;
    ctx.fillStyle = '#8c8ca0';
    ctx.textAlign = 'center';
    ctx.fillText((listPage + 1) + '/' + pages, 266, 99);
    drawIconBtn(ctx, 'pageUp', '◀', now, listPage > 0);
    drawIconBtn(ctx, 'pageDown', '▶', now, (listPage + 1) * LIST_ROWS < listItems.length);
    drawIconBtn(ctx, 'backCtrl', '⬅ 返回', now, false);

    if (listLoading) {
      ctx.font = '500 20px ' + FONT;
      ctx.fillStyle = '#8c8ca0';
      ctx.fillText('片单载入中…', HUD_W / 2, 240);
      ctx.textAlign = 'left';
      ctx.textBaseline = 'alphabetic';
      return;
    }
    if (!listItems.length) {
      ctx.font = '500 19px ' + FONT;
      ctx.fillStyle = '#8c8ca0';
      ctx.fillText(listSrc === 'hot' ? '热门推荐暂时拿不到'
        : listSrc === 'fav' ? '收藏夹是空的' : '还没有观看记录', HUD_W / 2, 214);
      ctx.fillText(listSrc === 'fav' ? '退出 VR 在「在线视频」里点 ☆ 收藏'
        : '也可以对大白说「播放○○」直接点播', HUD_W / 2, 246);
      ctx.textAlign = 'left';
      ctx.textBaseline = 'alphabetic';
      return;
    }
    for (let i = 0; i < LIST_ROWS; i++) {
      const idx = listPage * LIST_ROWS + i;
      const it = listItems[idx];
      const b = btnById('item-' + idx);
      if (!it || !b) continue;
      drawBigBtn(ctx, b, fitTo(ctx, '▶ ' + (it.title || '未知视频'), 330, '600 19px ' + FONT), now, true);
      if (it._cat) {
        ctx.font = '500 15px ' + FONT;
        ctx.fillStyle = 'rgba(160,180,210,0.6)';
        ctx.textAlign = 'right';
        ctx.fillText(it._cat, b.x + b.w - 12, b.y + b.h / 2 + 1);
        ctx.textAlign = 'left';
      }
    }
    ctx.textAlign = 'left';
    ctx.textBaseline = 'alphabetic';
  }

  /* ---------------- 总绘制 ---------------- */
  function draw() {
    const ctx = hud.ctx;
    if (!ctx) return;
    const now = performance.now();
    ctx.clearRect(0, 0, HUD_W, HUD_H);
    hud.buttons = buildButtons(); // 布局随模式重建（按钮表与画面同步）
    drawTopBar(ctx, now);
    if (hud.videoOpen) {
      if (hud.listOpen) drawList(ctx, now);
      else drawControls(ctx, now);
    }
    if (hud.tex) hud.tex.needsUpdate = true;
  }

  /* ---------------- 视频播放控制（与网页端 np 面板同一管线） ---------------- */
  function control(action: string, value?: number) {
    if (!App.videoBoardControl) { App.showToast('播放器尚未就绪'); return; }
    App.videoBoardControl({ action, ...(value !== undefined ? { value } : {}) } as any);
  }

  function pullVst(force?: boolean) {
    const now = performance.now();
    if (!force && now - vstAt < ST_PULL_MS) return;
    vstAt = now;
    vst = App.videoBoardGetState ? App.videoBoardGetState() : null;
  }

  async function refreshList() {
    if (listLoading) return;
    listLoading = true;
    hud.dirty = true;
    try {
      if (listSrc === 'hot') {
        // 视频源跟网页端当前选中的 chip 走（默认 all），面板看到的就是同一个源
        const plat = App.videoPlatformChips?.querySelector('.video-plat-chip[data-plat].active')?.getAttribute('data-plat') || 'all';
        const res = await fetch('/api/video_hub/api/hot?platform=' + encodeURIComponent(plat) + '&limit=24&page=1');
        const data = await res.json();
        listItems = (data.results || []).map((v: any) => Object.assign({}, v, { _cat: v.platform || '' }));
      } else if (listSrc === 'fav') {
        const res = await fetch('/api/video_hub/api/favorites');
        const data = await res.json();
        const cats: any[] = data.categories || [];
        listItems = (data.favorites || []).map((f: any) => {
          const c = cats.find((x: any) => x.id === f.category_id);
          return Object.assign({}, f.video || {}, { _cat: c ? c.name : '' });
        });
      } else {
        const res = await fetch('/api/video_hub/api/history');
        const data = await res.json();
        listItems = (data.history || []).map((h: any) => Object.assign({}, h.video || {}, { _cat: '看过' }));
      }
      if (listPage * LIST_ROWS >= listItems.length) listPage = 0;
    } catch (e) {
      listItems = [];
    } finally {
      listLoading = false;
      hud.dirty = true;
    }
  }

  async function playEntry(it: any) {
    if (!it || !it.webpage_url) { App.showToast('这个视频没有可播放的链接'); return; }
    hud.listOpen = false;
    hud.dirty = true;
    App.showToast('正在解析《' + (it.title || '') + '》…');
    try {
      await App.playVideoItem(it as VideoItem);
    } catch (e) {
      App.showToast('播放失败，换一部试试');
    }
  }

  /* ---------------- 对外：显示/隐藏/更新 ---------------- */
  hud.show = function show() {
    ensureScene();
    hud.active = true;
    // 每次 VR 会话重置：从迷你条开始，有播放时再自动展开
    hud.videoOpen = false;
    hud.listOpen = false;
    autoExpanded = false;
    userCollapsed = false;
    listItems = [];
    listPage = 0;
    lockExpand = null;
    hudOffY = App._xrEyeOffY || 0;
    hudAnchor = null;      // 下次 update 重新挂锚（不继承上一轮位置）
    hud.dirty = true;
    if (hud.mesh) hud.mesh.visible = true;
  };
  hud.hide = function hide() {
    hud.active = false;
    if (hud.mesh) hud.mesh.visible = false;
  };
  hud.markDirty = function markDirty() { hud.dirty = true; };

  /** 每帧调用：面板固定在 AI 角色右侧（侧向进入 VR 时确定，App._vrHudSide），
   * 朝向始终正对用户头部（走到哪都读得清）；+ 状态变化重绘 */
  hud.update = function update(dt: number) {
    if (!hud.active || !hud.mesh) return;
    const hp = App._xrHeadPos;
    if (!hp) return;
    const mc = App.modelGroup;
    if (!mc) return;
    const now = performance.now();

    // 角色侧向锚点（进入 VR 时确定，整场会话固定；拿不到时兜底角色 +X 侧）
    const side = App._vrHudSide || { x: 1, z: 0 };
    const expanded = !!hud.videoOpen;
    const worldH = HUD_WORLD_W * (HUD_H / HUD_W);
    // 点播面板在呼出中 → 遥控面板钉它上沿。读它的位姿而不是自己按视线重算：
    // 按视线算等于每帧跟头，头一动按钮就跟着滑，射线描不住也点不中。
    const vm = (expanded && App.vrVideo && App.vrVideo.open && App.vrVideo.mesh) || null;
    if (vm) {
      const gp = (vm.geometry as any).parameters;
      const vH = (gp && gp.height) || 1.4 * (560 / 760);  // 兜底：vr-video 的 WORLD_W 1.4 × 560/760
      hudIdeal.set(0, vH / 2 + worldH / 2 + REMOTE_GAP, 0)
        .applyQuaternion(vm.quaternion)   // 点播面板倾斜时沿它的"上"走，不脱离
        .add(vm.position);
      hudAnchor = null;
      hud.mesh.position.copy(hudIdeal);
    } else if (expanded) {
      // 兜底：点播面板没开/没建 → 视线正前方 1.85m，抬到眼高 +0.93m
      let hx = 0, hz = -1;
      const r = App.renderer;
      if (r && r.xr && r.xr.isPresenting) {
        try { r.xr.getCamera().getWorldDirection(hudFwd); hx = hudFwd.x; hz = hudFwd.z; } catch (_) { /* 取不到就沿默认前向摆 */ }
      }
      let len = Math.hypot(hx, hz);
      if (len < 1e-3) { hx = 0; hz = -1; len = 1; }
      hx /= len; hz /= len;
      hudIdeal.set(
        hp.x + hx * REMOTE_DIST,
        hp.y + (App._xrEyeOffY || 0) + REMOTE_UP,
        hp.z + hz * REMOTE_DIST
      );
      if (!hudAnchor) hudAnchor = hudIdeal.clone();
      // 软锚定：死区内不动（不晃），偏出死区按限速缓缓追上（走动/升降不丢面板）
      const dd = hudAnchor.distanceTo(hudIdeal);
      if (dd > REMOTE_DEAD) {
        const v = Math.min(dd * REMOTE_GAIN, REMOTE_MAX);
        hudAnchor.lerp(hudIdeal, Math.min(1, (v * dt) / dd));
      }
      hud.mesh.position.copy(hudAnchor);
    } else {
      // 收起：迷你状态条贴角色身侧偏上（整场会话固定，不跟头显）
      hudAnchor = null;
      hud.mesh.position.set(
        mc.position.x + side.x * 1.45,
        1.45 - worldH / 2 + (App._xrEyeOffY || 0) - hudOffY, // 视野升降：面板跟着视觉眼高走
        mc.position.z + side.z * 1.45
      );
    }
    // 朝向只定一次（展开/收起切换或用户绕到侧后方时重算）：每帧 lookAt 会让按钮
    // 随头部追踪噪声在空间里滑动，头显里描不住也点不中
    if (vm) {
      // 钉在点播面板上时朝向完全跟它一致（同一平面），本帧不参与重算
      hud.mesh.quaternion.copy(vm.quaternion);
      lockDir.set(0, 0, -1).applyQuaternion(hud.mesh.quaternion);
      lockExpand = expanded;
    } else {
      const tx = hud.mesh.position.x - hp.x, tz = hud.mesh.position.z - hp.z;
      const tl = Math.hypot(tx, tz) || 1;
      const off = (tx / tl) * lockDir.x + (tz / tl) * lockDir.z;
      if (lockExpand === null || lockExpand !== expanded || off < (expanded ? 0.86 : 0.6)) {
        hud.mesh.lookAt(hp.x, hp.y - (expanded ? 0.12 : 0.0), hp.z);
        lockDir.set(0, 0, -1).applyQuaternion(hud.mesh.quaternion);
        lockExpand = expanded;
      }
    }

    // 播放状态节流拉取（含 AI 语音点播的换片，title 变化自动重绘）
    pullVst();
    const sig = vst ? [
      vst.active ? 1 : 0, vst.title || '', vst.phase || '', vst.paused ? 1 : 0,
      vst.muted ? 1 : 0, Math.round((vst.volume || 0) * 10),
      vst.ready ? 1 : 0, vst.dead ? 1 : 0, vst.recovering ? 1 : 0
    ].join('|') : '';
    if (sig !== vstSig) { vstSig = sig; hud.dirty = true; }

    // 自动展开/收起：开播（含进 VR 时已在播）展开一次；用户手动收起后同一部片
    // 不再打扰；停播自动收起（正在浏览片单时不收）
    const act = !!(vst && vst.active);
    if (act && !autoExpanded && !userCollapsed) {
      autoExpanded = true;
      if (!hud.videoOpen) { hud.videoOpen = true; hud.dirty = true; }
    } else if (!act && (autoExpanded || userCollapsed)) {
      autoExpanded = false;
      userCollapsed = false;
      if (hud.videoOpen && !hud.listOpen) { hud.videoOpen = false; hud.dirty = true; }
    }
    // 播放中进度条节流重绘（时间文本/进度条平滑前进）
    if (hud.videoOpen && !hud.listOpen && act && !vst.paused && now - lastProgAt > PROG_REDRAW_MS) {
      lastProgAt = now;
      hud.dirty = true;
    }

    // AI 状态徽章（变化才重绘）
    const st = App.currentState || 'idle';
    if (st !== hud._state) { hud._state = st; hud.dirty = true; }

    if (hud.dirty) {
      hud.dirty = false;
      draw();
    }
  };
  App.updateVrHud = hud.update;

  /* ---------------- 命中测试：射线 → 按钮id / 进度条跳转 ---------------- */
  /** 命中口径：面板矩形 + 外扩磁力带（vr-ray 统一仲裁，跨面板互斥） */
  const probe = vrRayPanel({
    id: 'hud',
    active: () => !!(hud.active && hud.mesh && hud.mesh.visible),
    mesh: () => hud.mesh,
    cvW: () => HUD_W,
    cvH: () => HUD_H,
    hit: (px, py, loose, inside) => {
      for (const b of hud.buttons) {
        if (px >= b.x && px <= b.x + b.w && py >= b.y && py <= b.y + b.h) return b.id;
      }
      // 进度条：点按位置 → 跳转分数（'seek:0.42'，trigger 内校验可拖动性）
      if (hud.videoOpen && !hud.listOpen) {
        const sa = SEEK_AREA;
        if (px >= sa.x && px <= sa.x + sa.w && py >= sa.y && py <= sa.y + sa.h) {
          return 'seek:' + Math.max(0, Math.min(1, (px - sa.x) / sa.w)).toFixed(3);
        }
      }
      if (!loose) return null;
      // 视线/磁力带：吸附最近按钮（hud 按钮小，头显里靠它才点得中）
      let best = '';
      let bestD = Infinity;
      for (const b of hud.buttons) {
        const dx = Math.max(b.x - px, 0, px - (b.x + b.w));
        const dy = Math.max(b.y - py, 0, py - (b.y + b.h));
        const d = Math.hypot(dx, dy);
        if (d < bestD) { bestD = d; best = b.id; }
      }
      return bestD <= (inside ? 60 : 260) ? best : '';
    },
    trigger: (id) => hud.trigger(id)
  });
  App.vrRay?.register(probe);

  hud.hitTest = function hitTest(origin: THREE.Vector3, dir: THREE.Vector3) {
    const r = probe.pick(origin, dir, true);
    return r ? r.id : null;
  };

  /* ---------------- 命中动作 ---------------- */
  function flashBtn(id: string) {
    hud.flash = { id, until: performance.now() + 260 };
    hud.dirty = true;
  }
  hud.trigger = function trigger(id: string) {
    if (!hud.active) return;
    flashBtn(id.split(':')[0]);
    if (id === 'exit' && App.exitXrMode) { App.exitXrMode(); return; }
    if (id === 'toggleVideo') {
      const wasOpen = hud.videoOpen;
      hud.videoOpen = !hud.videoOpen;
      if (!hud.videoOpen) {
        hud.listOpen = false;
        // 播放中手动收起：本部片内不再自动展开（停播后重新 armed）
        if (vst && vst.active && wasOpen) userCollapsed = true;
      }
      hud.dirty = true;
      return;
    }
    if (id === 'list') { hud.listOpen = true; hud.dirty = true; refreshList(); return; }
    if (id === 'srcHot' || id === 'srcFav' || id === 'srcHist') {
      const src = id === 'srcHot' ? 'hot' : id === 'srcFav' ? 'fav' : 'hist';
      if (src !== listSrc) { listSrc = src as 'hot' | 'fav' | 'hist'; listPage = 0; listItems = []; refreshList(); }
      return;
    }
    if (id === 'backCtrl') { hud.listOpen = false; hud.dirty = true; return; }
    if (id === 'pageUp') { if (listPage > 0) { listPage--; hud.dirty = true; } return; }
    if (id === 'pageDown') {
      if ((listPage + 1) * LIST_ROWS < listItems.length) { listPage++; hud.dirty = true; }
      return;
    }
    if (id.indexOf('item-') === 0) {
      const it = listItems[Number(id.slice(5))];
      if (it) playEntry(it);
      return;
    }
    if (id.indexOf('seek:') === 0) {
      const frac = Number(id.slice(5));
      pullVst(true);
      if (!vst || !vst.active || !vst.seekable || !vst.duration) {
        App.showToast('这个视频暂不支持拖动进度');
        return;
      }
      control('seek', Math.round(frac * vst.duration));
      hud.dirty = true;
      return;
    }
    // 播放控制（动作前强制刷新状态，避免用旧 paused/volume 判断）
    pullVst(true);
    switch (id) {
      case 'togglePlay':
        control(vst && vst.paused ? 'resume' : 'pause');
        break;
      case 'back10':
        if (vst) control('seek', Math.max(0, (vst.currentTime || 0) - 10));
        break;
      case 'fwd10':
        if (vst) control('seek', (vst.currentTime || 0) + 10);
        break;
      case 'next':
        control('next');
        break;
      case 'vol-':
        control('volume', Math.max(0, ((vst && vst.volume) || 0.8) - 0.1));
        break;
      case 'vol+':
        control('volume', Math.min(1, ((vst && vst.volume) || 0.8) + 0.1));
        break;
      case 'mute':
        control('mute');
        break;
    }
    hud.dirty = true;
  };

  /* ---------------- 与非 VR 模式打通：包裹既有入口 ---------------- */
  // 1) 进入/退出 VR 时显示/隐藏面板（游戏模式 VR 由游戏自理，不显示）
  const _enter = App.enterXrMode;
  App.enterXrMode = async function (...args: any[]) {
    const r = await _enter.apply(this, args);
    hud.show();
    return r;
  };
  const _exit = App.exitXrMode;
  App.exitXrMode = function (...args: any[]) {
    hud.hide();
    return _exit.apply(this, args);
  };

  // 2) 状态徽章同步（setState 变换时重绘）
  const _setState = App.setState;
  App.setState = function (s) {
    const r = _setState.apply(this, arguments);
    if (hud.active) hud.dirty = true;
    return r;
  };

  // 3) 字幕同步已移除（2026-08-26）：说话文字由角色头顶气泡承担，
  //    面板不再显示/同步字幕（showSubtitle / showChatBubble 不做包裹）

  // 4) XR 手柄扳机：先命中面板按钮，未命中才回落到戳角色
  const _selStart = App._onControllerSelectStart;
  App._onControllerSelectStart = function (e: any) {
    const c = e.target;
    if (hud.active && c) {
      try {
        const o = hud._v3, d = new THREE.Vector3();
        if (App._xrCtrlRay(c, o, d, hud._q)) {
          const h = App._vrRayPick ? App._vrRayPick(o, d, true) : null;
          if (h) {
            h.panel.trigger(h.id);
            return;
          }
        }
      } catch (_) { /* 忽略异常，回退到戳一戳 */ }
    }
    return _selStart.apply(this, arguments);
  };

  // 5) 蓝牙手柄/兜底按键：视中心射线先命中面板按钮，未命中才回落到原逻辑
  const _padClick = App._xrPadClick;
  App._xrPadClick = function () {
    if (hud.active && !this._xrGameMode) {
      let origin: THREE.Vector3 | null = null, dir: THREE.Vector3 | null = null;
      if (this.xrPresenting && this.renderer && this.renderer.xr) {
        const o = new THREE.Vector3(), d = new THREE.Vector3();
        if (App._xrEyeRay(o, d)) { origin = o; dir = d; }
      }
      if (!origin || !dir) {
        const yaw = this.gyroYaw || 0, pitch = this.gyroPitch || 0, cp = Math.cos(pitch);
        origin = this.camera.position.clone();
        dir = new THREE.Vector3(-Math.sin(yaw) * cp, Math.sin(pitch), -Math.cos(yaw) * cp);
      }
      const h = App._vrRayPick ? App._vrRayPick(origin, dir, true) : null;
      if (h) {
        h.panel.trigger(h.id);
        return;
      }
    }
    return _padClick.apply(this, arguments);
  };
}
