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

  /** 3D 帧循环与静默态对齐（幂等）：每次调用都按目标状态写一遍，不看旧标志位。
   *  原来的「状态没变就早退」有个致命漂移 —— 若某次调用时 renderer 还没建好
   *  （早期调用、模块顺序变化），标志位已经翻成 true 而帧循环根本没停；之后再调就
   *  早退，永远修不回来。用户看到的就是「点了全屏，模型还在动」。 */
  const alignFrameLoop = (on: boolean) => {
    try {
      const renderer = App.renderer;
      if (!renderer) return;
      if (on) {
        if (!App.xrPresenting) { renderer.setAnimationLoop(null); loopStopped = true; }
      } else if (loopStopped) {
        // 只在确实停过时才恢复：XR 会话期间没停过，就别去抢 three 的帧调度
        renderer.setAnimationLoop(App.animate);
        loopStopped = false;
      }
    } catch { /* three 未就绪：忽略 */ }
  };

  /** 帧循环「刚被谁拉起来」之后必须调它一次（initThree 的最后一行）。
   *  为什么必须有：手机上模型（24MB VRM）加载要好几秒，比用户点 ⤢ 慢得多 ——
   *  setQuiet(true) 那一刻 renderer 还不存在，随后 initThree 无条件
   *  setAnimationLoop(App.animate)，帧循环就在静默态里跑起来了。
   *  实测（真实 initThree 再跑一次 = 帧循环晚到）：全屏静默下 4 秒内仍出帧，
   *  且 quietSnapshot().loopStopped 还停在 true —— 标志位在撒谎，手机一直发烫。 */
  App.syncQuietLoop = function syncQuietLoop() {
    alignFrameLoop(App.chatQuiet);
  };

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
    const changed = on !== App.chatQuiet;
    App.chatQuiet = on;
    document.documentElement.classList.toggle('chat-quiet', on);

    // 1) 3D 帧循环 —— 最大的一块开销。WebXR 会话中帧由头显驱动，不能停。
    alignFrameLoop(on);

    // 2) 任务大屏 iframe 自截图
    notifyBigscreen(on);

    // 3) 各模块自己的定时器 / 采样（热度、数据流、施法、看门狗、任务轮询…）
    //    广播仍只在状态真变化时发：回调虽是幂等的，但重复广播会打断模块自己的
    //    「暂停 → 恢复」成对逻辑（例如施法计时器只在 active>0 时才重启）。
    if (!changed) return;
    for (let i = 0; i < hooks.length; i += 1) {
      try { hooks[i](on); } catch { /* 同上 */ }
    }
  };

  /* 对外只读快照：调试 / 测试用 */
  App.quietSnapshot = function quietSnapshot() {
    return { quiet: App.chatQuiet, loopStopped, hooks: hooks.length };
  };
});
