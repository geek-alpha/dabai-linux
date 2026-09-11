import type { AppKernel } from '../types/app-kernel.js';

/* ============================================================
 *  全息舞台（41_holo_stage）
 *
 *  【这一层补什么】
 *  35 街机氛围负责常驻底噪、38 直播舱负责掉落与热度、39 施法态负责等待期。
 *  缺的是两件最基础的东西：
 *    · 空间 —— 角色像贴在一张纸上：没有光源、没有体积、没有「被投射出来」的证据
 *    · 确认 —— 成果发生了，反馈却只有角落里一个小徽章。努力没有被看见
 *  本层补的正是这两样：全息投影（空间）+ 舞台灯光（氛围）+ 欢呼引擎（确认）。
 *
 *  【欢呼引擎的第一性原理】
 *  成就感不来自画面华丽，来自「我的产出被立刻、明确地确认了」。
 *  开心消消乐的全部秘密只有一句话：**每一次消除都有反馈，且强度与成果大小成正比**。
 *  所以这里不是「多加几个动画」，而是一条统一的分级通道 —— 纯函数 cheerLevel()
 *  决定给多少，而「给不给」永远是给：
 *      任何成功         → 1 级：微光 + 一声清脆
 *      三连击 / S 评级   → 2~3 级：上行音阶 + 评级大字 + 粒子
 *      暴击             → 4 级：闪光 + 震动 + 冲击波 + 频闪 + 低频冲击
 *      升级 / 传说掉落   → 5 级：全屏庆祝 + 和弦 + 彩带 + 全场灯亮
 *  失败不欢呼（那是廉价的），但也不沉默：走「鼓励」分支 —— 下行柔音 + 一次呼吸光。
 *
 *  【为什么音高要跟着连击走】
 *  听觉对「递增」比视觉敏感得多。连击越高音越亮，不用盯着屏幕就知道自己正在连。
 *
 *  【三条自律】
 *  · 常驻动画全部是 CSS transform / opacity（走合成层），JS 不参与每帧
 *  · 整层 pointer-events:none；prefers-reduced-motion 时只留静态光、不放动画
 *  · 频闪最多 3 次且只在 4 级以上（光敏安全）；页面隐藏即停摆
 *
 *  【第四条：视觉预算】
 *  性能探针（tools/fx_perf_probe.py）量出来的账：全屏 + mix-blend-mode + 动画
 *  是最贵的一类 —— 每帧都要重新合成整个视口。所以本层守三条硬线：
 *    · 全屏元素不带动画（染色 / 色差 / 噪点全部静态，质地不靠"动"）
 *    · 常驻动画的周期一律 ≥ 4s（快 = 抢注意力 + 贵）
 *    · 跑不动就自己降档（掉帧看门狗 → html.fx-lite），不指望用户手动关
 *
 *  【接入方式】
 *  装饰 App.game.onToolResult（每次成果）与 App.arcade.pulse（暴击/升级的既有出口），
 *  零侵入拿到「成败 / 连击 / 暴击 / 升级」，不改 34_game_fx 一行。
 * ============================================================ */

/* ============================================================
 *  一、纯逻辑（无 DOM，可单测）
 * ============================================================ */

export interface CheerInput {
  success: boolean;
  combo?: number;
  crit?: boolean;
  levelUp?: boolean;
  rarity?: 'rare' | 'epic' | 'legend' | null;
  grade?: 'S' | 'A' | 'B' | 'C';
}

export const CHEER_MAX = 5;

/**
 * 欢呼强度 1~5（0 = 失败，走鼓励分支）。
 * 取「最高档」而不是「累加」：累加会让一次暴击叠到 8 级，
 * 而 5 级已经用满全屏资源，再高只是噪音，反而稀释了稀有感。
 */
