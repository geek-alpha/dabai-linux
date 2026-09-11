import type { AppKernel } from '../types/app-kernel.js';

/* ============================================================
 *  本轮对话时钟（40_turn_clock）
 *
 *  【为什么单独做一个模块】
 *  「一轮对话用了多久」这件事，之前有三个不同的答案：
 *    · 热度 HUD 的计时器 → 从**页面加载**算起，跟对话无关
 *    · 施法态秒表       → 从**单个工具开始**算起，工具结束就归零
 *    · 工具链内部       → startAt 从没被读出来用过
 *  三个都不等于「本轮对话耗时」，用户看到的自然「不准」。
 *
 *  【第一性原理】一轮对话只有两个真实端点：开始与结束。
 *  所以这里只做一件事：在轮开始时记一个单调时钟，轮结束时减一下。
 *  用 performance.now() 而非 Date.now()：后者会被系统对时/夏令时拨动，
 *  长时间运行必然漂移；前者是单调时钟，只增不减。
 *
 *  【显示规则】不足 60s 显示 12.3s（精度到 0.1 秒，看得出快慢）；
 *  超过 60s 显示 1:23（长度可控）。运行中实时走，结束后定格成本轮最终值。
 * ============================================================ */

export default function init(App: AppKernel) {
  let startedAt = 0;
  let lastMs = 0;
  let running = false;
  let turn = 0;

  // 状态变化回调：HUD 注册它，就能在开始/结束时**立刻**刷新，
  // 不必等下一个 1s tick（否则「已结束」的样式会迟一秒才出现）。
  let onChangeCb: ((running: boolean) => void) | null = null;
  const notify = () => {
    if (!onChangeCb) return;
    try { onChangeCb(running); } catch { /* 回调出错不影响时钟 */ }
  };

  const start = () => {
    // 新一轮开始：无条件重置。
    // 上一轮若因异常没收到 endTurn，这里就是兜底——宁可从头算，也不能累加。
    startedAt = performance.now();
    running = true;
    turn += 1;
    notify();
  };

  const stop = () => {
    if (!running) return;      // 重复 endTurn 不该把时间清零
    lastMs = performance.now() - startedAt;
    running = false;
    notify();
  };

  /** 当前已耗时 ms：运行中实时算，结束后定格 */
  const elapsed = () => (running ? performance.now() - startedAt : lastMs);

  /** 格式化：<60s 走 0.1 秒精度，>=60s 走 m:ss */
  const fmt = (ms?: number): string => {
    const v = Math.max(0, ms === undefined ? elapsed() : ms);
    if (v < 60000) return (v / 1000).toFixed(1) + 's';
    const total = Math.floor(v / 1000);
    return Math.floor(total / 60) + ':' + String(total % 60).padStart(2, '0');
  };

  /* ---------- 装饰工具链的三个端点（不改 31_tool_chain 一行） ---------- */
  if (App.toolChainBeginTurn) {
    const orig = App.toolChainBeginTurn;
    App.toolChainBeginTurn = function patched() {
      orig.call(this);
      try { start(); } catch { /* 时钟出错不能拖垮工具链 */ }
    };
  }

  if (App.toolChainEndTurn) {
    const orig = App.toolChainEndTurn;
    App.toolChainEndTurn = function patched() {
      orig.call(this);
      try { stop(); } catch { /* 同上 */ }
    };
  }

  if (App.toolChainAbort) {
    const orig = App.toolChainAbort;
    App.toolChainAbort = function patched() {
      orig.call(this);
      try { stop(); } catch { /* 同上 */ }
    };
  }

  App.turnClock = {
    start,
    stop,
    /** 当前已耗时 ms */
    get elapsed() { return elapsed(); },
    /** 是否正在计时 */
    get running() { return running; },
    /** 第几轮 */
    get turn() { return turn; },
    /** 上一轮最终耗时 ms */
    get last() { return lastMs; },
    /** 格式化（不传参=当前值） */
    fmt,
    /** 状态变化回调：开始/结束时立刻触发，供 HUD 即时刷新 */
    get onChange() { return onChangeCb; },
    set onChange(cb: ((running: boolean) => void) | null) { onChangeCb = cb; },
    /** 快照（调试 / 单测用） */
    snapshot: () => ({ running, elapsed: elapsed(), last: lastMs, turn }),
  };
}
