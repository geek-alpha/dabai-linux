import type { AppKernel } from '../types/app-kernel.js';

export default (function init(App: AppKernel) {
  /* ============================================================
   *  内联工具块 —— 编程工具式呈现
   *  工具调用以可展开的小块内联在对应正文段后面：
   *  头部 = 图标 + 工具名 + 状态；点击展开可见参数/结果/详细步骤。
   *  不叫「工具链」、不编号；完整执行状态仍投递给任务直播大屏。
   * ============================================================ */

  const TOOL_ICONS: Record<string, string> = {
    web_search: '🌐', search: '🔍', read: '📄', read_image: '🖼️', write: '✏️', edit: '📝',
    glob: '📂', grep: '🔎', pwsh: '💻', run_code: '⚙️',
    subagent: '🧩', subagent_fork: '🧬', send_message: '💬', list_agents: '👥', interrupt_agent: '✋',
    workflow: '🛠️', ralph: '🌀', skill: '📚', exit_plan_mode: '🧭',
    create_goal: '🎯', update_goal: '🎯', get_goal: '🎯', ask_user_question: '❓', todo_write: '✅',
    job_list: '📋', job_output: '📜', job_kill: '⏹️', fetch: '🛠️', evaluate: '🧮', remove: '✂️',
    codex: '🤖', ai: '🧑‍💻', image_gen: '🎨', video_gen: '🎬', music_play: '🎵', browser: '🌐'
  };
  function toolIcon(name: string) {
    const k = String(name || '');
    return TOOL_ICONS[k] || TOOL_ICONS[k.split('_')[0]] || '🔧';
  }
  function toolLabel(name: string) {
    if (!name) return '工具';
    return String(name).replace(/_/g, ' ');
  }

  function fmtArg(a: any) {
    if (a == null || a === '') return '（无参数）';
    let s = typeof a === 'string' ? a : JSON.stringify(a, null, 2);
    return s.trim() || '（无参数）';
  }
  function fmtResult(r: any) {
    if (r == null) return '（无返回）';
    return String(r).trim() || '（无返回）';
  }
  /** 头部摘要：一行显示工具大概执行了什么 */
  function fmtArgShort(a: any) {
    if (a == null || a === '') return '';
    let s = typeof a === 'string' ? a : JSON.stringify(a);
    s = s.replace(/\s+/g, ' ').trim();
    if (!s || s === '{}' || s === '[]') return '';
    return s.length > 64 ? s.slice(0, 64) + '…' : s;
  }

  interface ToolStep {
    name: string;
    status: string;
    success: boolean | undefined;
    el: HTMLElement;
    startedAt: number;   // 起始时间戳：交给游戏化层算耗时评级
  }

  let round = 0;                    // 当前轮次（thinking 时 +1）
  let steps: ToolStep[] = [];       // 当前轮次步骤记录
  let startAt = 0;

  function ensureBubble(): HTMLElement | null {
    let b = App._turnMsgEl;
    if (App.beginTurnBubble && (!b || !document.body.contains(b))) {
      b = App.beginTurnBubble();
    }
    return b;
  }

  /** 插入内联工具块：封存当前正文段，块追加在其后 */
  function addToolBlock(name: string, status: string): HTMLElement | null {
    const b = ensureBubble();
    if (!b) return null;
    if (App.sealTurnSeg) App.sealTurnSeg();
    const d = document.createElement('div');
    d.className = 'tool-inline ' + status;
    d.innerHTML =
      '<button type="button" class="tool-inline-head" aria-expanded="false">' +
        '<span class="tool-inline-ic"></span>' +
        '<span class="tool-inline-name"></span>' +
        '<span class="tool-inline-summary"></span>' +
        '<span class="tool-inline-state"></span>' +
        '<span class="tool-inline-caret">▸</span>' +
      '</button>' +
      '<div class="tool-inline-body" hidden>' +
        '<div class="tool-inline-args"></div>' +
        '<div class="tool-inline-result"></div>' +
      '</div>';
    (d.querySelector('.tool-inline-ic') as HTMLElement).textContent = toolIcon(name);
    (d.querySelector('.tool-inline-name') as HTMLElement).textContent = toolLabel(name);
    const state = d.querySelector('.tool-inline-state') as HTMLElement;
    state.innerHTML = '<span class="turn-spin">⟳</span>执行中';
    // 点击头部展开/收起详细内容
    const head = d.querySelector('.tool-inline-head') as HTMLElement;
    const body = d.querySelector('.tool-inline-body') as HTMLElement;
    head.addEventListener('click', () => {
      const open = !!body.hidden;
      body.hidden = !open;
      head.setAttribute('aria-expanded', String(open));
      d.classList.toggle('open', open);
      const caret = d.querySelector('.tool-inline-caret') as HTMLElement | null;
      if (caret) caret.textContent = open ? '▾' : '▸';
    });
    b.appendChild(d);
    return d;
  }

  function setState(st: ToolStep, status: string) {
    st.status = status;
    const state = st.el.querySelector('.tool-inline-state') as HTMLElement | null;
    if (!state) return;
    if (status === 'done') state.textContent = '✓ 完成';
    else if (status === 'error') state.textContent = '✗ 失败';
    else if (status === 'ended') state.textContent = '· 已结束';
    else state.innerHTML = '<span class="turn-spin">⟳</span>执行中';
    st.el.className = 'tool-inline ' + status;
  }

  // ---------- 对外 API ----------

  /** 新一轮回复开始：上一轮收尾 + 轮次 +1 */
  App.toolChainBeginTurn = function toolChainBeginTurn() {
    finalizePrevious();
    round += 1;
    steps = [];
    startAt = Date.now();
    if (App.game) App.game.onRoundStart();
  };

  /** 会话切换/历史恢复：清空记录 */
  App.toolChainReset = function toolChainReset() {
    steps = [];
    round = 0;
    startAt = 0;
    if (App.game) App.game.onRoundStart();
  };

  /** 工具调用开始：封存当前正文段并插入内联工具块 */
  App.toolChainStart = function toolChainStart(toolName: string, args: any, _toolDesc?: string) {
    // _toolDesc 由后端推送（工具自己的 description）。这里只做透传签名兼容 ——
    // 内联工具块按设计只显示工具名，说明交给屏幕中央的字幕（见 39_cast_fx）。
    const d = addToolBlock(toolName || '工具', 'running');
    if (!d) return;
    const st: ToolStep = { name: toolName || '工具', status: 'running', success: undefined, el: d, startedAt: Date.now() };
    steps.push(st);
    const argsEl = d.querySelector('.tool-inline-args') as HTMLElement | null;
    if (argsEl) argsEl.textContent = '参数\n' + fmtArg(args);
    const sumEl = d.querySelector('.tool-inline-summary') as HTMLElement | null;
    if (sumEl) sumEl.textContent = fmtArgShort(args);
    if (App.game) App.game.onToolStart(d);
    App.scrollToBottom();
    pushToBoard();
  };

  /** 工具执行心跳：仅更新执行中状态（不展示耗时，保持简洁） */
  App.toolChainProgress = function toolChainProgress(_toolName: string, _elapsed: number, _message?: string) {};

  /** 工具调用结果：回填到对应内联块 */
  App.toolChainResult = function toolChainResult(toolName: string, result: any, success: boolean) {
    let target: ToolStep | null = null;
    for (let i = steps.length - 1; i >= 0; i--) {
      const s = steps[i];
      if (s.status === 'running' && (!toolName || s.name === toolName)) { target = s; break; }
    }
    if (!target) {
      // 孤立结果：补一个只读内联块
      const d = addToolBlock(toolName || '工具', 'running');
      if (!d) return;
      target = { name: toolName || '工具', status: 'running', success: undefined, el: d, startedAt: Date.now() };
      steps.push(target);
    }
    target.success = success !== false;
    setState(target, success === false ? 'error' : 'done');
    const resEl = target.el.querySelector('.tool-inline-result') as HTMLElement | null;
    if (resEl) resEl.textContent = '结果\n' + fmtResult(result);
    // 游戏化层：评级 / 连击 / 暴击 / 经验结算（耗时 = 本次调用真实用时）
    if (App.game) App.game.onToolResult(target.el, success !== false, Date.now() - target.startedAt);
    App.scrollToBottom();
    pushToBoard();
  };

  /** 委派子任务：下钻入口由任务中心承载，聊天框不再挂载 */
  App.codexLinkTask = function codexLinkTask(_toolName: string, _taskId: string) {};

  /** 整轮收尾（audio_end：回复完成） */
  App.toolChainEndTurn = function toolChainEndTurn() {
    for (let i = 0; i < steps.length; i++) {
      if (steps[i].status === 'running') setState(steps[i], 'ended');
    }
    if (App.finishTurn) App.finishTurn(false);
    if (App.game) App.game.onRoundEnd();
    pushToBoard();
    App.scrollToBottom();
  };

  /** 被打断 / 出错 */
  App.toolChainAbort = function toolChainAbort() {
    for (let i = 0; i < steps.length; i++) {
      if (steps[i].status === 'running') setState(steps[i], 'ended');
    }
    if (App.finishTurn) App.finishTurn(true);
    if (App.game) App.game.onRoundEnd();
    pushToBoard();
  };

  // ---------- 内部 ----------

  function finalizePrevious() {
    for (let i = 0; i < steps.length; i++) {
      if (steps[i].status === 'running') setState(steps[i], 'ended');
    }
  }

  // ---------- 投递到直播大屏 ----------
  function pushToBoard() {
    if (!App.taskBoardOnToolChain) return;
    App.taskBoardOnToolChain({
      round,
      title: round > 0 ? '对话 · 第 ' + round + ' 轮' : '智能体 · 实时工具链',
      steps: steps.map(s => ({ name: s.name, status: s.status })),
      ts: Date.now()
    });
  }
});
