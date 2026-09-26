/* ============================================================
 * web/js/vr/vr-video.ts —— VR 世界内在线视频面板（模块 32）
 * ------------------------------------------------------------
 * 网页端的「在线视频」是一整块 DOM 弹窗（29_video_ui）：搜索框 + 平台/排序
 * chips + 缩略图网格 + 热门/收藏/历史。沉浸态里 DOM 完全不可见，头显里
 * 连搜片都做不到。本模块把这块面板在 VR 世界里重建一份，数据与播放仍走
 * 同一套管线：/api/video_hub/api/{search,hot,favorites,history}、缩略图走
 * /api/video_hub/thumb 代理、点播走 App.playVideoItem（画面投直播大屏），
 * 不另起一套播放器。
 *
 * 布局（画布 760×560，世界宽 1.4m，呼出时立在视线正前方）：
 *   顶栏   标题 + [搜索][热门][收藏][历史] + 刷新 + ✕
 *   搜索行 关键词框 + 🎤 语音输入 + 🔍 搜索 + 清空
 *   chips  平台（全部/B站/AcFun/YouTube）+ 排序（综合/最新/热门）
 *   网格   2 列 × 3 行卡片：缩略图 + 标题 + 平台/播放量 + ☆ 收藏
 *   底栏   ◀ 页码 ▶ + 状态提示
 *
 * 语音输入（头显里没键盘，这是唯一的「打字」方式）：点 🎤 → 面板进入
 * 待听状态 → 下一句语音识别结果（websocket transcript）被本模块经
 * App._vrVideoVoiceHook 截获，清掉「搜索/播放/我想看」这类口令词后当
 * 关键词自动搜索。语音链路本身仍是 AI 那套 STT，不另建识别器。
 *
 * 稳定性（头显里最要紧的是「不晃」）：位置软锚定 —— 死区内不动，偏出
 * 死区按限速追上；朝向只在挂锚点时 lookAt 一次，之后锁死。每帧 lookAt
 * 或每帧跟头都会让按钮随头部追踪噪声滑动，瞄不住也点不中。
 *
 * 交互：射线命中（小按钮带视线吸附）+ 扳机/确认键；由 vr-toolbar 的
 * video-btn 呼出，其余射线回落到下层面板。
 * ============================================================ */
import * as THREE from 'three';
import type { AppKernel, VrVideo, VrHudButton, VideoItem } from '../types/app-kernel.js';
import { vrRayPanel } from './vr-ray.js';

