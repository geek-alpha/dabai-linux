/* ============================================================
 * 33_stage_wheel.ts —— 舞台右上角工具栏：侧边竖排，可上下滚动
 * ------------------------------------------------------------
 * 把 .stage-tools 里的一排按钮改成竖排滚动条：
 *   - 按钮竖排，容器固定高度，超出的按钮滚动切换
 *   - 上/下箭头按钮切换一组
 *   - 滚轮 / 拖拽列表 也可滚动
 *   - 当前可见扇区（中间）按钮放大高亮（wheel-front）
 *   - 锁屏键也在列表内（箭头/滚轮能遍历到）；锁屏时轮盘收缩到只剩它
 * 兼容：锁屏 / 沉浸 / VR（沿用 .stage-tools 容器）
 * ============================================================ */
import type { AppKernel } from '../types/app-kernel.js';

export default (function initStageWheel(App: AppKernel) {
  const tools = document.getElementById('stage-tools');
  const ring = document.getElementById('stage-tools-ring');
  if (!tools || !ring) return;

  /* ---------- 收集按钮（排除固定在外的） ---------- */
  const btns = Array.from(ring.querySelectorAll<HTMLButtonElement>('.stage-tool-btn'))
    // 管理员专属按钮（data-admin-only）对普通用户直接从数组里剔除：轮盘是按数组下标
    // 滚动定位的，只用 CSS 隐藏会留下一个空白扇区，滚动位置也跟着错。
    .filter(b => !(b.dataset.adminOnly !== undefined && window.__ROLE !== 'admin'));
  // 锁屏键也在 btns 里（原来固定在容器外）——见 layout() 的锁屏分支
  const lockBtn = ring.querySelector<HTMLButtonElement>('#lock-mode-btn');

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
  const BTN = 34;        // 按钮直径
  const GAP = 4;         // 间距
  const STEP = BTN + GAP; // 每按钮步进
  // 230 ≈ 顶部安全区 + 收缩键 + 上下箭头 + 标签 + 底部留白
  const CHROME = 230;
  let VISIBLE = 0;       // 见 layout()：必须随窗口尺寸重算

  function computeVisible() {
    return Math.max(3, Math.min(4, Math.floor((window.innerHeight - CHROME) / STEP)));
  }

  /* ---------- 构建滚动轨道 ---------- */
  let track = ring.querySelector<HTMLElement>('.wheel-track');
  if (!track) {
    track = document.createElement('div');
    track.className = 'wheel-track';
    // 把按钮移进轨道
    btns.forEach(b => track!.appendChild(b));
    ring.appendChild(track);
  }

  let cursor = 0;               // 当前高亮按钮索引（光标）
  let index = 0;                 // 可见窗口顶部对应的按钮索引
  let maxIndex = 0;              // 见 layout()：随可见数变化

  /* ---------- 按当前视口重算几何 ---------- */
  // 为什么必须是函数而不是加载时算一次：窗口从最大化切回普通大小、
  // 手机转屏，都会让可用高度变化。旧实现只在加载时算，之后容器高度不变，
  // 多出来的按钮被 overflow:hidden 切在框外 —— 实测 1280x800 下 16 颗里
  // 有 9 颗既看不见也点不着（中心命中测试落到 three-canvas）。
  function layout() {
    // 锁屏时只剩锁屏键可见：可见数收缩到 1，并把光标滚到它身上。
    // 少了这步，track 的 translateY 会把它顶出 overflow:hidden 的容器 ——
    // 看不见也点不着，等于把自己锁在门外。
    if (document.body.classList.contains('locked') && lockBtn) {
      VISIBLE = 1;
      ring.style.height = `${BTN}px`;
      maxIndex = 0;
      // 其余按钮 display:none 不占位，锁屏键渲染在 track 首行 —— 所以位移必须归零，
      // 否则它会被 -index*STEP 顶出容器。cursor 不动，解锁后能回到原位置。
      index = 0;
      applyPos();
      return;
    }
    VISIBLE = computeVisible();
    ring.style.height = `${VISIBLE * BTN + (VISIBLE - 1) * GAP}px`;
    maxIndex = Math.max(0, btns.length - VISIBLE);
    goTo(cursor);
  }

  /* ---------- 应用滚动位置 ---------- */
  function applyPos() {
    track!.style.transform = `translateY(${-index * STEP}px)`;
    updateFront();
  }

  /* ---------- 更新当前扇区高亮 + 标签 ---------- */
  function updateFront() {
    btns.forEach((b, i) => {
      b.classList.toggle('wheel-front', i === cursor);
      b.classList.toggle('wheel-hidden', i < index || i >= index + VISIBLE);
    });
    if (labelEl) {
      const title = btns[cursor].title || btns[cursor].id || '';
      labelEl.textContent = title.split('：')[0];
      labelEl.classList.remove('hidden');
    }
  }

  /* ---------- 滚动到指定索引 ---------- */
  function goTo(i: number) {
    // 锁屏时轮盘只剩锁屏键，滚动会把唯一的解锁入口推出可视区
    if (document.body.classList.contains('locked')) return;
    cursor = Math.max(0, Math.min(btns.length - 1, i));
    // 尽量把 cursor 居中到可见窗口，使光标能遍历到首尾所有按钮
    const half = Math.floor(VISIBLE / 2);
    index = Math.max(0, Math.min(maxIndex, cursor - half));
    applyPos();
  }

  /* ---------- 上/下一颗 ---------- */
  // 基准必须是 cursor 而不是 index：goTo 里 index = clamp(cursor - half)，
  // 光标在首屏（cursor < half）时 index 被钳在 0，拿 index+dir 当目标会让
  // 光标永远停在 1 —— 实测 16 颗按钮 ▼ 只能走到 2 颗就卡死。
  function step(dir: 1 | -1) {
    goTo(cursor + dir);
  }

  upBtn?.addEventListener('click', () => step(-1));
  downBtn?.addEventListener('click', () => step(1));

  /* ---------- 滚轮滚动 ---------- */
  ring.addEventListener('wheel', (e) => {
    e.preventDefault();
    step(e.deltaY > 0 ? 1 : -1);
  }, { passive: false });

  /* ---------- 拖拽滚动（超过阈值才接管指针） ---------- */
  // 关键：绝不能在 pointerdown 里无条件 setPointerCapture。捕获会把后续
  // pointer 事件全部重定向到 ring，浏览器算出的 click target 就成了共同祖先
  // （ring），按钮自己的 click 监听器永远不触发 —— 实测坐标明明命中按钮、
  // 弹窗却不打开，这就是「侧边栏按钮点不动」的主因。
  // 现在只有真的拖动超过半格才捕获；纯点击不捕获，click 正常派发给按钮。
  const DRAG_THRESHOLD = STEP / 2;
  let pointerId = -1;
  let startY = 0;
  let startCursor = 0;
  let dragging = false;

  ring.addEventListener('pointerdown', (e) => {
    pointerId = e.pointerId;
    startY = e.clientY;
    startCursor = cursor;
    dragging = false;
  });
  ring.addEventListener('pointermove', (e) => {
    if (e.pointerId !== pointerId) return;
    const dy = e.clientY - startY;
    if (!dragging) {
      if (Math.abs(dy) < DRAG_THRESHOLD) return;
      dragging = true;
      try { ring.setPointerCapture(pointerId); } catch { /* 指针已抬起 */ }
    }
    goTo(startCursor - Math.round(dy / STEP));
  });
  const endDrag = (e: PointerEvent) => {
    if (e.pointerId !== pointerId) return;
    if (dragging) {
      // 拖动过就吞掉紧随的 click，否则松手时会给途经的按钮误触发一次
      const swallow = (ev: MouseEvent) => { ev.stopPropagation(); ev.preventDefault(); };
      ring.addEventListener('click', swallow, { capture: true, once: true });
    }
    dragging = false;
    pointerId = -1;
  };
  ring.addEventListener('pointerup', endDrag);
  ring.addEventListener('pointercancel', endDrag);

  /* ---------- 初始定位 + 跟随视口 ---------- */
  layout();
  // 锁屏状态由 08_state_switch 切 body.locked（启动时恢复锁屏也走这条路），
  // 轮盘跟着重排：进锁屏收缩到锁屏键，解锁恢复满屏。
  new MutationObserver(() => layout())
    .observe(document.body, { attributes: true, attributeFilter: ['class'] });
  // orientationchange 在部分安卓上早于 innerHeight 更新，统一走 resize
  // （转屏、窗口拖拽、浏览器工具栏收起都会触发 resize）。
  window.addEventListener('resize', () => layout());

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
    layout();
  }

  /* ---------- 暴露给 App（调试/其他模块） ---------- */
  (App as any)._stageWheel = {
    next: () => step(1),
    prev: () => step(-1),
    goTo,
    rebuild,
    get index() { return cursor; },
  };

  console.log(`[StageWheel] 竖排滚动条初始化：${btns.length} 个按钮，可见 ${VISIBLE} 个`);
});
