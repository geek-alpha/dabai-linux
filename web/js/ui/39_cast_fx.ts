import type { AppKernel } from '../types/app-kernel.js';

/* ============================================================
 *  施法态（39_cast_fx）—— 大白正在写代码的那段时间，屏幕必须是活的
 *
 *  【反思：之前错在哪】
 *  上一轮把爽感全放在了「工具结束之后」：结束后才掉礼物、才弹评级、才闪屏。
 *  可写代码最漫长、最需要陪伴的恰恰是**等待的那几十秒**——那时屏幕一动不动。
 *
 *  更糟的是：后端一直在推 tool_call_progress（含真实 elapsed），
 *  经过 09_websocket → 13_messages → App.toolChainProgress，
 *  而那个函数在 31_tool_chain 里是 **空的**：
 *      App.toolChainProgress = function (_n, _e, _m) {};
 *  后端说「已经跑了 12.3 秒」，前端收到，扔掉。这是最大的浪费。
 *
 *  【本模块做四件事】
 *  1. 接管那个空钩子，把真实 elapsed 变成看得见的秒表与档位
 *  2. 施法期间全屏进入高能态：能量场 + 中心法阵 + 光柱 + 扫描
 *  3. 等待本身被奖励化：等得越久档位越高、画面越猛，暗示「这一发很大」
 *  4. **说明搬到屏幕中央**：工具名 + 人话说明 + 秒表，做成大字幕
 *
 *  【为什么说明必须搬到中央（本轮改动）】
 *  旧版把「工具名 · 档位 · 秒表」放在 bottom:15%、12px 的小胶囊里 ——
 *  那是角色脚下的一条窄带，眼睛盯着中央时根本读不到；而真正在说「大白
 *  此刻在干什么」的信息，被塞在对话框里的一行小字里，既看不清又挡正文。
 *  现在它升格为屏幕中央的大字幕：一眼就知道「它在改写代码」。
 *
 *  【连发：短期多次调用 = 越来越猛】
 *  一次工具调用是一次施法；4 秒内再来一次就是「连发」。
 *  连发不是简单加个数字——它是**整体升温**：徽章弹出、冲击环炸开、
 *  音高随连发数递增、能量场外沿多一层呼吸光晕。
 *  连得越多越猛，这是「大白正在连轴干活」唯一能被看见的证据。
 *
 *  【三档（等待的价值阶梯）】
 *    0~3s    施法      青蓝，法阵缓转
 *    3~10s   深度施法  转紫，转速加快、能量场收束
 *    >10s    超载      转金，全场最强，大招蓄力感
 *  升档有独立音效与一次冲击——这是「等待被看见」的仪式。
 *
 *  【性能】
 *  秒表 100ms 定时器（不是每帧）；所有常驻动效是 CSS animation，走合成层；
 *  非施法态下内部动画**根本不定义**（不是隐藏），零空转；页面隐藏即停表。
 * ============================================================ */

interface Tier { at: number; cls: string; label: string; }

const TIERS: Tier[] = [
  { at: 0, cls: '', label: '施法' },
  { at: 3000, cls: 'cast-t1', label: '深度施法' },
  { at: 10000, cls: 'cast-t2', label: '超载 · 大招蓄力' },
];

/** 连发判定窗口：两次调用间隔小于它才算「连发」。4s 是「一口气干好几件事」的自然节奏 */
export const COMBO_WINDOW = 4000;

/* ---------- 工具说明：从工具**自己**的 description 里提炼 ----------
 *
 * 【为什么删掉了映射表】
 * 上一版维护了一张「工具名 → 人话」的表（47 条）+ 家族前缀兜底。它必然过期：
 * 工具是动态扩展的（技能按需加载、插件、用户自建），新工具不在表里就落进兜底，
 * 显示成「正在处理代码」这种不伦不类的说法 —— 比直接显示工具名更糟。
 *
 * 现在改成从工具自带的 description 里提炼首句。工具描述由工具作者写、
 * 和工具同生共死，是唯一不会过期的来源。实测效果：
 *   code_edit   "精准修改文件（v3.0：对标 ast-grep/comby…）。replace(默认)=…"
 *               → 「精准修改文件」
 *   code_search "批量检索代码：正则/关键词搜索项目内文件，支持类型过滤…。默认忽略…"
 *               → 「批量检索代码」
 *   shell_run   "执行一条 Windows 命令并返回输出。适合快速本机操作…"
 *               → 「执行一条 Windows 命令并返回输出」
 *
 * 提炼规则是纯字符串处理，不猜语义、不做词表：
 *   1. 取第一个句号/换行之前 —— 那本来就是作者写的「一句话概括」
 *   2. 去掉括号里的补充（版本号、对标对象这类对用户无意义的元信息）
 *   3. 仍超长就按冒号/逗号再切一刀
 *   4. 没有描述就显示工具名本身 —— 真实，不猜
 */

