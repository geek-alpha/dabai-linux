import type { AppKernel } from '../types/app-kernel.js';

/* ============================================================
 *  流式节奏引擎（37_stream_pulse）
 *
 *  【要解决的问题】
 *  AI 高频输出时，用户看到的只是「文本在变长」。那是信息，不是体验——
 *  没有节拍、没有呼吸、没有「对方正在组织语言」的存在感。
 *
 *  【第一性原理拆解】
 *  「生成」这件事本身包含三个可感知的物理量：
 *    · rate    速率 —— 此刻生成得多快
 *    · beat    节拍 —— 一句话说完了（句界）
 *    · breathe 呼吸 —— 停顿了，在组织下一句
 *  把这三个量从文本流里提取出来，写进 CSS 变量，其余全部交给 CSS 合成层。
 *
 *  【分层（关键设计，不是实现细节）】
 *    本模块  只做「测量」，不碰任何动画 → 纯逻辑、可单测、可替换
 *    CSS     只做「呈现」，读 --sp-* 变量 → 动画全在合成层
 *    大厅层  同样读这些变量 → AI 说得快，世界就快；AI 停顿，世界也静
 *  三层各司其职、互不引用，任一层可单独删掉而不影响另外两层。
 *
 *  【性能约定】
 *    单个 rAF 循环合并写入；数值量化到 0.01，不变则不写。
 *    HUD 数字与频谱走 120ms 定时器，不用每帧。页面隐藏时停摆。
 * ============================================================ */