export function cheerLevel(i: CheerInput): number {
  if (!i || !i.success) return 0;
  let lv = 1; // 任何成功，至少一声 —— 这是「不会有什么都没发生」的保证
  const combo = Number.isFinite(i.combo) ? Math.max(1, Math.floor(i.combo as number)) : 1;
  if (combo >= 8) lv = Math.max(lv, 3);
  else if (combo >= 3) lv = Math.max(lv, 2);
  if (i.grade === 'S') lv = Math.max(lv, 3);
  if (i.rarity === 'epic') lv = Math.max(lv, 3);
  if (i.crit) lv = Math.max(lv, 4);
  if (i.rarity === 'legend' || i.levelUp) lv = Math.max(lv, 5);
  return Math.min(CHEER_MAX, lv);
}

/** 评级文案：机台式「即时评价」，一眼就知道这一下有多好。索引 = 级数 */
export const CHEER_TIERS: { en: string; cn: string }[] = [
  { en: '', cn: '' },
  { en: 'NICE', cn: '稳' },
  { en: 'GOOD', cn: '漂亮' },
  { en: 'GREAT', cn: '干得漂亮' },
  { en: 'AMAZING', cn: '不可思议' },
  { en: 'UNBELIEVABLE', cn: '全场欢呼' },
];

/** 基础音阶（C 大调五声：明亮但不刺耳）；级数越高音越多 */
const CHEER_SCALE: number[][] = [
  [],
  [880.0],
  [880.0, 1174.66],
  [880.0, 1108.73, 1318.51],
  [880.0, 1108.73, 1318.51, 1760.0],
  [880.0, 1108.73, 1318.51, 1760.0, 2093.0],
];

/**
 * 欢呼音符：级数决定音数，连击决定整体升调。
 * 升调上限压在 +45% 以内（约 8 个半音），再高就尖了 —— 手感不能以难受为代价。
 */
export function cheerNotes(level: number, combo = 1): number[] {
  const lv = Math.max(0, Math.min(CHEER_MAX, Math.floor(level) || 0));
  const base = CHEER_SCALE[lv];
  if (!base || base.length === 0) return [];
  const c = Number.isFinite(combo) ? Math.max(1, Math.floor(combo)) : 1;
  const shift = Math.pow(2, Math.min(c - 1, 8) * 0.047);
  return base.map((f) => Math.round(f * shift * 100) / 100);
}

/** 彩带数量：5 级庆祝用。34 条是「满屏但不糊」的平衡点 */
export const CONFETTI_N = 34;

/* ---------- 掉帧看门狗（纯逻辑，可单测） ----------
 * 为什么需要它：用户机器千差万别，让「卡」的人自己去关效果是不负责的。
 * 但看门狗本身也不能乱动 —— 一次 GC、一次模型加载都会造成单帧尖刺，
 * 拿尖刺去降档等于把流畅的机器也降了。所以判据用 **p95**（稳态）而不是 max。
 */
export const SLOW_MS = 22;    // p95 超过它 ≈ 低于 45fps，视为掉帧
// 注意：60Hz 屏的真实帧间隔是 16.7ms，阈值必须**高于**它，
// 否则「满 60fps」永远判不出来，降档就再也升不回去。
export const FAST_MS = 17.5;  // p95 低于它 ≈ 满 60fps（含正常抖动）
const SPIKE_MS = 250;         // 单帧超过它 = 瞬时尖刺（切标签页/重任务），整窗丢弃

export interface FrameStats {
  /** 参与统计的帧数 */
  n: number;
  avg: number;
  p95: number;
  /** 超过 20ms 的帧占比（%） */
  jankPct: number;
  /** 本窗是否含瞬时尖刺，含则不可用于决策 */
  spike: boolean;
}

/** 把一串帧间隔压成可决策的统计量 */
export function frameStats(deltas: number[]): FrameStats {
  const d = (deltas || []).filter((x) => Number.isFinite(x) && x > 0);
  if (d.length === 0) return { n: 0, avg: 0, p95: 0, jankPct: 0, spike: false };
  const sorted = d.slice().sort((a, b) => a - b);
  const at = (q: number) => sorted[Math.min(sorted.length - 1, Math.floor(sorted.length * q))];
  return {
    n: d.length,
    avg: d.reduce((a, b) => a + b, 0) / d.length,
    p95: at(0.95),
    jankPct: (d.filter((x) => x > 20).length / d.length) * 100,
    spike: sorted[sorted.length - 1] > SPIKE_MS,
  };
}