/** 超过它才继续按冒号/逗号切一刀（"概括：细节" 的常见写法） */
const DESC_SPLIT = 18;
/** 最终硬上限：超过才截断加 …。
 *  24~28 字是 27px 字号下的舒适上限；实测 155 个真实工具的描述首句
 *  最短 4 字、平均 11.6 字、最长 25 字 —— 定 28 意味着**一个都不会被截断**。
 *  留这个上限只是防未来某个工具写了个超长首句，把字幕撑爆。 */
const DESC_MAX = 28;

/** 工具说明 → 屏幕上显示的一行字。纯函数，便于单测。 */
export function describe(toolDesc: string, toolName?: string): string {
  const name = String(toolName || '').trim();
  const raw = String(toolDesc || '').replace(/\s+/g, ' ').trim();

  if (raw) {
    // 1. 第一句：作者写的概括通常就在这
    let s = (raw.split(/[。！？\n]/)[0] || '').trim();
    // 2. 去括号补充说明（全角/半角都要去）
    s = s.replace(/[（(][^）)]*[）)]/g, '').trim();
    // 3. 仍超长：按冒号/逗号再切一刀（"概括：细节" 的常见写法）
    if (s.length > DESC_SPLIT) {
      s = (s.split(/[：:，,；;]/)[0] || s).trim();
    }
    if (s) {
      if (s.length <= DESC_MAX) return s;
      // 截断时优先落在空格边界：实测有工具的首句含路径（data/mixamo_cookies.json）
      // 或英文词（Agent Skill），硬切会把它们劈成两半，看起来像乱码。
      const cut = s.slice(0, DESC_MAX);
      const sp = cut.lastIndexOf(' ');
      return (sp > DESC_MAX * 0.5 ? cut.slice(0, sp) : cut) + '…';
    }
  }
  // 4. 兜底：显示工具名本身。不加「正在…」这类猜测性前缀 ——
  //    猜错了比不猜更糟，而工具名至少是真实的。
  return name || '正在施法';
}

/** 档位判定：纯函数，便于单测 */
export function tierOf(ms: number): number {
  if (!Number.isFinite(ms) || ms < 0) return 0;
  for (let i = TIERS.length - 1; i >= 0; i -= 1) {
    if (ms >= TIERS[i].at) return i;
  }
  return 0;
}

