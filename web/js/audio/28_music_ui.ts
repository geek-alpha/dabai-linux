// ========== 在线音乐管理 UI（搜索 / 自建歌单 / 队列播放） ==========

import type { AppKernel, MusicSong } from '../types/app-kernel.js';

export default function init_28_music_ui(App: AppKernel) {
  // 播放队列状态
  let queueSongs: MusicSong[] = [];   // [{source,id,name,artists}]
  let queueIndex = 0;
  let queueActive = false;

  /* ---------- 弹窗开关 ---------- */
  App.openMusicModal = function openMusicModal() {
    App.musicModal!.classList.add('show');
    if (!App._musicSearchDone) App.refreshMusicPlaylists();
    if (App.getBGMState) renderMusicNowPlaying();
    setTimeout(() => {
      if (App.musicPaneSearch!.style.display !== 'none') {
        App.musicSearchInput!.focus();
        App.musicSearchInput!.select();
      }
    }, 120);
  };
  App.closeMusicModal = function closeMusicModal() {
    App.musicModal!.classList.remove('show');
  };

  App.switchMusicTab = function switchMusicTab(tab: 'search' | 'playlists' | 'boards') {
    const isSearch = tab === 'search';
    App.musicTabSearch!.classList.toggle('active', isSearch);
    App.musicTabPlaylists!.classList.toggle('active', tab === 'playlists');
    App.musicTabBoards!.classList.toggle('active', tab === 'boards');
    App.musicPaneSearch!.style.display = isSearch ? '' : 'none';
    App.musicPanePlaylists!.style.display = tab === 'playlists' ? '' : 'none';
    App.musicPaneBoards!.style.display = tab === 'boards' ? '' : 'none';
    if (tab === 'playlists') App.refreshMusicPlaylists();
    if (tab === 'boards') App.loadMusicBoards();
  };

  /* ---------- 搜索 ---------- */
  App.musicSearch = async function musicSearch() {
    const kw = App.musicSearchInput!.value.trim();
    if (!kw) { App.showToast('请输入搜索关键词'); return; }
    App.musicSearchResults!.innerHTML = '<div style="text-align:center;color:var(--text-dim);padding:20px">搜索中…</div>';
    try {
      const res = await fetch(`/api/music/search?kw=${encodeURIComponent(kw)}&limit=12`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      App._musicSearchDone = true;
      App.renderMusicSearchResults(data.results || []);
    } catch (e) {
      const err = e as Error;
      App.musicSearchResults!.innerHTML = '<div style="text-align:center;color:var(--text-dim);padding:20px">搜索失败，请稍后再试</div>';
      App.showToast('搜索失败: ' + (err.message || e));
    }
  };

  App.renderMusicSearchResults = function renderMusicSearchResults(songs: MusicSong[]) {
    App.musicSearchResults!.innerHTML = '';
    if (songs.length === 0) {
      App.musicSearchResults!.innerHTML = '<div style="text-align:center;color:var(--text-dim);padding:20px">没搜到相关歌曲，换个关键词试试</div>';
      return;
    }
    for (const s of songs) {
      const card = document.createElement('div');
      card.className = 'model-card music-song-card';
      card.innerHTML = `
        <div class="model-preview music-preview">${s.source === 'kuwo' ? 'KW' : 'NTE'}</div>
        <div class="model-info">
          <div class="model-name">${App.escapeHtml(s.name || '未知')}</div>
          <div class="model-meta">${App.escapeHtml(s.artists || '未知歌手')} · ${App.escapeHtml(s.album || '')}${s.vip ? ' · VIP' : ''}</div>
        </div>
        <div class="model-actions music-actions">
          <button class="music-act-btn" data-act="play" title="播放">▶</button>
          <button class="music-act-btn" data-act="add" title="加入歌单">＋</button>
        </div>
      `;
      card.querySelector('[data-act="play"]')!.addEventListener('click', () => {
        App.playMusicSong(s);
      });
      card.querySelector('[data-act="add"]')!.addEventListener('click', () => {
        App.addSongToPlaylistUI(s);
      });
      App.musicSearchResults!.appendChild(card);
    }
  };

  /* ---------- 播放单曲 ---------- */
  App.playMusicSong = async function playMusicSong(song: MusicSong) {
    queueActive = false;
    App.showModelLoading(`解析播放链接…`);
    try {
      const res = await fetch(`/api/music/resolve?source=${encodeURIComponent(song.source)}&song_id=${encodeURIComponent(song.id)}`);
      const data = await res.json();
      if (!res.ok || !data.ok) {
        App.showToast(data.error || '这首歌暂时放不了（可能 VIP/版权受限）');
        return;
      }
      // 音量沿用用户上次在音乐界面调好的值（独立持久化，不重置）
      App.playMusicTrack(data.url, `${song.name} - ${song.artists}`);
      App.showToast('正在播放: ' + song.name);
    } catch (e) {
      const err = e as Error;
      App.showToast('播放失败: ' + (err.message || e));
    } finally {
      App.hideModelLoading();
    }
  };

  /* ---------- 自建歌单 ---------- */
  App.createMusicPlaylist = async function createMusicPlaylist() {
    const name = App.musicPlaylistName!.value.trim();
    if (!name) { App.showToast('请输入歌单名'); return; }
    try {
      const res = await fetch('/api/music/playlists', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name })
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      App.musicPlaylistName!.value = '';
      App.refreshMusicPlaylists();
      App.showToast(`歌单《${name}》创建成功`);
    } catch (e) {
      const err = e as Error;
      App.showToast('创建失败: ' + (err.message || e));
    }
  };

  App.refreshMusicPlaylists = async function refreshMusicPlaylists() {
    try {
      const res = await fetch('/api/music/playlists');
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      App.renderMusicPlaylists(data.playlists || []);
    } catch (e) {
      App.musicPlaylistsEl!.innerHTML = '<div style="text-align:center;color:var(--text-dim);padding:20px">获取歌单失败</div>';
    }
  };

  App.renderMusicPlaylists = async function renderMusicPlaylists(list: any[]) {
    App.musicPlaylistsEl!.innerHTML = '';
    if (list.length === 0) {
      App.musicPlaylistsEl!.innerHTML = '<div style="text-align:center;color:var(--text-dim);padding:20px">还没有歌单，先创建一个吧</div>';
      return;
    }
    for (const p of list) {
      // 拉详情拿到歌曲列表
      let detail: any = { songs: [] };
      try {
        const res = await fetch(`/api/music/playlists/${encodeURIComponent(p.id)}`);
        if (res.ok) detail = (await res.json()).playlist || { songs: [] };
      } catch (e) { /* ignore */ }
      const card = document.createElement('div');
      card.className = 'model-card';
      card.innerHTML = `
        <div class="model-preview music-preview">♫</div>
        <div class="model-info">
          <div class="model-name">${App.escapeHtml(p.name)}</div>
          <div class="model-meta">${p.song_count} 首</div>
        </div>
        <div class="model-actions music-actions">
          <button class="music-act-btn" data-act="play" title="播放歌单">▶</button>
          <button class="music-act-btn" data-act="del" title="删除歌单">✕</button>
        </div>
      `;
      card.querySelector('[data-act="play"]')!.addEventListener('click', () => {
        App.playPlaylistDetail({ id: p.id, name: p.name, songs: detail.songs });
      });
      card.querySelector('[data-act="del"]')!.addEventListener('click', async () => {
        if (!confirm(`删除歌单《${p.name}》？`)) return;
        try {
          const res = await fetch(`/api/music/playlists/${encodeURIComponent(p.id)}`, { method: 'DELETE' });
          if (!res.ok) throw new Error('删除失败');
          App.refreshMusicPlaylists();
          App.showToast('已删除歌单');
        } catch (e) {
          App.showToast('删除失败');
        }
      });
      // 展开歌曲列表
      if (detail.songs && detail.songs.length) {
        const songBox = document.createElement('div');
        songBox.className = 'music-playlist-songs';
        detail.songs.forEach((s: any, i: number) => {
          const row = document.createElement('div');
          row.className = 'music-song-row';
          row.innerHTML = `
            <span class="music-song-no">${i + 1}</span>
            <span class="music-song-name">${App.escapeHtml(s.name || '未知')}${s.artists ? ' - ' + App.escapeHtml(s.artists) : ''}</span>
            <span class="music-song-rm" title="移出歌单">✕</span>
          `;
          row.addEventListener('click', e => {
            const target = e.target as HTMLElement;
            if (target.classList.contains('music-song-rm')) {
              App.removeSongFromPlaylist(p.id, s.id);
              return;
            }
            App.playMusicSong(s);
          });
          songBox.appendChild(row);
        });
        card.appendChild(songBox);
      }
      App.musicPlaylistsEl!.appendChild(card);
    }
  };

  /* ---------- 热门榜单（云音乐热歌/新歌/飙升/原创） ---------- */
  App.loadMusicBoards = async function loadMusicBoards() {
    const el = App.musicBoardsEl;
    if (!el) return;
    if (App._musicBoards) {
      App.renderMusicBoards(App._musicBoards);
      return;
    }
    el.innerHTML = '<div class="music-board-loading">榜单加载中…</div>';
    try {
      const res = await fetch('/api/music/boards');
      if (!res.ok) throw new Error('HTTP ' + res.status);
      const data = await res.json();
      App._musicBoards = data.boards || [];
      if (!App._musicBoards.length) {
        el.innerHTML = '<div class="music-board-err">暂时拿不到榜单，稍后再试</div>';
        return;
      }
      App.renderMusicBoards(App._musicBoards);
    } catch (e) {
      el.innerHTML = '<div class="music-board-err">榜单加载失败，稍后再试</div>';
    }
  };

  App.renderMusicBoards = function renderMusicBoards(boards: any[]) {
    const el = App.musicBoardsEl!;
    el.innerHTML = '';
    boards.forEach((b, i) => {
      const card = document.createElement('div');
      card.className = 'music-board-card';
      const rankCls = i < 3 ? 'music-board-rank rank-hot' : 'music-board-rank';
      card.innerHTML = '<div class="' + rankCls + '">' + (i + 1) + '</div>'
        + '<div class="music-board-name">' + App.escapeHtml(b.name || '未知榜单') + '</div>'
        + '<div class="music-board-count">' + (b.song_count != null ? b.song_count + ' 首' : '点开查看') + '</div>';
      card.addEventListener('click', () => {
        App.loadMusicBoardSongs(b);
      });
      el.appendChild(card);
    });
  };

  App.loadMusicBoardSongs = async function loadMusicBoardSongs(board: any) {
    const el = App.musicBoardsEl!;
    el.innerHTML = '<div class="music-board-loading">《' + App.escapeHtml(board.name) + '》加载中…</div>';
    try {
      const res = await fetch('/api/music/boards/' + encodeURIComponent(board.id));
      if (!res.ok) throw new Error('HTTP ' + res.status);
      const data = await res.json();
      const songs = (data.board && data.board.songs) || [];
      el.innerHTML = '';
      if (!songs.length) {
        el.innerHTML = '<div class="music-board-err">这个榜单暂时没有可播放的歌曲</div>';
        return;
      }
      const head = document.createElement('div');
      head.className = 'music-boards-tip';
      head.textContent = '《' + (data.board.name || board.name) + '》 · 共 ' + songs.length + ' 首 · 点击歌曲播放：';
      const back = document.createElement('span');
      back.className = 'music-song-rm';
      back.textContent = '返回榜单';
      back.style.cssText = 'cursor:pointer;margin-left:8px;color:#7c5cff';
      back.addEventListener('click', (e: Event) => {
        e.stopPropagation();
        App.renderMusicBoards(App._musicBoards || []);
      });
      head.appendChild(back);
      el.appendChild(head);
      songs.forEach((s: any, i: number) => {
        const row = document.createElement('div');
        row.className = 'music-song-row';
        row.innerHTML = '<span class="music-song-no">' + (i + 1) + '</span>'
          + '<span class="music-song-name">' + App.escapeHtml(s.name || '未知') + (s.artists ? ' - ' + App.escapeHtml(s.artists) : '') + '</span>';
        row.addEventListener('click', () => {
          App.playMusicSong({ source: s.source, id: String(s.id), name: s.name || '未知', artists: s.artists || '' });
        });
        el.appendChild(row);
      });
    } catch (e) {
      el.innerHTML = '<div class="music-board-err">榜单歌曲加载失败，稍后再试</div>';
    }
  };

  /* ---------- 歌曲加入歌单 ---------- */
  App.addSongToPlaylistUI = async function addSongToPlaylistUI(song: MusicSong) {
    let pls: any[] = [];
    try {
      const res = await fetch('/api/music/playlists');
      if (res.ok) pls = (await res.json()).playlists || [];
    } catch (e) { /* ignore */ }
    const names = pls.map(p => p.name);
    const target = prompt(
      '把《' + song.name + '》加入哪个歌单？\n（输入现有歌单名，或输入新名字创建）\n现有：' +
      (names.join('、') || '（暂无）'),
      pls[0] ? pls[0].name : ''
    );
    if (!target) return;
    const targetName = target.trim();
    let pid: string | null = null;
    const exist = pls.find(p => p.name === targetName);
    if (exist) {
      pid = exist.id;
    } else {
      try {
        const res = await fetch('/api/music/playlists', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ name: targetName })
        });
        if (res.ok) pid = (await res.json()).playlist.id;
      } catch (e) { /* ignore */ }
    }
    if (!pid) { App.showToast('创建/定位歌单失败'); return; }
    try {
      const res = await fetch(`/api/music/playlists/${encodeURIComponent(pid)}/songs`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ song: { source: song.source, id: song.id, name: song.name, artists: song.artists } })
      });
      if (!res.ok) throw new Error('HTTP ' + res.status);
      App.showToast(`已加入歌单《${targetName}》`);
    } catch (e) {
      const err = e as Error;
      App.showToast('加入失败: ' + (err.message || e));
    }
  };

  App.removeSongFromPlaylist = async function removeSongFromPlaylist(pid: string, songId: string) {
    try {
      const res = await fetch(`/api/music/playlists/${encodeURIComponent(pid)}/songs/${encodeURIComponent(songId)}`, { method: 'DELETE' });
      if (!res.ok) throw new Error('HTTP ' + res.status);
      App.refreshMusicPlaylists();
      App.showToast('已移出歌单');
    } catch (e) {
      App.showToast('移除失败');
    }
  };

  /* ---------- 歌单队列播放 ---------- */
  App.playPlaylistDetail = function playPlaylistDetail(pl: any) {
    if (!pl.songs || !pl.songs.length) {
      App.showToast('歌单是空的');
      return;
    }
    queueSongs = pl.songs.map((s: MusicSong) => ({ ...s }));
    queueIndex = 0;
    queueActive = true;
    App.showToast(`开始播放歌单《${pl.name}》`);
    playQueueAt();
  };

  // AI 发来的 play_playlist 屏幕命令
  App.playPlaylistCmd = function playPlaylistCmd(args: any) {
    const songs = (args && args.songs) || [];
    if (!songs.length) {
      App.showToast(args && args.error ? args.error : '歌单是空的');
      return;
    }
    queueSongs = songs.map((s: MusicSong) => ({ ...s }));
    queueIndex = 0;
    queueActive = true;
    App.showToast(`开始播放歌单「${args.title || ''}」`);
    playQueueAt();
  };

  function playQueueAt() {
    if (!queueActive || queueIndex >= queueSongs.length) {
      queueActive = false;
      if (queueSongs.length && queueIndex >= queueSongs.length) {
        App.showToast('歌单播放完毕');
      }
      return;
    }
    const song = queueSongs[queueIndex];
    fetch(`/api/music/resolve?source=${encodeURIComponent(song.source)}&song_id=${encodeURIComponent(song.id)}`)
      .then(r => r.json())
      .then(data => {
        if (!data.ok) {
          console.warn('[Music] 跳过无法解析的歌曲:', song.name, data.error);
          queueIndex++;
          playQueueAt();
          return;
        }
        // 音量沿用用户上次调好的值（独立持久化，不重置）
        App.playMusicTrack(data.url, `${song.name} - ${song.artists}`);
      })
      .catch(err => {
        console.warn('[Music] 解析失败:', err);
        queueIndex++;
        playQueueAt();
      });
  }

  // 单曲播完 → 回报子智能体（music_end 闭环看护 worker）+ 下一首
  App.onMusicTrackEnded = function onMusicTrackEnded() {
    const final = !queueActive || queueIndex + 1 >= queueSongs.length;
    const wid = App._musicWorkerId || undefined;
    const name = (App.getCurrentBGM && App.getCurrentBGM()) || undefined;
    if (App.ws && App.ws.readyState === WebSocket.OPEN && (wid || name)) {
      App.ws.send(JSON.stringify({ type: 'music_end', worker_id: wid, name, final: !!final }));
    }
    if (!queueActive) return;
    queueIndex++;
    playQueueAt();
  };

  // 停止时清空队列 + 收回看护子智能体（music_stop）
  const _origStopBGM = App.stopBGM;
  App.stopBGM = function stopBGM() {
    queueActive = false;
    const wid = App._musicWorkerId;
    App._musicWorkerId = null;
    _origStopBGM.call(this);
    if (wid && App.ws && App.ws.readyState === WebSocket.OPEN) {
      App.ws.send(JSON.stringify({ type: 'music_stop', worker_id: wid }));
    }
  };

  /* ---------- 正在播放控制条：暂停/继续 + 停止 + 音量（状态实时同步） ---------- */
  const npEls = {
    panel: App.musicNowPlaying,
    title: App.musicNpTitle,
    state: App.musicNpState,
    toggle: App.musicNpToggle,
    stop: App.musicNpStop,
    vol: App.musicNpVol,
    volLabel: App.musicNpVolLabel,
    track: App.musicNpTrack,
    fill: App.musicNpFill,
    knob: App.musicNpKnob,
    time: App.musicNpTime,
  };
  let npVolDragging = false;   // 拖动音量中：不覆盖滑块位置
  let npSeeking = false;       // 拖动进度中：不覆盖进度预览

  const fmtT = (s: number) => {
    s = Math.max(0, Math.floor(s || 0));
    const m = Math.floor(s / 60), ss = s % 60;
    return m + ':' + String(ss).padStart(2, '0');
  };

  function renderMusicNowPlaying(dragFrac?: number) {
    const st = App.getBGMState ? App.getBGMState() : null;
    const els = npEls;
    if (!els.panel || !st) return;
    // 拖动进度中：忽略无参刷新（timeupdate 广播），保持拖动预览位置
    if (npSeeking && dragFrac === undefined) return;
    // 没有可播放的音乐 → 隐藏控制条
    if (st.stopped || !st.name) {
      els.panel.style.display = 'none';
      return;
    }
    els.panel.style.display = 'flex';
    els.title!.textContent = st.name;
    els.title!.title = st.name;
    els.state!.textContent = st.playing ? '播放中' : '已暂停';
    els.state!.classList.toggle('playing', !!st.playing);
    // 暂停/继续按钮：图标 + 文案与真实状态同步
    els.toggle!.textContent = st.playing ? '⏸' : '▶';
    els.toggle!.title = st.playing ? '暂停' : '继续播放';
    // 音量滑块（拖动中不被覆盖）
    if (!npVolDragging && els.vol) {
      const pct = Math.round(st.volume * 100);
      els.vol.value = String(pct);
      if (els.volLabel) els.volLabel.textContent = pct + '%';
    }
    // 进度条：时长已知 → 实时填充 + 可拖动；未知 → 禁用
    const dur = st.duration;
    const seekable = dur > 0;
    els.track!.classList.toggle('music-np-disabled', !seekable);
    els.track!.title = seekable ? '拖动跳转进度' : '时长未知，暂不支持拖动进度';
    const frac = dur > 0 ? Math.min(1, Math.max(0, st.currentTime / dur)) : 0;
    const showFrac = dragFrac !== undefined && seekable ? dragFrac : frac;
    els.fill!.style.width = (showFrac * 100).toFixed(2) + '%';
    els.knob!.style.left = (showFrac * 100).toFixed(2) + '%';
    if (dragFrac !== undefined && seekable) {
      els.time!.textContent = fmtT(dragFrac * dur) + ' / ' + fmtT(dur);
    } else if (seekable) {
      els.time!.textContent = fmtT(st.currentTime) + ' / ' + fmtT(dur);
    } else {
      els.time!.textContent = '— / —';
    }
  }

  // 播放器任何状态变化（播放/暂停/停止/音量/换歌）都实时刷新控制条
  if (App.onBGMStateChange) {
    App.onBGMStateChange(() => renderMusicNowPlaying());
  }

  // 按钮事件：暂停/继续 与 停止（停止走 stopBGM 包装：清队列 + 收回看护 worker）
  npEls.toggle && npEls.toggle.addEventListener('click', () => {
    App.toggleBGM();
  });
  npEls.stop && npEls.stop.addEventListener('click', () => {
    App.stopBGM();
    App.showToast('已停止播放音乐');
  });
  // 音量滑块：拖动时实时生效，变化后由状态事件回写（拖动中防覆盖）
  npEls.vol && npEls.vol.addEventListener('pointerdown', () => { npVolDragging = true; });
  npEls.vol && npEls.vol.addEventListener('pointerup', () => { npVolDragging = false; renderMusicNowPlaying(); });
  npEls.vol && npEls.vol.addEventListener('pointercancel', () => { npVolDragging = false; });
  npEls.vol && npEls.vol.addEventListener('input', () => {
    npVolDragging = true;
    const v = Math.max(0, Math.min(1, Number(npEls.vol!.value) / 100));
    App.setBGMVolume(v);
    if (npEls.volLabel) npEls.volLabel.textContent = Math.round(v * 100) + '%';
  });
  npEls.vol && npEls.vol.addEventListener('change', () => { npVolDragging = false; });

  // 进度条拖动：拖动中预览时间，松手跳转（时长未知时禁用）
  const trackPosOf = (ev: PointerEvent) => {
    const rect = npEls.track!.getBoundingClientRect();
    return rect.width > 0 ? Math.max(0, Math.min(1, (ev.clientX - rect.left) / rect.width)) : 0;
  };
  const trackSeekable = () => {
    const st = App.getBGMState ? App.getBGMState() : null;
    return !!(st && st.duration > 0);
  };
  npEls.track && npEls.track.addEventListener('pointerdown', (ev: PointerEvent) => {
    if (!trackSeekable()) {
      App.showToast('时长未知，暂不支持拖动进度');
      return;
    }
    npSeeking = true;
    npEls.track!.setPointerCapture && npEls.track!.setPointerCapture(ev.pointerId);
    renderMusicNowPlaying(trackPosOf(ev));
  });
  npEls.track && npEls.track.addEventListener('pointermove', (ev: PointerEvent) => {
    if (!npSeeking) return;
    renderMusicNowPlaying(trackPosOf(ev));
  });
  npEls.track && npEls.track.addEventListener('pointerup', (ev: PointerEvent) => {
    if (!npSeeking) return;
    npSeeking = false;
    const st = App.getBGMState ? App.getBGMState() : null;
    if (st && st.duration > 0) {
      App.seekBGM(trackPosOf(ev) * st.duration);
    }
    renderMusicNowPlaying();
  });
  npEls.track && npEls.track.addEventListener('pointercancel', () => {
    npSeeking = false;
    renderMusicNowPlaying();
  });

  // 初始渲染一次（若打开弹窗时正在播放）
  renderMusicNowPlaying();
}