export default function init(App: AppKernel) {
  const root = document.documentElement;

  /* ---------- 手感参数（集中在此，便于调校） ---------- */
  const WINDOW = 900;        // 速率统计窗口 ms
  const FULL_RATE = 55;      // 达到「满速」的字符/秒
  const BREATHE_GAP = 360;   // 静默超过此值 → 进入呼吸态
  const IDLE_TIMEOUT = 2500; // 静默超过此值 → 判定本轮结束，自动收尾
                             // （看门狗放在引擎内部：上游无论从哪条路径结束回复，
                             //   节奏都会自己归零，不需要在 4 个收尾点各插一次 end）
  const BEAT_MIN_GAP = 80;   // 两拍最小间隔，防止连续标点连击
  const HUD_INTERVAL = 120;  // HUD 刷新间隔 ms
  const BEAT_RE = /[。！？!?…；;：:\n]/;

  /* ---------- 状态 ---------- */
  type Phase = 'idle' | 'flowing' | 'breathing';
  let phase: Phase = 'idle';
  let samples: { t: number; n: number }[] = [];
  let lastFeedAt = 0;
  let lastBeatAt = 0;
  let beats = 0;
  let rate = 0;          // 字符/秒
  let norm = 0;          // 归一化 0~1
  let raf = 0;
  let lastWritten = -1;
  let beatPending = false;   // 脉冲 class 的重启是否已排进下一帧
  let hudTimer = 0;
  let hudEl: HTMLElement | null = null;
  let hudNum: HTMLElement | null = null;
  let hudBars: HTMLElement[] = [];

  /* ---------- HUD：把「速率」变成看得见的频谱 ---------- */
  function ensureHud() {
    if (hudEl && document.body.contains(hudEl)) return;
    // 优先挂进大厅氛围层：那里 pointer-events:none，天然不挡交互
    const host = document.getElementById('arcade-fx')
      || document.getElementById('chat-panel');
    if (!host) return;
    hudEl = document.createElement('div');
    hudEl.id = 'stream-hud';
    hudEl.setAttribute('aria-hidden', 'true');
    let html = '<div class="sh-bars">';
    for (let i = 0; i < 12; i++) html += '<i></i>';
    html += '</div><div class="sh-num"><b>0</b><span>字/秒</span></div>';
    hudEl.innerHTML = html;
    host.appendChild(hudEl);
    hudNum = hudEl.querySelector('.sh-num b') as HTMLElement | null;
    hudBars = Array.prototype.slice.call(hudEl.querySelectorAll('.sh-bars i'));
  }

  function updateHud() {
    hudTimer = 0;
    if (!hudEl) return;
    if (phase === 'idle') {
      hudEl.classList.remove('on');
      return;
    }
    hudEl.classList.add('on');
    if (hudNum) hudNum.textContent = String(Math.round(rate));
    // 频谱：正弦相位差让 12 根条形成波形，幅度由速率驱动（scaleY 走合成层）
    const t = performance.now() / 1000;
    for (let i = 0; i < hudBars.length; i++) {
      const w = 0.5 + 0.5 * Math.sin(t * 4 + i * 0.75);
      const h = 0.12 + norm * 0.88 * (0.4 + 0.6 * w);
      hudBars[i].style.transform = 'scaleY(' + h.toFixed(3) + ')';
    }
    hudTimer = window.setTimeout(updateHud, HUD_INTERVAL);
  }

  /* ---------- 核心：把测量结果写进 CSS 变量 ---------- */
  function write() {
    const v = Math.round(norm * 100) / 100;   // 量化，避免无意义的样式失效
    if (v === lastWritten) return;
    lastWritten = v;
    root.style.setProperty('--sp-rate', v.toFixed(2));
    root.style.setProperty('--sp-glow', (0.3 + v * 0.7).toFixed(2));
    // 超频：AI 高速运转时读数与大厅转金色——给一个「它在全力思考」的爽点
    root.classList.toggle('sp-over', v >= 0.8);
  }

  function schedule() {
    if (!raf && phase !== 'idle') raf = requestAnimationFrame(tick);
  }

  function tick() {
    raf = 0;
    if (phase === 'idle') return;
    const now = performance.now();
    while (samples.length && now - samples[0].t > WINDOW) samples.shift();
    let total = 0;
    for (let i = 0; i < samples.length; i++) total += samples[i].n;
    rate = total * (1000 / WINDOW);
    norm = Math.min(1, rate / FULL_RATE);
    // 看门狗：静默太久 = 本轮已结束，自行归零（不依赖上游显式调用 end）
    if (now - lastFeedAt > IDLE_TIMEOUT) { setPhase('idle'); return; }
    // 静默超过阈值：从「输出中」转入「呼吸中」——AI 在组织下一句
    if (phase === 'flowing' && now - lastFeedAt > BREATHE_GAP) setPhase('breathing');
    write();
    schedule();   // schedule 内部自行判断 phase，这里不再重复判断
  }

  function setPhase(p: Phase) {
    if (p === phase) return;
    phase = p;
    root.dataset.sp = p;          // CSS 用 [data-sp="..."] 分支
    if (p === 'idle') {
      samples = [];
      rate = 0;
      norm = 0;
      lastWritten = -1;
      root.style.setProperty('--sp-rate', '0');
      root.style.setProperty('--sp-glow', '0.3');
      root.classList.remove('sp-over');
    }
    schedule();
    if (!hudTimer) hudTimer = window.setTimeout(updateHud, 60);
  }

  /* 聊天全屏（不透明）：速率 HUD 被完全遮挡 —— rAF 与 HUD 定时器一起停。
   * 本模块只测量不绘制，但每帧的采样/写入仍是主线程开销，静默时一帧都不该跑。
   * 注册即回调，所以这里不需要在别处补一次初始状态。 */
  App.onQuiet((on: boolean) => {
    if (on) {
      if (raf) { cancelAnimationFrame(raf); raf = 0; }
      if (hudTimer) { clearTimeout(hudTimer); hudTimer = 0; }
    } else if (phase !== 'idle') {
      // 退出全屏时若流式仍在进行，接着测（看门狗会在静默超时后自行归零）
      schedule();
      if (!hudTimer) hudTimer = window.setTimeout(updateHud, 60);
    }
  });

  /* ---------- 节拍：句界处给一次视觉脉冲 ---------- */
  function beat(now: number) {
    if (now - lastBeatAt < BEAT_MIN_GAP) return;
    lastBeatAt = now;
    beats++;
    const el = App.pendingAIMsgEl || App._turnMsgEl;
    if (!el || !document.body.contains(el)) return;
    el.classList.remove('sp-beat');
    // 重启 CSS 动画不能靠 `void el.offsetWidth`：读布局会把整条消息的同步布局算完，
    // 而流式期间这条消息每帧都在变长 —— 实测这一读吃掉流式期主线程 Layout 的六成，
    // 帧 p95 从 18ms 被抬到 79ms、jank 从 4% 涨到 58%。
    // 隔一帧再加 class 一样能重启动画：浏览器自然重算样式，主线程不必阻塞在读取上。
    // 脉冲间隔 ≥BEAT_MIN_GAP(80ms)，中间那一帧空档落在两次脉冲之间，视觉无差别。
    if (beatPending) return;
    beatPending = true;
    requestAnimationFrame(() => {
      beatPending = false;
      if (el.isConnected) el.classList.add('sp-beat');
    });
  }

  /* ---------- 入口：喂入增量文本 ---------- */
  function feed(text: string) {
    if (!text) return;
    const now = performance.now();
    samples.push({ t: now, n: text.length });
    lastFeedAt = now;
    if (phase !== 'flowing') setPhase('flowing');
    if (BEAT_RE.test(text)) beat(now);
    schedule();
  }

  /* ---------- 用户一侧的节奏：打字也走同一套变量体系（人机同频） ---------- */
  const input = App.textInput;
  if (input) {
    let inputStamps: number[] = [];
    let decayTimer = 0;
    input.addEventListener('input', () => {
      const now = performance.now();
      inputStamps.push(now);
      while (inputStamps.length && now - inputStamps[0] > WINDOW) inputStamps.shift();
      const r = Math.min(1, inputStamps.length / 8);
      root.style.setProperty('--sp-input', r.toFixed(2));
      if (decayTimer) clearTimeout(decayTimer);
      decayTimer = window.setTimeout(() => {
        root.style.setProperty('--sp-input', '0');
        inputStamps = [];
      }, 600);
    }, { passive: true });
  }

  /* ---------- 页面隐藏即停摆，不留后台空转 ---------- */
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) {
      if (raf) { cancelAnimationFrame(raf); raf = 0; }
    } else {
      schedule();
    }
  });

  ensureHud();
  // 显式落一个初值：CSS 全部用 [data-sp="..."] 分支，没有初值会匹配不到任何分支
  root.dataset.sp = 'idle';

  /* ---------- 对外接口 ---------- */
  App.stream = {
    /** 喂入一段增量文本（由 13_messages 的 setTurnStreamText 调用） */
    feed,
    /** 新一轮输出开始 */
    begin() { ensureHud(); setPhase('flowing'); },
    /** 输出结束，清空所有节奏变量 */
    end() { setPhase('idle'); },
    /** 当前速率（字符/秒） */
    get rate() { return rate; },
    /** 本轮累计节拍数 */
    get beats() { return beats; },
    /** 手动触发一次脉冲（用户发送消息时用） */
    pulse() { beat(performance.now()); },
  };
}
