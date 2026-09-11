import type { AppKernel } from '../types/app-kernel.js';

/* ============================================================
 *  直播舱（38_live_room）
 *
 *  【为什么是「直播间」】
 *  直播间的成瘾性不来自内容，来自四条心理机制。拆开看：
 *    1. 可变奖励 —— 礼物随机掉。不知道下一个是什么，才停不下来（老虎机原理）
 *    2. 损失厌恶 —— 热度会掉、连击会断。沉没成本逼你继续
 *    3. 社会在场 —— 有人看着你。热度数字 + 弹幕提供「被注视感」
 *    4. 稀缺保底 —— 纯随机会让人绝望；保底让「再来一次」永远有理由
 *  写代码本身枯燥，缺的正是这四条。本模块把它们移植过来。
 *
 *  【为什么不能只加特效】
 *  已有的 34_game_fx 是「确定性奖励」：成功就涨经验，稳定可预期。
 *  确定性的东西不上瘾——上瘾来自「这次会不会出」。所以本模块的核心
 *  不是更多动画，而是一台带保底的掷骰机。
 *
 *  【三条自律（不做成噪音）】
 *    · 掉率克制：基础 28%；硬保底 25 次必出史诗 / 120 次必出传说
 *    · 弹幕克制：4s 冷却 + 同屏上限 3，绝不刷屏
 *    · 不挡交互：全部 pointer-events:none；热度走 1s 定时器，不用每帧
 *
 *  【接入方式】
 *  装饰 App.game.onToolResult（全项目唯一的工具结果出口），零侵入拿到
 *  元素 / 成败 / 耗时。不修改 34_game_fx 一行。
 * ============================================================ */

/** 稀有度：普通不出货，不设档位 */
type Rarity = 'rare' | 'epic' | 'legend';

interface Gift { icon: string; name: string; }

/** 礼物池：越稀有越少、越夸张 */
const GIFTS: Record<Rarity, Gift[]> = {
  rare: [
    { icon: '💗', name: '小心心' },
    { icon: '🌶️', name: '辣条' },
    { icon: '⭐', name: '星星' },
    { icon: '🍬', name: '糖果' },
    { icon: '👍', name: '点赞' },
  ],
  epic: [
    { icon: '🚀', name: '火箭' },
    { icon: '👑', name: '皇冠' },
    { icon: '💎', name: '钻石' },
  ],
  legend: [
    { icon: '🎆', name: '嘉年华' },
    { icon: '🏆', name: '冠军杯' },
  ],
};

/** 弹幕池：按事件分类。语气是「陪着你写代码的人」，不是解说员 */
const DANMAKU: Record<string, string[]> = {
  success: ['稳了', '这波漂亮', '一行不多，一行不少', '干净利落', '这个我熟', '通了，心里也通了'],
  crit: ['这速度我自己都怕', '开挂了属于是', '快到重影'],
  fail: ['没事，重来一次', '这个坑我踩过', '不是你的问题，是环境'],
  legend: ['哇，出金了', '这运气，去买张彩票吧', '手气这么好？'],
  hot: ['热度上来了', '有人在看我们写代码', '感觉被围观了'],
};

/** 热度阶梯：突破时触发「上热门」事件——直播间最爽的瞬间之一 */
const HOT_LEVELS = [
  { v: 500, name: '小火', icon: '🔥' },
  { v: 2000, name: '上热门', icon: '🔥' },
  { v: 8000, name: '爆火', icon: '💥' },
  { v: 30000, name: '全站第一', icon: '👑' },
];

/** 掉率与保底参数：手感全在这里，调这一个对象就够 */
const DROP = {
  base: 0.28,       // 基础掉率
  epic: 0.05,       // 史诗绝对概率
  legend: 0.01,     // 传说绝对概率
  softPer: 0.055,   // 每空手一次提升的掉率
  softMax: 0.45,    // 软保底上限
  pityEpic: 25,     // 25 次没出史诗 → 强制史诗
  pityLegend: 120,  // 120 次没出传说 → 强制传说
};

/** 火花线格数：12 格是「看得出趋势」与「不挤占头部空间」的平衡点 */
const SPARK_N = 12;

