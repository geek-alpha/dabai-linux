import type { AppKernel } from '../types/app-kernel.js';

/* ============================================================
 *  底部栏高度同步（36_layout_sync）
 *
 *  解决的问题：#chat-panel 与 #controls 都是 position:fixed，
 *  CSS 无法让它们互相感知尺寸，于是 #chat-panel 的 bottom 只能写死
 *  62px。但 #text-input 是自增高 textarea（最高 140px），输入栏实际
 *  高度在 67~165px 之间浮动 → 写死的高度必然重叠：单行叠 5px，
 *  打多行时叠 100px 以上，最后一条消息和输入框会糊在一起。
 *
 *  做法：用 ResizeObserver 把输入栏实测高度写进 CSS 变量 --controls-h，
 *  CSS 侧用 var(--controls-h, <原估算值>) 消费。变量缺失时优雅回退，
 *  所以这个模块即使加载失败，页面也只会退回改动前的表现，不会更糟。
 * ============================================================ */

export default function init(App: AppKernel) {
  const controls = document.getElementById('controls');
  if (!controls) return;

  const root = document.documentElement;

  const sync = () => {
    // offsetHeight 不受 transform 影响：输入栏收起时（translateY(100%)）也能测到真实高度
    const h = controls.offsetHeight;
    if (h > 0) root.style.setProperty('--controls-h', h + 'px');
  };

  sync();

  if (typeof ResizeObserver !== 'undefined') {
    // 输入框自增高 / 窗口缩放 / 安全区变化，都会触发
    const ro = new ResizeObserver(sync);
    ro.observe(controls);
  }
  // 视口高度变化会重算 dvh：面板底边让位（--controls-h）要重算
  window.addEventListener('resize', sync);

  // 字体异步加载完成后行高会变，高度可能再跳一次，补一次同步
  const fonts = (document as any).fonts;
  if (fonts && fonts.ready && typeof fonts.ready.then === 'function') {
    fonts.ready.then(sync).catch(() => {});
  }

  App.syncBottomBar = sync;
}
