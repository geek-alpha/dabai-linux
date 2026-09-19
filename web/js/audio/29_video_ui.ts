// ========== 在线视频管理 UI（搜索 / 大屏播放 / 连播队列） ==========
// 复用 video 技能的原生能力：
//   - 搜索：GET  /api/video_hub/api/search?q=...&platform=...&sort=...&limit=...
//   - 播放：POST /api/video_hub/api/play （body: {query 或 url}）→ entry
//   - 画面投到直播大屏（大白影院）：构造 play_video 屏幕指令调 App.videoBoardPlay
// 与 music 模块同风格：modal 弹窗 + 搜索框 + 结果卡片列表

import type { AppKernel } from '../types/app-kernel.js';

export interface VideoItem {
  title: string;
  webpage_url: string;
  platform?: string;
  uploader?: string;
  duration?: number | null;
  view_count?: number;
  thumbnail?: string;
}

const PLATFORM_LABEL: Record<string, string> = {
  bilibili: 'B站',
  acfun: 'AcFun',
  youtube: 'YouTube',
};

function fmtTime(sec: number | null | undefined): string {
  if (!sec || sec <= 0) return '';
  const s = Math.round(Number(sec));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const ss = s % 60;
  if (h > 0) return h + ':' + String(m).padStart(2, '0') + ':' + String(ss).padStart(2, '0');
  return m + ':' + String(ss).padStart(2, '0');
}

function fmtViews(n: number | undefined): string {
  if (!n || n <= 0) return '';
  if (n >= 10000) return (n / 10000).toFixed(1) + '万播放';
  return n + '播放';
}

function fmtDate(ts: number | undefined): string {
  if (!ts) return '';
  const d = new Date(ts * 1000);
  return (d.getMonth() + 1) + '月' + d.getDate() + '日';
}

