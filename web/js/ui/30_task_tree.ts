import type { AppKernel, TaskTreeData, TaskTreeNode } from '../types/app-kernel.js';

export default (function init(App: AppKernel) {
  /* ============================================================
   *  任务树卡片 —— 聊天框内的复杂任务结构深度展示
   *  支持任意层级的任务嵌套：逐级展开/折叠、状态徽章、进度条、
   *  全部展开/收起、整卡一键复制。
   *  用法：
   *    App.addTaskTreeMsg({
   *      title: '标题', description: '说明',
   *      nodes: [ { title, status, progress, desc, children: [...] } ]
   *    });
   *  status 支持：pending / queued / confirming / running / done / error / cancelled
   *  也可由后端经 websocket 推送：{ type: 'task_tree', data: {...} }
   * ============================================================ */

  const STATUS_META: Record<string, { label: string; icon: string }> = {
    confirming: { label: '待确认', icon: '⏳' },
    queued:     { label: '排队中', icon: '⏳' },
    pending:    { label: '待执行', icon: '📌' },
    running:    { label: '执行中', icon: '🔄' },
    done:       { label: '已完成', icon: '✅' },
    error:      { label: '失败',   icon: '❌' },
    cancelled:  { label: '已取消', icon: '🛑' }
  };

  interface TaskTreeCard extends HTMLElement {
    __ttData?: TaskTreeData;
  }

  function esc(s: unknown) {
    const d = document.createElement('div');
    d.textContent = s == null ? '' : String(s);
    return d.innerHTML;
  }

  // 递归构建节点 DOM；depth 从 0 开始
  function buildNode(node: TaskTreeNode, depth: number): HTMLElement {
    const status = STATUS_META[node.status || ''] || STATUS_META.pending;
    const hasKids = Array.isArray(node.children) && node.children.length > 0;
    // 层级较深或有 children 时默认折叠，降低视觉噪音；显式 TRUE 展开
    const open = node.open === true || (depth < 2 && !hasKids);
    const wrap = document.createElement('div');
    wrap.className = 'tt-node' + (open && hasKids ? ' open' : '');
    wrap.dataset.open = open ? '1' : '0';

    const row = document.createElement('div');
    row.className = 'tt-row';
    const caret = document.createElement('button');
    caret.type = 'button';
    caret.className = 'tt-caret' + (hasKids ? '' : ' is-leaf');
    caret.innerHTML = '&#9654;';
    const icon = document.createElement('span');
    icon.className = 'tt-status-icon';
    icon.innerHTML = hasKids ? '📂' : status.icon;
    const main = document.createElement('div');
    main.className = 'tt-main';
    main.innerHTML = '<div class="tt-node-title">' + esc(node.title || '(未命名任务)') + '</div>';
    if (node.desc) {
      const desc = document.createElement('div');
      desc.className = 'tt-node-desc';
      desc.textContent = node.desc;
      main.appendChild(desc);
    }
    if (typeof node.progress === 'number') {
      const p = document.createElement('div');
      p.className = 'tt-progress';
      const fill = document.createElement('div');
      fill.className = 'tt-progress-fill';
      fill.style.width = Math.max(0, Math.min(100, node.progress)) + '%';
      p.appendChild(fill);
      main.appendChild(p);
    }
    const badge = document.createElement('span');
    badge.className = 'tt-badge ' + (node.status || 'pending');
    badge.textContent = status.label;
    row.append(caret, icon, main, badge);
    wrap.appendChild(row);

    if (hasKids) {
      const kids = document.createElement('div');
      kids.className = 'tt-children';
      node.children!.forEach((child) => kids.appendChild(buildNode(child, depth + 1)));
      wrap.appendChild(kids);
    }
    return wrap;
  }

  // 整卡复制：把任务树拍平成易读文本
  function treeToText(n: TaskTreeNode): string {
    const lines: string[] = [];
    (function walk(node: TaskTreeNode, d: number) {
      const ind = '  '.repeat(d);
      const st = STATUS_META[node.status || ''] ? STATUS_META[node.status || ''].label : '';
      lines.push(ind + '- ' + (node.title || '(未命名任务)') + (st ? ' [' + st + ']' : '') +
        (typeof node.progress === 'number' ? ' (' + node.progress + '%)' : ''));
      if (node.desc) lines.push(ind + '  ' + node.desc);
      (node.children || []).forEach((c) => walk(c, d + 1));
    })(n, 0);
    return lines.join('\n');
  }

  function copyTree(card: TaskTreeCard) {
    const data = card.__ttData;
    if (!data) return;
    const text = (data.title ? data.title + '\n' : '') +
      (data.description ? data.description + '\n' : '') +
      data.nodes.map((n) => treeToText(n)).join('\n');
    const done = () => { if (App.showToast) App.showToast('任务已复制'); };
    if (App.copyPlainText) {
      App.copyPlainText(text).then(done).catch(() => App.showToast && App.showToast('复制失败'));
    } else if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(text).then(done).catch(() => App.showToast && App.showToast('复制失败'));
    } else if (App.showToast) App.showToast('当前环境不支持复制');
  }

  function setAll(card: TaskTreeCard, open: boolean) {
    card.querySelectorAll('.tt-node').forEach((n) => {
      const el = n as HTMLElement;
      if (el.querySelector('.tt-children')) {
        el.classList.toggle('open', open);
        el.dataset.open = open ? '1' : '0';
      }
    });
  }

  /** 渲染一棵任务树为聊天消息卡片 */
  App.addTaskTreeMsg = function addTaskTreeMsg(data: TaskTreeData, opts?: any): TaskTreeCard {
    if (!data || !data.nodes) {
      if (App.showToast) App.showToast('任务结构无效：缺少 nodes');
      return document.createElement('div') as TaskTreeCard;
    }
    const card = document.createElement('div') as TaskTreeCard;
    card.className = 'msg task-tree';
    card.__ttData = data;

    let headHTML = '<div class="tt-head"><span class="tt-title">📋 ' + esc(data.title || '任务计划') + '</span>';
    const total = treeCount(data.nodes);
    const doneCount = treeCount(data.nodes, (n) => n.status === 'done' || n.status === 'cancelled');
    headHTML += '<span class="tt-summary">' + doneCount + ' / ' + total + ' 完成</span></div>';
    headHTML += '<div class="tt-tools">' +
      '<button type="button" class="tt-tool-btn" data-act="expand">展开全部</button>' +
      '<button type="button" class="tt-tool-btn" data-act="collapse">收起全部</button>' +
      '<button type="button" class="tt-tool-btn" data-act="copy">复制任务</button>' +
      '</div>';
    if (data.description) headHTML += '<div class="tt-desc">' + esc(data.description) + '</div>';
    card.innerHTML = headHTML + '<div class="tt-nodes"></div>';

    const nodesEl = card.querySelector('.tt-nodes')!;
    (data.nodes || []).forEach((n) => nodesEl.appendChild(buildNode(n, 0)));

    // 卡片内事件委托
    card.addEventListener('click', (e) => {
      const tool = (e.target as HTMLElement).closest('.tt-tool-btn') as HTMLElement | null;
      if (tool) {
        const act = tool.dataset.act;
        if (act === 'expand') setAll(card, true);
        else if (act === 'collapse') setAll(card, false);
        else if (act === 'copy') copyTree(card);
        return;
      }
      const row = (e.target as HTMLElement).closest('.tt-row') as HTMLElement | null;
      if (!row) return;
      const node = row.closest('.tt-node') as HTMLElement | null;
      if (!node || !node.querySelector('.tt-children')) return;
      node.classList.toggle('open');
      node.dataset.open = node.classList.contains('open') ? '1' : '0';
    });

    App.messagesEl!.appendChild(card);
    if (App._trimMessages) App._trimMessages();
    if (App.bumpNewMsg) App.bumpNewMsg(card);
    if (App.scrollToBottom) App.scrollToBottom();
    if (App.notifyFullscreenChat) App.notifyFullscreenChat();
    return card;
  };

  // 便捷：把任意 message 对象里的 task_tree/tree 字段渲染成卡片
  App.maybeRenderTaskTree = function maybeRenderTaskTree(msg: any): boolean {
    if (!msg) return false;
    const tree = msg.task_tree || msg.tree || (msg.data && (msg.data.task_tree || msg.data.tree));
    if (!tree || !Array.isArray(tree.nodes)) return false;
    App.addTaskTreeMsg(tree);
    return true;
  };

  function treeCount(nodes: TaskTreeNode[], pred?: (n: TaskTreeNode) => boolean): number {
    let c = 0;
    (function walk(list: TaskTreeNode[]) {
      list.forEach((n) => {
        if (!pred || pred(n)) c++;
        if (n.children) walk(n.children);
      });
    })(nodes);
    return c;
  }

  // 暴露给其它模块复用的剪贴板工具（若尚未存在）
  if (!App.copyPlainText) {
    App.copyPlainText = function copyPlainText(text: string): Promise<void> {
      if (navigator.clipboard && window.isSecureContext) {
        return navigator.clipboard.writeText(text);
      }
      const ta = document.createElement('textarea');
      ta.value = text;
      ta.style.cssText = 'position:fixed;top:0;left:0;opacity:0;pointer-events:none;';
      document.body.appendChild(ta);
      ta.focus();
      ta.select();
      let ok = false;
      try { ok = document.execCommand('copy'); } catch (err) { ok = false; }
      ta.remove();
      return ok ? Promise.resolve() : Promise.reject(new Error('copy failed'));
    };
  }
});
