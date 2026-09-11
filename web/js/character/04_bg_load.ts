import * as THREE from 'three';
import type { GLTF } from 'three/addons/loaders/GLTFLoader.js';
import type { AppKernel } from '../types/app-kernel.js';

export default (function init(App: AppKernel) {
  /* ============================================================
   *  3D 背景场景加载
   * ============================================================ */
  App.BG_TARGET_SIZE = 24; // 背景模型目标尺寸（环绕角色）
  App.loadBackgroundFromUrl = async function loadBackgroundFromUrl(url: string, name: string) {
    App.showModelLoading(`加载背景 ${name} …`);
    try {
      const gltf = await App.gltfLoader!.loadAsync(url);
      App.applyBackground(gltf, url, name);
      // 角色立刻回到原点，并清除缓存中的旧位置，防止 applySavedPositions 再把它拉走
      App.resetAvatarToOrigin();
      localStorage.setItem('dabai.currentBackground', JSON.stringify({
        url,
        name
      }));
      setTimeout(App.applySavedPositions, 200);
      App.saveSceneState();
      App.showToast(`已切换背景：${name}`);

      // 防重：仅当背景名与上次发送不一致时才通知服务器和触发 AI
      const isNewBg = App._sentBgName !== name;
      if (isNewBg) {
        App._sentBgName = name;
        if (App.ws && App.ws.readyState === WebSocket.OPEN) {
          App.ws.send(JSON.stringify({
            type: 'set_background',
            name
          }));
        }
        // 启动阶段不触发 AI 动作
        if (!App._isBooting) {
          App.sendAIAction(`（你来到了${name}，环顾四周感受一下这个全新的环境，心情也跟着变化）`, true);
        }
      }
    } catch (err) {
      console.error('背景加载失败:', err);
      App.showToast('背景加载失败：' + ((err as Error).message || err));
    } finally {
      App.hideModelLoading();
    }
  };
  App.applyBackground = function applyBackground(gltf: GLTF, url: string, name: string) {
    App.disposeBackground();
    const root = gltf.scene;
    App.backgroundGroup = new THREE.Group();
    App.backgroundGroup.add(root);
    App.scene!.add(App.backgroundGroup);

    // 自动缩放与定位：放大到 BG_TARGET_SIZE，底部贴地，水平居中
    const box = new THREE.Box3().setFromObject(root);
    const size = box.getSize(new THREE.Vector3());
    const center = box.getCenter(new THREE.Vector3());
    const scale = App.BG_TARGET_SIZE / Math.max(0.001, Math.max(size.x, size.y, size.z));
    root.scale.setScalar(scale);
    root.updateMatrixWorld(true);
    const box2 = new THREE.Box3().setFromObject(root);
    const center2 = box2.getCenter(new THREE.Vector3());
    root.position.x += -center2.x;
    root.position.z += -center2.z;
    root.position.y += -box2.min.y; // 底部贴地

    // 记住背景包围盒中心(XZ)，用于初始地面射线检测
    App._bgCenterX = (box2.min.x + box2.max.x) / 2 + root.position.x;
    App._bgCenterZ = (box2.min.z + box2.max.z) / 2 + root.position.z;

    // 隐藏默认星空与地面光晕（背景模型自带环境）
    if (App.starField) App.starField.visible = false;
    if (App.parts.glow) App.parts.glow.visible = false;
    if (App.parts.contactShadow) App.parts.contactShadow.visible = false;
    // 关闭默认雾气
    if (App.scene!.fog) (App.scene!.fog as THREE.Fog).far = 60;
    // 按钮高亮：标识当前使用自定义背景
    if (App.bgBtn) App.bgBtn.classList.add('active');
    console.log('[BG] 背景加载成功:', name, 'scale:', scale.toFixed(2));
    App.refreshBgListSelection(name);
  };
  App.disposeBackground = function disposeBackground() {
    if (App.backgroundGroup) {
      if (App.selectedTarget === App.backgroundGroup) App.clearSelection();
      App.backgroundGroup.traverse(obj => {
        const mesh = obj as THREE.Mesh;
        if (mesh.geometry) mesh.geometry.dispose();
        if (mesh.material) {
          if (Array.isArray(mesh.material)) mesh.material.forEach(m => m.dispose());else mesh.material.dispose();
        }
      });
      App.scene!.remove(App.backgroundGroup);
      App.backgroundGroup = null;
    }
    // 恢复默认星空、地面光晕与雾气
    if (App.starField) App.starField.visible = true;
    if (App.parts.glow) App.parts.glow.visible = true;
    if (App.parts.contactShadow) App.parts.contactShadow.visible = true;
    if (App.scene!.fog) (App.scene!.fog as THREE.Fog).far = 20;
    // 取消按钮高亮
    if (App.bgBtn) App.bgBtn.classList.remove('active');
  };
  App.useDefaultBackground = function useDefaultBackground() {
    App.disposeBackground();
    localStorage.removeItem('dabai.currentBackground');
    App.refreshBgListSelection(null);
    // 角色立刻回到原点
    App.resetAvatarToOrigin();
    App.showToast('已切换为默认背景');
    // 防重：仅当之前发过非 null 的背景名时才通知服务器
    if (App._sentBgName !== null) {
      App._sentBgName = null;
      if (App.ws && App.ws.readyState === WebSocket.OPEN) {
        App.ws.send(JSON.stringify({
          type: 'set_background',
          name: null
        }));
      }
      if (!App._isBooting) {
        App.sendAIAction('（你回到了熟悉的星空下，一切又安静下来，仰望星空感受这份宁静）', true);
      }
    }
  };
  /* ============================================================
   *  移动模式：选中并平移角色 / 背景模型
   * ============================================================ */
});
