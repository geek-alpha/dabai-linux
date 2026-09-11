import type { AppKernel } from '../types/app-kernel.js';

/* ============================================================
 *  游戏化反馈层（34_game_fx）
 *
 *  把「工具调用」这件枯燥的事变成有节奏的正反馈：
 *    - 连击 Combo：连续成功递增，失败归零
 *    - 暴击 Crit：高连击概率触发，经验翻倍 + 金色粒子 + 屏幕闪光
 *    - 评级 Grade：按耗时给 S/A/B/C，音游式即时评价
 *    - 经验 / 等级：成功即涨经验，升级闪光 + 音效
 *    - 成就徽章：里程碑永久记录（localStorage）
 *    - 自动折叠：一轮工具块太多时收起前面的，聊天不被刷屏
 *    - 音效：WebAudio 实时合成，零资源文件
 *
 *  与 31_tool_chain 的分工：31 负责「把工具块画出来」，本模块只负责
 *  「给这次调用打分 / 加特效 / 记战绩」，通过 App.game.* 钩子接入。
 *
 *  设计原则：**评级/暴击/经验/折叠/成就全部是纯函数**（文件上半部分），
 *  可脱离浏览器单测；DOM 操作只发生在 init() 内。
 * ============================================================ */

/* ============================================================
 *  一、纯逻辑（无 DOM / 无全局依赖，可单测）
 * ============================================================ */

export type Grade = 'S' | 'A' | 'B' | 'C';

/** 评级阈值（毫秒）：S ≤1.5s，A ≤5s，B ≤15s，其余 C */
export const GRADE_LIMITS = { S: 1500, A: 5000, B: 15000 } as const;

export function gradeOf(ms: number): Grade {
  if (!Number.isFinite(ms) || ms < 0) return 'C';
  if (ms <= GRADE_LIMITS.S) return 'S';
  if (ms <= GRADE_LIMITS.A) return 'A';
  if (ms <= GRADE_LIMITS.B) return 'B';
  return 'C';
}

const GRADE_SCORE: Record<Grade, number> = { S: 4, A: 3, B: 2, C: 1 };

/** 评级分值：S=4 / A=3 / B=2 / C=1 */
export function gradeScore(g: Grade): number {
  return GRADE_SCORE[g] ?? 1;
}

/** 暴击率：连击 <3 不暴击；之后每多 1 连击 +5%，封顶 60% */
export function critChance(combo: number): number {
  if (!Number.isFinite(combo) || combo < 3) return 0;
  return Math.min(0.15 + (combo - 2) * 0.05, 0.6);
}

/** 单次工具经验：基础 6 × 评级系数，暴击翻倍 */
export function xpFor(grade: Grade, crit: boolean): number {
  const base = 6 * gradeScore(grade);
  return crit ? base * 2 : base;
}

export const XP_PER_LEVEL = 100;

export function levelOf(xp: number): number {
  return Math.floor(Math.max(0, xp) / XP_PER_LEVEL) + 1;
}

/** 当前等级内的进度 0~1 */
export function levelProgress(xp: number): number {
  return (Math.max(0, xp) % XP_PER_LEVEL) / XP_PER_LEVEL;
}

/** 一轮最多同时显示 6 个工具块，超出的收进折叠条 */
export const FOLD_VISIBLE = 6;
/** 折叠阈值：超过可见上限（第 7 个起）才需要折叠 */
export const FOLD_THRESHOLD = FOLD_VISIBLE + 1;

export function foldPlan(count: number): { folded: boolean; hidden: number } {
  if (!Number.isFinite(count) || count <= FOLD_VISIBLE) return { folded: false, hidden: 0 };
  return { folded: true, hidden: count - FOLD_VISIBLE };
}

/** 连击音阶：C 大调五声音阶，随连击循环上移，听感一路往上爬 */
const COMBO_SCALE = [523.25, 587.33, 659.25, 783.99, 880.0];

export function comboFreq(combo: number): number {
  const n = Number.isFinite(combo) ? Math.max(1, Math.floor(combo)) : 1; // 脏数据兜底为起始音
  const octave = Math.floor((n - 1) / COMBO_SCALE.length);
  const note = COMBO_SCALE[(n - 1) % COMBO_SCALE.length]!;
  return note * Math.pow(2, Math.min(octave, 2)); // 最多升两个八度，避免刺耳
}