export interface LiteStreak { slow: number; fast: number }

/**
 * 降档决策：慢要连续 2 窗才降（不因偶发掉帧误伤），
 * 快则要连续 4 窗才升回（升回来又要抖一次，代价比多等几秒高）。
 * 迟滞（hysteresis）是必需的 —— 在阈值附近来回切换比一直卡更难受。
 */
export function nextLiteMode(
  current: boolean,
  stats: FrameStats,
  streak: LiteStreak,
): { lite: boolean; streak: LiteStreak } {
  if (!stats || stats.n < 20 || stats.spike) return { lite: current, streak };
  if (stats.p95 > SLOW_MS) {
    const slow = streak.slow + 1;
    const next = { slow, fast: 0 };
    if (!current && slow >= 2) return { lite: true, streak: { slow: 0, fast: 0 } };
    return { lite: current, streak: next };
  }
  if (stats.p95 < FAST_MS) {
    const fast = streak.fast + 1;
    const next = { slow: 0, fast };
    if (current && fast >= 4) return { lite: false, streak: { slow: 0, fast: 0 } };
    return { lite: current, streak: next };
  }
  return { lite: current, streak: { slow: 0, fast: 0 } }; // 中间地带：不动
}


/* ============================================================
 *  二、DOM / 音频（init 内）
 * ============================================================ */