export default function init(App: AppKernel) {
  const stage = document.getElementById('stage');
  if (!stage) return;

  // 热重载 / 重复 init：清掉旧层
  const stale = document.getElementById('cast-fx');
  if (stale) stale.remove();

  const reduce =
    typeof window.matchMedia === 'function' &&
    window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  const root = document.createElement('div');
  root.id = 'cast-fx';
  root.setAttribute('aria-hidden', 'true');
  root.innerHTML = [
    '<div class="cf-aura"></div>',      // 全屏能量场
    '<div class="cf-ring"></div>',      // 中心法阵（双环反向）
    '<div class="cf-core"></div>',      // 核心光点
    '<div class="cf-beam"></div>',      // 竖向光柱
    // 中央大字幕：说明搬到这里，才真的「引人注意」
    '<div class="cf-hud">',
    '  <div class="cf-hud-main">',
    '    <span class="cf-spin">⟳</span>',
    '    <span class="cf-act">正在施法</span>',   // 人话说明：主角
    '  </div>',
    '  <div class="cf-hud-sub">',
    '    <span class="cf-name"></span>',          // 真实工具名（小字，给想看细节的人）
    '    <span class="cf-dot">·</span>',
    '    <span class="cf-tier">施法</span>',
    '    <span class="cf-dot">·</span>',
    '    <span class="cf-time">0.0s</span>',
    '    <span class="cf-par"></span>',           // 并行数：同时跑几个工具（×N）
    '    <span class="cf-combo"></span>',         // 连发：短期多次调用（连发 ×N）
    '  </div>',
    '</div>',
    '<div class="cf-combo-wave"></div>',  // 连发冲击环
    '<div class="cf-burst"></div>',       // 结束爆发
  ].join('');

  // 挂在氛围层之后：同层不设 z-index，靠 DOM 顺序，绝不盖住状态徽章与工具栏
  const anchor = document.getElementById('arcade-fx');
  if (anchor && anchor.parentElement === stage) anchor.insertAdjacentElement('afterend', root);
  else stage.appendChild(root);

  const html = document.documentElement;
  const nameEl = root.querySelector('.cf-name') as HTMLElement;
  const actEl = root.querySelector('.cf-act') as HTMLElement;
  const tierEl = root.querySelector('.cf-tier') as HTMLElement;
  const timeEl = root.querySelector('.cf-time') as HTMLElement;
  const parEl = root.querySelector('.cf-par') as HTMLElement;
  const comboEl = root.querySelector('.cf-combo') as HTMLElement;
  const waveEl = root.querySelector('.cf-combo-wave') as HTMLElement;
  const burstEl = root.querySelector('.cf-burst') as HTMLElement;

  /* ---------- 音效：与 34/38 同款 WebAudio 合成，零资源文件 ---------- */
  let audio: AudioContext | null = null;
  const tone = (freq: number, dur = 0.09, type: OscillatorType = 'sine', vol = 0.04, delay = 0) => {
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

  /* ---------- 状态 ---------- */
  let active = 0;            // 并发工具计数（串行为主，但留余量）
  let tier = 0;
  let elapsedMs = 0;
  let startedAt = 0;
  let tickTimer: ReturnType<typeof setInterval> | null = null;
  let combo = 0;             // 连发计数：4s 内再次调用 +1
  let lastCallAt = 0;        // 上一次调用起点（连发判定的时间基准）

  /** 把档位类写在 <html> 上：CSS 选择器是 html.cast-t1 #cast-fx。
   *  写在 #cast-fx 自己身上一个都匹配不到，颜色根本不会变（踩过）。 */
  const clearTier = () => {
    html.classList.remove('cast-t1', 'cast-t2');
    root.classList.remove('cf-up');
  };

  /* ---------- 连发：短期多次调用 = 越来越猛 ----------
   * 判定纯看时间窗口，**不随施法结束而重置**：工具之间有空档（active 归零）是常态，
   * 若那时把 combo 清零，「一口气干五件事」就永远连不起来。
   */
  const comboWave = () => {
    waveEl.classList.remove('on');
    void waveEl.offsetWidth;      // 强制回流：否则连续两次加同一个类不会重放动画
    waveEl.classList.add('on');
    setTimeout(() => waveEl.classList.remove('on'), 900);
  };

  const renderCombo = () => {
    comboEl.textContent = combo >= 2 ? '连发 ×' + combo : '';
    // 「热」写在 <html> 上：能量场外沿那层呼吸光晕由它驱动
    html.classList.toggle('casting-combo', combo >= 2);
    root.style.setProperty('--cf-n', String(Math.min(combo, 9)));
    if (combo >= 2) {
      comboEl.classList.remove('pop');
      void comboEl.offsetWidth;
      comboEl.classList.add('pop');
    }
  };

  /** 每次工具调用都调一次：按时间窗口累计连发，并触发一次冲击 */
  const noteCombo = () => {
    const now = performance.now();
    combo = (lastCallAt > 0 && now - lastCallAt < COMBO_WINDOW) ? combo + 1 : 1;
    lastCallAt = now;
    renderCombo();
    if (combo < 2 || reduce) return;
    comboWave();
    // 音高随连发递增：越连越高，这是「越连越爽」的听觉线索
    tone(523 + Math.min(combo, 8) * 66, 0.1, 'triangle', 0.035);
    if (combo === 3 && App.showToast) App.showToast('🔥 三连发 · 手感上来了');
    if (combo === 6 && App.showToast) App.showToast('🔥 六连发 · 停不下来');
  };

  const render = () => {
    timeEl.textContent = (elapsedMs / 1000).toFixed(1) + 's';
    // 并行度上屏：同时跑 N 个工具时才显示。这是「提速」唯一能被眼睛看见的证据
    parEl.textContent = active > 1 ? '×' + active : '';
    const t = tierOf(elapsedMs);
    if (t === tier) return;
    tier = t;
    html.classList.remove('cast-t1', 'cast-t2');
    if (TIERS[t].cls) html.classList.add(TIERS[t].cls);
    tierEl.textContent = TIERS[t].label;
    // 升档仪式：闪一次 + 音效。只有真的跨档才响，不是每秒都响
    if (!reduce) {
      root.classList.remove('cf-up');
      void root.offsetWidth;
      root.classList.add('cf-up');
      tone(t === 2 ? 1046 : 784, 0.16, 'triangle', 0.05);
      tone(t === 2 ? 1568 : 1175, 0.2, 'sine', 0.04, 0.07);
    }
    if (t === 2 && App.showToast) App.showToast('⚡ 超载 · 这一发很大');
  };

  const tick = () => {
    if (active <= 0) return;
    elapsedMs = performance.now() - startedAt;
    render();
  };

  const startTimer = () => {
    if (tickTimer || reduce) return;
    tickTimer = setInterval(tick, 100);
  };
  const stopTimer = () => {
    if (tickTimer) { clearInterval(tickTimer); tickTimer = null; }
  };
  // 聊天全屏：施法层看不见 —— 100ms 计时渲染全停；退出全屏时若施法仍在进行则恢复
  App.onQuiet((on) => {
    if (on) stopTimer();
    else if (active > 0) startTimer();
  });

  /* ---------- 施法开始 ---------- */
  const begin = (toolName: string, toolDesc?: string) => {
    const raw = String(toolName || '工具');
    active += 1;
    // 说明每次都更新：并行/连发时显示「最新那个」。只在 active===1 时设一次的话，
    // 并行跑 3 个工具时中央字幕会一直停在第一个工具上，说的是错的。
    nameEl.textContent = raw.replace(/_/g, ' ');
    actEl.textContent = describe(toolDesc || '', raw);

    if (active === 1) {
      startedAt = performance.now();
      elapsedMs = 0;
      tier = 0;
      tierEl.textContent = TIERS[0].label;
      clearTier();
      html.classList.add('casting');
      if (App.arcade) App.arcade.pulse('tool');
      if (!reduce) tone(392, 0.14, 'sine', 0.035);   // 起手低音
      startTimer();
    }
    noteCombo();   // 连发判定放在起手音之后：低音起手 → 高音叠上，听起来是「接上了」
    render();
  };

  /* ---------- 施法结束：爆发 ---------- */
  const burst = (ok: boolean) => {
    burstEl.className = 'cf-burst ' + (ok ? 'ok' : 'fail');
    // 12 颗粒子向外炸开
    let inner = '';
    for (let i = 0; i < 12; i += 1) {
      const ang = (Math.PI * 2 * i) / 12 + Math.random() * 0.4;
      const dist = 90 + Math.random() * 130;
      inner += '<i style="--dx:' + (Math.cos(ang) * dist).toFixed(0) + 'px;--dy:' +
        (Math.sin(ang) * dist).toFixed(0) + 'px;--d:' + (0.5 + Math.random() * 0.35).toFixed(2) + 's"></i>';
    }
    burstEl.innerHTML = inner;
    void burstEl.offsetWidth;
    burstEl.classList.add('on');
    setTimeout(() => burstEl.classList.remove('on'), 1000);
  };

  const end = (ok: boolean) => {
    // 已经结束就别再结算：迟到的 result（或重复推送）不该再炸一次
    if (active <= 0) return;
    active -= 1;
    if (active > 0) { render(); return; }   // 还有工具在跑：更新并行数，不爆发
    const spent = performance.now() - startedAt;
    stopTimer();
    html.classList.remove('casting', 'casting-combo');
    clearTier();
    tier = 0;
    if (!reduce) burst(ok);
    // 超载完成 = 长任务通关，给一次明确的回报感
    if (ok && spent >= TIERS[2].at && App.showToast) {
      App.showToast('🏆 超载完成 · 用时 ' + (spent / 1000).toFixed(1) + 's');
    }
  };

  /** 强制收尾：abort / endTurn / reset 共用，保证三条路径行为一致 */
  const forceStop = () => {
    active = 0;
    stopTimer();
    html.classList.remove('casting', 'casting-combo');
    clearTier();
    tier = 0;
    render();   // 并行数归零也要反映到界面上（否则会残留 ×N）
  };

  /* ---------- 装饰 31_tool_chain 的钩子（不改它一行） ---------- */
  if (App.toolChainStart) {
    const orig = App.toolChainStart;
    App.toolChainStart = function patched(toolName: string, args: any, toolDesc?: string) {
      orig.call(this, toolName, args, toolDesc);
      try { begin(toolName, toolDesc); } catch { /* 施法层出错不能拖垮工具链 */ }
    };
  }

  if (App.toolChainResult) {
    const orig = App.toolChainResult;
    App.toolChainResult = function patched(toolName: string, result: any, success: boolean) {
      orig.call(this, toolName, result, success);
      try { end(success !== false); } catch { /* 同上 */ }
    };
  }

  // 关键：接管那个空函数。
  //
  // 【单位契约】后端 elapsed 的单位是**秒**，而且是整数
  // （agent.py: `elapsed = int(time.monotonic() - start)`；
  //  server.py: `"elapsed": int(event.elapsed or 0)`）。
  // 前端内部一律用毫秒。曾经写成 `elapsedMs = elapsed` 直接赋值——
  // 心跳每 5 秒把秒表打回 0.0s，本地定时器再涨回来，秒表周期性抽搐。
  // 更糟的是这个 bug 骗过了测试：cast_fx_check 传的是 4200 / 11000（毫秒），
  // 我自己假设了错误的契约，于是测试全绿。教训：跨端字段必须写死单位。
  //
  // 【为什么不直接用后端值】后端心跳是整数秒，精度只有 1s，用它当主时钟
  // 秒表会一格一格地跳。所以策略是「本地 100ms 定时器为主、后端心跳为校准」：
  // 只在本地明显落后（被后台节流）时才拉回来。
  const CALIBRATE_MS = 1500;
  App.toolChainProgress = function toolChainProgress(_toolName: string, elapsed: number, _message?: string) {
    if (active <= 0) return;
    const remoteMs = elapsed * 1000;
    const localMs = performance.now() - startedAt;
    if (Number.isFinite(remoteMs) && remoteMs > localMs + CALIBRATE_MS) {
      // 校准方式是「把起点往前挪」，**不是**直接改 elapsedMs——
      // tick 每次都按 (now - startedAt) 重算，直接改会在 100ms 内被覆盖回去。
      // 挪完之后 localMs ≈ remoteMs，条件不再成立，所以不会反复加速。
      startedAt = performance.now() - remoteMs;
      elapsedMs = remoteMs;
      render();
    }
  };

  if (App.toolChainAbort) {
    const orig = App.toolChainAbort;
    App.toolChainAbort = function patched() {
      orig.call(this);
      try { forceStop(); } catch { /* 同上 */ }
    };
  }

  if (App.toolChainEndTurn) {
    const orig = App.toolChainEndTurn;
    App.toolChainEndTurn = function patched() {
      orig.call(this);
      // 整轮结束兜底：万一某个工具没有 result，施法态也必须收干净
      try { forceStop(); } catch { /* 同上 */ }
    };
  }

  document.addEventListener('visibilitychange', () => {
    if (document.hidden) stopTimer();
    else if (active > 0) startTimer();
  });

  /* ---------- 对外接口 ---------- */
  App.cast = {
    /** 手动进入施法（调试 / 单测用） */
    begin,
    /** 手动结束施法 */
    end,
    /** 当前是否在施法 */
    get active() { return active; },
    /** 已用时长 ms */
    get elapsed() { return active > 0 ? performance.now() - startedAt : elapsedMs; },
    /** 当前档位 0/1/2 */
    get tier() { return tier; },
    /** 当前连发数（1 = 没连上） */
    get combo() { return combo; },
    /** 工具说明 → 屏幕显示的一行字（暴露给测试，避免测试自己抄一份规则） */
    describe,
    /**
     * 强制归零（调试 / 自动化测试用）。
     * 存在的理由：施法态由后端推送驱动，若某个工具的 result 没推过来，
     * 前端就会一直停在「执行中」。测试需要确定性起点，人手也需要一个复位开关。
     */
    reset() {
      forceStop();
      elapsedMs = 0;
      combo = 0;          // reset 连时间窗口一起清：测试要的是干净起点
      lastCallAt = 0;
      renderCombo();
    },
  };
}