export interface RoundStat {
  tools: number;
  crits: number;
  xp: number;
  maxCombo: number;
  failed: number;
  grades: Record<Grade, number>;
}

export function emptyRound(): RoundStat {
  return { tools: 0, crits: 0, xp: 0, maxCombo: 0, failed: 0, grades: { S: 0, A: 0, B: 0, C: 0 } };
}

/**
 * 本轮战绩文案（HTML 片段）。
 * 只展示有意义的项：没连击就不提连击，没暴击就不提暴击 —— 避免每次都是同一句话。
 */
export function roundSummary(s: RoundStat): string {
  const parts = [`<b>${s.tools}</b> 个工具`];
  if (s.maxCombo >= 2) parts.push(`最高 <b>${s.maxCombo}</b> 连击`);
  if (s.crits > 0) parts.push(`<span class="gfx-crit-txt">💥 ${s.crits} 暴击</span>`);
  if (s.failed > 0) parts.push(`失败 ${s.failed}`);
  const grades = (['S', 'A', 'B', 'C'] as Grade[])
    .filter((g) => (s.grades?.[g] ?? 0) > 0)
    .map((g) => `${g}×${s.grades[g]}`);
  if (grades.length) parts.push(grades.join(' '));
  parts.push(`<b>+${s.xp}</b> XP`);
  return `⚔️ 本轮战绩：${parts.join(' · ')}`;
}

export interface GameStats {
  totalTools: number;
  combo: number;
  maxCombo: number;
  crits: number;
  sGrades: number;
  toolsThisRound: number;
  level: number;
}

export interface Achievement {
  id: string;
  name: string;
  icon: string;
  desc: string;
  check: (s: GameStats) => boolean;
}

/** 成就表：顺序即展示顺序 */
export const ACHIEVEMENTS: Achievement[] = [
  { id: 'first_blood', name: '首杀', icon: '🗡️', desc: '第一次调用工具', check: (s) => s.totalTools >= 1 },
  { id: 'combo5', name: '五连击', icon: '🔥', desc: '连续 5 次工具成功', check: (s) => s.maxCombo >= 5 },
  { id: 'combo10', name: '十连击', icon: '⚡', desc: '连续 10 次工具成功', check: (s) => s.maxCombo >= 10 },
  { id: 'combo20', name: '暴走', icon: '🌪️', desc: '连续 20 次工具成功', check: (s) => s.maxCombo >= 20 },
  { id: 'crit', name: '会心一击', icon: '💥', desc: '触发一次暴击', check: (s) => s.crits >= 1 },
  { id: 'crit10', name: '暴击狂魔', icon: '☄️', desc: '累计 10 次暴击', check: (s) => s.crits >= 10 },
  { id: 'grade_s', name: '秒回', icon: '🎯', desc: '拿到一次 S 评级（1.5 秒内完成）', check: (s) => s.sGrades >= 1 },
  { id: 'swarm', name: '五箭齐发', icon: '🏹', desc: '单轮调用 5 个工具', check: (s) => s.toolsThisRound >= 5 },
  { id: 'hundred', name: '百战', icon: '🏆', desc: '累计 100 次工具调用', check: (s) => s.totalTools >= 100 },
  { id: 'lv5', name: '渐入佳境', icon: '🌟', desc: '升到 5 级', check: (s) => s.level >= 5 },
];

/** 找出本次新解锁的成就（已拥有的不再返回） */
export function newlyUnlocked(stats: GameStats, owned: string[]): Achievement[] {
  const have = new Set(owned ?? []);
  return ACHIEVEMENTS.filter((a) => !have.has(a.id) && a.check(stats));
}

/* ============================================================
 *  二、DOM 特效（仅在浏览器 init 后可用）
 * ============================================================ */

interface Saved {
  xp: number;
  maxCombo: number;
  totalTools: number;
  crits: number;
  sGrades: number;
  achievements: string[];
}

