// ========== 背景音乐播放器 (BGM Player) ==========

import type { AppKernel, BGMState } from '../types/app-kernel.js';

export default function init_20_bgm_player(App: AppKernel) {
  let bgmAudio: HTMLAudioElement | null = null;
  let currentBgmName: string | null = null;
  // 音乐音量独立持久化（与视频/角色语音互不影响）：用户在音乐界面
  // 调好的音量跨歌曲、跨刷新沿用，不再被每次播放重置
  const MUSIC_VOLUME_KEY = 'dabai.musicVolume.v1';
  function loadMusicVolume(): number {
    try {
      const v = Number(JSON.parse(localStorage.getItem(MUSIC_VOLUME_KEY) || 'null'));
      if (isFinite(v) && v >= 0 && v <= 1) return v;
    } catch (e) { /* 读取失败用默认 */ }
    return 0.8;
  }
  let volume = loadMusicVolume();
  let lastTime = 0;       // 上次记录的播放位置
  let loopCount = 0;       // 循环次数
  const stateListeners: Array<(s: BGMState) => void> = [];  // UI 状态同步订阅者

  /** 当前播放状态快照（无 Audio 元素/无 src → stopped） */
  function snapshot(): BGMState {
    const a = bgmAudio;
    const hasSrc = !!(a && a.src);
    return {
      name: currentBgmName,
      playing: !!(hasSrc && !a!.paused),
      paused: !!(hasSrc && a!.paused && !a!.ended),
      stopped: !hasSrc,
      volume,
      currentTime: a ? a.currentTime : 0,
      duration: a && Number.isFinite(a.duration) ? a.duration : 0,
    };
  }

  /** 广播状态给所有订阅者（UI 据此实时同步按钮/标题） */
  function emitBgmState() {
    const st = snapshot();
    for (const cb of stateListeners) {
      try { cb(st); } catch (e) { /* 单个订阅者异常不影响其他订阅者 */ }
    }
  }

  /**
   * 通知服务器当前 BGM 状态
   */
  function notifyBgmState(name: string | null) {
    if (!App.ws || App.ws.readyState !== WebSocket.OPEN) return;
    App.ws.send(JSON.stringify({ type: 'set_bgm', name: name || null }));
  }

  // 创建 Audio 元素
  function ensureAudio(): HTMLAudioElement {
    if (!bgmAudio) {
      bgmAudio = new Audio();
      bgmAudio.loop = true;
      bgmAudio.volume = volume;
      // 单曲（在线音乐）播完时清理状态并通知服务器
      bgmAudio.addEventListener('ended', () => {
        if (!bgmAudio!.loop) {
          currentBgmName = null;
          console.log('[BGM] 在线音乐播放结束');
          notifyBgmState(null);
          emitBgmState();
          // 通知歌单队列控制器继续播下一首（若存在）
          if (App.onMusicTrackEnded) App.onMusicTrackEnded();
        }
      });
      // 状态事件 → 广播（UI 按钮/标题与真实播放状态实时同步）
      bgmAudio.addEventListener('play', emitBgmState);
      bgmAudio.addEventListener('pause', emitBgmState);
      bgmAudio.addEventListener('volumechange', emitBgmState);
      bgmAudio.addEventListener('loadeddata', emitBgmState);
      // 通过 timeupdate 检测循环（loop=true 时 ended 不会触发）
      bgmAudio.addEventListener('timeupdate', () => {
        const t = bgmAudio!.currentTime;
        // 当前时间比上次记录的时间显著回跳 → 说明完成了一次循环
        if (t < lastTime - 1.0) {
          loopCount++;
          console.log(`[BGM] 第 ${loopCount} 次循环结束`);
          App.sendAIAction && App.sendAIAction(`（《${currentBgmName}》已经循环播放了第${loopCount}遍，音乐还在继续陪伴着你）`);
        }
        lastTime = t;
        if (bgmAudio && bgmAudio.src) emitBgmState();  // 进度条实时刷新（约 4 次/秒）
      });
    }
    return bgmAudio;
  }

  /**
   * 解锁音频元素：首次用户交互时播放一次空内容，绕开浏览器的自动播放策略，
   * 这样之后 AI 触发的播放（无用户手势）也能正常出声。
   */
  function unlockAudioOnGesture() {
    try {
      const a = ensureAudio();
      if (a.src) {
        const p = a.play();
        if (p) p.then(() => { a.pause(); a.currentTime = 0; }).catch(() => {});
      }
    } catch (e) { /* 忽略 */ }
  }
  if (document.addEventListener) {
    document.addEventListener('pointerdown', unlockAudioOnGesture, { once: true, passive: true });
    document.addEventListener('keydown', unlockAudioOnGesture, { once: true });
  }

  /**
   * 播放背景音乐
   * @param url - 音频文件 URL
   * @param name - 音乐文件名（用于显示）
   */
  App.playBGM = function (url: string, name: string) {
    const audio = ensureAudio();
    audio.loop = true; // BGM 循环播放
    // 如果正在播放同一首，不做任何事
    if (currentBgmName === name && !audio.paused) {
      return;
    }
    // 停止当前播放
    audio.pause();
    audio.src = url;
    audio.load();
    audio.play().then(() => {
      currentBgmName = name;
      lastTime = 0;
      loopCount = 0;
      console.log('[BGM] 开始播放:', name);
      notifyBgmState(name);
      emitBgmState();
    }).catch(err => {
      console.warn('[BGM] 播放失败:', err);
      emitBgmState();
    });
  };

  /**
   * 播放在线音乐单曲（不循环，播完自动结束）
   * @param url - 音频流 URL（music-relay）
   * @param name - 歌名（用于显示）
   */
  App.playMusicTrack = function (url: string, name: string) {
    const audio = ensureAudio();
    audio.loop = false;
    audio.pause();
    audio.src = url;
    audio.load();
    audio.play().then(() => {
      currentBgmName = name;
      lastTime = 0;
      loopCount = 0;
      console.log('[BGM] 开始播放在线音乐:', name);
      notifyBgmState(name);
      emitBgmState();
    }).catch(err => {
      console.warn('[BGM] 在线音乐播放失败:', err);
      emitBgmState();
      // 播放失败 → 收回收看护的子智能体（否则它会一直干等播完信号）
      if (App._musicWorkerId && App.ws && App.ws.readyState === WebSocket.OPEN) {
        App.ws.send(JSON.stringify({ type: 'music_stop', worker_id: App._musicWorkerId }));
      }
    });
  };

  /**
   * 停止背景音乐
   */
  App.stopBGM = function () {
    if (bgmAudio) {
      bgmAudio.pause();
      bgmAudio.src = '';
      currentBgmName = null;
      console.log('[BGM] 已停止');
      notifyBgmState(null);
      emitBgmState();
    } else {
      emitBgmState();
    }
  };

  /**
   * 设置背景音乐音量（独立于视频/角色语音，持久化保存）
   * @param vol - 音量 0.0 ~ 1.0
   */
  App.setBGMVolume = function (vol: number) {
    volume = Math.max(0, Math.min(1, vol));
    if (bgmAudio) {
      bgmAudio.volume = volume;
    }
    try { localStorage.setItem(MUSIC_VOLUME_KEY, JSON.stringify(volume)); } catch (e) { /* 忽略 */ }
  };

  /**
   * 获取当前正在播放的 BGM 名称
   */
  App.getCurrentBGM = function () {
    return currentBgmName;
  };

  /**
   * BGM 是否正在播放
   */
  App.isBGMPlaying = function () {
    return !!(bgmAudio && !bgmAudio.paused);
  };

  /**
   * 暂停当前音乐（不换源，可继续恢复；保留 src 与进度）
   */
  App.pauseBGM = function () {
    const a = bgmAudio;
    if (a && a.src && !a.paused) {
      a.pause();  // pause 事件会广播状态
      console.log('[BGM] 已暂停:', currentBgmName);
    }
    emitBgmState();
  };

  /**
   * 继续播放（从暂停位置恢复）
   */
  App.resumeBGM = function () {
    const a = bgmAudio;
    if (!a || !a.src) {
      App.showToast && App.showToast('当前没有正在播放的音乐');
      emitBgmState();
      return;
    }
    if (a.paused) {
      const p = a.play();
      if (p) {
        p.then(() => {
          console.log('[BGM] 已继续:', currentBgmName);
        }).catch(err => {
          console.warn('[BGM] 继续播放失败:', err);
          App.showToast && App.showToast('继续播放失败：请先在页面任意位置点击一次解锁声音');
        });
      }
    }
    emitBgmState();
  };

  /**
   * 播放/暂停切换（UI 按钮用）
   */
  App.toggleBGM = function () {
    const a = bgmAudio;
    if (!a || !a.src) return;
    if (a.paused) App.resumeBGM(); else App.pauseBGM();
  };

  /**
   * 获取播放状态快照（UI 实时同步用）
   */
  App.getBGMState = function () {
    return snapshot();
  };

  /**
   * 跳转到指定播放位置（秒）。时长未知/不可寻址时提示。
   */
  App.seekBGM = function (seconds: number) {
    const a = bgmAudio;
    if (!a || !a.src) { App.showToast && App.showToast('当前没有正在播放的音乐'); return; }
    const dur = Number.isFinite(a.duration) ? a.duration : 0;
    if (!(dur > 0) && a.readyState < 1) { App.showToast && App.showToast('歌曲尚未就绪，稍后再试'); return; }
    const target = Math.max(0, Math.min(dur > 0 ? dur : Number.MAX_SAFE_INTEGER, seconds));
    try {
      a.currentTime = target;
      console.log('[BGM] 跳转进度:', target.toFixed(1), '/', dur.toFixed(1));
    } catch (e) {
      console.warn('[BGM] 跳转失败:', e);
      App.showToast && App.showToast('当前歌曲不支持拖动进度');
    }
    emitBgmState();
  };

  /**
   * 订阅播放状态变化（播放/暂停/停止/音量等都会回调）
   */
  App.onBGMStateChange = function (cb: (s: BGMState) => void) {
    if (typeof cb === 'function') stateListeners.push(cb);
  };
}
