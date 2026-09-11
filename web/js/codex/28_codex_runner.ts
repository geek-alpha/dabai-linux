import type { AppKernel } from '../types/app-kernel.js';

export default (function init(App: AppKernel) {
  /* ============================================================
   *  Codex / OpenCode 委派进度渲染 —— 可下钻执行明细链
   *  外部智能体（codex / opencode）作为主 Agent 的委派工具运行，
   *  后端把 codex_logs/<task_id>.log 实时解析成带全局 seq 的结构化
   *  条目（header/turn/tool/args/out/tool_end/log），通过 codex_log
   *  事件增量推送；本卡片按 seq 增量渲染成可逐级展开的明细树：
   *    任务 → 对话轮 → 工具步骤（参数/输出/耗时）→ 原始日志全文。
   *  seq 断档时自动经 /api/tasks/<id>/trace 回补；刷新/重连后全量
   *  同步，保证展示与后台真实日志文件一致（实时、准确、可追溯）。
   * ============================================================ */

  // ---------- 工具 ----------
  function esc(s) {
    const d = document.createElement('div');
    d.textContent = s == null ? '' : String(s);
    return d.innerHTML;
  }

  function fmtElapsed(sec) {
    sec = Math.max(0, sec | 0);
    const m = Math.floor(sec / 60);
    const s = sec % 60;
    return m ? `${m}分${s}秒` : `${s}秒`;
  }

  function fmtDur(ms) {
    if (ms == null) return '';
    return ms < 1000 ? `${ms}ms` : `${(ms / 1000).toFixed(1)}s`;
  }

  const TOOL_ICONS = {
    exec: '💻', bash: '💻', pwsh: '💻', shell: '💻', cmd: '💻', run_code: '⚙️',
    read: '📄', read_image: '🖼️', view: '👁️', list: '📋',
    write: '✏️', edit: '📝', apply_patch: '🩹', remove: '✂️', rename: '📛', copy: '📑',
    grep: '🔎', glob: '📂', search: '🔍', fetch: '🛠️', evaluate: '🧮',
    web_search: '🌐', web_fetch: '🌐',
    subagent: '🧩', subagent_fork: '🧬', send_message: '💬', list_agents: '👥', interrupt_agent: '✋',
    workflow: '🛠️', ralph: '🌀', skill: '📚', exit_plan_mode: '🧭',
    create_goal: '🎯', update_goal: '🎯', get_goal: '🎯', ask_user_question: '❓', todo_write: '✅',
    job_list: '📋', job_output: '📜', job_kill: '⏹️', plan: '🗺️', mcp: '🔌'
  };
  function toolIcon(name) { return TOOL_ICONS[name] || '🔧'; }
  function toolLabel(name) { return String(name || '工具').replace(/_/g, ' '); }

  // 挂到卡片的复制按钮：复制关键结果而不是整卡文本
  function setCardCopyText(card, text) {
    if (!card || text == null) return;
    if (App.ensureCopyBtn) App.ensureCopyBtn(card);
    const btn = card.querySelector('.msg-copy-btn');
    if (btn) btn.dataset.copyText = String(text);
  }

  const activeCards = new Map(); // key = tool+label -> element

  function ensureCard(tool, label) {
    const key = `${tool}:${label}`;
    let card = activeCards.get(key);
    if (card && document.body.contains(card)) return card;
    card = document.createElement('div');
    card.className = 'msg codex-card';
    card.dataset.codexKey = key;
    card.innerHTML = `
      <div class="codex-head">
        <span class="codex-icon">⚙️</span>
        <span class="codex-label">${esc(label)}</span>
        <span class="codex-tool">${esc(tool)}</span>
        <span class="codex-spinner"></span>
        <span class="codex-elapsed">0秒</span>
      </div>
      <div class="codex-task"></div>
      <div class="codex-status"></div>
      <div class="codex-counters" style="display:none"></div>
      <details class="codex-trace">
        <summary>
          <span class="codex-trace-title">🔬 执行明细</span>
          <span class="codex-trace-latest"></span>
        </summary>
        <div class="codex-trace-steps"></div>
      </details>
      <details class="codex-proc" style="display:none">
        <summary>
          <span class="codex-proc-title">📜 原始日志</span>
          <span class="codex-proc-latest"></span>
          <button type="button" class="codex-full-log">加载完整日志</button>
        </summary>
        <pre class="codex-pre"></pre>
      </details>
      <div class="codex-result" style="display:none"></div>
      <details class="codex-details codex-raw" style="display:none">
        <summary>查看原始输出</summary>
        <pre class="codex-pre"></pre>
      </details>
    `;
    card.__cx = {
      taskId: '',
      seq: 0,
      toolCount: 0,
      counts: { steps: 0, lines: 0 },
      steps: [],          // 已结构化的工具步骤视图
      raw: [],            // 原始日志行（内存，前端展示封顶）
      timer: null,
      startedAt: Date.now(),
      turnRole: ''
    };
    App.messagesEl.appendChild(card);
    App._trimMessages();
    App.bumpNewMsg(card);
    App.scrollToBottom();
    activeCards.set(key, card);
    return card;
  }

  function removeCard(tool, label) {
    const key = `${tool}:${label}`;
    const c = activeCards.get(key);
    if (c && c.__cx && c.__cx.timer) { clearInterval(c.__cx.timer); c.__cx.timer = null; }
    if (c) activeCards.delete(key);
  }

  function resetCard(card, taskId) {
    const st = card.__cx;
    if (st.timer) { clearInterval(st.timer); st.timer = null; }
    st.taskId = taskId || '';
    st.seq = 0;
    st.toolCount = 0;
    st.counts = { steps: 0, lines: 0 };
    st.steps = [];
    st.raw = [];
    st.turnRole = '';
    st.startedAt = Date.now();
    const trace = card.querySelector('.codex-trace-steps');
    if (trace) trace.innerHTML = '';
    const rawPre = card.querySelector('.codex-proc .codex-pre');
    if (rawPre) rawPre.textContent = '';
    const counters = card.querySelector('.codex-counters');
    if (counters) { counters.style.display = 'none'; counters.textContent = ''; }
    const proc = card.querySelector('.codex-proc');
    if (proc) proc.style.display = 'none';
    const traceDet = card.querySelector('.codex-trace');
    if (traceDet) traceDet.open = true;
    card.querySelector('.codex-elapsed').textContent = '0秒';
  }

  // ---------- 明细渲染 ----------

  function renderCounters(card) {
    const st = card.__cx;
    const el = card.querySelector('.codex-counters');
    if (!el) return;
    const steps = Math.max(st.counts.steps, st.toolCount);
    if (!steps) { el.style.display = 'none'; return; }
    el.style.display = '';
    el.textContent = `已执行 ${steps} 步 · ${st.counts.lines || st.seq || 0} 条日志 · 用时 ${fmtElapsed((Date.now() - st.startedAt) / 1000)}`;
  }

  function renderLatest(card) {
    const st = card.__cx;
    const el = card.querySelector('.codex-trace-latest');
    if (!el) return;
    const last = st.steps.length ? st.steps[st.steps.length - 1] : null;
    el.textContent = last
      ? `最新：${toolLabel(last.e.tool)} ${last.status === 'running' ? '运行中' : (last.dur_ms != null ? fmtDur(last.dur_ms) : last.status)}`
      : '';
    el.title = el.textContent;
  }

  function stepBadge(step) {
    if (step.status === 'running') return '运行中';
    if (step.status === 'success') return '✓ ' + fmtDur(step.dur_ms);
    if (step.status === 'error') return '✗ ' + fmtDur(step.dur_ms);
    return step.status || '已结束';
  }

  function createStepView(card, e) {
    const st = card.__cx;
    const wrap = document.createElement('div');
    wrap.className = 'cx-step running open';
    wrap.innerHTML =
      '<div class="cx-step-head">' +
        '<span class="cx-step-idx"></span>' +
        '<span class="cx-step-icon"></span>' +
        '<span class="cx-step-name"></span>' +
        '<span class="cx-step-badge">运行中</span>' +
      '</div>' +
      '<div class="cx-step-args"></div>' +
      '<div class="cx-step-out"></div>';
    wrap.querySelector('.cx-step-idx').textContent = '#' + (e.idx != null ? e.idx : st.steps.length + 1);
    wrap.querySelector('.cx-step-icon').textContent = toolIcon(e.tool);
    const nameEl = wrap.querySelector('.cx-step-name') as HTMLElement;
    nameEl.textContent = toolLabel(e.tool);
    nameEl.title = e.tool;
    const step = { e, args: [], out: [], status: 'running', dur_ms: null, el: wrap, open: true };
    wrap.addEventListener('click', (ev) => {
      if ((ev.target as HTMLElement).closest('.codex-full-log')) return;
      step.open = !step.open;
      wrap.classList.toggle('open', step.open);
    });
    // 折叠上一个步骤，让最新动作始终可见
    if (st.steps.length) {
      const prev = st.steps[st.steps.length - 1];
      prev.open = false;
      prev.el.classList.remove('open');
    }
    st.steps.push(step);
    card.querySelector('.codex-trace-steps').appendChild(wrap);
    while (st.steps.length > 80) {
      const old = st.steps.shift();
      if (old.el.parentNode) old.el.parentNode.removeChild(old.el);
    }
    // 首次出现工具步骤 → 打开明细区
    const traceDet = card.querySelector('.codex-trace');
    if (traceDet && !traceDet.open) traceDet.open = true;
    renderCounters(card);
    renderLatest(card);
  }

  function setStepState(step, status, dur_ms) {
    step.status = status;
    step.dur_ms = dur_ms != null ? dur_ms : step.dur_ms;
    step.el.className = 'cx-step ' + status + (step.open ? ' open' : '');
    const badge = step.el.querySelector('.cx-step-badge');
    if (badge) badge.textContent = stepBadge(step);
  }

  function lastOpenStep(st, toolName) {
    for (let i = st.steps.length - 1; i >= 0; i--) {
      const s = st.steps[i];
      if (s.status !== 'running') continue;
      if (!toolName || s.e.tool === toolName) return s;
    }
    for (let i = st.steps.length - 1; i >= 0; i--) {
      if (st.steps[i].status === 'running') return st.steps[i];
    }
    return st.steps.length ? st.steps[st.steps.length - 1] : null;
  }

  function appendStepText(step, kind, text) {
    if (!text) return;
    if (kind === 'args') {
      step.args.push(text);
      const box = step.el.querySelector('.cx-step-args');
      if (box) {
        box.textContent = '参数 ' + (step.args.length ? step.args[step.args.length - 1] : text);
        box.title = step.args.join('\n');
      }
    } else {
      step.out.push(text);
      if (step.out.length > 300) step.out.splice(0, step.out.length - 300);
      const box = step.el.querySelector('.cx-step-out');
      if (box) {
        const tail = step.out.slice(-30);
        box.textContent = tail.join('\n');
        box.title = step.out.join('\n');
      }
    }
  }

  function appendRaw(card, text) {
    const st = card.__cx;
    if (text == null || text === '') return;
    st.raw.push(text);
    if (st.raw.length > 3000) st.raw.splice(0, st.raw.length - 3000);
    const proc = card.querySelector('.codex-proc');
    proc.style.display = '';
    const pre = proc.querySelector('.codex-pre');
    pre.textContent = st.raw.join('\n');
    const title = proc.querySelector('.codex-proc-title');
    if (title) title.textContent = '📜 原始日志 · ' + st.raw.length + ' 行';
    const latestEl = proc.querySelector('.codex-proc-latest');
    if (latestEl) {
      const last = st.raw[st.raw.length - 1] || '';
      latestEl.textContent = last.length > 60 ? last.slice(0, 60) + '…' : last;
      latestEl.title = last;
    }
  }

  function applyTurn(card, e) {
    const st = card.__cx;
    const role = e.role || '';
    if (!role || role === st.turnRole) return;
    st.turnRole = role;
    const turn = document.createElement('div');
    turn.className = 'cx-turn';
    const icon = role === 'user' ? '👤' : role === 'codex' ? '🤖' : '✦';
    turn.textContent = `${icon} ${role}`;
    card.querySelector('.codex-trace-steps').appendChild(turn);
  }

  // 逐条应用结构化条目（seq 去重，工具/参数/输出/结束 归并为步骤）
  function applyEntries(card, entries) {
    const st = card.__cx;
    for (const e of entries || []) {
      if (!e || !e.seq) continue;
      if (e.seq <= st.seq) continue;
      st.seq = e.seq;
      const type = e.type;
      if (type === 'tool') {
        st.toolCount = Math.max(st.toolCount, e.idx || st.toolCount + 1);
        st.counts.steps = Math.max(st.counts.steps, e.idx || st.toolCount);
        createStepView(card, e);
        appendRaw(card, e.text);
      } else if (type === 'args') {
        const last = lastOpenStep(st, e.tool);
        if (last) appendStepText(last, 'args', e.text);
        appendRaw(card, e.text);
      } else if (type === 'out') {
        const last = lastOpenStep(st, e.tool);
        if (last) appendStepText(last, 'out', e.text);
        appendRaw(card, e.text);
      } else if (type === 'tool_end') {
        // 找最近一个未结束（名字优先匹配）的步骤
        let target = null;
        for (let i = st.steps.length - 1; i >= 0; i--) {
          const s = st.steps[i];
          if (s.status !== 'running') continue;
          if (s.e.tool === e.tool) { target = s; break; }
        }
        if (!target) {
          for (let i = st.steps.length - 1; i >= 0; i--) {
            if (st.steps[i].status === 'running') { target = st.steps[i]; break; }
          }
        }
        if (target) setStepState(target, e.status || 'error', e.dur_ms);
        appendRaw(card, e.text);
      } else if (type === 'turn') {
        applyTurn(card, e);
        appendRaw(card, e.text);
      } else {
        if (e.role) applyTurn(card, e);
        appendRaw(card, e.text);
      }
    }
    st.counts.lines = Math.max(st.counts.lines, st.seq);
    renderCounters(card);
    renderLatest(card);
    if (App.scrollToBottom && document.visibilityState === 'visible') App.scrollToBottom();
  }

  // ---------- 与后台对齐：trace 接口回补 / 全量同步 ----------

  async function fetchTrace(taskId, after, limit) {
    try {
      const r = await fetch('/api/tasks/' + encodeURIComponent(taskId) + '/trace?after=' + (after | 0) + '&limit=' + (limit || 2000));
      const data = await r.json();
      return data && data.ok ? data : null;
    } catch (e) { return null; }
  }

  async function backfillTrace(card) {
    const st = card.__cx;
    if (!st || !st.taskId) return;
    let guard = 0;
    while (guard++ < 30) {
      const data = await fetchTrace(st.taskId, st.seq, 2000);
      if (!data || !data.entries || !data.entries.length) break;
      applyEntries(card, data.entries);
      if (data.entries.length < 2000) break;
    }
    if (st.counts.steps < dataSteps(st)) st.counts.steps = dataSteps(st);
    renderCounters(card);
  }

  function dataSteps(st) { return st.steps.length; }

  async function syncTrace(card) {
    const st = card.__cx;
    if (!st || !st.taskId) return;
    resetCard(card, st.taskId);
    let guard = 0;
    while (guard++ < 60) {
      const data = await fetchTrace(st.taskId, st.seq, 2000);
      if (!data || !data.entries || !data.entries.length) break;
      applyEntries(card, data.entries);
      if (data.entries.length < 2000) break;
    }
  }

  async function loadFullLog(card) {
    const st = card.__cx;
    if (!st || !st.taskId) return;
    const btn = card.querySelector('.codex-full-log');
    if (btn) { btn.disabled = true; btn.textContent = '加载中…'; }
    try {
      const r = await fetch('/api/tasks/' + encodeURIComponent(st.taskId) + '/log?offset=0&max_lines=5000');
      const data = await r.json();
      const pre = card.querySelector('.codex-proc .codex-pre');
      if (data && data.ok && pre) {
        const lines = data.lines || [];
        pre.textContent = lines.join('\n') + (data.truncated || lines.length >= 5000 ? '\n…（已截断，文件总大小 ' + data.size + ' 字节，可再次点击加载最新）' : '');
      }
    } catch (e) { /* keep */ }
    if (btn) { btn.disabled = false; btn.textContent = '加载完整日志'; }
  }

  // ---------- 计时器（无事件时也持续刷新耗时） ----------
  function startTimer(card) {
    const st = card.__cx;
    if (st.timer) return;
    st.timer = setInterval(() => {
      if (!document.body.contains(card)) { clearInterval(st.timer); st.timer = null; return; }
      card.querySelector('.codex-elapsed').textContent = fmtElapsed((Date.now() - st.startedAt) / 1000);
      renderCounters(card);
    }, 1000);
  }
  function stopTimer(card) {
    const st = card.__cx;
    if (st.timer) { clearInterval(st.timer); st.timer = null; }
  }

  // 结论性结果：常驻展示（媒体链接内联渲染），并支持一键复制
  function showCardResult(card, text, ok) {
    const res = card.querySelector('.codex-result');
    res.style.display = '';
    res.classList.toggle('err', ok === false);
    if (App.renderMsgMedia) App.renderMsgMedia(res, String(text || '(无输出)'));
    else res.textContent = String(text || '(无输出)');
    setCardCopyText(card, text || '');
  }

  function finalizeCard(card, statusText, cls, ok) {
    const st = card.__cx;
    stopTimer(card);
    card.querySelector('.codex-spinner').style.display = 'none';
    const statusEl = card.querySelector('.codex-status');
    statusEl.textContent = statusText;
    statusEl.className = 'codex-status ' + cls;
    for (const s of st.steps) {
      if (s.status === 'running') setStepState(s, 'ended', null);
    }
    renderLatest(card);
    renderCounters(card);
  }

  // ---------- 通用消息气泡 ----------
  App.addCodexMsg = function addCodexMsg(text, kind) {
    const el = document.createElement('div');
    const isError = kind === 'error';
    el.className = 'msg codex-msg' + (isError ? ' codex-error' : '');
    const pre = document.createElement('pre');
    pre.className = 'codex-pre-inline';
    pre.textContent = text;
    el.appendChild(pre);
    App.messagesEl.appendChild(el);
    App._trimMessages();
    App.bumpNewMsg(el);
    App.scrollToBottom();
    App.notifyFullscreenChat();
  };

  // ---------- 事件处理（供 handleWSMessage 扩展调用） ----------
  App.handleCodexMessage = function handleCodexMessage(msg) {
    if (App.removeTyping) App.removeTyping();
    if (App.setState && App.State && App.currentState === App.State.THINKING) {
      try { App.setState(App.State.IDLE); } catch (e) {}
    }
    switch (msg.type) {
      case 'codex_msg': {
        App.addCodexMsg(msg.text, msg.kind);
        break;
      }
      case 'codex_start': {
        const card = ensureCard(msg.tool, msg.label);
        resetCard(card, msg.task_id || '');
        const taskEl = card.querySelector('.codex-task');
        taskEl.textContent = msg.task || '';
        taskEl.title = msg.task || '';
        const workDir = msg.work_dir ? `工作目录 ${msg.work_dir} · 最长${Math.floor((msg.timeout_sec || 900) / 60)}分钟` : '';
        const statusEl = card.querySelector('.codex-status');
        statusEl.textContent = `⏳ 已交给 ${msg.label}（${workDir}）`;
        statusEl.className = 'codex-status running';
        card.querySelector('.codex-spinner').style.display = 'inline-block';
        startTimer(card);
        if (msg.task_id) {
          syncTrace(card); // 刷新/重连/恢复后：先与后台全量对齐再增量
          if (App.codexLinkTask) App.codexLinkTask(msg.tool, msg.task_id);
        }
        App.bumpNewMsg(card);
        App.scrollToBottom();
        break;
      }
      case 'codex_log': {
        const card = ensureCard(msg.tool, msg.label);
        const st = card.__cx;
        if (msg.task_id && st.taskId && msg.task_id !== st.taskId) resetCard(card, msg.task_id);
        if (msg.task_id && !st.taskId) st.taskId = msg.task_id;
        st.counts.steps = Math.max(st.counts.steps, msg.steps_total || 0);
        st.counts.lines = Math.max(st.counts.lines, msg.lines_total || 0);
        startTimer(card);
        const entries = (msg.entries || []).filter(e => e && e.seq > st.seq);
        if (entries.length) {
          if ((msg.base_seq || 0) > st.seq + 1) {
            // 断档：先回补，再应用本批（applyEntries 会按 seq 去重）
            backfillTrace(card).then(() => applyEntries(card, entries));
          } else {
            applyEntries(card, entries);
          }
        }
        const statusEl = card.querySelector('.codex-status');
        statusEl.textContent = `🔄 进行中 ${fmtElapsed(msg.elapsed)}…`;
        statusEl.className = 'codex-status running';
        card.querySelector('.codex-elapsed').textContent = fmtElapsed(msg.elapsed);
        break;
      }
      case 'codex_progress': {
        const card = ensureCard(msg.tool, msg.label);
        const st = card.__cx;
        if (msg.task_id && st.taskId && msg.task_id !== st.taskId) resetCard(card, msg.task_id);
        st.counts.steps = Math.max(st.counts.steps, msg.steps_total || 0);
        st.counts.lines = Math.max(st.counts.lines, msg.lines_total || 0);
        startTimer(card);
        card.querySelector('.codex-elapsed').textContent = fmtElapsed(msg.elapsed);
        const statusEl = card.querySelector('.codex-status');
        statusEl.textContent = `🔄 进行中 ${fmtElapsed(msg.elapsed)}…`;
        statusEl.className = 'codex-status running';
        renderCounters(card);
        break;
      }
      case 'codex_error': {
        const card = activeCards.get(`${msg.tool}:${msg.label}`);
        if (card) {
          const statusEl = card.querySelector('.codex-status');
          statusEl.textContent = msg.message || '⚠️ 检测到报错输出，智能体会自行处理并继续';
          statusEl.className = 'codex-status running';
        }
        App.addCodexMsg(msg.message || '⚠️ 检测到报错输出，智能体会自行处理并继续', 'notice');
        break;
      }
      case 'codex_done': {
        const card = ensureCard(msg.tool, msg.label);
        const st = card.__cx;
        if (msg.task_id && st.taskId && msg.task_id !== st.taskId) resetCard(card, msg.task_id);
        st.counts.steps = Math.max(st.counts.steps, msg.steps_total || 0);
        st.counts.lines = Math.max(st.counts.lines, msg.lines_total || 0);
        finalizeCard(card,
          (msg.success ? '✅ ' : '❌ ') + (msg.label || msg.tool) +
          ` 完成（用时${fmtElapsed(msg.elapsed)}，exit=${msg.exit_code}，${Math.max(st.counts.steps, st.toolCount)} 步 / ${st.counts.lines || st.seq} 行）`,
          msg.success ? 'done ok' : 'done err', !!msg.success);
        card.querySelector('.codex-elapsed').textContent = `完成 · ${fmtElapsed(msg.elapsed)} · exit=${msg.exit_code}`;
        const summary = (msg.summary || '').trim() || '(无输出)';
        showCardResult(card, summary, !!msg.success);
        const rawDet = card.querySelector('.codex-raw');
        if (msg.raw_tail && msg.raw_tail !== summary) {
          rawDet.style.display = '';
          rawDet.querySelector('.codex-pre').textContent = msg.raw_tail;
        }
        if (msg.task_id) syncTrace(card); // 收尾前再对齐一次，确保不漏最后的输出
        App.bumpNewMsg(card);
        App.scrollToBottom();
        removeCard(msg.tool, msg.label);
        break;
      }
      case 'codex_terminated': {
        const card = ensureCard(msg.tool, msg.label);
        finalizeCard(card, msg.message || `🛑 ${msg.reason || '已终止'}`, 'terminated', false);
        card.querySelector('.codex-elapsed').textContent = `已终止 · ${fmtElapsed(msg.elapsed)}`;
        showCardResult(card, msg.message || msg.reason || '任务已终止', false);
        removeCard(msg.tool, msg.label);
        break;
      }
      case 'codex_timeout': {
        const card = ensureCard(msg.tool, msg.label);
        finalizeCard(card, msg.message || `⌛ ${msg.label} 超时`, 'timeout', false);
        card.querySelector('.codex-elapsed').textContent = `超时 · ${fmtElapsed(msg.elapsed)}`;
        showCardResult(card, msg.summary || msg.message || '执行超时', false);
        removeCard(msg.tool, msg.label);
        break;
      }
      default:
        return false;
    }
    return true;
  };

  // 卡片内事件委托：加载完整原始日志
  document.addEventListener('click', (e) => {
    const btn = (e.target as HTMLElement).closest('.codex-full-log');
    if (!btn) return;
    const card = btn.closest('.codex-card') as HTMLElement | null;
    if (card) loadFullLog(card);
  });

  // ---------- 挂接：拦截 handleWSMessage ----------
  const _origHandle = App.handleWSMessage;
  App.handleWSMessage = function patchedHandleWSMessage(msg) {
    if (msg && typeof msg.type === 'string' && msg.type.startsWith('codex')) {
      if (App.handleCodexMessage(msg)) return;
    }
    return _origHandle.call(this, msg);
  };

  console.log('[CodexRunner] 可下钻执行明细链就绪（0.5s 实时增量 · seq 回补 · 工具/参数/输出/耗时）');
});
