import * as THREE from 'three';
import type { AppKernel } from '../types/app-kernel.js';

export default (function init(App: AppKernel) {
  /* ============================================================
   *  第一人称探索模式
   * ============================================================ */
  App.enterFPV = function enterFPV() {
    if (App.fpvMode) return;
    App.fpvMode = true;
    if (App.fpvBtn) App.fpvBtn.classList.add('active');
    // 从当前相机位置平滑过渡到 FPV 起点（保持水平位置，降至人眼高度）
    App.fpvPos.set(App.camera!.position.x, App.FPV_HEIGHT, App.camera!.position.z);
    // 朝向场景中心 (0, FPV_HEIGHT, 0)
    App.fpvYaw = Math.atan2(App.camera!.position.x - 0, App.camera!.position.z - 0);
    App.fpvPitch = -0.05;
    // 暂存并关闭背景自转，避免探索时场景旋转
    App.fpvSavedAutoRotate = App.backgroundAutoRotate;
    App.backgroundAutoRotate = false;
    // 显示 UI：准星 + 退出按钮（摇杆在触摸/点击时动态浮现）
    if (App.fpvCrosshair) App.fpvCrosshair.style.display = 'block';
    if (App.fpvExitBtn) App.fpvExitBtn.style.display = '';
    App.canvas!.style.cursor = 'grab';
    App.showToast('第一人称模式 · 左半屏拖拽=移动 · 右半屏拖拽=转向 · 滚轮调高度');
    App.sendAIAction('（用户走到了你的身边，正在好奇地打量你、绕着你转，你能感受到Ta的目光在你身上游走）', true);
  };
  App.exitFPV = function exitFPV() {
    if (!App.fpvMode) return;
    App.fpvMode = false;
    if (App.fpvBtn) App.fpvBtn.classList.remove('active');
    // 恢复背景自转
    App.backgroundAutoRotate = App.fpvSavedAutoRotate;
    // 隐藏 UI
    if (App.fpvCrosshair) App.fpvCrosshair.style.display = 'none';
    if (App.fpvExitBtn) App.fpvExitBtn.style.display = 'none';
    if (App.fpvJoystick) App.fpvJoystick.style.display = 'none';
    App.canvas!.style.cursor = App.moveMode ? 'crosshair' : 'grab';
    // 重置输入
    Object.keys(App.fpvKeys).forEach(k => App.fpvKeys[k] = false);
    App.fpvMoveVec.x = 0;
    App.fpvMoveVec.y = 0;
    App.fpvMovePointerId = null;
    App.fpvLookPointerId = null;
    // 保留 FPV 探索时的相机位置：计算与默认相机目标的偏移
    const mcY = 1.0;
    // 根据当前 FPV 距离更新 camZoom，使后续缩放从合理基准开始
    const dist = Math.hypot(App.fpvPos.x, App.fpvPos.z);
    App.camZoom = THREE.MathUtils.clamp(dist / App.cameraDistance, App.MIN_ZOOM, App.MAX_ZOOM);
    // 用更新后的 camZoom 计算默认目标位置，再求偏移
    const baseZ = App.cameraDistance * App.camZoom;
    const baseY = mcY + (App.cameraHeight - mcY) * App.camZoom;
    App.camOffsetX = App.fpvPos.x;
    App.camOffsetY = App.fpvPos.y - baseY;
    App.camOffsetZ = App.fpvPos.z - baseZ;
    // 标记刚退出：下一帧动画循环中立即 snap 角色角度，跳过缓慢 lerp
    App.fpvJustExited = true;
    // 重置拖拽环绕偏移，确保相机在退出FPV后从默认位置开始
    App.dragOrbitYaw = 0;
    App.dragOrbitPitch = 0;
    App.saveSceneState();
    App.showToast('已退出第一人称探索');
    App.sendAIAction('（用户回到了你面前，停下来注视着你，目光里充满了温柔和期待）', true);
  };
  App.toggleFPV = function toggleFPV() {
    if (App.fpvMode) App.exitFPV();else App.enterFPV();
  }; // 显示浮动摇杆在指定屏幕坐标
  App.showFloatingJoystick = function showFloatingJoystick(x: number, y: number) {
    if (!App.fpvJoystick) return;
    App.fpvJoystick.style.left = x + 'px';
    App.fpvJoystick.style.top = y + 'px';
    App.fpvJoystick.style.display = 'block';
    if (App.fpvJoystickThumb) App.fpvJoystickThumb.style.transform = 'translate(0,0)';
  }; // 更新摇杆拇指位置 + 移动向量
  App.updateFloatingJoystick = function updateFloatingJoystick(dx: number, dy: number) {
    const maxR = 50;
    const len = Math.hypot(dx, dy);
    const cl = Math.min(len, maxR);
    const nx = len > 0 ? dx / len * cl : 0;
    const ny = len > 0 ? dy / len * cl : 0;
    App.fpvMoveVec.x = nx / maxR;
    App.fpvMoveVec.y = ny / maxR;
    if (App.fpvJoystickThumb) App.fpvJoystickThumb.style.transform = `translate(${nx}px, ${ny}px)`;
  }; // 隐藏浮动摇杆并停止移动
  App.hideFloatingJoystick = function hideFloatingJoystick() {
    App.fpvMoveVec.x = 0;
    App.fpvMoveVec.y = 0;
    App.fpvMovePointerId = null;
    if (App.fpvJoystick) App.fpvJoystick.style.display = 'none';
  }; // 键盘移动
  App.onFPVKeyDown = function onFPVKeyDown(e: KeyboardEvent) {
    if (!App.fpvMode) return;
    // 不拦截输入框中的按键（允许正常打字聊天）
    const tag = (e.target as HTMLElement | null)?.tagName;
    if (tag === 'INPUT' || tag === 'TEXTAREA') return;
    const k = e.key.toLowerCase();
    if (k === 'w' || k === 'arrowup' || k === 'a' || k === 'arrowleft' || k === 's' || k === 'arrowdown' || k === 'd' || k === 'arrowright') {
      App.fpvKeys[k] = true;
      e.preventDefault();
    }
    if (k === 'escape') App.exitFPV();
  };
  App.onFPVKeyUp = function onFPVKeyUp(e: KeyboardEvent) {
    const k = e.key.toLowerCase();
    App.fpvKeys[k] = false;
  }; // 在 animate 中更新 FPV 相机
  App.updateFPVCamera = function updateFPVCamera(dt: number) {
    // 合并键盘 + 摇杆输入
    let forward = 0,
      strafe = 0;
    if (App.fpvKeys['w'] || App.fpvKeys['arrowup']) forward += 1;
    if (App.fpvKeys['s'] || App.fpvKeys['arrowdown']) forward -= 1;
    if (App.fpvKeys['d'] || App.fpvKeys['arrowright']) strafe += 1;
    if (App.fpvKeys['a'] || App.fpvKeys['arrowleft']) strafe -= 1;
    forward += -App.fpvMoveVec.y; // 摇杆上推 = 前进
    strafe += App.fpvMoveVec.x; // 摇杆右推 = 右移

    const len = Math.hypot(forward, strafe);
    if (len > 0.01) {
      if (len > 1) {
        forward /= len;
        strafe /= len;
      }
      const speed = App.FPV_MOVE_SPEED * dt;
      const cosY = Math.cos(App.fpvYaw),
        sinY = Math.sin(App.fpvYaw);
      // forward = (-sinY, 0, -cosY), right = (cosY, 0, -sinY)
      App.fpvPos.x += (-sinY * forward + cosY * strafe) * speed;
      App.fpvPos.z += (-cosY * forward - sinY * strafe) * speed;
    }
    App.camera!.position.copy(App.fpvPos);
    App.camera!.rotation.set(App.fpvPitch, App.fpvYaw, 0, 'YXZ');
  };
  /* ============================================================
   *  点击角色部位交互
   * ============================================================ */
});
