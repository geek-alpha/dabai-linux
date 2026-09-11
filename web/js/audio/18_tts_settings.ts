import type { AppKernel, TTSConfig, TTSEngine } from '../types/app-kernel.js';

export default (function init(App: AppKernel) {
  /* ============================================================
   *  TTS 语音合成设置
   * ============================================================ */
  App.ttsVoicesLoaded = false;
  App.ttsCharsLoaded = false;
  App.currentTTSEngine = 'edge_tts';
  App.savedEdgeVoice = 'zh-CN-YunxiNeural';
  App.initTTSConfig = async function initTTSConfig() {
    // 打开 / 关闭弹窗
    App.ttsBtn?.addEventListener('click', App.openTTSModal);
    App.ttsModalClose?.addEventListener('click', () => App.ttsModal?.classList.remove('show'));
    App.ttsModal?.querySelector('.modal-backdrop')?.addEventListener('click', () => App.ttsModal?.classList.remove('show'));
    // 引擎切换 tab
    App.ttsModal?.querySelectorAll('.tts-tab').forEach(tab => {
      tab.addEventListener('click', () => App.switchTTSEngine((tab as HTMLElement).dataset.engine as TTSEngine));
    });
    // 语速滑块实时显示
    App.ttsRateRange?.addEventListener('input', () => {
      const v = parseInt(App.ttsRateRange!.value);
      App.ttsRateVal!.textContent = (v >= 0 ? '+' : '') + v + '%';
    });
    // 保存
    App.ttsSaveBtn?.addEventListener('click', App.saveTTSConfig);
    // 拉取当前配置回填
    try {
      const res = await fetch('/api/tts/config');
      const cfg = await res.json() as TTSConfig;
      App.applyTTSConfig(cfg);
    } catch (e) {
      console.warn('TTS 配置拉取失败', e);
    }
  };
  App.applyTTSConfig = function applyTTSConfig(cfg: TTSConfig) {
    App.currentTTSEngine = cfg.engine || 'edge_tts';
    App.ttsModal?.querySelectorAll('.tts-tab').forEach(t => {
      t.classList.toggle('active', (t as HTMLElement).dataset.engine === App.currentTTSEngine);
    });
    if (App.ttsEdgePanel) App.ttsEdgePanel.style.display = App.currentTTSEngine === 'edge_tts' ? '' : 'none';
    if (App.ttsGsoPanel) App.ttsGsoPanel.style.display = App.currentTTSEngine === 'gpt_sovits' ? '' : 'none';
    if (cfg.edge_voice) {
      App.savedEdgeVoice = cfg.edge_voice;
      if (App.ttsVoiceSelect && App.ttsVoiceSelect.options.length > 0) App.ttsVoiceSelect.value = cfg.edge_voice;
    }
    if (cfg.edge_rate) {
      const pct = parseInt(cfg.edge_rate) || 0;
      if (App.ttsRateRange) App.ttsRateRange.value = String(pct);
      if (App.ttsRateVal) App.ttsRateVal.textContent = (pct >= 0 ? '+' : '') + pct + '%';
    }
    if (cfg.gptsovits_url && App.ttsGsoUrl) App.ttsGsoUrl.value = cfg.gptsovits_url;
    if (cfg.gptsovits_ref_audio && App.ttsGsoRef) App.ttsGsoRef.value = cfg.gptsovits_ref_audio;
    if (cfg.gptsovits_character && App.ttsGsoChar) App.ttsGsoChar.value = cfg.gptsovits_character;
  };
  App.openTTSModal = async function openTTSModal() {
    App.ttsModal?.classList.add('show');
    // 懒加载 edge_tts 音色列表
    if (!App.ttsVoicesLoaded && App.ttsVoiceSelect) {
      App.ttsVoiceSelect.innerHTML = '<option>加载中…</option>';
      try {
        const res = await fetch('/api/tts/voices');
        const data = await res.json() as { voices?: { name: string; friendly?: string }[] };
        App.ttsVoiceSelect.innerHTML = '';
        for (const v of data.voices || []) {
          const opt = document.createElement('option');
          opt.value = v.name;
          opt.textContent = v.friendly || v.name;
          App.ttsVoiceSelect.appendChild(opt);
        }
        if (App.savedEdgeVoice) App.ttsVoiceSelect.value = App.savedEdgeVoice;
        App.ttsVoicesLoaded = true;
      } catch (e) {
        App.ttsVoiceSelect.innerHTML = '<option>加载失败</option>';
      }
    }
    // 懒加载角色列表（gpt_sovits.json）
    if (!App.ttsCharsLoaded) {
      try {
        const res = await fetch('/api/tts/characters');
        const data = await res.json() as { characters?: string[] };
        const dl = App.$('tts-char-list');
        if (dl && data.characters && data.characters.length > 0) {
          dl.innerHTML = '';
          for (const c of data.characters) {
            const opt = document.createElement('option');
            opt.value = c;
            dl.appendChild(opt);
          }
        }
        App.ttsCharsLoaded = true;
      } catch (e) {/* 配置文件不存在时静默，保留手动输入 */}
    }
  };
  App.switchTTSEngine = function switchTTSEngine(engine: TTSEngine) {
    App.currentTTSEngine = engine;
    App.ttsModal?.querySelectorAll('.tts-tab').forEach(t => {
      t.classList.toggle('active', (t as HTMLElement).dataset.engine === engine);
    });
    if (App.ttsEdgePanel) App.ttsEdgePanel.style.display = engine === 'edge_tts' ? '' : 'none';
    if (App.ttsGsoPanel) App.ttsGsoPanel.style.display = engine === 'gpt_sovits' ? '' : 'none';
  };
  App.saveTTSConfig = async function saveTTSConfig() {
    const pct = parseInt(App.ttsRateRange?.value || '0');
    const engine = App.currentTTSEngine;
    const refAudio = App.ttsGsoRef?.value.trim() || '';
    const gsoUrl = App.ttsGsoUrl?.value.trim() || '';

    // GPT-SoVITS 模式只需服务地址（参考音频可选，服务端已绑定角色）
    if (engine === 'gpt_sovits' && !gsoUrl) {
      App.showToast('请填写 GPT-SoVITS 服务地址');
      App.ttsGsoUrl?.focus();
      return;
    }
    const payload = {
      engine: engine,
      edge_voice: App.ttsVoiceSelect?.value || App.savedEdgeVoice,
      edge_rate: (pct >= 0 ? '+' : '') + pct + '%',
      gptsovits_url: gsoUrl,
      gptsovits_ref_audio: refAudio,
      gptsovits_character: App.ttsGsoChar?.value.trim() || ''
    };
    try {
      const res = await fetch('/api/tts/config', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json'
        },
        body: JSON.stringify(payload)
      });
      const data = await res.json() as { ok?: boolean };
      if (data.ok) {
        App.savedEdgeVoice = payload.edge_voice;
        App.showToast(engine === 'gpt_sovits' ? '已切换 GPT-SoVITS，说话即生效' : '语音设置已保存');
        App.sendAIAction(engine === 'gpt_sovits' ? '（你的声音换了一种全新的质感，听听自己的新嗓音，感受一下这个独特的声音）' : '（你的嗓音换了，下次说话的声音会不一样了，感受一下自己的新音色）', true);
        App.ttsModal?.classList.remove('show');
      } else {
        App.showToast('保存失败');
      }
    } catch (e) {
      App.showToast('保存失败: ' + (e as Error).message);
    }
  };
  /* ============================================================
   *  启动
   * ============================================================ */
});