export default function init(App: AppKernel) {
  const KEY = 'dabai_live_v1';

  interface Saved { best: number; gifts: number; legends: number; total: number; }
  const load = (): Saved => {
    const dflt: Saved = { best: 0, gifts: 0, legends: 0, total: 0 };
    try {
      const raw = localStorage.getItem(KEY);
      if (!raw) return dflt;
      return { ...dflt, ...JSON.parse(raw) };
    } catch {
      return dflt;
    }
  };
  const saved = load();
  const persist = () => {
    try { localStorage.setItem(KEY, JSON.stringify(saved)); } catch { /* 隐私模式：不值得报错 */ }
  };

  /* ---------- 音效：与 34_game_fx 同款 WebAudio 合成，零资源文件 ---------- */
  let audio: AudioContext | null = null;
  const tone = (freq: number, dur = 0.08, type: OscillatorType = 'sine', vol = 0.05, delay = 0) => {
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
  const sfx = {
    rare: () => { tone(880, 0.09, 'triangle', 0.04); tone(1320, 0.12, 'sine', 0.035, 0.05); },
    epic: () => [523, 784, 1047].forEach((f, i) => tone(f, 0.13, 'triangle', 0.05, i * 0.06)),
    legend: () => [523, 659, 784, 1047, 1319, 1568].forEach((f, i) => tone(f, 0.2, 'sine', 0.055, i * 0.08)),
    hot: () => { tone(196, 0.3, 'sawtooth', 0.05); tone(1568, 0.25, 'sine', 0.04, 0.06); },
    // 空手音：极轻、极短。不是「失败」，是「差一点」的张力
    near: () => tone(660, 0.05, 'sine', 0.016),
  };

  /* ---------- 热度 HUD：注入聊天面板头部（与 gfx-hud 并列，不额外占屏） ---------- */
  const hud = document.createElement('span');
  hud.className = 'lv-hot';
  hud.id = 'live-hot';
  hud.title = '算力热度 —— 你的代码在燃烧';
  let barsHtml = '';
  for (let i = 0; i < SPARK_N; i += 1) barsHtml += '<i></i>';
  hud.innerHTML =
    '<span class="lv-flame">🔥</span>' +
    '<b class="lv-num">0</b>' +
    '<span class="lv-spark">' + barsHtml + '</span>' +
    '<em class="lv-timer">00:00</em>';
  // 注意：头部那行是 class="chat-head-actions"，不是 id。只查 id 会落空、
  // 整块 HUD 被 append 到 body 末尾（视觉上跑到底部去），所以两级都查。
  const headActions = document.getElementById('chat-head-actions')
    || document.querySelector('.chat-head-actions');
  if (headActions && headActions.parentElement) headActions.parentElement.insertBefore(hud, headActions);
  else document.body.appendChild(hud);

  const elNum = hud.querySelector('.lv-num') as HTMLElement;
  const elTimer = hud.querySelector('.lv-timer') as HTMLElement;
  const elBars = Array.prototype.slice.call(hud.querySelectorAll('.lv-spark i')) as HTMLElement[];

  /* ---------- 弹幕层：飘在舞台上，天然继承 pointer-events:none ---------- */
  const danHost = document.getElementById('arcade-fx')
    || document.getElementById('stage')
    || document.body;
  let dmLayer = document.getElementById('live-danmaku');
  if (!dmLayer) {
    dmLayer = document.createElement('div');
    dmLayer.id = 'live-danmaku';
    dmLayer.setAttribute('aria-hidden', 'true');
    danHost.appendChild(dmLayer);
  }
  const dmQueue: string[] = [];
  let dmShowing = 0;
  let lastDmAt = 0;

  const popDanmaku = () => {
    const text = dmQueue.shift();
    if (text === undefined) return;
    dmShowing += 1;
    const el = document.createElement('div');
    el.className = 'lv-dm';
    el.textContent = text;
    el.style.top = (10 + Math.random() * 44) + '%';
    el.style.setProperty('--d', (5.2 + Math.random() * 1.6).toFixed(2) + 's');
    dmLayer!.appendChild(el);
    let closed = false;
    const done = () => {
      if (closed) return;
      closed = true;
      el.remove();
      dmShowing -= 1;
      if (dmQueue.length) popDanmaku();
    };
    el.addEventListener('animationend', done);
    setTimeout(done, 9000); // 兜底：动画被禁用时也能回收
  };

  const shootDanmaku = (kind: string) => {
    const pool = DANMAKU[kind];
    if (!pool) return;
    const now = performance.now();
    if (now - lastDmAt < 4000) return; // 冷却：绝不刷屏
    if (dmShowing >= 3) return;        // 同屏上限
    lastDmAt = now;
    dmQueue.push(pool[(Math.random() * pool.length) | 0]);
    if (dmQueue.length > 3) dmQueue.shift();
    if (dmShowing === 0) popDanmaku();
  };

  /* ---------- 可变奖励引擎：随机 + 保底 ---------- */
  const pity = { sinceDrop: 0, sinceEpic: 0, sinceLegend: 0 };

  const rollDrop = (): Rarity | null => {
    pity.sinceDrop += 1;
    pity.sinceEpic += 1;
    pity.sinceLegend += 1;

    // 硬保底优先：连续空手到阈值直接给，不让玩家绝望
    if (pity.sinceLegend >= DROP.pityLegend) {
      pity.sinceLegend = 0; pity.sinceEpic = 0; pity.sinceDrop = 0;
      return 'legend';
    }
    if (pity.sinceEpic >= DROP.pityEpic) {
      pity.sinceEpic = 0; pity.sinceDrop = 0;
      return 'epic';
    }

    const r = Math.random();
    if (r < DROP.legend) {
      pity.sinceLegend = 0; pity.sinceEpic = 0; pity.sinceDrop = 0;
      return 'legend';
    }
    if (r < DROP.legend + DROP.epic) {
      pity.sinceEpic = 0; pity.sinceDrop = 0;
      return 'epic';
    }
    // 软保底：空手越久越容易出货（抽卡手感的核心）
    const rate = Math.min(DROP.softMax, DROP.base + pity.sinceDrop * DROP.softPer);
    if (r < DROP.legend + DROP.epic + rate) {
      pity.sinceDrop = 0;
      return 'rare';
    }
    return null;
  };

  /* ---------- 掉落呈现：中央礼物舞台 ----------
   *
   * 【为什么从「工具块旁飘起」改成「屏幕中央弹出」】
   * 旧实现把礼物锚在工具块右边缘（rect.right - 24），那是屏幕最右侧的一条窄带。
   * 三个后果：
   *   1. 礼物出现在余光里 —— 眼睛正盯着角色或代码时，根本不会注意到
   *   2. 尺寸只有 20px 图标 + 12px 文字，跟「收到礼物」这件事的分量完全不符
   *   3. 同时掉两个就叠在同一像素上，后一个直接盖住前一个
   * 直播间里礼物是**主秀**：正中央、由小到大砸出来、连送时自动摞成一摞。
   *
   * 【两层结构，各管一件事】
   *   .lv-gift-slot  只管堆叠定位（translateY + 纵深缩放），带 transition ——
   *                  新礼物插进来时，旧礼物是被平滑地「顶上去」的
   *   .lv-gift       只管入场动画（scale 由小到大）
   * 为什么必须分开：入场动画的 transform 会覆盖堆叠用的 transform。
   * 挤在同一个元素上，要么动画吃掉定位、要么定位吃掉动画 —— 礼物会瞬移。
   */
  const STACK_MAX = 5;        // 同屏最多摞 5 个，超出的立刻收走
  const STACK_GAP = 88;       // 每层纵向间距（px）
  const STACK_SHRINK = 0.075; // 每深一层缩小 7.5%：制造纵深，像真的一摞

  const staleStage = document.getElementById('live-gifts');
  if (staleStage) staleStage.remove();   // 热重载：清掉旧舞台，否则礼物会叠两份
  const stage = document.createElement('div');
  stage.id = 'live-gifts';
  stage.setAttribute('aria-hidden', 'true');
  document.body.appendChild(stage);

  interface Slot { el: HTMLElement; rarity: Rarity; timer: number; }
  const live: Slot[] = [];

  /** 重排堆叠：最新的贴着屏幕中心（--y=0），越旧越往上、越小、越淡 */
  const relayout = () => {
    const n = live.length;
    // 自适应间距：矮屏（笔记本 / 手机横屏）上 5 个礼物按固定 88px 排会顶出屏幕外，
    // 所以整摞超过 300px 时自动压缩。越挤越紧，但绝不溢出可视区。
    const gap = Math.min(STACK_GAP, 300 / Math.max(1, n - 1));
    for (let i = 0; i < n; i += 1) {
      const depth = n - 1 - i;              // 0 = 最新
      const el = live[i].el;
      el.style.setProperty('--y', (-depth * gap).toFixed(1) + 'px');
      el.style.setProperty('--k', (1 - depth * STACK_SHRINK).toFixed(3));
      el.style.setProperty('--o', (1 - depth * 0.14).toFixed(3));
      el.style.zIndex = String(60 - depth);
    }
  };

  /** 正常到期：滑出 + 淡出，剩下的平滑落位 */
  const retire = (slot: Slot) => {
    const idx = live.indexOf(slot);
    if (idx < 0) return;
    live.splice(idx, 1);
    window.clearTimeout(slot.timer);
    slot.el.classList.add('out');
    setTimeout(() => slot.el.remove(), 600);
    relayout();
  };

  const showDrop = (_host: HTMLElement | null, rarity: Rarity) => {
    const pool = GIFTS[rarity];
    const gift = pool[(Math.random() * pool.length) | 0];

    const slot = document.createElement('div');
    slot.className = 'lv-gift-slot ' + rarity;
    const card = document.createElement('div');
    card.className = 'lv-gift ' + rarity;
    card.innerHTML = '<span class="lv-gift-ic">' + gift.icon + '</span>' +
      '<span class="lv-gift-tx"><b>' + App.escapeHtml(gift.name) + '</b>' +
      '<i>' + (rarity === 'legend' ? '传说' : rarity === 'epic' ? '史诗' : '稀有') + '</i></span>';
    slot.appendChild(card);
    stage.appendChild(slot);

    const s: Slot = { el: slot, rarity, timer: 0 };
    live.push(s);

    // 超上限：把最旧的立刻收走（不是等它自然到期），否则刷屏时会堆成一条长龙。
    // 先 shift 出数组再操作 DOM —— 若走 retire()，循环里会反复 relayout。
    while (live.length > STACK_MAX) {
      const old = live.shift() as Slot;
      window.clearTimeout(old.timer);
      old.el.classList.add('out');
      setTimeout(() => old.el.remove(), 600);
    }

    relayout();

    // 越稀有留得越久：传说有资格多占一会儿屏幕
    const life = rarity === 'legend' ? 3400 : rarity === 'epic' ? 2500 : 1900;
    s.timer = window.setTimeout(() => retire(s), life);

    if (rarity === 'legend') {
      const banner = document.createElement('div');
      banner.className = 'lv-banner legend';
      banner.innerHTML = '<b>' + gift.icon + ' ' + App.escapeHtml(gift.name) + '</b><i>传说掉落 · 恭喜</i>';
      document.body.appendChild(banner);
      void banner.offsetWidth;
      banner.classList.add('on');
      setTimeout(() => { banner.classList.remove('on'); setTimeout(() => banner.remove(), 500); }, 2000);
      if (App.arcade) App.arcade.pulse('levelup');
    } else if (rarity === 'epic' && App.arcade) {
      App.arcade.pulse('crit');
    }
  };

  /* ---------- 热度飘字 ----------
   * 热度涨了，但反馈只有 HUD 上一个小数字在变 —— 那等于没反馈。
   * 飘字让「+80」这个数字本身被看见，而且是**从礼物同一片区域升起**，
   * 于是「干活 → 收热度 → 掉礼物」在视觉上是同一件事的连续三段。
   */
  let gainLayer: HTMLElement | null = document.getElementById('live-gain');
  const showGain = (n: number, up: boolean) => {
    if (!gainLayer) {
      gainLayer = document.createElement('div');
      gainLayer.id = 'live-gain';
      gainLayer.setAttribute('aria-hidden', 'true');
      document.body.appendChild(gainLayer);
    }
    if (gainLayer.childElementCount >= 3) gainLayer.firstElementChild?.remove();
    const el = document.createElement('div');
    el.className = 'lv-gain ' + (up ? 'up' : 'down');
    // 横向随机散开：连击时三条飘字若完全重叠，看起来像只有一条
    el.style.setProperty('--x', ((Math.random() - 0.5) * 110).toFixed(0) + 'px');
    el.textContent = (up ? '+' : '−') + Math.round(Math.abs(n));
    gainLayer.appendChild(el);
    setTimeout(() => el.remove(), 1500);
  };

  /* ---------- 热度 ---------- */
  let heat = 0;
  let hotIdx = -1;          // 已触发到第几级，避免重复弹
  const samples: number[] = new Array(SPARK_N).fill(0);
  let sampleIdx = 0;

  /** 热度数字弹一下：让「涨了」这件事被看见。走 transform，合成层 */
  const bumpHeat = () => {
    elNum.classList.remove('bump');
    void elNum.offsetWidth;   // 强制回流：否则连续两次加同一个类不会重放动画
    elNum.classList.add('bump');
  };

  const addHeat = (n: number) => {
    heat = Math.max(0, Math.min(99999, heat + n));
    if (heat > saved.best) { saved.best = heat; persist(); }
    renderHud();   // 立即反映：等 1s 定时器会让人以为没反应
    // 数字弹一下。门槛 10 是为了把「工具完成 +80」和「每秒自然 +2」分开——
    // 后者每秒都来，若也弹，数字就会一直抖，反而看不出哪次是真的收获。
    if (Math.abs(n) >= 10) bumpHeat();
    // 逐级判定：只触发「刚跨过」的那一级
    for (let i = HOT_LEVELS.length - 1; i >= 0; i -= 1) {
      if (heat >= HOT_LEVELS[i].v && i > hotIdx) {
        hotIdx = i;
        const lv = HOT_LEVELS[i];
        sfx.hot();
        App.showToast(lv.icon + ' ' + lv.name + '！热度 ' + heat);
        const banner = document.createElement('div');
        banner.className = 'lv-banner hot';
        banner.innerHTML = '<b>' + lv.icon + ' ' + lv.name + '</b><i>热度突破 ' + lv.v + '</i>';
        document.body.appendChild(banner);
        void banner.offsetWidth;
        banner.classList.add('on');
        setTimeout(() => { banner.classList.remove('on'); setTimeout(() => banner.remove(), 500); }, 2000);
        shootDanmaku('hot');
        break;
      }
    }
  };

  const fmtHeat = (n: number): string => {
    if (n >= 10000) return (n / 10000).toFixed(1) + 'W';
    if (n >= 1000) return (n / 1000).toFixed(1) + 'K';
    return String(Math.round(n));
  };

  const renderHud = () => {
    elNum.textContent = fmtHeat(heat);
    elNum.classList.toggle('warm', heat >= 500);
    elNum.classList.toggle('blaze', heat >= 8000);
    // 本轮对话耗时：真相来源是 40_turn_clock。
    // 曾经的实现是从**页面加载**算起（Date.now() - startedAt），跟「一轮对话」
    // 毫无关系——页面开着不动它也在涨，所以看起来「不准」。
    const tc = App.turnClock;
    elTimer.textContent = tc ? tc.fmt() : '';
    elTimer.classList.toggle('running', !!(tc && tc.running));
  };

  // 时钟开始/结束时立刻刷新 HUD：否则「已结束」的样式要等下一个 1s tick 才出现，
  // 用户会看到数字明明停了、样式却还在「运行中」的一秒错位。
  if (App.turnClock) App.turnClock.onChange = () => renderHud();

  /** 火花线：16 根条 = 最近 16 个采样。走 scaleY，合成层 */
  const paintSpark = () => {
    samples[sampleIdx % samples.length] = heat;
    sampleIdx += 1;
    let max = 1;
    for (const v of samples) if (v > max) max = v;
    for (let i = 0; i < elBars.length; i += 1) {
      // 从旧到新排列，视觉上是「从左往右的历史」
      const v = samples[(sampleIdx + i) % samples.length];
      elBars[i].style.transform = 'scaleY(' + (0.08 + (v / max) * 0.92).toFixed(3) + ')';
    }
  };

  let tickTimer: ReturnType<typeof setInterval> | null = null;
  const tick = () => {
    // 【修正】旧逻辑：每秒固定 +2，衰减却是 heat*1.2% —— 热度 500 时每秒掉 6，
    // 也就是 AI 越拼命、热度反而越低，因果是反的。
    // 正确的因果：在干活就涨（越快涨越多），闲着才掉。
    const rate = App.stream ? App.stream.rate : 0;
    const casting = App.cast ? App.cast.active > 0 : false;
    if (rate > 0 || casting) {
      let gain = 2;
      if (rate > 0) gain += Math.min(rate, 60) * 0.5;  // 满速输出约 +30/秒
      if (casting) gain += 6;                          // 工具正在跑：额外加成
      addHeat(gain);
    } else if (heat > 0) {
      addHeat(-Math.max(1, heat * 0.02));
    }
    renderHud();
  };
  const startTick = () => { if (!tickTimer) tickTimer = setInterval(tick, 1000); };
  const stopTick = () => { if (tickTimer) { clearInterval(tickTimer); tickTimer = null; } };
  startTick();

  let sparkTimer: ReturnType<typeof setInterval> | null = null;
  const startSpark = () => { if (!sparkTimer) sparkTimer = setInterval(paintSpark, 800); };
  const stopSpark = () => { if (sparkTimer) { clearInterval(sparkTimer); sparkTimer = null; } };
  startSpark();

  // 注意：这里**不接**聊天全屏静默 —— 热度 HUD 注入在聊天面板头部，
  // 全屏聊天时用户照样看得见它；1s tick + 800ms 火花本身也不值钱。
  // 静默要打在真正的成本上（3D 帧循环、大屏截图），不是打在看得见的 UI 上。
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) { stopTick(); stopSpark(); }
    else { startTick(); startSpark(); }
  });

  /* ---------- 主钩子：工具结果 → 热度 + 掉落 + 弹幕 ---------- */
  const onResult = (el: HTMLElement | null, success: boolean, costMs: number) => {
    if (!success) {
      const lost = Math.max(120, heat * 0.08);
      addHeat(-lost);                       // 失败掉热度，比自然衰减狠
      showGain(lost, false);
      shootDanmaku('fail');
      return;
    }

    const snap = App.game && App.game.snapshot ? App.game.snapshot() : null;
    const combo = snap ? snap.combo : 1;
    const fast = costMs < 1500;
    let gain = 80 * (1 + Math.min(combo, 12) * 0.15);
    if (fast) gain *= 1.5;
    addHeat(gain);
    showGain(gain, true);

    const rarity = rollDrop();
    if (rarity) {
      saved.gifts += 1;
      if (rarity === 'legend') saved.legends += 1;
      saved.total += 1;
      persist();
      sfx[rarity]();
      showDrop(el, rarity);
      if (rarity === 'legend') shootDanmaku('legend');
    } else {
      // 空手也要有反馈：七成次数不出货，若一声不响，观感就是「什么都没发生」。
      // 老虎机的手感恰恰来自「这次差一点」——所以给一次极轻的滑光 + 短音。
      if (el) {
        el.classList.remove('lv-near');
        void el.offsetWidth;
        el.classList.add('lv-near');
        setTimeout(() => el.classList.remove('lv-near'), 620);
      }
      sfx.near();
      shootDanmaku(fast && combo >= 3 ? 'crit' : 'success');
    }
  };

  /* 装饰而非改写：34_game_fx 一行不动，拿到 el 必须靠包一层 */
  if (App.game) {
    const orig = App.game.onToolResult;
    App.game.onToolResult = function patched(el: HTMLElement, success: boolean, costMs: number) {
      orig.call(this, el, success, costMs);
      try {
        onResult(el, success, costMs);
      } catch {
        /* 直播舱出问题绝不能拖垮工具链 */
      }
    };
  }

  renderHud();
  paintSpark();

  /* ---------- 对外接口 ---------- */
  App.live = {
    /** 手动掷一次（调试 / 单测用），返回稀有度 */
    roll: rollDrop,
    /**
     * 手动掉落一次（调试 / 单测用）。
     * 为什么需要：showDrop 是内部函数，而走 onToolResult 触发掉落是随机的 ——
     * 测试没法确定性地造出「同时 3 个礼物」来验证堆叠。有了它，堆叠逻辑可测。
     */
    drop: (rarity: Rarity) => showDrop(null, rarity),
    /** 手动加热度 */
    heat: (n: number) => addHeat(n),
    /** 快照 */
    snapshot: () => ({ heat, best: saved.best, gifts: saved.gifts, legends: saved.legends, pity: { ...pity } }),
    reset() {
      heat = 0;
      hotIdx = -1;
      saved.best = 0;
      saved.gifts = 0;
      saved.legends = 0;
      saved.total = 0;
      pity.sinceDrop = 0; pity.sinceEpic = 0; pity.sinceLegend = 0;
      persist();
      renderHud();
    },
  };
}
