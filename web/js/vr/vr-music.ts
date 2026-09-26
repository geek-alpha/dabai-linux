/* ============================================================
 * web/js/vr/vr-music.ts —— VR 世界内在线音乐面板（模块 32）
 * ------------------------------------------------------------
 * 网页端的「在线音乐」是一整块 DOM 弹窗（28_music_ui），沉浸态里完全
 * 不可见 —— 头显里选歌/换歌/调音量都够不着。本模块把这块面板在 VR 世界
 * 里重建一份，数据与播放仍走同一套管线：/api/music/* 取榜单与歌单、
 * App.playMusicSong 点播、App.getBGMState/onBGMStateChange 同步状态、
 * App.seekBGM/setBGMVolume 控制，不另起一套播放器。
 *
 * 布局（画布 520×440，世界宽 1.2m，呼出时立在视线正前方）：
 *   顶栏   标题 + [榜单][歌单] 切换 + ✕
 *   播放卡 歌名 / 状态徽章 / 进度条（点按跳转）
 *   控制行 ⏮ ⏯ ⏭ 🔉− 🔊＋ ⏹
 *   列表   榜单→歌曲 或 歌单→歌曲，4 行一页，点行即点播
 *
 * 稳定性（头显里最要紧的是「不晃」）：位置软锚定 —— 死区内不动，偏出
 * 死区按限速追上；朝向只在挂锚点时 lookAt 一次，之后锁死。每帧 lookAt
 * 或每帧跟头都会让按钮随头部追踪噪声滑动，瞄不住也点不中。
 *
 * 交互：射线命中（带视线吸附，头显里不必精确对准）+ 扳机/确认键；
 * 由 vr-toolbar 的 music-btn 呼出，其余射线回落到下层面板。
 * ============================================================ */
import * as THREE from 'three';
import type { AppKernel, VrMusic, VrHudButton, MusicSong, BGMState } from '../types/app-kernel.js';
import { vrRayPanel } from './vr-ray.js';

