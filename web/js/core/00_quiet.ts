import type { AppKernel } from '../types/app-kernel.js';

/* ============================================================
 *  聊天全屏静默总闸（html.chat-quiet）
 *
 *  聊天框占满屏幕时，屏幕上看得到的只有对话本身 —— 后台还在跑的一切渲染
 *  都是白烧的 CPU：3D 帧循环（每帧蒙皮/骨骼/材质）、任务大屏 iframe 的
 *  html2canvas 全文档截图、热度火花 / 街机数据流 / 施法计时 / 全息看门狗
 *  采样 / 任务轮询……用户一个都看不见。
 *
 *  这里给它们一个总闸：进全屏 → 全停；退出全屏 → 原样恢复。
 *  与 html.fx-lite（41_holo_stage 的掉帧降档）分工：
 *    fx-lite   = 看得见但少画一点（保画面）
 *    chat-quiet = 根本看不见，一帧都不画（保 CPU）
 *
 *  订阅制：各模块调 App.onQuiet(fn) 注册自己的启停，注册时立刻收到当前
 *  状态 —— 因此谁先谁后加载都正确，不需要在 app.ts 里排依赖顺序。
 * ============================================================ */
export default (function init(App: AppKernel) {
  App.chatQuiet = false;

  const hooks: Array<(on: boolean) => void> = [];
  /** 注册静默回调：立即以当前状态回调一次，之后每次切换广播 */
  App.onQuiet = function onQuiet(fn: (on: boolean) => void) {
    hooks.push(fn);
    try { fn(App.chatQuiet); } catch { /* 单个订阅异常不影响其他订阅 */ }
  };

  let loopStopped = false;

  /** 任务大屏 iframe 在自截图：父页面停帧循环拦不住它，必须显式通知 */
  const notifyBigscreen = (on: boolean) => {
    try {
      const frames = document.querySelectorAll('iframe');
      for (let i = 0; i < frames.length; i += 1) {
        const w = frames[i].contentWindow;
        if (w) w.postMessage({ type: 'bigscreen-pause', on }, '*');
      }
    } catch { /* 跨域 / 已卸载：忽略 */ }
  };

  App.setQuiet = function setQuiet(on: boolean) {
    on = !!on;
    if (on === App.chatQuiet) return;
    App.chatQuiet = on;
    document.documentElement.classList.toggle('chat-quiet', on);

    // 1) 3D 帧循环 —— 最大的一块开销。WebXR 会话中帧由头显驱动，不能停。
    try {
      const renderer = App.renderer;
      if (renderer) {
        if (on) {
          if (!App.xrPresenting) { renderer.setAnimationLoop(null); loopStopped = true; }
        } else if (loopStopped) {
          renderer.setAnimationLoop(App.animate);
          loopStopped = false;
        }
      }
    } catch { /* three 未就绪：忽略 */ }

    // 2) 任务大屏 iframe 自截图
    notifyBigscreen(on);

    // 3) 各模块自己的定时器 / 采样（热度、数据流、施法、看门狗、任务轮询…）
    for (let i = 0; i < hooks.length; i += 1) {
      try { hooks[i](on); } catch { /* 同上 */ }
    }
  };

  /* 对外只读快照：调试 / 测试用 */
  App.quietSnapshot = function quietSnapshot() {
    return { quiet: App.chatQuiet, loopStopped, hooks: hooks.length };
  };
});
