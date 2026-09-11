/* ============================================================
 * 33_stage_wheel.ts —— 舞台右上角工具栏：侧边竖排，可上下滚动
 * ------------------------------------------------------------
 * 把 .stage-tools 里的一排按钮改成竖排滚动条：
 *   - 按钮竖排，容器固定高度，超出的按钮滚动切换
 *   - 上/下箭头按钮切换一组
 *   - 滚轮 / 拖拽列表 也可滚动
 *   - 当前可见扇区（中间）按钮放大高亮（wheel-front）
 *   - 锁屏键（stage-tools-fixed）固定在列表外，始终可点
 * 兼容：锁屏 / 沉浸 / VR（沿用 .stage-tools 容器）
 * ============================================================ */
import type { AppKernel } from '../types/app-kernel.js';

export default (function initStageWheel(App: AppKernel) {
  const tools = document.getElementById('stage-tools');
  const ring = document.getElementById('stage-tools-ring');
  if (!tools || !ring) return;

  /* ---------- 收集按钮（排除固定在外的） ---------- */
  const btns = Array.from(ring.querySelectorAll<HTMLButtonElement>('.stage-tool-btn'))
    .filter(b => !b.classList.contains('stage-tools-fixed'));

  const labelEl = document.getElementById('stage-tools-label');
  const upBtn = document.getElementById('wheel-up-btn');
  const downBtn = document.getElementById('wheel-down-btn');
  const toggleBtn = document.getElementById('stage-tools-toggle');

  if (btns.length === 0) return;

  /* ---------- 展开/收缩切换 ---------- */
  function setCollapsed(c: boolean) {
    tools.classList.toggle('collapsed', c);
    if (toggleBtn) {
      toggleBtn.title = c ? '展开工具栏' : '收起工具栏';
    }
    try { localStorage.setItem('dabai.stageTools.collapsed', c ? '1' : '0'); } catch { /* ignore */ }
  }
  toggleBtn?.addEventListener('click', () => {
    setCollapsed(!tools.classList.contains('collapsed'));
  });
  // 默认收缩（右上角只留一个小按钮）
  let saved: string | null = null;
  try { saved = localStorage.getItem('dabai.stageTools.collapsed'); } catch { /* ignore */ }
  setCollapsed(saved === '0' ? false : true);

  /* ---------- 布局参数 ---------- */
  const BTN = 40;        // 按钮直径
  const GAP = 4;         // 间距
  const STEP = BTN + GAP; // 每按钮步进
  // 同时可见按钮数：目标 6 个；矮屏按可用高度自动降级，避免按钮顶出屏幕
  // （230 ≈ 顶部安全区 + 收缩键 + 上下箭头 + 标签 + 底部留白）
  const VISIBLE = Math.max(3, Math.min(6, Math.floor((window.innerHeight - 230) / STEP)));
  const H = VISIBLE * BTN + (VISIBLE - 1) * GAP; // 容器高度
  ring.style.height = `${H}px`;

  /* ---------- 构建滚动轨道 ---------- */
  let track = ring.querySelector<HTMLElement>('.wheel-track');
  if (!track) {
    track = document.createElement('div');
    track.className = 'wheel-track';
    // 把按钮移进轨道
    btns.forEach(b => track!.appendChild(b));
    ring.appendChild(track);
  }

  let index = 0;                 // 当前顶部扇区索引
  const maxIndex = Math.max(0, btns.length - VISIBLE);

  /* ---------- 应用滚动位置 ---------- */
  function applyPos() {
    track!.style.transform = `translateY(${-index * STEP}px)`;
    updateFront();
  }

  /* ---------- 更新当前扇区高亮 + 标签 ---------- */
  function updateFront() {
    // 当前扇区 = 可见区中间那颗（随可见数自适应：4 个取第 2、6 个取第 3）
    const front = Math.min(index + Math.max(0, Math.floor(VISIBLE / 2) - 1), btns.length - 1);
    btns.forEach((b, i) => {
      b.classList.toggle('wheel-front', i === front);
      b.classList.toggle('wheel-hidden', i < index || i >= index + VISIBLE);
    });
    if (labelEl) {
      const title = btns[front].title || btns[front].id || '';
      labelEl.textContent = title.split('：')[0];
      labelEl.classList.remove('hidden');
    }
  }

  /* ---------- 滚动到指定索引 ---------- */
  function goTo(i: number) {
    index = Math.max(0, Math.min(maxIndex, i));
    applyPos();
  }

  /* ---------- 上/下一组 ---------- */
  function step(dir: 1 | -1) {
    goTo(index + dir);
  }

  upBtn?.addEventListener('click', () => step(-1));
  downBtn?.addEventListener('click', () => step(1));

  /* ---------- 滚轮滚动 ---------- */
  ring.addEventListener('wheel', (e) => {
    e.preventDefault();
    goTo(index + (e.deltaY > 0 ? 1 : -1));
  }, { passive: false });

  /* ---------- 拖拽滚动 ---------- */
  let dragging = false;
  let startY = 0;
  let startIndex = 0;
  ring.addEventListener('pointerdown', (e) => {
    dragging = true;
    startY = e.clientY;
    startIndex = index;
    ring.setPointerCapture(e.pointerId);
  });
  ring.addEventListener('pointermove', (e) => {
    if (!dragging) return;
    const dy = e.clientY - startY;
    // 每 STEP 像素滚动一格
    const delta = Math.round(dy / STEP);
    goTo(startIndex - delta);
  });
  ring.addEventListener('pointerup', () => { dragging = false; });
  ring.addEventListener('pointercancel', () => { dragging = false; });

  /* ---------- 初始定位 ---------- */
  goTo(0);

  /* ---------- 动态重建（运行时加入按钮时调用） ---------- */
  function rebuild() {
    const fresh = Array.from(ring.querySelectorAll<HTMLButtonElement>('.stage-tool-btn'))
      .filter(b => !b.classList.contains('stage-tools-fixed'));
    const known = new Set(btns);
    let changed = false;
    fresh.forEach((b) => {
      if (!known.has(b)) { btns.push(b); known.add(b); changed = true; }
    });
    if (!changed) return;
    // 新按钮加入轨道
    fresh.forEach(b => {
      if (b.parentElement !== track) track!.appendChild(b);
    });
    goTo(index);
  }

  /* ---------- 暴露给 App（调试/其他模块） ---------- */
  (App as any)._stageWheel = {
    next: () => step(1),
    prev: () => step(-1),
    goTo,
    rebuild,
    get index() { return index; },
  };

  console.log(`[StageWheel] 竖排滚动条初始化：${btns.length} 个按钮，可见 ${VISIBLE} 个`);
});