export default function init(App: AppKernel) {
  const stage = document.getElementById('stage');
  if (!stage) return;

  // 热重载 / 重复 init：清掉旧层，否则会叠出两套光
  for (const id of ['holo-fx', 'light-fx', 'holo-cheer', 'holo-confetti']) {
    const old = document.getElementById(id);
    if (old) old.remove();
  }

  const reduce =
    typeof window.matchMedia === 'function' &&
    window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  /* ---------- 全息投影层：让角色看起来是「被投射出来的」 ----------
   * 全息投影的视觉证据只有四条，全部来自物理直觉：
   *   1. 光源在下方 → 底座 + 向上的光锥（不是从天上照下来）
   *   2. 有体积    → 光锥内的空气散射（blur + screen 混合）
   *   3. 在重建    → 逐行扫描带上下扫
   *   4. 信号不稳  → 极轻的色差与噪点，偶尔抖一下
   */
  const holo = document.createElement('div');
  holo.id = 'holo-fx';
  holo.setAttribute('aria-hidden', 'true');
  holo.dataset.state = 'idle';
  holo.innerHTML = [
    '<div class="hf-cone"></div>',       // 光锥：投影仪往上打
    '<div class="hf-base"></div>',       // 投影底座：椭圆光环
    '<div class="hf-base-spin"></div>',  // 底座刻度环（反向旋转 = 机械在转）
    '<div class="hf-scan"></div>',       // 逐行扫描：信号在重建
    '<div class="hf-aber"></div>',       // 色差：信号不稳定
    '<div class="hf-grain"></div>',      // 全息噪点
    '<div class="hf-frame"><i></i><i></i><i></i><i></i></div>', // 四角框标
    '<div class="hf-ticks"></div>',      // 边缘刻度：被观测 / 被分析
  ].join('');

  /* ---------- 舞台灯光层：空间一直是活的 ----------
   * 追光（顶部锥形）+ 双侧斜射（底部往上）+ 染色 + 频闪。
   * 为什么必须有「侧射」：只有顶光画面是平的；两侧斜射制造纵深，
   * 角色才会从背景里「站」出来 —— 这是舞台摄影的基本功，不是装饰。
   */
  const light = document.createElement('div');
  light.id = 'light-fx';
  light.setAttribute('aria-hidden', 'true');
  light.dataset.state = 'idle';
  light.innerHTML = [
    '<div class="lf-wash"></div>',       // 氛围染色（随状态换色）
    '<div class="lf-spot"></div>',       // 顶部追光
    '<div class="lf-sweep lf-sweep-l"></div>',
    '<div class="lf-sweep lf-sweep-r"></div>',
    '<div class="lf-strobe"></div>',     // 频闪（4 级以上，最多 3 次）
    '<div class="lf-rim"></div>',        // 轮廓边缘光
  ].join('');

  // 挂在氛围层之后：同层不设 z-index，靠 DOM 顺序 —— 施法字幕（39）仍在其上
  const anchor = document.getElementById('arcade-fx');
  if (anchor && anchor.parentElement === stage) {
    anchor.insertAdjacentElement('afterend', light);
    light.insertAdjacentElement('afterend', holo);
  } else {
    stage.appendChild(light);
    stage.appendChild(holo);
  }

  /* ---------- 状态联动：复用 #status-badge 这个唯一状态出口 ----------
   * 与 35_arcade_fx 同源同色，两层不会各说各话。
   */
  const badge = document.getElementById('status-badge');
  const stateOf = (el: HTMLElement | null): string => {
    const c = el ? el.className : '';
    if (c.includes('thinking')) return 'thinking';
    if (c.includes('listening')) return 'listening';
    if (c.includes('speaking')) return 'speaking';
    if (c.includes('active')) return 'idle';
    return 'offline';
  };
  const applyState = () => {
    const st = stateOf(badge);
    if (holo.dataset.state !== st) holo.dataset.state = st;
    if (light.dataset.state !== st) light.dataset.state = st;
  };
  applyState();
  if (badge && typeof MutationObserver !== 'undefined') {
    new MutationObserver(applyState).observe(badge, { attributes: true, attributeFilter: ['class'] });
  }

  /* ---------- 音效：WebAudio 实时合成，零资源文件（与 34/38/39 同款） ---------- */
  let audio: AudioContext | null = null;
  const tone = (freq: number, dur = 0.1, type: OscillatorType = 'sine', vol = 0.045, delay = 0) => {
    if (App.gameMuted) return;
    try {
      if (!audio) {
        const Ctor = window.AudioContext || (window as any).webkitAudioContext;
        if (!Ctor) return;
        audio = new Ctor();
      }
      if (audio.state === 'suspended') void audio.resume();
      const t0 = audio.currentTime + delay;
      const osc = audio.createOscillator();
      const gain = audio.createGain();
      osc.type = type;
      osc.frequency.setValueAtTime(freq, t0);
      gain.gain.setValueAtTime(0, t0);
      gain.gain.linearRampToValueAtTime(vol, t0 + 0.008);
      gain.gain.exponentialRampToValueAtTime(0.0001, t0 + dur);
      osc.connect(gain).connect(audio.destination);
      osc.start(t0);
      osc.stop(t0 + dur + 0.02);
    } catch { /* 音频不可用不影响主流程 */ }
  };

  /** 分级欢呼音：音数随级数、音高随连击；4 级以上叠低频冲击给「重量」 */
  const cheerSound = (level: number, combo: number) => {
    const notes = cheerNotes(level, combo);
    notes.forEach((f, i) => {
      tone(f, 0.12 + level * 0.025, i === 0 ? 'triangle' : 'sine', 0.042, i * 0.055);
    });
    if (level >= 4) {
      tone(98, 0.34, 'sawtooth', 0.05);        // 低频：胸口那一下
      tone(196, 0.24, 'sine', 0.038, 0.02);
    }
    if (level >= 5) {
      // 「欢呼」是高频闪烁 + 一层余韵，不是更大声
      [1568, 1760, 2093].forEach((f, i) => tone(f, 0.3, 'sine', 0.028, 0.32 + i * 0.07));
    }
  };

  /** 失败：下行两音 + 极轻的呼吸光。不刺耳、不惩罚 —— 是「再来一次」的语气 */
  const consoleSound = () => {
    tone(392, 0.12, 'sine', 0.03);
    tone(294, 0.16, 'sine', 0.026, 0.09);
  };

  /* ---------- 评级大字层 ---------- */
  const cheerHost = document.createElement('div');
  cheerHost.id = 'holo-cheer';
  cheerHost.setAttribute('aria-hidden', 'true');
  document.body.appendChild(cheerHost);

  /** 评级大字：中央偏上弹出。放在偏上是为了不和 39 的中央施法字幕打架 */
  const showTier = (level: number) => {
    const t = CHEER_TIERS[level];
    if (!t || !t.en) return;
    const el = document.createElement('div');
    el.className = 'hc-tier lv' + level;
    el.innerHTML = '<b>' + t.en + '</b><i>' + t.cn + '</i>';
    cheerHost.appendChild(el);
    void el.offsetWidth; // 强制回流：否则连续两次同一个类不会重放动画
    el.classList.add('on');
    const life = level >= 5 ? 1800 : level >= 4 ? 1500 : 1100;
    setTimeout(() => { el.classList.remove('on'); setTimeout(() => el.remove(), 500); }, life);
  };

  /* ---------- 彩带（5 级专用） ---------- */
  const confettiHost = document.createElement('div');
  confettiHost.id = 'holo-confetti';
  confettiHost.setAttribute('aria-hidden', 'true');
  document.body.appendChild(confettiHost);

  const confetti = () => {
    if (reduce) return;
    for (let i = 0; i < CONFETTI_N; i += 1) {
      const p = document.createElement('i');
      p.style.setProperty('--x', (Math.random() * 100).toFixed(2) + '%');
      p.style.setProperty('--dx', ((Math.random() - 0.5) * 220).toFixed(0) + 'px');
      p.style.setProperty('--d', (1.6 + Math.random() * 1.4).toFixed(2) + 's');
      p.style.setProperty('--delay', (Math.random() * 0.35).toFixed(2) + 's');
      p.style.setProperty('--rot', (Math.random() * 720 - 360).toFixed(0) + 'deg');
      p.style.background = ['#ffd54f', '#4ade80', '#00e5ff', '#ff6b9d', '#c084fc'][i % 5];
      confettiHost.appendChild(p);
      setTimeout(() => p.remove(), 3400);
    }
  };

  /* ---------- 屏幕震动：抖 stage 而不是 body ----------
   * 抖 body 会让 fixed 定位的输入栏一起晃、还可能触发滚动条抖动；
   * 抖 stage 只影响 3D 舞台那一块，观感反而更准（是「场景」在震）。
   */
  const SHAKES = ['holo-shake-s', 'holo-shake-m', 'holo-shake-l'];
  const shake = (power = 1) => {
    if (reduce) return;
    const cls = SHAKES[Math.max(0, Math.min(2, power - 1))] as string;
    stage.classList.remove(...SHAKES);
    void stage.offsetWidth;
    stage.classList.add(cls);
    setTimeout(() => stage.classList.remove(cls), 620);
  };

  /* ---------- 频闪：最多 3 次（光敏安全），只给 4 级以上 ---------- */
  const strobeEl = light.querySelector('.lf-strobe') as HTMLElement | null;
  const strobe = () => {
    if (reduce || !strobeEl) return;
    strobeEl.classList.remove('on');
    void strobeEl.offsetWidth;
    strobeEl.classList.add('on');
    setTimeout(() => strobeEl.classList.remove('on'), 460);
  };

  /* ---------- 冲击波：从全息底座扩散（空间原点，不是屏幕中心） ---------- */
  const shockEl = holo.querySelector('.hf-base') as HTMLElement | null;

  /* ---------- 欢呼主入口 ---------- */
  let cheerCount = 0;

  const celebrate = (level: number, opts?: { combo?: number }) => {
    const lv = Math.max(0, Math.min(CHEER_MAX, Math.floor(level) || 0));
    if (lv <= 0) return;
    cheerCount += 1;
    const combo = opts && Number.isFinite(opts.combo) ? (opts.combo as number) : 1;

    // 全息层：整层「亮一下」——级别越高越亮越久。
    // reduced-motion 下跳过：亮度骤变也是刺激源，这类用户的反馈走评级大字与音效。
    if (!reduce) {
      holo.dataset.cheer = String(lv);
      light.dataset.cheer = String(lv);
      const ttl = 220 + lv * 160;
      setTimeout(() => {
        // 只清理「自己设置的那一级」：期间若又来了更高的一级，不要把它盖掉
        if (holo.dataset.cheer === String(lv)) {
          delete holo.dataset.cheer;
          delete light.dataset.cheer;
        }
      }, ttl);
    }

    cheerSound(lv, combo);
    if (lv >= 3) showTier(lv);
    if (lv >= 4) {
      strobe();
      shake(lv >= 5 ? 3 : 2);
    }
    // 投影底座应一下：**每一级都有**（5 级是炸开，其余是脉冲）。
    // 这一下是「每个成果都值得欢呼」最直接的证据 —— 投影仪在回应你，
    // 而不是只有大成果才有反应。
    if (shockEl) {
      shockEl.classList.remove('pulse', 'boom');
      void shockEl.offsetWidth;
      shockEl.classList.add(lv >= 5 ? 'boom' : 'pulse');
      const ttl = lv >= 5 ? 1000 : 520;
      setTimeout(() => shockEl.classList.remove('pulse', 'boom'), ttl);
    }
    if (lv >= 5) confetti();
  };

  /** 失败 / 落空：不欢呼，但给一次「被看见」的呼吸 */
  const encourage = () => {
    if (reduce) return;
    holo.classList.remove('hf-dim');
    void holo.offsetWidth;
    holo.classList.add('hf-dim');
    setTimeout(() => holo.classList.remove('hf-dim'), 700);
    consoleSound();
  };

  /** 按输入自动分级并欢呼，返回实际级数（0 = 走了鼓励分支） */
  const cheer = (input: CheerInput): number => {
    const lv = cheerLevel(input);
    if (lv <= 0) encourage();
    else celebrate(lv, { combo: input.combo });
    return lv;
  };

  /* ---------- 接入一：每次工具成果（装饰 34 的钩子，不改它一行） ---------- */
  if (App.game && typeof App.game.onToolResult === 'function') {
    const orig = App.game.onToolResult;
    App.game.onToolResult = function patched(el: HTMLElement, success: boolean, costMs: number) {
      orig.call(this, el, success, costMs);
      try {
        const snap = App.game && App.game.snapshot ? App.game.snapshot() : null;
        const combo = snap && Number.isFinite(snap.combo) ? Number(snap.combo) : 1;
        cheer({ success, combo, grade: gradeOf(costMs) });
      } catch { /* 欢呼层出错绝不能拖垮工具链 */ }
    };
  }

  /* ---------- 接入二：暴击 / 升级 ----------
   * 34_game_fx 在这两件事上本来就会调 App.arcade.pulse —— 那是既有出口，
   * 装饰它就能零侵入拿到「暴击」「升级」，比去改 34 内部干净得多。
   */
  if (App.arcade && typeof App.arcade.pulse === 'function') {
    const origPulse = App.arcade.pulse;
    App.arcade.pulse = function patched(kind?: 'crit' | 'levelup' | 'tool') {
      origPulse.call(this, kind);
      try {
        if (kind === 'crit') celebrate(4, { combo: 1 });
        else if (kind === 'levelup') celebrate(5, { combo: 1 });
      } catch { /* 同上 */ }
    };
  }

  /* ---------- 页面隐藏：停掉整层的动画（不留后台空转） ---------- */
  const setPaused = (on: boolean) => {
    holo.classList.toggle('hf-paused', on);
    light.classList.toggle('lf-paused', on);
  };
  document.addEventListener('visibilitychange', () => setPaused(document.hidden));

  /* ---------- 掉帧看门狗：测到持续掉帧就自动降档 ----------
   * 原则：保住「每帧跑满」比保住「每个动效都在」重要。降的是环境氛围，
   * 成果反馈（评级大字 / 彩带 / 底座脉冲 / 音效）一律不动。
   * 采样本身也要便宜：稳定下来后就不连续采样了，改成每 20s 探 1s。
   */
  const html = document.documentElement;
  let lite = html.classList.contains('fx-lite');
  let streak: LiteStreak = { slow: 0, fast: 0 };
  let lastStats: FrameStats = frameStats([]);
  let windows = 0;

  const applyLite = (on: boolean) => {
    if (on === lite) return;
    lite = on;
    html.classList.toggle('fx-lite', on);
  };

  const IDLE_SAMPLE_MS = 20000;   // 稳定后：每 20s 采 1s
  const WARMUP_WINDOWS = 10;      // 前 10 窗连续采：刚加载完最需要盯着
  let sampling = false;
  let deltas: number[] = [];
  let lastFrame = 0;
  let windowStart = 0;
  let idleTimer: ReturnType<typeof setTimeout> | null = null;

  const tick = (now: number) => {
    if (!sampling) return;
    const dt = now - lastFrame;
    lastFrame = now;
    deltas.push(dt);
    if (now - windowStart >= 1000) {
      const stats = frameStats(deltas);
      lastStats = stats;
      deltas = [];
      windowStart = now;
      windows += 1;
      const r = nextLiteMode(lite, stats, streak);
      streak = r.streak;
      applyLite(r.lite);
      // 已经稳定且不需要盯着了 → 退到低频探测，别让看门狗自己变成负担
      if (!lite && windows > WARMUP_WINDOWS) {
        sampling = false;
        if (idleTimer) clearTimeout(idleTimer);
        idleTimer = setTimeout(startSampling, IDLE_SAMPLE_MS);
        return;
      }
    }
    requestAnimationFrame(tick);
  };

  function startSampling() {
    if (sampling || document.hidden || App.chatQuiet) return;
    sampling = true;
    deltas = [];
    lastFrame = performance.now();
    windowStart = lastFrame;
    requestAnimationFrame(tick);
  }

  startSampling();
  // 聊天全屏：看门狗采样本身就是每帧 rAF —— 全屏时全息层看不见，采样一起停
  App.onQuiet((on) => { if (on) sampling = false; else startSampling(); });
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) sampling = false;
    else startSampling();
  });

  /* ---------- 对外接口 ---------- */
  App.holo = {
    root: holo,
    light,
    /** 手动欢呼（调试 / 单测用） */
    celebrate,
    /** 按输入自动分级（工具结果走这条） */
    cheer,
    /** 失败鼓励 */
    encourage,
    /** 屏幕震动 1~3 */
    shake,
    /** 频闪一次 */
    strobe,
    /** 彩带 */
    confetti,
    /** 纯函数暴露：测试用它，避免测试自己抄一份规则 */
    level: cheerLevel,
    notes: cheerNotes,
    tiers: CHEER_TIERS,
    /** 累计欢呼次数（快照用） */
    get count() { return cheerCount; },
    /** 是否处于低配降档（环境氛围已减负，成果反馈不变） */
    get lite() { return lite; },
    /** 手动切换低配（调试 / 测试用；平时由看门狗自动决定） */
    setLite(on: boolean) { applyLite(!!on); },
    /** 帧率快照：{ lite, 最近一窗统计 } */
    perf() { return { lite, stats: lastStats, windows }; },
    reset() { cheerCount = 0; delete holo.dataset.cheer; delete light.dataset.cheer; },
  };
}

/* 与 34_game_fx 的评级阈值保持一致：这里只用来喂给 cheerLevel，
 * 真正的评级徽章仍由 34 生成（不重复造一份 UI）。 */
function gradeOf(ms: number): 'S' | 'A' | 'B' | 'C' {
  if (!Number.isFinite(ms) || ms < 0) return 'C';
  if (ms <= 1500) return 'S';
  if (ms <= 5000) return 'A';
  if (ms <= 15000) return 'B';
  return 'C';
}
