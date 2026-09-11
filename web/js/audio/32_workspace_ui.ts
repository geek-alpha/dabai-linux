// ========== 工作区设置 UI（全局工作目录：DSH / Codex / OpenCode / shell 共用） ==========
// 与音乐/视频面板同风格：modal 弹窗 + 当前工作区 + 已保存工作区列表 + 可逐级下钻的目录浏览器
// 已保存列表：随时收藏多个工作区，一键激活切换，持久化到后端 workspace_saved.json

import type { AppKernel } from '../types/app-kernel.js';

interface DirEntry { name: string; path: string }

let browseStack: string[] = []; // 目录浏览器当前所在路径栈（空=根）

function esc(s: string): string {
  return String(s == null ? '' : s)
    .replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;').replaceAll("'", '&#39;');
}

export default function init_32_workspace_ui(App: AppKernel) {
  const rootsEl = () => App.workspaceRoots;
  const browsePathEl = () => App.workspaceBrowsePath;
  const upBtn = () => App.workspaceUpBtn;
  const savedEl = () => App.workspaceSavedList;

  /* ---------- 弹窗开关 ---------- */
  App.openWorkspaceModal = function openWorkspaceModal() {
    App.workspaceModal!.classList.add('show');
    browseStack = [];
    App.refreshWorkspace();
    setTimeout(() => {
      if (App.workspacePathInput) App.workspacePathInput.focus();
    }, 120);
  };
  App.closeWorkspaceModal = function closeWorkspaceModal() {
    App.workspaceModal!.classList.remove('show');
  };

  /* ---------- 读取当前工作区 ---------- */
  App.refreshWorkspace = async function refreshWorkspace() {
    try {
      const res = await fetch('/api/workspace');
      if (!res.ok) throw new Error('HTTP ' + res.status);
      const data = await res.json();
      const cwd = data.cwd || '';
      if (App.workspaceCurrentPath) App.workspaceCurrentPath.textContent = cwd || '（未设置）';
      if (App.workspacePathInput) App.workspacePathInput.value = cwd || '';
    } catch (e) {
      const err = e as Error;
      App.showToast('读取工作区失败: ' + (err.message || e));
      if (App.workspaceCurrentPath) App.workspaceCurrentPath.textContent = '（读取失败）';
    }
    await App.loadWorkspaceDirs();
    await App.loadSavedWorkspaces();
  };

  /* ---------- 已保存工作区列表 ---------- */
  App.loadSavedWorkspaces = async function loadSavedWorkspaces() {
    const el = savedEl();
    if (!el) return;
    el.innerHTML = '<div style="text-align:center;color:#8888aa;padding:12px">加载中…</div>';
    try {
      const res = await fetch('/api/workspaces');
      if (!res.ok) throw new Error('HTTP ' + res.status);
      const data = await res.json();
      const items: { path: string; exists: boolean }[] = data.workspaces || [];
      const current = data.current || '';
      el.innerHTML = '';
      if (!items.length) {
        el.innerHTML = '<div style="text-align:center;color:#8888aa;padding:12px">还没有保存的工作区，浏览目录后点「收藏」添加</div>';
        return;
      }
      for (const it of items) {
        const isCur = it.path === current;
        const row = document.createElement('div');
        row.className = 'ws-saved-item' + (isCur ? ' current' : '');
        row.innerHTML =
          '<span class="ws-saved-mark">' + (isCur ? '✓' : '·') + '</span>'
          + '<span class="ws-saved-path' + (it.exists ? '' : ' gone') + '">' + esc(it.path) + '</span>'
          + '<span class="ws-saved-actions">'
          + '<button class="ws-saved-use' + (isCur ? ' disabled' : '') + '" title="切换到此工作区"' + (isCur ? ' disabled' : '') + '>使用</button>'
          + '<button class="ws-saved-del" title="移出列表">✕</button>'
          + '</span>';
        row.querySelector('.ws-saved-use')!.addEventListener('click', async (e) => {
          e.stopPropagation();
          if (isCur) return;
          await App.activateSavedWorkspace(it.path);
        });
        row.querySelector('.ws-saved-del')!.addEventListener('click', async (e) => {
          e.stopPropagation();
          if (!confirm('把该路径移出已保存列表？（不会删除磁盘上的目录）')) return;
          try {
            const res = await fetch('/api/workspaces', {
              method: 'DELETE',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ path: it.path }),
            });
            if (!res.ok) throw new Error('HTTP ' + res.status);
            await App.loadSavedWorkspaces();
          } catch (e2) {
            const err = e2 as Error;
            App.showToast('移除失败: ' + (err.message || e2));
          }
        });
        el.appendChild(row);
      }
    } catch (e) {
      const err = e as Error;
      el.innerHTML = '<div style="text-align:center;color:#8888aa;padding:12px">加载失败: ' + esc(err.message || String(e)) + '</div>';
    }
  };

  /* ---------- 激活已保存工作区 ---------- */
  App.activateSavedWorkspace = async function activateSavedWorkspace(path: string) {
    try {
      const res = await fetch('/api/workspaces/' + encodeURIComponent(path) + '/activate', {
        method: 'POST',
      });
      const data = await res.json();
      if (!res.ok) throw new Error((data && data.detail) || ('HTTP ' + res.status));
      if (App.workspaceCurrentPath) App.workspaceCurrentPath.textContent = data.cwd || path;
      if (App.workspacePathInput) App.workspacePathInput.value = data.cwd || path;
      App.showToast('已切换到: ' + (data.cwd || path));
      await App.loadSavedWorkspaces();
    } catch (e) {
      const err = e as Error;
      App.showToast('切换失败: ' + (err.message || e));
    }
  };

  /* ---------- 收藏当前路径到已保存列表 ---------- */
  App.saveWorkspaceToSaved = async function saveWorkspaceToSaved() {
    const path = App.workspacePathInput!.value.trim();
    if (!path) { App.showToast('请先填写/选择路径'); return; }
    try {
      const res = await fetch('/api/workspaces', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error((data && data.detail) || ('HTTP ' + res.status));
      App.showToast('已收藏: ' + path);
      await App.loadSavedWorkspaces();
    } catch (e) {
      const err = e as Error;
      App.showToast('收藏失败: ' + (err.message || e));
    }
  };

  /* ---------- 目录下钻浏览 ---------- */
  App.loadWorkspaceDirs = async function loadWorkspaceDirs() {
    const el = rootsEl();
    if (!el) return;
    const current = browseStack.length ? browseStack[browseStack.length - 1] : '';
    if (browsePathEl()) {
      browsePathEl().textContent = current || '根目录';
    }
    if (upBtn()) {
      upBtn().style.visibility = browseStack.length ? 'visible' : 'hidden';
    }
    el.innerHTML = '<div style="text-align:center;color:#8888aa;padding:14px">加载中…</div>';
    try {
      const q = current ? '?path=' + encodeURIComponent(current) : '';
      const res = await fetch('/api/workspace/list' + q);
      if (!res.ok) throw new Error('HTTP ' + res.status);
      const data = await res.json();
      const dirs: DirEntry[] = data.dirs || [];
      el.innerHTML = '';
      if ((data.error && !dirs.length) || (!dirs.length && !data.error)) {
        el.innerHTML = '<div style="text-align:center;color:#8888aa;padding:14px">'
          + esc(data.error || '这个目录下没有子目录') + '</div>';
        return;
      }
      for (const d of dirs) {
        const item = document.createElement('div');
        item.className = 'ws-root-item';
        item.innerHTML =
          '<span class="ws-dir-icon">📁</span>'
          + '<span class="ws-root-label ws-dir-name">' + esc(d.name) + '</span>'
          + '<span class="ws-dir-actions">'
          + '<button class="ws-dir-open" title="进入此目录">进入</button>'
          + '<button class="ws-dir-pick" title="选此目录并收藏">收藏</button>'
          + '</span>';
        item.querySelector('.ws-dir-open')!.addEventListener('click', (e) => {
          e.stopPropagation();
          browseStack.push(d.path);
          App.loadWorkspaceDirs();
        });
        item.querySelector('.ws-dir-pick')!.addEventListener('click', (e) => {
          e.stopPropagation();
          if (App.workspacePathInput) App.workspacePathInput.value = d.path;
          App.saveWorkspaceToSaved();
        });
        item.addEventListener('click', () => {
          browseStack.push(d.path);
          App.loadWorkspaceDirs();
        });
        el.appendChild(item);
      }
    } catch (e) {
      const err = e as Error;
      el.innerHTML = '<div style="text-align:center;color:#8888aa;padding:14px">加载失败: '
        + esc(err.message || String(e)) + '</div>';
    }
  };

  App.workspaceGoUp = function workspaceGoUp() {
    if (browseStack.length) browseStack.pop();
    App.loadWorkspaceDirs();
  };

  /* ---------- 保存为当前工作区（所有智能体生效） ---------- */
  App.saveWorkspace = async function saveWorkspace() {
    const path = App.workspacePathInput!.value.trim();
    if (!path) { App.showToast('请输入工作区路径'); return; }
    try {
      const res = await fetch('/api/workspace', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error((data && data.detail) || ('HTTP ' + res.status));
      if (App.workspaceCurrentPath) App.workspaceCurrentPath.textContent = data.cwd || path;
      App.showToast('工作区已切换: ' + (data.cwd || path));
      await App.loadSavedWorkspaces();
    } catch (e) {
      const err = e as Error;
      App.showToast('设置失败: ' + (err.message || e));
    }
  };
}