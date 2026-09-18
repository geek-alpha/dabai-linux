import type { AgentInfo, AppKernel, TaskEvent, TaskItem } from '../types/app-kernel.js';

export default (function init(App: AppKernel) {
  /* ============================================================
   *  任务中心 —— 大白指挥中枢的可视化面板
   *  统一展示 DSH 智能体 / codex / opencode / 后台命令 的进度、日志与结果
   *  用户可确认、拒绝、中断；task_event 实时增量更新
   * ============================================================ */

  const STATUS_META: Record<string, { label: string; cls: string; icon: string }> = {
    confirming: { label: '待确认', cls: 'confirming', icon: '⏳' },
    queued:     { label: '排队中', cls: 'queued',     icon: '⏳' },
    running:    { label: '执行中', cls: 'running',    icon: '🔄' },
    done:       { label: '已完成', cls: 'done',       icon: '✅' },
    error:      { label: '失败',   cls: 'error',      icon: '❌' },
    cancelled:  { label: '已取消', cls: 'cancelled',  icon: '🛑' }
  };
  // 智能体目录（与后端 task_orchestrator.AGENTS 一致）：谁是谁、长什么样、干什么的
  const AGENT_DB: Record<string, AgentInfo> = {
    dsh:      { name: 'DSH 智能体', icon: '🤖', color: '#7c5cff', desc: 'DeepSeek Harness 里的 AI 智能体 —— 解决复杂任务的好帮手。自带 bash/文件/搜索/联网等独立工具集，适合跨系统、多步骤、需要深入调查的复杂任务；执行前需你确认，可在 DSH 网页实时查看过程。' },
    codex:    { name: 'Codex',      icon: '⚙️', color: '#ffb84d', desc: '本机 AI 编码助手 —— 攻坚顶级难题。算法难题、复杂重构、棘手 bug、性能优化等高难度编码挑战交给它，本机直跑。' },
    opencode: { name: 'OpenCode',   icon: '✦', color: '#00c2ff', desc: '本机 AI 编码助手 —— 解决日常问题。日常的小需求、快速改文件、写简单脚本、跑测试等常规编码活儿交给它，本机直跑、速度快。' },
    shell:    { name: '后台命令',   icon: '💻', color: '#4ade80', desc: '直接在电脑上运行的命令（/bg），适合下载、构建、训练等长耗时任务。' },
    steps:    { name: '多步命令',   icon: '🧭', color: '#b39ddb', desc: '按顺序执行的多条命令编排（由 LLM 拆解）。' },
  };
  function agentOf(t: TaskItem): AgentInfo {
    if (t && t.agent && t.agent.name) {
      return {
        name: t.agent.name || '',
        icon: t.agent.icon || '•',
        color: t.agent.color || '#8c8ca0',
        desc: t.agent.desc || ''
      };
    }
    return AGENT_DB[(t && t.channel) || ''] || { name: (t && t.channel) || '任务', icon: '•', color: '#8c8ca0', desc: '' };
  }

  let tasks: TaskItem[] = [];
  let detailMap = new Map<string, TaskItem>();
  let open = false;
  let pollTimer: number | null = null;
  let selectedId: string | null = null;
  let detailTimer: number | null = null;
  // codex/opencode 任务的结构化执行明细缓存（/api/tasks/<id>/trace）
  const traceCache = new Map<string, { entries: any[]; fetchedAt: number; steps: number; lines: number }>();

  function esc(s: unknown) {
    const d = document.createElement('div');
    d.textContent = s == null ? '' : String(s);
    return d.innerHTML;
  }

  function fmtTime(ms: number) {
    if (!ms) return '';
    const d = new Date(ms);
    return d.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' });
  }

  function el(tag: string, cls?: string, text?: string): HTMLElement {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined) e.textContent = text;
    return e;
  }

  // ---------- 面板 ----------

  function ensurePanel() {
    if (document.getElementById('task-center')) return;
    const panel = document.createElement('div');
    panel.id = 'task-center';
    panel.className = 'task-center';
    panel.innerHTML =
      '<div class="task-center-head">' +
        '<span class="task-center-title">🧭 任务中心 · AI 助手指挥台</span>' +
        '<button id="task-center-close" class="task-center-close">×</button>' +
      '</div>' +
      '<div class="task-center-body">' +
        '<div class="task-center-side">' +
          '<div class="task-center-toolbar"><button id="task-center-clear" class="task-center-clear">🗑 清除已完成</button></div>' +
          '<div id="task-center-list" class="task-center-list"></div>' +
        '</div>' +
        '<div id="task-center-detail" class="task-center-detail">' +
          '<div class="task-detail-empty">选择左侧任务查看进度与日志</div>' +
        '</div>' +
      '</div>' +
      '<div id="task-center-hint" class="task-center-hint"></div>';
    document.body.appendChild(panel);
    document.getElementById('task-center-close')!.addEventListener('click', () => App.closeTaskCenter());
    document.getElementById('task-center-clear')!.addEventListener('click', () => {
      fetch('/api/tasks/clear', { method: 'POST' }).then(r => r.json()).then((d: any) => {
        if (d && d.ok) {
          tasks = tasks.filter(t => !['done', 'error', 'cancelled'].includes(t.status || ''));
          detailMap.clear();
          selectedId = null;
          renderList();
          const wrap = document.getElementById('task-center-detail');
          if (wrap) wrap.innerHTML = '<div class="task-detail-empty">选择左侧任务查看进度与日志</div>';
          if (App.showToast) App.showToast('已清除 ' + (d.cleared || 0) + ' 个已完成任务');
        }
      }).catch(() => {});
    });
  }

  // ---------- 打开 / 关闭 / 轮询 ----------

  App.openTaskCenter = function openTaskCenter() {
    ensurePanel();
    open = true;
    document.getElementById('task-center')!.classList.add('show');
    refresh();
    if (pollTimer) clearInterval(pollTimer);
    // 兜底轮询，不再是数据主源：任务事件已广播到所有连接
    // （task_orchestrator.set_broadcast），实时性由 ws 的 task_event 保证。
    // 这里只为「ws 断线重连期间」兜底——2.5s 全量拉一次，在手机上是每轮 700ms 的空转。
    pollTimer = setInterval(refresh, 15000);
  };

  App.closeTaskCenter = function closeTaskCenter() {
    open = false;
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
    const panel = document.getElementById('task-center');
    if (panel) panel.classList.remove('show');
  };

  App.toggleTaskCenter = function toggleTaskCenter() {
    if (open) App.closeTaskCenter(); else App.openTaskCenter();
  };

  /** 外部入口（工具链子任务徽章等）：打开任务中心并定位到指定任务详情 */
  App.selectTaskCenter = function selectTaskCenter(taskId: string) {
    App.openTaskCenter();
    selectedId = taskId;
    renderList();
    loadDetail(taskId).then(() => renderDetail(taskId));
  };

  async function fetchList(): Promise<void> {
    fetch('/api/tasks').then(r => r.json()).then((data: any) => {
      if (data && data.ok) {
        tasks = data.tasks || [];
        renderList();
      }
    }).catch(() => {});
  }

  function refresh() {
    if (!open) return;
    fetchList().then(() => {
      if (selectedId) renderDetail(selectedId);
      updateBadge();
    });
  }

  // 全量列表最多 1 秒拉一次（尾随合并）：task_event 改成广播后会密集到达，
  // 每个事件都全量拉一次会变成每秒几十个请求。窗口内的事件不丢——
  // 尾随那一次把它们造成的状态变化一起同步过来。
  const LIST_SYNC_MS = 1000;
  let listSyncAt = 0;
  let listSyncTimer: number | null = null;

  function refreshList() {
    if (!open) return;
    const wait = LIST_SYNC_MS - (Date.now() - listSyncAt);
    if (wait <= 0) {
      listSyncAt = Date.now();
      fetchList().then(() => updateBadge());
      return;
    }
    if (listSyncTimer != null) return;
    listSyncTimer = window.setTimeout(() => {
      listSyncTimer = null;
      listSyncAt = Date.now();
      if (open) fetchList().then(() => updateBadge());
    }, wait);
  }

  // ---------- 实时事件 ----------

  App.handleTaskEvent = function handleTaskEvent(ev: TaskEvent) {
    if (!ev || !ev.id) return;
    // 增量就地更新详情缓存（高频日志流不再每次 fetch 全量）
    const t = detailMap.get(ev.id);
    if (t) {
      if (ev.status) t.status = ev.status;
      if (ev.title) t.title = ev.title;
      if (ev.brief) t.brief = ev.brief;
      if (ev.step) (t.steps = t.steps || []).push(ev.step);
      if (ev.log) (t.logs = t.logs || []).push(ev.log);
      if (ev.logs && ev.logs.length) (t.logs = t.logs || []).push(...ev.logs);
      if (t.logs && t.logs.length > 300) t.logs = t.logs.slice(-300);
      if (ev.result !== undefined) t.result = ev.result;
      if (ev.error !== undefined) t.error = ev.error;
      t.updated_at = Date.now();
    }
    if (open) {
      if (ev.id === selectedId) {
        if (detailTimer) clearTimeout(detailTimer);
        detailTimer = setTimeout(() => {
          detailTimer = null;
          renderDetail(ev.id);
        }, 150);
      }
      refreshList();
    }
    updateBadge();
  };

  function updateBadge() {
    const btn = document.getElementById('task-btn');
    if (!btn) return;
    const active = (tasks || []).filter(t => ['confirming', 'queued', 'running'].includes(t.status || '')).length;
    let badge = btn.querySelector('.task-badge') as HTMLElement | null;
    if (active > 0) {
      if (!badge) {
        badge = el('span', 'task-badge');
        btn.appendChild(badge);
      }
      badge.textContent = String(active);
      badge.style.display = 'inline-flex';
      btn.classList.add('has-active');
    } else if (badge) {
      badge.style.display = 'none';
      btn.classList.remove('has-active');
    }
  }

  // ---------- 渲染 ----------

  function renderList() {
    const listEl = document.getElementById('task-center-list');
    if (!listEl) return;
    const prevTop = listEl.scrollTop; // 轮询重建时保持列表滚动位置
    listEl.innerHTML = '';
    if (!tasks.length) {
      listEl.appendChild(el('div', 'task-list-empty', '暂无任务 —— 让大白委派点活儿吧'));
      return;
    }
    for (const t of tasks) {
      const meta = STATUS_META[t.status || ''] || { label: t.status || '', cls: '', icon: '•' };
      const ag = agentOf(t);
      const row = el('div', 'task-row' + (t.id === selectedId ? ' selected' : ''));
      row.innerHTML =
        '<div class="task-row-top">' +
          '<span class="task-row-icon" style="color:' + ag.color + '">' + (ag.icon || '•') + '</span>' +
          '<span class="task-row-title">' + esc(t.title || '任务') + '</span>' +
        '</div>' +
        '<div class="task-row-meta">' +
          '<span class="task-chip ' + meta.cls + '">' + meta.icon + ' ' + esc(meta.label) + '</span>' +
          '<span class="task-agent-name" style="color:' + ag.color + '">' + esc(ag.name || '') + '</span>' +
        '</div>' +
        (typeof t.progress === 'number' ? '<div class="task-row-progress"><div class="task-row-progress-fill" style="width:' + Math.max(0, Math.min(100, t.progress)) + '%"></div></div>' : '');
      row.addEventListener('click', () => {
        selectedId = t.id;
        renderList();
        loadDetail(t.id).then(() => renderDetail(t.id));
      });
      listEl.appendChild(row);
    }
    listEl.scrollTop = prevTop;
  }

  async function loadDetail(taskId: string): Promise<void> {
    try {
      const r = await fetch('/api/tasks/' + encodeURIComponent(taskId));
      const data = await r.json();
      if (data && data.ok) detailMap.set(taskId, data.task);
    } catch (e) { /* keep stale */ }
  }

  // ---------- 执行明细（下钻） ----------

  async function loadTaskTrace(taskId: string): Promise<void> {
    try {
      const r = await fetch('/api/tasks/' + encodeURIComponent(taskId) + '/trace?after=0&limit=3000');
      const data = await r.json();
      if (data && data.ok) {
        traceCache.set(taskId, {
          entries: data.entries || [],
          fetchedAt: Date.now(),
          steps: data.steps_total || 0,
          lines: data.lines_total || 0,
        });
      }
    } catch (e) { /* keep stale */ }
  }

  // 把扁平条目归并成「工具步骤：参数/输出/耗时」
  function buildTraceSteps(entries: any[]): any[] {
    const steps: any[] = [];
    for (const e of entries || []) {
      if (!e || !e.type) continue;
      if (e.type === 'tool') {
        steps.push({ name: e.tool || '工具', idx: e.idx || steps.length + 1, status: 'running', dur_ms: null, args: [], out: [] });
      } else if (e.type === 'args' && steps.length) {
        steps[steps.length - 1].args.push(e.text || '');
      } else if (e.type === 'out' && steps.length) {
        steps[steps.length - 1].out.push(e.text || '');
      } else if (e.type === 'tool_end' && steps.length) {
        let t = null;
        for (let i = steps.length - 1; i >= 0; i--) {
          if (steps[i].status === 'running') { t = steps[i]; break; }
        }
        if (t) { t.status = e.status || 'error'; t.dur_ms = e.dur_ms != null ? e.dur_ms : null; }
      }
    }
    return steps;
  }

  function renderTaskTrace(fold: HTMLDetailsElement, taskId: string) {
    const body = fold.querySelector('.task-trace-body') as HTMLElement;
    const stateEl = fold.querySelector('.task-trace-state') as HTMLElement | null;
    if (!body) return;
    let cached = traceCache.get(taskId);
    if (!cached) {
      if (stateEl) stateEl.textContent = '加载中…';
      loadTaskTrace(taskId).then(() => {
        if (document.body.contains(fold)) renderTaskTrace(fold, taskId);
      });
      return;
    }
    if (stateEl) stateEl.textContent = cached.steps ? cached.steps + ' 步 · ' + cached.lines + ' 行' : '暂无明细';
    body.innerHTML = '';
    const steps = buildTraceSteps(cached.entries);
    if (!steps.length) {
      body.appendChild(el('div', 'task-trace-empty', '还没有工具调用步骤（智能体仍在思考或正在输出）'));
      return;
    }
    for (const s of steps) {
      const row = el('div', 'task-trace-step ' + s.status);
      const head = el('div', 'task-trace-head');
      head.innerHTML =
        '<span class="task-trace-idx">#' + s.idx + '</span>' +
        '<span class="task-trace-icon">🔧</span>' +
        '<span class="task-trace-name">' + esc(s.name) + '</span>' +
        '<span class="task-trace-badge">' + (s.status === 'running' ? '运行中' : s.status === 'success' ? '✓ ' + (s.dur_ms != null ? s.dur_ms + 'ms' : '') : '✗ ' + (s.dur_ms != null ? s.dur_ms + 'ms' : '')) + '</span>';
      row.appendChild(head);
      if (s.args.length) {
        const a = el('div', 'task-trace-args');
        a.textContent = '参数 ' + s.args.join(' ');
        a.title = s.args.join('\n');
        row.appendChild(a);
      }
      if (s.out.length) {
        const o = el('div', 'task-trace-out');
        o.textContent = s.out.slice(0, 6).join('\n') + (s.out.length > 6 ? '\n…共 ' + s.out.length + ' 行' : '');
        o.title = s.out.join('\n');
        row.appendChild(o);
      }
      row.addEventListener('click', () => row.classList.toggle('open'));
      body.appendChild(row);
    }
  }

  function ensureTaskTrace(wrap: HTMLElement, t: TaskItem) {
    const isCodex = t.kind === 'codex-tool' || t.channel === 'codex' || t.channel === 'opencode';
    if (!isCodex) return;
    const box = el('div', 'task-trace-box');
    const fold = document.createElement('details');
    fold.className = 'task-trace-fold';
    fold.innerHTML =
      '<summary class="task-log-summary">' +
        '<span class="task-log-title">🔬 执行明细（下钻：每一步的动作/输入/输出）</span>' +
        '<span class="task-trace-state"></span>' +
      '</summary>' +
      '<div class="task-trace-body"></div>';
    box.appendChild(fold);
    wrap.appendChild(box);
    renderTaskTrace(fold, t.id);
    // 明细区展开时自动刷新（执行中每 5s 一次，终态后一次）
    fold.addEventListener('toggle', () => {
      if (fold.open) renderTaskTrace(fold, t.id);
    });
    if (t.status === 'running' || t.status === 'queued' || t.status === 'confirming') {
      setTimeout(() => {
        if (document.body.contains(fold)) renderTaskTrace(fold, t.id);
      }, 5000);
    }
  }

  // ---------- 长跑引擎：轮次下钻（点一轮看它到底干了什么） ----------

  const longrunTraceCache = new Map<number, any>();

  async function loadLongrunTrace(cycle: number): Promise<any> {
    if (longrunTraceCache.has(cycle)) return longrunTraceCache.get(cycle);
    try {
      const r = await fetch('/api/longrun/trace/' + encodeURIComponent(String(cycle)));
      const data = await r.json();
      longrunTraceCache.set(cycle, data);
      return data;
    } catch (e) {
      return null;
    }
  }

  function traceEventLines(events: any[]): string[] {
    const lines: string[] = [];
    for (const e of events || []) {
      if (!e || !e.type) continue;
      if (e.type === 'prompt') {
        lines.push('【本轮动作】' + (e.action || '') + '（prompt ' + (e.chars || 0) + ' 字）');
      } else if (e.type === 'ToolCallStart') {
        lines.push('▸ ' + (e.tool_name || '工具') + ' ' + String(e.hint || e.arguments || '').slice(0, 120));
      } else if (e.type === 'ToolCallResult') {
        lines.push('  ' + (e.success === false ? '✗' : '✓') + ' ' +
          String(e.result || '').replace(/\n/g, ' ').slice(0, 160));
      } else if (e.type === 'exit') {
        lines.push('退出码 ' + e.exit + ' · ' + (e.dur != null ? e.dur + 's' : '') + ' · ' +
          (e.progressed ? '✓ 有进展' : '✗ 无进展（' + (e.reason || '台账没变') + '）'));
        const u = e.usage || {};
        if (u.in != null) lines.push('token ' + u.in + '→' + u.out + (u.rounds ? ' · ' + u.rounds + ' 轮' : ''));
      } else if (e.type === 'StreamDelta' && e.text) {
        lines.push(String(e.text));
      }
    }
    return lines;
  }

  function renderLongrunRounds(wrap: HTMLElement, t: any) {
    const rounds = (t.extra && t.extra.rounds) || [];
    if (!rounds.length) return;
    const box = el('div', 'task-steps');
    box.appendChild(el('div', 'task-section-label', '轮次台账 · 点一轮看它到底干了什么'));
    const pane = el('pre', 'task-log-pre');
    pane.style.display = 'none';
    for (const rd of rounds.slice().reverse()) {
      const row = el('div', 'task-step');
      row.innerHTML = '<span class="task-step-dot"></span><span></span>';
      const txt = row.lastElementChild as HTMLElement;
      const time = rd.t ? new Date(rd.t * 1000).toLocaleTimeString('zh-CN', { hour12: false }) : '';
      txt.textContent = '第 ' + rd.cycle + ' 轮 [' + (rd.goal || '') + '] ' + (rd.ok ? '✓' : '✗') + ' ' +
        (rd.dur != null ? rd.dur + 's ' : '') + (rd.tool_count ? rd.tool_count + ' 个工具 ' : '') + time +
        (rd.trace ? ' · 可下钻' : ' · 无 trace');
      if (rd.trace) {
        row.style.cursor = 'pointer';
        row.title = '点开看这一轮的 prompt / 工具调用 / 退出码';
        row.addEventListener('click', () => {
          if (pane.style.display !== 'none' && pane.dataset.cycle === String(rd.cycle)) {
            pane.style.display = 'none';
            return;
          }
          pane.dataset.cycle = String(rd.cycle);
          pane.style.display = '';
          pane.textContent = '加载第 ' + rd.cycle + ' 轮 trace…';
          loadLongrunTrace(rd.cycle).then((d: any) => {
            if (!d) { pane.textContent = 'trace 读取失败'; return; }
            if (!d.ok) { pane.textContent = d.error || '该轮没有 trace'; return; }
            const lines = traceEventLines(d.events || []);
            pane.textContent = '第 ' + rd.cycle + ' 轮 · ' + d.lines + ' 条事件\n' + (lines.join('\n') || '（空）');
          });
        });
      } else {
        row.style.opacity = '0.55';
      }
      box.appendChild(row);
    }
    box.appendChild(pane);
    wrap.appendChild(box);
  }

  function renderDetail(taskId: string) {
    const wrap = document.getElementById('task-center-detail');
    if (!wrap) return;
    const t = detailMap.get(taskId);
    if (!t) { wrap.innerHTML = '<div class="task-detail-empty">加载中…</div>'; return; }
    const meta = STATUS_META[t.status || ''] || { label: t.status || '', cls: '', icon: '•' };

    // 全量重建前记住滚动状态：面板滚动位置 + 日志滚动位置（贴底则继续跟随）
    const prevWrapTop = wrap.scrollTop;
    const prevLog = wrap.querySelector('.task-log-pre') as HTMLElement | null;
    const prevLogTop = prevLog ? prevLog.scrollTop : 0;
    const prevLogStick = prevLog ? (prevLog.scrollHeight - prevLog.scrollTop - prevLog.clientHeight < 40) : true;
    const prevLogFold = wrap.querySelector('.task-log-fold') as HTMLDetailsElement | null;
    const prevLogOpen = !!(prevLogFold && prevLogFold.open);

    wrap.innerHTML = '';

    const ag = agentOf(t);
    const head = el('div', 'task-detail-head');
    head.innerHTML =
      '<span class="task-row-icon" style="color:' + ag.color + '">' + (ag.icon || '•') + '</span>' +
      '<div class="task-detail-title">' +
        '<div class="task-detail-name">' + esc(t.title || '任务') + '</div>' +
        '<div class="task-detail-sub">' + esc(ag.name || t.channel || '') + ' · 任务 ' + esc(t.kind || '') + '</div>' +
      '</div>' +
      '<span class="task-chip ' + meta.cls + '">' + meta.icon + ' ' + esc(meta.label) + '</span>';
    wrap.appendChild(head);

    // 执行者卡片：一眼看清派给了谁 + 这个智能体是谁
    const exec = el('div', 'task-executor');
    exec.innerHTML =
      '<div class="task-executor-row">' +
        '<span class="task-executor-icon" style="color:' + ag.color + '">' + (ag.icon || '•') + '</span>' +
        '<span class="task-executor-name" style="color:' + ag.color + '">' + esc(ag.name || '') + '</span>' +
      '</div>' +
      '<div class="task-executor-desc">' + esc(ag.desc || '') + '</div>';
    wrap.appendChild(exec);

    if (t.brief) {
      const brief = el('div', 'task-detail-brief');
      brief.innerHTML = App.mdToHtml ? App.mdToHtml(String(t.brief)) : esc(t.brief);
      wrap.appendChild(brief);
    }

    if (t.steps && t.steps.length) {
      const stepsBox = el('div', 'task-steps');
      stepsBox.appendChild(el('div', 'task-section-label', '进展'));
      for (const s of t.steps) {
        const row = el('div', 'task-step');
        row.innerHTML = '<span class="task-step-dot"></span><span></span>';
        const stepText = row.lastElementChild as HTMLElement;
        if (stepText) stepText.innerHTML = App.mdToHtml ? App.mdToHtml(String(s)) : esc(s);
        stepsBox.appendChild(row);
      }
      wrap.appendChild(stepsBox);
    }

    // 长跑引擎：轮次可下钻（每轮一个 trace 文件）
    if (t.kind === 'longrun') renderLongrunRounds(wrap, t);

    if (t.logs && t.logs.length) {
      // 过程性日志默认折叠：摘要行露出最新一条，点开看全文
      const logBox = el('div', 'task-logs');
      const fold = document.createElement('details');
      fold.className = 'task-log-fold';
      fold.open = prevLogOpen;
      const latest = String(t.logs[t.logs.length - 1] || '');
      const sum = el('summary', 'task-log-summary');
      sum.innerHTML = '<span class="task-log-title">📜 实时日志 · ' + t.logs.length + ' 行</span>' +
        '<span class="task-log-latest"></span>';
      const latestEl = sum.querySelector('.task-log-latest') as HTMLElement;
      latestEl.textContent = latest.length > 60 ? latest.slice(0, 60) + '…' : latest;
      latestEl.title = latest;
      const pre = el('pre', 'task-log-pre', t.logs.join('\n'));
      fold.appendChild(sum);
      fold.appendChild(pre);
      logBox.appendChild(fold);
      wrap.appendChild(logBox);
    }

    // codex/opencode：可下钻的执行明细（工具步骤：参数/输出/耗时）
    ensureTaskTrace(wrap, t);

    if (t.status === 'done' && t.result) {
      const resBox = el('div', 'task-result-box');
      const resEl = el('div', 'task-result');
      if (App.renderMsgMedia) {
        App.renderMsgMedia(resEl, String(t.result).slice(0, 20000));
        if (App.taskBoardAddMedia) {
          const urls = App.extractMediaUrls ? App.extractMediaUrls(String(t.result)) : [];
          if (urls.length) App.taskBoardAddMedia(urls);
        }
      } else {
        resEl.textContent = String(t.result).slice(0, 20000);
      }
      resBox.appendChild(resEl);
      const fullRes = String(t.result || '');
      if (fullRes.length > 800) {
        const moreBtn = el('button', 'task-btn-act task-btn-ghost task-more-btn', '展开全文');
        moreBtn.addEventListener('click', () => {
          resEl.classList.toggle('expanded');
          moreBtn.textContent = resEl.classList.contains('expanded') ? '收起' : '展开全文';
        });
        resBox.appendChild(moreBtn);
      }
      wrap.appendChild(resBox);
    }
    if (t.status === 'error' && t.error) {
      const errEl = el('div', 'task-error');
      errEl.innerHTML = App.mdToHtml ? App.mdToHtml(String(t.error).slice(0, 2000)) : esc(String(t.error).slice(0, 2000));
      wrap.appendChild(errEl);
    }

    const actions = el('div', 'task-actions');
    if (t.status === 'confirming') {
      const rejectBtn = el('button', 'task-btn-act task-btn-ghost', '拒绝');
      const approveBtn = el('button', 'task-btn-act task-btn-ok', '确认执行');
      rejectBtn.addEventListener('click', () => act(taskId, false));
      approveBtn.addEventListener('click', () => act(taskId, true));
      actions.appendChild(rejectBtn);
      actions.appendChild(approveBtn);
    } else if (t.status === 'running' || t.status === 'queued') {
      const killBtn = el('button', 'task-btn-act task-btn-ghost', '中断');
      killBtn.addEventListener('click', () => kill(taskId));
      actions.appendChild(killBtn);
    }
    if (t.status === 'done' || t.status === 'error' || t.status === 'cancelled') {
      const closeBtn = el('button', 'task-btn-act task-btn-ghost', '知道了');
      closeBtn.addEventListener('click', () => App.closeTaskCenter());
      actions.appendChild(closeBtn);
    }
    if (actions.children.length) wrap.appendChild(actions);

    // 恢复滚动：面板回到原位置；日志贴底则跟随最新，否则停在原处
    wrap.scrollTop = prevWrapTop;
    const newLog = wrap.querySelector('.task-log-pre') as HTMLElement | null;
    if (newLog) {
      newLog.scrollTop = prevLogStick ? newLog.scrollHeight : prevLogTop;
    }

    const hint = document.getElementById('task-center-hint');
    if (hint) {
      if (t.status === 'running' && t.channel === 'dsh') {
        hint.textContent = '💡 DSH 智能体正在执行，可打开 DSH 网页（127.0.0.1:3080）看同一会话的实时工具调用。';
      } else if (t.status === 'confirming') {
        hint.textContent = '💡 需要你确认后才会真正执行 —— 大白也做不了主，你说了算。';
      } else {
        hint.textContent = '💡 所有委派任务都会在这里全程可见：进度、日志、结果，随时可中断。';
      }
    }
  }

  // 用户拒绝委派后：让 AI 自然回应，别让对话卡在"待确认"（画图类需求会转回 image_gen_create）
  App.notifyTaskDeclined = function notifyTaskDeclined() {
    if (!App.sendAIAction) return;
    App.sendAIAction(
      '（用户刚刚拒绝了刚才委派给智能体的任务，任务已取消，请不要重复委派同一件事。' +
      '如果用户本意是画图/图片/壁纸/立绘/头像/插画/海报/视频等视觉生成，请直接用 image_gen_create 技能自己生成并把结果展示给用户；' +
      '如果是其他需求，就自然回应并询问用户想怎么做。）',
      true
    );
  };

  function act(taskId: string, approve: boolean) {
    fetch('/api/tasks/' + encodeURIComponent(taskId) + '/confirm', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ approve })
    }).then(() => {
      setTimeout(refresh, 400);
      if (!approve && App.notifyTaskDeclined) App.notifyTaskDeclined();
    });
  }

  function kill(taskId: string) {
    fetch('/api/tasks/' + encodeURIComponent(taskId) + '/kill', { method: 'POST' })
      .then(() => setTimeout(refresh, 400));
  }

  // ============================================================
  //  DSH 智能体 · 聊天框直播卡（执行过程/结果一眼可判）
  // ============================================================
  const dshCards = new Map<string, HTMLElement>(); // task_id -> card DOM

  function ensureDshCard(ev: TaskEvent): HTMLElement {
    // 需要 title/brief：事件不携带时用占位，稍后统一从详情补齐
    let card = dshCards.get(ev.id);
    if (card && document.body.contains(card)) return card;
    card = document.createElement('div');
    card.className = 'msg dsh-card';
    card.dataset.taskId = ev.id;
    card.innerHTML =
      '<div class="dsh-head">' +
        '<span class="dsh-icon">🤖</span>' +
        '<span class="dsh-title">DSH 智能体 · <span class="dsh-task-title">任务</span></span>' +
        '<span class="dsh-status">…</span>' +
        '<span class="dsh-elapsed"></span>' +
      '</div>' +
      '<div class="dsh-brief" style="display:none"></div>' +
      '<div class="dsh-steps"></div>' +
      '<div class="dsh-log" style="display:none"></div>' +
      '<div class="dsh-result" style="display:none"></div>' +
      '<div class="dsh-error" style="display:none"></div>' +
      '<div class="dsh-actions"></div>';
    card.addEventListener('click', (e) => {
      const tgt = e.target as HTMLElement;
      if (tgt.closest('.dsh-action')) return;
      if (tgt.closest && tgt.closest('.msg-media-link')) return; // 点击卡内图片/视频不跳转
      if (tgt.closest && tgt.closest('.msg-copy-btn')) return;
      // 正在选中/复制卡内文字时不要跳转任务中心
      const sel = window.getSelection && window.getSelection();
      if (sel && String(sel).trim()) return;
      App.openTaskCenter();
    });
    App.messagesEl!.appendChild(card);
    App._trimMessages();
    App.bumpNewMsg(card);
    App.scrollToBottom();
    App.notifyFullscreenChat();
    loadDshCardDetail(ev.id); // 补 title/brief
    dshCards.set(ev.id, card);
    return card;
  }

  function loadDshCardDetail(taskId: string) {
    fetch('/api/tasks/' + encodeURIComponent(taskId)).then(r => r.json()).then((data: any) => {
      const card = dshCards.get(taskId);
      if (!card || !data || !data.ok) return;
      const t = data.task;
      const titleEl = card.querySelector('.dsh-task-title') as HTMLElement | null;
      if (titleEl && t.title) titleEl.textContent = t.title;
      const briefEl = card.querySelector('.dsh-brief') as HTMLElement | null;
      if (briefEl && t.brief) {
        briefEl.style.display = 'block';
        briefEl.textContent = t.brief;
      }
      // 补齐遗漏的日志（任务中心详情）
      if (t.logs && t.logs.length) {
        const logEl = card.querySelector('.dsh-log') as HTMLElement | null;
        if (logEl) {
          logEl.style.display = 'block';
          logEl.textContent = '…' + t.logs[t.logs.length - 1];
        }
      }
      if (t.status === 'done' && t.result) showDshResult(card, t.result);
      if (t.status === 'error' && t.error) showDshError(card, t.error);
    }).catch(() => {});
  }

  function dshStatusEl(card: HTMLElement) { return card.querySelector('.dsh-status') as HTMLElement | null; }
  function dshActionsEl(card: HTMLElement) { return card.querySelector('.dsh-actions') as HTMLElement | null; }

  function setDshStatus(card: HTMLElement, status: string) {
    const el = dshStatusEl(card)!;
    const map: Record<string, string> = {
      confirming: '⏳ 待确认', queued: '⏳ 排队中', running: '🔄 执行中',
      done: '✅ 已完成', error: '❌ 失败', cancelled: '🛑 已取消'
    };
    el.textContent = map[status] || status;
    el.className = 'dsh-status ' + (status || '');
  }

  function renderDshActions(card: HTMLElement, taskId: string, status: string) {
    const a = dshActionsEl(card)!;
    a.innerHTML = '';
    const mk = (txt: string, cls: string, fn: () => void) => {
      const b = document.createElement('button');
      b.className = 'dsh-action ' + cls;
      b.textContent = txt;
      b.addEventListener('click', (e) => { e.stopPropagation(); fn(); });
      a.appendChild(b);
    };
    if (status === 'confirming') {
      mk('拒绝', 'ghost', () => dshConfirm(taskId, false));
      mk('确认执行', 'ok', () => dshConfirm(taskId, true));
    } else if (status === 'running' || status === 'queued') {
      mk('中断', 'ghost', () => dshKill(taskId));
    }
    if (status === 'done' || status === 'error' || status === 'cancelled') {
      mk('查看任务中心', 'ghost', () => App.openTaskCenter());
    }
    a.style.display = a.children.length ? '' : 'none';
  }

  function dshConfirm(taskId: string, approve: boolean) {
    fetch('/api/tasks/' + encodeURIComponent(taskId) + '/confirm', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ approve })
    }).then(() => {
      if (!approve && App.notifyTaskDeclined) App.notifyTaskDeclined();
    }).catch(() => {});
  }

  function dshKill(taskId: string) {
    fetch('/api/tasks/' + encodeURIComponent(taskId) + '/kill', { method: 'POST' }).catch(() => {});
  }

  function addDshStep(card: HTMLElement, step: string) {
    const box = card.querySelector('.dsh-steps') as HTMLElement;
    const row = document.createElement('div');
    row.className = 'dsh-step';
    row.innerHTML = '<span class="dsh-step-dot"></span><span></span>';
    row.lastChild!.textContent = step;
    box.appendChild(row);
    // 最多保留 8 步
    while (box.children.length > 8) box.removeChild(box.firstChild!);
  }

  function showDshResult(card: HTMLElement, text: any) {
    const el = card.querySelector('.dsh-result') as HTMLElement;
    el.style.display = 'block';
    const full = '✅ 执行完成，结果如下：\n' + (text || '');
    if (App.renderMsgMedia) {
      App.renderMsgMedia(el, full);
      if (App.taskBoardAddMedia) {
        const urls = App.extractMediaUrls ? App.extractMediaUrls(full) : [];
        if (urls.length) App.taskBoardAddMedia(urls);
      }
    } else {
      el.textContent = full;
    }
    // 复制按钮只复制结果本身（而不是整卡文本）
    if (App.ensureCopyBtn) App.ensureCopyBtn(card);
    const btn = card.querySelector('.msg-copy-btn') as HTMLElement | null;
    if (btn) btn.dataset.copyText = String(text || '');
    App.bumpNewMsg(card);
    const brief = card.querySelector('.dsh-brief') as HTMLElement | null;
    if (brief && brief.textContent && text && text.length > 120) {
      brief.title = brief.textContent;
    }
  }

  function showDshError(card: HTMLElement, text: any) {
    const el = card.querySelector('.dsh-error') as HTMLElement;
    el.style.display = 'block';
    el.textContent = '❌ ' + (text || '执行失败');
  }

  // 供 09_websocket 的确认卡片判断：聊天框是否已有该任务的直播卡
  App.dshCardExists = function dshCardExists(taskId: string) {
    const c = dshCards.get(taskId);
    return !!(c && document.body.contains(c));
  };

  // 事件驱动（09_websocket 的 task_event 会调用）
  App.dshCardOnEvent = function dshCardOnEvent(ev: TaskEvent) {
    if (!ev || !ev.id || ev.channel !== 'dsh') return;
    const card = ensureDshCard(ev);
    if (ev.title) {
      const te = card.querySelector('.dsh-task-title') as HTMLElement | null;
      if (te) te.textContent = ev.title;
    }
    if (ev.status) {
      setDshStatus(card, ev.status);
      renderDshActions(card, ev.id, ev.status);
      if (ev.status === 'running') {
        const be = card.querySelector('.dsh-brief') as HTMLElement | null;
        if (be && be.style.display === 'none') { be.style.display = 'block'; be.textContent = ev.brief || ''; }
      }
    }
    if (ev.event === 'confirming' || ev.brief) {
      const be = card.querySelector('.dsh-brief') as HTMLElement;
      be.style.display = 'block';
      if (ev.brief) be.textContent = ev.brief;
    }
    if (ev.step) addDshStep(card, ev.step);
    if (ev.log) {
      const le = card.querySelector('.dsh-log') as HTMLElement;
      le.style.display = 'block';
      le.textContent = '…' + ev.log;
    }
    if (ev.logs && ev.logs.length) {
      const le = card.querySelector('.dsh-log') as HTMLElement;
      le.style.display = 'block';
      le.textContent = '…' + ev.logs[ev.logs.length - 1];
    }
    if (ev.result !== undefined) showDshResult(card, ev.result);
    if (ev.error !== undefined) showDshError(card, ev.error);
  };

  // 页面恢复/刷新后同步 DSH 直播卡：只补「连接建立之前已发生过」的那一段，
  // 之后的增量全由广播的 task_event 送达，所以低频兜底就够。
  function syncDshCardsOnce() {
    fetch('/api/tasks').then(r => r.json()).then((data: any) => {
      if (!data || !data.ok) return;
      const now = Date.now();
      for (const t of (data.tasks || [])) {
        if (!t || t.channel !== 'dsh') continue;
        if (dshCards.has(t.id)) continue;
        // 只恢复：进行中的任务 + 最近 10 分钟内刚结束的任务
        const active = ['confirming', 'queued', 'running'].includes(t.status);
        const recent = now - (t.updated_at || 0) < 600000;
        if (!active && !recent) continue;
        ensureDshCard({ id: t.id, channel: 'dsh', status: t.status, title: t.title, brief: t.brief });
      }
    }).catch(() => {});
  }
  const _dshSyncTimer = setInterval(syncDshCardsOnce, 30000);

  // 轻量计时器：刷新执行中卡片的耗时
  setInterval(() => {
    if (!dshCards.size) return;
    const now = Date.now();
    for (const [id, card] of dshCards) {
      if (!document.body.contains(card)) { dshCards.delete(id); continue; }
      const st = dshStatusEl(card)!.textContent || '';
      if (!st.includes('执行中') && !st.includes('待确认') && !st.includes('排队中')) {
        continue;
      }
      let start = Number(card.dataset.startAt);
      if (!start) { start = now; card.dataset.startAt = String(now); }
      const sec = Math.max(0, (now - start) / 1000) | 0;
      const el = card.querySelector('.dsh-elapsed') as HTMLElement | null;
      if (el) el.textContent = sec < 60 ? sec + '秒' : Math.floor(sec / 60) + '分' + (sec % 60) + '秒';
    }
  }, 1000);

  // 注意：这里**不接**聊天全屏静默 —— 执行卡片就在聊天流里（全屏时看得见），
  // 任务中心面板 z-index 165 也浮在聊天面板之上；这些轮询本身很便宜。

  // ---------- 工具栏按钮 ----------

  App.initTaskCenter = function initTaskCenter() {
    ensurePanel();
    const btn = document.getElementById('task-btn');
    if (btn) btn.addEventListener('click', () => App.toggleTaskCenter());
    updateBadge();
  };

  setTimeout(() => {
    try { App.initTaskCenter(); } catch (e) {}
  }, 900);

  console.log('[TaskCenter] 任务中心就绪：DSH/Codex/OpenCode/后台命令统一可视化');
});