export default function init_29_video_ui(App: AppKernel) {
  /* ---------- 搜索结果分页状态（下拉持续加载更多） ---------- */
  const PAGE_SIZE = 12;  // 每页条数（与后端 limit 一致）
  type SearchPager = { keyword: string; platform: string; sort: string; page: number; loading: boolean; hasMore: boolean };
  let searchPager: SearchPager = { keyword: '', platform: 'all', sort: 'relevance', page: 0, loading: false, hasMore: true };
  let renderedUrls: Set<string> = new Set(); // 已渲染卡片 url（跨页去重：B站排序漂移/AcFun重复返回）
  let hotMode = false;   // 结果区是否处于「热门推荐」模式（打开弹窗时自动加载）
  let hotLoadSeq = 0;    // 热门请求序号：防止并发请求串结果

  /* ---------- 收藏夹本地状态 ---------- */
  let lastSearchVideos: VideoItem[] = [];   // 最近一次搜索结果（收藏后刷新按钮状态用）
  let favButtons: Map<string, HTMLButtonElement> = new Map(); // url -> 搜索卡片上的收藏按钮
  let playedSearchItem: VideoItem | null = null; // 最近从搜索结果里点播的那部（队列空时按搜索顺序连播的游标）
  App._videoFavCache = App._videoFavCache || new Map();       // url -> favorite id
  App._videoFavFilter = null;                                  // null=全部 ''=未分类 其他=分类id

  /* ---------- 弹窗开关 ---------- */
  App.openVideoModal = function openVideoModal() {
    App.videoModal!.classList.add('show');
    // 每次打开同步收藏状态（☆/★ 与收藏列表保持一致）
    App.refreshVideoFavorites();
    // 同步连播队列（排队多部视频，播完自动接下一部）
    App.videoQueueSync();
    // 同步视频源 chips（启用的平台/自定义源才显示）
    syncPlatformChips();
    // 打开即加载官网热门/推荐（无需输入关键词；搜索时自动切回搜索结果）
    loadVideoHot();
    setTimeout(() => {
      if (App.videoPaneSearch!.style.display !== 'none') {
        App.videoSearchInput!.focus();
        App.videoSearchInput!.select();
      }
    }, 120);
  };
  App.closeVideoModal = function closeVideoModal() {
    App.videoModal!.classList.remove('show');
  };

  /* ---------- 热门推荐：打开弹窗自动加载，渲染复用搜索结果卡片 ---------- */
  async function loadVideoHot() {
    const seq = ++hotLoadSeq;          // 并发防串：只认最后一次发起的请求
    const plat = currentPlatform();
    hotMode = true;
    searchPager = { keyword: '', platform: plat, sort: 'relevance', page: 0, loading: false, hasMore: true };
    App.videoSearchResults!.innerHTML =
      '<div style="text-align:center;color:var(--text-dim);padding:20px">正在加载热门推荐…</div>';
    try {
      const params = new URLSearchParams({ platform: plat, limit: String(PAGE_SIZE), page: '1' });
      const res = await fetch('/api/video_hub/api/hot?' + params.toString());
      if (!res.ok) throw new Error('HTTP ' + res.status);
      const data = await res.json();
      if (!hotMode || seq !== hotLoadSeq) return;  // 用户已开始搜索或又开了一次弹窗，丢弃过期结果
      searchPager.page = 1;
      searchPager.hasMore = data.has_more !== false;
      const videos: VideoItem[] = data.results || [];
      App.renderVideoSearchResults(videos);
      if (!videos.length) {
        searchPager.hasMore = false;
        App.videoSearchResults!.innerHTML =
          '<div style="text-align:center;color:var(--text-dim);padding:20px">暂时没有热门视频，试试在上方搜索想看的视频</div>';
      } else {
        prependHotHeader();
        updateSearchFooter();
        maybeAutoFillMore();
      }
    } catch (e) {
      if (!hotMode || seq !== hotLoadSeq) return;
      // 静默降级：不打断用户，仅轻提示 + 占位文案
      searchPager.hasMore = false;
      App.videoSearchResults!.innerHTML =
        '<div style="text-align:center;color:var(--text-dim);padding:20px">热门推荐暂时加载失败，试试在上方搜索想看的视频</div>';
      App.showToast('热门推荐加载失败，可直接搜索');
    }
  }

  function prependHotHeader() {
    const old = document.getElementById('video-hot-header');
    if (old) old.remove();
    const header = document.createElement('div');
    header.id = 'video-hot-header';
    header.style.cssText =
      'grid-column:1 / -1;text-align:center;color:var(--accent,#7c5cff);' +
      'font-size:14px;font-weight:600;padding:8px 0 2px;letter-spacing:1px';
    header.textContent = '🔥 热门推荐';
    const container = App.videoSearchResults!;
    container.insertBefore(header, container.firstChild);
  }

  /* ---------- 视频源 / 排序 chips：切换后用当前关键词立即重搜 ---------- */
  function currentPlatform(): string {
    const active = App.videoPlatformChips?.querySelector('.video-plat-chip[data-plat].active');
    return (active?.getAttribute('data-plat')) || 'all';
  }

  function currentSort(): string {
    const active = App.videoPlatformChips?.querySelector('.video-plat-chip[data-sort].active');
    return (active?.getAttribute('data-sort')) || 'relevance';
  }

  if (App.videoPlatformChips) {
    App.videoPlatformChips.addEventListener('click', (ev) => {
      const chip = (ev.target as HTMLElement).closest('.video-plat-chip') as HTMLButtonElement | null;
      if (!chip || chip.classList.contains('active')) return;
      // 同组内单选：平台 chips（data-plat）和排序 chips（data-sort）互不影响
      const scope = chip.hasAttribute('data-plat') ? '[data-plat]' : '[data-sort]';
      App.videoPlatformChips!.querySelectorAll('.video-plat-chip' + scope)
        .forEach(c => c.classList.toggle('active', c === chip));
      // 已有搜索词时切源/切排序立即重搜；热门模式下切平台重载热门；否则等用户输入后按新条件搜
      if (searchPager.keyword && !searchPager.loading) App.videoSearch();
      else if (hotMode && !searchPager.loading) loadVideoHot();
    });
  }

  /* ---------- 刷新按钮：热门模式重载热门；搜索模式按当前关键词重搜 ---------- */
  const refreshBtn = document.getElementById('video-refresh-btn');
  if (refreshBtn) {
    refreshBtn.addEventListener('click', () => {
      if (hotMode) {
        loadVideoHot();
      } else if (searchPager.keyword) {
        App.videoSearch();
      } else {
        App.showToast('当前没有可刷新的内容');
      }
    });
  }

  /* ---------- 搜索（第 1 页；下拉持续加载更多见 loadSearchMore） ---------- */
  App.videoSearch = async function videoSearch() {
    const kw = App.videoSearchInput!.value.trim();
    const plat = currentPlatform();
    const srt = currentSort();
    if (!kw) { App.showToast('请输入搜索关键词'); return; }
    hotMode = false;   // 用户开始搜索 → 从热门推荐切回搜索结果
    if (searchPager.loading) { App.showToast('正在搜索中，稍等一下…'); return; }
    searchPager = { keyword: kw, platform: plat, sort: srt, page: 0, loading: true, hasMore: true };
    App.videoSearchResults!.innerHTML =
      '<div style="text-align:center;color:var(--text-dim);padding:20px">搜索中…</div>';
    try {
      const params = new URLSearchParams({
        q: kw, platform: plat, sort: srt,
        limit: String(PAGE_SIZE), page: '1',
      });
      const res = await fetch('/api/video_hub/api/search?' + params.toString());
      if (!res.ok) throw new Error('HTTP ' + res.status);
      const data = await res.json();
      if (searchPager.keyword !== kw || searchPager.platform !== plat || searchPager.sort !== srt) return;  // 已换关键词/换源/换排序，丢弃过期结果
      searchPager.page = 1;
      searchPager.hasMore = data.has_more !== false;
      App.renderVideoSearchResults(data.results || []);
    } catch (e) {
      const err = e as Error;
      App.videoSearchResults!.innerHTML =
        '<div style="text-align:center;color:var(--text-dim);padding:20px">搜索失败，请稍后再试</div>';
      App.showToast('搜索失败: ' + (err.message || e));
    } finally {
      searchPager.loading = false;
      maybeAutoFillMore();
      // 搜索期间用户切了视频源/排序 → 自动按新条件重搜，无需再点一次
      if (searchPager.keyword && (searchPager.platform !== currentPlatform() || searchPager.sort !== currentSort())) App.videoSearch();
    }
  };

  /* ---------- 缩略图：走后端代理（B站图床防盗链/YouTube 图床需代理，浏览器直连会挂） ---------- */
  function thumbUrl(v: VideoItem): string {
    return v.thumbnail ? '/api/video_hub/thumb?u=' + encodeURIComponent(v.thumbnail) : '';
  }

  /* ---------- 网格卡片：16:9 缩略图 + 时长角标 + 标题 + meta + 操作按钮（搜索/收藏共用） ---------- */
  interface CardAction { key: string; text: string; title: string; onClick: (ev?: Event) => void }

  function buildVideoGridCard(
    v: VideoItem,
    actions: CardAction[],
    extra?: { catTag?: string; createdText?: string },
  ): { card: HTMLDivElement; actBtns: Record<string, HTMLButtonElement> } {
    const plat = PLATFORM_LABEL[String(v.platform || '').toLowerCase()] || String(v.platform || '');
    const meta = [
      fmtViews(v.view_count),
      v.uploader ? 'UP: ' + v.uploader : '',
      plat,
      extra?.createdText || '',
    ].filter(Boolean).join(' · ');

    const card = document.createElement('div');
    card.className = 'video-card-grid';

    // 缩略图区（无图/加载失败时露出 ▶ 占位）
    const thumbWrap = document.createElement('div');
    thumbWrap.className = 'video-thumb-wrap';
    const ph = document.createElement('span');
    ph.className = 'video-thumb-ph';
    ph.textContent = '▶';
    thumbWrap.appendChild(ph);
    const t = thumbUrl(v);
    if (t) {
      const img = document.createElement('img');
      img.className = 'video-thumb';
      img.alt = '';
      img.loading = 'lazy';
      img.decoding = 'async';
      img.src = t;
      img.addEventListener('error', () => img.remove());
      thumbWrap.appendChild(img);
    }
    const durText = fmtTime(v.duration);
    if (durText) {
      const badge = document.createElement('span');
      badge.className = 'video-dur-badge';
      badge.textContent = durText;
      thumbWrap.appendChild(badge);
    }

    // 信息区
    const body = document.createElement('div');
    body.className = 'video-card-body';
    const name = document.createElement('div');
    name.className = 'video-card-title';
    name.title = v.title || '';
    name.textContent = v.title || '未知视频';
    if (extra?.catTag) {
      const tag = document.createElement('span');
      tag.className = 'video-fav-cat-tag';
      tag.textContent = extra.catTag;
      name.appendChild(tag);
    }
    const metaEl = document.createElement('div');
    metaEl.className = 'video-card-meta';
    metaEl.textContent = meta || '…';
    body.append(name, metaEl);

    // 操作区
    const actRow = document.createElement('div');
    actRow.className = 'video-card-acts';
    const actBtns: Record<string, HTMLButtonElement> = {};
    for (const a of actions) {
      const btn = document.createElement('button');
      btn.className = 'music-act-btn';
      btn.type = 'button';
      btn.title = a.title;
      btn.textContent = a.text;
      btn.addEventListener('click', (e) => a.onClick(e));
      actBtns[a.key] = btn;
      actRow.appendChild(btn);
    }

    card.append(thumbWrap, body, actRow);
    return { card, actBtns };
  }

  /* ---------- 搜索结果卡片 ---------- */
  function buildVideoCard(v: VideoItem): HTMLDivElement {
    const { card, actBtns } = buildVideoGridCard(v, [
      { key: 'play', text: '▶', title: '大屏播放', onClick: () => { App.playVideoItem(v); } },
      { key: 'queue', text: '⏭', title: '加入连播队列（播完自动接下一部）', onClick: () => { App.videoQueueAdd(v); } },
      { key: 'fav', text: App.isVideoFavorited(v.webpage_url || '') ? '★' : '☆',
        title: App.isVideoFavorited(v.webpage_url || '') ? '已在收藏夹，点击取消' : '加入收藏夹',
        onClick: async () => {
          const fid = App.isVideoFavorited(v.webpage_url || '');
          if (fid) {
            await App.removeVideoFavorite(fid);
          } else {
            await App.addVideoFavorite(v);
          }
        } },
    ]);
    if (v.webpage_url) favButtons.set(v.webpage_url, actBtns['fav']);
    return card;
  }

  /* ---------- 追加一页结果（跨页去重，插在 footer 之前） ---------- */
  function appendVideoSearchResults(videos: VideoItem[]) {
    const container = App.videoSearchResults!;
    const footer = searchFooterEl();
    let added = 0;
    for (const v of videos) {
      const url = v.webpage_url || '';
      if (!url || renderedUrls.has(url)) continue;  // 去重：B站各页排序会漂移、AcFun重复返回
      renderedUrls.add(url);
      const card = buildVideoCard(v);
      if (footer) container.insertBefore(card, footer);
      else container.appendChild(card);
      lastSearchVideos.push(v);
      added++;
    }
    return added;
  }

  /* ---------- 结果区底部提示条（加载中 / 已经到底） ---------- */
  function searchFooterEl(): HTMLElement | null {
    return App.videoSearchResults!.querySelector('#video-search-footer');
  }

  function setSearchFooter(text: string) {
    let el = searchFooterEl() as HTMLDivElement | null;
    if (!el) {
      el = document.createElement('div');
      el.id = 'video-search-footer';
      el.className = 'video-search-footer';
      el.style.cssText = 'text-align:center;color:var(--text-dim);padding:14px;font-size:13px';
      App.videoSearchResults!.appendChild(el);
    }
    el.textContent = text;
  }

  function updateSearchFooter() {
    if (!searchPager.hasMore) setSearchFooter('— 已经到底啦 —');
    else if (searchFooterEl()) searchFooterEl()!.remove();
  }

  /* ---------- 下拉持续加载：滚动接近底部时加载下一页 ---------- */
  async function loadSearchMore() {
    const st = searchPager;
    if (st.loading || !st.hasMore) return;
    if (!st.keyword && !hotMode) return;   // 既无关键词又不是热门模式 → 无源可加载
    st.loading = true;
    const nextPage = st.page + 1;
    setSearchFooter('加载中…');
    try {
      let data: any;
      if (st.keyword) {
        // 搜索结果翻页
        const params = new URLSearchParams({
          q: st.keyword, platform: st.platform, sort: st.sort,
          limit: String(PAGE_SIZE), page: String(nextPage),
        });
        const res = await fetch('/api/video_hub/api/search?' + params.toString());
        if (!res.ok) throw new Error('HTTP ' + res.status);
        data = await res.json();
        if (searchPager.keyword !== st.keyword || searchPager.platform !== st.platform || searchPager.sort !== st.sort) return;  // 已换关键词/换源/换排序，丢弃过期页
      } else {
        // 热门推荐翻页
        const params = new URLSearchParams({
          platform: st.platform, limit: String(PAGE_SIZE), page: String(nextPage),
        });
        const res = await fetch('/api/video_hub/api/hot?' + params.toString());
        if (!res.ok) throw new Error('HTTP ' + res.status);
        data = await res.json();
        if (!hotMode || searchPager.platform !== st.platform) return;  // 已切平台/已开始搜索，丢弃过期页
      }
      st.page = nextPage;
      st.hasMore = data.has_more !== false;
      appendVideoSearchResults(data.results || []);
      updateSearchFooter();
    } catch (e) {
      const err = e as Error;
      setSearchFooter('加载失败：' + (err.message || e) + '，再往下拉重试');
      App.showToast('加载更多失败: ' + (err.message || e));
    } finally {
      st.loading = false;
    }
  }

  /* ---------- 追加后内容不满一屏时自动补页（保证滚动加载可触发） ---------- */
  function maybeAutoFillMore() {
    const el = App.videoSearchResults!;
    setTimeout(() => {
      if (searchPager.loading || !searchPager.hasMore) return;
      if (el.scrollHeight <= el.clientHeight + 40) loadSearchMore();
    }, 60);
  }

  // 滚动触底加载（容器本身可滚动：.music-results max-height + overflow-y:auto）
  App.videoSearchResults!.addEventListener('scroll', () => {
    const el = App.videoSearchResults!;
    if (el.scrollTop + el.clientHeight >= el.scrollHeight - 240) loadSearchMore();
  }, { passive: true });

  App.renderVideoSearchResults = function renderVideoSearchResults(videos: VideoItem[]) {
    lastSearchVideos = [];
    playedSearchItem = null;
    favButtons.clear();
    renderedUrls.clear();
    App.videoSearchResults!.innerHTML = '';
    if (!videos.length) {
      searchPager.hasMore = false;
      App.videoSearchResults!.innerHTML =
        '<div style="text-align:center;color:var(--text-dim);padding:20px">没搜到相关视频，换个关键词试试</div>';
      return;
    }
    appendVideoSearchResults(videos);
  };

  /* ---------- 播放（复用 video 技能原生解析 + 直播大屏） ---------- */
  // opts.auto=true：大屏播完后的搜索顺序自动连播 —— 不弹全屏 loading、
  // toast 用「连播」口吻；返回 true 表示已成功投屏，false 表示失败。
  App.playVideoItem = async function playVideoItem(v: VideoItem, opts?: { auto?: boolean }): Promise<boolean> {
    const auto = !!(opts && opts.auto);
    // 记录「本次是从最近搜索结果里播的哪一部」（队列空时自动连播按此顺延）
    playedSearchItem = lastSearchVideos.includes(v) ? v : null;
    if (!auto) App.showModelLoading('解析播放链接…');
    try {
      // 与 skills/video skill.py 同款：优先按链接解析
      const body: any = { url: v.webpage_url, platform: 'all', sort: 'relevance' };
      const res = await fetch('/api/video_hub/api/play', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      const data = await res.json();
      if (!res.ok || !data || !data.stream || !data.stream.key) {
        App.showToast((data && data.error) || '这个视频暂时放不了，换个试试');
        return false;
      }
      // 构造与 skill._screen_args 一致的 play_video 屏幕指令参数（同源相对流地址）
      const st = data.stream || {};
      let url = '';
      let fallback = '';
      if (st.mode === 'direct') {
        url = '/api/video_hub/proxy?k=' + encodeURIComponent(st.key);
      } else if (st.mode === 'relay') {
        url = '/api/video_hub/relay/' + encodeURIComponent(st.key);
        fallback = '/api/video_hub/relay/' + encodeURIComponent(st.key) + '?t=1';
      }
      if (!url) {
        App.showToast('解析不出可播放的流，换一个视频试试');
        return false;
      }
      const title = data.title || v.title || '未知视频';
      const payload = {
        url: url,
        fallback_url: fallback,
        title: title,
        uploader: data.uploader || v.uploader || '',
        platform: data.platform || v.platform || '',
        height: st.height || 0,
        mode: st.mode || '',
        webpage_url: data.webpage_url || v.webpage_url || '',
        duration: data.duration || v.duration || 0,
        message: (auto ? '▶ 自动连播下一部：《' : '已在大屏播放《') + title + '》。',
      };
      if (App.videoBoardPlay) {
        App.videoBoardPlay(payload);
        // 播放成功 → 记入观看历史（静默失败不打扰）
        App.recordVideoHistory(v);
        return true;
      } else {
        console.warn('[VideoUI] videoBoardPlay 未初始化');
        App.showToast('大屏播放器尚未就绪');
        return false;
      }
    } catch (e) {
      const err = e as Error;
      App.showToast('播放失败: ' + (err.message || e));
      return false;
    } finally {
      if (!auto) App.hideModelLoading();
    }
  };

  /* ---------- 搜索顺序自动连播：连播队列为空时的隐式片单 ---------- */
  // 大屏播完一部后若队列取不到片，调用这里接着播「最近一次搜索结果」的下一部：
  // 游标优先（最近从列表点播的那部顺延），其次按刚播完视频的原页链接匹配位置。
  App.videoNextFromSearch = function videoNextFromSearch(endedUrl?: string): VideoItem | null {
    const list = lastSearchVideos;
    if (!list.length) return null;
    if (playedSearchItem) {
      const i = list.indexOf(playedSearchItem);
      if (i >= 0) return i + 1 < list.length ? list[i + 1] : null;
    }
    const url = String(endedUrl || '');
    if (url) {
      const j = list.findIndex(x => x.webpage_url === url);
      if (j >= 0) return j + 1 < list.length ? list[j + 1] : null;
    }
    return null;
  };

  /* ---------- 正在播放面板：暂停/继续 + 可拖动进度条 + 音量 + 下一集（控制大屏播放器） ---------- */
  const np = (() => {
    const root = App.videoModal as HTMLElement;
    const $ = (id: string) => root.querySelector(id) as HTMLElement;
    const els = {
      panel: $('#video-now-playing'),
      title: $('#video-np-title'),
      toggle: $('#video-np-toggle') as HTMLButtonElement,
      track: $('#video-np-track'),
      fill: $('#video-np-fill'),
      knob: $('#video-np-knob'),
      time: $('#video-np-time'),
      stop: $('#video-np-stop') as HTMLButtonElement,
      fav: $('#video-np-fav') as HTMLButtonElement,
      next: $('#video-np-next') as HTMLButtonElement,
      mute: $('#video-np-mute') as HTMLButtonElement,
      vol: $('#video-np-vol') as HTMLInputElement,
    };
    let dragging = false;
    let volDragging = false;   // 音量条拖动中：轮询不覆盖滑块位置

    const control = (action: string, value?: number) => {
      if (!App.videoBoardControl) { App.showToast('大屏播放器尚未就绪'); return; }
      App.videoBoardControl({ action, ...(value !== undefined ? { value } : {}) });
    };

    const posOf = (ev: PointerEvent) => {
      const r = els.track.getBoundingClientRect();
      return Math.max(0, Math.min(1, r.width > 0 ? (ev.clientX - r.left) / r.width : 0));
    };

    const fmtT = (s: number) => {
      s = Math.max(0, Math.floor(s || 0));
      const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), ss = s % 60;
      return h ? h + ':' + String(m).padStart(2, '0') + ':' + String(ss).padStart(2, '0')
               : m + ':' + String(ss).padStart(2, '0');
    };

    const render = (st: any, dragFrac?: number) => {
      els.panel.style.display = 'flex';
      els.title.textContent = st.title || '正在播放…';
      els.title.title = st.title || '';
      els.toggle.textContent = st.paused ? '▶' : '⏸';
      els.toggle.title = st.paused ? '继续播放' : '暂停';
      const frac = dragFrac !== undefined
        ? dragFrac
        : (st.duration > 0 ? Math.min(1, Math.max(0, st.currentTime / st.duration)) : 0);
      els.fill.style.width = (frac * 100).toFixed(2) + '%';
      els.knob.style.left = (frac * 100).toFixed(2) + '%';
      if (dragFrac !== undefined && st.duration > 0) {
        // 拖动预览：时间文本与滑块跟随手指位置，松手后才真正跳转
        els.time.textContent = fmtT(dragFrac * st.duration) + ' / ' + fmtT(st.duration);
      } else {
        els.time.textContent = st.duration > 0
          ? fmtT(st.currentTime) + ' / ' + fmtT(st.duration)
          : '—';
      }
      els.track.classList.toggle('video-np-disabled', !st.seekable);
      els.track.title = st.seekable ? '拖动跳转进度' : '时长未知，暂不支持拖动进度';
      els.toggle.disabled = !st.ready;
      els.stop.disabled = !st.ready;
      // 下一集 / 静音 / 音量：与播放器状态同步
      els.next.disabled = !st.ready;
      els.mute.disabled = !st.ready;
      els.vol.disabled = !st.ready;
      if (!volDragging) {
        els.vol.value = String(Math.round((st.muted ? 0 : (st.volume || 0)) * 100));
      }
      els.mute.textContent = st.muted ? '🔇' : '🔊';
      els.mute.title = st.muted ? '取消静音' : '静音';
      // np 收藏按钮：有原页链接才可收藏；★/☆ 与收藏夹状态同步
      const curFid = st.webpage_url ? App.isVideoFavorited(st.webpage_url) : null;
      els.fav.textContent = curFid ? '★' : '☆';
      els.fav.title = curFid
        ? '已收藏当前视频，点击取消'
        : (st.webpage_url ? '收藏当前播放的视频' : '当前播放的视频暂无可收藏的链接');
      els.fav.disabled = !st.webpage_url;
    };

    els.toggle.addEventListener('click', () => {
      const st = App.videoBoardGetState ? App.videoBoardGetState() : null;
      if (!st) return;
      control(st.paused ? 'resume' : 'pause');
    });
    els.stop.addEventListener('click', () => control('stop'));
    els.next.addEventListener('click', () => control('next'));
    els.mute.addEventListener('click', () => control('mute'));
    els.vol.addEventListener('pointerdown', () => { volDragging = true; });
    els.vol.addEventListener('pointerup', () => { volDragging = false; });
    els.vol.addEventListener('pointercancel', () => { volDragging = false; });
    els.vol.addEventListener('input', () => {
      volDragging = true;
      control('volume', Number(els.vol.value) / 100);
    });
    els.vol.addEventListener('change', () => { volDragging = false; });
    els.fav.addEventListener('click', async () => {
      const st = App.videoBoardGetState ? App.videoBoardGetState() : null;
      if (!st || !st.webpage_url) { App.showToast('当前播放的视频暂无可收藏的链接'); return; }
      const fid = App.isVideoFavorited(st.webpage_url);
      if (fid) {
        await App.removeVideoFavorite(fid);
      } else {
        await App.addVideoFavorite({
          title: st.title || '未知视频',
          webpage_url: st.webpage_url,
          platform: st.platform || '',
          uploader: st.uploader || '',
          duration: st.duration || 0,
          view_count: 0,
        });
      }
    });
    els.track.addEventListener('pointerdown', (ev: PointerEvent) => {
      const st = App.videoBoardGetState ? App.videoBoardGetState() : null;
      if (!st || !st.seekable) {
        App.showToast('时长未知，暂不支持拖动进度');
        return;
      }
      dragging = true;
      try { els.track.setPointerCapture(ev.pointerId); } catch (e) {}
      render(st, posOf(ev));
    });
    els.track.addEventListener('pointermove', (ev: PointerEvent) => {
      if (!dragging) return;
      const st = App.videoBoardGetState ? App.videoBoardGetState() : null;
      if (st) render(st, posOf(ev));
    });
    const endDrag = (ev: PointerEvent) => {
      if (!dragging) return;
      dragging = false;
      try { els.track.releasePointerCapture(ev.pointerId); } catch (e) {}
      const st = App.videoBoardGetState ? App.videoBoardGetState() : null;
      if (!st || !st.seekable) return;
      control('seek', Math.round(posOf(ev) * st.duration));
    };
    els.track.addEventListener('pointerup', endDrag);
    els.track.addEventListener('pointercancel', () => { dragging = false; });

    // 轮询大屏播放器状态（500ms；只在弹窗打开时更新 DOM，开销可忽略）
    setInterval(() => {
      if (!App.videoModal || !App.videoModal.classList.contains('show')) return;
      if (!App.videoBoardGetState) return;
      const st = App.videoBoardGetState();
      if (!st) {
        els.panel.style.display = 'none';
        return;
      }
      if (dragging) return;  // 拖动中显示的是预览位置，不被轮询覆盖
      render(st);
    }, 500);

    return els;
  })();
  // 面板元素已就位（供调试/后续复用），播完/停播时由轮询隐藏
  void np;

  /* ---------- 收藏夹：搜索卡片按钮状态同步（收藏/取消后就地更新，不整表闪烁） ---------- */
  function syncSearchFavButtons() {
    for (const v of lastSearchVideos) {
      const btn = favButtons.get(v.webpage_url || '');
      if (!btn) continue;
      const favId = App.isVideoFavorited(v.webpage_url || '');
      btn.textContent = favId ? '★' : '☆';
      btn.title = favId ? '已在收藏夹，点击取消' : '加入收藏夹';
    }
  }

  /* ---------- 收藏夹：页签 ---------- */
  App.switchVideoTab = function switchVideoTab(tab: 'search' | 'favorites' | 'history' | 'sources') {
    // 视频源页签只对管理员存在（DOM 上标了 data-admin-only），兜底防别处误调
    if (tab === 'sources' && window.__ROLE !== 'admin') tab = 'search';
    const isSearch = tab === 'search';
    const isFav = tab === 'favorites';
    const isHist = tab === 'history';
    const isSrc = tab === 'sources';
    App.videoTabSearch!.classList.toggle('active', isSearch);
    App.videoTabFavorites!.classList.toggle('active', isFav);
    App.videoTabHistory!.classList.toggle('active', isHist);
    App.videoTabSources!.classList.toggle('active', isSrc);
    App.videoPaneSearch!.style.display = isSearch ? '' : 'none';
    App.videoPaneFavorites!.style.display = isFav ? '' : 'none';
    App.videoPaneHistory!.style.display = isHist ? '' : 'none';
    App.videoPaneSources!.style.display = isSrc ? '' : 'none';
    if (isFav) App.refreshVideoFavorites();
    if (isHist) App.refreshVideoHistory();
    if (isSrc) App.refreshVideoSources();
  };

  /* ---------- 收藏夹：拉取 & 渲染 ---------- */
  App.refreshVideoFavorites = async function refreshVideoFavorites() {
    try {
      const res = await fetch('/api/video_hub/api/favorites');
      if (!res.ok) throw new Error('HTTP ' + res.status);
      const data = await res.json();
      App._videoFavCache.clear();
      for (const f of data.favorites || []) {
        if (f.video && f.video.webpage_url) App._videoFavCache.set(f.video.webpage_url, f.id);
      }
      syncSearchFavButtons();
      if (App.videoPaneFavorites!.style.display !== 'none') {
        App.renderVideoFavorites(data);
      }
    } catch (e) {
      const err = e as Error;
      App.showToast('获取收藏失败: ' + (err.message || e));
    }
  };

  App.isVideoFavorited = function isVideoFavorited(url: string): string | null {
    if (!url) return null;
    return App._videoFavCache.get(url) || null;
  };

  App.renderVideoFavorites = function renderVideoFavorites(data: any) {
    const cats: any[] = data.categories || [];
    const favs: any[] = data.favorites || [];
    const catName = (cid: string | null) => {
      if (!cid) return '未分类';
      const c = cats.find(x => x.id === cid);
      return c ? c.name : '未分类';
    };
    const countOf = (pred: (f: any) => boolean) => favs.filter(pred).length;

    /* ---- 分类 chips（点选过滤；✎ 重命名；✕ 删除） ---- */
    const chips = App.videoFavCategories!;
    chips.innerHTML = '';
    const makeChip = (key: string | null, label: string, count: number,
                      extra?: (chip: HTMLElement) => void) => {
      const chip = document.createElement('span');
      chip.className = 'video-fav-chip' + (App._videoFavFilter === key ? ' active' : '');
      const labelEl = document.createElement('span');
      labelEl.textContent = label;
      chip.appendChild(labelEl);
      const cnt = document.createElement('span');
      cnt.className = 'video-fav-chip-count';
      cnt.textContent = String(count);
      chip.appendChild(cnt);
      if (extra) extra(chip);
      chip.addEventListener('click', () => {
        App._videoFavFilter = key;
        App.renderVideoFavorites(data);
      });
      chips.appendChild(chip);
    };
    makeChip(null, '全部', favs.length);
    for (const c of cats) {
      const n = countOf(f => f.category_id === c.id);
      makeChip(c.id, c.name, n, chip => {
        const renameBtn = document.createElement('button');
        renameBtn.className = 'video-fav-chip-act';
        renameBtn.textContent = '✎';
        renameBtn.title = '重命名分类';
        renameBtn.addEventListener('click', ev => {
          ev.stopPropagation();
          App.renameVideoCategory(c.id);
        });
        chip.appendChild(renameBtn);
        const delBtn = document.createElement('button');
        delBtn.className = 'video-fav-chip-act danger';
        delBtn.textContent = '✕';
        delBtn.title = '删除分类（视频归入未分类）';
        delBtn.addEventListener('click', ev => {
          ev.stopPropagation();
          App.deleteVideoCategory(c.id);
        });
        chip.appendChild(delBtn);
      });
    }
    const unsorted = countOf(f => !f.category_id);
    if (unsorted > 0) makeChip('', '未分类', unsorted);

    /* ---- 收藏视频列表（按当前过滤显示） ---- */
    const list = App.videoFavList!;
    list.innerHTML = '';
    const shown = App._videoFavFilter === null
      ? favs
      : favs.filter(f => (App._videoFavFilter === '' ? !f.category_id : f.category_id === App._videoFavFilter));
    if (!shown.length) {
      list.innerHTML = '<div class="video-fav-empty">' +
        (favs.length ? '这个分类下还没有视频' : '收藏夹是空的，去「搜索视频」页把喜欢的视频收藏起来吧') + '</div>';
      return;
    }
    for (const f of shown) {
      const v: any = f.video || {};
      const { card } = buildVideoGridCard(v, [
        { key: 'play', text: '▶', title: '大屏播放', onClick: () => { App.playVideoItem(v); } },
        { key: 'move', text: '📁', title: '移动到分类（点选）', onClick: (ev) => { App.moveVideoFavorite(f.id, ev); } },
        { key: 'rm', text: '☆', title: '取消收藏', onClick: async () => { await App.removeVideoFavorite(f.id); } },
      ], {
        catTag: catName(f.category_id || null),
        createdText: f.created ? '收藏于 ' + fmtDate(f.created) : '',
      });
      list.appendChild(card);
    }
  };

  /* ---------- 观看历史：拉取 & 渲染 ---------- */
  App.refreshVideoHistory = async function refreshVideoHistory() {
    try {
      const res = await fetch('/api/video_hub/api/history');
      if (!res.ok) throw new Error('HTTP ' + res.status);
      const data = await res.json();
      if (App.videoPaneHistory!.style.display !== 'none') {
        App.renderVideoHistory(data);
      }
    } catch (e) {
      const err = e as Error;
      App.showToast('获取观看历史失败: ' + (err.message || e));
    }
  };

  App.renderVideoHistory = function renderVideoHistory(data: any) {
    const list = App.videoHistoryList!;
    list.innerHTML = '';
    const items: any[] = data.history || [];
    if (!items.length) {
      list.innerHTML = '<div class="video-fav-empty">还没有观看记录，去「搜索视频」页看一部片子吧</div>';
      return;
    }
    for (const h of items) {
      const v: any = h.video || {};
      const { card } = buildVideoGridCard(v, [
        { key: 'play', text: '▶', title: '再次观看（大屏播放）', onClick: () => { App.playVideoItem(v); } },
        { key: 'rm', text: '✕', title: '删除这条记录', onClick: async () => {
          if (!confirm('删除这条观看记录？')) return;
          try {
            const res = await fetch('/api/video_hub/api/history/' + encodeURIComponent(h.id), { method: 'DELETE' });
            if (!res.ok) {
              const d = await res.json().catch(() => null);
              App.showToast((d && d.error) || '删除失败');
              return;
            }
            App.showToast('已删除这条记录');
            App.refreshVideoHistory();
          } catch (e) {
            const err = e as Error;
            App.showToast('删除失败: ' + (err.message || e));
          }
        } },
      ], {
        createdText: h.watched_at ? '看过于 ' + fmtDate(h.watched_at) : '',
      });
      list.appendChild(card);
    }
  };

  /* ---------- 观看历史：记录一次观看（播放成功时调用，静默失败不打扰） ---------- */
  App.recordVideoHistory = async function recordVideoHistory(v: VideoItem) {
    if (!v || !v.webpage_url) return;
    try {
      await fetch('/api/video_hub/api/history', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ video: {
          title: v.title || '',
          webpage_url: v.webpage_url,
          platform: v.platform || '',
          uploader: v.uploader || '',
          duration: v.duration || 0,
          view_count: v.view_count || 0,
          thumbnail: v.thumbnail || '',
        } }),
      });
    } catch (e) { /* 静默失败：历史记录不影响播放主流程 */ }
  };

  App.clearVideoHistory = async function clearVideoHistory() {
    if (!confirm('清空全部观看历史？此操作不可恢复。')) return;
    try {
      const res = await fetch('/api/video_hub/api/history', { method: 'DELETE' });
      if (!res.ok) {
        const d = await res.json().catch(() => null);
        App.showToast((d && d.error) || '清空失败');
        return;
      }
      App.renderVideoHistory({ history: [] });
      App.showToast('观看历史已清空');
    } catch (e) {
      const err = e as Error;
      App.showToast('清空失败: ' + (err.message || e));
    }
  };

  /* ---------- 收藏夹：分类管理 ---------- */
  /* ---------- 视频源：拉取 & 渲染（内置平台开关 + 自定义源管理） ---------- */
  App.refreshVideoSources = async function refreshVideoSources() {
    // 视频源是管理员配置层：列源接口对普通用户 403，别发无谓请求
    if (window.__ROLE !== 'admin') return;
    try {
      const res = await fetch('/api/video_hub/api/sources');
      if (!res.ok) throw new Error('HTTP ' + res.status);
      const data = await res.json();
      if (App.videoPaneSources!.style.display !== 'none') {
        App.renderVideoSources(data);
      }
    } catch (e) {
      const err = e as Error;
      App.showToast('获取视频源失败: ' + (err.message || e));
    }
  };

  App.renderVideoSources = function renderVideoSources(data: any) {
    const builtinEl = App.videoSrcBuiltin!;
    const customEl = App.videoSrcCustom!;
    builtinEl.innerHTML = '';
    customEl.innerHTML = '';
    // 内置平台：开关行
    const builtins: any[] = data.builtin || [];
    for (const p of builtins) {
      const row = document.createElement('div');
      row.className = 'video-src-item';
      const name = document.createElement('span');
      name.className = 'video-src-item-name';
      name.textContent = p.name || p.id;
      const hint = document.createElement('span');
      hint.className = 'video-src-item-hint';
      hint.textContent = p.hint || '';
      const sw = document.createElement('button');
      sw.className = 'video-src-switch' + (p.enabled ? ' on' : '');
      sw.type = 'button';
      sw.title = p.enabled ? '点击关闭（搜索页将不再显示该平台）' : '点击启用';
      sw.addEventListener('click', () => App.toggleVideoSource(p.id, !p.enabled));
      row.append(name, hint, sw);
      builtinEl.appendChild(row);
    }
    // 自定义源：名称 + 链接 + 开关 + 删除
    const customs: any[] = data.custom || [];
    if (!customs.length) {
      const empty = document.createElement('div');
      empty.className = 'video-src-item';
      empty.style.color = 'var(--text-dim)';
      empty.style.fontSize = '12px';
      empty.textContent = '还没有自定义视频源。填好名称和搜索链接（含 {kw}）点「添加」即可。';
      customEl.appendChild(empty);
      return;
    }
    for (const c of customs) {
      const row = document.createElement('div');
      row.className = 'video-src-item';
      const name = document.createElement('span');
      name.className = 'video-src-item-name';
      name.textContent = c.name || c.id;
      name.title = c.search_url || '';
      const hint = document.createElement('span');
      hint.className = 'video-src-item-hint';
      hint.textContent = c.search_url || '';
      const sw = document.createElement('button');
      sw.className = 'video-src-switch' + (c.enabled ? ' on' : '');
      sw.type = 'button';
      sw.title = c.enabled ? '点击关闭' : '点击启用';
      sw.addEventListener('click', () => App.toggleVideoSource(c.id, !c.enabled));
      const del = document.createElement('button');
      del.className = 'video-src-del';
      del.type = 'button';
      del.textContent = '✕';
      del.title = '删除这个视频源';
      del.addEventListener('click', () => App.removeVideoSource(c.id));
      row.append(name, hint, sw, del);
      customEl.appendChild(row);
    }
  };

  /* ---------- 视频源：开关（内置平台 / 自定义源统一走 PATCH） ---------- */
  App.toggleVideoSource = async function toggleVideoSource(id: string, enabled: boolean) {
    try {
      const isBuiltin = !id.startsWith('custom_');
      const url = isBuiltin
        ? '/api/video_hub/api/sources/builtin/' + encodeURIComponent(id)
        : '/api/video_hub/api/sources/custom/' + encodeURIComponent(id);
      const res = await fetch(url, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ enabled }),
      });
      if (!res.ok) {
        const d = await res.json().catch(() => null);
        App.showToast((d && d.error) || '切换失败');
        return;
      }
      App.showToast(enabled ? '已启用' : '已关闭');
      App.refreshVideoSources();
      // 搜索页 chips 同步：关闭的平台从 chips 里移除/隐藏
      syncPlatformChips();
    } catch (e) {
      const err = e as Error;
      App.showToast('切换失败: ' + (err.message || e));
    }
  };

  /* ---------- 视频源：添加自定义源 ---------- */
  App.addVideoSource = async function addVideoSource() {
    const name = App.videoSrcName!.value.trim();
    const url = App.videoSrcUrl!.value.trim();
    if (!name || !url) { App.showToast('请填写名称和搜索链接'); return; }
    try {
      const res = await fetch('/api/video_hub/api/sources/custom', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name, search_url: url }),
      });
      const data = await res.json();
      if (!res.ok) {
        App.showToast((data && data.error) || '添加失败');
        return;
      }
      App.videoSrcName!.value = '';
      App.videoSrcUrl!.value = '';
      App.showToast('已添加视频源「' + name + '」');
      App.refreshVideoSources();
      syncPlatformChips();
    } catch (e) {
      const err = e as Error;
      App.showToast('添加失败: ' + (err.message || e));
    }
  };

  /* ---------- 视频源：删除自定义源 ---------- */
  App.removeVideoSource = async function removeVideoSource(id: string) {
    if (!confirm('删除这个自定义视频源？')) return;
    try {
      const res = await fetch('/api/video_hub/api/sources/custom/' + encodeURIComponent(id), { method: 'DELETE' });
      if (!res.ok) {
        const d = await res.json().catch(() => null);
        App.showToast((d && d.error) || '删除失败');
        return;
      }
      App.showToast('已删除');
      App.refreshVideoSources();
      syncPlatformChips();
    } catch (e) {
      const err = e as Error;
      App.showToast('删除失败: ' + (err.message || e));
    }
  };

  /* ---------- 视频源：同步搜索页 chips（只显示启用的平台 + 自定义源） ---------- */
  async function syncPlatformChips() {
    // 平台清单属于管理员配置：普通用户不重建 chips，静态 chips 只有「全部」+ 排序
    if (window.__ROLE !== 'admin') return;
    try {
      const res = await fetch('/api/video_hub/api/sources');
      if (!res.ok) return;
      const data = await res.json();
      const chips = App.videoPlatformChips;
      if (!chips) return;
      // 记录当前选中平台
      const cur = currentPlatform();
      // 重建 chips：全部 + 启用的内置平台 + 启用的自定义源
      const builtinOn = (data.builtin || []).filter((p: any) => p.enabled);
      const customOn = (data.custom || []).filter((c: any) => c.enabled);
      const html: string[] = [];
      html.push('<button class="video-plat-chip active" data-plat="all" type="button">全部</button>');
      for (const p of builtinOn) {
        html.push(`<button class="video-plat-chip" data-plat="${p.id}" type="button">${p.name.replace('哔哩哔哩', 'B站')}</button>`);
      }
      for (const c of customOn) {
        html.push(`<button class="video-plat-chip" data-plat="${c.id}" type="button">${c.name}</button>`);
      }
      // 排序 chips 保留
      html.push('<span class="video-sort-label">排序</span>');
      html.push('<button class="video-plat-chip active" data-sort="relevance" type="button">综合</button>');
      html.push('<button class="video-plat-chip" data-sort="new" type="button">最新</button>');
      html.push('<button class="video-plat-chip" data-sort="hot" type="button">热门</button>');
      chips.innerHTML = html.join('');
      // 恢复当前选中平台（若已被关闭则回落到 all）
      const platChip = chips.querySelector(`.video-plat-chip[data-plat="${cur}"]`) as HTMLButtonElement | null;
      if (platChip) {
        chips.querySelectorAll('.video-plat-chip[data-plat]').forEach(c => c.classList.remove('active'));
        platChip.classList.add('active');
      }
    } catch (e) { /* 静默失败：chips 保持原样 */ }
  }

  /* ---------- 收藏夹：分类管理 ---------- */
  App.createVideoCategory = async function createVideoCategory() {
    const name = App.videoFavCategoryInput!.value.trim();
    if (!name) { App.showToast('请输入分类名'); return; }
    try {
      const res = await fetch('/api/video_hub/api/favorites/categories', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name }),
      });
      const data = await res.json();
      if (!res.ok || !data.category) {
        App.showToast((data && data.error) || '创建失败');
        return;
      }
      App.videoFavCategoryInput!.value = '';
      App.showToast('分类《' + name + '》创建成功');
      App.refreshVideoFavorites();
    } catch (e) {
      const err = e as Error;
      App.showToast('创建失败: ' + (err.message || e));
    }
  };

  App.renameVideoCategory = async function renameVideoCategory(cid: string) {
    let oldName = '';
    try {
      const res = await fetch('/api/video_hub/api/favorites');
      if (res.ok) {
        const cats = ((await res.json()).categories) || [];
        const c = cats.find((x: any) => x.id === cid);
        if (c) oldName = c.name;
      }
    } catch (e) { /* ignore */ }
    const input = prompt('重命名分类（当前：' + (oldName || '?') + '）', oldName);
    if (input === null) return;
    const name = input.trim();
    if (!name) { App.showToast('分类名不能为空'); return; }
    if (name === oldName) return;
    try {
      const res = await fetch('/api/video_hub/api/favorites/categories/' + encodeURIComponent(cid), {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name }),
      });
      const data = await res.json();
      if (!res.ok || !data.category) {
        App.showToast((data && data.error) || '重命名失败');
        return;
      }
      App.showToast('已重命名为《' + name + '》');
      App.refreshVideoFavorites();
    } catch (e) {
      const err = e as Error;
      App.showToast('重命名失败: ' + (err.message || e));
    }
  };

  App.deleteVideoCategory = async function deleteVideoCategory(cid: string) {
    if (!confirm('删除这个分类？分类下的视频会归入「未分类」，不会丢失。')) return;
    try {
      const res = await fetch('/api/video_hub/api/favorites/categories/' + encodeURIComponent(cid), { method: 'DELETE' });
      if (!res.ok) {
        const data = await res.json().catch(() => null);
        App.showToast((data && data.error) || '删除失败');
        return;
      }
      if (App._videoFavFilter === cid) App._videoFavFilter = null;
      App.showToast('分类已删除');
      App.refreshVideoFavorites();
    } catch (e) {
      const err = e as Error;
      App.showToast('删除失败: ' + (err.message || e));
    }
  };

  /* ---------- 收藏夹：收藏 / 取消 / 移动 ---------- */
  // 收藏夹是一个合集：点 ☆ 直接加入，不再要求输入/选择分类；
  // 分类只是可选整理手段（收藏夹页签里「📁」点选移动）。
  App.addVideoFavorite = async function addVideoFavorite(v: VideoItem, categoryId?: string | null): Promise<string | null> {
    const targetId = categoryId === undefined ? null : categoryId;
    try {
      const res = await fetch('/api/video_hub/api/favorites', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          video: {
            title: v.title || '',
            webpage_url: v.webpage_url || '',
            platform: v.platform || '',
            uploader: v.uploader || '',
            duration: v.duration || 0,
            view_count: v.view_count || 0,
            thumbnail: v.thumbnail || '',
          },
          category_id: targetId ?? null,
        }),
      });
      const data = await res.json();
      if (!res.ok || !data.favorite) {
        App.showToast((data && data.error) || '收藏失败，请重试');
        return null;
      }
      App._videoFavCache.set(v.webpage_url || '', data.favorite.id);
      if (data.existed) {
        App.showToast('《' + (v.title || '未知视频') + '》已在收藏夹中');
      } else {
        App.showToast('已加入收藏夹：《' + (v.title || '未知视频') + '》');
      }
      syncSearchFavButtons();
      if (App.videoPaneFavorites!.style.display !== 'none') App.refreshVideoFavorites();
      return data.favorite.id;
    } catch (e) {
      const err = e as Error;
      App.showToast('收藏失败: ' + (err.message || e));
      return null;
    }
  };

  App.removeVideoFavorite = async function removeVideoFavorite(fid: string) {
    try {
      const res = await fetch('/api/video_hub/api/favorites/' + encodeURIComponent(fid), { method: 'DELETE' });
      if (!res.ok) {
        const data = await res.json().catch(() => null);
        App.showToast((data && data.error) || '取消收藏失败');
        return;
      }
      for (const [url, id] of App._videoFavCache) {
        if (id === fid) App._videoFavCache.delete(url);
      }
      App.showToast('已取消收藏');
      syncSearchFavButtons();
      App.refreshVideoFavorites();
    } catch (e) {
      const err = e as Error;
      App.showToast('取消收藏失败: ' + (err.message || e));
    }
  };

  /* ---------- 分类点选菜单（点选即走，不用输入文字） ----------
   * 在锚点附近弹出可选分类列表；onPick(分类id|null, 分类名) 后自行关闭。 */
  function openCategoryPicker(
    cats: any[],
    ev: Event | undefined,
    onPick: (cid: string | null, name: string) => void,
  ) {
    const old = document.getElementById('video-fav-cpick');
    if (old) old.remove();
    const menu = document.createElement('div');
    menu.id = 'video-fav-cpick';
    menu.style.cssText = [
      'position:fixed', 'z-index:300', 'background:#14142c',
      'border:1px solid rgba(124,92,255,0.45)', 'border-radius:12px',
      'padding:8px', 'display:flex', 'flex-direction:column', 'gap:6px',
      'min-width:160px', 'max-height:260px', 'overflow:auto',
      'box-shadow:0 12px 32px rgba(0,0,0,0.55)',
    ].join(';');
    const mkBtn = (label: string, pick: () => void) => {
      const b = document.createElement('button');
      b.type = 'button';
      b.textContent = label;
      b.style.cssText = [
        'background:rgba(124,92,255,0.14)', 'border:1px solid rgba(124,92,255,0.35)',
        'color:#e6e0ff', 'border-radius:8px', 'padding:7px 10px',
        'text-align:left', 'font-size:13px', 'cursor:pointer',
      ].join(';');
      b.addEventListener('click', (e) => { e.stopPropagation(); detach(); menu.remove(); pick(); });
      menu.appendChild(b);
    };
    mkBtn('📥 未分类', () => onPick(null, ''));
    for (const c of cats) mkBtn('📁 ' + (c.name || '未命名'), () => onPick(String(c.id), String(c.name || '')));
    document.body.appendChild(menu);
    // 定位：有事件跟着手指/鼠标，无事件居中；都钳制在视口内
    const r = menu.getBoundingClientRect();
    let x = ev && typeof MouseEvent !== 'undefined' && ev instanceof MouseEvent
      ? ev.clientX + 8 : Math.round((window.innerWidth - r.width) / 2);
    let y = ev && typeof MouseEvent !== 'undefined' && ev instanceof MouseEvent
      ? ev.clientY + 8 : Math.round((window.innerHeight - r.height) / 2);
    x = Math.max(8, Math.min(window.innerWidth - r.width - 8, x));
    y = Math.max(8, Math.min(window.innerHeight - r.height - 8, y));
    menu.style.left = x + 'px';
    menu.style.top = y + 'px';
    // 菜单外点击关闭（下一帧再挂监听，避免触发本次打开的 pointerdown）
    const closeOnDoc = (e: PointerEvent) => {
      if (!menu.contains(e.target as Node)) { detach(); menu.remove(); }
    };
    const detach = () => document.removeEventListener('pointerdown', closeOnDoc, true);
    setTimeout(() => document.addEventListener('pointerdown', closeOnDoc, true), 0);
  }

  App.moveVideoFavorite = async function moveVideoFavorite(fid: string, ev?: Event): Promise<void> {
    let cats: any[] = [];
    try {
      const res = await fetch('/api/video_hub/api/favorites');
      if (res.ok) cats = ((await res.json()).categories) || [];
    } catch (e) { /* ignore */ }
    const doMove = async (targetId: string | null, name: string) => {
      try {
        const res = await fetch('/api/video_hub/api/favorites/' + encodeURIComponent(fid) + '/category', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ category_id: targetId }),
        });
        if (!res.ok) {
          const data = await res.json().catch(() => null);
          App.showToast((data && data.error) || '移动失败');
          return;
        }
        App.showToast(name ? '已移动到《' + name + '》' : '已归入未分类');
        App.refreshVideoFavorites();
      } catch (e) {
        const err = e as Error;
        App.showToast('移动失败: ' + (err.message || e));
      }
    };
    openCategoryPicker(cats, ev, doMove);
  };

  /* ---------- 连播队列：排队 / 查看 / 移除 / 清空 ----------
   * 服务端 /api/video_hub/api/queue（video_lib 队列与技能 video_queue 互通）：
   * 当前大屏视频播完后自动取队列下一部连播（30_task_big_screen.videoAdvanceEnded）。
   */
  App.videoQueueSync = async function videoQueueSync() {
    try {
      const res = await fetch('/api/video_hub/api/queue');
      if (!res.ok) throw new Error('HTTP ' + res.status);
      const data = await res.json();
      App.renderVideoQueue(data.queue || []);
    } catch (e) {
      // 轮询失败静默收起面板，不打扰用户
      if (App.videoQueuePanel) App.videoQueuePanel.style.display = 'none';
    }
  };

  App.renderVideoQueue = function renderVideoQueue(queue: any[]) {
    const panel = App.videoQueuePanel, list = App.videoQueueList;
    if (!panel || !list) return;
    if (!queue.length) { panel.style.display = 'none'; list.innerHTML = ''; return; }
    panel.style.display = '';
    const rowMeta = (q: any) => [
      fmtTime(q.duration),
      q.uploader ? 'UP: ' + q.uploader : '',
      PLATFORM_LABEL[String(q.platform || '').toLowerCase()] || String(q.platform || ''),
    ].filter(Boolean).join(' · ');
    list.innerHTML = '';
    queue.forEach((q, i) => {
      const row = document.createElement('div');
      row.className = 'video-queue-item';
      const idx = document.createElement('span');
      idx.className = 'video-queue-idx';
      idx.textContent = String(i + 1);
      const name = document.createElement('div');
      name.className = 'video-queue-name';
      const metaTxt = rowMeta(q);
      name.innerHTML =
        '<div class="video-queue-title-txt">' + App.escapeHtml(q.title || '未知视频') + '</div>' +
        (metaTxt ? '<div class="video-queue-meta">' + App.escapeHtml(metaTxt) + '</div>' : '');
      const rm = document.createElement('button');
      rm.className = 'music-act-btn video-np-btn';
      rm.textContent = '✕';
      rm.title = '移出队列';
      rm.addEventListener('click', () => App.videoQueueRemove(i));
      row.append(idx, name, rm);
      list.appendChild(row);
    });
  };

  App.videoQueueAdd = async function videoQueueAdd(v: VideoItem) {
    if (!v.webpage_url) { App.showToast('这个视频没有可解析的链接'); return; }
    App.showModelLoading('解析并加入连播队列…');
    try {
      const res = await fetch('/api/video_hub/api/queue', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ url: v.webpage_url }),
      });
      const data = await res.json();
      if (!res.ok || !data.queued) {
        App.showToast((data && data.error) || '加入队列失败');
        return;
      }
      const pos = Number(data.position) || 1;
      App.showToast('已加入连播队列（第 ' + pos + ' 位），当前视频播完自动接上');
      await App.videoQueueSync();
    } catch (e) {
      const err = e as Error;
      App.showToast('加入队列失败: ' + (err.message || e));
    } finally {
      App.hideModelLoading();
    }
  };

  App.videoQueueRemove = async function videoQueueRemove(i: number) {
    try {
      const res = await fetch('/api/video_hub/api/queue?i=' + i, { method: 'DELETE' });
      if (!res.ok) {
        const data = await res.json().catch(() => null);
        App.showToast((data && data.error) || '移出队列失败');
        return;
      }
      await App.videoQueueSync();
    } catch (e) {
      const err = e as Error;
      App.showToast('移出队列失败: ' + (err.message || e));
    }
  };

  App.videoQueueClearAll = async function videoQueueClearAll() {
    if (!confirm('清空连播队列？')) return;
    try {
      const res = await fetch('/api/video_hub/api/queue?all=1', { method: 'DELETE' });
      if (!res.ok) {
        const data = await res.json().catch(() => null);
        App.showToast((data && data.error) || '清空队列失败');
        return;
      }
      App.renderVideoQueue([]);
      App.showToast('连播队列已清空');
    } catch (e) {
      const err = e as Error;
      App.showToast('清空队列失败: ' + (err.message || e));
    }
  };

  // 轮询队列（2 秒，仅弹窗打开时）：当前视频播完 / AI 技能加片后队列自动变化
  setInterval(() => {
    if (!App.videoModal || !App.videoModal.classList.contains('show')) return;
    App.videoQueueSync();
  }, 2000);
}
