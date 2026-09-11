import type { AppKernel } from '../types/app-kernel.js';

export default (function init(App: AppKernel) {
  /* ============================================================
   *  对我的称呼设置
   *  用户可设置 AI 对自己的称呼；留空则 AI 不随意称呼
   *  （DOM 已并入角色卡片，name* 引用可能不存在，全部可选访问）
   * ============================================================ */
  App.initNameConfig = function initNameConfig() {
    App.nameBtn?.addEventListener('click', App.openNameModal);
    App.nameModalClose?.addEventListener('click', () => App.nameModal?.classList.remove('show'));
    App.nameModal?.querySelector('.modal-backdrop')?.addEventListener('click', () => App.nameModal?.classList.remove('show'));
    App.nameSaveBtn?.addEventListener('click', App.saveUserName);
    // 拉取当前配置回填
    try {
      fetch('/api/config/user_name')
        .then(r => r.json())
        .then((cfg: { user_name?: string }) => {
          if (cfg && cfg.user_name && App.userNameInput) {
            App.userNameInput.value = cfg.user_name;
          }
        })
        .catch(() => {});
    } catch (e) {
      console.warn('称呼配置拉取失败', e);
    }
  };
  App.openNameModal = function openNameModal() {
    App.nameModal?.classList.add('show');
  };
  App.saveUserName = async function saveUserName() {
    const name = (App.userNameInput?.value || '').trim();
    try {
      const res = await fetch('/api/config/user_name', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json'
        },
        body: JSON.stringify({ user_name: name })
      });
      const data = await res.json() as { ok?: boolean };
      if (data.ok) {
        App.showToast(name ? `已设置：AI 以后叫你「${name}」` : '已清除称呼，AI 不会随便称呼你');
        App.sendAIAction(name
          ? `（用户刚刚设置了自己的称呼，以后请始终用「${name}」称呼用户，记住并自然地开始使用这个称呼，不要用其他称呼）`
          : '（用户刚刚清除了称呼设置，以后请只用"你"称呼用户，不要擅自起昵称或使用未经确认的称呼）', true);
        App.nameModal?.classList.remove('show');
      } else {
        App.showToast('保存失败');
      }
    } catch (e) {
      App.showToast('保存失败: ' + (e as Error).message);
    }
  };
});
