import * as THREE from 'three';
import type * as ThreeNS from 'three';
import type { AppKernel } from '../types/app-kernel.js';

export default (function init(App: AppKernel) {
  /* ============================================================
   *  移动模式：选中并平移角色 / 背景模型
   * ============================================================ */
  App.MIN_TARGET_SCALE = 0.2;
  App.MAX_TARGET_SCALE = 5.0;
  App.setMoveMode = function setMoveMode(on: boolean) {
    App.moveMode = on;
    if (App.moveBtn) App.moveBtn.classList.toggle('active', on);
    if (!on) App.clearSelection();
    App.canvas!.style.cursor = on ? 'crosshair' : 'grab';
    App.showToast(on ? '移动模式已开启 · 点击选中后拖动移动位置 · 滚轮/双指缩放大小' : '移动模式已关闭');
    App.sendAIAction(on ? '（用户开始重新布置你的环境，你看着周围的物品被挪动，好奇发生了什么变化）' : '（布置完成了，你可以好好感受现在的新环境了）', true);
  }; // 缩放当前选中目标（以模型脚底为中心，整体等比缩放）
  App.scaleSelectedTarget = function scaleSelectedTarget(factor: number) {
    if (!App.selectedTarget || !factor || !isFinite(factor)) return;
    const s = THREE.MathUtils.clamp(App.selectedTarget.scale.x * factor, App.MIN_TARGET_SCALE, App.MAX_TARGET_SCALE);
    App.selectedTarget.scale.setScalar(s);
  }; // 收集当前可选中的顶层对象（角色优先于背景，便于重叠时优先选中角色）
  App.getSelectableTargets = function getSelectableTargets() {
    const list: ThreeNS.Object3D[] = [];
    if (App.modelGroup) list.push(App.modelGroup);else if (App.proceduralChar && App.proceduralChar.visible) list.push(App.proceduralChar);
    if (App.backgroundGroup) list.push(App.backgroundGroup);
    return list;
  }; // 由命中子对象向上找到顶层可选对象
  App.findTopLevelSelected = function findTopLevelSelected(obj: ThreeNS.Object3D | null) {
    const targets = App.getSelectableTargets();
    let cur = obj;
    while (cur) {
      if (targets.includes(cur)) return cur;
      cur = cur.parent;
    }
    return null;
  };
  App.selectTarget = function selectTarget(obj: ThreeNS.Object3D | null) {
    if (App.selectedTarget === obj) return;
    App.clearSelection();
    if (!obj) return;
    App.selectedTarget = obj;
    // BoxHelper 自动跟随对象世界变换，但需每帧 update()
    App.selectionHelper = new THREE.BoxHelper(obj, 0x00e5ff);
    const mat = App.selectionHelper.material as ThreeNS.LineBasicMaterial;
    mat.depthTest = false;
    mat.transparent = true;
    mat.opacity = 0.9;
    App.selectionHelper.renderOrder = 999;
    App.scene!.add(App.selectionHelper);
  };
  App.clearSelection = function clearSelection() {
    if (App.selectionHelper) {
      App.scene!.remove(App.selectionHelper);
      App.selectionHelper.geometry.dispose();
      (App.selectionHelper.material as ThreeNS.Material).dispose();
      App.selectionHelper = null;
    }
    App.selectedTarget = null;
  };
  App.updatePointerNdc = function updatePointerNdc(e: { clientX: number; clientY: number }) {
    const rect = App.canvas!.getBoundingClientRect();
    App.pointerNdc.x = (e.clientX - rect.left) / rect.width * 2 - 1;
    App.pointerNdc.y = -((e.clientY - rect.top) / rect.height) * 2 + 1;
  };
  App.onMovePointerDown = function onMovePointerDown(e: PointerEvent) {
    App.updatePointerNdc(e);
    App.raycaster.setFromCamera(App.pointerNdc, App.camera!);
    const targets = App.getSelectableTargets();
    const hits = targets.length ? App.raycaster.intersectObjects(targets, true) : [];
    if (hits.length) {
      const top = App.findTopLevelSelected(hits[0].object);
      if (top) {
        App.selectTarget(top);
        // 以目标当前 y 为拖拽平面高度
        App.dragPlane.set(new THREE.Vector3(0, 1, 0), -top.position.y);
        // 计算射线与平面交点，记录与目标中心的水平偏移，避免点击瞬间跳到中心
        if (App.raycaster.ray.intersectPlane(App.dragPlane, App.dragHitPoint)) {
          App.dragOffsetX = App.dragHitPoint.x - top.position.x;
          App.dragOffsetZ = App.dragHitPoint.z - top.position.z;
        }
        return;
      }
    }
    // 点击空白：取消选中
    App.clearSelection();
  };
  App.onMovePointerMove = function onMovePointerMove(e: PointerEvent) {
    if (!App.selectedTarget) return;
    App.updatePointerNdc(e);
    App.raycaster.setFromCamera(App.pointerNdc, App.camera!);
    if (App.raycaster.ray.intersectPlane(App.dragPlane, App.dragHitPoint)) {
      App.selectedTarget.position.x = App.dragHitPoint.x - App.dragOffsetX;
      App.selectedTarget.position.z = App.dragHitPoint.z - App.dragOffsetZ;
      // 仅修改 X/Z，保留 Y
    }
  };
  /* ============================================================
   *  第一人称探索模式
   * ============================================================ */
});
