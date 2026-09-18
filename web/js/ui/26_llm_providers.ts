import type { AppKernel } from '../types/app-kernel.js';

export default (function init(App: AppKernel) {
  /* ============================================================
   *  模型供应商（全局资源）：大厅「供应商」按钮 → 列表 / 添加 / 编辑 / 删除 / 设为当前
   * ============================================================ */
  App.openProviderModal = function openProviderModal() {
    // 供应商是全局凭证库（/api/llm、/api/config 对普通用户 403）。闸门放在打开函数里：
    // 轮盘「供应商」按钮、角色卡「管理供应商」两条入口共用。
    if (window.__ROLE !== 'admin') {
      App.showToast('模型供应商仅管理员可管理');
      return;
    }
    App.providerModal?.classList.add('show');
    App.refreshProviderList();
  };
  App.closeProviderModal = function closeProviderModal() {
    App.providerModal?.classList.remove('show');
  };

  App.refreshProviderList = async function refreshProviderList() {
    App.loadLlmProxyConfig?.();
    if (App.providerList) {
      App.providerList.innerHTML = '<div style="text-align:center;color:var(--text-dim);padding:20px">加载中…</div>';
    }
    try {
      const res = await fetch('/api/llm/providers');
      const data = await res.json();
      const providers = (data && data.providers) || [];
      App.renderProviderList(providers);
      if (App.providerModalActive) {
        const active = (data && data.active_id) || '';
        const ap = providers.find(p => p.id === active);
        App.providerModalActive.textContent = active
          ? ('当前使用：' + (ap ? ap.name : active))
          : '未设置当前供应商';
      }
    } catch (e) {
      if (App.providerList) {
        App.providerList.innerHTML = '<div style="text-align:center;color:var(--text-dim);padding:20px">加载失败</div>';
      }
    }
  };

  App.renderProviderList = function renderProviderList(providers) {
    const listEl = App.providerList;
    if (!listEl) return;
    listEl.innerHTML = '';
    if (!providers.length) {
      listEl.innerHTML = '<div style="text-align:center;color:var(--text-dim);padding:20px">还没有供应商，点下方「添加模型供应商」</div>';
      return;
    }
    for (const p of providers) {
      const item = document.createElement('div');
      item.className = 'provider-item' + (p._active ? ' active' : '');
      const kindLabel = p.kind === 'ollama' ? 'Ollama 本地' : '自定义 API';
      const modelLabel = p.default_model ? p.default_model : '（未设默认模型）';
      const visionLabel = p.vision === true ? '读图：强制开启' : (p.vision === false ? '读图：强制关闭' : '读图：自动判定');
      item.innerHTML = `
        <div class="provider-item-main">
          <div class="provider-item-name">${App.escapeHtml(p.name)} ${p._active ? '<span class="provider-item-badge">当前使用</span>' : ''}</div>
          <div class="provider-item-meta">${kindLabel} · ${App.escapeHtml(p.base_url || '未配置 Base URL')}</div>
          <div class="provider-item-meta">默认模型：${App.escapeHtml(modelLabel)}</div>
          <div class="provider-item-meta">${visionLabel}</div>
        </div>
        <div class="provider-item-actions">
          ${p._active ? '' : '<button class="role-card-edit" data-act="1" title="设为当前使用">启用</button>'}
          <button class="role-card-edit" data-edit="1" title="编辑">编辑</button>
          <button class="model-delete" data-del="1" title="删除">删</button>
        </div>
      `;
      const actBtn = item.querySelector('[data-act]') as HTMLElement | null;
      if (actBtn) {
        actBtn.addEventListener('click', () => App.activateProvider(p.id));
      }
      (item.querySelector('[data-edit]') as HTMLElement).addEventListener('click', () => App.openProviderEditor(p));
      (item.querySelector('[data-del]') as HTMLElement).addEventListener('click', () => App.deleteProvider(p.id));
      listEl.appendChild(item);
    }
  };

  App.openProviderEditor = function openProviderEditor(provider) {
    if (App.providerEditTitle) App.providerEditTitle.textContent = provider ? '编辑模型供应商' : '添加模型供应商';
    if (App.providerName) App.providerName.value = (provider && provider.name) || '';
    if (App.providerKind) App.providerKind.value = (provider && provider.kind) || 'custom';
    if (App.providerBaseUrl) App.providerBaseUrl.value = (provider && provider.base_url) || '';
    if (App.providerApiKey) App.providerApiKey.value = (provider && provider.api_key) || '';
    if (App.providerDefaultModel) App.providerDefaultModel.value = (provider && provider.default_model) || '';
    if (App.providerVision) {
      App.providerVision.value = provider && provider.vision === true
        ? 'true'
        : (provider && provider.vision === false ? 'false' : '');
    }
    if (App.providerDeleteBtn) App.providerDeleteBtn.style.display = provider ? '' : 'none';
    if (App.providerModels) {
      App.providerModels.style.display = 'none';
      App.providerModels.innerHTML = '';
    }
    if (App.providerTestResult) App.providerTestResult.textContent = '按上方地址测试连通性，成功后可选默认模型';
    App._editingProviderId = provider ? provider.id : null;
    App.providerEditModal?.classList.add('show');
  };

  App.saveProvider = async function saveProvider() {
    const name = App.providerName?.value.trim() || '';
    if (!name) {
      App.showToast('请填写供应商名称');
      return;
    }
    const base_url = App.providerBaseUrl?.value.trim() || '';
    if (!base_url) {
      App.showToast('请填写 Base URL');
      return;
    }
    const body = {
      name,
      kind: App.providerKind?.value || 'custom',
      base_url,
      api_key: App.providerApiKey?.value.trim() || '',
      default_model: App.providerDefaultModel?.value.trim() || '',
      // 三态：''=自动（交给模型名+实测判定），true/false=用户强制
      vision: App.providerVision?.value === 'true' ? true
        : (App.providerVision?.value === 'false' ? false : null)
    };
    const editingId = App._editingProviderId;
    try {
      let res;
      if (editingId) {
        res = await fetch(`/api/llm/providers/${encodeURIComponent(editingId)}`, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body)
        });
      } else {
        res = await fetch('/api/llm/providers', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ ...body, activate: false })
        });
      }
      if (!res.ok) {
        const d = await res.json().catch(() => ({}));
        throw new Error(d.message || d.detail || `HTTP ${res.status}`);
      }
      App.llmGlobalConfig = null;
      App.llmProvidersCache = null;
      App.showToast(editingId ? '供应商已更新' : '供应商已添加');
      App.providerEditModal?.classList.remove('show');
      await App.refreshProviderList();
      App.loadRcLlmGlobalConfig?.(true);
    } catch (err) {
      App.showToast('保存供应商失败：' + ((err as Error).message || err));
    }
  };

  App.deleteProvider = async function deleteProvider(pid) {
    if (!confirm('删除这个供应商？引用它的角色卡片将改回「跟随全局当前供应商」。')) return;
    try {
      const res = await fetch(`/api/llm/providers/${encodeURIComponent(pid)}`, { method: 'DELETE' });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      App.llmGlobalConfig = null;
      App.llmProvidersCache = null;
      App.showToast('供应商已删除');
      await App.refreshProviderList();
      App.loadRcLlmGlobalConfig?.(true);
    } catch (err) {
      App.showToast('删除供应商失败');
    }
  };

  App.activateProvider = async function activateProvider(pid) {
    try {
      const res = await fetch(`/api/llm/providers/${encodeURIComponent(pid)}/activate`, { method: 'POST' });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      App.llmGlobalConfig = null;
      App.llmProvidersCache = null;
      App.showToast('已启用供应商：' + ((data.provider && data.provider.name) || pid));
      await App.refreshProviderList();
      App.loadRcLlmGlobalConfig?.(true);
    } catch (err) {
      App.showToast('启用供应商失败');
    }
  };

  App.testProvider = async function testProvider() {
    const kind = App.providerKind?.value || 'custom';
    const base_url = App.providerBaseUrl?.value.trim() || '';
    const api_key = App.providerApiKey?.value.trim() || '';
    const resultEl = App.providerTestResult;
    const modelsEl = App.providerModels;
    if (!base_url) {
      if (resultEl) resultEl.textContent = '请先填写 Base URL';
      return;
    }
    if (resultEl) resultEl.textContent = '正在测试…';
    if (modelsEl) {
      modelsEl.style.display = 'none';
      modelsEl.innerHTML = '';
    }
    try {
      const res = await fetch('/api/llm/providers/test', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ kind, base_url, api_key })
      });
      const data = await res.json();
      const list = data.models || [];
      if (data.error) {
        if (resultEl) resultEl.textContent = '连接失败：' + data.error;
        return;
      }
      if (modelsEl) {
        modelsEl.style.display = '';
        modelsEl.innerHTML = '<option value="">（可选为默认模型）</option>';
        for (const m of list) {
          const opt = document.createElement('option');
          opt.value = m;
          opt.textContent = m;
          modelsEl.appendChild(opt);
        }
      }
      if (resultEl) {
        resultEl.textContent = `连接成功，共 ${list.length} 个模型` + (list.length ? '，可直接选为默认模型' : '');
      }
    } catch (e) {
      if (resultEl) resultEl.textContent = '测试失败：' + String((e as Error).message || e);
    }
  };

  /* ---------- 云端 API 网络代理（LLM / TTS / STT / 画图） ---------- */
  App.loadLlmProxyConfig = async function loadLlmProxyConfig() {
    try {
      const res = await fetch('/api/llm/config');
      const data = await res.json();
      const val = (data && data.llm_proxy) || '';
      const mode = (!val || val === 'off') ? 'off' : (val === 'auto' ? 'auto' : 'custom');
      if (App.llmProxyMode) App.llmProxyMode.value = mode;
      if (App.llmProxyUrl) {
        App.llmProxyUrl.value = (mode === 'custom') ? val : '';
        App.llmProxyUrl.style.display = (mode === 'custom') ? '' : 'none';
      }
      if (App.llmProxyStatus) {
        App.llmProxyStatus.textContent = mode === 'auto'
          ? '自动模式：云端 API 走 fq 本地代理，未启动会自动拉起 Clash（同在线视频）'
          : (mode === 'custom' ? `自定义代理：${val}` : '当前直连，不走代理');
      }
    } catch (e) { /* 弹窗只读加载，失败静默 */ }
  };

  App.saveLlmProxy = async function saveLlmProxy() {
    const mode = App.llmProxyMode?.value || 'off';
    let val = mode;
    if (mode === 'custom') {
      val = (App.llmProxyUrl?.value || '').trim();
      if (!val) {
        App.showToast('请填写自定义代理地址');
        return;
      }
    }
    try {
      const res = await fetch('/api/llm/config', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ llm_proxy: val })
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      App.llmGlobalConfig = null;
      App.showToast('代理设置已保存并生效');
      await App.loadLlmProxyConfig?.();
    } catch (err) {
      App.showToast('保存代理设置失败：' + String((err as Error).message || err));
    }
  };

  /** 供应商编辑弹窗里「选为默认模型」联动 */
  App.providerModels?.addEventListener('change', () => {
    if (App.providerDefaultModel && App.providerModels) {
      App.providerDefaultModel.value = App.providerModels.value;
    }
  });

  /* ---------- 事件绑定，只做一次 ---------- */
  App.llmProviderBtn?.addEventListener('click', () => App.openProviderModal());
  App.providerModalClose?.addEventListener('click', () => App.closeProviderModal());
  App.providerModal?.querySelector('.modal-backdrop')?.addEventListener('click', () => App.closeProviderModal());
  App.providerCreateBtn?.addEventListener('click', () => App.openProviderEditor(null));
  App.providerEditClose?.addEventListener('click', () => App.providerEditModal?.classList.remove('show'));
  App.providerEditModal?.querySelector('.modal-backdrop')?.addEventListener('click', () => App.providerEditModal?.classList.remove('show'));
  App.providerSaveBtn?.addEventListener('click', () => App.saveProvider());
  App.providerDeleteBtn?.addEventListener('click', () => {
    const pid = App._editingProviderId;
    if (pid) App.deleteProvider(pid);
  });
  App.providerTestBtn?.addEventListener('click', () => App.testProvider());
  App.llmProxyMode?.addEventListener('change', () => {
    if (App.llmProxyUrl) {
      App.llmProxyUrl.style.display = (App.llmProxyMode?.value === 'custom') ? '' : 'none';
    }
  });
  App.llmProxySaveBtn?.addEventListener('click', () => App.saveLlmProxy());
});
