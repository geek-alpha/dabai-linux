import type { AppKernel } from '../types/app-kernel.js';

// html2canvas 由 vendor 本地脚本动态注入（同源），补全局类型声明
declare global {
  interface Window {
    html2canvas?: (el: HTMLElement, opts?: Record<string, unknown>) => Promise<HTMLCanvasElement>;
  }
  const html2canvas: ((el: HTMLElement, opts?: Record<string, unknown>) => Promise<HTMLCanvasElement>) | undefined;
}

export default (function init(App: AppKernel) {
  /* ============================================================
   *  任务直播大屏（自适应）—— 把任务中心进度实时投到大白角色身后
   *  大屏会根据信息量自由调整大小与形状：
   *  - 全屏焦点（视频/图片/交互信息）：固定 16:9 满幅布满大屏；
   *  - 待机直播画面：按内容量自适应高度（内容多=满屏 16:9，
   *    内容少=更矮的宽幅横幅），媒体墙/工作流链/任务/跑马灯
   *    走行式布局引擎，互不重叠、排版优美。
   * ============================================================ */

  const DRAW_W = 1280, DRAW_H = 720;  // 逻辑画布分辨率（满幅 16:9）
  const BOARD_W = 3.8;                // Billboard 世界宽度（固定），高度随内容自适应（基础值翻倍）
  const MIN_CONTENT_H = 400;          // 待机画面最小内容高（内容再少也不会扁过这个；保底让「大白直播间」待机横幅够大够醒目）
  const GUTTER = 30;                  // 左右安全边距
  let board = null;                   // THREE.Mesh（屏幕）
  let frame = null;                   // 霓虹边框（Group）
  let frameStrips = [];               // 四条边框 mesh，随屏幕尺寸缩放
  let canvas = null, ctx = null;
  let texture = null;
  let tasks = [];                     // 最近任务列表
  let detailCache = new Map();        // id -> full
  let lastDraw = 0;
  let ready = false;
  let liveChain = null;               // 工作流工具链（31_tool_chain 实时投递）
  let mediaItems = [];                // [{ url, kind, el, ready, dead, w, h }]
  const MEDIA_MAX = 6;
  // 全屏焦点模式：待机直播画面（默认）↔ 最近交互信息布满大屏
  const SHOWCASE_MS = 60000;          // 每条信息停留 1 分钟
  let showcase = null;                // { id, kind:'media'|'info', url?, payload?, ts }
  let showcaseSeq = 0;
  let displayH = DRAW_H;              // 当前"展示高度"（平滑插值中），决定屏幕实际大小与形状
  const VR_BOARD_SCALE = 4.0;         // VR 沉浸模式大屏整体放大倍数（再翻倍=巨幕，占满视野）
  let vrScaleCur = 1;                 // 当前生效的放大系数（进入/退出 VR 平滑过渡）
  let vrFrozen = false;               // VR 世界锚定标记：进入 VR 后首个有效头部帧计算一次位姿并定格，整场会话不再改写
  // 网页渲染桥状态：3D 大屏优先显示 bigscreen.html 的网页画面（iframe 自截图贴纹理），
  // 失败自动回退手绘。state: 'loading'（iframe 加载中）→ 'ready'（截图流正常）→ 'failed'（回退手绘）
  let webRender = { state: 'idle', iframe: null, readyAt: 0, failAt: 0, lastFrame: 0 };

  // 移动端识别（与 webxr-vr / game-mode 同一定义；App 方法未挂载时回退 UA 判断）
  const isMobileDev = () => (App && App._isMobileDevice)
    ? App._isMobileDevice()
    : /Android|iPhone|iPad|iPod|webOS/i.test(navigator.userAgent);

  // 智能体目录（与后端一致）
  const AGENT_DB = {
    dsh:      { name: 'DSH 智能体', icon: '🤖', color: '#7c5cff' },
    opencode: { name: 'OpenCode',   icon: '✦', color: '#00c2ff' },
    codex:    { name: 'Codex',      icon: '⚙️', color: '#ffb84d' },
    shell:    { name: '后台命令',   icon: '💻', color: '#4ade80' },
    steps:    { name: '多步命令',   icon: '🧭', color: '#b39ddb' }
  };
  function agentOf(t) {
    if (t && t.agent && t.agent.name) return t.agent;
    return AGENT_DB[t && t.channel] || { name: (t && t.channel) || '任务', icon: '•', color: '#8c8ca0' };
  }
  const STATUS = {
    confirming: { label: '待确认', color: '#ffd54f', icon: '⏳' },
    queued:     { label: '排队中', color: '#b39ddb', icon: '⏳' },
    running:    { label: '执行中', color: '#00e5ff', icon: '🔄' },
    done:       { label: '已完成', color: '#4ade80', icon: '✅' },
    error:      { label: '失败',   color: '#ff6b6b', icon: '❌' },
    cancelled:  { label: '已取消', color: '#8c8ca0', icon: '🛑' }
  };

  // ---------- 场景搭建（首次 update 时惰性创建） ----------

  function ensureBoard() {
    if (ready || board) return;
    const THREE = App.THREE;
    if (!THREE || !App.scene || !App.camera) return;
    if (!App.modelGroup) return;

    canvas = document.createElement('canvas');
    canvas.width = DRAW_W; canvas.height = DRAW_H;
    ctx = canvas.getContext('2d');
    texture = new THREE.CanvasTexture(canvas);
    texture.anisotropy = 4;
    texture.colorSpace = THREE.SRGBColorSpace;

    // 单位几何 + scale 驱动尺寸：切换大小/形状时零重建，纯数值动画
    board = new THREE.Mesh(
      new THREE.PlaneGeometry(1, 1),
      new THREE.MeshBasicMaterial({ map: texture, transparent: true, opacity: 0.94, depthWrite: false, side: THREE.DoubleSide })
    );
    board.renderOrder = 5;
    App.scene.add(board);

    // 霓虹边框：四条以单位几何创建，尺寸随屏幕 scale 自适应
    frame = new THREE.Group();
    const mk = () => {
      const m = new THREE.Mesh(
        new THREE.BoxGeometry(1, 1, 0.03),
        new THREE.MeshBasicMaterial({ color: 0x00e5ff, transparent: true, opacity: 0.85 })
      );
      frame.add(m);
      return m;
    };
    frameStrips = [mk(), mk(), mk(), mk()]; // 上 下 右 左
    App.scene.add(frame);

    // VR 世界锚定注册：_xrCaptureWorld 收集世界对象时带上大屏，
    // 使其随世界整体平移/旋转（VR 玩家移动 = 世界反向位移 → 大屏世界位置不变）
    App._vrScreenWorldObjs = [board, frame];
// ============================================================
    //  网页取帧模式（bigscreen.html）：大屏内容改为真正的网页绘制
    //  - 隐藏 iframe 指向 /bigscreen.html（同源，浏览器直接打开即预览）
    //  - 每帧用 html2canvas 把 iframe 的 DOM 画面取帧贴到 Billboard 纹理
    //  - 3D 大屏与网页预览看到完全一致的画面（同源同视觉）
    //  - 视频直播（大白影院）保持现有直接 drawImage 路径（html2canvas
    //    不支持 video 元素，这是已知边界，影院画面仍走 Canvas 手绘）
    // ============================================================

    // ---------- 网页渲染桥（bigscreen.html 自截图 → 贴纹理） ----------
    // 3D 大屏显示的就是真正的网页画面：隐藏 iframe 加载 /static/bigscreen.html，
    // 由 iframe 内脚本用 html2canvas 自截图，postMessage 把帧数据传回父页面，
    // 父页面画到 canvas 再贴 Billboard 纹理。两边看到完全一致的画面，
    // 以后改大屏样式只改 bigscreen.html 的 HTML/CSS，预览直接开网页。
    // 失败（iframe 加载失败 / html2canvas 不可用 / 截图异常）自动回退手绘 draw()。
    webRender = { state: 'loading', iframe: null, readyAt: 0, failAt: 0, lastFrame: 0 };
    try {
      const fr = document.createElement('iframe');
      fr.style.cssText = 'position:fixed;left:-9999px;top:0;width:1280px;height:720px;border:0;visibility:hidden;pointer-events:none;';
      fr.src = '/static/bigscreen.html';
      fr.onload = () => { webRender.state = 'loading'; }; // 等 bigscreen-ready 消息确认
      document.body.appendChild(fr);
      webRender.iframe = fr;
    } catch (e) { webRender.state = 'failed'; webRender.failAt = Date.now(); }

    ready = true;
    applyDisplaySize();
    requestTasks();
    console.log('[TaskBoard] 任务直播大屏已挂载（角色身后，尺寸自适应，网页取帧模式）');
  }


  // ---------- 网页渲染桥：接收 iframe 帧数据 ----------
  // bigscreen.html 在 iframe 内用 html2canvas 自截图，postMessage 把帧数据传回。
  // 收到 ready → 切到网页渲染；收到 frame → 画到 canvas 贴纹理；收到 fail → 回退手绘。
  window.addEventListener('message', (ev) => {
    if (!ev.data || typeof ev.data !== 'object') return;
    const d = ev.data;
    if (d.type === 'bigscreen-ready') {
      webRender.state = 'ready';
      webRender.readyAt = Date.now();
      console.log('[TaskBoard] 网页渲染桥就绪（bigscreen.html 截图流已接通）');
    } else if (d.type === 'bigscreen-frame') {
      if (webRender.state !== 'ready') return;
      try {
        const w = d.w || DRAW_W, h = d.h || DRAW_H;
        if (!canvas || canvas.width !== w || canvas.height !== h) {
          canvas = document.createElement('canvas');
          canvas.width = w; canvas.height = h;
          ctx = canvas.getContext('2d');
          if (texture) { texture.image = canvas; texture.needsUpdate = true; }
        }
        const img = new ImageData(new Uint8ClampedArray(d.data), w, h);
        ctx.putImageData(img, 0, 0);
        webRender.lastFrame = Date.now();
        if (texture) texture.needsUpdate = true;
      } catch (e) { /* 帧数据异常：忽略，下一帧继续 */ }
    } else if (d.type === 'bigscreen-fail') {
      webRender.state = 'failed';
      webRender.failAt = Date.now();
      console.log('[TaskBoard] 网页渲染桥失败，回退手绘 draw()');
    }
  });

  // 按"展示高度"驱动屏幕大小/形状（平滑插值期间每帧调用，纯数值更新）：
  // - 宽度也参与缩放：内容占比越高屏幕越大（最大满幅 1.9），内容少时整体更小巧；
  // - 高度 = 宽度 × 内容比例（等比，画布文字不被拉伸变形）；
  // - texture 用 repeat/offset 裁切，只显示画布上部内容区（canvas 高度固定 720）。
  function applyDisplaySize() {
    if (!board || !ready) return;
    const frac = Math.max(0.28, Math.min(1, displayH / DRAW_H)); // 内容占比
    const worldW = BOARD_W * (0.7 + 0.3 * frac) * vrScaleCur;    // 宽度参与缩放（VR 时整体放大）
    const worldH = worldW * displayH / DRAW_W;                   // 等比高度
    board.scale.set(worldW, worldH, 1);
    if (texture) {
      const vFrac = Math.max(0.01, Math.min(1, displayH / DRAW_H));
      texture.repeat.set(1, vFrac);
      texture.offset.set(0, 0);
      texture.needsUpdate = true;
    }
    const t = 0.02;
    if (frameStrips.length === 4) {
      const [top, bottom, right, left] = frameStrips;
      top.position.set(0, worldH / 2 + t, 0);     top.scale.set(worldW + t * 2, t * 2.4, 1);
      bottom.position.set(0, -worldH / 2 - t, 0); bottom.scale.set(worldW + t * 2, t * 2.4, 1);
      right.position.set(worldW / 2 + t, 0, 0);   right.scale.set(t * 2.4, worldH + t * 2, 1);
      left.position.set(-worldW / 2 - t, 0, 0);   left.scale.set(t * 2.4, worldH + t * 2, 1);
    }
  }

  // ---------- 数据 ----------

  // 页面加载后的首次轮询 = 历史预热（静默）；之后新完成/新出现的带媒体结果
  // 要抢全屏焦点 —— 兜底 task_event 推送丢失/断开时也能"画好立刻上大屏"
  let pollWarmed = false;
  const pollSeenDone = new Set();

  // 活跃任务优先补拉（正在跑的「过程」最需要实时）
  function activeRank(t) {
    return ['running', 'confirming', 'queued'].includes(t.status) ? 0 : 1;
  }

  // 拉任务详情补进缓存；同一任务并发去重，失败静默（下一轮轮询会再补）
  const detailFetching = new Set();
  function fetchDetail(id) {
    if (detailFetching.has(id)) return;
    detailFetching.add(id);
    fetch('/api/tasks/' + encodeURIComponent(id)).then(r => r.json()).then(d => {
      detailFetching.delete(id);
      if (!d || !d.ok || !d.task) return;
      detailCache.set(id, d.task);
      if (App.taskBoardAddMedia && App.extractMediaUrls && d.task.result) {
        const urls = App.extractMediaUrls(String(d.task.result));
        if (urls.length) App.taskBoardAddMedia(urls, { silent: true });
      }
      markDirty();
    }).catch(() => { detailFetching.delete(id); });
  }

  let pollTimer: ReturnType<typeof setTimeout> | null = null;
  let boardQuiet = false;
  // 聊天全屏：任务大屏整个看不见 —— 2.5s 轮询停掉，退出全屏立刻补一轮
  App.onQuiet((on) => {
    boardQuiet = on;
    if (on) {
      if (pollTimer) { clearTimeout(pollTimer); pollTimer = null; }
    } else if (ready) {
      requestTasks();
    }
  });

  /** 自排下一轮轮询：先清旧 id，重复调用不会叠出两条循环 */
  function schedulePoll() {
    if (pollTimer) clearTimeout(pollTimer);
    pollTimer = setTimeout(() => { pollTimer = null; requestTasks(); }, 2500);
  }

  function requestTasks() {
    if (boardQuiet) return;
    fetch('/api/tasks').then(r => r.json()).then(data => {
      if (data && data.ok) {
        tasks = data.tasks || [];
        const firstPoll = !pollWarmed;
        if (App.taskBoardAddMedia && App.extractMediaUrls) {
          for (const t of tasks) {
            if (!t || !t.result) continue;
            const urls = App.extractMediaUrls(String(t.result));
            if (!urls.length) continue;
            const silent = firstPoll || t.status !== 'done' || pollSeenDone.has(t.id);
            App.taskBoardAddMedia(urls, { silent });
            if (t.status === 'done') pollSeenDone.add(t.id);
          }
        }
        pollWarmed = true;
        // 补齐「过程」：不只拉焦点任务 —— detailCache 是内存态，刷新（F5）后清空，
        // 只补焦点会让其余任务的过程永久空白；这里对所有「缺详情 / 日志比后端少」的任务补拉。
        const need = tasks.filter(t => {
          const d = detailCache.get(t.id);
          if (!d) return true;
          return (t.logs_count || 0) > ((d.logs || []).length);
        });
        need.sort((a, b) => activeRank(a) - activeRank(b));
        for (const t of need.slice(0, 4)) fetchDetail(t.id);
        markDirty();
      }
    }).catch(() => {});
    schedulePoll();
  }

  let dirty = true;
  function markDirty() { dirty = true; }

  App.taskBoardOnEvent = function taskBoardOnEvent(ev) {
    if (!ev || !ev.id) return;
    let t = detailCache.get(ev.id);
    if (!t) {
      // 刷新后缓存为空：先建桩记下这条过程，再补拉完整详情
      // （原来直接丢弃，导致刷新后非焦点任务的过程永远空白）
      t = { id: ev.id, title: ev.title || '', status: ev.status || 'running',
            steps: [], logs: [], result: '', error: '', agent: ev.agent };
      detailCache.set(ev.id, t);
      fetchDetail(ev.id);
    }
    if (ev.status) t.status = ev.status;
    if (ev.step) t.steps = [...(t.steps || []), ev.step];
    if (ev.log) t.logs = [...(t.logs || []), ev.log];
    if (ev.result !== undefined) t.result = ev.result;
    if (ev.error !== undefined) t.error = ev.error;
    markDirty();
    if (ev.result !== undefined && App.taskBoardAddMedia && App.extractMediaUrls) {
      const urls = App.extractMediaUrls(String(ev.result || ''));
      if (urls.length) App.taskBoardAddMedia(urls);
    }
    // 任务收尾且结论不带媒体 → 也全屏播报这条最近的交互信息
    if (ev.status === 'done' || ev.status === 'error') {
      const resText = String(ev.result || '').trim();
      const errText = ev.status === 'error' ? String(ev.error || '任务执行失败').trim() : '';
      const hasText = resText || errText;
      const withMedia = hasText && App.extractMediaUrls
        ? App.extractMediaUrls(resText + ' ' + errText).length > 0
        : false;
      if (hasText && !withMedia) {
        showcase = {
          id: 'info-' + (++showcaseSeq),
          kind: 'info',
          payload: {
            title: (ev.status === 'error' ? '❌ ' : '✅ ') + (ev.title || '任务') + ' · ' +
              (STATUS[ev.status] ? STATUS[ev.status].label : ev.status),
            text: (ev.status === 'error' ? '⚠️ ' + errText : resText).slice(0, 400),
            accent: ev.status === 'error' ? '#ff6b6b' : '#4ade80',
            tag: ev.status === 'error' ? '❌ 任务失败' : '✅ 任务完成'
          },
          ts: Date.now()
        };
      }
    }
    markDirty();
    requestTasks();
  };

  // 工作流工具链实时投递（31_tool_chain 调用）
  App.taskBoardOnToolChain = function taskBoardOnToolChain(chain) {
    if (!chain || !chain.steps) return;
    liveChain = { round: chain.round || 0, title: chain.title || '', steps: chain.steps, ts: Date.now() };
    markDirty();
  };

  // 最近的交互信息（AI 回复等聊天消息）→ 全屏焦点
  App.taskBoardOnInteraction = function taskBoardOnInteraction(info) {
    if (!info || !info.text) return;
    const urls = App.extractMediaUrls ? App.extractMediaUrls(String(info.text)) : [];
    if (urls.length) return; // 媒体已由媒体墙全屏接管
    showcase = {
      id: 'info-' + (++showcaseSeq),
      kind: 'info',
      payload: {
        title: info.title || '💬 最近交互',
        text: String(info.text).slice(0, 400),
        accent: info.accent || '#00e5ff',
        tag: info.tag || '💬 最近交互'
      },
      ts: Date.now()
    };
    markDirty();
  };

  // 聚焦最新生成的媒体产物：同一条重复投递只续期展示时长
  function focusMedia(item) {
    if (!item) return;
    if (showcase && showcase.kind === 'media' && showcase.url === item.url) {
      showcase.ts = Date.now();
      markDirty();
      return;
    }
    showcase = { id: 'media-' + (++showcaseSeq), kind: 'media', url: item.url, ts: Date.now() };
    markDirty();
  }

  // 智能体产物媒体墙：实时展示 + 全屏焦点。opts.silent = 轮询预热不抢焦点
  App.taskBoardAddMedia = function taskBoardAddMedia(urls, opts) {
    opts = opts || {};
    if (!Array.isArray(urls)) urls = [urls];
    let added = false;
    let newest = null;
    for (const raw of urls) {
      let url = String(raw || '').trim();
      if (!url || mediaItems.some(it => it.url === url)) continue;
      if (url.startsWith('/')) url = location.origin + url;
      const kind = /\.(mp4|webm|ogv|mov|m4v)(?:[?#]|$)/i.test(url) ? 'video' : 'img';
      const item = { url, kind, el: null, ready: false, dead: false, w: 1280, h: 720 };
      if (kind === 'video') {
        const v = document.createElement('video');
        v.muted = true; v.loop = true; v.playsInline = true; v.preload = 'auto'; v.src = url;
        v.addEventListener('loadeddata', () => {
          if (item.dead) return;
          item.ready = true;
          item.w = v.videoWidth || 1280; item.h = v.videoHeight || 720;
          if (ready) { try { const p = v.play(); if (p && p.catch) p.catch(() => {}); } catch (e) {} }
          markDirty();
        });
        v.addEventListener('error', () => { item.dead = true; markDirty(); });
        item.el = v;
      } else {
        const im = new Image();
        im.referrerPolicy = 'no-referrer';
        im.onload = () => {
          if (item.dead) return;
          item.ready = true;
          item.w = im.naturalWidth || 1280; item.h = im.naturalHeight || 720;
          markDirty();
        };
        im.onerror = () => { item.dead = true; markDirty(); };
        im.src = url;
        item.el = im;
      }
      mediaItems.push(item);
      newest = item;
      added = true;
    }
    if (added) {
      while (mediaItems.length > MEDIA_MAX) {
        const dropped = mediaItems.shift();
        if (dropped && dropped.el && dropped.kind === 'video') {
          try { dropped.el.pause(); } catch (e) {}
          dropped.el.src = '';
        }
      }
      if (!opts.silent && newest) focusMedia(newest);
      markDirty();
    }
    if (showcase && showcase.kind === 'media' && !mediaItems.some(it => it.url === showcase.url)) {
      const alt = mediaItems[mediaItems.length - 1];
      if (alt) { showcase.url = alt.url; showcase.ts = Date.now(); }
      else { showcase = null; }
      markDirty();
    }
  };

  // ============================================================
  //  大白影院：在线视频点播播放（VOD，video 技能，能力内建）
  //  带声音全屏投放 + 完整进度控制（播放/暂停/继续/拖动进度/音量/静音/
  //  下一集）+ 连播（播完自动接队列下一部）。
  //  direct 流原生可拖（Range）；relay 实时合流用服务端元数据真实时长当
  //  点播总长，拖动时经 ?ss=N 重起流定位 —— 两种模式都按 VOD 提供完整控制。
  //  断流自动恢复：卡顿看门狗 → direct 同流快重载(Range 续播) →
  //  强制重解析续播（relay 用 ?ss= 服务端定位）→ 从头重试 → 放弃回待机
  // ============================================================
  let videoLive = null;
  // phase: loading(取流中) → playing(播放中) → recovering(自动恢复中) → dead / ended
  const STALL_MS = 10000;        // 播放中缓冲超过 10 秒无进展 → 判定卡死触发恢复
  const LOAD_TIMEOUT_MS = 25000; // 初次取流超时未起播 → 触发恢复
  const MAX_RECOVERY = 3;        // 自动恢复次数上限（稳定播放 30 秒后重置）

  // entry.stream → 播放地址（主服务原生端点 /api/video_hub/*，同源相对路径，
  // 手机/局域网与桌面端统一可用；ss>0 时 relay 从该秒起播=断点续播）
  function videoStreamUrls(entry, ss) {
    const st = (entry && entry.stream) || {};
    const s = ss > 0 ? '?ss=' + Math.max(0, ss).toFixed(1) : '';
    if (st.mode === 'direct') return { url: '/api/video_hub/proxy?k=' + st.key, fallback: '' };
    if (st.mode === 'relay') {
      const base = '/api/video_hub/relay/' + st.key;
      return { url: base + s, fallback: base + (s ? s + '&t=1' : '?t=1') };
    }
    return { url: '', fallback: '' };
  }

  function videoEntryInfo(entry) {
    const st = (entry && entry.stream) || {};
    return {
      title: (entry && entry.title) || '',
      uploader: (entry && entry.uploader) || '',
      platform: (entry && entry.platform) || '',
      height: st.height || 0,
      mode: st.mode || '',
      webpage_url: (entry && entry.webpage_url) || '',
      duration: (entry && entry.duration) || 0   // 服务端解析的真实时长（秒）
    };
  }

  // VOD 时长判定：relay 实时合流没有真实时长（浏览器视作直播流，
  // el.duration=Infinity），用服务端元数据的真实时长当点播总长；direct 用元素时长。
  function videoKnownDuration(vl): number {
    if (!vl) return 0;
    let dur = 0;
    try {
      const d = vl.el && vl.el.duration;
      if (isFinite(d) && d > 0) dur = Number(d);
    } catch (e) {}
    const real = vl.info && vl.info.duration;
    if (isFinite(real) && Number(real) > 0) dur = Math.max(dur, Number(real));
    return dur;
  }

  // relay 是 ffmpeg 实时合流（live 流），浏览器 currentTime 从 0 重新计时，
  // 不含服务端 ?ss= 的定位偏移（ss=N 起播后元素时间轴又归零）。
  // 真实播放位置 = ss 偏移 + 元素 currentTime；direct 流无偏移（0）→ 通用等价于原生 currentTime。
  function videoRealPosition(vl): number {
    if (!vl || !vl.el) return 0;
    return (vl.seekBase || 0) + (vl.el.currentTime || 0);
  }

  // 音量持久化（localStorage，跨视频/跨刷新沿用上次音量）
  const VIDEO_VOLUME_KEY = 'dabai.videoVolume.v1';
  function loadVideoVolume(): { volume: number; muted: boolean } {
    try {
      const raw = localStorage.getItem(VIDEO_VOLUME_KEY);
      if (raw) {
        const o = JSON.parse(raw);
        const volume = Number(o && o.volume);
        return {
          volume: isFinite(volume) ? Math.max(0, Math.min(1, volume)) : 0.8,
          muted: !!(o && o.muted)
        };
      }
    } catch (e) { /* 读取失败忽略 */ }
    return { volume: 0.8, muted: false };
  }
  function saveVideoVolume(volume: number, muted: boolean) {
    try { localStorage.setItem(VIDEO_VOLUME_KEY, JSON.stringify({ volume, muted })); } catch (e) { /* 忽略 */ }
  }

  // 是否真的播到片尾：relay 直播流的 el.duration 是不断增长的估计值，
  // 流中途断掉浏览器也会误发 ended——必须以服务端给的真实时长为准
  function videoNearEnd(vl) {
    const real = vl && vl.info && vl.info.duration;
    if (isFinite(real) && real > 0) return vl.lastPos >= real - 10;
    const d = vl && vl.el && vl.el.duration;
    return isFinite(d) && d > 0 && vl.lastPos >= d - 8;
  }

  // 上报播放进度（video_status 工具可见），5 秒一次
  function videoReportState() {
    if (!videoLive || !videoLive.el || videoLive.el.readyState < 1) return;
    videoLive.lastReport = Date.now();
    const v = videoLive.el;
    try {
      fetch('/api/video_hub/api/report', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          position: videoRealPosition(videoLive),
          duration: isFinite(v.duration) ? v.duration : null,
          paused: v.paused,
          volume: Math.round((v.muted ? 0 : v.volume) * 100) / 100
        }),
        keepalive: true
      }).catch(() => {});
    } catch (e) { /* 忽略上报失败 */ }
  }

  function videoTeardown(silent) {
    if (!videoLive) return;
    // 关闭/退出大屏 → 收回看护子智能体（video_stop，静默不打扰主智能体）
    const wid = App._videoWorkerId;
    App._videoWorkerId = null;
    if (wid && App.ws && App.ws.readyState === WebSocket.OPEN) {
      App.ws.send(JSON.stringify({ type: 'video_stop', worker_id: wid }));
    }
    disposeVideoElement(videoLive.el);
    videoLive = null;
    dirty = true;
    if (!silent && App.showToast) App.showToast('📺 大屏回到待机');
  }

  function _vhSignal(ms) {
    const c = new AbortController();
    setTimeout(() => { try { c.abort(); } catch (e) {} }, ms);
    return c.signal;
  }

  // 队列空时的隐式连播：从视频面板（29_video_ui）最近一次搜索结果里
  // 找到刚播完那部的位置，按搜索顺序接下一部。返回 true = 已起播。
  async function playNextFromSearch(endedUrl?: string): Promise<boolean> {
    if (!App.videoNextFromSearch || !App.playVideoItem) return false;
    const nx = App.videoNextFromSearch(endedUrl || '');
    if (!nx) return false;
    const ok = await App.playVideoItem(nx, { auto: true });
    return !!ok;
  }

  // 播完 → 回报子智能体（worker_id 带回 → 闭环看护）+ 取队列下一部连播；
  // 队列没有片时兜底：按最近搜索结果的顺序自动接着播（隐式播放列表）。
  function videoAdvanceEnded() {
    if (!videoLive) return;
    videoLive.phase = 'ended';
    videoLive.ended = true;
    dirty = true;
    const wid = App._videoWorkerId;
    App._videoWorkerId = null;
    const endedUrl = (videoLive.info && videoLive.info.webpage_url) || '';
    fetch('/api/video_hub/api/ended', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ worker_id: wid || undefined })
    })
      .then(r => r.json())
      .then(async d => {
        const next = d && d.next;
        const urls = next ? videoStreamUrls(next, 0) : { url: '', fallback: '' };
        if (next && urls.url) {
          videoLoad(urls.url, videoEntryInfo(next), urls.fallback, 0);
          if (App.showToast) App.showToast('▶ 连播：' + (next.title || '下一部'));
          return;
        }
        // 视频界面没有播放列表 → 自动按搜索顺序接下一部；搜不到可接的才回待机
        if (!(await playNextFromSearch(endedUrl))) {
          videoTeardown(true);
          if (App.showToast) App.showToast('📺 播放完毕，队列已播完');
        }
      })
      .catch(() => videoTeardown(true));
  }

  function videoOnError() {
    const vl = videoLive;
    if (!vl) return;
    if (!vl.started) {
      // 起播阶段失败：先试转码兜底流，再强制重解析
      if (vl.fallbackUrl && !vl.triedFallback) {
        vl.triedFallback = true;
        vl.ready = false;
        vl.phase = 'loading';
        vl.loadStartedAt = Date.now();
        vl.url = vl.fallbackUrl;
        vl.el.src = vl.url;
        vl.el.load();
        const p = vl.el.play();
        if (p && p.catch) p.catch(() => {});
        return;
      }
      if (vl.info.webpage_url && vl.recoveryTries < MAX_RECOVERY) {
        videoRecover('load');
        return;
      }
      vl.phase = 'dead';
      vl.dead = true;
      vl.errAt = Date.now();
      vl.errText = '视频加载失败';
      dirty = true;
      return;
    }
    // 播放中断流 → 自动恢复
    videoRecover('error');
  }

  async function videoRecover(reason) {
    const vl = videoLive;
    if (!vl || vl.phase === 'recovering' || vl.phase === 'dead') return;
    // 已播到片尾附近断流：直接按播完处理（接队列下一部）
    if (videoNearEnd(vl)) {
      videoAdvanceEnded();
      return;
    }
    if (vl.recoveryTries >= MAX_RECOVERY) {
      vl.phase = 'dead';
      vl.dead = true;
      vl.errAt = Date.now();
      vl.errText = '多次自动恢复未成功';
      dirty = true;
      return;
    }
    vl.recoveryTries++;
    vl.phase = 'recovering';
    dirty = true;
    // 恢复开始立即停掉旧流（原代码误引用了不存在的 v，pause 永远没执行，
    // 导致恢复期间旧流持续解码+发热，且 currentTime 继续前进干扰看门狗）
    const el = vl.el;
    if (el) { try { el.pause(); } catch (e) {} }
    const pos = Math.max(0, vl.lastPos || 0);
    try {
      // ① direct 流 key 仍有效时最快：同流重载 + Range 续播（每次断流只试一次）
      if (vl.info.mode === 'direct' && reason !== 'load' && !vl.reloadTried && vl.url) {
        vl.reloadTried = true;
        videoLoad(vl.url, vl.info, vl.fallbackUrl, pos);
        return;
      }
      // ② 强制重解析（服务重启 key 失效 / 上游直链断流），拿全新流续播
      if (!vl.info.webpage_url) throw new Error('no webpage_url');
      const r = await fetch('/api/video_hub/api/play', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ url: vl.info.webpage_url, force: true }),
        signal: _vhSignal(20000)
      });
      if (!r.ok) throw new Error('HTTP ' + r.status);
      const entry = await r.json();
      const st = entry && entry.stream;
      if (!st || !st.key) throw new Error('re-resolve failed');
      const info = videoEntryInfo(entry);
      // direct 前端 currentTime 续播；relay 服务端 ?ss= 定位到断点前 2 秒
      const urls = videoStreamUrls(entry, info.mode === 'direct' ? 0 : (pos > 5 ? pos - 2 : 0));
      videoLoad(urls.url, info, urls.fallback, info.mode === 'direct' ? pos : 0);
    } catch (e) {
      // ③ 网络彻底不通：等 10 秒再试一轮（保持"恢复中"画面）
      if (vl.recoveryTries < MAX_RECOVERY) {
        setTimeout(() => {
          if (videoLive === vl && vl.phase === 'recovering') videoRecover('retry');
        }, 10000);
      } else {
        vl.phase = 'dead';
        vl.dead = true;
        vl.errAt = Date.now();
        vl.errText = '网络不可用，自动恢复失败';
        dirty = true;
      }
    }
  }

  // rVFC 帧追踪（防重入）：video.load()/断流恢复后浏览器会丢弃挂起的回调，
  // 在元素 playing 时再武装一次，保证「画面冻结看门狗」与「跟随新帧重绘」持续有效。
  function armFrameTracker(v) {
    if (typeof v.requestVideoFrameCallback !== 'function' || v.__dabaiFrameArmed) return;
    v.__dabaiFrameArmed = true;
    const frameLoop = () => {
      if (!v.__dabaiFrameArmed) return;
      if (videoLive && videoLive.el === v) {
        videoLive.lastFrameTs = Date.now();
        videoLive.framePending = true;
        dirty = true;
      }
      if (!v.paused && !v.ended) {
        v.requestVideoFrameCallback(frameLoop);
      } else {
        v.__dabaiFrameArmed = false; // 停摆：等下次 playing 事件重新武装
      }
    };
    v.requestVideoFrameCallback(frameLoop);
  }

  // 像素探针看门狗（仅无 requestVideoFrameCallback 的老浏览器/电视盒子启用）：
  // 播放中 currentTime 持续前进、但画面像素一直纹丝不动超过 STALL_MS → 判定
  // 「有声音但画面冻结」触发恢复。每 2 秒对当前帧抽 10×6 采样做哈希比对，开销可忽略。
  let __vhPxCanvas = null, __vhPxCtx = null;
  // 影院氛围底（模糊帧）离屏缓存：新帧到达时按 ~1.6fps 重渲一次，其余重绘直接 blit，
  // 避免每帧 26px 高斯模糊（桌面端单帧最大开销）压垮主线程/GPU ——
  // 主线程与合成器过载正是「画面冻结、声音照常」的常见诱因之一。
  let __vhBlur = null;
  function videoPxStallProbe(vl, vd) {
    const now = Date.now();
    if (!__vhPxCanvas) {
      __vhPxCanvas = document.createElement('canvas');
      __vhPxCanvas.width = 10; __vhPxCanvas.height = 6;
      __vhPxCtx = __vhPxCanvas.getContext('2d', { willReadFrequently: true });
    }
    if (!__vhPxCtx) return false;
    const probe = (vl.pxProbe = vl.pxProbe || { lastAt: 0, lastSig: '', sameSince: 0, lastPos: vd.currentTime || 0 });
    if (now - probe.lastAt < 2000) return false; // 2 秒采样一次
    probe.lastAt = now;
    if (vd.paused || vd.readyState < 2) { probe.sameSince = 0; probe.lastPos = vd.currentTime || 0; return false; }
    const curPos = vd.currentTime || 0;
    let sig = '';
    try {
      __vhPxCtx.drawImage(vd, 0, 0, 10, 6);
      const d = __vhPxCtx.getImageData(0, 0, 10, 6).data;
      let h = 0;
      for (let i = 0; i < d.length; i += 16) h = (h * 31 + d[i]) >>> 0;
      sig = String(h);
    } catch (e) { sig = ''; }
    const timeAdvanced = curPos > probe.lastPos + 0.05;
    probe.lastPos = curPos;
    if (!timeAdvanced || !sig) { probe.sameSince = 0; return false; }
    if (sig === probe.lastSig) {
      probe.sameSince = probe.sameSince || now;
      if (now - probe.sameSince > STALL_MS) return true; // 时间在走、画面纹丝不动超阈值 → 冻结
    } else {
      probe.sameSince = 0;
    }
    probe.lastSig = sig;
    return false;
  }

  // 换片/恢复：每次播放都新建一个【全新】<video> 元素，绝不复用旧元素。
  // 长时间播放后浏览器解码管线可能进入卡死状态（画面冻结、声音照常推进）——
  // 复用一个已卡死的元素换 src/load 也救不回来（这就是「换视频仍然卡住」的根因：
  // 新片子的音轨能解，视频轨依旧停摆）。新建元素 + 彻底释放旧元素解码器，
  // 让每次起播/恢复都从干净状态开始。
  function disposeVideoElement(v) {
    if (!v) return;
    v.__dabaiFrameArmed = false; // 停掉 rVFC 追踪循环
    if (v.__dabaiHandlers) {
      for (const pair of v.__dabaiHandlers) {
        try { v.removeEventListener(pair[0], pair[1]); } catch (e) {}
      }
      v.__dabaiHandlers = null;
    }
    try { v.pause(); } catch (e) {}
    try {
      v.removeAttribute('src'); // 先清 src 再 load，释放网络/解码资源
      v.load();
    } catch (e) {}
  }

  // opts.autoplay=false：跳转重载时保持"暂停中"状态（onLoadedData 与起播都不自动播放）
  function videoLoad(url, info, fallbackUrl, startPos, opts?: any) {
    opts = opts || {};
    const autoplay = opts.autoplay !== false;
    const prev = videoLive;
    const v = document.createElement('video');
    v.playsInline = true;
    v.preload = 'auto';
    // 音量持久化：每个新元素（起播/换片/恢复/跳转）都沿用上次设置的音量
    const savedVol = loadVideoVolume();
    v.volume = savedVol.volume;
    v.crossOrigin = 'anonymous';
    // 继承静音状态（连播/恢复时无用户手势，保持已静音继续，点按后统一开声）；
    // 无上一元素（新开播）时用持久化的静音设置
    if (prev && prev.el) v.muted = !!prev.el.muted;
    else v.muted = savedVol.muted;

    const onLoadedData = () => {
      if (!videoLive || videoLive.el !== v) return;
      videoLive.ready = true;
      videoLive.triedFallback = false;
      // direct 流断点续播：Range 请求直接跳到上次位置
      if (videoLive.startPos > 0 && videoLive.info.mode === 'direct') {
        try { v.currentTime = videoLive.startPos; } catch (e) {}
      }
      dirty = true;
      if (videoLive.autoplayPending === false) {
        // 暂停中跳转：就绪即可（起播位置由服务端 ?ss= 定位），不自动播放。
        // 标记为 playing 态（元素 paused → 看门狗跳过），避免取流超时误触发恢复。
        videoLive.phase = 'playing';
        videoLive.lastProgressTs = Date.now();
        videoLive.recoveredAt = Date.now();
        return;
      }
      const p = v.play();
      if (p && p.catch) p.catch(() => {});
    };
    const onPlaying = () => {
      if (!videoLive || videoLive.el !== v) return;
      videoLive.started = true;
      videoLive.phase = 'playing';
      videoLive.lastProgressTs = Date.now();
      videoLive.recoveredAt = Date.now();
      if (videoLive.recoveryTries > 0 && App.showToast) {
        App.showToast('🔄 网络波动，已自动恢复播放');
      }
      dirty = true;
      // 恢复/换片后浏览器可能丢弃了挂起回调；rVFC 同时只能挂一个回调链，
      // 先把 armed 复位再武装 —— 旧挂起的回调因 armed=false 会静默退出，不会双循环
      (v as any).__dabaiFrameArmed = false;
      armFrameTracker(v);
    };
    const onTimeUpdate = () => {
      if (!videoLive || videoLive.el !== v) return;
      const t = (videoLive.seekBase || 0) + (v.currentTime || 0);
      if (t > 0) {
        videoLive.lastPos = t;
        videoLive.lastProgressTs = Date.now();
        dirty = true; // 进度条/时间文本变化 → 触发重绘（不再只靠 40ms 定时兜着）
      }
      // 稳定播放 30 秒 → 恢复次数清零（下一轮偶发断流仍能自动救）
      if (videoLive.recoveryTries && Date.now() - (videoLive.recoveredAt || 0) > 30000) {
        videoLive.recoveryTries = 0;
        videoLive.reloadTried = false;
      }
    };
    const onError = videoOnError;
    const onEnded = () => {
      if (!videoLive || videoLive.el !== v) return;
      // relay 流中途断掉浏览器会误发 ended（duration 是增长估计值）：
      // 真实时长没播完 → 当作断流自动恢复，而不是跳下一部
      if (!videoNearEnd(videoLive)) {
        videoRecover('stall');
        return;
      }
      videoAdvanceEnded();
    };
    const handlers: Array<[string, EventListener]> = [
      ['loadeddata', onLoadedData as EventListener],
      ['playing', onPlaying as EventListener],
      ['timeupdate', onTimeUpdate as EventListener],
      ['error', onError as EventListener],
      ['ended', onEnded as EventListener],
    ];
    for (const pair of handlers) (v as any).addEventListener(pair[0], pair[1]);
    (v as any).__dabaiHandlers = handlers;
    // 新帧呈现追踪（requestVideoFrameCallback）：只在解码器真正交付了
    // 新视频帧时回调。「有声音但画面冻结」时它静止 → 看门狗据此判定卡死；
    // cinema 重绘也跟随它（无新帧就不再重绘+上传纹理，降温）。
    armFrameTracker(v);

    // 先释放旧元素（停网络/解码器/事件监听，杜绝卡死元素残留），再干净起播
    if (prev && prev.el && prev.el !== v) disposeVideoElement(prev.el);

    // relay 跳转/断点续播经服务端 ?ss=N 定位起播，live 元素 currentTime 从 0 重新计时；
    // 记录 ss 偏移作为真实时间轴起点（进度显示/播完判定/断点续播都按真实位置计算）
    let seekBase = 0;
    if (info && info.mode === 'relay') {
      const qs = String(url || '').split('?')[1] || '';
      const ssN = Number(new URLSearchParams(qs).get('ss') || '0');
      if (isFinite(ssN) && ssN > 0) seekBase = ssN;
    }

    videoLive = {
      el: v, url: url, fallbackUrl: fallbackUrl || '',
      info: info || {}, startPos: startPos || 0, seekBase: seekBase,
      autoplayPending: autoplay,
      phase: 'loading', ready: false, dead: false, started: false, ended: false,
      triedFallback: false, reloadTried: false,
      recoveryTries: (prev && prev.recoveryTries) || 0,
      lastPos: seekBase > 0 ? seekBase : (startPos || 0), lastProgressTs: Date.now(), loadStartedAt: Date.now(),
      recoveredAt: 0, mutedAutoplay: false, userPaused: false, autoPaused: false,
      errAt: 0, errText: '', lastReport: 0,
      rfvc: typeof v.requestVideoFrameCallback === 'function',
      lastFrameTs: Date.now(), framePending: false,
      lastDipTs: Date.now(),   // 最近一次真正把视频帧画上大屏的时刻（绘制停滞看门狗喂水印）
      pxProbe: null            // 无 rVFC 浏览器使用的像素探针状态
    };
    v.src = url;
    v.load();
    showcase = null; // 视频接管全屏焦点
    dirty = true;
    if (!autoplay) return; // 暂停中跳转：等用户手动继续
    const p = v.play();
    if (p && p.catch) p.catch(() => {
      // 有声自动播放被浏览器拦截 → 静音重试 + 点按任意处恢复声音
      if (!videoLive || videoLive.el !== v) return;
      v.muted = true;
      videoLive.mutedAutoplay = true;
      dirty = true;
      const p2 = v.play();
      if (p2 && p2.catch) p2.catch(() => {});
      document.addEventListener('pointerdown', function videoUnmute() {
        if (videoLive && videoLive.el) {
          videoLive.el.muted = false;
          videoLive.mutedAutoplay = false;
          dirty = true;
        }
      }, { once: true });
    });
  }

  // AI 屏幕命令入口：play_video（skills/video 技能发出）
  App.videoBoardPlay = function videoBoardPlay(payload) {
    const args = payload || {};
    if (!args.url) {
      console.warn('[VideoBoard] play_video 缺少 url');
      return;
    }
    // 子智能体看护：server 注入 worker_id，播完经 /api/video_hub/api/ended 带回
    App._videoWorkerId = args.worker_id || null;
    // 视频自带声音，停掉背景/在线音乐避免叠加
    if (App.isBGMPlaying && App.isBGMPlaying()) App.stopBGM();
    videoLoad(args.url, {
      title: args.title || '',
      uploader: args.uploader || '',
      platform: args.platform || '',
      height: args.height || 0,
      mode: args.mode || '',
      webpage_url: args.webpage_url || args.webpageUrl || '',
      duration: Number(args.duration) || 0
    }, args.fallback_url || args.fallbackUrl || '', 0);
    if (App.showToast) App.showToast(args.message || ('🎬 大白影院：' + (args.title || '开始播放')));
  };

  // AI 屏幕命令入口：control_video（pause/resume/seek/volume/mute/stop/next）
  App.videoBoardControl = function videoBoardControl(payload) {
    const args = payload || {};
    const action = args.action;
    if (!videoLive && action !== 'stop') {
      if (App.showToast) App.showToast(args.message || '当前没有在播的视频');
      return;
    }
    const v = videoLive ? videoLive.el : null;
    try {
      switch (action) {
        case 'pause':
          v.pause();
          if (videoLive) videoLive.userPaused = true;
          break;
        case 'resume': {
          const p = v.play();
          if (p && p.catch) p.catch(() => {});
          if (videoLive) { videoLive.userPaused = false; videoLive.ended = false; }
          break;
        }
        case 'seek': {
          const target = Math.max(0, Number(args.value) || 0);
          const info: any = videoLive.info || {};
          if (info.mode === 'relay') {
            // VOD 模拟：relay 实时合流无字节级 Range/时长，浏览器无法原生 seek；
            // 用服务端元数据真实时长当点播总长，跳转时让 ffmpeg 从 ?ss=N 重起流
            // （快进定位到目标秒附近的关键帧），等价于点播拖动。
            const real = info.duration;
            if (!(isFinite(real) && real > 0)) {
              if (App.showToast) App.showToast('这个视频拿不到时长，暂不支持拖动进度');
              break;
            }
            const t = Math.min(target, Number(real));
            const wasPaused = !!v.paused;
            const base = videoLive.url.split('?')[0];
            const hasT = /[?&]t=1/.test(videoLive.url);
            const qs = [hasT ? 't=1' : '', t > 0 ? 'ss=' + t.toFixed(1) : ''].filter(Boolean).join('&');
            const url = base + (qs ? '?' + qs : '');
            // 兜底流同样带 ss 定位（与 videoStreamUrls 同款：ss 保持 + t=1 转码），
            // 避免主流加载失败退回 fallback 时从 0 起播、进度却按 ss 偏移显示
            const fallback = base + (qs ? qs + '&t=1' : '?t=1');
            videoLoad(url, videoLive.info, fallback, 0, { autoplay: !wasPaused });
            if (App.showToast) App.showToast('已跳转 ' + videoFmtTime(t));
          } else {
            v.currentTime = target;
          }
          break;
        }
        case 'volume': {
          const vol = Math.max(0, Math.min(1, Number(args.value) || 0));
          v.volume = vol;
          v.muted = false;
          saveVideoVolume(vol, false);
          break;
        }
        case 'mute': {
          const muted = args.value === undefined ? !v.muted : !!Number(args.value);
          v.muted = muted;
          saveVideoVolume(v.volume, muted);
          break;
        }
        case 'next': {
          // 下一集：优先从连播队列弹出（与播完自动连播同一队列源，服务端 pop_next）；
          // 队列空/取不到流时兜底按最近搜索结果的顺序接下一部
          const curUrl = (videoLive && videoLive.info && videoLive.info.webpage_url) || '';
          fetch('/api/video_hub/api/control', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ action: 'next' }),
            signal: _vhSignal(15000)
          })
            .then(r => r.json())
            .then(async d => {
              const nxt = d && d.next;
              const urls = nxt ? videoStreamUrls(nxt, 0) : { url: '', fallback: '' };
              if (nxt && urls.url) {
                videoLoad(urls.url, videoEntryInfo(nxt), urls.fallback, 0);
                if (App.showToast) App.showToast('⏭ 下一集：' + (nxt.title || ''));
                return;
              }
              if (!(await playNextFromSearch(curUrl))) {
                if (App.showToast) App.showToast('队列和搜索结果里都没有下一部了');
              }
            })
            .catch(() => {
              if (App.showToast) App.showToast('获取下一集失败，请稍后再试');
            });
          break;
        }
        case 'stop':
          videoTeardown(true);
          break;
        default:
          console.warn('[VideoBoard] 未知 action:', action);
      }
    } catch (e) {
      console.warn('[VideoBoard] control 失败:', e);
    }
    dirty = true;
    if (App.showToast && args.message) App.showToast(args.message);
  };

  // 视频面板 UI（29_video_ui）轮询状态：正在播的片子 / 进度 / 是否支持拖动
  App.videoBoardGetState = function videoBoardGetState() {
    const vl = videoLive;
    if (!vl || !vl.el) return null;
    const v = vl.el;
    const dur = videoKnownDuration(vl);
    return {
      active: true,
      title: vl.info.title || '',
      mode: vl.info.mode || '',
      phase: vl.phase || '',
      paused: !!v.paused,
      ended: !!vl.ended,
      dead: !!vl.dead,
      recovering: vl.phase === 'recovering',
      ready: !!vl.ready,
      currentTime: videoRealPosition(vl),
      duration: dur,
      // 点播（VOD）：direct 走 Range 拖动；relay 用服务端真实时长 + ?ss= 重起流模拟拖动
      seekable: dur > 0,
      // 音量/静音（np 面板音量条与静音键用）
      volume: v.muted ? 0 : (typeof v.volume === 'number' ? v.volume : 0.8),
      muted: !!v.muted,
      // 当前播放视频原页信息（视频面板「收藏当前」用）
      webpage_url: vl.info.webpage_url || '',
      uploader: vl.info.uploader || '',
      platform: vl.info.platform || ''
    };
  };

  // ---------- 绘制工具 ----------

  function rr(ctx, x, y, w, h, r) {
    ctx.beginPath();
    ctx.moveTo(x + r, y);
    ctx.arcTo(x + w, y, x + w, y + h, r);
    ctx.arcTo(x + w, y + h, x, y + h, r);
    ctx.arcTo(x, y + h, x, y, r);
    ctx.arcTo(x, y, x + w, y, r);
    ctx.closePath();
  }

  // 中文逐字换行
  function wrapText(ctx, text, maxW) {
    const lines = [];
    for (const raw of String(text == null ? '' : text).split('\n')) {
      let line = '';
      for (const ch of raw) {
        const test = line + ch;
        if (line && ctx.measureText(test).width > maxW) { lines.push(line); line = ch; }
        else line = test;
      }
      if (line) lines.push(line);
    }
    return lines.length ? lines : [''];
  }

  // 单行截断到最大宽度（省略号收尾）
  function fitText(text, maxW) {
    let s = String(text == null ? '' : text).replace(/\s+/g, ' ').trim();
    if (!s) return '';
    if (ctx.measureText(s).width <= maxW) return s;
    let cut = s.length;
    while (cut > 1 && ctx.measureText(s.slice(0, cut) + '…').width > maxW) cut--;
    return s.slice(0, cut) + '…';
  }

  // 当前活跃的工作流链（实时链优先；无则用运行任务的最新步骤）→ { title, steps } | null
  function activeChain() {
    let chain = liveChain;
    if (!chain || !chain.steps || !chain.steps.length) {
      const focus = tasks.find(t => t.status === 'running') ||
                    tasks.find(t => ['confirming', 'queued'].includes(t.status));
      if (!focus) return null;
      const d2 = detailCache.get(focus.id);
      if (!d2 || !d2.steps || !d2.steps.length) return null;
      const lst = d2.steps.slice(-5);
      chain = {
        round: 0,
        title: (focus.agent && focus.agent.name ? focus.agent.name + ' · ' : '') + (focus.title || '任务'),
        steps: lst.map((s, i) => ({
          name: s,
          status: (focus.status === 'running') && i === lst.length - 1 ? 'running' : 'done'
        }))
      };
    }
    const hasRun = chain.steps.some(s => s.status === 'running');
    if (!hasRun && Date.now() - (chain.ts || 0) > 60000) return null; // 链已结束超 1 分钟收起
    return chain;
  }

  // ---------- 布局引擎：内容多少决定大屏大小与各区块位置（互不重叠） ----------
  // 行式结构（自上而下）：顶栏+统计(固定) → 媒体墙(可选) → 工作流链(可选)
  // → 任务卡(弹性，按剩余空间自适应张数与高度) → 跑马灯(底部固定)。
  // contentH = 所有内容的总高，直接把 Billboard 高度映射上去。
  function computeLayout(H?: number) {
    H = H || DRAW_H;         // 当前展示高度（动画中随屏幕边缘变化）
    const L = {
      header: 62,            // 顶栏基线
      stats: 108,            // 统计条基线
      media: null,           // { y0, th, n }
      chain: null,           // { y0, h, data }
      tasks: null,           // { y0, h, shown[] }
      idle: false,           // 空屏待命
      idleY: 0,
      contentH: H,
      tickerY: 0,
    };
    let cursor = 108;        // 统计条下方
    // 1) 媒体墙（存在未失败的媒体就占行：就绪显示图、未就绪显示骨架占位）
    const live = mediaItems.filter(it => !it.dead);
    if (live.length) {
      const th = Math.min(96, Math.max(64, Math.round(460 / live.length))); // 多条自动变矮
      L.media = { y0: cursor + 24, th, n: live.length };
      cursor += 24 + th + 18;
    }
    // 2) 工作流工具链（有活动链才占行）
    const chain = activeChain();
    if (chain) {
      L.chain = { y0: cursor, h: 46, data: chain };
      cursor += 46 + 12;
    }
    // 3) 任务卡：按当前可视高度算能放几张；放不下就少放
    const tickerH = 46;
    let cardCount = 0;
    let taskH = 0;
    for (let n = Math.min(tasks.length, 3); n >= 1; n--) {
      const per = Math.min(168, (H - cursor - tickerH - 14 - 10 * (n - 1)) / n);
      if (per >= 120) { cardCount = n; taskH = per * n + 10 * (n - 1); break; }
    }
    if (cardCount > 0) {
      L.tasks = { y0: cursor + 8, h: taskH, shown: tasks.slice(0, cardCount) };
      cursor += 8 + taskH + 14;
    }
    // 4) 收尾：算总内容高
    if (!tasks.length && !L.media && !L.chain) {
      L.idle = true;
    }
    let contentH = Math.min(H, Math.max(MIN_CONTENT_H, cursor + tickerH));
    if (cardCount === 0 && tasks.length) {
      // 极端：任务存在但空间不足（几乎不可能）→ 至少显示 1 张
      contentH = H;
      L.tasks = { y0: cursor + 8, h: 130, shown: tasks.slice(0, 1) };
    }
    L.contentH = contentH;
    L.tickerY = contentH - 12;           // 跑马灯文字基线
    L.idleY = Math.round((cursor + (contentH - tickerH)) / 2);
    return L;
  }

  // ---------- 待机直播画面渲染（按布局） ----------

  function drawBackdrop(now) {
    const H = Math.max(64, Math.round(displayH)); // 底板跟随当前展示高度
    const grad = ctx.createLinearGradient(0, 0, 0, H);
    grad.addColorStop(0, 'rgba(36,50,108,0.97)');
    grad.addColorStop(0.5, 'rgba(25,34,84,0.97)');
    grad.addColorStop(1, 'rgba(18,24,60,0.97)');
    ctx.fillStyle = grad;
    rr(ctx, 4, 4, DRAW_W - 8, H - 8, 18);
    ctx.fill();
    ctx.strokeStyle = 'rgba(0,229,255,0.32)';
    ctx.lineWidth = 2;
    ctx.stroke();
    // 顶部氛围光（紫→青渐变）：待机也绝不显黑屏，始终有"直播中"的光感
    const glow = ctx.createRadialGradient(DRAW_W / 2, -20, 10, DRAW_W / 2, -20, DRAW_W * 0.62);
    glow.addColorStop(0, 'rgba(124,92,255,0.26)');
    glow.addColorStop(0.55, 'rgba(0,229,255,0.10)');
    glow.addColorStop(1, 'rgba(0,229,255,0)');
    ctx.fillStyle = glow;
    ctx.fillRect(0, 0, DRAW_W, H);
    // 左彩色边缘
    ctx.fillStyle = '#9d7bff';
    ctx.fillRect(6, 8, 8, H - 16);
    // 扫描线（动效暗示"直播中"）
    if (now % 3000 < 1600) {
      const sy = 16 + ((now % 1600) / 1600) * (H - 32);
      const sg = ctx.createLinearGradient(0, sy - 26, 0, sy + 26);
      sg.addColorStop(0, 'rgba(0,229,255,0)');
      sg.addColorStop(0.5, 'rgba(0,229,255,0.07)');
      sg.addColorStop(1, 'rgba(0,229,255,0)');
      ctx.fillStyle = sg;
      ctx.fillRect(16, sy - 26, DRAW_W - 32, 52);
    }
  }

  function drawHeader(now) {
    ctx.fillStyle = '#ffffff';
    ctx.font = '700 34px "Microsoft YaHei", sans-serif';
    const title = '◉ 大白直播间';
    ctx.fillText(title, 34, 62);
    const titleW = ctx.measureText(title).width;
    ctx.font = '600 22px "Microsoft YaHei", sans-serif';
    ctx.fillStyle = 'rgba(255,255,255,0.62)';
    const suffix = '· AI 任务直播';
    const suffixW = ctx.measureText(suffix).width;
    ctx.fillText(suffix, 34 + titleW + 12, 61);
    const live = titleW + 12 + suffixW;
    ctx.font = '600 24px "Microsoft YaHei", sans-serif';
    ctx.fillStyle = '#00e5ff';
    const blink = now % 1400 < 900;
    const lx = 34 + live + 18;
    if (blink) {
      ctx.fillStyle = '#ff4d6d';
      rr(ctx, lx, 40, 88, 30, 15);
      ctx.fill();
      ctx.fillStyle = '#fff';
      ctx.fillText('LIVE', lx + 18, 62);
    }
    // 待机指示
    ctx.font = '600 20px "Microsoft YaHei", sans-serif';
    const idleChip = '⚪ 待机 · 新信息自动全屏';
    const idleW = ctx.measureText(idleChip).width + 30;
    ctx.fillStyle = 'rgba(255,255,255,0.08)';
    rr(ctx, lx + 96, 44, idleW, 26, 13);
    ctx.fill();
    ctx.fillStyle = 'rgba(255,255,255,0.5)';
    ctx.fillText(idleChip, lx + 108, 63);
    // 时间
    const d = new Date();
    ctx.fillStyle = 'rgba(255,255,255,0.6)';
    ctx.font = '500 22px "Microsoft YaHei", sans-serif';
    ctx.textAlign = 'right';
    ctx.fillText(d.toLocaleTimeString('zh-CN', { hour12: false }), DRAW_W - 30, 60);
    ctx.textAlign = 'left';
  }

  function drawStats() {
    const st = { running: 0, confirming: 0, done: 0, error: 0 };
    for (const t of tasks) if (STATUS[t.status]) st[t.status] = (st[t.status] || 0) + 1;
    ctx.font = '600 22px "Microsoft YaHei", sans-serif';
    ctx.fillStyle = '#9be8ff';
    ctx.fillText(
      '执行中 ' + st.running + '  ·  待确认 ' + st.confirming + '  ·  已完成 ' + st.done + '  ·  异常 ' + st.error,
      34, 108
    );
  }

  // 媒体墙：每张按原始宽高比自适应槽位（竖图窄槽、横图宽槽、整体居中），
  // 就绪媒体完整 contain 显示不裁切；未就绪媒体显示呼吸骨架占位，排版不跳变。
  function drawMediaRow(now, rect) {
    const items = mediaItems.filter(it => !it.dead).slice(-(rect.n || 6));
    ctx.font = '600 19px "Microsoft YaHei", sans-serif';
    ctx.fillStyle = 'rgba(255,255,255,0.65)';
    ctx.fillText('📺 智能体产物 · 实时展示', GUTTER, rect.y0 - 14);

    const y0 = rect.y0;
    const th = rect.th;
    const gap = 12;
    const avail = DRAW_W - GUTTER * 2;
    // 槽宽 = 高 × 原比例（未就绪先按 16:9 占位），限制在 [56, 200] 防过窄/过宽
    const slots = items.map(it => {
      const ratio = (it.ready && it.w > 0 && it.h > 0) ? (it.w / it.h) : 16 / 9;
      return Math.max(56, Math.min(200, Math.round(th * ratio)));
    });
    // 总宽超出可用宽 → 等比微缩（保持比例关系），并整体水平居中
    const rawTotal = slots.reduce((a, b) => a + b, 0) + gap * (items.length - 1);
    const fit = rawTotal > avail ? avail / rawTotal : 1;
    const slotWs = slots.map(w => Math.max(48, Math.round(w * fit)));
    const netTotal = slotWs.reduce((a, b) => a + b, 0) + gap * (items.length - 1);
    const offsetX = Math.max(0, (avail - netTotal) / 2);
    const xs = [];
    let acc = GUTTER + offsetX;
    for (const w of slotWs) { xs.push(acc); acc += w + gap; }

    const tagFont = () => '600 ' + Math.max(13, Math.round(th * 0.16)) + 'px "Microsoft YaHei", sans-serif';
    for (let i = 0; i < items.length; i++) {
      const it = items[i];
      const x = xs[i];
      const slotW = slotWs[i];
      const r = 8;
      // 底板 + 描边（视频品红 / 图片青色）
      ctx.fillStyle = 'rgba(14,20,50,0.6)';
      rr(ctx, x - 3, y0 - 3, slotW + 6, th + 6, r + 1);
      ctx.fill();
      ctx.strokeStyle = it.kind === 'video' ? 'rgba(255,77,109,0.7)' : 'rgba(0,229,255,0.55)';
      ctx.lineWidth = 1.6;
      ctx.stroke();

      if (it.ready) {
        // 完整 contain：按原始宽高比缩放、居中，不裁切画面
        const iw = it.w || 1280, ih = it.h || 720;
        const sc = Math.min(slotW / iw, th / ih);
        const dw = iw * sc, dh = ih * sc;
        try { ctx.drawImage(it.el, x + (slotW - dw) / 2, y0 + (th - dh) / 2, dw, dh); } catch (e) {}
      } else {
        // 未就绪：骨架屏（呼吸脉冲底板 + 类型徽标 + 加载文案）
        const pulse = 0.5 + 0.3 * Math.abs(Math.sin(now / 340));
        ctx.fillStyle = 'rgba(42,62,112,' + (0.32 + pulse * 0.26) + ')';
        rr(ctx, x - 3, y0 - 3, slotW + 6, th + 6, r + 1);
        ctx.fill();
        ctx.font = tagFont();
        const tag = it.kind === 'video' ? '▶ 视频' : '🖼 图片';
        const tw2 = ctx.measureText(tag).width + 16;
        ctx.fillStyle = 'rgba(0,0,0,0.5)';
        rr(ctx, x + 5, y0 + 5, tw2, Math.max(18, Math.round(th * 0.22)), 12);
        ctx.fill();
        ctx.fillStyle = '#ffffff';
        ctx.fillText(tag, x + 12, y0 + 5 + Math.max(18, Math.round(th * 0.22)) - 6);
        ctx.font = '500 ' + Math.max(12, Math.round(th * 0.15)) + 'px "Microsoft YaHei", sans-serif';
        ctx.fillStyle = 'rgba(255,255,255,' + (0.4 + pulse * 0.3) + ')';
        const ltx = '加载中…';
        ctx.fillText(ltx, x + (slotW - ctx.measureText(ltx).width) / 2, y0 + th / 2 + 4);
      }
    }
  }

  // 工作流工具链面板：芯片流自适应宽度，超出截断
  function drawChainRow(now, rect) {
    const chain = rect.data;
    const y0 = rect.y0;
    const H = rect.h;
    ctx.fillStyle = 'rgba(16,22,52,0.5)';
    rr(ctx, GUTTER, y0 - 8, DRAW_W - GUTTER * 2, H + 4, 10);
    ctx.fill();
    ctx.strokeStyle = 'rgba(0,229,255,0.25)';
    ctx.lineWidth = 1.2;
    ctx.stroke();

    ctx.font = '600 17px "Microsoft YaHei", sans-serif';
    ctx.fillStyle = '#a49bff';
    ctx.fillText('⚡ 工作流工具链', GUTTER + 10, y0 + 17);

    let owner = '';
    if (chain.title) owner = chain.title;
    else if (chain.round) owner = '对话 · 第 ' + chain.round + ' 轮';
    if (owner) {
      ctx.font = '500 14px "Microsoft YaHei", sans-serif';
      ctx.fillStyle = 'rgba(255,255,255,0.5)';
      let ow = ctx.measureText(owner).width;
      if (ow > 200) { owner = owner.slice(0, 12) + '…'; ow = ctx.measureText(owner).width; }
      ctx.fillText(owner, DRAW_W - GUTTER - 10 - ow, y0 + 17);
    }

    const ST = {
      running: { c: '#00e5ff' },
      done:    { c: '#4ade80' },
      error:   { c: '#ff6b6b' },
      ended:   { c: '#8c8ca0' }
    };
    const mark = (s) =>
      s.status === 'running' ? '◌' :
      s.status === 'error'   ? '✕' :
      s.status === 'done'    ? '✓' : '·';
    let x = GUTTER + 150;
    const limit = DRAW_W - GUTTER - 10 - (owner ? Math.min(ctx.measureText(owner).width, 200) + 24 : 0);
    for (let i = 0; i < chain.steps.length; i++) {
      const s = chain.steps[i];
      const st = ST[s.status] || ST.ended;
      const nm = String(s.name || '步骤').replace(/_/g, ' ').slice(0, 12);
      const w = Math.max(66, ctx.measureText(nm).width + 34);
      if (x + w > limit) break;
      ctx.fillStyle = st.c + '20';
      rr(ctx, x, y0 - 2, w, 22, 11);
      ctx.fill();
      ctx.strokeStyle = st.c + (s.status === 'running' ? 'cc' : '88');
      ctx.lineWidth = s.status === 'running' ? 2 : 1.3;
      ctx.stroke();
      ctx.font = '600 15px "Microsoft YaHei", sans-serif';
      ctx.fillStyle = st.c;
      ctx.fillText(mark(s) + ' ' + nm, x + 9, y0 + 15);
      x += w + 14;
      if (i < chain.steps.length - 1) {
        ctx.font = '700 15px sans-serif';
        ctx.fillStyle = 'rgba(255,255,255,0.32)';
        ctx.fillText('›', x - 11, y0 + 15);
      }
    }
  }

  // 任务卡：高度弹性、按布局矩形排版，头部/状态/日志/结果互不重叠
  function drawTasks(now, rect) {
    const shown = rect.shown || [];
    const gap = 10;
    const n = shown.length;
    const perH = n ? (rect.h - gap * (n - 1)) / n : 0;
    for (let i = 0; i < n; i++) {
      const t = shown[i];
      const yb = rect.y0 + i * (perH + gap);
      const yt = yb + perH;
      const meta = STATUS[t.status] || { label: t.status, color: '#ccc', icon: '•' };
      const ag = agentOf(t);
      const hasRes = t.status === 'done' && (t.result || '').trim();
      const d2 = detailCache.get(t.id);

      // 卡片底
      ctx.fillStyle = 'rgba(20,26,58,0.55)';
      rr(ctx, GUTTER, yb, DRAW_W - GUTTER * 2, perH - 8, 12);
      ctx.fill();
      ctx.strokeStyle = ag.color + '77';
      ctx.lineWidth = 2;
      ctx.stroke();

      // 头部（相对卡片顶部，空间不足自动紧凑）
      const row1y = yb + 26;
      ctx.font = '24px sans-serif';
      ctx.fillText(ag.icon || '•', GUTTER + 24, row1y);
      ctx.font = '700 22px "Microsoft YaHei", sans-serif';
      ctx.fillStyle = ag.color;
      ctx.fillText(fitText(ag.name || '', 110), GUTTER + 58, row1y - 2);
      ctx.font = '700 24px "Microsoft YaHei", sans-serif';
      ctx.fillStyle = '#fff';
      ctx.fillText(fitText(t.title || '任务', DRAW_W - GUTTER * 2 - 340), GUTTER + 176, row1y);
      // 状态胶囊
      const label = meta.icon + ' ' + meta.label;
      ctx.font = '600 19px "Microsoft YaHei", sans-serif';
      const lw = ctx.measureText(label).width + 24;
      ctx.fillStyle = meta.color + '2e';
      rr(ctx, DRAW_W - GUTTER - lw, yb + 10, lw, 32, 16);
      ctx.fill();
      ctx.strokeStyle = meta.color + '88';
      ctx.lineWidth = 1.5;
      ctx.stroke();
      ctx.fillStyle = meta.color;
      ctx.textAlign = 'right';
      ctx.fillText(label, DRAW_W - GUTTER - 13, yb + 32);
      ctx.textAlign = 'left';

      // 第二行：最新步骤/日志（单行截断，绝不压到头部/底部）
      let line = '';
      if (t.status === 'confirming') line = '⏳ 等待用户确认后执行…';
      else if (hasRes) line = '✅ 已交付结果（见下方预览）';
      else if (d2 && d2.steps && d2.steps.length) line = d2.steps[d2.steps.length - 1];
      else if (d2 && d2.logs && d2.logs.length) line = '…' + d2.logs[d2.logs.length - 1];
      // 详情还没补到 → 用列表里的日志尾巴兜底（刷新后卡片不再只剩一个空状态）
      else if (t.logs_tail && t.logs_tail.length) line = '…' + t.logs_tail[t.logs_tail.length - 1];
      else if (t.status === 'error' && d2 && d2.error) line = '⚠️ ' + d2.error;
      if (line && perH > 80) {
        ctx.font = '400 20px "Microsoft YaHei", sans-serif';
        ctx.fillStyle = 'rgba(255,255,255,0.75)';
        ctx.fillText(fitText(line, DRAW_W - GUTTER * 2 - 60), GUTTER + 24, yb + 64);
      }

      // 时间小字（底部）
      ctx.font = '400 17px "Microsoft YaHei", sans-serif';
      ctx.fillStyle = 'rgba(255,255,255,0.38)';
      ctx.fillText(
        (d2 ? (d2.kind || '') + ' · ' : '') + new Date(t.updated_at).toLocaleTimeString('zh-CN', { hour12: false }),
        GUTTER + 24, yt - 12
      );

      // 完成结果预览条（第二行下方、时间上方）
      if (hasRes && perH > 104) {
        const py = yt - 36;
        ctx.fillStyle = 'rgba(74,222,128,0.12)';
        rr(ctx, GUTTER + 24, py - 4, DRAW_W - GUTTER * 2 - 48, 30, 8);
        ctx.fill();
        ctx.strokeStyle = 'rgba(74,222,128,0.4)';
        ctx.lineWidth = 1.5;
        ctx.stroke();
        ctx.fillStyle = '#a9f0c2';
        ctx.font = '500 18px "Microsoft YaHei", sans-serif';
        ctx.fillText(fitText('✓ ' + String(t.result || ''), DRAW_W - GUTTER * 2 - 90), GUTTER + 38, py + 16);
      }
    }
  }

  // 空屏待命画面（垂直居中于内容区）—— 直播间待机视觉
  function drawIdleScreen(now, L) {
    const midY = L.idleY;
    const pulse = 0.55 + 0.45 * Math.sin(now / 850);
    // 主标语：KEEP WATCHING（青色辉光呼吸）
    ctx.save();
    ctx.shadowColor = 'rgba(0,229,255,' + (0.30 + 0.30 * pulse).toFixed(2) + ')';
    ctx.shadowBlur = 22;
    ctx.fillStyle = 'rgba(255,255,255,0.95)';
    ctx.font = '700 44px "Microsoft YaHei", sans-serif';
    ctx.fillText('KEEP WATCHING', 34, midY - 36);
    ctx.restore();
    // 副标语：超能陆战队整装待命（白色大字）
    ctx.save();
    ctx.shadowColor = 'rgba(124,92,255,0.5)';
    ctx.shadowBlur = 12;
    ctx.fillStyle = '#ffffff';
    ctx.font = '700 32px "Microsoft YaHei", sans-serif';
    ctx.fillText('超能陆战队 · 整装待命', 35, midY + 8);
    ctx.restore();
    // 成员列阵（亮白）
    ctx.font = '600 23px "Microsoft YaHei", sans-serif';
    ctx.fillStyle = 'rgba(228,236,255,0.9)';
    ctx.fillText('🤖 DSH 智能体   ·   ⚙️ Codex   ·   ✦ OpenCode   ·   💻 后台命令', 35, midY + 64);
  }

  // 底部跑马灯（固定一行，不与其他区块重叠）
  function drawTicker(now, L) {
    const baseY = L.tickerY;
    const allLogs = [];
    for (const t of tasks) {
      const d = detailCache.get(t.id);
      // 详情还没补到 → 用列表里的日志尾巴兜底，刷新后跑马灯不再空白
      allLogs.push(...((d && d.logs && d.logs.length) ? d.logs : (t.logs_tail || [])));
    }
    if (!allLogs.length) {
      ctx.fillStyle = 'rgba(255,255,255,0.3)';
      ctx.font = '500 20px "Microsoft YaHei", sans-serif';
      const hint = '— 暂无实时日志 · 委派任务后这里会滚动直播 —';
      ctx.fillText(hint, (DRAW_W - ctx.measureText(hint).width) / 2, baseY);
      return;
    }
    const line = allLogs.slice(-6).map(s => s.replace(/\n/g, ' ')).filter(Boolean).join(' ◈ ');
    if (!line) return;
    ctx.font = '500 20px "Microsoft YaHei", sans-serif';
    const tw = ctx.measureText(line).width;
    const speed = 90;
    const off = ((now / 1000) * speed) % Math.max(tw + 60, 1);
    const x0 = DRAW_W + 20 - off;
    ctx.fillStyle = 'rgba(4,233,255,0.8)';
    ctx.fillText(line, x0, baseY);
    if (x0 + tw < 60) ctx.fillText(line, x0 + tw + 80, baseY);
  }

  // ---------- 全屏焦点（视频/图片/交互信息布满大屏） ----------

  // 全屏媒体：背景 = 原图放大铺满 + 模糊 + 压暗（保持"布满大屏"氛围），
  // 主体 = 完整 contain 按原始宽高比居中显示，绝不裁切画面；
  // 加载中为优雅骨架（呼吸圆环 + 类型提示），失败态居中提示。
  function drawShowcaseMedia(now) {
    const item = mediaItems.find(it => it.url === showcase.url);
    ctx.fillStyle = '#04060f';
    ctx.fillRect(0, 0, DRAW_W, DRAW_H);
    if (!item) return;

    if (item.ready && !item.dead) {
      const iw = item.w || 1280, ih = item.h || 720;
      // 1) 背景：cover 铺满原图帧 + 高斯模糊 + 压暗（营造深空氛围）。
      //    移动端跳过逐帧模糊（发热/解码饥饿主因），只保留暗色罩
      const bsc = Math.max(DRAW_W / iw, DRAW_H / ih);
      const bw = iw * bsc, bh = ih * bsc;
      if (!isMobileDev()) {
        let blurred = false;
        try { ctx.filter = 'blur(26px)'; blurred = true; } catch (e) {}
        try { ctx.drawImage(item.el, (DRAW_W - bw) / 2, (DRAW_H - bh) / 2, bw, bh); } catch (e) {}
        if (blurred) { try { ctx.filter = 'none'; } catch (e) {} }
      }
      ctx.fillStyle = 'rgba(6,10,30,0.55)';
      ctx.fillRect(0, 0, DRAW_W, DRAW_H);
      // 2) 主体：contain 完整显示（原比例，不裁切）
      const sc = Math.min(DRAW_W / iw, DRAW_H / ih);
      const dw = iw * sc, dh = ih * sc;
      const dx = (DRAW_W - dw) / 2, dy = (DRAW_H - dh) / 2;
      try { ctx.drawImage(item.el, dx, dy, dw, dh); } catch (e) {}
      // 3) 主体柔光描边，收边优美
      ctx.strokeStyle = 'rgba(255,255,255,0.14)';
      ctx.lineWidth = 2;
      rr(ctx, dx - 5, dy - 5, dw + 10, dh + 10, 10);
      ctx.stroke();
    } else if (item.dead) {
      ctx.fillStyle = 'rgba(255,107,107,0.9)';
      ctx.font = '700 40px "Microsoft YaHei", sans-serif';
      const w0 = ctx.measureText('⚠ 媒体加载失败').width;
      ctx.fillText('⚠ 媒体加载失败', (DRAW_W - w0) / 2, DRAW_H / 2 + 4);
      ctx.font = '500 24px "Microsoft YaHei", sans-serif';
      ctx.fillStyle = 'rgba(255,255,255,0.55)';
      const w1 = ctx.measureText('将自动回到待机直播画面…').width;
      ctx.fillText('将自动回到待机直播画面…', (DRAW_W - w1) / 2, DRAW_H / 2 + 52);
    } else {
      // 加载中：呼吸圆环 spinner + 类型提示（排版居中、节奏舒缓）
      const pulse = 0.5 + 0.4 * Math.sin(now / 260);
      const cx = DRAW_W / 2, cy = DRAW_H / 2 - 36;
      ctx.strokeStyle = 'rgba(0,229,255,0.22)';
      ctx.lineWidth = 5;
      ctx.beginPath();
      ctx.arc(cx, cy, 26, 0, Math.PI * 2);
      ctx.stroke();
      const ang = ((now / 1000) % 1) * Math.PI * 2;
      ctx.strokeStyle = 'rgba(0,229,255,' + (0.7 + 0.3 * pulse) + ')';
      ctx.beginPath();
      ctx.arc(cx, cy, 26, ang, ang + Math.PI * 1.4);
      ctx.stroke();
      ctx.font = '600 28px "Microsoft YaHei", sans-serif';
      ctx.fillStyle = 'rgba(255,255,255,0.85)';
      const t1 = '正在加载媒体…';
      ctx.fillText(t1, cx - ctx.measureText(t1).width / 2, cy + 70);
      ctx.font = '500 20px "Microsoft YaHei", sans-serif';
      ctx.fillStyle = 'rgba(255,255,255,0.45)';
      const t2 = item.kind === 'video' ? '▶ 视频生成完毕 · 等待缓冲' : '🖼 图片生成完毕 · 等待加载';
      ctx.fillText(t2, cx - ctx.measureText(t2).width / 2, cy + 108);
    }
  }

  function drawShowcaseInfo(now) {
    const g = ctx.createLinearGradient(0, 0, 0, DRAW_H);
    g.addColorStop(0, 'rgba(18,24,60,0.98)');
    g.addColorStop(0.55, 'rgba(10,15,42,0.98)');
    g.addColorStop(1, 'rgba(6,10,30,0.98)');
    ctx.fillStyle = g;
    ctx.fillRect(0, 0, DRAW_W, DRAW_H);

    const p = showcase.payload || {};
    const accent = p.accent || '#00e5ff';
    ctx.font = '700 34px "Microsoft YaHei", sans-serif';
    ctx.fillStyle = accent;
    ctx.fillText(p.title || '💬 最近交互', 100, 92);
    ctx.fillStyle = accent + '55';
    ctx.fillRect(100, 112, 120, 4);

    ctx.font = '600 40px "Microsoft YaHei", sans-serif';
    ctx.fillStyle = '#ffffff';
    const body = p.text || '';
    const allLines = wrapText(ctx, body, DRAW_W - 200);
    const lines = allLines.slice(0, 7);
    const lh = 66;
    let y = DRAW_H / 2 - (lines.length * lh) / 2 + 44;
    for (const ln of lines) {
      ctx.fillText(ln, 100, y);
      y += lh;
    }
    if (allLines.length > 7) {
      ctx.font = '600 32px "Microsoft YaHei", sans-serif';
      ctx.fillStyle = 'rgba(255,255,255,0.45)';
      ctx.fillText('……', DRAW_W / 2 - 34, y + 22);
    }
  }

  function drawShowcaseFooter(now) {
    const g = ctx.createLinearGradient(0, DRAW_H - 190, 0, DRAW_H);
    g.addColorStop(0, 'rgba(0,0,0,0)');
    g.addColorStop(1, 'rgba(0,0,0,0.72)');
    ctx.fillStyle = g;
    ctx.fillRect(0, DRAW_H - 190, DRAW_W, 190);

    const isMedia = showcase.kind === 'media';
    const shown = mediaItems.find(it => it.url === showcase.url);
    const tag = isMedia
      ? (shown && shown.kind === 'video' ? '▶ 视频 · 生成完毕' : '🖼 图片 · 生成完毕')
      : (showcase.payload && showcase.payload.tag) || '💬 最近交互';
    const tagC = isMedia ? '#ff4d6d' : (showcase.payload && showcase.payload.accent) || '#00e5ff';
    ctx.font = '700 26px "Microsoft YaHei", sans-serif';
    const tw2 = ctx.measureText(tag).width + 40;
    ctx.fillStyle = 'rgba(0,0,0,0.5)';
    rr(ctx, 48, DRAW_H - 112, tw2, 48, 24);
    ctx.fill();
    ctx.strokeStyle = tagC + 'cc';
    ctx.lineWidth = 1.6;
    ctx.stroke();
    ctx.fillStyle = '#ffffff';
    ctx.fillText(tag, 68, DRAW_H - 79);

    const remainMs = Math.max(0, SHOWCASE_MS - (Date.now() - showcase.ts));
    const remainS = Math.ceil(remainMs / 1000);
    ctx.font = '600 26px "Microsoft YaHei", sans-serif';
    ctx.fillStyle = 'rgba(255,255,255,0.85)';
    ctx.textAlign = 'right';
    ctx.fillText('⏳ 停留 ' + remainS + 's · 无新信息自动回到待机', DRAW_W - 48, DRAW_H - 79);
    ctx.textAlign = 'left';

    const frac = Math.max(0, remainMs / SHOWCASE_MS);
    ctx.fillStyle = 'rgba(255,255,255,0.16)';
    rr(ctx, 48, DRAW_H - 40, DRAW_W - 96, 6, 3);
    ctx.fill();
    ctx.fillStyle = tagC;
    rr(ctx, 48, DRAW_H - 40, Math.max(6, (DRAW_W - 96) * frac), 6, 3);
    ctx.fill();
  }

  function drawShowcase(now) {
    ctx.clearRect(0, 0, DRAW_W, DRAW_H);
    if (showcase.kind === 'media') drawShowcaseMedia(now);
    else drawShowcaseInfo(now);
    ctx.strokeStyle = 'rgba(0,229,255,0.55)';
    ctx.lineWidth = 3;
    rr(ctx, 3, 3, DRAW_W - 6, DRAW_H - 6, 14);
    ctx.stroke();
    drawShowcaseFooter(now);
  }

  // ---------- 大白影院：视频直播全屏画面 ----------
  // 三段式布局：顶栏(片名) / 正片区(独占中间，不被遮挡) / 底栏(进度条)，互不重叠

  function videoFmtTime(s) {
    s = Math.max(0, Math.floor(s || 0));
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), ss = s % 60;
    return h ? h + ':' + String(m).padStart(2, '0') + ':' + String(ss).padStart(2, '0')
             : m + ':' + String(ss).padStart(2, '0');
  }

  function drawVideoLive(now, newFrame) {
    const it = videoLive;
    const v = it.el;
    it.dippedNewFrame = false;              // 本次绘制是否消费了新视频帧（看门狗喂食统计）
    const TOP_H = 84;                       // 顶栏：影院标识 + 片名/UP主
    const BOT_H = 64;                       // 底栏：进度条 + 时间/状态
    const vy0 = TOP_H, vy1 = DRAW_H - BOT_H, vh = vy1 - vy0;
    const vcy = (vy0 + vy1) / 2;

    ctx.fillStyle = '#04060f';
    ctx.fillRect(0, 0, DRAW_W, DRAW_H);

    const iw = v.videoWidth || 1280, ih = v.videoHeight || 720;

    // ---- 正片区 ----
    if (it.ready && !it.dead) {
      // 氛围底：当前帧 cover 放大 + 模糊 + 压暗（只铺正片区）。
      // 移动端跳过逐帧大面积高斯模糊与再放大绘制（发热/解码饥饿的最大贡献点），
      // 只用暗色罩营造氛围，显著降低 canvas 每帧开销。
      // 桌面端模糊底做离屏缓存：只在新帧到达时按 ~1.6fps 重渲（26px 高斯模糊
      // 看不出 600ms 延迟），其余重绘直接用缓存 blit —— 这是桌面端单帧最大开销，
      // 压垮主线程/GPU 正是「画面冻结、声音照常」的常见诱因。
      if (newFrame && (!it.lastBlurTs || now - it.lastBlurTs > 600)) {
        it.refreshBlur = true;
        it.lastBlurTs = now;
      }
      if (!isMobileDev()) {
        const bsc = Math.max(DRAW_W / iw, vh / ih);
        const bw = Math.round(iw * bsc), bh = Math.round(ih * bsc);
        if (it.refreshBlur || !__vhBlur || __vhBlur.width !== bw || __vhBlur.height !== bh) {
          it.refreshBlur = false;
          if (!__vhBlur) __vhBlur = document.createElement('canvas');
          if (__vhBlur.width !== bw || __vhBlur.height !== bh) {
            __vhBlur.width = bw; __vhBlur.height = bh;
          }
          const bc = __vhBlur.getContext('2d');
          if (bc) {
            try { bc.filter = 'blur(26px)'; } catch (e) {}
            try { bc.drawImage(v, 0, 0, bw, bh); } catch (e) {}
            try { bc.filter = 'none'; } catch (e) {}
          } else { __vhBlur = null; }
        }
        if (__vhBlur) {
          try { ctx.drawImage(__vhBlur, (DRAW_W - bw) / 2, vcy - bh / 2, bw, bh); } catch (e) {}
        }
      }
      ctx.fillStyle = 'rgba(6,10,30,0.45)';
      ctx.fillRect(0, vy0, DRAW_W, vh);

      // 正片：contain 完整显示在正片区（上下留 10px 呼吸空间）
      const sc = Math.min(DRAW_W / iw, (vh - 20) / ih);
      const dw = iw * sc, dh = ih * sc;
      const dx = (DRAW_W - dw) / 2, dy = vcy - dh / 2;
      try { ctx.drawImage(v, dx, dy, dw, dh); } catch (e) {}
      ctx.strokeStyle = 'rgba(255,255,255,0.14)';
      ctx.lineWidth = 2;
      rr(ctx, dx - 5, dy - 5, dw + 10, dh + 10, 10);
      ctx.stroke();
      // 真实把一帧画上了大屏 → 喂「绘制停滞看门狗」（paintStall）：
      // 解码/时间都在走但渲染管线（canvas/合成/纹理上传）卡住时也按卡死处理。
      // rVFC 浏览器：只有这次重绘真的消费了新视频帧才算「画了」：复用旧帧的重绘不算数，
      // 否则画面冻结但重绘循环还在跑时，看门狗被持续喂饱、永远不触发（旧 bug）。
      // 无 rVFC 浏览器（Firefox/老电视盒子）：没有新帧通知可用，framePending 恒为 false，
      // 若也按「消费新帧」喂食，播 10 秒必误判 paintStall → 无限恢复循环直到死亡。
      // 这类浏览器的画面冻结交给像素探针（pxStall）兜底，paint 看门狗恒喂饱即视为不适用。
      if (newFrame || !it.rfvc) it.dippedNewFrame = true;
      if (videoLive && videoLive.el === v && it.dippedNewFrame) videoLive.lastDipTs = Date.now();

      // 暂停标记（正片区中央）
      if (v.paused) {
        ctx.fillStyle = 'rgba(0,0,0,0.55)';
        rr(ctx, DRAW_W / 2 - 86, vcy - 38, 172, 76, 16);
        ctx.fill();
        ctx.font = '700 40px "Microsoft YaHei", sans-serif';
        ctx.fillStyle = '#ffffff';
        const t = '⏸ 已暂停';
        ctx.fillText(t, DRAW_W / 2 - ctx.measureText(t).width / 2, vcy + 14);
      }
      // 缓冲中（播放着但数据不够）
      if (!v.paused && v.readyState <= 2) {
        const ang = ((now / 1000) % 1) * Math.PI * 2;
        ctx.strokeStyle = 'rgba(0,229,255,0.75)';
        ctx.lineWidth = 5;
        ctx.beginPath();
        ctx.arc(DRAW_W / 2, vcy, 24, ang, ang + Math.PI * 1.3);
        ctx.stroke();
      }
    } else if (it.dead) {
      ctx.fillStyle = 'rgba(255,107,107,0.92)';
      ctx.font = '700 42px "Microsoft YaHei", sans-serif';
      const t0 = '⚠ ' + (it.errText || '视频加载失败');
      ctx.fillText(t0, (DRAW_W - ctx.measureText(t0).width) / 2, vcy - 6);
      ctx.font = '500 24px "Microsoft YaHei", sans-serif';
      ctx.fillStyle = 'rgba(255,255,255,0.6)';
      const t1 = '将自动回到待机画面…';
      ctx.fillText(t1, (DRAW_W - ctx.measureText(t1).width) / 2, vcy + 44);
    } else if (it.phase === 'recovering') {
      // 断流自动恢复中：旋转圆环 + 进度提示（正片区中央）
      const pulse = 0.5 + 0.4 * Math.sin(now / 260);
      ctx.strokeStyle = 'rgba(255,184,77,0.28)';
      ctx.lineWidth = 5;
      ctx.beginPath();
      ctx.arc(DRAW_W / 2, vcy - 16, 26, 0, Math.PI * 2);
      ctx.stroke();
      const ang = ((now / 700) % 1) * Math.PI * 2;
      ctx.strokeStyle = 'rgba(255,184,77,' + (0.75 + 0.25 * pulse) + ')';
      ctx.beginPath();
      ctx.arc(DRAW_W / 2, vcy - 16, 26, ang, ang + Math.PI * 1.4);
      ctx.stroke();
      ctx.font = '600 28px "Microsoft YaHei", sans-serif';
      ctx.fillStyle = 'rgba(255,255,255,0.9)';
      const t1 = '🔄 网络波动 · 正在自动恢复（' + (it.recoveryTries || 1) + '/' + MAX_RECOVERY + '）…';
      ctx.fillText(t1, DRAW_W / 2 - ctx.measureText(t1).width / 2, vcy + 56);
      ctx.font = '500 20px "Microsoft YaHei", sans-serif';
      ctx.fillStyle = 'rgba(255,255,255,0.5)';
      const t2 = '恢复后将从断点附近继续播放';
      ctx.fillText(t2, DRAW_W / 2 - ctx.measureText(t2).width / 2, vcy + 90);
    } else {
      // 取流中：呼吸圆环（正片区中央）
      const pulse = 0.5 + 0.4 * Math.sin(now / 260);
      ctx.strokeStyle = 'rgba(0,229,255,0.22)';
      ctx.lineWidth = 5;
      ctx.beginPath();
      ctx.arc(DRAW_W / 2, vcy - 16, 26, 0, Math.PI * 2);
      ctx.stroke();
      const ang = ((now / 1000) % 1) * Math.PI * 2;
      ctx.strokeStyle = 'rgba(0,229,255,' + (0.7 + 0.3 * pulse) + ')';
      ctx.beginPath();
      ctx.arc(DRAW_W / 2, vcy - 16, 26, ang, ang + Math.PI * 1.4);
      ctx.stroke();
      ctx.font = '600 28px "Microsoft YaHei", sans-serif';
      ctx.fillStyle = 'rgba(255,255,255,0.85)';
      const t1 = it.triedFallback ? '源不兼容 · 已切换转码流…' : '正在解析视频流…';
      ctx.fillText(t1, DRAW_W / 2 - ctx.measureText(t1).width / 2, vcy + 56);
    }

    // ---- 顶栏：影院标识 + 片名（独立底板，不遮正片）----
    ctx.fillStyle = 'rgba(10,14,36,0.98)';
    ctx.fillRect(0, 0, DRAW_W, TOP_H);
    ctx.fillStyle = 'rgba(0,229,255,0.28)';
    ctx.fillRect(0, TOP_H - 2, DRAW_W, 2);

    let lx = 34;
    ctx.font = '700 25px "Microsoft YaHei", sans-serif';
    ctx.fillStyle = '#ff4d6d';
    const cine = '▶ 大白影院';
    ctx.fillText(cine, lx, 52);
    lx += ctx.measureText(cine).width + 16;
    if (now % 1400 < 950) {
      ctx.fillStyle = '#ff4d6d';
      rr(ctx, lx, 32, 70, 26, 13);
      ctx.fill();
      ctx.fillStyle = '#fff';
      ctx.font = '700 17px "Microsoft YaHei", sans-serif';
      ctx.fillText('LIVE', lx + 16, 50);
    }
    lx += 82;
    const pf = it.info.platform || '';
    if (pf) {
      const label = /bili/i.test(pf) ? '哔哩哔哩' : /acfun/i.test(pf) ? 'AcFun' : pf;
      ctx.font = '600 19px "Microsoft YaHei", sans-serif';
      const lw = ctx.measureText(label).width + 26;
      ctx.fillStyle = 'rgba(0,229,255,0.16)';
      rr(ctx, lx, 30, lw, 30, 15);
      ctx.fill();
      ctx.strokeStyle = 'rgba(0,229,255,0.5)';
      ctx.lineWidth = 1.4;
      ctx.stroke();
      ctx.fillStyle = '#9be8ff';
      ctx.fillText(label, lx + 13, 51);
    }

    // 片名右对齐（与左侧标识之间留白），UP主/清晰度小字在下
    ctx.font = '700 27px "Microsoft YaHei", sans-serif';
    ctx.fillStyle = '#ffffff';
    ctx.textAlign = 'right';
    ctx.fillText(fitText(it.info.title || '在线视频', DRAW_W - 420), DRAW_W - 34, 42);
    ctx.font = '500 18px "Microsoft YaHei", sans-serif';
    ctx.fillStyle = 'rgba(255,255,255,0.62)';
    const sub = (it.info.uploader ? 'UP：' + it.info.uploader : '') +
                (it.info.height ? ' · ' + it.info.height + 'P' : '');
    if (sub) ctx.fillText(fitText(sub, DRAW_W - 420), DRAW_W - 34, 68);
    ctx.textAlign = 'left';

    // ---- 底栏：进度条 + 时间/状态（独立底板，不遮正片）----
    ctx.fillStyle = 'rgba(10,14,36,0.98)';
    ctx.fillRect(0, vy1, DRAW_W, BOT_H);
    ctx.fillStyle = 'rgba(0,229,255,0.28)';
    ctx.fillRect(0, vy1, DRAW_W, 2);

    const realDur = videoKnownDuration(it);   // relay 合流用元数据真实时长（点播总长）
    const hasDur = realDur > 0 && it.ready;
    const curT = videoRealPosition(it);
    const durT = hasDur ? realDur : 0;
    const barW = DRAW_W - 96;
    const barY = vy1 + 40;

    ctx.font = '600 21px "Microsoft YaHei", sans-serif';
    ctx.fillStyle = 'rgba(255,255,255,0.92)';
    const timeTxt = hasDur
      ? videoFmtTime(curT) + ' / ' + videoFmtTime(durT)
      : (it.ready ? '时长未知 · 暂不支持拖动' : '—');
    ctx.fillText(timeTxt, 48, vy1 + 27);

    ctx.textAlign = 'right';
    const chips = [];
    if (it.ready && v.muted) chips.push(it.mutedAutoplay ? '🔇 点按任意处开声音' : '🔇 已静音');
    if (it.info.mode === 'relay' && it.ready) chips.push('点播 · 高清合流');
    if (it.ended) chips.push('▶ 取下一部…');
    if (chips.length) {
      ctx.fillStyle = 'rgba(255,255,255,0.72)';
      ctx.fillText(chips.join(' · '), DRAW_W - 48, vy1 + 27);
    }
    ctx.textAlign = 'left';

    // 进度条：直播式流（时长未知）用流动光条，其余按真实进度
    ctx.fillStyle = 'rgba(255,255,255,0.18)';
    rr(ctx, 48, barY, barW, 8, 4);
    ctx.fill();
    if (hasDur) {
      const frac = Math.max(0, Math.min(1, curT / durT));
      const w = Math.max(6, barW * frac);
      ctx.fillStyle = '#ff4d6d';
      rr(ctx, 48, barY, w, 8, 4);
      ctx.fill();
      ctx.beginPath();
      ctx.arc(48 + w, barY + 4, 6, 0, Math.PI * 2);
      ctx.fillStyle = '#ffffff';
      ctx.fill();
    } else if (it.ready) {
      const w = barW * (0.22 + 0.1 * Math.sin(now / 500));
      const off = ((now / 2600) % 1) * (barW - barW * 0.4);
      ctx.fillStyle = 'rgba(255,77,109,0.85)';
      rr(ctx, 48 + off, barY, w, 8, 4);
      ctx.fill();
    }
  }
  // ---------- 主绘制 ----------

  function draw(now, vNewFrame) {
    ctx.clearRect(0, 0, DRAW_W, DRAW_H); // 画布固定 720，下部透明区域由纹理裁切隐藏


    if (videoLive) { drawVideoLive(now, !!vNewFrame); return; }   // 大白影院：视频直播全屏
    if (showcase) { drawShowcase(now); return; }  // 全屏焦点：恒 16:9 满幅

    const L = computeLayout(Math.round(displayH)); // 布局跟随平滑中的屏幕边缘
    drawBackdrop(now);
    drawHeader(now);
    drawStats();
    if (L.media) drawMediaRow(now, L.media);
    if (L.chain) drawChainRow(now, L.chain);
    if (L.idle) drawIdleScreen(now, L);
    else if (L.tasks) drawTasks(now, L.tasks);
    drawTicker(now, L);
  }

  // ---------- 每帧更新：跟随角色身后 + 面向相机 + 尺寸自适应 + 节流重绘 ----------

  App.updateTaskBigScreen = function updateTaskBigScreen(dt) {
    if (!App.modelGroup) return;
    ensureBoard();
    if (!ready) return;

    // 大白影院：断流看门狗（卡死/取流超时自动恢复）+ 失败 5 秒回待机 + 5 秒进度上报
    if (videoLive) {
      const vl = videoLive;
      const vd = vl.el;
      if (vl.dead) {
        if (Date.now() - (vl.errAt || 0) > 5000) videoTeardown(true);
      } else if (vl.phase === 'playing' && !vd.paused) {
        // 卡死看门狗，四种形态：
        // ① 缓冲无进展：readyState≤2 且 timeupdate 停滞（传统断流）；
        // ② 有声音但画面冻结：解码器停止交付视频帧（rVFC 不再回调）。
        //    此时 currentTime 仍随音频前进、readyState 仍≥3，
        //    旧条件两个都不满足 → 永远不恢复，画面永久卡死。这是主 bug。
        // ③ 绘制停滞：解码/帧都在走，但真正把帧画上大屏的动作停了
        //    （canvas/合成/纹理上传卡住时 rVFC 仍可能回调）→ 同样按卡死处理。
        // ④ 无 rVFC 的老浏览器：解码器停交付帧无法直接得知，用轻量像素探针兜底
        //    （currentTime 前进但画面像素不变）。
        const bufferStall = vd.readyState <= 2 &&
          Date.now() - (vl.lastProgressTs || 0) > STALL_MS;
        const frameStall = !!vl.rfvc &&
          Date.now() - (vl.lastFrameTs || 0) > STALL_MS;
        // rVFC 浏览器：lastDipTs 只在真实绘制了新帧时喂；无 rVFC：绘制循环正常即可喂
        // （画面冻结由像素探针负责，paint 看门狗恒喂饱=不适用，避免误判引发恢复风暴）
        const paintStall = Date.now() - (vl.lastDipTs || 0) > STALL_MS;
        const pxStall = !vl.rfvc && videoPxStallProbe(vl, vd);
        if (bufferStall || frameStall || paintStall || pxStall) videoRecover('stall'); // 卡死 → 自动恢复
      } else if (vl.phase === 'loading' &&
                 Date.now() - (vl.loadStartedAt || 0) >
                 (vl.triedFallback ? LOAD_TIMEOUT_MS * 2 : LOAD_TIMEOUT_MS)) {
        videoRecover('load');   // 初次取流迟迟不起播 → 强制重解析
      } else if (Date.now() - (vl.lastReport || 0) > 5000) {
        videoReportState();
      }
    }

    // 全屏焦点超时（1 分钟无新信息）→ 回到待机（大白影院播放中不超时）
    if (showcase && !videoLive && Date.now() - showcase.ts >= SHOWCASE_MS) {
      showcase = null;
      dirty = true;
    }
    // 焦点媒体加载失败 → 3 秒后回待机
    if (showcase && showcase.kind === 'media') {
      const it = mediaItems.find(x => x.url === showcase.url);
      if (it && it.dead) {
        if (!showcase.deadAt) showcase.deadAt = Date.now();
        else if (Date.now() - showcase.deadAt > 3000) {
          showcase = null;
          dirty = true;
        }
      }
    }

    // 内容量决定屏幕目标形状：大白影院/全屏焦点恒满幅；待机按布局内容高自适应。
    // displayH 做平滑插值（帧率无关指数趋近），宽度也随内容占比参与缩放
    const targetH = (videoLive || showcase) ? DRAW_H : computeLayout().contentH;
    const delta = targetH - displayH;
    if (Math.abs(delta) > 0.5) {
      displayH += delta * (1 - Math.exp(-dt * 7)); // 约 0.4s 平滑到位
      dirty = true; // 平滑期间高频重绘，让跑马灯/待命区跟随屏幕边缘
    } else {
      displayH = targetH;
    }
    // VR 放大系数平滑过渡（进入/退出 VR 时大屏缓缓变大/复原，不瞬跳）
    const vrTarget = App.xrPresenting ? VR_BOARD_SCALE : 1;
    if (Math.abs(vrTarget - vrScaleCur) > 0.005) {
      vrScaleCur += (vrTarget - vrScaleCur) * (1 - Math.exp(-dt * 5));
    } else {
      vrScaleCur = vrTarget;
    }
    if (board && frame) {
      board.visible = true;
      frame.visible = true;
      applyDisplaySize();
    }

    // 大屏可见时恢复直播视频播放（大白影院仅在"被游戏模式自动暂停"时续播，
    // AI/用户主动暂停的保持暂停）
    if (videoLive && videoLive.el && videoLive.ready && !videoLive.dead &&
        videoLive.el.paused && videoLive.autoPaused && !videoLive.userPaused) {
      try {
        const p = videoLive.el.play();
        if (p && p.catch) p.catch(() => {});
      } catch (e) {}
      videoLive.autoPaused = false;
    }
    // 媒体墙视频：大白影院播放中时全部暂停（无谓的背景解码是解热+抢解码资源
    // 的主因，直接诱发主视频画面冻结）；影院结束后恢复循环播放
    for (const it of mediaItems) {
      if (it.kind !== 'video' || !it.el) continue;
      if (videoLive && videoLive.el) {
        if (!it.el.paused) { try { it.el.pause(); } catch (e) {} }
      } else if (it.ready && !it.dead && it.el.paused) {
        try { const p = it.el.play(); if (p && p.catch) p.catch(() => {}); } catch (e) {}
      }
    }

    const THREE = App.THREE;
    const avatar = App.modelGroup;

    // VR 沉浸模式：大屏锚定在世界固定位置（像影院里挂好的幕布）——
    // 进入 VR 后按进入视角计算一次位姿并定格，此后不再每帧改写；
    // 大屏同时在 _xrWorldObjs 世界对象列表里，随世界整体平移/旋转
    // （VR 里玩家移动 = 世界反向位移），因此大屏世界位置恒定不变，
    // 玩家可以真实地走近/远离，角色转身/跳舞、玩家走动/转头都不带动大屏。
    // 退出 VR 恢复「跟随角色身后 + 面向相机」的常规逻辑。
    if (!App.xrPresenting) {
      vrFrozen = false;
      // 大屏跟随角色身后 + 面向主相机（普通模式，每帧）
      const camX = App.camera.position.x;
      const camY = App.camera.position.y;
      const camZ = App.camera.position.z;
      const back = new THREE.Vector3(
        avatar.position.x - camX, 0, avatar.position.z - camZ
      );
      if (back.lengthSq() < 0.0001) back.set(0, 0, -1);
      back.normalize();
      const dist = 1.55;
      // 屏幕高度：满幅焦点 2.85m；待机窄幅自动降到 ~1.9m（角色头肩高度）。
      // 之前固定 2.85m 会把待机小横幅顶到默认机位画面上方之外（观感=大屏黑屏/消失），
      // 现在按展示高度平滑插值，待机横幅始终完整入画。
      const SCREEN_LIFT_FULL = 2.85;
      const SCREEN_LIFT_IDLE = 1.90;
      const liftFrac = Math.max(0, Math.min(1, (displayH - MIN_CONTENT_H) / (DRAW_H - MIN_CONTENT_H)));
      const lift = SCREEN_LIFT_IDLE + (SCREEN_LIFT_FULL - SCREEN_LIFT_IDLE) * liftFrac;
      board.position.set(
        avatar.position.x + back.x * dist,
        avatar.position.y + lift,
        avatar.position.z + back.z * dist
      );
      board.lookAt(camX, camY, camZ);
      frame.position.copy(board.position);
      frame.quaternion.copy(board.quaternion);
    } else if (!vrFrozen) {
      // 进入 VR 后首个有效头部帧：按进入视角计算并定格大屏世界位姿。
      // 头显位置：优先 XR 相机矩阵（_xrHeadPos 由手柄循环兜底）。
      // camY>0.5 是就绪探测：会话首帧 XR 相机矩阵可能仍是单位矩阵（头在原点/地面），
      // 直接定格会把屏焊死在脚边，须跳过等下一帧真实头部数据。
      let camX = App._xrHeadPos && App._xrHeadPos.x;
      let camY = App._xrHeadPos && App._xrHeadPos.y;
      let camZ = App._xrHeadPos && App._xrHeadPos.z;
      try {
        const renderer = App.renderer;
        const xrCam = renderer && renderer.xr ? renderer.xr.getCamera() : null;
        if (xrCam && xrCam.matrixWorld) {
          const e = xrCam.matrixWorld.elements;
          camX = e[12]; camY = e[13]; camZ = e[14];
        }
      } catch (_) {}
      if (camX == null) { camX = 0; camY = 1.6; camZ = 0; }
      if (camY > 0.5) {
        const back = new THREE.Vector3(
          avatar.position.x - camX, 0, avatar.position.z - camZ
        );
        if (back.lengthSq() < 0.0001) back.set(0, 0, -1);
        back.normalize();
        const dist = 1.55;
        const SCREEN_LIFT_FULL = 2.85;
        const SCREEN_LIFT_IDLE = 1.90;
        const liftFrac = Math.max(0, Math.min(1, (displayH - MIN_CONTENT_H) / (DRAW_H - MIN_CONTENT_H)));
        const lift = SCREEN_LIFT_IDLE + (SCREEN_LIFT_FULL - SCREEN_LIFT_IDLE) * liftFrac
                     + 0.5;  // VR 巨幕整体再抬高 0.5m
        board.position.set(
          avatar.position.x + back.x * dist,
          avatar.position.y + lift,
          avatar.position.z + back.z * dist
        );
        board.lookAt(camX, camY, camZ);
        frame.position.copy(board.position);
        frame.quaternion.copy(board.quaternion);
        // 注册进世界对象列表：后续世界平移/旋转（= 玩家移动/转身/缩放）带动大屏，
        // 保持其世界位置不变（ensureBoard 早于 VR 创建时已由 _xrCaptureWorld 收集，
        // 这里兜底迟创建/迟进入的情况）
        if (App._xrWorldObjs && App._xrWorldObjs.indexOf(board) < 0) {
          App._xrWorldObjs.push(board, frame);
        }
        vrFrozen = true;
      }
    }
    // else：VR 已定格 → 位姿保持世界固定，不随角色/头显改写（尺寸缩放照常更新）

    const now = performance.now();
    const cinemaLive = !!(videoLive && videoLive.ready && !videoLive.dead);
    const newFrame = cinemaLive && !!(videoLive.framePending) && !videoLive.el.paused;
    const hasLiveVideo = cinemaLive ||
      (showcase && showcase.kind === 'media') ||
      mediaItems.some(it => it.ready && !it.dead && it.kind === 'video');
    const animating = Math.abs(targetH - displayH) > 0.5; // 尺寸过渡中 → 高帧率
    const mobile = isMobileDev();
    // 移动端下调最大重绘率：降低 canvas 重绘 + GPU 纹理上传（3.7MB/帧）的持续压力
    const frameMs = animating ? 33 :
      (cinemaLive ? 40 : (hasLiveVideo ? (mobile ? 140 : 110) : (mobile ? 260 : 180)));
    let shouldDraw = dirty || now - lastDraw > frameMs;
    if (cinemaLive && videoLive.rfvc) {
      // 影院重绘跟随真实新帧（rVFC）：无新帧时不再高频重绘+上传纹理。
      // 「有声音但画面冻结」时 rVFC 停摆 → 重绘停下（降温）+ 看门狗超时触发恢复。
      // 有新帧时也限最高 ~30fps：720p 画布 + 每帧 3.7MB 纹理上传按视频原始帧率
      // （60fps 高帧率源）持续绘制会压满 GPU 带宽/主线程，正是诱发「画面冻结、
      // 声音照常」的要因之一。帧流健康（600ms 内有新帧）时忽略 timeupdate 的
      // dirty 重绘请求（进度 UI 随帧绘制更新即可）；帧停摆后由 dirty/兜底刷新。
      const framesRecent = (now - (videoLive.lastFrameTs || 0)) < 600;
      shouldDraw = (newFrame && now - lastDraw >= 33) ||
                   (dirty && !framesRecent) || now - lastDraw > (mobile ? 1000 : 500);
      // 兜底：帧循环本身被降档（性能档 20fps）或后台标签页节流时，rAF 间隔可能
      // 超过 33ms，若只靠 newFrame 触发，新帧会被反复跳过 → 画面停滞（声音照常）。
      // 距上次重绘超过 120ms 仍无一次成功绘制 → 强制画一帧，保证画面持续前进。
      if (!shouldDraw && now - lastDraw > 120) shouldDraw = true;
    }

    if (shouldDraw) {
      if (canvas && texture) {
        // 网页渲染桥优先：iframe 截图流正常（ready 且最近 2s 内有帧）时，
        // 直接用网页帧（postMessage 已把帧画进 canvas），不再手绘。
        // 视频直播（cinema）保持原路径：html2canvas 截不了 video 元素，
        // 影院画面必须走视频帧直绘，否则大屏会黑屏。
        const webFresh = webRender.state === 'ready' &&
          (now - webRender.lastFrame) < 2000 &&
          !cinemaLive;
        if (webFresh) {
          if (texture) texture.needsUpdate = true;
        } else {
          draw(now, newFrame);
          texture.needsUpdate = true;
        }
      }
      // framePending 只在「真的执行了重绘」时清零：shouldDraw=false 的帧保留标记，
      // 否则新帧到达后若被节流跳过，标记被清 → 下一轮 newFrame=false → 永远画不上（丢帧卡帧）。
      if (videoLive && newFrame && shouldDraw) videoLive.framePending = false;
      lastDraw = now;
      dirty = false;
    }
  };

  // 首次有任务时提示
  App.taskBoardOnNotify = function taskBoardOnNotify() {
    if (App.showToast) App.showToast('📺 任务进度已投到角色身后的大屏');
  };
});