export default function init(App: AppKernel) {
  const storeKey = 'dabai_game_v1';

  const load = (): Saved => {
    const dflt: Saved = { xp: 0, maxCombo: 0, totalTools: 0, crits: 0, sGrades: 0, achievements: [] };
    try {
      const raw = localStorage.getItem(storeKey);
      if (!raw) return dflt;
      const p = JSON.parse(raw);
      return { ...dflt, ...p, achievements: Array.isArray(p?.achievements) ? p.achievements : [] };
    } catch {
      return dflt;
    }
  };
  const save = () => {
    try {
      localStorage.setItem(storeKey, JSON.stringify(saved));
    } catch {
      /* 隐私模式 / 配额满：游戏数据不值得报错 */
    }
  };

  const saved = load();
  let combo = 0;
  let turnBlocks: HTMLElement[] = []; // 本轮（一条 AI 回复内）的工具块
  let lastLevel = levelOf(saved.xp);
  let roundStat: RoundStat = emptyRound();
  let settleTimer: ReturnType<typeof setTimeout> | null = null;

  /* ---------- 音效：WebAudio 实时合成（零资源文件） ---------- */
  let audio: AudioContext | null = null;
  const audioCtx = (): AudioContext | null => {
    if (App.gameMuted) return null;
    try {
      if (!audio) {
        const Ctor = window.AudioContext || (window as any).webkitAudioContext;
        if (!Ctor) return null;
        audio = new Ctor();
      }
      if (audio.state === 'suspended') void audio.resume();
      return audio;
    } catch {
      return null;
    }
  };
  const tone = (freq: number, dur = 0.08, type: OscillatorType = 'sine', vol = 0.05, delay = 0) => {
    const ctx = audioCtx();
    if (!ctx) return;
    const t0 = ctx.currentTime + delay;
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.type = type;
    osc.frequency.setValueAtTime(freq, t0);
    gain.gain.setValueAtTime(0, t0);
    gain.gain.linearRampToValueAtTime(vol, t0 + 0.008);
    gain.gain.exponentialRampToValueAtTime(0.0001, t0 + dur);
    osc.connect(gain).connect(ctx.destination);
    osc.start(t0);
    osc.stop(t0 + dur + 0.02);
  };
  const sfx = {
    combo: (n: number) => tone(comboFreq(n), 0.09, 'triangle', 0.045),
    crit: () => {
      tone(180, 0.22, 'sawtooth', 0.06);
      tone(1400, 0.12, 'square', 0.03, 0.01);
      tone(2100, 0.16, 'sine', 0.035, 0.04);
    },
    grade: (g: Grade) => {
      if (g === 'S') [784, 988, 1319].forEach((f, i) => tone(f, 0.1, 'triangle', 0.05, i * 0.06));
      else if (g === 'A') [659, 880].forEach((f, i) => tone(f, 0.09, 'triangle', 0.04, i * 0.06));
      else if (g === 'B') tone(523, 0.08, 'triangle', 0.035);
      else tone(300, 0.1, 'sine', 0.03);
    },
    fail: () => {
      tone(220, 0.16, 'sawtooth', 0.045);
      tone(160, 0.22, 'sawtooth', 0.04, 0.08);
    },
    levelUp: () => [523, 659, 784, 1047].forEach((f, i) => tone(f, 0.14, 'sine', 0.05, i * 0.09)),
    achievement: () => [880, 1175].forEach((f, i) => tone(f, 0.18, 'sine', 0.05, i * 0.12)),
  };

  /* ---------- HUD：注入聊天面板头部（不额外占屏，收起时随面板一起隐藏） ---------- */
  const hud = document.createElement('span');
  hud.className = 'gfx-hud';
  hud.id = 'gfx-hud';
  hud.title = '点击查看成就墙';
  hud.innerHTML = '<b class="gfx-lv">Lv.1</b><span class="gfx-combo" hidden>x0</span><span class="gfx-mute" title="静音 / 恢复">🔊</span>';
  // 注意：头部那行是 class="chat-head-actions"（HTML 里没有对应 id）。
  // 只查 id 会落空 → 整块 HUD 被 append 到 body 末尾、跑到面板外面去。
  const headActions = document.getElementById('chat-head-actions')
    || document.querySelector('.chat-head-actions');
  if (headActions && headActions.parentElement) headActions.parentElement.insertBefore(hud, headActions);
  else document.body.appendChild(hud);

  const hudLv = hud.querySelector('.gfx-lv') as HTMLElement;
  const hudCombo = hud.querySelector('.gfx-combo') as HTMLElement;
  const hudMute = hud.querySelector('.gfx-mute') as HTMLElement;

  hudMute.addEventListener('click', (e) => {
    e.stopPropagation();
    App.gameMuted = !App.gameMuted;
    hudMute.textContent = App.gameMuted ? '🔇' : '🔊';
  });

  const renderHud = () => {
    hudLv.textContent = `Lv.${levelOf(saved.xp)}`;
    hudLv.style.setProperty('--p', `${(levelProgress(saved.xp) * 100).toFixed(1)}%`);
    if (combo >= 2) {
      hudCombo.hidden = false;
      hudCombo.textContent = `x${combo}`;
      hudCombo.classList.toggle('hot', combo >= 5);
      hudCombo.classList.remove('pop');
      void hudCombo.offsetWidth;
      hudCombo.classList.add('pop');
    } else {
      hudCombo.hidden = true;
    }
  };

  /* ---------- 成就墙（点击 HUD 展开） ---------- */
  const panel = document.createElement('div');
  panel.className = 'gfx-ach-panel';
  panel.id = 'gfx-ach-panel';
  document.body.appendChild(panel);
  let panelOpen = false;

  const resetAll = () => {
    saved.xp = 0;
    saved.maxCombo = 0;
    saved.totalTools = 0;
    saved.crits = 0;
    saved.sGrades = 0;
    saved.achievements = [];
    combo = 0;
    lastLevel = 1;
    save();
    renderHud();
    renderPanel();
  };

  function renderPanel() {
    const owned = new Set(saved.achievements);
    const done = ACHIEVEMENTS.filter((a) => owned.has(a.id)).length;
    panel.innerHTML =
      '<div class="gfx-ach-head">成就 <b>' + done + '</b> / ' + ACHIEVEMENTS.length +
      ' · Lv.' + levelOf(saved.xp) + ' · ' + saved.totalTools + ' 次工具</div>' +
      '<div class="gfx-ach-bar"><i style="width:' + ((done / ACHIEVEMENTS.length) * 100).toFixed(1) + '%"></i></div>' +
      ACHIEVEMENTS.map((a) =>
        '<div class="gfx-ach-item ' + (owned.has(a.id) ? 'owned' : '') + '">' +
        '<span class="gfx-ach-ic">' + a.icon + '</span>' +
        '<div class="gfx-ach-txt"><b>' + App.escapeHtml(a.name) + '</b><i>' + App.escapeHtml(a.desc) + '</i></div>' +
        '</div>').join('') +
      '<div class="gfx-ach-foot"><span class="gfx-ach-reset">清空战绩</span></div>';
    const reset = panel.querySelector('.gfx-ach-reset') as HTMLElement | null;
    if (reset) reset.addEventListener('click', (e) => { e.stopPropagation(); resetAll(); });
  }

  hud.addEventListener('click', (e) => {
    if (e.target === hudMute) return;
    panelOpen = !panelOpen;
    if (panelOpen) renderPanel();
    panel.classList.toggle('on', panelOpen);
  });
  document.addEventListener('click', (e) => {
    if (!panelOpen) return;
    const t = e.target as Node;
    if (!panel.contains(t) && !hud.contains(t)) { panelOpen = false; panel.classList.remove('on'); }
  });

  /* ---------- 全屏闪光 ---------- */
  const flash = document.createElement('div');
  flash.id = 'gfx-flash';
  document.body.appendChild(flash);
  const doFlash = (kind: 'crit' | 'levelup') => {
    flash.className = kind;
    void flash.offsetWidth; // 强制重排以重放动画
    flash.classList.add('on');
    setTimeout(() => flash.classList.remove('on'), 640);
    // 大厅同步扩散一圈冲击波：屏幕内外的反馈对上，打击感才完整
    if (App.arcade) App.arcade.pulse(kind);
  };

  /* ---------- 粒子爆发 ---------- */
  const burst = (host: HTMLElement, count: number, gold: boolean) => {
    const rect = host.getBoundingClientRect();
    if (!rect.width && !rect.height) return; // 隐藏 / 未布局时跳过
    const layer = document.createElement('div');
    layer.className = 'gfx-particles';
    layer.style.left = (rect.left + rect.width / 2) + 'px';
    layer.style.top = (rect.top + rect.height / 2) + 'px';
    for (let i = 0; i < count; i += 1) {
      const p = document.createElement('i');
      const ang = (Math.PI * 2 * i) / count + Math.random() * 0.5;
      const dist = 34 + Math.random() * 66;
      p.style.setProperty('--dx', (Math.cos(ang) * dist) + 'px');
      p.style.setProperty('--dy', (Math.sin(ang) * dist) + 'px');
      p.style.setProperty('--d', (0.45 + Math.random() * 0.4) + 's');
      if (!gold && Math.random() > 0.5) p.style.background = '#00e5ff';
      layer.appendChild(p);
    }
    document.body.appendChild(layer);
    setTimeout(() => layer.remove(), 950);
  };

  /* ---------- 浮动文字（连击 / 暴击 / 评级） ---------- */
  const floatText = (host: HTMLElement, text: string, cls: string) => {
    const rect = host.getBoundingClientRect();
    if (!rect.width && !rect.height) return;
    const el = document.createElement('div');
    el.className = 'gfx-float ' + cls;
    el.textContent = text;
    el.style.left = Math.max(8, rect.right - 70) + 'px';
    el.style.top = (rect.top + 2) + 'px';
    document.body.appendChild(el);
    setTimeout(() => el.remove(), 1100);
  };

  /* ---------- 成就弹窗（队列串行，避免叠在一起） ---------- */
  const achQueue: Achievement[] = [];
  let achShowing = false;
  const showAch = () => {
    if (achShowing || achQueue.length === 0) return;
    const a = achQueue.shift()!;
    achShowing = true;
    sfx.achievement();
    const el = document.createElement('div');
    el.className = 'gfx-ach-toast';
    el.innerHTML = '<span class="gfx-ach-toast-ic">' + a.icon + '</span>' +
      '<div class="gfx-ach-toast-txt"><b>成就解锁 · ' + App.escapeHtml(a.name) + '</b><i>' + App.escapeHtml(a.desc) + '</i></div>';
    document.body.appendChild(el);
    void el.offsetWidth;
    el.classList.add('on');
    setTimeout(() => {
      el.classList.remove('on');
      setTimeout(() => { el.remove(); achShowing = false; showAch(); }, 400);
    }, 2400);
  };

  const checkAchievements = () => {
    const stats: GameStats = {
      totalTools: saved.totalTools,
      combo,
      maxCombo: saved.maxCombo,
      crits: saved.crits,
      sGrades: saved.sGrades,
      toolsThisRound: turnBlocks.length,
      level: levelOf(saved.xp),
    };
    const fresh = newlyUnlocked(stats, saved.achievements);
    if (fresh.length === 0) return;
    for (const a of fresh) {
      saved.achievements.push(a.id);
      achQueue.push(a);
    }
    save();
    showAch();
    if (panelOpen) renderPanel();
  };

  /* ---------- 升级 ---------- */
  const gainXp = (n: number) => {
    saved.xp += n;
    const lv = levelOf(saved.xp);
    if (lv > lastLevel) {
      lastLevel = lv;
      sfx.levelUp();
      doFlash('levelup');
      App.showToast('🎉 升级！Lv.' + lv);
      hud.classList.add('levelup');
      setTimeout(() => hud.classList.remove('levelup'), 1200);
    }
    save();
    renderHud();
  };

  /* ---------- 自动折叠：一轮工具块太多时收起前面的 ---------- */
  const refold = () => {
    const host = App._turnMsgEl;
    if (!host) return;
    const old = host.querySelector('.gfx-fold-bar');
    if (old) old.remove();
    for (const b of turnBlocks) b.classList.remove('gfx-hidden');
    const plan = foldPlan(turnBlocks.length);
    if (!plan.folded) {
      host.classList.remove('gfx-open');
      return;
    }
    const keepFrom = turnBlocks.length - FOLD_VISIBLE; // 只留最后 6 个可见
    for (let i = 0; i < keepFrom; i += 1) turnBlocks[i].classList.add('gfx-hidden');
    const cost = turnBlocks.reduce((s, b) => s + Number(b.dataset.costMs || 0), 0) / 1000;
    const bar = document.createElement('div');
    bar.className = 'gfx-fold-bar';
    const label = (open: boolean) =>
      (open ? '▾ 已展开 ' : '▸ 已折叠 ') + plan.hidden + ' 个工具 · 本轮 ' + turnBlocks.length + ' 个 · 累计 ' + cost.toFixed(1) + 's';
    bar.textContent = label(false);
    bar.addEventListener('click', () => {
      const open = !host.classList.contains('gfx-open');
      host.classList.toggle('gfx-open', open);
      bar.textContent = label(open);
    });
    turnBlocks[keepFrom].before(bar);
  };

  /* ---------- 本轮战绩结算 ---------- */
  const settleRound = (immediate = false) => {
    if (settleTimer) { clearTimeout(settleTimer); settleTimer = null; }
    if (!immediate) { settleTimer = setTimeout(() => settleRound(true), 2400); return; }
    const host = App._turnMsgEl;
    if (!host || roundStat.tools < 2) return; // 单个工具不值得小结，避免刷屏
    if (host.querySelector('.gfx-round')) return; // 一轮只结算一次
    const el = document.createElement('div');
    el.className = 'gfx-round';
    el.innerHTML = roundSummary(roundStat);
    host.appendChild(el);
    App.scrollToBottom();
  };

  /** 新一轮回复开始：连击归零、折叠与战绩重置 */
  const startRound = () => {
    combo = 0;
    turnBlocks = [];
    roundStat = emptyRound();
    if (settleTimer) { clearTimeout(settleTimer); settleTimer = null; }
    renderHud();
  };

  /* ---------- 对外接口：由 31_tool_chain 调用 ---------- */

  App.game = {
    /** 工具块插入后调用：纳入本轮折叠统计 */
    onToolStart(el: HTMLElement) {
      if (!el) return;
      turnBlocks.push(el);
      refold();
    },

    /** 工具执行完成：评级 / 连击 / 暴击 / 经验 / 成就 */
    onToolResult(el: HTMLElement, success: boolean, costMs: number) {
      if (!el) return;
      el.dataset.costMs = String(Math.max(0, costMs));
      refold();

      if (!success) {
        combo = 0;
        roundStat.failed += 1;
        sfx.fail();
        floatText(el, 'MISS', 'miss');
        renderHud();
        checkAchievements();
        settleRound();
        return;
      }

      const grade = gradeOf(costMs);
      combo += 1;
      saved.totalTools += 1;
      saved.maxCombo = Math.max(saved.maxCombo, combo);
      if (grade === 'S') saved.sGrades += 1;

      // 暴击：连击越高越容易触发
      const crit = Math.random() < critChance(combo);
      if (crit) {
        saved.crits += 1;
        el.classList.add('gfx-crit');
        doFlash('crit');
        burst(el, 18, true);
        floatText(el, '💥 暴击 x2', 'crit');
        sfx.crit();
      } else {
        burst(el, 8, false);
      }

      // 评级徽章（插在状态文字前）
      const badge = document.createElement('span');
      badge.className = 'gfx-grade g-' + grade;
      badge.textContent = grade;
      badge.title = '耗时 ' + (costMs / 1000).toFixed(2) + 's';
      const head = el.querySelector('.tool-inline-head');
      const state = el.querySelector('.tool-inline-state');
      if (head) head.insertBefore(badge, state);
      if (combo >= 2) floatText(el, 'COMBO x' + combo, 'combo');
      sfx.grade(grade);
      if (!crit) sfx.combo(combo);

      const got = xpFor(grade, crit);
      gainXp(got);
      renderHud();
      checkAchievements();

      roundStat.tools += 1;
      roundStat.xp += got;
      roundStat.maxCombo = Math.max(roundStat.maxCombo, combo);
      roundStat.grades[grade] += 1;
      if (crit) roundStat.crits += 1;
      settleRound();
    },

    /** 新一轮回复开始（thinking） */
    onRoundStart: startRound,

    /** 整轮结束（audio_end / 打断）：立刻结算，不必再等静默 */
    onRoundEnd() {
      settleRound(true);
    },

    /** 状态快照（调试 / 外部读取） */
    snapshot() {
      return { ...saved, combo, roundTools: turnBlocks.length, level: levelOf(saved.xp) };
    },

    reset: resetAll,
  };

  App.gameMuted = false;
  renderHud();
}