export default function initVrMusic(App: AppKernel) {
  if (App.vrMusic) return; // 幂等：避免重复初始化/重复包裹

  const CV_W = 520;
  const CV_H = 440;
  const WORLD_W = 1.2;                  // 面板世界宽（米）
  const PER_PAGE = 4;                   // 列表每页行数
  const ROW_Y0 = 280;                   // 首行 y
  const ROW_H = 37;                     // 行距
  const ROW_HH = 34;                    // 行高
  const SEEK = { x: 24, y: 126, w: 472, h: 22 }; // 进度条命中区
  const PROG_REDRAW_MS = 500;           // 播放中进度条重绘间隔
  const FONT = '"Microsoft YaHei", "PingFang SC", "Noto Sans SC", sans-serif';

  /* 呼出面板的摆位参数（照抄 vr-toolbar 已验证的软锚定量级） */
  const DIST = 1.75;                    // 呼出距离（米）：1.25 时 1.2m 宽面板占视野 51°（贴脸），1.75 降到 38°
  const REACH = 2.8;                    // 走远到此距离重挂
  const OFF_COS = 0.55;                 // 绕到面板侧后方（余弦低于此）重挂
  const FOLLOW_DEAD = 0.10;             // 软锚定死区：此范围内不跟（不晃）
  const FOLLOW_GAIN = 1.8;
  const FOLLOW_MAX = 1.2;

  const STATE_COLOR: Record<string, string> = {
    idle: '#4ade80', thinking: '#fbbf24', listening: '#38bdf8', speaking: '#f87171'
  };

  /* ---------------- 面板对象 ---------------- */
  const vm = App.vrMusic = {
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
  } as VrMusic;

  /* ---------------- 数据与视图状态 ---------------- */
  type View = 'boards' | 'playlists' | 'songs';
  let view: View = 'boards';
  let prevView: View = 'boards';        // songs 视图的返回目标
  let items: any[] = [];                // 当前列表项（榜单 / 歌单）
  let songs: MusicSong[] = [];          // songs 视图的歌曲（也是 VR 面板的播放队列）
  let listTitle = '';
  let page = 0;
  let loading = false;
  let errMsg = '';
  let qi = -1;                          // 队列当前索引
  let vrQueue = false;                  // 队列由 VR 面板发起（自然播完自动续下一首）
  let bgm: BGMState | null = null;
  let lastProgAt = 0;

  /* ---------------- 定位状态 ---------------- */
  let anchor: THREE.Vector3 | null = null;
  const lockDir = new THREE.Vector3(0, 0, -1);
  const idealPos = new THREE.Vector3();
  const fwd = new THREE.Vector3();
  const head = new THREE.Vector3();
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

  function fmtT(s: number): string {
    s = Math.max(0, Math.floor(s || 0));
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), ss = s % 60;
    return h ? h + ':' + String(m).padStart(2, '0') + ':' + String(ss).padStart(2, '0')
             : m + ':' + String(ss).padStart(2, '0');
  }

  /* ---------------- 按钮表（画布像素坐标，随视图重建） ---------------- */
  function buildButtons(): VrHudButton[] {
    const btns: VrHudButton[] = [
      { id: 'tabBoards', x: 176, y: 14, w: 84, h: 36 },
      { id: 'tabPlaylists', x: 268, y: 14, w: 84, h: 36 },
      { id: 'close', x: CV_W - 50, y: 13, w: 38, h: 38 },
      { id: 'prev', x: 24, y: 176, w: 73, h: 56 },
      { id: 'toggle', x: 103, y: 176, w: 73, h: 56 },
      { id: 'next', x: 182, y: 176, w: 73, h: 56 },
      { id: 'vol-', x: 261, y: 176, w: 73, h: 56 },
      { id: 'vol+', x: 340, y: 176, w: 73, h: 56 },
      { id: 'stop', x: 419, y: 176, w: 73, h: 56 }
    ];
    if (view === 'songs') btns.push({ id: 'back', x: 24, y: 238, w: 104, h: 32 });
    if (items.length > PER_PAGE || view === 'songs') {
      btns.push({ id: 'pageUp', x: 396, y: 240, w: 44, h: 28 });
      btns.push({ id: 'pageDown', x: 448, y: 240, w: 44, h: 28 });
    }
    const rows = view === 'songs' ? songs.length : items.length;
    for (let i = 0; i < PER_PAGE; i++) {
      if (rows > 0 && page * PER_PAGE + i >= rows) break;
      btns.push({ id: 'item-' + i, x: 24, y: ROW_Y0 + i * ROW_H, w: 472, h: ROW_HH });
    }
    return btns;
  }

  /* ---------------- 场景构建 ---------------- */
  function ensureScene() {
    if (vm.mesh) return;
    const cv = document.createElement('canvas');
    cv.width = CV_W;
    cv.height = CV_H;
    vm.cv = cv;
    vm.ctx = cv.getContext('2d');

    const tex = new THREE.CanvasTexture(cv);
    tex.colorSpace = THREE.SRGBColorSpace;
    tex.minFilter = THREE.LinearFilter;
    vm.tex = tex;

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
    mesh.renderOrder = 510; // 压在 vr-hud（500）之上：呼出面板要在最前面
    mesh.visible = false;
    App.scene!.add(mesh);
    vm.mesh = mesh;
    vm.buttons = buildButtons();
    vm.dirty = true;
  }

  function btnById(id: string) {
    return vm.buttons.find(b => b.id === id);
  }

  /* ---------------- 绘制：通用按钮 ---------------- */
  // 小图标按钮（顶栏 tab / 关闭 / 翻页 / 返回）
  function drawSmallBtn(ctx: CanvasRenderingContext2D, id: string, label: string, now: number, active: boolean) {
    const b = btnById(id);
    if (!b) return;
    const flashed = !!(vm.flash && vm.flash.id === id && now < vm.flash.until);
    const hovered = vm.hoverId === id;
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
    ctx.font = 'bold 19px ' + FONT;
    ctx.fillStyle = (flashed || active) ? '#ffffff' : 'rgba(232,246,255,0.85)';
    ctx.fillText(label, b.x + b.w / 2, b.y + b.h / 2 + 1);
    ctx.textAlign = 'left';
    ctx.textBaseline = 'alphabetic';
  }

  // 控制行大按钮
  function drawBigBtn(ctx: CanvasRenderingContext2D, id: string, label: string, now: number) {
    const b = btnById(id);
    if (!b) return;
    const flashed = !!(vm.flash && vm.flash.id === id && now < vm.flash.until);
    const hovered = vm.hoverId === id;
    roundRect(ctx, b.x, b.y, b.w, b.h, 12);
    ctx.fillStyle = flashed ? 'rgba(0,229,255,0.32)'
      : hovered ? 'rgba(255,255,255,0.15)' : 'rgba(255,255,255,0.07)';
    ctx.fill();
    ctx.lineWidth = flashed ? 2.5 : 1.2;
    ctx.strokeStyle = flashed ? 'rgba(0,229,255,0.9)' : 'rgba(255,255,255,0.22)';
    ctx.stroke();
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.font = 'bold 23px ' + FONT;
    ctx.fillStyle = flashed ? '#ffffff' : '#e8f2ff';
    ctx.fillText(label, b.x + b.w / 2, b.y + b.h / 2 + 1);
    ctx.textAlign = 'left';
    ctx.textBaseline = 'alphabetic';
  }

  // 列表行（左对齐文字 + 右侧副标题）
  function drawRow(ctx: CanvasRenderingContext2D, id: string, label: string, sub: string,
                   now: number, active: boolean) {
    const b = btnById(id);
    if (!b) return;
    const flashed = !!(vm.flash && vm.flash.id === id && now < vm.flash.until);
    const hovered = vm.hoverId === id;
    roundRect(ctx, b.x, b.y, b.w, b.h, 10);
    ctx.fillStyle = flashed ? 'rgba(0,229,255,0.32)'
      : active ? 'rgba(0,229,255,0.16)'
      : hovered ? 'rgba(255,255,255,0.13)' : 'rgba(255,255,255,0.06)';
    ctx.fill();
    ctx.lineWidth = 1.2;
    ctx.strokeStyle = active ? 'rgba(0,229,255,0.7)' : 'rgba(255,255,255,0.18)';
    ctx.stroke();
    ctx.textBaseline = 'middle';
    ctx.textAlign = 'left';
    ctx.font = '600 19px ' + FONT;
    ctx.fillStyle = active ? '#8ff3ff' : '#e8f2ff';
    ctx.fillText(fitTo(ctx, label, sub ? 356 : 440, '600 19px ' + FONT), b.x + 14, b.y + b.h / 2 + 1);
    if (sub) {
      ctx.font = '500 15px ' + FONT;
      ctx.fillStyle = 'rgba(160,180,210,0.7)';
      ctx.textAlign = 'right';
      ctx.fillText(sub, b.x + b.w - 12, b.y + b.h / 2 + 1);
    }
    ctx.textAlign = 'left';
    ctx.textBaseline = 'alphabetic';
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
    const st = App.currentState || 'idle';
    ctx.beginPath();
    ctx.arc(30, 32, 7, 0, Math.PI * 2);
    ctx.fillStyle = STATE_COLOR[st] || '#4ade80';
    ctx.fill();
    ctx.font = 'bold 20px ' + FONT;
    ctx.fillStyle = '#f2f6ff';
    ctx.fillText('🎵 在线音乐', 46, 33);

    drawSmallBtn(ctx, 'tabBoards', '榜单', now, view !== 'playlists');
    drawSmallBtn(ctx, 'tabPlaylists', '歌单', now, view === 'playlists');
    drawSmallBtn(ctx, 'close', '✕', now, false);
  }

  /* ---------------- 绘制：正在播放卡 ---------------- */
  function drawNowPlaying(ctx: CanvasRenderingContext2D, now: number) {
    roundRect(ctx, 8, 64, CV_W - 16, 104, 16);
    ctx.fillStyle = 'rgba(8, 12, 26, 0.88)';
    ctx.fill();
    ctx.lineWidth = 2;
    ctx.strokeStyle = 'rgba(124, 92, 255, 0.5)';
    ctx.stroke();

    const active = !!(bgm && bgm.name && !bgm.stopped);
    ctx.textBaseline = 'middle';
    ctx.textAlign = 'left';
    ctx.font = 'bold 21px ' + FONT;
    ctx.fillStyle = '#ffffff';
    ctx.fillText('♫ ' + fitTo(ctx, active ? String(bgm!.name) : '未在播放', 310, 'bold 21px ' + FONT), 24, 92);

    // 状态徽章
    let text = '未播放', color = '#8c8ca0';
    if (active) {
      if (bgm!.playing) { text = '播放中'; color = '#4ade80'; }
      else if (bgm!.paused) { text = '已暂停'; color = '#ffd54f'; }
      else { text = '已停止'; color = '#8c8ca0'; }
    }
    roundRect(ctx, CV_W - 152, 76, 128, 30, 15);
    ctx.fillStyle = 'rgba(255,255,255,0.08)';
    ctx.fill();
    ctx.font = '600 17px ' + FONT;
    ctx.fillStyle = color;
    ctx.textAlign = 'center';
    ctx.fillText(text, CV_W - 88, 92);

    // 进度条（可点按跳转）
    const dur = (bgm && bgm.duration) || 0;
    const cur = (bgm && bgm.currentTime) || 0;
    const frac = dur > 0 ? Math.min(1, Math.max(0, cur / dur)) : 0;
    const barY = SEEK.y + 4, barH = 14;
    roundRect(ctx, SEEK.x, barY, SEEK.w, barH, barH / 2);
    ctx.fillStyle = 'rgba(255,255,255,0.12)';
    ctx.fill();
    if (frac > 0) {
      roundRect(ctx, SEEK.x, barY, Math.max(barH, SEEK.w * frac), barH, barH / 2);
      const g = ctx.createLinearGradient(SEEK.x, 0, SEEK.x + SEEK.w, 0);
      g.addColorStop(0, '#00e5ff');
      g.addColorStop(1, '#7c5cff');
      ctx.fillStyle = g;
      ctx.fill();
    }
    ctx.beginPath();
    ctx.arc(SEEK.x + SEEK.w * frac, barY + barH / 2, 9, 0, Math.PI * 2);
    ctx.fillStyle = '#ffffff';
    ctx.fill();

    ctx.font = '600 17px ' + FONT;
    ctx.fillStyle = '#e8f2ff';
    ctx.textAlign = 'center';
    ctx.fillText(dur > 0 ? fmtT(cur) + ' / ' + fmtT(dur) : '— 时长未知 · 暂不支持拖动 —',
      CV_W / 2, 156);
    ctx.textAlign = 'left';
    ctx.textBaseline = 'alphabetic';
  }

  /* ---------------- 绘制：控制行 ---------------- */
  function drawControls(ctx: CanvasRenderingContext2D, now: number) {
    const playing = !!(bgm && bgm.playing);
    drawBigBtn(ctx, 'prev', '⏮', now);
    drawBigBtn(ctx, 'toggle', playing ? '⏸' : '▶', now);
    drawBigBtn(ctx, 'next', '⏭', now);
    drawBigBtn(ctx, 'vol-', '🔉−', now);
    drawBigBtn(ctx, 'vol+', '🔊＋', now);
    drawBigBtn(ctx, 'stop', '⏹', now);
  }

  /* ---------------- 绘制：列表区 ---------------- */
  function drawList(ctx: CanvasRenderingContext2D, now: number) {
    ctx.textBaseline = 'middle';
    ctx.textAlign = 'left';
    const inSongs = view === 'songs';
    const title = inSongs ? '《' + listTitle + '》'
      : view === 'boards' ? '热门榜单' : '我的歌单';
    const tx = inSongs ? 140 : 24;
    ctx.font = 'bold 20px ' + FONT;
    ctx.fillStyle = inSongs ? '#b39ddb' : '#e8f2ff';
    ctx.fillText(fitTo(ctx, title, inSongs ? 240 : 300, 'bold 20px ' + FONT), tx, 254);
    if (inSongs) drawSmallBtn(ctx, 'back', '⬅ 返回', now, false);

    const rows = inSongs ? songs.length : items.length;
    const pages = Math.max(1, Math.ceil(rows / PER_PAGE));
    if (rows > PER_PAGE || inSongs) {
      ctx.font = '600 17px ' + FONT;
      ctx.fillStyle = '#8c8ca0';
      ctx.textAlign = 'center';
      ctx.fillText((page + 1) + '/' + pages, 370, 254);
      drawSmallBtn(ctx, 'pageUp', '◀', now, page > 0);
      drawSmallBtn(ctx, 'pageDown', '▶', now, (page + 1) * PER_PAGE < rows);
    }

    if (loading) {
      ctx.font = '500 20px ' + FONT;
      ctx.fillStyle = '#8c8ca0';
      ctx.textAlign = 'center';
      ctx.fillText('载入中…', CV_W / 2, 360);
      ctx.textAlign = 'left';
      ctx.textBaseline = 'alphabetic';
      return;
    }
    if (errMsg) {
      ctx.font = '500 19px ' + FONT;
      ctx.fillStyle = '#ff8f8f';
      ctx.textAlign = 'center';
      ctx.fillText(fitTo(ctx, errMsg, 460, '500 19px ' + FONT), CV_W / 2, 360);
      ctx.textAlign = 'left';
      ctx.textBaseline = 'alphabetic';
      return;
    }
    if (!rows) {
      ctx.font = '500 19px ' + FONT;
      ctx.fillStyle = '#8c8ca0';
      ctx.textAlign = 'center';
      ctx.fillText(inSongs ? '这里暂时没有可播放的歌曲' : '列表是空的', CV_W / 2, 340);
      if (!inSongs) {
        ctx.fillText('也可以对大白说「播放○○」直接点播', CV_W / 2, 372);
      }
      ctx.textAlign = 'left';
      ctx.textBaseline = 'alphabetic';
      return;
    }

    for (let i = 0; i < PER_PAGE; i++) {
      const idx = page * PER_PAGE + i;
      const id = 'item-' + i;
      if (inSongs) {
        const s = songs[idx];
        if (!s) continue;
        drawRow(ctx, id, (idx + 1) + '. ' + (s.name || '未知') + (s.artists ? ' - ' + s.artists : ''),
          '', now, idx === qi);
      } else {
        const it = items[idx];
        if (!it) continue;
        drawRow(ctx, id, it.name || '未知', it.sub || '', now, false);
      }
    }
    ctx.textAlign = 'left';
    ctx.textBaseline = 'alphabetic';
  }

  /* ---------------- 总绘制 ---------------- */
  function draw() {
    const ctx = vm.ctx;
    if (!ctx) return;
    const now = performance.now();
    ctx.clearRect(0, 0, CV_W, CV_H);
    vm.buttons = buildButtons(); // 布局随视图重建（按钮表与画面同步）
    drawTopBar(ctx, now);
    drawNowPlaying(ctx, now);
    drawControls(ctx, now);
    drawList(ctx, now);
    if (vm.tex) vm.tex.needsUpdate = true;
  }

  /* ---------------- 数据：榜单 / 歌单 / 歌曲列表 ---------------- */
  async function loadBoards() {
    loading = true; errMsg = ''; items = []; page = 0; vm.dirty = true;
    try {
      const res = await fetch('/api/music/boards');
      if (!res.ok) throw new Error('HTTP ' + res.status);
      const data = await res.json();
      items = (data.boards || []).map((b: any) => ({
        _kind: 'board', id: b.id, name: b.name,
        sub: b.song_count != null ? b.song_count + ' 首' : ''
      }));
      if (!items.length) errMsg = '暂时拿不到榜单';
    } catch (e) {
      errMsg = '榜单加载失败';
    } finally {
      loading = false;
      vm.dirty = true;
    }
  }

  async function loadPlaylists() {
    loading = true; errMsg = ''; items = []; page = 0; vm.dirty = true;
    try {
      const res = await fetch('/api/music/playlists');
      if (!res.ok) throw new Error('HTTP ' + res.status);
      const data = await res.json();
      items = (data.playlists || []).map((p: any) => ({
        _kind: 'playlist', id: p.id, name: p.name,
        sub: (p.song_count != null ? p.song_count : 0) + ' 首'
      }));
      if (!items.length) errMsg = '还没有歌单（可在网页端「在线音乐」里创建）';
    } catch (e) {
      errMsg = '歌单加载失败';
    } finally {
      loading = false;
      vm.dirty = true;
    }
  }

  /** 榜单/歌单 → 歌曲列表（同时成为 VR 面板的播放队列） */
  async function openList(it: any) {
    if (!it || !it.id) return;
    loading = true; errMsg = ''; songs = []; page = 0; qi = -1; vrQueue = false; vm.dirty = true;
    try {
      const url = it._kind === 'board'
        ? '/api/music/boards/' + encodeURIComponent(it.id)
        : '/api/music/playlists/' + encodeURIComponent(it.id);
      const res = await fetch(url);
      if (!res.ok) throw new Error('HTTP ' + res.status);
      const data = await res.json();
      const arr = it._kind === 'board'
        ? ((data.board && data.board.songs) || [])
        : ((data.playlist && data.playlist.songs) || []);
      songs = arr.map((s: any) => ({
        source: s.source, id: String(s.id),
        name: s.name || '未知', artists: s.artists || ''
      }));
      listTitle = it.name || '';
      prevView = view;
      view = 'songs';
      if (!songs.length) errMsg = '这里暂时没有可播放的歌曲';
    } catch (e) {
      errMsg = '歌曲列表加载失败';
    } finally {
      loading = false;
      vm.dirty = true;
    }
  }

  function playIndex(i: number) {
    if (i < 0 || i >= songs.length) return;
    qi = i;
    vrQueue = true;
    vm.dirty = true;
    const s = songs[i];
    Promise.resolve(App.playMusicSong(s)).catch(() => { App.showToast('这首歌放不了，换一首试试'); });
  }

  function step(delta: number) {
    if (!songs.length) { App.showToast('先从榜单或歌单里选一首'); return; }
    if (qi < 0) { playIndex(0); return; }
    const n = qi + delta;
    if (n < 0) { App.showToast('已经是第一首'); return; }
    if (n >= songs.length) { App.showToast('已经是最后一首'); return; }
    playIndex(n);
  }

  function pullBgm() {
    bgm = App.getBGMState ? App.getBGMState() : null;
  }

  /* ---------------- 对外：显示/隐藏/更新 ---------------- */
  vm.show = function show() {
    ensureScene();
    vm.active = true;
    vm.open = false;   // 每次进 VR 不自动弹面板：需要时从工具栏呼出
    anchor = null;
    page = 0;
    pullBgm();
    vm.dirty = true;
    if (vm.mesh) vm.mesh.visible = false;
  };
  vm.hide = function hide() {
    vm.active = false;
    vm.open = false;
    anchor = null;
    if (vm.mesh) vm.mesh.visible = false;
  };
  vm.markDirty = function markDirty() { vm.dirty = true; };

  /** 呼出/收起面板（vr-toolbar 的 music-btn 调它） */
  vm.toggle = function toggle() {
    vm.open = !vm.open;
    vm.dirty = true;
    if (vm.open) {
      anchor = null;              // 每次呼出重新挂到视线正前方
      pullBgm();
      if (view === 'boards' && !items.length && !loading) loadBoards();
      if (view === 'playlists' && !items.length && !loading) loadPlaylists();
      App.showToast('在线音乐 · 用射线点歌');
    }
    if (vm.mesh) vm.mesh.visible = vm.open;
  };

  /* ---------------- 命中测试：射线 → 按钮 id / 进度条跳转 ---------------- */
  /** 视线吸附：离交点最近的按钮（不限矩形，半径内都算）—— 头显里视线不必精确对准 */
  function nearestItem(px: number, py: number, snapR?: number): string {
    let best = '';
    let bestD = Infinity;
    for (const b of vm.buttons) {
      const dx = Math.max(b.x - px, 0, px - (b.x + b.w));
      const dy = Math.max(b.y - py, 0, py - (b.y + b.h));
      const d = Math.hypot(dx, dy);
      if (d < bestD) { bestD = d; best = b.id; }
    }
    // 面板内只在按钮附近吸附（34px）：歌名/进度条那片不该误触发邻近按钮；
    // 面板外的磁力带（inside=false）则放宽，让射线擦边也能锁住
    const lim = snapR == null ? 34 : snapR;
    return bestD <= lim ? best : '';
  }

  /** 命中口径：面板矩形 + 外扩磁力带（vr-ray 统一仲裁，跨面板互斥） */
  const probe = vrRayPanel({
    id: 'music',
    active: () => !!(vm.active && vm.open && vm.mesh && vm.mesh.visible),
    mesh: () => vm.mesh,
    cvW: () => CV_W,
    cvH: () => CV_H,
    hit: (px, py, loose, inside) => {
      for (const b of vm.buttons) {
        if (px >= b.x - 6 && px <= b.x + b.w + 6 && py >= b.y - 6 && py <= b.y + b.h + 6) return b.id;
      }
      // 进度条：点按位置 → 跳转分数（trigger 内校验时长）
      if (px >= SEEK.x && px <= SEEK.x + SEEK.w && py >= SEEK.y && py <= SEEK.y + SEEK.h) {
        return 'seek:' + Math.max(0, Math.min(1, (px - SEEK.x) / SEEK.w)).toFixed(3);
      }
      if (!loose) return null;
      return nearestItem(px, py, inside ? undefined : 260) || null;
    },
    trigger: (id) => vm.trigger(id)
  });
  App.vrRay?.register(probe);

  vm.hitTest = function hitTest(origin: THREE.Vector3, dir: THREE.Vector3, snap?: boolean) {
    const r = probe.pick(origin, dir, !!snap);
    return r ? r.id : null;
  };

  /* ---------------- 命中动作 ---------------- */
  vm.trigger = function trigger(id: string) {
    if (!vm.active || !vm.open) return;
    vm.flash = { id: id.split(':')[0], until: performance.now() + 260 };
    vm.dirty = true;

    if (id === 'close') { vm.toggle(); return; }
    if (id === 'tabBoards') {
      if (view !== 'boards') { view = 'boards'; page = 0; if (!items.length) loadBoards(); }
      return;
    }
    if (id === 'tabPlaylists') {
      if (view !== 'playlists') { view = 'playlists'; page = 0; loadPlaylists(); }
      return;
    }
    if (id === 'back') {
      view = prevView === 'songs' ? 'boards' : prevView;
      page = 0;
      errMsg = '';
      if (!items.length) { if (view === 'playlists') loadPlaylists(); else loadBoards(); }
      return;
    }
    if (id === 'pageUp') { if (page > 0) { page--; vm.dirty = true; } return; }
    if (id === 'pageDown') {
      const rows = view === 'songs' ? songs.length : items.length;
      if ((page + 1) * PER_PAGE < rows) { page++; vm.dirty = true; }
      return;
    }
    if (id.indexOf('item-') === 0) {
      const idx = page * PER_PAGE + Number(id.slice(5));
      if (view === 'songs') playIndex(idx);
      else openList(items[idx]);
      return;
    }
    if (id.indexOf('seek:') === 0) {
      pullBgm();
      const dur = (bgm && bgm.duration) || 0;
      if (!bgm || !bgm.name || bgm.stopped || !(dur > 0)) {
        App.showToast('这首歌暂不支持拖动进度');
        return;
      }
      App.seekBGM(Number(id.slice(5)) * dur);
      vm.dirty = true;
      return;
    }
    // 播放控制（动作前刷新状态，避免用旧的 playing/volume 判断）
    pullBgm();
    switch (id) {
      case 'prev': step(-1); break;
      case 'next': step(1); break;
      case 'toggle':
        if (!bgm || !bgm.name || bgm.stopped) { step(1); break; }
        if (bgm.playing) App.pauseBGM(); else App.resumeBGM();
        break;
      case 'vol-':
        App.setBGMVolume(Math.max(0, ((bgm && bgm.volume) || 0.5) - 0.1));
        App.showToast('音乐音量 ' + Math.round(Math.max(0, ((bgm && bgm.volume) || 0.5) - 0.1) * 100) + '%');
        break;
      case 'vol+':
        App.setBGMVolume(Math.min(1, ((bgm && bgm.volume) || 0.5) + 0.1));
        App.showToast('音乐音量 ' + Math.round(Math.min(1, ((bgm && bgm.volume) || 0.5) + 0.1) * 100) + '%');
        break;
      case 'stop':
        vrQueue = false;
        App.stopBGM();
        App.showToast('已停止播放音乐');
        break;
    }
    pullBgm();
    vm.dirty = true;
  };

  /* ---------------- 每帧：软锚定定位 + 视线吸附 + 重绘 ---------------- */
  const gazeDir = new THREE.Vector3();

  /** 本面板锁定：全局仲裁里锁到的是我，就返回按钮 id（跨面板互斥） */
  function mine(o: THREE.Vector3, d: THREE.Vector3, loose: boolean): string {
    if (App.vrRay) return App.vrRay.pickMine('music', o, d, loose);
    const r = probe.pick(o, d, loose);
    return r ? r.id : '';
  }

  function updateGaze() {
    let id = '';
    const r = App.renderer;
    if (r && r.xr && r.xr.isPresenting) {
      try {
        if (App._xrEyeRay(vm._v3, gazeDir)) id = mine(vm._v3, gazeDir, true);
      } catch (_) { /* 取不到就只留手柄射线 hover */ }
    }
    if (id !== vm.hoverId) { vm.hoverId = id; vm.dirty = true; }
  }

  vm.update = function update(dt: number) {
    if (!vm.active || !vm.mesh) return;
    const hp = App._xrHeadPos;
    if (!hp) return;
    const now = performance.now();

    if (!vm.open) {
      if (vm.mesh.visible) vm.mesh.visible = false;
      return;
    }
    vm.mesh.visible = true;

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
      vm.mesh.position.copy(anchor);
      // 朝向只定这一次：每帧 lookAt 会让按钮随头部追踪噪声滑动，瞄不住也点不中
      vm.mesh.lookAt(hp.x, hp.y - 0.06, hp.z);
      lockDir.set(0, 0, -1).applyQuaternion(vm.mesh.quaternion);
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
    vm.mesh.position.copy(anchor);

    updateGaze();

    // 播放中进度条节流重绘（时间文本/进度条平滑前进）
    if (bgm && bgm.playing && now - lastProgAt > PROG_REDRAW_MS) {
      lastProgAt = now;
      pullBgm();
      vm.dirty = true;
    }
    if (vm.dirty) { vm.dirty = false; draw(); }
  };
  App.updateVrMusic = vm.update;

  /* ---------------- 与非 VR 模式打通 ---------------- */
  // 1) 进入/退出 VR 时显示/隐藏（游戏模式 VR 由游戏自理，沿用 vr-hud 的判定）
  const _enter = App.enterXrMode;
  App.enterXrMode = async function (...args: any[]) {
    const res = await _enter.apply(this, args);
    vm.show();
    return res;
  };
  const _exit = App.exitXrMode;
  App.exitXrMode = function (...args: any[]) {
    vm.hide();
    return _exit.apply(this, args);
  };

  // 2) 播放状态同步：状态变化重绘；自然播完（ended）时队列续下一首
  App.onBGMStateChange(function (s: BGMState) {
    const wasPlaying = !!(bgm && bgm.playing);
    bgm = s;
    vm.dirty = true;
    // ended 的特征：playing→false 且既非暂停也非停止（暂停/停止各有自己的标志位）
    if (wasPlaying && !s.playing && !s.paused && !s.stopped && vrQueue) {
      if (qi + 1 < songs.length) playIndex(qi + 1);
      else { vrQueue = false; App.showToast('歌单播放完毕'); }
    }
  });

  // 3) XR 手柄扳机：先命中音乐面板，未命中才回落到下层面板/戳角色
  const _selStart = App._onControllerSelectStart;
  App._onControllerSelectStart = function (e: any) {
    const c = e && e.target;
    if (vm.active && vm.open && c) {
      try {
        const o = vm._v3, d = new THREE.Vector3();
        if (App._xrCtrlRay(c, o, d, vm._q)) {
          const id = mine(o, d, true);
          if (id) { vm.trigger(id); return; }
        }
      } catch (_) { /* 忽略异常，回退到下层 */ }
    }
    return _selStart.apply(this, arguments);
  };

  // 4) 蓝牙手柄/兜底按键：视中心射线先命中音乐面板，未命中才回落到原逻辑
  const _padClick = App._xrPadClick;
  App._xrPadClick = function () {
    if (vm.active && vm.open && !this._xrGameMode) {
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
      if (id) { vm.trigger(id); return; }
    }
    return _padClick.apply(this, arguments);
  };
}