export default function initVrVideo(App: AppKernel) {
  if (App.vrVideo) return; // 幂等：避免重复初始化/重复包裹

  const CV_W = 760;
  const CV_H = 560;
  const WORLD_W = 1.4;                  // 面板世界宽（米）
  const COLS = 2;
  const ROWS = 3;
  const PER_PAGE = COLS * ROWS;
  const CARD_W = 358;
  const CARD_H = 118;
  const CARD_GAP_X = 12;
  const CARD_GAP_Y = 10;
  const GRID_X = 20;
  const GRID_Y = 156;
  const THUMB_W = 144;
  const THUMB_H = 81;
  const PAGE_SIZE = 12;                 // 后端一页条数（与网页端一致）
  const FONT = '"Microsoft YaHei", "PingFang SC", "Noto Sans SC", sans-serif';

  /* 呼出面板的摆位参数（照抄 vr-music 已验证的软锚定量级） */
  const DIST = 1.25;                    // 呼出距离（米）：1.85 时 760px 画布的 16px 字只占 0.9° 视角、1.50 是 1.1°，都偏小；1.25 拉到 1.35°
  const REACH = 3.0;                    // 走远到此距离重挂
  const OFF_COS = 0.55;                 // 绕到面板侧后方（余弦低于此）重挂
  const FOLLOW_DEAD = 0.10;             // 软锚定死区：此范围内不跟（不晃）
  const FOLLOW_GAIN = 1.8;
  const FOLLOW_MAX = 1.2;

  const PLATFORMS = [
    { id: 'all', label: '全部' },
    { id: 'bilibili', label: 'B站' },
    { id: 'acfun', label: 'AcFun' },
    { id: 'youtube', label: 'YouTube' }
  ];
  const SORTS = [
    { id: 'relevance', label: '综合' },
    { id: 'new', label: '最新' },
    { id: 'hot', label: '热门' }
  ];
  const PLATFORM_LABEL: Record<string, string> = { bilibili: 'B站', acfun: 'AcFun', youtube: 'YouTube' };

  /* ---------------- 面板对象 ---------------- */
  const vv = App.vrVideo = {
    active: false,
    open: false,
    mesh: null,
    tex: null,
    cv: null,
    ctx: null,
    buttons: [],
    flash: null,
    dirty: true,
    hoverId: '',
    _ray: new THREE.Raycaster(),
    _v3: new THREE.Vector3(),
    _q: new THREE.Quaternion(),
    show: () => {},
    hide: () => {},
    markDirty: () => {},
    update: () => {},
    hitTest: () => null,
    trigger: () => {},
    toggle: () => {}
  } as VrVideo;

  /* ---------------- 数据与视图状态 ---------------- */
  type View = 'search' | 'hot' | 'fav' | 'hist';
  let view: View = 'hot';
  let keyword = '';
  let plat = 'all';
  let sort = 'relevance';
  let page = 0;                         // 当前页（0 基）
  let loading = false;
  let errMsg = '';
  let voiceWait = false;                // 待听：下一句语音当搜索词
  let seq = 0;                          // 请求序号：翻页/换源并发时只认最后一次
  let remote: Map<number, VideoItem[]> = new Map();  // 远端分页缓存（搜索/热门）
  let pool: VideoItem[] = [];           // 本地分页池（收藏/历史一次拉全）
  let hasMore = true;                   // 远端还有下一页
  const thumbs = new Map<string, HTMLImageElement | 0>(); // 缩略图缓存（0=加载失败）

  /* ---------------- 定位状态 ---------------- */
  let anchor: THREE.Vector3 | null = null;
  const lockDir = new THREE.Vector3(0, 0, -1);
  const idealPos = new THREE.Vector3();
  const fwd = new THREE.Vector3();
  let fdirX = 0, fdirZ = -1;

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

  /** 标题最多两行（超出第二行尾省略号）：卡片文字区窄，单行截断读不出片名 */
  function wrapTwo(ctx: CanvasRenderingContext2D, s: string, maxW: number, font: string): string[] {
    ctx.font = font;
    const t = String(s || '未知视频');
    const out: string[] = [];
    let line = '';
    for (const ch of t) {
      if (ctx.measureText(line + ch).width > maxW) {
        out.push(line);
        line = ch;
        if (out.length >= 2) break;
      } else line += ch;
    }
    if (out.length < 2 && line) out.push(line);
    if (out.length === 2 && out.join('').length < t.length) {
      const last = out[1];
      out[1] = (last.length > 1 ? last.slice(0, -1) : last) + '…';
    }
    return out;
  }

  function fmtT(sec: number | null | undefined): string {
    const s = Math.max(0, Math.floor(Number(sec) || 0));
    if (!s) return '';
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), ss = s % 60;
    return h ? h + ':' + String(m).padStart(2, '0') + ':' + String(ss).padStart(2, '0')
             : m + ':' + String(ss).padStart(2, '0');
  }

  function fmtViews(n: number | undefined): string {
    if (!n || n <= 0) return '';
    return n >= 10000 ? (n / 10000).toFixed(1) + '万播放' : n + '播放';
  }

  /* ---------------- 缩略图：走后端代理（图床防盗链，浏览器直连会挂） ---------------- */
  function thumbUrl(v: any): string {
    return v && v.thumbnail ? '/api/video_hub/thumb?u=' + encodeURIComponent(v.thumbnail) : '';
  }

  function thumbOf(v: any): HTMLImageElement | null {
    const u = thumbUrl(v);
    if (!u) return null;
    const c = thumbs.get(u);
    if (c === 0) return null;
    if (c) return (c.complete && c.naturalWidth > 0) ? c : null;
    const img = new Image();
    thumbs.set(u, img);
    img.onload = () => { vv.dirty = true; };
    img.onerror = () => { thumbs.set(u, 0); };
    img.src = u;
    return null;
  }

  /* ---------------- 按钮表（画布像素坐标） ---------------- */
  function buildButtons(): VrHudButton[] {
    const btns: VrHudButton[] = [
      { id: 'tabSearch', x: 170, y: 8, w: 96, h: 36 },
      { id: 'tabHot', x: 272, y: 8, w: 96, h: 36 },
      { id: 'tabFav', x: 374, y: 8, w: 96, h: 36 },
      { id: 'tabHist', x: 476, y: 8, w: 96, h: 36 },
      { id: 'refresh', x: 596, y: 8, w: 96, h: 36 },
      { id: 'close', x: CV_W - 50, y: 7, w: 38, h: 38 },
      { id: 'mic', x: 548, y: 60, w: 52, h: 42 },
      { id: 'go', x: 606, y: 60, w: 52, h: 42 },
      { id: 'clearKw', x: 664, y: 60, w: 76, h: 42 },
      { id: 'pageUp', x: 620, y: 530, w: 52, h: 30 },
      { id: 'pageDown', x: 680, y: 530, w: 52, h: 30 }
    ];
    for (let i = 0; i < PLATFORMS.length; i++) {
      btns.push({ id: 'plat-' + PLATFORMS[i].id, x: 20 + i * 68, y: 112, w: 64, h: 34 });
    }
    for (let i = 0; i < SORTS.length; i++) {
      btns.push({ id: 'sort-' + SORTS[i].id, x: 312 + i * 68, y: 112, w: 64, h: 34 });
    }
    const rows = pageItems().length;
    for (let i = 0; i < PER_PAGE; i++) {
      if (i >= rows) break;
      const col = i % COLS;
      const row = Math.floor(i / COLS);
      btns.push({
        id: 'card-' + i,
        x: GRID_X + col * (CARD_W + CARD_GAP_X),
        y: GRID_Y + row * (CARD_H + CARD_GAP_Y),
        w: CARD_W,
        h: CARD_H
      });
      btns.push({ id: 'fav-' + i, x: GRID_X + col * (CARD_W + CARD_GAP_X) + CARD_W - 40, y: GRID_Y + row * (CARD_H + CARD_GAP_Y) + 8, w: 32, h: 32 });
    }
    return btns;
  }

  /* ---------------- 场景构建 ---------------- */
  function ensureScene() {
    if (vv.mesh) return;
    const cv = document.createElement('canvas');
    cv.width = CV_W;
    cv.height = CV_H;
    vv.cv = cv;
    vv.ctx = cv.getContext('2d');

    const tex = new THREE.CanvasTexture(cv);
    tex.colorSpace = THREE.SRGBColorSpace;
    tex.minFilter = THREE.LinearFilter;
    vv.tex = tex;

    const geo = new THREE.PlaneGeometry(WORLD_W, WORLD_W * (CV_H / CV_W));
    const mat = new THREE.MeshBasicMaterial({
      map: tex,
      transparent: true,
      depthWrite: false,
      side: THREE.DoubleSide,
      polygonOffset: true,
      polygonOffsetFactor: -4
    });
    const mesh = new THREE.Mesh(geo, mat);
    mesh.renderOrder = 512; // 压在 vr-hud（500）/vr-music（510）之上：呼出面板要在最前面
    mesh.visible = false;
    App.scene!.add(mesh);
    vv.mesh = mesh;
    vv.buttons = buildButtons();
    vv.dirty = true;
  }

  function btnById(id: string) {
    return vv.buttons.find(b => b.id === id);
  }

  /* ---------------- 当前页条目 / 页数 ---------------- */
  function pageItems(): VideoItem[] {
    if (view === 'search' || view === 'hot') return remote.get(page) || [];
    return pool.slice(page * PER_PAGE, page * PER_PAGE + PER_PAGE);
  }

  function pageCount(): number {
    if (view === 'search' || view === 'hot') {
      let n = 0;
      remote.forEach((_v, k) => { if (k + 1 > n) n = k + 1; });
      return Math.max(1, n + (hasMore ? 1 : 0));
    }
    return Math.max(1, Math.ceil(pool.length / PER_PAGE));
  }

  /* ---------------- 绘制：通用按钮 ---------------- */
  function drawSmallBtn(ctx: CanvasRenderingContext2D, id: string, label: string, now: number, active: boolean) {
    const b = btnById(id);
    if (!b) return;
    const flashed = !!(vv.flash && vv.flash.id === id && now < vv.flash.until);
    const hovered = vv.hoverId === id;
    roundRect(ctx, b.x, b.y, b.w, b.h, 10);
    ctx.fillStyle = flashed ? 'rgba(0,229,255,0.34)'
      : active ? 'rgba(0,229,255,0.22)'
      : hovered ? 'rgba(255,255,255,0.16)' : 'rgba(8, 12, 24, 0.55)';
    ctx.fill();
    ctx.lineWidth = flashed ? 2.5 : 1.5;
    ctx.strokeStyle = (flashed || active) ? 'rgba(0,229,255,0.9)' : 'rgba(255,255,255,0.35)';
    ctx.stroke();
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.font = 'bold 18px ' + FONT;
    ctx.fillStyle = (flashed || active) ? '#ffffff' : 'rgba(232,246,255,0.85)';
    ctx.fillText(label, b.x + b.w / 2, b.y + b.h / 2 + 1);
    ctx.textAlign = 'left';
    ctx.textBaseline = 'alphabetic';
  }

  function drawChip(ctx: CanvasRenderingContext2D, id: string, label: string, now: number, active: boolean) {
    const b = btnById(id);
    if (!b) return;
    const hovered = vv.hoverId === id;
    roundRect(ctx, b.x, b.y, b.w, b.h, 17);
    ctx.fillStyle = active ? 'rgba(124,92,255,0.42)' : hovered ? 'rgba(255,255,255,0.16)' : 'rgba(8,12,24,0.5)';
    ctx.fill();
    ctx.lineWidth = active ? 2 : 1.2;
    ctx.strokeStyle = active ? 'rgba(160,140,255,0.95)' : 'rgba(255,255,255,0.28)';
    ctx.stroke();
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.font = (active ? 'bold ' : '500 ') + '16px ' + FONT;
    ctx.fillStyle = active ? '#ffffff' : 'rgba(226,236,250,0.82)';
    ctx.fillText(label, b.x + b.w / 2, b.y + b.h / 2 + 1);
    ctx.textAlign = 'left';
    ctx.textBaseline = 'alphabetic';
  }

  /* ---------------- 绘制：卡片 ---------------- */
  function drawCard(ctx: CanvasRenderingContext2D, i: number, it: VideoItem, now: number) {
    const b = btnById('card-' + i);
    if (!b || !it) return;
    const hovered = vv.hoverId === 'card-' + i || vv.hoverId === 'fav-' + i;
    roundRect(ctx, b.x, b.y, b.w, b.h, 12);
    ctx.fillStyle = hovered ? 'rgba(255,255,255,0.15)' : 'rgba(255,255,255,0.06)';
    ctx.fill();
    ctx.lineWidth = 1.4;
    ctx.strokeStyle = hovered ? 'rgba(0,229,255,0.7)' : 'rgba(255,255,255,0.18)';
    ctx.stroke();

    const tx = b.x + 10;
    const ty = b.y + (CARD_H - THUMB_H) / 2;
    const img = thumbOf(it);
    if (img) {
      ctx.drawImage(img, tx, ty, THUMB_W, THUMB_H);
    } else {
      roundRect(ctx, tx, ty, THUMB_W, THUMB_H, 8);
      ctx.fillStyle = 'rgba(0,0,0,0.45)';
      ctx.fill();
      ctx.font = '34px ' + FONT;
      ctx.fillStyle = 'rgba(200,220,255,0.35)';
      ctx.textAlign = 'center';
      ctx.textBaseline = 'middle';
      ctx.fillText('▶', tx + THUMB_W / 2, ty + THUMB_H / 2 + 1);
      ctx.textAlign = 'left';
      ctx.textBaseline = 'alphabetic';
    }
    const dur = fmtT(it.duration);
    if (dur) {
      ctx.font = '500 13px ' + FONT;
      const dw = ctx.measureText(dur).width + 12;
      roundRect(ctx, tx + THUMB_W - dw - 6, ty + THUMB_H - 24, dw, 19, 6);
      ctx.fillStyle = 'rgba(0,0,0,0.72)';
      ctx.fill();
      ctx.fillStyle = '#e8f2ff';
      ctx.textAlign = 'center';
      ctx.textBaseline = 'middle';
      ctx.fillText(dur, tx + THUMB_W - dw / 2 - 6, ty + THUMB_H - 14);
      ctx.textAlign = 'left';
      ctx.textBaseline = 'alphabetic';
    }

    const tX = tx + THUMB_W + 14;
    const tW = b.x + b.w - tX - 56;   // 给右上角 ☆ 让位
    const lines = wrapTwo(ctx, it.title || '未知视频', tW, '600 18px ' + FONT);
    ctx.font = '600 18px ' + FONT;
    ctx.fillStyle = '#eaf2ff';
    ctx.textBaseline = 'middle';
    for (let k = 0; k < lines.length; k++) ctx.fillText(lines[k], tX, b.y + 30 + k * 24);

    const meta = [
      PLATFORM_LABEL[String(it.platform || '').toLowerCase()] || String(it.platform || ''),
      fmtViews(it.view_count),
      it.uploader ? 'UP: ' + it.uploader : ''
    ].filter(Boolean).join(' · ');
    ctx.font = '500 14px ' + FONT;
    ctx.fillStyle = 'rgba(160,180,210,0.78)';
    ctx.fillText(fitTo(ctx, meta || '点按播放', tW, '500 14px ' + FONT), tX, b.y + CARD_H - 22);
    ctx.textBaseline = 'alphabetic';

    const starred = !!(it.webpage_url && App.isVideoFavorited && App.isVideoFavorited(it.webpage_url));
    drawSmallBtn(ctx, 'fav-' + i, starred ? '★' : '☆', now, starred);
  }

  /* ---------------- 绘制：顶栏 ---------------- */
  function drawTopBar(ctx: CanvasRenderingContext2D, now: number) {
    roundRect(ctx, 8, 8, CV_W - 16, 48, 24);
    ctx.fillStyle = 'rgba(10, 14, 28, 0.88)';
    ctx.fill();
    ctx.lineWidth = 2.5;
    ctx.strokeStyle = 'rgba(0, 229, 255, 0.55)';
    ctx.stroke();

    ctx.textAlign = 'left';
    ctx.textBaseline = 'middle';
    ctx.font = 'bold 20px ' + FONT;
    ctx.fillStyle = '#f2f6ff';
    ctx.fillText('🎬 在线视频', 24, 33);

    drawSmallBtn(ctx, 'tabSearch', '搜索', now, view === 'search');
    drawSmallBtn(ctx, 'tabHot', '🔥热门', now, view === 'hot');
    drawSmallBtn(ctx, 'tabFav', '📋收藏', now, view === 'fav');
    drawSmallBtn(ctx, 'tabHist', '🕘历史', now, view === 'hist');
    drawSmallBtn(ctx, 'refresh', '刷新', now, false);
    drawSmallBtn(ctx, 'close', '✕', now, false);
    ctx.textBaseline = 'alphabetic';
  }

  /* ---------------- 绘制：搜索行 ---------------- */
  function drawSearchRow(ctx: CanvasRenderingContext2D, now: number) {
    roundRect(ctx, 20, 60, 520, 42, 12);
    ctx.fillStyle = 'rgba(0,0,0,0.42)';
    ctx.fill();
    ctx.lineWidth = 1.6;
    ctx.strokeStyle = voiceWait ? 'rgba(0,229,255,0.95)' : 'rgba(255,255,255,0.22)';
    ctx.stroke();

    const hint = voiceWait ? '🎤 正在听…说出要搜的视频'
      : keyword ? keyword
        : '点 🎤 说片名（头显里没键盘），也可以直接对大白说「播放…」';
    ctx.font = voiceWait || keyword ? '600 18px ' + FONT : '500 16px ' + FONT;
    ctx.fillStyle = voiceWait ? '#8ff3ff' : keyword ? '#eaf2ff' : 'rgba(160,180,210,0.75)';
    ctx.textAlign = 'left';
    ctx.textBaseline = 'middle';
    ctx.fillText(fitTo(ctx, hint, 496, voiceWait || keyword ? '600 18px ' + FONT : '500 16px ' + FONT), 34, 82);
    ctx.textBaseline = 'alphabetic';

    drawSmallBtn(ctx, 'mic', '🎤', now, voiceWait);
    drawSmallBtn(ctx, 'go', '🔍', now, false);
    drawSmallBtn(ctx, 'clearKw', '清空', now, false);
  }

  /* ---------------- 绘制：平台 / 排序 chips ---------------- */
  function drawChips(ctx: CanvasRenderingContext2D, now: number) {
    for (const p of PLATFORMS) drawChip(ctx, 'plat-' + p.id, p.label, now, plat === p.id);
    for (const s of SORTS) drawChip(ctx, 'sort-' + s.id, s.label, now, sort === s.id);

    ctx.font = '500 15px ' + FONT;
    ctx.fillStyle = 'rgba(160,180,210,0.6)';
    ctx.textAlign = 'right';
    ctx.textBaseline = 'middle';
    const tip = loading ? '载入中…'
      : errMsg ? errMsg
        : view === 'search' ? (keyword ? '搜索：' + keyword : '还没搜过')
          : view === 'hot' ? '🔥 热门推荐'
            : view === 'fav' ? '📋 我的收藏' : '🕘 观看历史';
    ctx.fillText(fitTo(ctx, tip, 220, '500 15px ' + FONT), CV_W - 20, 129);
    ctx.textAlign = 'left';
    ctx.textBaseline = 'alphabetic';
  }

  /* ---------------- 绘制：网格 ---------------- */
  function drawGrid(ctx: CanvasRenderingContext2D, now: number) {
    const items = pageItems();
    if (loading && !items.length) {
      ctx.font = '500 20px ' + FONT;
      ctx.fillStyle = 'rgba(160,180,210,0.8)';
      ctx.textAlign = 'center';
      ctx.textBaseline = 'middle';
      ctx.fillText('载入中…', CV_W / 2, 320);
      ctx.textAlign = 'left';
      ctx.textBaseline = 'alphabetic';
      return;
    }
    if (!items.length) {
      ctx.font = '500 19px ' + FONT;
      ctx.fillStyle = 'rgba(160,180,210,0.8)';
      ctx.textAlign = 'center';
      ctx.textBaseline = 'middle';
      ctx.fillText(view === 'search' ? (keyword ? '没搜到相关视频，换个词试试' : '点 🎤 说片名开始搜索')
        : view === 'fav' ? '收藏夹是空的' : view === 'hist' ? '还没有观看记录' : '热门推荐暂时拿不到', CV_W / 2, 312);
      ctx.font = '500 16px ' + FONT;
      ctx.fillStyle = 'rgba(140,160,190,0.7)';
      ctx.fillText(view === 'fav' ? '退出 VR 在「在线视频」里点 ☆ 收藏' : '也可以直接对大白说「播放○○」', CV_W / 2, 348);
      ctx.textAlign = 'left';
      ctx.textBaseline = 'alphabetic';
      return;
    }
    for (let i = 0; i < items.length && i < PER_PAGE; i++) drawCard(ctx, i, items[i], now);
  }

  /* ---------------- 绘制：底栏 ---------------- */
  function drawFooter(ctx: CanvasRenderingContext2D, now: number) {
    const total = pageCount();
    ctx.font = '600 17px ' + FONT;
    ctx.fillStyle = '#8c8ca0';
    ctx.textAlign = 'left';
    ctx.textBaseline = 'middle';
    ctx.fillText('第 ' + (page + 1) + ' / ' + total + ' 页 · 点卡片即在大屏播放', 20, 546);
    ctx.textBaseline = 'alphabetic';
    drawSmallBtn(ctx, 'pageUp', '◀', now, false);
    drawSmallBtn(ctx, 'pageDown', '▶', now, false);
  }

  /* ---------------- 总绘制 ---------------- */
  function draw() {
    const ctx = vv.ctx;
    if (!ctx) return;
    const now = performance.now();
    ctx.clearRect(0, 0, CV_W, CV_H);
    vv.buttons = buildButtons();  // 布局随条目数重建（按钮表与画面同步）
    roundRect(ctx, 4, 4, CV_W - 8, CV_H - 8, 20);
    ctx.fillStyle = 'rgba(6, 9, 20, 0.9)';
    ctx.fill();
    ctx.lineWidth = 2;
    ctx.strokeStyle = 'rgba(0,229,255,0.28)';
    ctx.stroke();

    drawTopBar(ctx, now);
    drawSearchRow(ctx, now);
    drawChips(ctx, now);
    drawGrid(ctx, now);
    drawFooter(ctx, now);
    if (vv.tex) vv.tex.needsUpdate = true;
  }

  /* ---------------- 数据加载 ---------------- */
  let reqSeq = 0;   // 请求序号：换源/换页/换视图后，过期响应一律丢弃
  let pendingPage = -1;    // 载入中又翻页：记下目标页，等当前请求落地再拉

  function resetPager() {
    page = 0;
    remote = new Map();
    pool = [];
    hasMore = true;
    errMsg = '';
    pendingPage = -1;
    reqSeq++;
  }

  async function loadRemote(p: number) {
    if (view !== 'search' && view !== 'hot') return;
    if (loading || remote.has(p)) return;
    if (p > 0 && !hasMore) return;
    if (view === 'search' && !keyword) { vv.dirty = true; return; }
    loading = true;
    errMsg = '';
    vv.dirty = true;
    const my = ++reqSeq;
    try {
      const params = new URLSearchParams({ platform: plat, limit: String(PAGE_SIZE), page: String(p + 1) });
      let url: string;
      if (view === 'hot') {
        url = '/api/video_hub/api/hot?' + params.toString();
      } else {
        params.set('q', keyword);
        params.set('sort', sort);
        url = '/api/video_hub/api/search?' + params.toString();
      }
      const res = await fetch(url);
      if (!res.ok) throw new Error('HTTP ' + res.status);
      const data = await res.json();
      if (my !== reqSeq) return;   // 已换源/换页/换视图：丢弃过期结果
      remote.set(p, (data.results || []) as VideoItem[]);
      hasMore = data.has_more !== false;
    } catch (e) {
      if (my === reqSeq) errMsg = '加载失败，点「刷新」重试';
    } finally {
      if (my === reqSeq) {
        loading = false;
        vv.dirty = true;
        if (pendingPage >= 0) {
          const pp = pendingPage;
          pendingPage = -1;
          loadRemote(pp);   // 载入中翻的页：现在补上
        }
      }
    }
  }

  async function loadPool(kind: 'fav' | 'hist') {
    loading = true;
    errMsg = '';
    vv.dirty = true;
    const my = ++reqSeq;
    try {
      const url = kind === 'fav' ? '/api/video_hub/api/favorites' : '/api/video_hub/api/history';
      const res = await fetch(url);
      if (!res.ok) throw new Error('HTTP ' + res.status);
      const data = await res.json();
      if (my !== reqSeq) return;
      const arr: any[] = kind === 'fav' ? (data.favorites || []) : (data.history || []);
      pool = arr.map((x: any) => (kind === 'fav' ? (x.video || {}) : (x.video || {}))) as VideoItem[];
    } catch (e) {
      if (my === reqSeq) errMsg = kind === 'fav' ? '收藏夹加载失败' : '观看历史加载失败';
    } finally {
      if (my === reqSeq) { loading = false; vv.dirty = true; }
    }
  }

  function refreshCurrent() {
    if (view === 'fav') loadPool('fav');
    else if (view === 'hist') loadPool('hist');
    else loadRemote(page);
  }

  /** 换源/换排序：清分页后按新条件重载当前视图 */
  function reloadForFilter() {
    resetPager();
    vv.dirty = true;
    refreshCurrent();
  }

  function switchView(v: View) {
    if (view === v) return;
    view = v;
    resetPager();
    vv.dirty = true;
    if (v === 'fav') loadPool('fav');
    else if (v === 'hist') loadPool('hist');
    else loadRemote(0);
  }

  /* ---------------- 语音输入（头显里唯一的「打字」方式） ---------------- */
  const FILLER = /^(帮我|请|麻烦|我想|我要|给我|来|搜索|搜一下|搜|查找|查一下|查|找一下|找|播放|放一下|放|看|想看|视频|片子|电影|一部|个|的)+/;

  function cleanKeyword(s: string): string {
    let t = String(s || '').replace(/[，。！？、,.!?；;：:""''（）()【】\[\]]/g, ' ').replace(/\s+/g, ' ').trim();
    t = t.replace(FILLER, '').trim();
    t = t.replace(/(的)?(视频|片子|电影)$/, '').trim();
    return t;
  }

  function startVoice() {
    voiceWait = !voiceWait;
    vv.dirty = true;
    App.showToast(voiceWait ? '🎤 请说出要搜的视频' : '已取消语音输入');
  }

  /** websocket transcript → 关键词（只在「待听」状态下吃这一句，其余交给 AI） */
  function onVoiceText(text: string) {
    if (!voiceWait) return;
    voiceWait = false;
    const kw = cleanKeyword(text);
    vv.dirty = true;
    if (!kw) { App.showToast('没听清，再点 🎤 说一次'); return; }
    keyword = kw;
    view = 'search';
    resetPager();
    App.showToast('搜索：' + kw);
    loadRemote(0);
  }

  /* ---------------- 命中动作 ---------------- */
  async function playIndex(i: number) {
    const it = pageItems()[i];
    if (!it || !it.webpage_url) { App.showToast('这个视频没有可播放的链接'); return; }
    App.showToast('正在解析《' + (it.title || '') + '》…');
    try {
      await App.playVideoItem(it);
    } catch (e) {
      App.showToast('播放失败，换一部试试');
    }
  }

  async function toggleFav(i: number) {
    const it = pageItems()[i];
    if (!it || !it.webpage_url) { App.showToast('这个视频没有可收藏的链接'); return; }
    const fid = App.isVideoFavorited ? App.isVideoFavorited(it.webpage_url) : null;
    try {
      if (fid) { await App.removeVideoFavorite(fid); App.showToast('已取消收藏'); }
      else { await App.addVideoFavorite(it); App.showToast('已加入收藏夹'); }
    } catch (e) {
      App.showToast('收藏操作失败');
    }
    vv.dirty = true;
  }

  function goPage(p: number) {
    if (p < 0 || p >= pageCount() || p === page) return;
    page = p;
    vv.dirty = true;
    if ((view === 'search' || view === 'hot') && !remote.has(p)) {
      if (loading) pendingPage = p;
      else loadRemote(p);
    }
  }

  vv.trigger = function trigger(id: string) {
    if (!vv.active || !vv.open) return;
    vv.flash = { id: id.split(':')[0], until: performance.now() + 260 };
    vv.dirty = true;

    if (id === 'close') { vv.toggle(); return; }
    if (id === 'mic') { startVoice(); return; }
    if (id === 'clearKw') { keyword = ''; resetPager(); refreshCurrent(); return; }
    if (id === 'go') {
      if (!keyword) { startVoice(); return; }
      view = 'search';
      resetPager();
      loadRemote(0);
      return;
    }
    if (id === 'refresh') { refreshCurrent(); App.showToast('已刷新'); return; }
    if (id === 'tabSearch') { switchView('search'); return; }
    if (id === 'tabHot') { switchView('hot'); return; }
    if (id === 'tabFav') { switchView('fav'); return; }
    if (id === 'tabHist') { switchView('hist'); return; }
    if (id.indexOf('plat-') === 0) {
      const p = id.slice(5);
      if (p !== plat) { plat = p; reloadForFilter(); }
      return;
    }
    if (id.indexOf('sort-') === 0) {
      const s = id.slice(5);
      if (s !== sort) { sort = s; reloadForFilter(); }
      return;
    }
    if (id === 'pageUp') { goPage(page - 1); return; }
    if (id === 'pageDown') { goPage(page + 1); return; }
    if (id.indexOf('fav-') === 0) { toggleFav(Number(id.slice(4))); return; }
    if (id.indexOf('card-') === 0) { playIndex(Number(id.slice(5))); return; }
  };

  /* ---------------- 命中测试：射线 → 按钮 id ---------------- */
  /** 视线吸附：小按钮附近（30px）都算命中；卡片本身很大，按矩形精确命中 */
  function nearestItem(px: number, py: number, snapR?: number): string {
    let best = '';
    let bestD = Infinity;
    for (const b of vv.buttons) {
      if (b.w >= 100 || b.h >= 60) continue;
      const dx = Math.max(b.x - px, 0, px - (b.x + b.w));
      const dy = Math.max(b.y - py, 0, py - (b.y + b.h));
      const d = Math.hypot(dx, dy);
      if (d < bestD) { bestD = d; best = b.id; }
    }
    // 面板内只在按钮附近吸附（30px）；面板外的磁力带放宽，射线擦边也能锁住
    const lim = snapR == null ? 30 : snapR;
    return bestD <= lim ? best : '';
  }

  /** 命中口径：面板矩形 + 外扩磁力带（vr-ray 统一仲裁，跨面板互斥） */
  const probe = vrRayPanel({
    id: 'video',
    active: () => !!(vv.active && vv.open && vv.mesh && vv.mesh.visible),
    mesh: () => vv.mesh,
    cvW: () => CV_W,
    cvH: () => CV_H,
    hit: (px, py, loose, inside) => {
      // ☆ 叠在卡片上，先查它（否则永远被卡片吃掉）
      for (const b of vv.buttons) {
        if (b.id.indexOf('fav-') !== 0) continue;
        if (px >= b.x - 4 && px <= b.x + b.w + 4 && py >= b.y - 4 && py <= b.y + b.h + 4) return b.id;
      }
      for (const b of vv.buttons) {
        if (px >= b.x - 6 && px <= b.x + b.w + 6 && py >= b.y - 6 && py <= b.y + b.h + 6) return b.id;
      }
      if (!loose) return null;
      return nearestItem(px, py, inside ? undefined : 260) || null;
    },
    trigger: (id) => vv.trigger(id)
  });
  App.vrRay?.register(probe);

  vv.hitTest = function hitTest(origin: THREE.Vector3, dir: THREE.Vector3, snap?: boolean) {
    const r = probe.pick(origin, dir, !!snap);
    return r ? r.id : null;
  };

  /* ---------------- 对外：显示/隐藏/更新 ---------------- */
  vv.show = function show() {
    ensureScene();
    vv.active = true;
    vv.open = false;   // 每次进 VR 不自动弹面板：需要时从工具栏呼出
    anchor = null;
    voiceWait = false;
    resetPager();
    vv.dirty = true;
    if (vv.mesh) vv.mesh.visible = false;
  };

  vv.hide = function hide() {
    vv.active = false;
    vv.open = false;
    anchor = null;
    voiceWait = false;
    if (vv.mesh) vv.mesh.visible = false;
  };

  vv.markDirty = function markDirty() { vv.dirty = true; };

  /** 呼出/收起面板（vr-toolbar 的 video-btn 调它） */
  vv.toggle = function toggle() {
    vv.open = !vv.open;
    vv.dirty = true;
    if (vv.open) {
      anchor = null;             // 每次呼出重新挂到视线正前方
      if (!pageItems().length && !loading) refreshCurrent();
      App.showToast('在线视频 · 点 🎤 说片名，点卡片播放');
    }
    if (vv.mesh) vv.mesh.visible = vv.open;
  };

  /* ---------------- 每帧：软锚定定位 + 视线吸附 + 重绘 ---------------- */
  const gazeDir = new THREE.Vector3();

  /** 本面板锁定：全局仲裁里锁到的是我，就返回按钮 id（跨面板互斥） */
  function mine(o: THREE.Vector3, d: THREE.Vector3, loose: boolean): string {
    if (App.vrRay) return App.vrRay.pickMine('video', o, d, loose);
    const r = probe.pick(o, d, loose);
    return r ? r.id : '';
  }

  function updateGaze() {
    let id = '';
    const r = App.renderer;
    if (r && r.xr && r.xr.isPresenting) {
      try {
        if (App._xrEyeRay(vv._v3, gazeDir)) id = mine(vv._v3, gazeDir, true);
      } catch (_) { /* 取不到就只留手柄射线 hover */ }
    }
    if (id !== vv.hoverId) { vv.hoverId = id; vv.dirty = true; }
  }

  vv.update = function update(dt: number) {
    if (!vv.active || !vv.mesh) return;
    const hp = App._xrHeadPos;
    if (!hp) return;

    if (!vv.open) {
      if (vv.mesh.visible) vv.mesh.visible = false;
      return;
    }
    vv.mesh.visible = true;

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

    // 走远 / 绕到面板侧后方：重挂一次（跳变），否则只剩一块斜着的面板
    if (anchor) {
      const tx = anchor.x - hp.x, tz = anchor.z - hp.z;
      const tl = Math.hypot(tx, tz) || 1;
      if (tl > REACH || (tx / tl) * lockDir.x + (tz / tl) * lockDir.z < OFF_COS) anchor = null;
    }
    if (!anchor) {
      const yaw = Math.atan2(hx, hz);
      fdirX = Math.sin(yaw); fdirZ = Math.cos(yaw);
      anchor = new THREE.Vector3(
        hp.x + fdirX * DIST,
        hp.y + (App._xrEyeOffY || 0) - 0.06,
        hp.z + fdirZ * DIST
      );
      vv.mesh.position.copy(anchor);
      // 朝向只定这一次：每帧 lookAt 会让按钮随头部追踪噪声滑动，瞄不住也点不中
      vv.mesh.lookAt(hp.x, hp.y - 0.06, hp.z);
      lockDir.set(0, 0, -1).applyQuaternion(vv.mesh.quaternion);
    }
    // 软锚定：死区内不动（不晃、按钮不滑），偏出死区按限速缓缓追上（走动/升降不丢面板）
    idealPos.set(
      hp.x + fdirX * DIST,
      hp.y + (App._xrEyeOffY || 0) - 0.06,
      hp.z + fdirZ * DIST
    );
    const dd = anchor.distanceTo(idealPos);
    if (dd > FOLLOW_DEAD) {
      const v = Math.min(dd * FOLLOW_GAIN, FOLLOW_MAX);
      anchor.lerp(idealPos, Math.min(1, (v * dt) / dd));
    }
    vv.mesh.position.copy(anchor);

    updateGaze();
    if (vv.dirty) { vv.dirty = false; draw(); }
  };
  App.updateVrVideo = vv.update;

  /* ---------------- 与非 VR 打通 ---------------- */
  // 1) 进入/退出 VR 时显示/隐藏（游戏模式 VR 由游戏自理，沿用 vr-hud 的判定）
  const _enter = App.enterXrMode;
  App.enterXrMode = async function (...args: any[]) {
    const res = await _enter.apply(this, args);
    vv.show();
    return res;
  };
  const _exit = App.exitXrMode;
  App.exitXrMode = function (...args: any[]) {
    vv.hide();
    return _exit.apply(this, args);
  };

  // 2) 语音关键词：websocket 收到 transcript 时调它（待听状态才吃这一句）
  App._vrVideoVoiceHook = onVoiceText;

  // 3) XR 手柄扳机：先命中视频面板，未命中才回落到下层面板/戳角色
  const _selStart = App._onControllerSelectStart;
  App._onControllerSelectStart = function (e: any) {
    const c = e && e.target;
    if (vv.active && vv.open && c) {
      try {
        const o = vv._v3, d = new THREE.Vector3();
        if (App._xrCtrlRay(c, o, d, vv._q)) {
          const id = mine(o, d, true);
          if (id) { vv.trigger(id); return; }
        }
      } catch (_) { /* 忽略异常，回退到下层 */ }
    }
    return _selStart.apply(this, arguments as any);
  };

  // 4) 蓝牙手柄/兜底按键：视中心射线先命中视频面板，未命中才回落到原逻辑
  const _padClick = App._xrPadClick;
  App._xrPadClick = function () {
    if (vv.active && vv.open && !this._xrGameMode) {
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
      const id = mine(origin, dir, true);
      if (id) { vv.trigger(id); return; }
    }
    return _padClick.apply(this, arguments as any);
  };
}
