import type { AppKernel } from '../types/app-kernel.js';

/* ============================================================
 *  街机氛围层（35_arcade_fx）
 *
 *  目标：把 3D 舞台从「一张静图」变成「街机厅」——持续可见的动态、
 *  随对话状态呼吸的光、像游戏 HUD 一样的信息层。
 *
 *  三条硬约束（决定实现方式，不是可选项）：
 *    1. **零每帧 JS**：所有常驻动效都是 CSS transform / opacity 动画，
 *       走合成层不触发重排；JS 只在「状态切换」「低频换内容」「脉冲」时动一次。
 *    2. **不挡交互**：整层 pointer-events:none，插在 canvas 之后、
 *       状态徽章/工具栏之前 → DOM 顺序保证 UI 永远绘制在氛围之上。
 *    3. **可关**：prefers-reduced-motion 时只留静态 HUD（无动画）；
 *       页面隐藏时数据流停摆（不空转）。
 *
 *  状态联动不额外挂钩子：直接 MutationObserver 监听 #status-badge 的
 *  class 变化（它已经是全局状态的唯一出口），零耦合拿到
 *  idle / thinking / listening / speaking。
 * ============================================================ */

const HEX = '0123456789ABCDEF';

/** 一行随机十六进制（数据流用） */
function hexLine(len: number): string {
  let s = '';
  for (let i = 0; i < len; i += 1) s += HEX[(Math.random() * 16) | 0];
  return s;
}

/** 状态徽章 class → 氛围层状态（颜色 / 动效速度由此切换） */
function stateOf(badge: HTMLElement | null): string {
  const c = badge ? badge.className : '';
  if (c.includes('thinking')) return 'thinking';
  if (c.includes('listening')) return 'listening';
  if (c.includes('speaking')) return 'speaking';
  if (c.includes('active')) return 'idle';
  return 'offline';
}

export default function init(App: AppKernel) {
  const stage = document.getElementById('stage');
  if (!stage) return;

  // 热重载 / 重复 init：清掉旧层，避免叠出两套
  const stale = document.getElementById('arcade-fx');
  if (stale) stale.remove();

  const reduce =
    typeof window.matchMedia === 'function' &&
    window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  const root = document.createElement('div');
  root.id = 'arcade-fx';
  root.setAttribute('aria-hidden', 'true');
  root.dataset.state = 'idle';
  root.innerHTML = [
    '<div class="af-sky"></div>',       // 顶部天光（缓慢呼吸）
    '<div class="af-grid"></div>',      // 透视网格地板（向前滚动）
    '<div class="af-horizon"></div>',   // 地平线光带
    '<div class="af-scan"></div>',      // CRT 扫描线
    '<div class="af-vignette"></div>',  // 四周压暗（聚焦角色）
    '<div class="af-stream af-stream-l"><b></b><b></b></div>',
    '<div class="af-stream af-stream-r"><b></b><b></b></div>',
    '<div class="af-corners"><i></i><i></i><i></i><i></i></div>',
    '<div class="af-edge af-edge-t"></div>',
    '<div class="af-edge af-edge-b"></div>',
    '<div class="af-shock"></div>',     // 冲击波（暴击 / 升级时扩散）
  ].join('');

  const canvas = stage.querySelector('#three-canvas');
  if (canvas && canvas.parentElement === stage) canvas.insertAdjacentElement('afterend', root);
  else stage.insertBefore(root, stage.firstChild);

  /* ---------- 状态联动 ---------- */
  const badge = document.getElementById('status-badge');
  const applyState = () => {
    const st = stateOf(badge);
    if (root.dataset.state !== st) root.dataset.state = st;
  };
  applyState();
  if (badge && typeof MutationObserver !== 'undefined') {
    new MutationObserver(applyState).observe(badge, {
      attributes: true,
      attributeFilter: ['class'],
    });
  }

  /* ---------- 数据流：低频换内容，滚动交给 CSS ---------- */
  const reels = Array.from(root.querySelectorAll('.af-stream b')) as HTMLElement[];
  const ROWS = 14;
  const paintReels = () => {
    const lines: string[] = [];
    for (let i = 0; i < ROWS; i += 1) lines.push(hexLine(6));
    const text = lines.join('\n');
    for (const r of reels) r.textContent = text;
  };
  paintReels();

  let reelTimer: ReturnType<typeof setInterval> | null = null;
  const startReels = () => {
    if (reelTimer || reduce) return;
    // 3s 一换：观感是「数据在流」，成本是每秒 0.67 次文本写入
    reelTimer = setInterval(paintReels, 3000);
  };
  const stopReels = () => {
    if (reelTimer) { clearInterval(reelTimer); reelTimer = null; }
  };
  startReels();
  let quiet = false;
  const syncReels = () => {
    if (document.hidden || quiet) stopReels();
    else startReels();
  };
  document.addEventListener('visibilitychange', syncReels);
  // 聊天全屏：街机数据流层看不见 —— 3s 一次的文本重写停掉
  App.onQuiet((on) => { quiet = on; syncReels(); });

  /* ---------- 冲击波：暴击 / 升级时从屏幕中心扩散 ---------- */
  const shock = root.querySelector('.af-shock') as HTMLElement | null;
  let shockTimer: ReturnType<typeof setTimeout> | null = null;
  const pulse = (kind: 'crit' | 'levelup' | 'tool' = 'tool') => {
    if (reduce || !shock) return;
    shock.className = 'af-shock ' + kind;
    void shock.offsetWidth; // 强制重排以重放动画
    shock.classList.add('on');
    if (shockTimer) clearTimeout(shockTimer);
    shockTimer = setTimeout(() => shock.classList.remove('on'), 900);
  };

  /* ---------- 对外接口 ---------- */
  App.arcade = {
    root,
    pulse,
    /** 状态手动刷新（外部改了徽章 class 又不想等 observer 时用） */
    refresh: applyState,
  };
}
