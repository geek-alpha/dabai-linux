import type { AppKernel, BackgroundInfo } from '../types/app-kernel.js';

export default (function init(App: AppKernel) {
  /* ============================================================
   *  背景场景管理 UI
   * ============================================================ */
  App.openBgModal = function openBgModal() {
    App.bgModal!.classList.add('show');
    App.refreshBackgroundList();
  };
  App.closeBgModal = function closeBgModal() {
    App.bgModal!.classList.remove('show');
  };
  App.refreshBackgroundList = async function refreshBackgroundList() {
    try {
      const res = await fetch('/api/backgrounds');
      const data = await res.json() as { backgrounds?: BackgroundInfo[] };
      App.renderBackgroundList(data.backgrounds || []);
    } catch (e) {
      App.showToast('获取背景列表失败');
    }
  };
  App.renderBackgroundList = function renderBackgroundList(items: BackgroundInfo[]) {
    App.bgListEl!.innerHTML = '';
    const currentSaved: { url: string; name: string } | null = JSON.parse(localStorage.getItem('dabai.currentBackground') || 'null');
    // 默认背景卡片（内置星空背景，无文件），注册为 'default'，始终显示在列表首位
    const usingDefault = !currentSaved || currentSaved.url === 'default';
    const defaultCard = document.createElement('div');
    defaultCard.className = 'model-card';
    defaultCard.dataset.bg = 'default';
    defaultCard.dataset.name = 'default';
    if (usingDefault) defaultCard.classList.add('active');
    defaultCard.innerHTML = `
            <div class="model-preview default-preview">
                <svg viewBox="0 0 24 24" width="32" height="32"><path fill="currentColor" d="M20 4H4c-1.1 0-2 .9-2 2v12c0 1.1.9 2 2 2h16c1.1 0 2-.9 2-2V6c0-1.1-.9-2-2-2zm0 14H4V6h16v12zm-9-3l-2-2.5L7 16h10l-3.5-4.5z"/></svg>
            </div>
            <div class="model-info">
                <div class="model-name">默认背景 · 星空</div>
                <div class="model-meta">默认星空</div>
            </div>
            <div class="model-check" style="display:${usingDefault ? 'block' : 'none'}">✓</div>
        `;
    defaultCard.addEventListener('click', () => {
      App.useDefaultBackground();
      App.closeBgModal();
    });
    App.bgListEl!.appendChild(defaultCard);
    for (const m of items) {
      // default 卡片已在上面固定渲染，跳过数据里的默认条目避免重复
      if (m.is_default || m.name === 'default') continue;
      const card = document.createElement('div');
      card.className = 'model-card';
      card.dataset.url = m.url;
      card.dataset.name = m.name;
      const isActive = currentSaved && currentSaved.url === m.url;
      if (isActive) card.classList.add('active');
      const sizeKB = (m.size / 1024).toFixed(0);
      const sizeStr = Number(sizeKB) > 1024 ? `${(Number(sizeKB) / 1024).toFixed(1)}MB` : `${sizeKB}KB`;
      card.innerHTML = `
            <div class="model-preview">${m.type.toUpperCase()}</div>
            <div class="model-info">
                <div class="model-name">${App.escapeHtml(m.name)}</div>
                <div class="model-meta">${m.type.toUpperCase()} · ${sizeStr}</div>
            </div>
            <div class="model-actions">
                <button class="model-rename" title="重命名">
                    <svg viewBox="0 0 24 24" width="16" height="16"><path fill="currentColor" d="M3 17.25V21h3.75L17.81 9.94l-3.75-3.75L3 17.25zM20.71 7.04c.39-.39.39-1.02 0-1.41l-2.34-2.34c-.39-.39-1.02-.39-1.41 0l-1.83 1.83 3.75 3.75 1.83-1.83z"/></svg>
                </button>
                <button class="model-delete" title="删除">
                    <svg viewBox="0 0 24 24" width="18" height="18"><path fill="currentColor" d="M6 19c0 1.1.9 2 2 2h8c1.1 0 2-.9 2-2V7H6v12zM19 4h-3.5l-1-1h-5l-1 1H5v2h14V4z"/></svg>
                </button>
            </div>
            <div class="model-check" style="display:${isActive ? 'block' : 'none'}">✓</div>
        `;
      const nameEl = card.querySelector('.model-name') as HTMLElement;
      const renameBtn = card.querySelector('.model-rename') as HTMLElement;

      // 点击切换背景
      card.addEventListener('click', e => {
        if ((e.target as HTMLElement).closest('.model-delete, .model-rename, .model-name-edit')) return;
        App.loadBackgroundFromUrl(m.url, m.name);
        App.closeBgModal();
      });

      // 重命名
      renameBtn.addEventListener('click', async e => {
        e.stopPropagation();
        const oldName = nameEl.textContent || '';
        const input = document.createElement('input');
        input.className = 'model-name-edit';
        input.value = oldName;
        nameEl.replaceWith(input);
        input.focus();
        input.select();
        const doRename = async () => {
          const newName = input.value.trim();
          input.replaceWith(nameEl);
          if (!newName || newName === oldName) return;
          try {
            const res = await fetch(`/api/background/${encodeURIComponent(oldName)}/rename`, {
              method: 'PUT',
              headers: {
                'Content-Type': 'application/json'
              },
              body: JSON.stringify({
                new_name: newName
              })
            });
            if (!res.ok) {
              const err = await res.json().catch(() => ({} as { detail?: string }));
              throw new Error(err.detail || `HTTP ${res.status}`);
            }
            const data = await res.json() as { new_name: string; url: string };
            card.dataset.name = data.new_name;
            card.dataset.url = data.url;
            nameEl.textContent = data.new_name;
            // 如果重命名的是当前使用的背景，更新 localStorage
            if (currentSaved && currentSaved.url === `/backgrounds/${oldName}`) {
              currentSaved.url = data.url;
              currentSaved.name = data.new_name;
              localStorage.setItem('dabai.currentBackground', JSON.stringify(currentSaved));
            }
            App.showToast('已重命名');
          } catch (err) {
            App.showToast('重命名失败：' + ((err as Error).message || err));
          }
        };
        input.addEventListener('keydown', e => {
          if (e.key === 'Enter') {
            e.preventDefault();
            doRename();
          }
          if (e.key === 'Escape') {
            input.value = oldName;
            input.replaceWith(nameEl);
          }
        });
        input.addEventListener('blur', doRename);
      });

      // 删除
      (card.querySelector('.model-delete') as HTMLElement).addEventListener('click', async e => {
        e.stopPropagation();
        if (!confirm(`删除背景 ${m.name}？`)) return;
        try {
          await fetch(`/api/background/${encodeURIComponent(m.name)}`, {
            method: 'DELETE'
          });
          if (currentSaved && currentSaved.url === m.url) localStorage.removeItem('dabai.currentBackground');
          App.refreshBackgroundList();
          App.showToast('已删除');
        } catch (err) {
          App.showToast('删除失败');
        }
      });
      App.bgListEl!.appendChild(card);
    }
    App.refreshBgListSelection(currentSaved && currentSaved.url !== 'default' ? currentSaved.name : null);
  };
  App.uploadBackgroundFile = async function uploadBackgroundFile(file: File) {
    const ext = file.name.split('.').pop()!.toLowerCase();
    if (!['glb', 'gltf', 'vrm'].includes(ext)) {
      App.showToast('仅支持 .glb / .gltf / .vrm');
      return;
    }
    if (file.size > 80 * 1024 * 1024) {
      App.showToast('文件超过 80MB 上限');
      return;
    }
    App.showModelLoading(`上传背景 ${file.name} …`);
    try {
      const fd = new FormData();
      fd.append('file', file);
      const res = await fetch('/api/background/upload', {
        method: 'POST',
        body: fd
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({} as { detail?: string }));
        throw new Error(err.detail || `HTTP ${res.status}`);
      }
      const data = await res.json() as { url: string; name: string };
      App.showToast('上传成功，正在加载…');
      await App.loadBackgroundFromUrl(data.url, data.name);
      App.closeBgModal();
    } catch (err) {
      App.showToast('上传失败：' + ((err as Error).message || err));
    } finally {
      App.hideModelLoading();
    }
  };
  /* ============================================================
   *  事件绑定
   * ============================================================ */
});
