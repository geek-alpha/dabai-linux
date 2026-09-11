import type { AppKernel } from '../types/app-kernel.js';

export default (function init(App: AppKernel) {
  /* ============================================================
   *  Toast
   * ============================================================ */
  App.toastTimer = null;
  App.showToast = function showToast(text: string) {
    App.toastEl!.textContent = text;
    App.toastEl!.classList.add('show');
    clearTimeout(App.toastTimer);
    App.toastTimer = setTimeout(() => App.toastEl?.classList.remove('show'), 2500);
  };
  /* ============================================================
   *  模型管理 UI
   * ============================================================ */
});
