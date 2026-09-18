import type { AppKernel } from '../types/app-kernel.js';

export default (function init(App: AppKernel) {
  const {
    THREE: THREE,
    GLTFLoader: GLTFLoader,
    VRMLoaderPlugin: VRMLoaderPlugin,
    VRMUtils: VRMUtils
  } = App;
  /* ============================================================
   *  角色卡片：外形（模型）+ 声音（TTS）+ 系统提示词（人设）
   *           + 称呼 + 工具调用（开关 + 工具白名单）
   *           + 大语言模型（model / base_url / api_key，留空沿用默认）
   *  一键切换整套角色配置
   * ============================================================ */
  App.roleCardActiveId = localStorage.getItem('dabai.activeRoleCard') || null;

  // 卡片分两层权限：切换（apply）谁都能点——点一张卡就换成它的人设/外形/声音；
  // 编辑（新建/改/删）只留给管理员，卡片写的是全机唯一一套运行配置
  // （settings.json + tts_config.json），服务端对卡片增删改一律 403。
  const canManageCards = () => window.__ROLE === 'admin';

  App.initRoleCards = function initRoleCards() {
    // 打开 / 关闭列表弹窗
    App.roleCardBtn?.addEventListener('click', App.openRoleCardModal);
    App.roleCardModalClose?.addEventListener('click', () => App.roleCardModal.classList.remove('show'));
    App.roleCardModal?.querySelector('.modal-backdrop')?.addEventListener('click', () => App.roleCardModal.classList.remove('show'));
    // 新建卡片
    App.roleCardCreateBtn?.addEventListener('click', () => App.openRoleCardEditor(null));
    // 编辑弹窗关闭
    App.roleCardEditClose?.addEventListener('click', () => App.roleCardEditModal.classList.remove('show'));
    App.roleCardEditModal?.querySelector('.modal-backdrop')?.addEventListener('click', () => App.roleCardEditModal.classList.remove('show'));
    // 引擎 tab
    App.rcTtsTabs?.querySelectorAll('.tts-tab').forEach(tab => {
      tab.addEventListener('click', () => App.switchRcTTSEngine((tab as HTMLElement).dataset.engine));
    });
    // 语速滑块实时显示
    App.rcRateRange?.addEventListener('input', () => {
      const v = parseInt(App.rcRateRange.value);
      App.rcRateVal.textContent = (v >= 0 ? '+' : '') + v + '%';
    });
    // 保存并切换
    App.rcApplyBtn?.addEventListener('click', App.saveRoleCard);
    // 删除
    App.rcDeleteBtn?.addEventListener('click', App.deleteRoleCard);
    // 编辑弹窗内上传新模型
    App.rcModelUploadBtn?.addEventListener('click', () => App.rcModelFileInput?.click());
    App.rcModelFileInput?.addEventListener('change', e => {
      const f = (e.target as HTMLInputElement).files[0];
      if (f) App.uploadModelFile(f);
      (e.target as HTMLInputElement).value = '';
    });
    // 大语言模型（供应商是全局资源，在「大厅 → 供应商」管理）：卡片内选供应商 + 模型
    App.rcLlmProviderSelect?.addEventListener('change', () => {
      App.switchRcLlmProvider(App.rcLlmProviderSelect?.value || '');
    });
    App.rcLlmManageBtn?.addEventListener('click', () => {
      if (App.openProviderModal) App.openProviderModal();
    });
    App.rcLlmRefreshBtn?.addEventListener('click', () => {
      App.loadRcLlmModels();
    });
    App.rcLlmTemperature?.addEventListener('input', () => {
      if (App.rcLlmTempVal) {
        App.rcLlmTempVal.textContent = Number(App.rcLlmTemperature.value).toFixed(2);
      }
    });
    // 读图能力：卡片绑的模型跟供应商默认模型常常不是同一个，改动即重判
    App.rcLlmVision?.addEventListener('change', () => App.refreshRcLlmVisionTip?.());
    App.rcLlmModel?.addEventListener('change', () => App.refreshRcLlmVisionTip?.());
    // 缓存全局默认温度（卡片未单独设定时沿用）
    App.rcLlmDefaultTemp = 0.2;
    fetch('/api/llm/config').then(r => r.json()).then(cfg => {
      if (cfg && cfg.temperature != null) App.rcLlmDefaultTemp = Number(cfg.temperature);
    }).catch(() => {});
    // 工具开关：关闭时禁用工具多选列表
    App.rcToolsEnabled?.addEventListener('change', () => {
      App.rcToolsField?.classList.toggle('disabled', !App.rcToolsEnabled.checked);
    });
    // 专属动作开关：关闭时禁用动作多选列表
    App.rcAnimEnabled?.addEventListener('change', () => {
      App.rcAnimField?.classList.toggle('disabled', !App.rcAnimEnabled.checked);
    });
  };

  /* ---------- 列表弹窗 ---------- */
  App.openRoleCardModal = async function openRoleCardModal() {
    App.roleCardModal?.classList.add('show');
    await App.refreshRoleCardList();
  };

  App.refreshRoleCardList = async function refreshRoleCardList() {
    try {
      await App.loadRcLlmGlobalConfig(true);
      const _uid = localStorage.getItem('dabai.userId') || '';
      const res = await fetch('/api/character_cards?user_id=' + encodeURIComponent(_uid));
      const data = await res.json();
      App.renderRoleCardList(data.cards || []);
    } catch (e) {
      App.roleCardList.innerHTML = '<div style="text-align:center;color:var(--text-dim);padding:20px">加载失败</div>';
    }
  };

  App.renderRoleCardList = function renderRoleCardList(cards) {
    App.roleCardList.innerHTML = '';
    if (!cards.length) {
      App.roleCardList.innerHTML = canManageCards()
        ? '<div style="text-align:center;color:var(--text-dim);padding:20px">还没有角色卡片，点击下方按钮从当前配置创建</div>'
        : '<div style="text-align:center;color:var(--text-dim);padding:20px">还没有角色卡片</div>';
      return;
    }
    for (const c of cards) {
      const card = document.createElement('div');
      card.className = 'role-card';
      card.dataset.id = c.id;
      const isActive = App.roleCardActiveId === c.id;
      if (isActive) card.classList.add('active');
      const tts = c.tts || {};
      const voiceLabel = tts.engine === 'gpt_sovits'
        ? (tts.gptsovits_character || 'GPT-SoVITS')
        : (tts.edge_voice || '默认音色');
      const modelLabel = c.model_name ? c.model_name.replace(/\.(glb|gltf|vrm)$/i, '') : '默认外形';
      const promptBrief = (c.system_prompt || '').slice(0, 24) || '（未填写系统提示词）';
      const toolsCfg = c.tools;
      const toolsLabel = toolsCfg
        ? (toolsCfg.enabled === false
            ? '工具：关'
            : (toolsCfg.allowed && toolsCfg.allowed.length ? `工具：${toolsCfg.allowed.length}个` : '工具：全开'))
        : '';
      const animCfg = c.animations;
      const animLabel = animCfg
        ? (animCfg.enabled === false
            ? '动作：关'
            : (animCfg.allowed && animCfg.allowed.length ? `动作：${animCfg.allowed.length}个` : '动作：全开'))
        : '';
      const llmCfg = c.llm || {};
      const llmProviderName = App.providerNameById(llmCfg.provider_id || '');
      let llmLabel = '';
      if (llmCfg.provider_id || llmCfg.model) {
        llmLabel = llmProviderName
          ? `大模型：${llmProviderName}${llmCfg.model ? ' · ' + llmCfg.model : ''}`
          : (llmCfg.model ? `大模型：${llmCfg.model}` : '');
      }
      card.innerHTML = `
                <div class="role-card-avatar">${App.escapeHtml((c.name || '?').slice(0, 1))}</div>
                <div class="role-card-info">
                    <div class="role-card-name">${App.escapeHtml(c.name)}</div>
                    <div class="role-card-role">角色：${App.escapeHtml(c.role_name || c.name || 'AI助手')}</div>
                    <div class="role-card-meta">
                        <span class="role-card-tag">${App.escapeHtml(modelLabel)}</span>
                        <span class="role-card-tag">${App.escapeHtml(voiceLabel)}</span>
                        ${c.user_name ? `<span class="role-card-tag">称呼：${App.escapeHtml(c.user_name)}</span>` : ''}
                        ${toolsLabel ? `<span class="role-card-tag">${App.escapeHtml(toolsLabel)}</span>` : ''}
                        ${animLabel ? `<span class="role-card-tag">${App.escapeHtml(animLabel)}</span>` : ''}
                        ${llmLabel ? `<span class="role-card-tag">${App.escapeHtml(llmLabel)}</span>` : ''}
                        <span class="role-card-tag">${App.escapeHtml(promptBrief)}</span>
                    </div>
                </div>
                ${canManageCards() ? `
                <div class="role-card-actions">
                    <button class="role-card-edit" title="编辑">
                        <svg viewBox="0 0 24 24" width="15" height="15"><path fill="currentColor" d="M3 17.25V21h3.75L17.81 9.94l-3.75-3.75L3 17.25zM20.71 7.04c.39-.39.39-1.02 0-1.41l-2.34-2.34c-.39-.39-1.02-.39-1.41 0l-1.83 1.83 3.75 3.75 1.83-1.83z"/></svg>
                    </button>
                </div>` : ''}
                <div class="role-card-check" style="display:${isActive ? 'block' : 'none'}">✓</div>
            `;
      // 点击卡片 → 一键切换（所有登录用户都能切；编辑按钮只对管理员渲染）
      card.addEventListener('click', e => {
        if ((e.target as HTMLElement).closest('.role-card-edit')) return;
        App.applyRoleCard(c.id);
      });
      // 编辑
      card.querySelector('.role-card-edit')?.addEventListener('click', e => {
        e.stopPropagation();
        App.openRoleCardEditor(c.id);
      });
      App.roleCardList.appendChild(card);
    }
  };

  /* ---------- 编辑弹窗 ---------- */
  App.openRoleCardEditor = async function openRoleCardEditor(cardId) {
    if (!canManageCards()) {
      App.showToast('角色卡片由管理员管理');
      return;
    }
    // 每次打开编辑器重置预填模型缓存，避免上一次编辑的模型串进来
    App._rcPresetModel = '';
    App.rcEditingId = cardId || null;
    App.rcDeleteBtn.style.display = cardId ? '' : 'none';
    document.getElementById('role-card-edit-title').textContent = cardId ? '编辑角色卡片' : '新建角色卡片';
    App.roleCardEditModal?.classList.add('show');

    // 懒加载模型列表
    await App.loadRcModels();
    // 懒加载音色列表
    await App.loadRcVoices();
    // 懒加载工具列表
    await App.loadRcTools();
    // 懒加载动作库配置（专属动作多选列表）
    await App.loadRcAnims();
    // 懒加载语音识别（STT）独立配置
    App.loadRcSttConfig();

    if (cardId) {
      // 编辑：用卡片数据回填
      try {
        const res = await fetch('/api/character_cards');
        const data = await res.json();
        const c = (data.cards || []).find(x => x.id === cardId);
        if (c) {
          await App.fillRoleCardForm(c);
          return;
        }
      } catch (e) { /* 回退到当前配置 */ }
    }
    // 新建：捕获当前配置回填
    const current = await App.captureCurrentRole();
    await App.fillRoleCardForm(current);
  };

  App.captureCurrentRole = async function captureCurrentRole() {
    const current = {
      name: '',
      role_name: 'AI助手',
      user_name: '',
      model_url: '',
      model_name: '',
      system_prompt: '',
      tts: {
        engine: 'edge_tts',
        edge_voice: '',
        edge_rate: '+8%',
        gptsovits_url: 'http://127.0.0.1:7860/',
        gptsovits_ref_audio: '',
        gptsovits_character: '',
        api_url: '',
        api_key: '',
        api_model: '',
        api_voice: ''
      },
      tools: { enabled: true, allowed: [] },
      animations: { enabled: true, allowed: [] },
      llm: { provider_id: '', model: '', temperature: null }
    };
    try {
      const [ttsRes, roleRes, nameRes, toolsRes, llmRes] = await Promise.all([
        fetch('/api/tts/config').then(r => r.json()),
        fetch('/api/config/role').then(r => r.json()),
        fetch('/api/config/user_name').then(r => r.json()),
        fetch('/api/config/tools').then(r => r.json()),
        fetch('/api/llm/config').then(r => r.json())
      ]);
      // 当前模型（localStorage 中持久化）
      const savedModel = JSON.parse(localStorage.getItem('dabai.currentModel') || 'null');
      if (savedModel && savedModel.url) {
        current.model_url = savedModel.url;
        current.model_name = savedModel.name || savedModel.url.split('/').pop();
      }
      if (ttsRes) {
        current.tts.engine = ttsRes.engine || 'edge_tts';
        current.tts.edge_voice = ttsRes.edge_voice || '';
        current.tts.edge_rate = ttsRes.edge_rate || '+8%';
        current.tts.gptsovits_url = ttsRes.gptsovits_url || '';
        current.tts.gptsovits_ref_audio = ttsRes.gptsovits_ref_audio || '';
        current.tts.gptsovits_character = ttsRes.gptsovits_character || '';
        current.tts.api_url = ttsRes.api_url || '';
        current.tts.api_key = ttsRes.api_key || '';
        current.tts.api_model = ttsRes.api_model || '';
        current.tts.api_voice = ttsRes.api_voice || '';
      }
      if (roleRes) {
        current.role_name = roleRes.role_name || 'AI助手';
        current.system_prompt = roleRes.system_prompt || '';
      }
      if (nameRes) {
        current.user_name = nameRes.user_name || '';
      }
      if (toolsRes) {
        current.tools = {
          enabled: !!toolsRes.enable_tools,
          allowed: toolsRes.allowed_tools || []
        };
      }
      if (llmRes) {
        current.llm = {
          provider_id: llmRes.active_id || '',
          model: llmRes.model || '',
          temperature: (llmRes.temperature != null) ? Number(llmRes.temperature) : null
        };
      }
    } catch (e) {
      console.warn('捕获当前角色配置失败', e);
    }
    return current;
  };

  App.fillRoleCardForm = async function fillRoleCardForm(data) {
    const tts = data.tts || {};
    App.rcName.value = data.name || '';
    App.rcRoleName.value = data.role_name || '';
    App.rcUserName.value = data.user_name || '';
    App.rcSystemPrompt.value = data.system_prompt || '';
    // 模型
    if (data.model_url) {
      App.rcModelSelect.value = data.model_url;
    }
    // 大语言模型：卡片声明供应商（provider_id）+ 模型；未声明则跟随全局当前供应商
    const llm = data.llm || {};
    const gCfg = await App.loadRcLlmGlobalConfig(true);
    let pid = (llm.provider_id || '').trim();
    if (!pid) {
      // 旧卡兼容：按 base_url 精确匹配供应商（绝不按端口/名字猜测，避免串配置）
      const legacyBase = (llm.base_url || '').trim();
      const providers = (gCfg && gCfg.providers) || [];
      if (legacyBase) {
        const m = providers.filter(p => p.base_url === legacyBase);
        if (m.length === 1) pid = m[0].id;
      } else if (llm.provider) {
        const p0 = providers.filter(p => p.kind === (llm.provider || '').trim());
        if (p0.length === 1) pid = p0[0].id;
      }
    }
    App.rcLlmProviderId = pid || '';
    if (App.rcLlmProviderSelect) {
      App.renderRcProviderOptions();
      App.rcLlmProviderSelect.value = pid || '';
    }
    // 预填模型（先于加载模型列表；loadRcLlmModels 会保留该值）
    const provObj = ((gCfg && gCfg.providers) || []).find(p => p.id === pid);
    const presetModel = (llm.model || '').trim() || (provObj && provObj.default_model) || '';
    // 存一份给 loadRcLlmModels 兜底：下拉框此时还没有对应选项，
    // 直接赋 select.value 会被浏览器静默丢弃（HTML 里不存在该选项），
    // 导致加载列表后落到「选第一个模型」分支、保存时把卡片模型悄悄换掉。
    App._rcPresetModel = presetModel;
    if (presetModel && App.rcLlmModel) App.rcLlmModel.value = presetModel;
    const temp = (llm.temperature != null && llm.temperature !== '')
      ? Number(llm.temperature)
      : (App.rcLlmDefaultTemp ?? 0.2);
    App.rcLlmTemperature.value = temp;
    App.rcLlmTempVal.textContent = Number(temp).toFixed(2);
    if (App.rcLlmVision) {
      App.rcLlmVision.value = llm.vision === true ? 'true' : (llm.vision === false ? 'false' : '');
    }
    // 严格从所选供应商加载模型列表（未选供应商则只提示，绝不串用其它提供方）
    await App.loadRcLlmModels();
    App.refreshRcLlmVisionTip?.();
    // TTS
    App.switchRcTTSEngine(tts.engine || 'edge_tts');
    const rate = parseInt(tts.edge_rate) || 0;
    App.rcRateRange.value = String(rate);
    App.rcRateVal.textContent = (rate >= 0 ? '+' : '') + rate + '%';
    if (tts.edge_voice) {
      App.rcVoiceSelect.value = tts.edge_voice;
    }
    App.rcGsoUrl.value = tts.gptsovits_url || '';
    App.rcGsoRef.value = tts.gptsovits_ref_audio || '';
    App.rcGsoChar.value = tts.gptsovits_character || '';
    if (App.rcTtsApiUrl) App.rcTtsApiUrl.value = tts.api_url || '';
    if (App.rcTtsApiKey) App.rcTtsApiKey.value = tts.api_key || '';
    if (App.rcTtsApiModel) App.rcTtsApiModel.value = tts.api_model || '';
    if (App.rcTtsApiVoice) App.rcTtsApiVoice.value = tts.api_voice || '';
    // 工具配置
    const toolsCfg = data.tools || {};
    App.rcToolsEnabled.checked = toolsCfg.enabled !== false;
    App.rcToolsField?.classList.toggle('disabled', toolsCfg.enabled === false);
    App.renderRcTools(toolsCfg.allowed || []);
    // 专属动作配置
    const animCfg = data.animations || {};
    App.rcAnimEnabled.checked = animCfg.enabled !== false;
    App.rcAnimField?.classList.toggle('disabled', animCfg.enabled === false);
    App.renderRcAnims(animCfg.allowed || []);
  };

  App.loadRcModels = async function loadRcModels() {
    try {
      const res = await fetch('/api/models');
      const data = await res.json();
      App.rcModelSelect.innerHTML = '<option value="">默认（不切换外形）</option>';
      for (const m of data.models || []) {
        const opt = document.createElement('option');
        opt.value = m.url;
        opt.textContent = m.name;
        App.rcModelSelect.appendChild(opt);
      }
    } catch (e) {
      App.rcModelSelect.innerHTML = '<option value="">加载失败</option>';
    }
  };

  App.loadRcVoices = async function loadRcVoices() {
    if (App.rcVoicesLoaded) return;
    App.rcVoiceSelect.innerHTML = '<option value="">加载中…</option>';
    try {
      const res = await fetch('/api/tts/voices');
      const data = await res.json();
      App.rcVoiceSelect.innerHTML = '';
      for (const v of data.voices || []) {
        const opt = document.createElement('option');
        opt.value = v.name;
        opt.textContent = v.friendly || v.name;
        App.rcVoiceSelect.appendChild(opt);
      }
      App.rcVoicesLoaded = true;
    } catch (e) {
      App.rcVoiceSelect.innerHTML = '<option value="">加载失败</option>';
    }
  };

  /* ---------- 大语言模型（全局供应商 + 卡片选用） ---------- */
  /** 拉取全局 LLM 配置（供应商列表 + 当前激活 + 温度）并缓存 */
  App.loadRcLlmGlobalConfig = async function loadRcLlmGlobalConfig(force) {
    if (App.llmGlobalConfig && !force) return App.llmGlobalConfig;
    try {
      const res = await fetch('/api/llm/config');
      App.llmGlobalConfig = await res.json();
    } catch (e) {
      console.warn('加载全局 LLM 配置失败', e);
      App.llmGlobalConfig = null;
    }
    return App.llmGlobalConfig;
  };

  /** 供应商 id → 显示名（读缓存；未加载时返回空串） */
  App.providerNameById = function providerNameById(pid) {
    if (!pid) return '';
    const g = App.llmGlobalConfig;
    const list = (g && g.providers) || [];
    const p = list.find(x => x.id === pid);
    return p ? p.name : '';
  };

  /** 用缓存供应商列表填充卡片编辑弹窗的「供应商」下拉框 */
  App.renderRcProviderOptions = function renderRcProviderOptions() {
    const sel = App.rcLlmProviderSelect;
    if (!sel) return;
    const cur = sel.value;
    sel.innerHTML = '<option value="">（跟随全局当前供应商）</option>';
    const g = App.llmGlobalConfig;
    const list = (g && g.providers) || [];
    for (const p of list) {
      const opt = document.createElement('option');
      opt.value = p.id;
      opt.textContent = p.name + (p._active ? '（当前）' : '');
      sel.appendChild(opt);
    }
    if (cur) sel.value = cur;
  };

  /** 切换卡片表单当前编辑的供应商：回填下拉框 + 从该供应商加载模型列表 */
  App.switchRcLlmProvider = async function switchRcLlmProvider(providerId) {
    App.rcLlmProviderId = providerId || '';
    // 切换供应商 = 用户主动换模型来源，清掉预填缓存与旧选择
    App._rcPresetModel = '';
    const sel = App.rcLlmProviderSelect;
    if (sel) {
      App.renderRcProviderOptions();
      sel.value = App.rcLlmProviderId || '';
    }
    // 切供应商后清掉旧模型选择（模型列表必须来自所选供应商）
    if (App.rcLlmModel) App.rcLlmModel.value = '';
    await App.loadRcLlmModels();
  };

  /** 把「这张卡用的模型能不能看见注入的截图」摊开显示 */
  App.refreshRcLlmVisionTip = async function refreshRcLlmVisionTip() {
    const tip = App.rcLlmVisionTip;
    if (!tip) return;
    const qs = new URLSearchParams({
      provider_id: App.rcLlmProviderId || '',
      model: App.rcLlmModel?.value.trim() || '',
      override: App.rcLlmVision?.value || ''
    });
    try {
      const d = await (await fetch('/api/vision/check?' + qs.toString())).json();
      if (d.ok) {
        tip.textContent = `✓ ${d.model} 能读图（${d.why}）`;
        return;
      }
      tip.textContent = `✗ ${d.model} 看不见图（${d.why}）→ `
        + '截图注入后它读不到，换一个支持读图的模型';
    } catch (e) {
      tip.textContent = '判定不可用（接口未生效？）';
    }
  };


  /** 从所选供应商自动加载可用大模型列表 —— 只从该供应商拉取，绝不串用其它提供方。
   *  保留当前已选模型；拉取失败时仍保留当前模型选项，避免空白无法选择 */
  App.loadRcLlmModels = async function loadRcLlmModels() {
    const sel = App.rcLlmModel;
    if (!sel) return;
    const pid = App.rcLlmProviderId || '';
    const tipEl = App.rcLlmTip;
    // 优先取下拉框当前值；为空时回退到编辑器预填的卡片已保存模型，
    // 确保「打开卡片编辑 → 什么都不改 → 保存」永远不会把模型改成列表第一个
    const prevValue = sel.value || App._rcPresetModel || '';
    if (tipEl) {
      tipEl.textContent = pid
        ? '正在从该供应商加载模型列表…'
        : '未指定供应商 = 跟随全局当前供应商，应用卡片时使用它的默认模型（可在「大厅 → 供应商」查看/切换）';
    }
    sel.innerHTML = '<option value="">（跟随全局当前供应商）</option>';
    if (!pid) {
      // 未指定供应商：同样保留卡片已保存的模型，防止保存时被清空
      if (prevValue) {
        const opt = document.createElement('option');
        opt.value = prevValue;
        opt.textContent = prevValue;
        sel.appendChild(opt);
        sel.value = prevValue;
      }
      return;
    }
    try {
      const params = new URLSearchParams();
      params.set('provider', pid);
      const res = await fetch('/api/llm/models?' + params.toString());
      const data = await res.json();
      const list = data.models || [];
      const allList = data.all_models || list;
      sel.innerHTML = '';
      if (!list.length) {
        // 供应商未返回模型：保留之前已选值（仍可保存）
        if (prevValue) {
          const opt = document.createElement('option');
          opt.value = prevValue;
          opt.textContent = prevValue;
          sel.appendChild(opt);
          sel.value = prevValue;
        } else {
          const opt = document.createElement('option');
          opt.value = '';
          opt.textContent = '（无可用模型，请在「供应商」里检查 Base URL 后点刷新）';
          sel.appendChild(opt);
        }
      } else {
        for (const m of list) {
          const opt = document.createElement('option');
          opt.value = m;
          opt.textContent = m;
          sel.appendChild(opt);
        }
        // 卡片保存模型防漂移：已保存的模型不在当前可对话列表（被过滤/暂不可用/接口波动）时，
        // 保留原值为可选项并明确提示，绝不静默替换成列表第一个模型 ——
        // 否则用户随意编辑一次保存，卡片的 LLM 模型就会被悄悄换掉，设定无法持久
        const savedIsNonChat = !!(prevValue && !list.includes(prevValue) && allList.includes(prevValue));
        if (prevValue && list.includes(prevValue)) {
          sel.value = prevValue;
        } else if (savedIsNonChat) {
          // 卡片里保存的是非聊天模型（如嵌入模型）：保留并明确提示，避免静默替换
          const opt = document.createElement('option');
          opt.value = prevValue;
          opt.textContent = `${prevValue}（非聊天模型，不可用于对话）`;
          sel.appendChild(opt);
          sel.value = prevValue;
        } else if (prevValue) {
          // 模型不在列表但也不是已知的非聊天模型：仍然保留原值，绝不擅自替换
          const opt = document.createElement('option');
          opt.value = prevValue;
          opt.textContent = `${prevValue}（不在当前可对话列表，保存将保留原模型）`;
          sel.appendChild(opt);
          sel.value = prevValue;
        } else if (list.length) {
          sel.value = list[0];
        } else {
          sel.value = '';
        }
      }
      if (tipEl) {
        const preservedPrev = !!(prevValue && list.length && !list.includes(prevValue));
        tipEl.textContent = data.error
          ? `模型列表加载失败：${data.error}`
          : preservedPrev
            ? `警告：上次保存的模型「${prevValue}」未出现在当前可对话列表中（可能被过滤或暂不可用），已保留原选项防止保存时模型被静默替换；如确有新模型请手动选择`
            : (list.length
                ? `已从该供应商加载 ${list.length} 个可对话模型（已过滤嵌入/图像/OCR 等）`
                : '该供应商未返回模型，请检查 Base URL 后点刷新');
      }
    } catch (e) {
      sel.innerHTML = '';
      if (prevValue) {
        const opt = document.createElement('option');
        opt.value = prevValue;
        opt.textContent = prevValue;
        sel.appendChild(opt);
        sel.value = prevValue;
      }
      if (tipEl) tipEl.textContent = '模型列表加载失败（已保留当前模型）';
    }
  };

  /* ---------- 语音识别（STT）独立设置 ---------- */
  /** 拉取全局 STT 配置并缓存 */
  App.loadRcSttConfig = async function loadRcSttConfig(force) {
    if (App.rcSttLoaded && !force) return;
    try {
      const res = await fetch('/api/stt/config');
      const cfg = await res.json();
      if (App.rcSttApiUrl) App.rcSttApiUrl.value = cfg.api_url || '';
      if (App.rcSttApiKey) App.rcSttApiKey.value = cfg.api_key || '';
      if (App.rcSttModel) App.rcSttModel.value = cfg.model || '';
      App.switchRcSttProvider(cfg.provider || 'cloud');
      App.rcSttLoaded = true;
    } catch (e) {
      console.warn('加载语音识别配置失败', e);
    }
  };

  /** 切换识别方式：cloud 仅云端 / auto 自动（云端优先，无本地兜底） */
  App.switchRcSttProvider = function switchRcSttProvider(provider) {
    if (!['auto', 'cloud'].includes(provider)) provider = 'cloud';
    App.rcSttProvider = provider;
    App.rcSttTabs?.querySelectorAll('.tts-tab').forEach(t => {
      t.classList.toggle('active', (t as HTMLElement).dataset.provider === provider);
    });
    if (App.rcSttCloudPanel) App.rcSttCloudPanel.style.display = '';
  };

  /** 保存语音识别设置（独立于大语言模型配置，立即全局生效） */
  App.saveRcSttConfig = async function saveRcSttConfig() {
    const payload = {
      provider: App.rcSttProvider || 'cloud',
      api_url: App.rcSttApiUrl?.value.trim() || '',
      api_key: App.rcSttApiKey?.value.trim() || '',
      model: App.rcSttModel?.value.trim() || ''
    };
    try {
      const res = await fetch('/api/stt/config', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      if (data.ok) {
        if (App.rcSttTip) App.rcSttTip.textContent = '已保存，下次说话即生效';
        setTimeout(() => { if (App.rcSttTip) App.rcSttTip.textContent = ''; }, 3000);
        App.showToast('语音识别设置已保存');
      } else {
        App.showToast('保存失败');
      }
    } catch (err) {
      App.showToast('保存语音识别设置失败：' + (err.message || err));
    }
  };

  App.loadRcTools = async function loadRcTools() {
    if (App.rcToolsLoaded) return;
    App.rcToolsList.innerHTML = '<div class="tts-tip" style="padding:8px">加载中…</div>';
    try {
      const res = await fetch('/api/tools');
      const data = await res.json();
      App.allRcTools = data.tools || [];
      App.rcToolsLoaded = true;
    } catch (e) {
      App.allRcTools = [];
      App.rcToolsList.innerHTML = '<div class="tts-tip" style="padding:8px">工具列表加载失败</div>';
    }
  };

  App.renderRcTools = function renderRcTools(selected) {
    const sel = new Set(selected || []);
    if (!App.allRcTools || !App.allRcTools.length) {
      App.rcToolsList.innerHTML = '<div class="tts-tip" style="padding:8px">暂无可用工具</div>';
      return;
    }
    const allChecked = App.allRcTools.every(t => sel.has(t.name));
    let html = `<label class="tools-check-all"><input type="checkbox" id="rc-tools-all" ${allChecked ? 'checked' : ''}> 全选</label>`;
    for (const t of App.allRcTools) {
      const checked = sel.has(t.name);
      const sourceTag = t.source === 'mcp' ? '（MCP）' : '';
      html += `<label class="tools-item" title="${App.escapeHtml(t.description || '')}">` +
        `<input type="checkbox" value="${App.escapeHtml(t.name)}" ${checked ? 'checked' : ''}>` +
        `<span class="tools-item-name">${App.escapeHtml(t.name)}${sourceTag}</span>` +
        `<span class="tools-item-desc">${App.escapeHtml(t.description || '')}</span>` +
        `</label>`;
    }
    App.rcToolsList.innerHTML = html;
    const allBox = document.getElementById('rc-tools-all') as HTMLInputElement | null;
    allBox?.addEventListener('change', () => {
      App.rcToolsList.querySelectorAll('.tools-item input').forEach(cb => { (cb as HTMLInputElement).checked = allBox.checked; });
    });
  };

  App.collectRcTools = function collectRcTools() {
    const enabled = App.rcToolsEnabled?.checked ?? true;
    if (!enabled) return { enabled: false, allowed: [] };
    const checked = [];
    App.rcToolsList?.querySelectorAll('.tools-item input:checked').forEach(cb => checked.push((cb as HTMLInputElement).value));
    // 全选（或全部勾选）时清空白名单 = 全部可用，语义更简洁
    const allChecked = checked.length === (App.allRcTools || []).length;
    return { enabled: true, allowed: allChecked ? [] : checked };
  };

  /* ---------- 专属动作配置 ---------- */
  App.loadRcAnims = async function loadRcAnims() {
    if (App.rcAnimsLoaded) return;
    App.rcAnimList.innerHTML = '<div class="tts-tip" style="padding:8px">加载中…</div>';
    try {
      const res = await fetch('/anim/animation-library.json');
      if (!res.ok) throw new Error('HTTP ' + res.status);
      App.allRcAnims = await res.json();
      App.rcAnimsLoaded = true;
    } catch (e) {
      App.allRcAnims = null;
      App.rcAnimList.innerHTML = '<div class="tts-tip" style="padding:8px">动作库配置加载失败</div>';
    }
  };

  /** 渲染专属动作多选列表（按分类分组） */
  App.renderRcAnims = function renderRcAnims(selected) {
    const sel = new Set(selected || []);
    const cfg = App.allRcAnims;
    if (!cfg || !cfg.categories) {
      App.rcAnimList.innerHTML = '<div class="tts-tip" style="padding:8px">暂无动作库配置</div>';
      App.updateRcAnimCount();
      return;
    }
    const catKeys = Object.keys(cfg.categories);
    if (!catKeys.length) {
      App.rcAnimList.innerHTML = '<div class="tts-tip" style="padding:8px">动作库为空</div>';
      App.updateRcAnimCount();
      return;
    }
    const allAnims = catKeys.reduce((acc, k) => acc.concat(cfg.categories[k].animations || []), []);
    const allChecked = allAnims.length > 0 && allAnims.every(a => sel.has(a.name));
    let html = `<label class="tools-check-all"><input type="checkbox" id="rc-anim-all" ${allChecked ? 'checked' : ''}> 全选（= 执行全部动作）</label>`;
    for (const key of catKeys) {
      const cat = cfg.categories[key];
      const anims = cat.animations || [];
      if (!anims.length) continue;
      html += `<div class="anim-group-label">${App.escapeHtml(cat.label || key)}` +
        `<span class="anim-group-count">${anims.length}</span></div>`;
      for (const a of anims) {
        const checked = sel.has(a.name);
        const meta = a.loop ? '循环' : '单次';
        html += `<label class="tools-item" title="${App.escapeHtml(a.description || '')}">` +
          `<input type="checkbox" value="${App.escapeHtml(a.name)}" ${checked ? 'checked' : ''}>` +
          `<span class="tools-item-name">${App.escapeHtml(a.name)}</span>` +
          `<span class="tools-item-desc">${App.escapeHtml(meta)}${a.emotion ? ' · ' + App.escapeHtml(a.emotion) : ''}</span>` +
          `</label>`;
      }
    }
    App.rcAnimList.innerHTML = html;
    // 事件委托：全选 + 单项勾选都实时刷新计数
    App.rcAnimList.addEventListener('change', (e: Event) => {
      const target = e.target as HTMLInputElement;
      if (target.id === 'rc-anim-all') {
        App.rcAnimList.querySelectorAll('.tools-item input').forEach(cb => { (cb as HTMLInputElement).checked = target.checked; });
      }
      App.updateRcAnimCount();
    });
    App.updateRcAnimCount();
  };

  /** 刷新已选动作计数（0/0 = 全部动作） */
  App.updateRcAnimCount = function updateRcAnimCount() {
    const el = document.getElementById('rc-anim-count');
    if (!el) return;
    const cfg = App.allRcAnims;
    const allAnims = cfg && cfg.categories
      ? Object.keys(cfg.categories).reduce((acc, k) => acc.concat(cfg.categories[k].animations || []), [])
      : [];
    const checked = App.rcAnimList?.querySelectorAll('.tools-item input:checked').length || 0;
    el.textContent = `${checked} / ${allAnims.length}`;
  };

  App.collectRcAnims = function collectRcAnims() {
    const enabled = App.rcAnimEnabled?.checked ?? true;
    if (!enabled) return { enabled: false, allowed: [] };
    const checked = [];
    App.rcAnimList?.querySelectorAll('.tools-item input:checked').forEach(cb => checked.push((cb as HTMLInputElement).value));
    // 全选（或全部勾选）时清空列表 = 全部动作，语义更简洁
    const cfg = App.allRcAnims;
    const allAnims = cfg && cfg.categories
      ? Object.keys(cfg.categories).reduce((acc, k) => acc.concat(cfg.categories[k].animations || []), [])
      : [];
    const allChecked = allAnims.length > 0 && checked.length === allAnims.length;
    return { enabled: true, allowed: allChecked ? [] : checked };
  };

  App.switchRcTTSEngine = function switchRcTTSEngine(engine) {
    App.rcTtsTabs?.querySelectorAll('.tts-tab').forEach(t => {
      t.classList.toggle('active', (t as HTMLElement).dataset.engine === engine);
    });
    if (App.rcTtsEdgePanel) App.rcTtsEdgePanel.style.display = engine === 'edge_tts' ? '' : 'none';
    if (App.rcTtsGsoPanel) App.rcTtsGsoPanel.style.display = engine === 'gpt_sovits' ? '' : 'none';
    if (App.rcTtsApiPanel) App.rcTtsApiPanel.style.display = engine === 'api' ? '' : 'none';
  };

  App.collectRoleCardForm = function collectRoleCardForm() {
    const engine = (App.rcTtsTabs?.querySelector('.tts-tab.active') as HTMLElement)?.dataset.engine || 'edge_tts';
    return {
      name: App.rcName.value.trim(),
      role_name: App.rcRoleName.value.trim(),
      user_name: App.rcUserName?.value.trim() || '',
      model_url: App.rcModelSelect.value,
      model_name: App.rcModelSelect.selectedOptions[0]?.textContent || '',
      system_prompt: App.rcSystemPrompt.value.trim(),
      tts: {
        engine,
        edge_voice: App.rcVoiceSelect?.value || '',
        edge_rate: (parseInt(App.rcRateRange?.value) || 0) >= 0
          ? '+' + (parseInt(App.rcRateRange?.value) || 0) + '%'
          : parseInt(App.rcRateRange?.value) + '%',
        gptsovits_url: App.rcGsoUrl?.value.trim() || '',
        gptsovits_ref_audio: App.rcGsoRef?.value.trim() || '',
        gptsovits_character: App.rcGsoChar?.value.trim() || '',
        api_url: App.rcTtsApiUrl?.value.trim() || '',
        api_key: App.rcTtsApiKey?.value.trim() || '',
        api_model: App.rcTtsApiModel?.value.trim() || '',
        api_voice: App.rcTtsApiVoice?.value.trim() || ''
      },
      tools: App.collectRcTools(),
      animations: App.collectRcAnims(),
      llm: {
        provider_id: App.rcLlmProviderId || '',
        model: App.rcLlmModel?.value.trim() || '',
        temperature: App.rcLlmTemperature ? Number(App.rcLlmTemperature.value) : null,
        vision: App.rcLlmVision?.value === 'true' ? true
          : (App.rcLlmVision?.value === 'false' ? false : null)
      }
    };
  };

  /* ---------- 保存 / 应用 / 删除 ---------- */
  App.saveRoleCard = async function saveRoleCard() {
    if (!canManageCards()) {
      App.showToast('角色卡片由管理员管理');
      return;
    }
    const payload = App.collectRoleCardForm();
    if (!payload.name) {
      App.showToast('请填写卡片名称');
      App.rcName?.focus();
      return;
    }
    let cardId = App.rcEditingId;
    try {
      if (cardId) {
        const res = await fetch(`/api/character_cards/${cardId}`, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload)
        });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        cardId = data.card.id;
      } else {
        const res = await fetch('/api/character_cards', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload)
        });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        cardId = data.card.id;
      }
      App.roleCardEditModal?.classList.remove('show');
      App.showToast('卡片已保存');
      // 保存只改配置：仅当编辑的是当前生效卡片时静默同步一遍（人设/模型/语音/动作即时生效）。
      // 绝不碰会话——开新对话/切对话只归「新对话」按钮和会话列表管。
      if (cardId && cardId === App.roleCardActiveId) {
        await App.applyRoleCard(cardId, { notify: false, toast: '' });
      }
    } catch (err) {
      App.showToast('保存失败：' + (err.message || err));
    }
  };

  /** 应用角色卡片：写配置 + 换外形/语音/动作。opts.notify=false 不通知 AI，
   *  opts.toast='' 不弹提示。会话永远不动（开/切对话只归「新对话」按钮）。 */
  App.applyRoleCard = async function applyRoleCard(cardId, opts) {
    const _opts = opts || {};
    try {
      const _uid = localStorage.getItem('dabai.userId') || '';
      const res = await fetch(`/api/character_cards/${cardId}/apply`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ user_id: _uid })
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      const card = data.card || {};
      App.roleCardActiveId = cardId;
      localStorage.setItem('dabai.activeRoleCard', cardId);
      // 1. 切换外形（model_url 未变时跳过重载，避免编辑卡片内容也重新加载 VRM）
      if (data.model_url) {
        const currentModel = JSON.parse(localStorage.getItem('dabai.currentModel') || '{}');
        if (currentModel.url !== data.model_url) {
          await App.loadModelFromUrl(data.model_url, data.model_name);
        }
      }
      // 1.5 应用专属动作配置（未配置 → 执行全部动作）
      if (App.setRoleAnimationConfig) {
        App.setRoleAnimationConfig(card.animations);
      }
      // 2. 刷新 TTS 设置（后端已切换，同步前端弹窗状态）
      try {
        const ttsRes = await fetch('/api/tts/config');
        const ttsCfg = await ttsRes.json();
        App.applyTTSConfig(ttsCfg);
      } catch (e) { /* 忽略 */ }
      // 3. 只刷新会话列表（记忆空间换了，列表要跟着变）。
      //    不切会话：当前这条对话由「新对话」按钮和会话列表决定，卡片操作一律不动它。
      if (App.ws && App.ws.readyState === WebSocket.OPEN) {
        App.ws.send(JSON.stringify({ type: 'list_sessions' }));
      }
      // 4. 通知 AI 人设已切换
      const roleName = card.role_name || card.name || 'AI助手';
      const voiceDesc = (card.tts && card.tts.engine === 'gpt_sovits')
        ? (card.tts.gptsovits_character || '新的声音')
        : '新的声音';
      const nameHint = card.user_name
        ? `，称呼用户为「${card.user_name}」`
        : `，不用特定称呼，称呼用户为"你"即可`;
      const toolsCfg = card.tools || {};
      let toolsHint = '';
      if (toolsCfg.enabled === false) {
        toolsHint = '，同时你当前不能调用任何工具（能力已被关闭），需要实时信息时如实告知用户即可';
      } else if (toolsCfg.allowed && toolsCfg.allowed.length) {
        toolsHint = `，你当前只能调用以下工具：${toolsCfg.allowed.join('、')}`;
      }
      if (_opts.notify !== false) {
        App.sendAIAction(
          `（用户为你切换了完整的角色设定：你现在叫「${roleName}」，用「${voiceDesc}」说话，人设也更新了${nameHint}${toolsHint}。` +
          `请完全按照新的人设来认识自己和与用户相处，说话语气、性格、与用户的关系都以角色设定为准，` +
          `不要刻意提"用户切换了角色"这件事）`, true);
      }
      // 4. 刷新列表选中态
      App.refreshRoleCardList();
      // 4.1 在提示里带上当前生效的供应商 + 模型，明确「保存后立刻生效」
      let modelHint = '';
      try {
        const llmRes = await fetch('/api/llm/config');
        const llmCfg = await llmRes.json();
        App.llmGlobalConfig = llmCfg;
        const ap = (llmCfg.providers || []).find(p => p.id === llmCfg.active_id);
        const pName = ap ? ap.name : '';
        modelHint = pName ? (`，模型：${pName} · ${llmCfg.model || '默认'}`) : '';
      } catch (e) { /* 提示里不带模型信息也不影响 */ }
      if (_opts.toast !== '') {
        App.showToast(_opts.toast || `已切换角色：${card.name || roleName}${modelHint}`);
      }
      App.closeRoleCardModalIfOpen();
    } catch (err) {
      App.showToast('切换失败：' + (err.message || err));
    }
  };

  /** 启动时静默恢复上次使用的角色卡片配置（仅返回卡片数据，不触发切换动作/AI 消息） */
  App.restoreActiveRoleCard = async function restoreActiveRoleCard() {
    try {
      const _uid = localStorage.getItem('dabai.userId') || '';
      const res = await fetch('/api/character_cards?user_id=' + encodeURIComponent(_uid));
      const data = await res.json();
      // 按人隔离：服务端返回的是「本用户」当前生效的卡片（各人各自一份），
      // 本地 localStorage 只作无网络时的兜底。
      const activeId = data.active_id || App.roleCardActiveId;
      if (activeId) App.roleCardActiveId = activeId;
      const card = (data.cards || []).find(c => c.id === activeId);
      if (!card) {
        // 卡片已被删除 → 清除持久化的活动卡片标记
        App.roleCardActiveId = null;
        localStorage.removeItem('dabai.activeRoleCard');
        return null;
      }
      return card;
    } catch (e) {
      console.warn('恢复角色卡片配置失败', e);
      return null;
    }
  };

  App.deleteRoleCard = async function deleteRoleCard() {
    if (!canManageCards()) {
      App.showToast('角色卡片由管理员管理');
      return;
    }
    if (!App.rcEditingId) return;
    if (!confirm('删除这张角色卡片？')) return;
    try {
      await fetch(`/api/character_cards/${App.rcEditingId}`, { method: 'DELETE' });
      if (App.roleCardActiveId === App.rcEditingId) {
        App.roleCardActiveId = null;
        localStorage.removeItem('dabai.activeRoleCard');
      }
      App.roleCardEditModal?.classList.remove('show');
      await App.refreshRoleCardList();
      App.showToast('已删除');
    } catch (err) {
      App.showToast('删除失败');
    }
  };

  App.closeRoleCardModalIfOpen = function closeRoleCardModalIfOpen() {
    if (App.roleCardModal?.classList.contains('show')) {
      App.roleCardModal.classList.remove('show');
    }
  };
  /* ============================================================
   *  启动
   * ============================================================ */
});
