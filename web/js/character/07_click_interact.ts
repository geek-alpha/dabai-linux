import type { AppKernel } from '../types/app-kernel.js';
export default (function init(App: AppKernel) {
  const {
    THREE: THREE,
    GLTFLoader: GLTFLoader,
    VRMLoaderPlugin: VRMLoaderPlugin,
    VRMUtils: VRMUtils
  } = App;
  /* ============================================================
   *  点击角色部位交互
   * ============================================================ */
  App.getWorldCenter = function getWorldCenter(obj) {
    const center = new THREE.Vector3();
    if (obj) {
      const box = new THREE.Box3().setFromObject(obj);
      box.getCenter(center);
    }
    return center;
  }; // 每帧更新：弹簧阻尼击退 + 身体摇晃
  App.applyClickWobble = function applyClickWobble(target) {
    if (!App.clickWobble.active) return;
    const cw = App.clickWobble;

    // 撤销上一帧的位移和旋转
    target.position.x -= cw.prevPosX;
    target.position.z -= cw.prevPosZ;
    target.position.y -= cw.prevPosY;
    target.rotation.z -= cw.prevRotZ;
    target.rotation.x -= cw.prevRotX;
    target.rotation.y -= cw.prevRotY;

    // === 位移：弹簧阻尼 ===
    cw.velX += -cw.stiffness * cw.posX;
    cw.velZ += -cw.stiffness * cw.posZ;
    cw.velY += -cw.stiffness * cw.posY;
    cw.velX *= cw.damping;
    cw.velZ *= cw.damping;
    cw.velY *= cw.damping;
    cw.posX += cw.velX;
    cw.posZ += cw.velZ;
    cw.posY += cw.velY;

    // === 旋转：弹簧阻尼 ===
    cw.rotVelX += -cw.rotStiffness * cw.rotX;
    cw.rotVelZ += -cw.rotStiffness * cw.rotZ;
    cw.rotVelY += -cw.rotStiffness * cw.rotY;
    cw.rotVelX *= cw.rotDamping;
    cw.rotVelZ *= cw.rotDamping;
    cw.rotVelY *= cw.rotDamping;
    cw.rotX += cw.rotVelX;
    cw.rotZ += cw.rotVelZ;
    cw.rotY += cw.rotVelY;

    // 检查是否归零
    const settled = Math.abs(cw.posX) < cw.settleThreshold &&
                    Math.abs(cw.posZ) < cw.settleThreshold &&
                    Math.abs(cw.posY) < cw.settleThreshold &&
                    Math.abs(cw.velX) < cw.settleThreshold &&
                    Math.abs(cw.velZ) < cw.settleThreshold &&
                    Math.abs(cw.velY) < cw.settleThreshold &&
                    Math.abs(cw.rotX) < 0.001 &&
                    Math.abs(cw.rotZ) < 0.001 &&
                    Math.abs(cw.rotY) < 0.001;
    if (settled) {
      cw.active = false;
      cw.posX = 0; cw.posZ = 0; cw.posY = 0;
      cw.velX = 0; cw.velZ = 0; cw.velY = 0;
      cw.rotX = 0; cw.rotZ = 0; cw.rotY = 0;
      cw.rotVelX = 0; cw.rotVelZ = 0; cw.rotVelY = 0;
      cw.prevPosX = 0; cw.prevPosZ = 0; cw.prevPosY = 0;
      cw.prevRotZ = 0; cw.prevRotX = 0; cw.prevRotY = 0;
      return;
    }

    // 应用新位移
    target.position.x += cw.posX;
    target.position.z += cw.posZ;
    target.position.y += cw.posY;

    // 应用身体摇晃（叠加到现有旋转上）
    target.rotation.z += cw.rotZ;
    target.rotation.x += cw.rotX;
    target.rotation.y += cw.rotY;

    // 保存供下帧撤销
    cw.prevPosX = cw.posX;
    cw.prevPosZ = cw.posZ;
    cw.prevPosY = cw.posY;
    cw.prevRotZ = cw.rotZ;
    cw.prevRotX = cw.rotX;
    cw.prevRotY = cw.rotY;
  };
  App.identifyModelPart = function identifyModelPart(hitPoint) {
    // VRM 模式：找最近骨骼
    if (App.modelType === 'vrm' && Object.keys(App.vrmBones).length > 0) {
      const boneGroups = {
        '头顶': ['head', 'Head', 'HEAD'],
        '脸颊': ['leftEye', 'rightEye', 'Eye_L', 'Eye_R', 'nose', 'Nose', 'jaw', 'Jaw'],
        '耳朵': ['leftEar', 'rightEar', 'Ear_L', 'Ear_R'],
        '脖子': ['neck', 'Neck', 'NECK'],
        '肩膀': ['leftShoulder', 'rightShoulder', 'Shoulder_L', 'Shoulder_R'],
        '胸口': ['chest', 'upperChest', 'Chest', 'Spine1', 'Spine2', 'spine2'],
        '肚子': ['spine', 'Spine', 'SPINE'],
        '腰': ['hips', 'Hips', 'HIPS'],
        '左手': ['leftHand', 'LeftHand', 'Hand_L', 'leftThumbProximal', 'leftIndexProximal'],
        '右手': ['rightHand', 'RightHand', 'Hand_R', 'rightThumbProximal', 'rightIndexProximal'],
        '左臂': ['leftUpperArm', 'LeftUpperArm', 'UpperArm_L', 'leftLowerArm', 'LeftLowerArm', 'LowerArm_L'],
        '右臂': ['rightUpperArm', 'RightUpperArm', 'UpperArm_R', 'rightLowerArm', 'RightLowerArm', 'LowerArm_R'],
        '左大腿': ['leftUpperLeg', 'LeftUpperLeg', 'UpperLeg_L'],
        '右大腿': ['rightUpperLeg', 'RightUpperLeg', 'UpperLeg_R'],
        '左小腿': ['leftLowerLeg', 'LeftLowerLeg', 'LowerLeg_L'],
        '右小腿': ['rightLowerLeg', 'RightLowerLeg', 'LowerLeg_R'],
        '左脚': ['leftFoot', 'LeftFoot', 'Foot_L', 'leftToes', 'LeftToes', 'Toes_L'],
        '右脚': ['rightFoot', 'RightFoot', 'Foot_R', 'rightToes', 'RightToes', 'Toes_R']
      };
      let bestName = '身体',
        bestPos = new THREE.Vector3(),
        bestDist = Infinity;
      for (const [groupName, boneNames] of Object.entries(boneGroups)) {
        for (const boneName of boneNames) {
          const bone = App.vrmBones[boneName];
          if (!bone) continue;
          const pos = new THREE.Vector3();
          bone.getWorldPosition(pos);
          const dist = hitPoint.distanceTo(pos);
          if (dist < bestDist) {
            bestDist = dist;
            bestName = groupName;
            bestPos.copy(pos);
          }
        }
      }
      return {
        name: bestName,
        center: bestPos
      };
    }
    // GLTF 回退：用模型整体包围盒中心 + 高度细分
    const center = App.getWorldCenter(App.modelGroup);
    const p = hitPoint.clone();
    const bbox = new THREE.Box3().setFromObject(App.modelGroup);
    const size = bbox.getSize(new THREE.Vector3());
    const minY = bbox.min.y;
    const height = size.y || 1.8;
    const relY = (p.y - minY) / height;
    if (relY > 0.8) return {
      name: '头顶',
      center: new THREE.Vector3(p.x, minY + height * 0.88, p.z)
    };
    if (relY > 0.6) return {
      name: '胸口',
      center: new THREE.Vector3(p.x, minY + height * 0.7, p.z)
    };
    if (relY > 0.4) return {
      name: '肚子',
      center: new THREE.Vector3(p.x, minY + height * 0.5, p.z)
    };
    if (relY > 0.2) return {
      name: '腰',
      center: new THREE.Vector3(p.x, minY + height * 0.3, p.z)
    };
    return {
      name: '腿',
      center: new THREE.Vector3(p.x, minY + height * 0.1, p.z)
    };
  };
  App.exitFocusMode = function exitFocusMode() {
    if (!App.focusPart.active) return;
    App.focusPart.active = false;
    const mcY = 1.0;
    const orbitR = App.cameraDistance * App.camZoom;
    const baseY = mcY + (App.cameraHeight - mcY) * App.camZoom;
    const cp = App.camera.position;
    App.camOffsetX = cp.x - orbitR * Math.sin(App.gyroYaw) * Math.cos(App.gyroPitch);
    App.camOffsetY = cp.y - baseY - orbitR * Math.sin(App.gyroPitch);
    App.camOffsetZ = cp.z - orbitR * Math.cos(App.gyroYaw) * Math.cos(App.gyroPitch);
  };
  App.handleCharacterClick = function handleCharacterClick(e) {
    if (!App.currentAvatar || App.moveMode) return;
    // 仅 WebXR 沉浸：DOM 点击不触发戳一戳（VR 手柄射线直接调用 triggerPokeAt）
    if (App.xrPresenting) return;
    App.updatePointerNdc(e);
    App.clickRaycaster.setFromCamera(App.pointerNdc, App.camera);
    const hits = App.clickRaycaster.intersectObjects([App.currentAvatar], true);
    if (hits.length === 0) return;
    const hitPoint = hits[0].point.clone();
    App.triggerPokeAt(hitPoint, App.identifyModelPart(hitPoint));
  };

  // 戳一戳核心交互（DOM 点击与 VR 手柄共用）
  App.triggerPokeAt = function triggerPokeAt(hitPoint, result) {
    if (!App.currentAvatar) return;
    // 非管理员触碰身体：静默忽略，不给任何反馈（DOM 点击与 VR 手柄射线都汇聚到这里，
    // 一次拦截即关闭击退摇晃、相机聚焦与亲密互动消息三条副作用）
    if (!App.IS_ADMIN) return;

    // 触发击退 + 身体摇晃：基于物理的力矩计算
    // 力方向 = 从视点指向点击点（即推的方向）；WebXR 中用真实头部位置
    const eyePos = (App.xrMode === 'webxr' && App._xrHeadPos) ? App._xrHeadPos : App.camera.position;
    const forceDir = hitPoint.clone().sub(eyePos).normalize();
    // 杠杆臂 = 从角色脚底(支点)指向点击点的水平投影
    const pivot = new THREE.Vector3();
    App.currentAvatar.getWorldPosition(pivot);
    pivot.y = 0; // 以脚底为支点（符合脚踏实地）
    const leverArm = hitPoint.clone().sub(pivot);
    const leverHoriz = Math.sqrt(leverArm.x * leverArm.x + leverArm.z * leverArm.z);
    const leverUp = leverArm.y; // 垂直高度提供倾斜力矩

    // 力矩 = 杠杆臂 × 力方向（叉积）
    // torque.x: 绕X轴旋转（前后倒）= lever.z * force.y - lever.y * force.z
    // torque.z: 绕Z轴旋转（左右倾）= lever.x * force.y - lever.y * force.x
    const torqueX = leverArm.z * forceDir.y - leverArm.y * forceDir.z;
    const torqueZ = leverArm.x * forceDir.y - leverArm.y * forceDir.x;

    // 杠杆效率：远离支点的点击放大旋转
    const leverage = Math.min(leverUp * 2.5 + leverHoriz * 0.8, 2.0);
    // 位移缩放：下半身稳重，上半身活跃
    const transScale = 0.4 + Math.min(leverUp * 0.6, 0.8);

    const cw = App.clickWobble;
    cw.active = true;
    const impulse = 0.011;
    // 不清零 pos/rot——弹簧系统会从当前位置自然过渡到新速度
    cw.velX = forceDir.x * impulse * transScale;
    cw.velZ = forceDir.z * impulse * transScale;
    cw.velY = Math.abs(forceDir.y) * impulse * 0.3 + (leverUp > 1.0 ? 0.0015 : 0.0);

    // 旋转：力矩驱动，杠杆越远晃得越厉害
    cw.rotVelZ = torqueZ * 0.006 * leverage;  // 左右摇晃
    cw.rotVelX = -torqueX * 0.006 * leverage;  // 前后摇晃（推前仰后）
    // 侧推产生扭转
    cw.rotVelY = (forceDir.x * leverArm.z - forceDir.z * leverArm.x) * 0.004 * leverage;

    // 相机聚焦（非 FPV 模式）：
    //   普通模式：focusPart 相机平滑对准部位
    //   WebXR：点击时角色保持当前位置不动（传送会因视线/锚点偏差把角色推远），
    //   只做戳的互动反馈；如需凑近查看，可用摇杆移动靠近
    if (!App.fpvMode) {
      if (App.xrPresenting) {
        App.showToast(`戳了${result.name}一下~`);
      } else {
        const camToPart = App.camera.position.clone().sub(result.center).normalize();
        const focusDist = App.IS_ADMIN ? 1.2 : 2.0;
        App.focusPart.active = true;
        App.focusPart.lookAt.copy(result.center);
        App.focusPart.target.copy(result.center).addScaledVector(camToPart, focusDist);
        App.focusPart.target.y = Math.max(App.IS_ADMIN ? 0.3 : App.USER_MIN_VIEW_HEIGHT, App.focusPart.target.y);
        App.focusPart.time = 0;
        App.focusPart.name = result.name;
      }
    }
    App.showToast(
      App.xrPresenting ? `戳了${result.name}一下~` : `戳了${result.name}一下~`
    );

    // 发送互动消息给 AI，触发个性化回复（亲密互动版）
    const pokeMessages = {
      '额头': ['（在你额头上亲了一下）', '（用手摸了摸你的额头）', '（在你的额头上轻轻落下一吻）'],
      '鼻子': ['（用手指刮了一下你的鼻尖）', '（调皮地捏了捏你的鼻子）', '（凑近你，鼻尖轻轻碰了碰你）'],
      '嘴唇': ['（用手指轻轻点了一下你的嘴唇）', '（温柔地亲了你一口）', '（凑近你，在你唇边呵了一口气）'],
      '脸颊': ['（捏了捏你的脸蛋）', '（在你脸颊上亲了一下）', '（双手捧住你的脸）'],
      '耳朵': ['（在耳边轻轻吹了口气）', '（靠近你耳边说悄悄话）', '（轻轻咬了一下你的耳垂）'],
      '头顶': ['（温柔地摸着你的头顶）', '（揉了揉你蓬松的头发）', '（用手臂护住你的头）'],
      '后脑勺': ['（从背后轻轻托住你的后脑勺）', '（揉了揉你的后脑勺）', '（从背后靠近，在你后脑轻吻）'],
      '脖子': ['（把头埋在你颈窝蹭了蹭）', '（亲了一下你的脖子）', '（从背后环住你，下巴搁在你肩上）'],
      '肩膀': ['（双手搭在你肩膀上）', '（从背后抱住你，头靠在你肩上）', '（轻轻按摩你的肩膀）'],
      '胸口': ['（把头埋进你的大胸亲了亲~）', '（用手揉了揉你的大胸感受温度~）', '（在你的大胸狠狠地摸了摸~）'],
      '肚子': ['（轻轻戳了戳你的肚子）', '（双手环住你的腰靠着你）', '（摸了摸你的腹肌）'],
      '腰': ['（从背后搂住你的腰）', '（双手轻轻环住你的腰）', '（在你腰间蹭了蹭脸）'],
      '小腹': ['（手掌轻轻贴在你的小腹上）', '（从后面把你整个抱住）', '（双手轻轻按在你的小腹上）'],
      '左手': ['（握住了你的左手，和你十指相扣）', '（牵起你的左手轻轻吻了一下）', '（用两只手捧住你的右手）'],
      '右手': ['（牵住了你的右手）', '（把脸贴在右手心蹭了蹭）', '（握住你的右手放到自己心口）'],
      '左臂': ['（挽住了你的左臂）', '（轻轻抚摸着你的左臂）', '（把脸颊贴在你左臂上）'],
      '右臂': ['（靠在你右臂上）', '（抱住你的右臂不肯松开）', '（枕着你的右臂撒娇）'],
      '左前臂': ['（握住你的左手腕）', '（用手指在左手心画圈圈）', '（把下巴搁在你手心里）'],
      '右前臂': ['（拉过你的右手把玩）', '（掰着你的手指一根一根数）', '（把你的手贴在脸上）'],
      '左上臂': ['（轻轻捏了捏你的左臂肌肉）', '（靠在你的左臂旁取暖）'],
      '右上臂': ['（搭着你的右臂当靠枕）', '（用手指戳了戳你的右手肌肉）'],
      '左大腿': ['（把腿搭在你左腿上）', '（轻轻坐在了你腿上）', '（抱着你的左腿蹭了蹭）'],
      '右大腿': ['（蹭了蹭你的右腿）', '（侧坐在你右腿上）', '（手搭在你大腿上）'],
      '左小腿': ['（用小脚趾蹭了蹭你的左小腿）', '（踩着你的左脚晃啊晃）'],
      '右小腿': ['（轻轻踢了一下你的小腿）', '（脚背碰了碰你的脚踝）'],
      '左脚': ['（踩了踩你的左脚）', '（用脚趾头轻轻戳你的脚）'],
      '右脚': ['（踢了你右脚一下）', '（踮起脚尖踩在你脚上）'],
      '头': ['（亲了亲你的额头）', '（摸了摸你的头）'],
      '身体': ['（从背后抱住了你）', '（把整个人靠在你怀里）'],
      '腿': ['（轻轻踢了一下你）', '（把腿靠近你蹭了蹭）']
    };
    const msgs = pokeMessages[result.name] || pokeMessages['身体'];
    const pokeText = msgs[Math.floor(Math.random() * msgs.length)];

    // 直接发给AI，不在聊天中显示用户消息（更像自然互动）
    // ui: true 标记系统生成的点击互动文本（非用户原话）：
    // 服务端保留互动信号（RL 奖励/AI 回应），但不记录短期记忆，避免重复点击污染记忆
    if (App.ws && App.ws.readyState === WebSocket.OPEN) {
      App.ws.send(JSON.stringify({
        type: 'text',
        content: pokeText,
        ui: true
      }));
      App.setState(App.State.THINKING);
      App.showTyping();
    }
  };
  // 通用AI互动：发送动作提示给AI（不显示在聊天中）
  // userDriven=true → 用户驱动的动作（点选/换装/设置等）走 text 通道：
  //   算作真实用户输入、产生关系奖励（但不打断AI——统一调度纪律见下）
  // userDriven=false（默认）→ AI 自主行为（跳舞/走动/游戏解说等）走 ai_action 通道：
  //   不算用户输入、不产生奖励
  // 统一调度纪律：所有 AI action 不能互相打扰、不能打扰用户——
  //   AI 正在回复（THINKING/SPEAKING）或用户正在说话（LISTENING）时排队等待，
  //   AI 空闲（IDLE）后自动补发；只有用户语音/文字输入能打断 AI。
  App._pendingAIActions = [];
  App._flushPendingAIActions = function _flushPendingAIActions() {
    if (!App._pendingAIActions || App._pendingAIActions.length === 0) return;
    if (App.currentState !== App.State.IDLE) return; // 只在完全空闲时补发
    const next = App._pendingAIActions.shift();
    App._sendAIActionNow(next.message, next.userDriven);
  };
  App._sendAIActionNow = function _sendAIActionNow(message, userDriven) {
    if (!App.ws || App.ws.readyState !== WebSocket.OPEN) return;
    if (userDriven) {
      // 用户驱动的 UI 动作（点选/换装/设置等）：算真实用户互动（产生关系奖励），
      // 但文本是系统生成的描述而非用户原话——ui: true 标记让服务端不记录短期记忆
      App.ws.send(JSON.stringify({
        type: 'text',
        content: message,
        ui: true
      }));
      App.setState(App.State.THINKING);
      App.showTyping();
    } else {
      // AI 自主行为：走独立 ai_action 通道（服务端不视为用户输入、不打断 AI 说话）
      App.ws.send(JSON.stringify({
        type: 'ai_action',
        content: message
      }));
    }
  };
  App.sendAIAction = function sendAIAction(message, userDriven) {
    if (!App.ws || App.ws.readyState !== WebSocket.OPEN) return;
    // 统一调度纪律：AI 正在回复或用户正在说话时，AI action 排队等待（不打断、不打扰）
    if (App.currentState === App.State.THINKING || App.currentState === App.State.SPEAKING || App.currentState === App.State.LISTENING) {
      App._pendingAIActions.push({ message, userDriven });
      // 队列上限：防止长时间忙碌时无限堆积（保留最新，丢弃最旧）
      if (App._pendingAIActions.length > 20) App._pendingAIActions.shift();
      return;
    }
    // 空闲：直接发送
    App._sendAIActionNow(message, userDriven);
  };
  // 帧异常日志节流（避免每帧刷屏）
  let _lastAnimErrorLog = 0;
  App.animate = function animate() {
    // 帧循环由 initThree 里 renderer.setAnimationLoop 统一驱动：
    // 非 XR 走 rAF，WebXR 会话由 XR 帧调度接管（three.js 在 sessionstart/
    // sessionend 自动切换），这里不再手动 requestAnimationFrame，
    // 避免进入/退出 VR 时出现双循环导致的抖动、卡死
    //
    // 注意：three 的 rAF / XR 帧循环都是在「回调执行完之后」才调度下一帧，
    // 因此本帧内任何未捕获异常都会让循环永久停摆 → 画面卡死（动作切换、
    // 模型加载等瞬间尤其容易触发）。整帧包 try/catch：单帧异常只记录、
    // 不打断循环，保证画面永远在跑。
    try {
    // 注意：getElapsedTime() 内部会调用 getDelta() 消耗掉时间差，
    // 必须先取 dt 再取 t，否则 dt 恒为 0（曾导致第一人称摇杆移动失效）
    const dt = Math.min(App.clock.getDelta(), 0.05); // 限制 50ms 防止弹簧骨骼爆飞
    const t = App.clock.elapsedTime;

    // 平滑过渡（复位/恢复位置时用，替代硬瞬移）：每帧插值到目标点
    if (App._smoothTeleport) App.updateSmoothTeleport(dt);

    // 性能分级：判断本帧是否应该完整渲染（XR 会话必须每帧渲染）
    const doRender = App.xrPresenting ? true : App.shouldRenderFrame();

    // 计算音频幅度 (口型同步 / lipsync)
    let mouthOpen = 0;
    if (App.currentState === App.State.SPEAKING) {
      let hasAudioSignal = false;
      if (App.analyserData && App.analyser) {
        App.analyser.getByteTimeDomainData(App.analyserData);
        let sum = 0;
        for (let i = 0; i < App.analyserData.length; i++) {
          const v = (App.analyserData[i] - 128) / 128;
          sum += v * v;
        }
        const raw = Math.sqrt(sum / App.analyserData.length);
        if (raw > 0.015) {
          hasAudioSignal = true;
          // 将语音响度映射到合理的嘴型范围 0~1
          const norm = Math.min(1, (raw - 0.015) / 0.15);
          mouthOpen = norm * norm * (3 - 2 * norm); // smoothstep 曲线，范围 0~1
        }
      }
      // 无音频分析器时用备用动画
      if (!hasAudioSignal && mouthOpen < 0.005) {
        const syllable = Math.max(0, Math.sin(t * 16) * 0.4 + Math.sin(t * 27) * 0.2 + 0.15);
        const phrase = 0.3 + 0.7 * Math.max(0, Math.sin(t * 2.6 + 0.5));
        mouthOpen = 0.5 * syllable * phrase;
      }
    }

    // 统一动作调度：走路 / 摆姿势 / 转圈 等概率轮换（与角色类型无关）
    App.updateActionScheduler(dt);
    // 表情动作引擎：先计算微动作偏移/眼神/情绪覆盖，animateModel 内部叠加
    if (App.updateMotionSystem) App.updateMotionSystem(dt);
    // Mixamo 动作混合器：在 animateModel（内部含 vrm.update）之前推进，
    // 动作播放期间 animateModel 会跳过程序式骨骼写入，由混合器接管
    if (App.updateMixamoMixer) App.updateMixamoMixer(dt);
    App.animateModel(t, dt, mouthOpen);

    // VR模式：摇晃强度 → AI 感知提示（节流发送，用户摇晃时 AI 能感受到强度）
    if (App.updateVRShakeNotify) App.updateVRShakeNotify();

    // WebXR：每帧处理手柄摇杆移动/转身（射线视觉等）
    if (App.updateXRControllers) App.updateXRControllers(dt);
    // VR 世界内快捷面板（状态/视频遥控）：锚定角色右侧定位 + 节流重绘
    if (App.updateVrHud) App.updateVrHud(dt);

    // 恋爱养成系统更新（优先级高于普通互动RL）
    if (App.datingSystemActive && App._datingSystem) {
      App._datingSystem.update(dt);
    } else if (App.engagementRLActive && App._engagementRL) {
      App._engagementRL.update(dt);
    }
    // RL 自我表达：视觉层表情动作智能体（学习"怎么表现"）
    if (App._expressionRL && App._expressionRL.enabled) {
      App._expressionRL.update(dt);
    }

    // ========== 角色物理更新（重力 + 背景碰撞）==========
    // 在角色动画之后、相机更新之前执行，确保碰撞解决后的位置被相机正确使用
    {
      App.updatePlayerPhysics(dt);
    }

    // 轻量内存巡检（每 30 秒）：清理已播完音频等可回收资源，不阻塞主流程
    if (App.memoryTick) {
      App._memoryTickAcc = (App._memoryTickAcc || 30) - dt;
      if (App._memoryTickAcc <= 0) {
        App._memoryTickAcc = 30;
        App.memoryTick();
      }
    }

    // 任务直播大屏：跟随角色身后、面向相机、节流重绘（每帧调用，渲染帧外也不断流）
    if (App.updateTaskBigScreen) App.updateTaskBigScreen(dt);

    // --- 以下为视觉更新，在跳帧时不执行以节省GPU ---
    if (!doRender) return;

    // 地面光晕呼吸 + 接触阴影
    if (App.parts.glow) {
      const s = 1 + Math.sin(t * 1.6) * 0.08;
      App.parts.glow.scale.set(s, s, s);
      (App.parts.glow as any).material.opacity = 0.3 + Math.sin(t * 1.6) * 0.08;
    }
    if (App.parts.contactShadow) {
      (App.parts.contactShadow as any).material.opacity = (App.currentState === App.State.SPEAKING ? 0.35 : 0.45) + Math.sin(t * 1.6) * 0.03;
    }
    // 头顶对话气泡：说话/思考时显示，跟随头部（渲染帧才更新，纯视觉）
    if (App.updateSpeechBubble) App.updateSpeechBubble();
    if (App.fpvMode) {
      // 第一人称相机：由键盘/触摸驱动位置与朝向
      App.updateFPVCamera(dt);
    } else if (App.xrMode && App.xrMode !== 'off') {
      // WebXR 沉浸（含会话建立/退出过渡期）：相机由 WebXRManager 接管（头部追踪
      // 叠加在站位上），常规轨道相机逻辑全部跳过 —— 否则 requestSession 的异步
      // 等待期间轨道分支会继续 lerp 相机，进入后视角错位/画面跳变
      if (App.xrPresenting) {
        // 角色朝向固定，仅在临时动作结束后拉回固定锚点
        if (App.updateXRFaceUser) App.updateXRFaceUser(dt);
        // 视野升降：低头看地面1秒→缓缓升高俯视角色；抬头看天空1秒→缓缓降低（修改 XR 左右眼矩阵 Y 分量）
        if (App.updateXRHeight) App.updateXRHeight(dt);
      }
    } else if (App.focusPart.active) {
      // 点击部位聚焦模式：相机锁定不动，仅用户拖拽/缩放时退出
      App.focusPart.time += dt;
      const t_progress = Math.min(1, App.focusPart.time * 1.0);
      App.camera.position.lerp(App.focusPart.target, t_progress < 1 ? 0.08 : 1.0);
      App.camera.lookAt(App.focusPart.lookAt);
    } else {
      // 轨道环绕 + 拖拽环绕：相机以模型为中心球面环绕
      const mcY = 1.0;
      const orbitR = App.cameraDistance * App.camZoom; // 环绕半径
      const baseY = mcY + (App.cameraHeight - mcY) * App.camZoom; // 基准高度
      // 球面坐标：gyroYaw+拖拽控制水平环绕，gyroPitch+拖拽控制垂直视角
      const orbYaw = App.gyroYaw + App.dragOrbitYaw;
      let orbPitch = App.gyroPitch + App.dragOrbitPitch;
      // 隐私护栏：陀螺仪与拖拽两条输入在这里汇总，非管理员统一夹紧上下打量幅度
      if (!App.IS_ADMIN) {
        orbPitch = Math.max(-App.USER_ORBIT_PITCH_LIMIT, Math.min(App.USER_ORBIT_PITCH_LIMIT, orbPitch));
      }
      // 摄像机自动跟踪角色：无操作超过15秒，相机跟随角色移动
      const now = Date.now() / 1000;
      const idleDuration = now - App.lastInteractionTime;
      const gazeBoosted = now < App.gazeBoostUntil; // 交互后短暂触发的心有灵犀窗口
      // 心有灵犀：闲置15秒后只在4秒窗口内触发一次，或交互增益窗口激活时短暂触发
      const gazeIdleWindow = idleDuration >= App.AUTO_CAM_DELAY && idleDuration < App.AUTO_CAM_DELAY + App.MUTUAL_GAZE_WINDOW;
      App.mutualGaze = (gazeIdleWindow || gazeBoosted) && App.currentAvatar;
      // 懒初始化 autoLookTarget
      if (!App.autoLookTarget) App.autoLookTarget = new THREE.Vector3(0, mcY, 0);
      // 相机始终以角色为轨道中心（缩放/旋转都围绕角色）
      // 复用临时向量：原实现每帧 new 一个 Vector3，60fps 下持续制造垃圾对象
      if (!App._rawLookTargetTmp) App._rawLookTargetTmp = new THREE.Vector3();
      const rawLookTarget = App._rawLookTargetTmp;
      if (App.modelGroup) rawLookTarget.set(App.modelGroup.position.x, mcY, App.modelGroup.position.z);
      else rawLookTarget.set(0, mcY, 0);
      App.autoLookTarget.lerp(rawLookTarget, 0.08);
      // 倾斜角度：根据当前轨道半径计算 lookAt 目标偏移
      // 正值 = 向上仰视（lookAt 上移），负值 = 向下俯视（lookAt 下移）
      const cameraTiltRad = App.cameraTiltDeg * Math.PI / 180;
      const effectiveDist = orbitR * Math.cos(orbPitch);
      const tiltOffset = effectiveDist * Math.tan(cameraTiltRad);
      App.targetCamPos.set(App.camOffsetX + App.autoLookTarget.x + orbitR * Math.sin(orbYaw) * Math.cos(orbPitch), baseY + App.camOffsetY + orbitR * Math.sin(orbPitch), App.camOffsetZ + App.autoLookTarget.z + orbitR * Math.cos(orbYaw) * Math.cos(orbPitch));
      if (!App.IS_ADMIN) App.clampCameraForUser(App.targetCamPos);
      // 相机平滑过渡
      App.camera.position.lerp(App.targetCamPos, 0.10);
      // 看向带倾斜偏移的角色位置
      App.camera.lookAt(App.autoLookTarget.x, App.autoLookTarget.y + tiltOffset, App.autoLookTarget.z);
    }

    // 背景星空视差 (与陀螺仪反向，营造远景深度) —— VR 模式下跳过（VR 中头部追踪自带视差）
    if (App.starField && App.xrMode === 'off') App.starField.rotation.y = App.gyroYaw * 0.6;

    // 3D 背景模型：缓慢自转（VR 下关闭 —— 世界必须与用户刚性对齐，
    // 背景自转会带动地板漂移、并与注视归位的世界旋转互相打架）+ 陀螺仪视差（VR 下跳过）
    if (App.backgroundGroup) {
      if (App.backgroundAutoRotate && App.xrMode === 'off') App.backgroundGroup.rotation.y += dt * 0.05;
      if (App.xrMode === 'off') {
        // 叠加陀螺仪视差（比角色和星空更弱，营造多层次远景感）
        App.backgroundGroup.rotation.x = App.gyroPitch * 0.4;
        // 注意：自转累积在 .rotation.y，陀螺仪偏移单独用 .rotation.z 微调避免冲突
        App.backgroundGroup.rotation.z = App.gyroYaw * 0.2;
      }
    }

    // 选中高亮跟随目标变换（每帧更新边界框）
    if (App.selectionHelper && App.selectedTarget) {
      try {
        App.selectionHelper.update();
      } catch (_) {}
    }

    // AI 自主触发：长时间无交互时主动开口
    App.checkProactiveTrigger();

    // AI 自主系统：大厅自主移动更新
    if (App.aiAutonomyController) {
      App.aiAutonomyController.update(dt);
    }

    // 动态渲染：根据实测帧率自动升降采样率（不改变场景/游戏逻辑）
    if (App.adaptiveFrame && doRender) App.adaptiveFrame(dt);

    // 渲染（WebXR 由 renderer 内部路由到 XR 帧缓冲）
    App.renderer.render(App.scene, App.camera);
    } catch (e) {
      const now = performance.now();
      if (now - _lastAnimErrorLog > 1000) {
        _lastAnimErrorLog = now;
        console.error('[Animate] 帧异常（已自动续帧，画面不会卡死）:', e);
      }
    }
  };

  /* ============================================================
   *  头顶对话气泡（THREE.Sprite + Canvas 纹理，普通模式与 VR 通用）
   * ------------------------------------------------------------
   *  单气泡、无历史栈：任意时刻只有「当前正在说的这一句」。
   *  高并发加固（设计目标：稳定、抗突发、不堆叠）：
   *   1) 单实例强保证：每次刷新前清理同组内其它气泡 Sprite，
   *      杜绝"前几句气泡 + 后一句"同屏堆叠（模型热切换/异常路径的残留一并清掉）。
   *   2) 双缓冲画布：绘制只发生在离屏画布，画满后才原子换给纹理；
   *      正在显示的那张画布永不被就地改写 → 旧台词不会与新台词混帧。
   *   3) 分句序号守卫：showChatBubble(text, seq, session) 丢弃同会话内
   *      乱序/重复的过期分句（打断后服务端冲刷的残余不覆盖当前台词）。
   *   4) 每帧合并（latest-wins）：同一帧内被调用 N 次只重绘 1 次、只上传 1 次，
   *      服务端一次性冲刷上百个分片也不会造成绘制/纹理上传风暴。
   *   5) 布局缓存 + 字号阶梯：换行/字号自适应结果按文本缓存（≤64 条），
   *      重复台词零测量；字号只走 5 级阶梯，测量次数有上界。
   *  由 HEAD_BUBBLE_ENABLED 开关控制，false = 禁用（调用方不受影响）。
   * ============================================================ */
  const HEAD_BUBBLE_ENABLED = true;
  // ---- 布局常量：调风格只改这里，绘制流程里不含魔法数字 ----
  const BUBBLE_W = 1024;               // 画布宽（纹理基准分辨率，像素）
  const BUBBLE_PAD_Y = 28;             // 文本上下留白
  const BUBBLE_TAIL_H = 40;            // 指向说话者头顶的三角尾巴高度
  const BUBBLE_MAX_LINES = 4;          // 最多 4 行，超出以省略号截断
  const BUBBLE_FONTS = [56, 48, 42, 38, 34]; // 字号阶梯：取第一个能装进 4 行的
  const BUBBLE_TEXT_MAX = 400;         // 布局前截断（4 行 × 最小字号装不下更多）
  const BUBBLE_CACHE_MAX = 64;         // 布局缓存条数上限
  const BUBBLE_FONT = '"Microsoft YaHei", sans-serif';
  const BUBBLE_COLOR = '#7c5cff';      // 主角色霓虹紫（与 UI 主色调一致）
  // ---- 运行态 ----
  App._speechBubbleCanvas = null;      // 上屏画布（= 纹理 image），永不被就地改写
  App._speechBubbleDrawCanvas = null;  // 离屏绘制画布（双缓冲的另一半）
  App._speechBubbleText = '';
  App._speechBubblePending = null;     // 待绘制文本（同帧合并，latest-wins）
  App._speechBubbleSeq = -Infinity;    // 已接受的最大分句序号（乱序守卫）
  App._speechBubbleSession = null;     // 序号所属会话（换会话即重置序号）
  App._speechBubbleLayouts = new Map(); // 文本 → 布局（换行/字号/行高/画布高）缓存
  App._speechTmpVec = new THREE.Vector3();
  App._chatBubbleVisible = false;  // 显示态（true=说话中，false=淡出中）
  App._chatBubblePop = 0;          // 弹出进度 -0.5(预热) → 1
  App._chatBubbleOpacity = 0;
  App._chatBubbleShowT = 0;        // 超时保险基准（秒）
  App._chatBubblePhase = Math.random() * Math.PI * 2; // 漂浮相位
  App._chatBubbleW = 0.72;         // 气泡世界宽度（米，2026-08-26 再缩小：近看不显大）
  App._chatBubbleH = 0.5;
  App._chatBubbleBaseY = 1.95;     // 气泡底边锚点高度（米，无头骨兜底）
  App._bubbleElapsedT = 0;         // 内部计时（秒，驱动漂浮/淡出）
  App._bubbleLastT = 0;

  /** 取（懒建）离屏绘制画布：所有绘制先落在这里，画满才换给纹理 */
  function bubbleDrawCanvas() {
    let cv = App._speechBubbleDrawCanvas;
    if (!cv) {
      cv = document.createElement('canvas');
      cv.width = BUBBLE_W;
      cv.height = 1;
      App._speechBubbleDrawCanvas = cv;
    }
    return cv;
  }

  /** 气泡 Sprite 单实例保证：清掉同组内其它气泡 Sprite（热切换/异常路径的残留） */
  App._ensureSingleSpeechBubble = function _ensureSingleSpeechBubble() {
    const g = App.modelGroup;
    if (!g) return null;
    const keep = App.speechBubble || null;
    for (let i = g.children.length - 1; i >= 0; i--) {
      const c = g.children[i];
      if (!c || c === keep || !c.isSprite) continue;
      if (!c.userData || !c.userData.__dabaiBubble) continue;
      g.remove(c);   // 残留气泡：摘掉，避免同屏堆叠
      try {
        if (c.material) {
          if (c.material.map) c.material.map.dispose();
          c.material.dispose();
        }
      } catch (_) { /* 释放失败不影响显示 */ }
    }
    if (keep && keep.parent !== g) g.add(keep); // 换模型后重新挂到当前模型组
    return keep;
  };

  /** 懒建气泡 Sprite（纹理参数一次性设定：线性过滤 + 不生成 mipmap，避免缩放模糊与重传） */
  App._createSpeechBubble = function _createSpeechBubble() {
    const THREE = App.THREE;
    if (!THREE || !App.modelGroup) return null;
    const cv = App._speechBubbleCanvas || bubbleDrawCanvas();
    const tex = App._speechBubbleTex || new THREE.CanvasTexture(cv);
    tex.minFilter = THREE.LinearFilter;
    tex.magFilter = THREE.LinearFilter;
    tex.generateMipmaps = false;
    App._speechBubbleTex = tex;
    const sprite = new THREE.Sprite(new THREE.SpriteMaterial({
      map: tex,
      transparent: true,
      depthWrite: false,
      depthTest: false, // 永远绘制在角色之上：头发/头骨等几何体无法遮挡文字
      sizeAttenuation: true,
      opacity: 0
    }));
    sprite.renderOrder = 999;
    sprite.raycast = () => {}; // 不参与戳一戳/射线拾取
    sprite.userData = { __dabaiBubble: true }; // 单实例识标
    App.speechBubble = sprite;
    App.modelGroup.add(sprite);
    App._ensureSingleSpeechBubble();
    return sprite;
  };

  /**
   * 文本 → 布局：换行 + 字号自适应 + 行高 + 画布高。
   * 带缓存（重复台词零测量）；字号只走 5 级阶梯（测量次数有上界）。
   */
  App._measureBubbleLayout = function _measureBubbleLayout(text) {
    const cache = App._speechBubbleLayouts;
    const hit = cache.get(text);
    if (hit) return hit;
    const ctx = bubbleDrawCanvas().getContext('2d');
    if (!ctx) return null; // 无 2D 上下文：放弃绘制，不影响说话流程
    // 超长文本先截断：4 行 × 最小字号也装不下更多，避免无谓的测量开销
    const src = text.length > BUBBLE_TEXT_MAX ? text.slice(0, BUBBLE_TEXT_MAX) : text;
    const maxW = BUBBLE_W - 140;
    const wrap = (font) => {
      ctx.font = 'bold ' + font + 'px ' + BUBBLE_FONT;
      const ls = [];
      let cur = '';
      for (const ch of src) {
        if (ch === '\n') { if (cur) ls.push(cur); cur = ''; continue; }
        if (cur && ctx.measureText(cur + ch).width > maxW) { ls.push(cur); cur = ch; }
        else cur += ch;
      }
      if (cur) ls.push(cur);
      return ls;
    };
    let font = BUBBLE_FONTS[0];
    let lines = wrap(font);
    for (let i = 1; i < BUBBLE_FONTS.length && lines.length > BUBBLE_MAX_LINES; i++) {
      font = BUBBLE_FONTS[i];
      lines = wrap(font);
    }
    if (lines.length > BUBBLE_MAX_LINES) {
      lines = lines.slice(0, BUBBLE_MAX_LINES);
      const last = lines[BUBBLE_MAX_LINES - 1];
      lines[BUBBLE_MAX_LINES - 1] = (last.length > 1 ? last.slice(0, last.length - 1) : last) + '…';
    }
    const lineH = Math.ceil(font * 1.34); // 整数行高且恒大于字号 → 行与行永不叠字
    const H = Math.round(BUBBLE_PAD_Y * 2 + lines.length * lineH + BUBBLE_TAIL_H);
    const layout = { lines, font, lineH, H };
    if (cache.size >= BUBBLE_CACHE_MAX) cache.clear(); // 简易容量上限：够用且零维护
    cache.set(text, layout);
    return layout;
  };

  /**
   * 在离屏画布上绘制赛博风气泡（圆角深色底 + 霓虹描边 + 指向头顶的尾巴），
   * 画满后原子换给纹理。返回是否成功上屏。
   */
  App._drawChatBubbleCanvas = function _drawChatBubbleCanvas(text) {
    const layout = App._measureBubbleLayout(text);
    if (!layout || !layout.lines.length) return false;
    const cv = bubbleDrawCanvas();
    const ctx = cv.getContext('2d');
    if (!ctx) return false;
    const W = BUBBLE_W;
    const H = layout.H;
    const font = layout.font;
    const lines = layout.lines;
    const lineH = layout.lineH;
    if (cv.width !== W) cv.width = W;      // 只在尺寸变化时重设（重设会重建位图并清空）
    if (cv.height !== H) cv.height = H;
    ctx.clearRect(0, 0, W, H);
    // 气泡主体（圆角矩形）
    const r = 46;
    const bx = 4, by = 4;
    const bw = W - bx * 2;
    const bh = H - BUBBLE_TAIL_H - by;
    const body = () => {
      ctx.beginPath();
      ctx.moveTo(bx + r, by);
      ctx.arcTo(bx + bw, by, bx + bw, by + bh, r);
      ctx.arcTo(bx + bw, by + bh, bx, by + bh, r);
      ctx.arcTo(bx, by + bh, bx, by, r);
      ctx.arcTo(bx, by, bx + bw, by, r);
      ctx.closePath();
    };
    // 霓虹描边辉光
    ctx.save();
    ctx.shadowColor = BUBBLE_COLOR;
    ctx.shadowBlur = 26;
    body();
    ctx.fillStyle = 'rgba(8, 12, 30, 0.92)';
    ctx.fill();
    ctx.lineWidth = 9;
    ctx.strokeStyle = BUBBLE_COLOR;
    ctx.stroke();
    ctx.restore();
    // 尾巴（底部居中，尖端指向下方头顶）
    ctx.save();
    ctx.shadowColor = BUBBLE_COLOR;
    ctx.shadowBlur = 20;
    ctx.beginPath();
    ctx.moveTo(W / 2 - 46, bh + 2);
    ctx.lineTo(W / 2 + 46, bh + 2);
    ctx.lineTo(W / 2, H - 2);
    ctx.closePath();
    ctx.fillStyle = 'rgba(8, 12, 30, 0.92)';
    ctx.fill();
    ctx.lineWidth = 8;
    ctx.strokeStyle = BUBBLE_COLOR;
    ctx.stroke();
    ctx.restore();
    // 台词文本（水平居中、逐行绘制；行高恒大于字号 → 永不上下叠字）
    ctx.save();
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.fillStyle = '#f2f6ff';
    ctx.shadowColor = BUBBLE_COLOR;
    ctx.shadowBlur = 10;
    ctx.font = 'bold ' + font + 'px ' + BUBBLE_FONT;
    const totalTextH = lines.length * lineH;
    let ty = BUBBLE_PAD_Y + lineH * 0.5 + (bh - BUBBLE_PAD_Y * 2 - totalTextH) * 0.5;
    for (let i = 0; i < lines.length; i++) {
      ctx.fillText(lines[i], W / 2, ty);
      ty += lineH;
    }
    ctx.restore();
    // 原子换给纹理：先画满再上屏，正在显示的画布永不被就地改写（无残影、无新旧混帧）
    const prevOnScreen = App._speechBubbleCanvas;
    App._speechBubbleCanvas = cv;
    App._speechBubbleDrawCanvas = (prevOnScreen && prevOnScreen !== cv)
      ? prevOnScreen
      : document.createElement('canvas');
    const tex = App._speechBubbleTex;
    if (tex) {
      tex.image = cv;
      tex.needsUpdate = true;
    }
    return true;
  };

  /** 气泡世界尺寸与位置：底边锚在头顶上方，气泡越高越向上生长（新句更高时自动向上让位） */
  App._layoutSpeechBubbleSprite = function _layoutSpeechBubbleSprite() {
    const cv = App._speechBubbleCanvas;
    if (!cv || !App.speechBubble) return;
    // VR 放大到 1.15m（与快捷面板 1.1m 同量级）：头显角分辨率远低于屏幕，
    // 0.72m 在 1.8m 观距下文字过小难读。
    const asp = cv.height / Math.max(1, cv.width);
    const vr = !!App.xrPresenting;
    App._chatBubbleW = vr ? 1.15 : 0.72;
    App._chatBubbleH = Math.min(vr ? 1.25 : 0.85, App._chatBubbleW * asp);
    let hy = 1.95;
    if (App.headBone) {
      try {
        App.headBone.getWorldPosition(App._speechTmpVec);
        App.modelGroup.worldToLocal(App._speechTmpVec);
        hy = App._speechTmpVec.y + 0.30;
      } catch (_) { hy = 1.95; }
    }
    App._chatBubbleBaseY = hy;
    App.speechBubble.position.y = hy + App._chatBubbleH * 0.5;
  };

  /**
   * 说话时显示气泡：每段语音开始播放时调用，text = 当前正在说的分句。
   * seq / session 可选：用于丢弃乱序或过期的迟到分句（防串台）。
   * 只登记待绘制文本，真正重绘合并到本帧的 updateSpeechBubble（同帧 N 次调用 = 1 次绘制）。
   */
  App.showChatBubble = function showChatBubble(text, seq, session) {
    if (!HEAD_BUBBLE_ENABLED) return; // 已禁用：不创建/不显示气泡
    if (!App.modelGroup || !App.THREE) return;
    const txt = String(text == null ? '' : text).trim();
    if (!txt) return;                 // 空文本不显示（隐藏由 hideChatBubble 负责）
    // 换会话：序号从 1 重新开始（否则新回复会被旧序号守卫误杀）
    if (session !== undefined && session !== null && session !== App._speechBubbleSession) {
      App._speechBubbleSession = session;
      App._speechBubbleSeq = -Infinity;
    }
    // 乱序守卫：仅在带会话信息时生效（同会话内序号不回退），过期分句直接丢弃
    if (session !== undefined && session !== null && typeof seq === 'number' && isFinite(seq)) {
      if (seq <= App._speechBubbleSeq) return;
      App._speechBubbleSeq = seq;
    }
    // 同句重复（重连重放/同一 tick 重复推送）：只续期不重绘
    if (txt === App._speechBubbleText && App.speechBubble && App._chatBubbleVisible) {
      App._speechBubblePending = null;
      App._chatBubbleShowT = App._bubbleElapsedT;
      return;
    }
    if (!App.speechBubble && !App._createSpeechBubble()) return;
    App._ensureSingleSpeechBubble();
    App._speechBubblePending = txt;   // 待绘制：本帧合并重绘（latest-wins）
    App._chatBubbleVisible = true;
    App._chatBubblePop = -0.5;    // 预热：等 CanvasTexture 上传完成再淡入，防止旧文字闪现
    App._chatBubbleOpacity = 0;   // 干净重开，防止旧淡出残留
    App._chatBubbleShowT = App._bubbleElapsedT;
    App.speechBubble.visible = true;
    App.speechBubble.material.opacity = 0;
  };

  /** 说话结束调用（自动淡出）；未绘制的待显示台词一并丢弃，避免淡出后又冒出来 */
  App.hideChatBubble = function hideChatBubble() {
    App._speechBubblePending = null;
    App._speechBubbleText = '';
    App._chatBubbleVisible = false;
  };

  // 每帧更新：合并重绘 / 弹出 / 漂浮 / 淡出（doRender 每帧调用，内部自算 dt）
  App.updateSpeechBubble = function updateSpeechBubble() {
    if (!HEAD_BUBBLE_ENABLED) return; // 已禁用：跳过气泡动画
    if (!App.speechBubble) return;
    const now = performance.now();
    const dt = Math.min(0.1, Math.max(0.001, (now - App._bubbleLastT) / 1000));
    App._bubbleLastT = now;
    App._bubbleElapsedT += dt;
    const mat = App.speechBubble.material;
    // 合并绘制：本帧有新台词才重绘一次（同一帧内 N 次 showChatBubble = 1 次绘制 + 1 次上传）
    if (App._speechBubblePending != null) {
      const txt = App._speechBubblePending;
      App._speechBubblePending = null;
      if (txt !== App._speechBubbleText) {
        try {
          if (App._drawChatBubbleCanvas(txt)) {
            App._speechBubbleText = txt;
            App._layoutSpeechBubbleSprite();
          }
        } catch (e) {
          console.warn('[气泡] 绘制失败（本次刷新忽略，不影响说话）:', e);
        }
      }
    }
    // 超时保险：说话流程异常中断未调用 hideBubble 时，15s 后强制隐藏，杜绝气泡残留
    if (App._chatBubbleVisible && App._bubbleElapsedT - App._chatBubbleShowT > 15) {
      App._chatBubbleVisible = false;
    }
    if (!App._chatBubbleVisible) {
      App._chatBubbleOpacity = Math.max(0, App._chatBubbleOpacity - dt * 3.2);
      mat.opacity = App._chatBubbleOpacity;
      if (App._chatBubbleOpacity <= 0) App.speechBubble.visible = false;
      return;
    }
    // 近看补偿：相机离得越近，气泡世界尺寸越小（屏幕占比大致恒定），
    // 避免镜头怼近时气泡撑满屏幕；拉远时轻微放大保证可读性。
    // 基准距离 = 默认相机距离 2.5（此时 near=1，尺寸不改变）。
    // VR 例外：固定 near=1（世界尺寸恒定，与快捷面板一致）——VR 标准
    // 观距 1.8m 会被常规补偿缩到 0.73 倍，文字过小。
    let near = 1;
    if (!App.xrPresenting) {
      try {
        const d = App._speechTmpVec.copy(App.camera.position).sub(App.speechBubble.position).length();
        near = Math.min(1.6, Math.max(0.5, d / 2.5));
      } catch (_) { near = 1; }
    }
    // 预热阶段：等 CanvasTexture 上传完成（约 0.08s）再淡入，避免新台词瞬间仍显示旧纹理
    if (App._chatBubblePop < 0) {
      App._chatBubblePop += dt * 6;
      App.speechBubble.visible = true;
      App.speechBubble.scale.set(App._chatBubbleW * 0.3, App._chatBubbleH * 0.3, 1);
      mat.opacity = 0;
      return;
    }
    App.speechBubble.visible = true;
    App._chatBubblePop = Math.min(1, App._chatBubblePop + dt * 5);
    const e = 1 - Math.pow(1 - App._chatBubblePop, 3);   // easeOutCubic 弹出
    const s = 0.55 + 0.45 * e;
    App.speechBubble.scale.set(App._chatBubbleW * s * near, App._chatBubbleH * s * near, 1);
    App._chatBubbleOpacity = Math.min(1, App._chatBubbleOpacity + dt * 7);
    mat.opacity = App._chatBubbleOpacity;
    // 轻微漂浮（底部锚点仍在 _chatBubbleBaseY，弹跳幅度随近看补偿同步缩小）
    App.speechBubble.position.y = App._chatBubbleBaseY + App._chatBubbleH * s * near / 2 +
      Math.sin(App._bubbleElapsedT * 2.2 + App._chatBubblePhase) * 0.05 * near;
  };

  /* ============================================================
   *  角色物理系统：重力 + 背景模型碰撞检测
   *  参考 treasure-hunt.js 的 _updatePlayerPhysics / _checkWallCollision
   * ============================================================ */

  /** 重力 + 着地 + 碰撞解决（子步进防止高速穿透薄物体） */
  App.updatePlayerPhysics = function updatePlayerPhysics(dt) {
    const avatar = App.currentAvatar;
    if (!avatar) return;
    // 平滑过渡期间由 updateSmoothTeleport 独占位置，物理让路
    if (App._smoothTeleport) return;

    // 射线检测脚下地面
    const groundY = App._raycastGround(avatar.position.x, avatar.position.z, avatar.position.y);

    if (App._playerIsGrounded) {
      const prevGroundY = App._playerGroundY;
      // 走空/下台阶：地面降低超过阈值，开始下落
      if (groundY < prevGroundY - 0.05) {
        App._playerIsGrounded = false;
      } else {
        App._playerGroundY = groundY;
        if (avatar.position.y < groundY) {
          avatar.position.y = groundY;
        }
        return;
      }
    }

    // 下落：子步进
    const subDt = dt / App._PHYSICS_SUBSTEPS;
    for (let i = 0; i < App._PHYSICS_SUBSTEPS; i++) {
      App._playerVelocityY -= App._GRAVITY * subDt;
      if (App._playerVelocityY < -App._MAX_FALL_SPEED) {
        App._playerVelocityY = -App._MAX_FALL_SPEED;
      }
      avatar.position.y += App._playerVelocityY * subDt;

      const subGroundY = App._raycastGround(avatar.position.x, avatar.position.z, avatar.position.y);

      if (avatar.position.y <= subGroundY) {
        avatar.position.y = subGroundY;
        App._playerIsGrounded = true;
        App._playerGroundY = subGroundY;
        App._playerVelocityY = 0;
        if (!App._lastGroundedLog || performance.now() - App._lastGroundedLog > 2000) {
          App._lastGroundedLog = performance.now();
          console.log('[物理] 着地 Y=' + avatar.position.y.toFixed(2));
        }
        return;
      }
    }
  };

  /** 平滑过渡（复位/恢复位置）：smoothstep 插值到目标点，替代硬瞬移 */
  App.updateSmoothTeleport = function updateSmoothTeleport(dt) {
    const st = App._smoothTeleport;
    if (!st) return;

    const avatar = App.currentAvatar || App.modelGroup;
    if (!avatar) { App._smoothTeleport = null; return; }
    st.t += dt / st.dur;
    const k = st.t >= 1 ? 1 : st.t * st.t * (3 - 2 * st.t);
    avatar.position.x = st.x0 + (st.x1 - st.x0) * k;
    avatar.position.y = st.y0 + (st.y1 - st.y0) * k;
    avatar.position.z = st.z0 + (st.z1 - st.z0) * k;
    if (st.t >= 1) {
      App._smoothTeleport = null;
      App._playerGroundY = avatar.position.y;
      App._playerIsGrounded = true;
      App._playerVelocityY = 0;
    }
  };

  /** 射线检测：从角色脚底上方往下射，命中背景mesh则返回命中点Y */
  App._raycastGround = function _raycastGround(x, z, currentY) {
    if (!App.backgroundGroup) return 0;

    App._groundRayOrigin.set(x, currentY + 0.3, z);
    App._groundRaycaster.set(App._groundRayOrigin, App._groundRayDir);
    App._groundRaycaster.far = currentY + 1.5;

    const hits = App._groundRaycaster.intersectObjects(App.backgroundGroup.children, true);
    if (hits.length > 0) {
      return hits[0].point.y;
    }
    return 0; // 无背景或未命中 → y=0
  };

  /** 查找场景地板：从高处往下射，取所有命中点中最低的那个（真正的室内地板，不是天花板） */
  App._findFloorY = function _findFloorY(x, z) {
    if (!App.backgroundGroup) return 0;

    App._groundRayOrigin.set(x, 100, z);
    App._groundRaycaster.set(App._groundRayOrigin, App._groundRayDir);
    App._groundRaycaster.far = 200;

    const hits = App._groundRaycaster.intersectObjects(App.backgroundGroup.children, true);
    if (hits.length === 0) return 0;

    // 取所有命中点中 Y 最低的那个 = 真正的室内地面（穿透天花板到地板）
    let lowestY = hits[0].point.y;
    for (let i = 1; i < hits.length; i++) {
      if (hits[i].point.y < lowestY) lowestY = hits[i].point.y;
    }
    return lowestY;
  };

  App.checkProactiveTrigger = function checkProactiveTrigger() {
    if (!App.ws || App.ws.readyState !== WebSocket.OPEN) return;

    if (App.currentState !== App.State.IDLE) return;
    if (App.isRecording || App.isPlayingQueue) return;
    const now = Date.now();
    if (now - App.lastUserActivityTime < App.PROACTIVE_SILENCE_MS) return;
    if (now - App.lastProactiveTime < App.PROACTIVE_COOLDOWN_MS) return;
    // RL 调度去重：RL 心跳刚派发过主动说话（2 分钟内），前端召唤跳过，
    // 避免两条主动路径叠加造成"自言自语"观感
    if (App._rlLastDispatchTime && (now - App._rlLastDispatchTime) < 120000) return;
    App.lastProactiveTime = now;
    App.ws.send(JSON.stringify({
      type: 'proactive'
    }));
  }; // 计算头部看向相机的局部旋转角度 {x: pitch, y: yaw}
  // 在非探索模式下让角色始终注视用户，增强被重视感
  // 统一约定：模型 mesh 前方向为 +Z，相机位于 +Z，因此头部只需轻微调整
  // 收紧跟随范围（yaw ≤7°、pitch ≤5°）：lookAt 只做轻微注视，大角度转向由动作系统负责，
  // 避免"转头动作结束后头部一直偏着不复位"的观感
  App.computeHeadLookAt = function computeHeadLookAt(modelRoot) {
    if (!modelRoot || App.fpvMode) return null;
    // VR（WebXR）中主相机 position 冻结在进入时的站位（头显姿态只写矩阵不写 position），
    // 用真实头部位置（_xrHeadPos，每帧从 XR 相机矩阵更新）让头部注视始终跟随用户。
    // 复用临时向量避免每帧 clone 触发 GC 微卡顿（VR 90Hz 每帧调用）
    const camPos = (App.xrPresenting && App._xrHeadPos) ? App._xrHeadPos : App.camera.position;
    if (!App._headLookTmpVec) App._headLookTmpVec = new THREE.Vector3();
    const localCam = App._headLookTmpVec.copy(camPos);
    modelRoot.worldToLocal(localCam);
    const dx = localCam.x;
    const dz = localCam.z;
    const dy = localCam.y - 1.4; // 近似头部高度
    const horizDist = Math.max(0.01, Math.hypot(dx, dz));
    const yaw = THREE.MathUtils.clamp(Math.atan2(-dx, dz), -0.12, 0.12);
    const pitch = THREE.MathUtils.clamp(Math.atan2(dy, horizDist), -0.10, 0.08);
    return {
      x: pitch,
      y: yaw
    };
  }; // 计算角色身体应转向的世界Y角度（面向相机）
  // 统一约定：模型 mesh 前方向为 +Z，目标角度 = atan2(dx, dz)
  // 可传入 camPos 计算指定相机位置下的目标角度（用于重置时直接对齐默认视角）
  App.computeBodyFaceCam = function computeBodyFaceCam(modelRoot, camPos) {
    if (!modelRoot || App.fpvMode) return 0;
    // 省略 camPos = 基准机位方位（相机基准偏移固定，不含拖拽环绕/陀螺仪）：
    // 用户转视角时角色朝向不动 —— 60° 打量的是用户的视点，不是让角色转角度
    if (!camPos) return Math.atan2(App.camOffsetX, App.camOffsetZ + App.cameraDistance * App.camZoom);
    const dx = camPos.x - modelRoot.position.x;
    const dz = camPos.z - modelRoot.position.z;
    return Math.atan2(dx, dz);
  };
  /* 安全设置 VRM 表情：仅在 expressionMap 中存在时才设置 */
  App.setVRMExpression = function setVRMExpression(exp, name, value) {
    if (!exp || !exp.expressionMap) return false;
    if (!exp.expressionMap[name]) return false;
    exp.setValue(name, value);
    return true;
  };
  /* 查找可用的表情名 (VRM 1.0 用小写，VRM 0.x 用大写) */
  App.findExpression = function findExpression(exp, candidates) {
    if (!exp || !exp.expressionMap) return null;
    for (const name of candidates) {
      if (exp.expressionMap[name]) return name;
    }
    return null;
  }; // 缓存表情名，避免每帧查找
  App.exprNames = {
    mouth: null,
    blink: null,
    blinkLeft: null,
    blinkRight: null,
    happy: null,
    angry: null,
    sad: null,
    surprised: null,
    relaxed: null,
    thoughtful: null,
    sorrow: null,
    fun: null,
    lookUp: null,
    lookDown: null,
    neutral: null
  };
  App.refreshExprNames = function refreshExprNames() {
    if (!App.vrm || !App.vrm.expressionManager) {
      App.exprNames = {
        mouth: null,
        blink: null,
        blinkLeft: null,
        blinkRight: null,
        happy: null,
        angry: null,
        sad: null,
        surprised: null,
        relaxed: null,
        thoughtful: null,
        sorrow: null,
        fun: null,
        lookUp: null,
        lookDown: null,
        neutral: null
      };
      return;
    }
    const exp = App.vrm.expressionManager;
    App.exprNames.mouth = App.findExpression(exp, ['aa', 'A', 'oh', 'O', 'ou', 'U', 'ih', 'I']);
    App.exprNames.blink = App.findExpression(exp, ['blink', 'Blink', 'blinkLeft', 'BlinkL']);
    App.exprNames.blinkLeft = App.findExpression(exp, ['blinkLeft', 'BlinkL', 'blink_L', 'Blink_L', 'winkLeft', 'WinkLeft']);
    App.exprNames.blinkRight = App.findExpression(exp, ['blinkRight', 'BlinkR', 'blink_R', 'Blink_R', 'winkRight', 'WinkRight']);
    App.exprNames.happy = App.findExpression(exp, ['happy', 'Joy', 'fun', 'Fun']);
    App.exprNames.angry = App.findExpression(exp, ['angry', 'Angry', 'mad', 'Mad']);
    App.exprNames.sad = App.findExpression(exp, ['sad', 'Sad', 'sorrow', 'Sorrow']);
    App.exprNames.surprised = App.findExpression(exp, ['surprised', 'Surprised', 'surprise', 'Surprise', 'shock', 'Shock']);
    App.exprNames.relaxed = App.findExpression(exp, ['relaxed', 'Relaxed', 'peaceful', 'Peaceful', 'calm', 'Calm']);
    App.exprNames.thoughtful = App.findExpression(exp, ['thoughtful', 'Thoughtful', 'think', 'Think']);
    App.exprNames.sorrow = App.findExpression(exp, ['sorrow', 'Sorrow', 'sad', 'Sad']);
    App.exprNames.fun = App.findExpression(exp, ['fun', 'Fun', 'happy', 'Joy']);
    App.exprNames.lookUp = App.findExpression(exp, ['lookUp', 'LookUp', 'lookup', 'Lookup', 'browUp', 'BrowUp']);
    App.exprNames.lookDown = App.findExpression(exp, ['lookDown', 'LookDown', 'lookdown', 'Lookdown', 'browDown', 'BrowDown']);
    App.exprNames.neutral = App.findExpression(exp, ['neutral', 'Neutral']);
    // 列出可用表情
    const mapped = [];
    for (const k in App.exprNames) { if (App.exprNames[k]) mapped.push(k + ':' + App.exprNames[k]); }
    console.log('[VRM] 表情映射 →', mapped.join(', ') || '(无可用表情)');
  };
  /* ========== 表情状态机 ========== */
  // 表情值：每个表情的当前值 0-1，平滑过渡
  App.exprValues = {};
  App.exprTargets = {};
  App.exprChangeTimer = 0;
  App.exprChangeInterval = 4.0; // 空闲时切换表情的间隔
  App.exprRandomPool = []; // 空闲时从可用表情池中随机选取

  App.initExpressionState = function initExpressionState() {
    App.exprValues = {};
    App.exprTargets = {};
    for (const k in App.exprNames) {
      if (App.exprNames[k]) {
        App.exprValues[k] = 0;
        App.exprTargets[k] = 0;
      }
    }
    // 构建空闲随机表情池：排除 mouth/blink/blinkLeft/blinkRight/lookUp/lookDown/neutral
    const emotionKeys = ['happy', 'fun', 'relaxed', 'surprised', 'thoughtful', 'sad', 'angry', 'sorrow'];
    App.exprRandomPool = emotionKeys.filter(k => App.exprNames[k]);
  };
  // 不在这里驱动的表情（由外部独立控制）
  App.EXPR_EXTERNAL_KEYS = new Set(['mouth', 'blink', 'blinkLeft', 'blinkRight']);
  // 说话时会干扰嘴型的情绪表情（部分 VRM 模型的 happy/surprised 包含嘴部 blendshape）
  App.EXPR_MOUTH_BLOCK_KEYS = new Set(['happy', 'fun', 'surprised', 'sad', 'angry', 'sorrow']);

  /* ========== 眨眼增强系统：五种等概率 ========== */
  App.blinkType = 'normal'; // 'normal' | 'slow' | 'double' | 'winkLeft' | 'winkRight' | 'closeHold'
  App.blinkPhase = 0; // 当前眨眼阶段进度 0~1
  App.blinkDuration = 0.15; // 当前眨眼的持续时间

  // 调度下一个眨眼类型（五种等概率各 20%）
  App.scheduleNextBlink = function scheduleNextBlink() {
    const r = Math.random();
    if (r < 0.2) {
      // 20% 普通眨眼
      App.blinkType = 'normal';
      App.blinkDuration = 0.12 + Math.random() * 0.08; // 0.12~0.20s
    } else if (r < 0.4) {
      // 20% 慢眨眼（放松、满足感）
      App.blinkType = 'slow';
      App.blinkDuration = 0.35 + Math.random() * 0.2; // 0.35~0.55s
    } else if (r < 0.6) {
      // 20% 双连眨
      App.blinkType = 'double';
      App.blinkDuration = 0.1;
    } else if (r < 0.8) {
      // 20% 挤眼（慢挤，50%左眼50%右眼）
      App.blinkType = Math.random() < 0.5 ? 'winkLeft' : 'winkRight';
      App.blinkDuration = 0.35 + Math.random() * 0.2; // 0.35~0.55s 与慢眨眼同速
    } else {
      // 20% 全闭眼停留（仅空闲时，非空闲降级为慢眨眼）
      if (App.currentState === App.State.IDLE) {
        App.blinkType = 'closeHold';
        App.blinkDuration = 0.7 + Math.random() * 0.6; // 0.7~1.3s
      } else {
        App.blinkType = 'slow';
        App.blinkDuration = 0.35 + Math.random() * 0.2;
      }
    }
    App.blinkPhase = 0;
  };

  // 根据状态决定目标表情
  App.computeExprTargetsByState = function computeExprTargetsByState() {
    const targets: Record<string, number> = {};
    for (const k in App.exprNames) { if (App.exprNames[k]) targets[k] = 0; }
    switch (App.currentState) {
      case App.State.IDLE:
        // 空闲：默认微笑+放松，偶尔随机切换
        targets.happy = 0.4;
        targets.relaxed = 0.3;
        break;
      case App.State.THINKING:
        targets.thoughtful = 0.5;
        targets.relaxed = 0.2;
        // 微微皱眉思考
        if (App.exprNames.sad) targets.sad = 0.15;
        break;
      case App.State.LISTENING:
        // 倾听：兴趣/惊讶+微笑
        targets.happy = 0.35;
        if (App.exprNames.surprised) targets.surprised = 0.3;
        break;
      case App.State.SPEAKING:
        // 说话时只保留眼睛/眉毛/脸颊的表情，避免嘴部相关表情干扰 lipsync
        // 惊讶/开心等表情可能包含嘴部 blendshape，说话时排除这些
        targets.happy = 0.2;
        targets.fun = 0.25;
        if (App.exprNames.relaxed) targets.relaxed = 0.15;
        break;
    }
    return targets;
  };

  // 在空闲状态随机切换表情
  App.updateIdleExpression = function updateIdleExpression(dt) {
    // 原有随机表情切换逻辑
    if (App.currentState !== App.State.IDLE) {
      App.exprChangeTimer = 0;
      App.exprChangeInterval = 4 + Math.random() * 6;
      return;
    }
    // 情绪覆盖（RL/动作系统）生效期间暂停随机表情，避免打架
    if (App.emotionOverlayActive && App.emotionOverlayActive()) {
      App.exprChangeTimer = 0;
      return;
    }
    App.exprChangeTimer += dt;
    if (App.exprChangeTimer >= App.exprChangeInterval && App.exprRandomPool.length > 0) {
      App.exprChangeTimer = 0;
      App.exprChangeInterval = 4 + Math.random() * 6; // 4~10秒变化一次
      // 合并 lookUp/lookDown 到表情切换中（小概率触发）
      const pool = [...App.exprRandomPool];
      if (App.exprNames.lookUp) pool.push('lookUp');
      if (App.exprNames.lookDown) pool.push('lookDown');
      // 随机选一个表情突出显示
      const pick = pool[Math.floor(Math.random() * pool.length)];
      for (const k in App.exprTargets) {
        App.exprTargets[k] = 0;
      }
      // lookUp/lookDown 强度稍低，避免鬼脸
      if (pick === 'lookUp' || pick === 'lookDown') {
        App.exprTargets[pick] = 0.35 + Math.random() * 0.25;
      } else {
        App.exprTargets[pick] = 0.5;
      }
      // 保留基础微笑
      if (App.exprNames.happy && pick !== 'happy') App.exprTargets.happy = 0.2;
      if (App.exprNames.relaxed && pick !== 'relaxed') App.exprTargets.relaxed = 0.15;
    }
  };

  // 更新所有表情值（平滑过渡 + 根据状态设置目标）
  App.updateExpressions = function updateExpressions(dt) {
    const exp = App.vrm && App.vrm.expressionManager;
    if (!exp || !exp.expressionMap) return;
    // 首次初始化
    if (Object.keys(App.exprValues).length === 0) App.initExpressionState();
    // 根据状态设定目标表情（基础值）
    const baseTargets = App.computeExprTargetsByState();
    // 合并空闲随机表情
    App.updateIdleExpression(dt);
    // 合并：空闲随机优先级高于基础
    if (App.currentState === App.State.IDLE && App.exprChangeTimer > 0) {
      // 空闲随机模式已在 exprTargets 中设定，保留
    } else {
      // 非空闲或未触发随机时，使用基础目标
      for (const k in baseTargets) {
        App.exprTargets[k] = baseTargets[k];
      }
    }
    // 情绪覆盖（RL/动作系统的临时情绪：害羞/惊讶/委屈/爱慕等）优先级最高
    const overlayTargets = App.getEmotionOverlayTargets ? App.getEmotionOverlayTargets() : null;
    if (overlayTargets) {
      for (const k in overlayTargets) {
        App.exprTargets[k] = Math.max(App.exprTargets[k] || 0, overlayTargets[k] || 0);
      }
    }
    // 平滑过渡并设置（排除 mouth/blink，它们由外部独立控制）
    // 说话时额外屏蔽可能包含嘴部 blendshape 的情绪表情
    const isSpeaking = App.currentState === App.State.SPEAKING;
    for (const k in App.exprTargets) {
      if (!App.exprNames[k] || App.EXPR_EXTERNAL_KEYS.has(k)) continue;
      if (isSpeaking && App.EXPR_MOUTH_BLOCK_KEYS.has(k)) continue;
      const target = App.exprTargets[k] || 0;
      App.exprValues[k] = App.lerp(App.exprValues[k] || 0, target, 0.04);
      if (Math.abs(App.exprValues[k] - target) < 0.005) App.exprValues[k] = target;
      // 很小的值直接清零避免残留
      if (App.exprValues[k] < 0.01 && target === 0) App.exprValues[k] = 0;
      exp.setValue(App.exprNames[k], App.exprValues[k]);
    }
  };
  /* 调优弹簧骨骼：增大阻尼、降低刚度，防止头发衣物乱摆穿模 */
  App.calmSpringBones = function calmSpringBones() {
    if (!App.vrm || !App.vrm.springBoneManager) {
      console.log('[VRM] 无弹簧骨骼管理器');
      return;
    }
    const mgr = App.vrm.springBoneManager;
    // three-vrm v3: joints 数组 (可能叫 joints / _sortedJoints)
    const joints = mgr.joints || mgr._sortedJoints || Array.from(mgr._joints || []);
    if (!joints || joints.length === 0) {
      console.log('[VRM] 弹簧骨骼列表为空');
      return;
    }

    // 检查碰撞体
    const colliderGroups = mgr.colliderGroups || mgr._colliderGroups || [];
    console.log(`[VRM] 碰撞体组数: ${colliderGroups.length}`);
    let count = 0;
    for (const j of joints) {
      const s = j.settings;
      if (!s) continue;
      // 增大阻尼 (默认 0.4 → 0.85)：减少震荡
      s.dragForce = Math.min(0.95, (s.dragForce ?? 0.4) * 2.0);
      // 保持刚度 (默认 1.0 → 0.85)：裙子/头发靠 stiffness 撑住形态，
      // 压太低（0.35）会让裙子失去刚性 → 乱飞
      s.stiffness = Math.max(0.8, Math.min(1.0, (s.stiffness ?? 1) * 0.85));
      // 降低重力 (减少下垂幅度)
      s.gravityPower = (s.gravityPower ?? 0) * 0.6;
      count++;
    }
    console.log(`[VRM] 弹簧骨骼调优完成: ${count} 个关节 (dragForce↑ stiffness↓ gravity↓)`);

    // 如果模型无碰撞体，添加基础碰撞球防止穿模
    if (colliderGroups.length === 0) {
      App.addBodyColliders(mgr);
    }
  };
  /* 为缺乏碰撞体的模型添加基础身体碰撞球 */
  App.addBodyColliders = function addBodyColliders(mgr) {
    if (!App.vrm.humanoid) return;
    const h = App.vrm.humanoid;
    // 在关键骨骼上添加球体碰撞器
    const colliderDefs = [{
      bone: 'head',
      offset: [0, 0, 0],
      radius: 0.09
    }, {
      bone: 'chest',
      offset: [0, 0, 0],
      radius: 0.12
    }, {
      bone: 'spine',
      offset: [0, 0, 0],
      radius: 0.10
    }, {
      bone: 'hips',
      offset: [0, 0, 0],
      radius: 0.12
    }, {
      bone: 'leftUpperArm',
      offset: [0, -0.1, 0],
      radius: 0.06
    }, {
      bone: 'rightUpperArm',
      offset: [0, -0.1, 0],
      radius: 0.06
    }];
    let added = 0;
    for (const def of colliderDefs) {
      const boneNode = h.getNormalizedBoneNode(def.bone);
      if (!boneNode) continue;
      try {
        // three-vrm v3 API: 创建碰撞体组
        const group = {
          colliders: [{
            position: new THREE.Vector3(def.offset[0], def.offset[1], def.offset[2]),
            radius: def.radius
          }],
          bones: new Set() // empty for now
        };
        // 尝试不同的 API 注册方式
        if (typeof mgr.addColliderGroup === 'function') {
          mgr.addColliderGroup(def.bone, group);
          added++;
        }
      } catch (e) {/* API 不兼容，跳过 */}
    }
    if (added > 0) {
      console.log(`[VRM] 添加了 ${added} 个身体碰撞球`);
    } else {
      console.log('[VRM] 碰撞体 API 不兼容，仅靠阻尼+平滑防穿模');
    }
  }; // VRM 放松站姿常量（normalized 空间：人形面向 -Z，+Y 上）
  // 左臂沿 -X 方向延伸 → 绕 Z 正向旋转使手臂下垂；右臂 +X → 负向。
  // 但 VRM1 模型的左臂在 +X 侧，符号相反，由加载时探测的 App.vrmArmRestSign 决定
  App.ARM_REST_Z = 1.35; // 加载后立即应用放松站姿（消除 T-pose）
  // 手臂旋转要按模型手性取符号：VRM0 左臂在 -X 侧（+1.35 即下垂），VRM1 左臂在 +X 侧（-1.35 才下垂）。
  // 偏移量一并乘符号，保证两个模型的摆动方向视觉一致。
  App.armZ = function armZ(offset) {
    return (App.ARM_REST_Z + (offset || 0)) * (App.vrmArmRestSign || 1);
  };
  App.applyVrmRestPose = function applyVrmRestPose() {
    const B = App.vrmBones;
    if (B.leftUpperArm) B.leftUpperArm.rotation.set(0, 0, App.armZ(0));
    if (B.rightUpperArm) B.rightUpperArm.rotation.set(0, 0, -App.armZ(0));
    if (B.leftLowerArm) B.leftLowerArm.rotation.set(-0.15, 0, 0);
    if (B.rightLowerArm) B.rightLowerArm.rotation.set(-0.15, 0, 0);
    if (B.leftHand) B.leftHand.rotation.set(0, 0, 0.1);
    if (B.rightHand) B.rightHand.rotation.set(0, 0, -0.1);
    // 让 VRM 立即更新一次
    if (App.vrm && App.vrm.humanoid) App.vrm.humanoid.update();
  };
  App.animateModel = function animateModel(t, dt, mouthOpen) {
    if (!App.modelGroup) return;

    // Mixamo 动作播放中：全身骨骼由 AnimationMixer 接管，跳过程序式骨骼写入，
    // 仅保留朝向相机 + 口型/眨眼表情 + VRM 更新（弹簧骨骼），避免两者打架
    if (App._mixamoActiveClip) {
      App.modelGroup.rotation.y = App.smoothRotY;
      App.modelGroup.rotation.x = App.smoothRotX;
      App.modelGroup.rotation.z = 0;
      if (App.modelType === 'vrm' && App.vrm && App.vrm.expressionManager) {
        const exp = App.vrm.expressionManager;
        // 口型同步（说话时由音频驱动）
        const mouthScale = App.vrmMouthScale || 0.6;
        const targetMouth = App.currentState === App.State.SPEAKING ? Math.min(0.7, mouthOpen * mouthScale) : (App.emotionMouth || 0);
        const lerpK = targetMouth > (App.smoothMouth || 0) ? 0.25 : 0.4;
        App.smoothMouth = App.lerp(App.smoothMouth || 0, targetMouth, lerpK);
        if (Math.abs(App.smoothMouth - targetMouth) < 0.001) App.smoothMouth = targetMouth;
        if (App.smoothMouth < 0.005) App.smoothMouth = 0;
        if (App.exprNames.mouth) exp.setValue(App.exprNames.mouth, App.smoothMouth);
        // 普通眨眼（Mixamo 播放期间不处理挤眼/双连眨等特殊眨眼）
        App.blinkTimer += dt;
        if (App.blinkTimer > App.nextBlinkAt) {
          App.blinkPhase = Math.min(1, (App.blinkPhase * App.blinkDuration + dt) / App.blinkDuration);
          const p = App.blinkPhase;
          const s = Math.abs(Math.cos(p * Math.PI));
          if (App.exprNames.blink) exp.setValue(App.exprNames.blink, s);
          if (p >= 1) {
            if (App.exprNames.blink) exp.setValue(App.exprNames.blink, 0);
            App.blinkPhase = 0;
            App.blinkTimer = 0;
            App.scheduleNextBlink();
            App.nextBlinkAt = App.blinkTimer + 2 + Math.random() * 4;
          }
        }
        App.updateExpressions(dt);
      }
      if (App.modelType === 'vrm' && App.vrm) App.vrm.update(dt);
      App.applyClickWobble(App.modelGroup);
      return;
    }

    const B = App.vrmBones;

    // 补外层声明（仅为通过类型检查，运行时取值不变）：原 stepsPerSegment 声明于
    // `if (App._playerIsGrounded)` 内层块、walkCyclePhase 声明于 `if (walkProgress <= 0)`
    // 内层块，而下方对二者的引用位于兄弟作用域（原 JS 依赖运行时短路、引用从未真正求值）；
    // 在此共同外层函数顶部声明，任何分支下运行时行为与原 JS 完全一致
    const stepsPerSegment: number = 1;
    const walkCyclePhase: number = 0;

    // 动作子系统更新（pose/walk/turn）
    App.updatePoseTimer(dt);
    App.updateWalkTimer(dt);
    App.updateTurnAction(dt);
    const blend = App.computePoseBlend(); // 当前姿态混合因子 0-1
    const po = App.ALL_POSES[App.currentPose] || App.ALL_POSES.rest; // 当前姿态偏移（统一姿态库）

    // 辅助：对姿态偏移做混合获取
    function poseVal(bone, axis, base) {
      const cur = po[bone] && po[bone][axis] !== undefined ? po[bone][axis] : 0;
      // 退出时用前一帧残留渐隐，进入时干净切入
      return base + cur * blend;
    }
    function poseValAxis(bone, axis) {
      return po[bone] && po[bone][axis] !== undefined ? po[bone][axis] : 0;
    }

    // 随机移动位置 + 行走弹跳
    let walkProgress = 0;
    let prevX = 0, prevZ = 0, newX = 0, newZ = 0;
    if (App.idleWalkTarget && App.idleWalkProgress < 1) {
      walkProgress = App.idleWalkProgress;
      // 线性插值（与游戏模式 _applyPlayerMovement 一致）：全程恒定速度移动，
      // 线速度 = 段长 × idleWalkSpeed（大厅 AI 行走 = AI_LOBBY_WALK_SPEED 自然步行 1.6）；
      // 不再使用 smoothstep（其峰值速度达 1.5× 均值，超过最大速度）；
      // 起步/停步的自然感由 applyFullBodyWalkAnimation 内部 ramp 统一处理，与游戏模式完全相同
      prevX = App.modelGroup.position.x;
      prevZ = App.modelGroup.position.z;
      newX = App.idleWalkStart.x + (App.idleWalkTarget.x - App.idleWalkStart.x) * walkProgress;
      newZ = App.idleWalkStart.z + (App.idleWalkTarget.z - App.idleWalkStart.z) * walkProgress;
      App.modelGroup.position.x = newX;
      App.modelGroup.position.z = newZ;

      // 统一的全身走路动画（游戏模式 + 非游戏模式共用）
      // 用 currentAction === WALK 而非 walkProgress > 0，避免段间进度归零时动画骤停
      const isInWalkAction = App.currentAction && App.currentAction.type === App.ActionType.WALK;
      const dx = newX - prevX;
      const dz = newZ - prevZ;
      if (isInWalkAction && (Math.abs(dx) > 0.0001 || Math.abs(dz) > 0.0001)) {
        // AI 驱动行走：步长与游戏内一致（动作幅度一致），避免大步长动画频率过高
        App.applyFullBodyWalkAnimation(dt, {
          x: dx, z: dz, isMoving: true,
          stepLength: App._aiDrivenWalk ? (App.AI_LOBBY_WALK_STEP_LENGTH || 0.8) : undefined,
        });
      }
    } else {
      // 未在走路动作中，通知共享动画系统复位
      // 否则每帧复位 _fullWalkRampT 会导致用户操控时 ramp 卡在低位，步频慢于 AI 自主移动
      if (!App.currentAction || App.currentAction.type !== App.ActionType.WALK) {
        App.applyFullBodyWalkAnimation(dt, { x: 0, z: 0, isMoving: false });
      }
    }

    // 松手后 dragOrbitYaw/dragOrbitPitch 保持不动，不自动回弹
    // 计算一次相机朝向，避免重复调用
    const camFaceY = App.computeBodyFaceCam(App.modelGroup);
    // 随机移动：行走时身体直接面向移动方向；停止后 smoothWalkFaceOff 过渡回看相机
    if (App.idleWalkTarget && App.idleWalkProgress < 1) {
      let rawFaceOff = App.walkFacingAngle - camFaceY;
      // 归一化到 [-PI, PI]
      while (rawFaceOff > Math.PI) rawFaceOff -= Math.PI * 2;
      while (rawFaceOff < -Math.PI) rawFaceOff += Math.PI * 2;
      App.smoothWalkFaceOff = App.lerp(App.smoothWalkFaceOff, rawFaceOff, 0.04);
    } else {
      App.smoothWalkFaceOff = App.lerp(App.smoothWalkFaceOff, 0, 0.05);
    }
    // 心有灵犀：保持面向用户的注视，不做额外的背向偏转
    if (App.mutualGaze && !App.wasMutualGaze) {
      App.wasMutualGaze = true;
    }
    if (!App.mutualGaze) App.wasMutualGaze = false;
    // VR 中朝向由 updateXRFaceUser 独占，微摆动归零，避免 smoothRotY 每帧被两个系统搅动
    const bodyMicroWobble = (App.mutualGaze || App.xrPresenting || App.xrMode === 'webxr') ? 0 : Math.sin(t * 0.35) * 0.008;
    // 行走时身体直接面向移动方向，避免相机角度变化导致漂移；停止后 smoothWalkFaceOff 自动过渡回看相机
    const isTurning = App.currentAction && App.currentAction.type === App.ActionType.TURN;
    // VR（WebXR）：App.camera.position 在 XR 会话中不随头显更新（冻结在进入时的站位），
    // 若仍拉向 camFaceY 会与 updateXRFaceUser（用真实头部位置）争夺 smoothRotY，
    // 造成角色朝向在两个目标间抖动。VR 中朝向完全交给 updateXRFaceUser 负责。
    const vrActive = App.xrPresenting || App.xrMode === 'webxr';
    const baseTargetY = isTurning ? App.smoothRotY : (vrActive ? App.smoothRotY : camFaceY + App.smoothWalkFaceOff);
    const targetY = isTurning ? App.smoothRotY : baseTargetY + App.gyroYaw * 0.15 + bodyMicroWobble;
    const targetX = App.gyroPitch * 0.15;
    // 自适应旋转平滑：心有灵犀 / 行走转身都要柔和自然
    // 归一化角度差到 [-PI, PI]，确保走最短路径（修复转身后不自动回看的问题）
    let rotYDiff = targetY - App.smoothRotY;
    while (rotYDiff > Math.PI) rotYDiff -= Math.PI * 2;
    while (rotYDiff < -Math.PI) rotYDiff += Math.PI * 2;
    const baseRate = App.mutualGaze ? 0.035 : 0.03;
    const rotYRate = baseRate + 0.04 / (1 + Math.abs(rotYDiff) * 4);
    if (isTurning) {
      // 转圈动作自行控制 rotation.y，不拉回相机方向
      App.smoothRotX = App.lerp(App.smoothRotX, targetX, 0.06);
    } else if (!vrActive && walkProgress > 0 && App.idleWalkTarget) {
      // 大厅行走：与游戏模式 _applyPlayerMovement 相同的朝向逻辑 ——
      // 面向每帧实际移动方向（atan2(dx, dz)），最短角度路径快速转向（8.0*dt），
      // 转向先于位移完成，角色绝不倒退/侧向行走
      // （VR：行走位置照常移动，但朝向完全交给 updateXRFaceUser 独占，避免两个系统争抢 smoothRotY）
      const walkDx = newX - prevX;
      const walkDz = newZ - prevZ;
      if (Math.abs(walkDx) > 0.0001 || Math.abs(walkDz) > 0.0001) {
        const moveAngle = Math.atan2(walkDx, walkDz);
        let walkDiff = moveAngle - App.smoothRotY;
        while (walkDiff > Math.PI) walkDiff -= Math.PI * 2;
        while (walkDiff < -Math.PI) walkDiff += Math.PI * 2;
        App.smoothRotY += walkDiff * Math.min(1, 8.0 * dt);
      }
      App.smoothRotX = App.lerp(App.smoothRotX, targetX, 0.06);
    } else if (App.fpvJustExited) {
      App.smoothRotY = targetY;
      App.smoothRotX = targetX;
      App.fpvJustExited = false;
    } else {
      App.smoothRotY += rotYDiff * rotYRate;
      App.smoothRotX = App.lerp(App.smoothRotX, targetX, 0.06);
    }
    App.modelGroup.rotation.y = App.smoothRotY;
    App.modelGroup.rotation.x = App.smoothRotX;
    // VR模式：头显左右晃动（摇头）→ 角色整体左右摆动（强度0-100，可叠加累积）
    App.modelGroup.rotation.z = 0;
    if (App.vrShake && App.xrMode === 'webxr' && App.vrShake.leftRight > 0) {
      const vrS = App.vrShake;
      const swayAmp = Math.min(0.3, 0.02 + vrS.leftRight * 0.005); // 强度5 → ±0.045，强度30 → ±0.17（上限±0.3）
      const swayFreq = 5; // 固定摆动速度（约 0.8Hz），不随强度变化
      App.modelGroup.rotation.z = Math.sin(t * swayFreq + 0.8) * swayAmp;
    }
    // 渐强因子 + 速度因子：提前到 walkBob 之前计算（原声明在函数后部，
    // walkBob 先引用 sf 会触发 TDZ 报错，导致行走时整个动画中断）
    if (walkProgress > 0) {
      // 不累加 _idleWalkRampT，旧的 ramp 保持 0
    } else {
      App._idleWalkRampT = 0;
    }
    const rampFactor = 0.35 + 0.65 * Math.min(1.0, (App._idleWalkRampT || 0) / 1.2);
    // 速度因子与游戏模式 applyFullBodyWalkAnimation 一致：min(2, max(0.5, 实际线速度/3.0))
    // 大厅 AI 行走线速度 = 段长 × idleWalkSpeed = 自然步行 1.6 → 因子 0.53（贴下限 0.5 保底步频）
    const linearSpeed = walkProgress > 0 && App.idleWalkTarget
      ? Math.hypot(App.idleWalkTarget.x - App.idleWalkStart.x, App.idleWalkTarget.z - App.idleWalkStart.z) * (App.idleWalkSpeed || 0)
      : 0;
    const walkSpeedFactor = walkProgress > 0 ? Math.min(2.0, Math.max(0.5, linearSpeed / 3.0)) : 1.0;
    const sf = walkSpeedFactor * rampFactor;

    {
      // 着地时叠加呼吸/行走弹跳，下落时由物理系统控制Y位置
      if (App._playerIsGrounded) {
        // AI 驱动行走步长 = AI_LOBBY_WALK_STEP_LENGTH（0.8），弹跳步频与步行速度匹配
        const stepLen = (App._aiDrivenWalk && App.AI_LOBBY_WALK_STEP_LENGTH) || App.WALK_STEP_LENGTH;
        const stepsPerSegment = walkProgress > 0 ? Math.max(1, Math.hypot(App.idleWalkTarget.x - App.idleWalkStart.x, App.idleWalkTarget.z - App.idleWalkStart.z) / stepLen) : 1;
        const walkBob = walkProgress > 0 ? Math.abs(Math.sin(walkProgress * Math.PI * 2 * stepsPerSegment)) * 0.125 * sf : 0;
        // VR模式：头显上下晃动（点头）→ 角色上下弹跳（强度0-100，可叠加累积）
        let vrBounceY = 0;
        if (App.vrShake && App.xrMode === 'webxr' && App.vrShake.upDown > 0) {
          const vrS = App.vrShake;
          const bounceAmp = Math.min(0.35, 0.02 + vrS.upDown * 0.006); // 强度4 → ±0.044，强度30 → ±0.2（上限±0.35）
          const bounceFreq = 5; // 固定弹跳速度（约 0.8Hz），不随强度变化
          vrBounceY = Math.sin(t * bounceFreq) * bounceAmp;
        }
        App.modelGroup.position.y = App._playerGroundY + Math.sin(t * 1.2) * 0.003 + walkBob + vrBounceY;
      }
    }

    // --- 脊椎 / 呼吸 + 姿态偏移 + idle 活力摆动 ---
    const energy = App.currentState === App.State.IDLE ? 0.6 + Math.sin(App.idleEnergy) * 0.4 : 1.0;
    const breathAmp = 0.008 + (App.currentState === App.State.IDLE ? 0.006 : 0);
    const bodySwayY = App.currentState === App.State.IDLE ? Math.sin(App.idleEnergy * 0.7) * 0.012 * energy : 0;
    const bodySwayZ = App.currentState === App.State.IDLE ? Math.cos(App.idleEnergy * 0.5) * 0.006 * energy : 0;
    if (B.spine) B.spine.rotation.x = App.lerp(B.spine.rotation.x, poseVal('spine', 'x', Math.sin(t * 1.2) * breathAmp), 0.08);
    if (B.spine) B.spine.rotation.y = App.lerp(B.spine.rotation.y, poseVal('spine', 'y', bodySwayY), 0.08);
    if (B.spine) B.spine.rotation.z = App.lerp(B.spine.rotation.z, poseVal('spine', 'z', bodySwayZ), 0.08);
    if (B.chest) B.chest.rotation.x = App.lerp(B.chest.rotation.x, Math.sin(t * 1.2) * (breathAmp * 0.75) + poseValAxis('chest', 'x') * blend, 0.08);
    if (B.upperChest) B.upperChest.rotation.x = App.lerp(B.upperChest.rotation.x, Math.sin(t * 1.2) * (breathAmp * 0.5), 0.08);

    // --- 颈部 + 姿态偏移 ---
    if (B.neck) {
      let nz = Math.sin(t * 0.5) * 0.015 + poseValAxis('neck', 'z') * blend;
      B.neck.rotation.z = App.lerp(B.neck.rotation.z, nz, 0.06);
    }

    // --- 头部 (看向相机 + 状态微调 + 姿态偏移) ---
    // 心有灵犀：轻微歪头注视，幅度小、过渡慢
    if (App.mutualGaze) App.gazeHeadTiltAcc = App.lerp(App.gazeHeadTiltAcc, 0.05, 0.025);else App.gazeHeadTiltAcc = App.lerp(App.gazeHeadTiltAcc, 0, 0.04);
    if (B.head) {
      const look = App.computeHeadLookAt(App.modelGroup);
      let tY,
        tX,
        tZ = 0;
      const headMicroAmp = App.mutualGaze ? 0.008 : 0.03;
      if (look) {
        tY = look.y + Math.sin(t * 0.4) * headMicroAmp;
        tX = look.x + Math.sin(t * 0.6) * headMicroAmp * 0.5 + App.gazeHeadTiltAcc;
      } else {
        tY = Math.sin(t * 0.4) * 0.05;
        tX = Math.sin(t * 0.6) * 0.025 + App.gazeHeadTiltAcc;
      }
      if (App.currentState === App.State.THINKING) {
        tX += -0.1;
        tY += 0.08;
      } else if (App.currentState === App.State.LISTENING) {
        tX += 0.05;
        tY += -0.04;
      } else if (App.currentState === App.State.SPEAKING) {
        tY += Math.sin(t * 2.5) * 0.04;
        tX += Math.sin(t * 1.8) * 0.015;
      }
      // 姿态偏移（混合因子控制）
      tX += poseValAxis('head', 'x') * blend;
      tY += poseValAxis('head', 'y') * blend;
      tZ = App.lerp(tZ, poseValAxis('head', 'z') * blend, 0.06);
      // 动作系统偏移（抬头/低头/转头/点头等）：并入本通道，与 lookAt/眼神统一钳制
      if (App.motionOffsets && App.motionOffsets.head) {
        tX += App.motionOffsets.head.x || 0;
        tY += App.motionOffsets.head.y || 0;
        tZ += App.motionOffsets.head.z || 0;
      }
      // 眼神控制偏移（视线目标：看左/看右/抬头/低头/移开/害羞等）
      if (App.getGazeOffsets) {
        const gazeOff = App.getGazeOffsets();
        tX += gazeOff.x;
        tY += gazeOff.y;
      }
      // 防 NaN：任何上游异常数值按 0 处理，杜绝骨骼矩阵被污染导致的"打转/无法复位"
      if (!Number.isFinite(tX)) tX = 0;
      if (!Number.isFinite(tY)) tY = 0;
      if (!Number.isFinite(tZ)) tZ = 0;
      // 头部总旋转硬钳制：水平 ±0.35（≈20°）、俯仰 -0.22/+0.20（≈12.6°/11.5°）
      // 姿态/眼神/动作全部并入后才钳制 → 头部永远转不过头（防穿模、可复位）
      tY = THREE.MathUtils.clamp(tY, -0.35, 0.35);
      tX = THREE.MathUtils.clamp(tX, -0.22, 0.20);
      tZ = THREE.MathUtils.clamp(tZ, -0.12, 0.12);
      // 慢速平滑写入（0.045/帧 ≈ 约 1.1 秒到位）：头部动作更优雅，避免"甩头/生硬"
      B.head.rotation.y = App.lerp(B.head.rotation.y, tY, 0.045);
      B.head.rotation.x = App.lerp(B.head.rotation.x, tX, 0.045);
      if (Math.abs(tZ) > 0.001) B.head.rotation.z = App.lerp(B.head.rotation.z, tZ, 0.045);
    }
    // 颈部动作偏移（低头/抬头/转头时脖子自然跟随，独立平滑写入；避免与头部叠加超限）
    if (B.neck && App.motionOffsets && App.motionOffsets.neck) {
      const no = App.motionOffsets.neck;
      B.neck.rotation.y = App.lerp(B.neck.rotation.y || 0, Number.isFinite(no.y) ? no.y : 0, 0.045);
      B.neck.rotation.x = App.lerp(B.neck.rotation.x || 0, Number.isFinite(no.x) ? no.x : 0, 0.045);
    }

    // --- 行走时骨骼动画由 applyFullBodyWalkAnimation 完全接管 ---
    // 避免旧代码的 lerp 与共享函数的 lerp 在同一骨骼上冲突
    if (walkProgress <= 0) {
    // --- 行走摆臂 + idle 微摆 ——
    const walkCyclePhase = walkProgress > 0 ? walkProgress * Math.PI * 2 * stepsPerSegment * rampFactor : 0;

    const ARM_SWING_AMP = 0.50 * sf; // 前后摆臂幅度（rad），保持优雅
    // 左臂与左腿反相，右臂与左臂反相
    const leftArmSwing = walkProgress > 0 ? -Math.sin(walkCyclePhase) * ARM_SWING_AMP : 0;
    const rightArmSwing = walkProgress > 0 ? Math.sin(walkCyclePhase) * ARM_SWING_AMP : 0;
    const idleArmSwing = App.currentState === App.State.IDLE && walkProgress <= 0 ? Math.sin(App.idleEnergy * 0.9) * 0.035 * energy : 0;
    if (B.leftUpperArm) {
      // 摆臂主要用 rotation.x（前后方向），z 保持 rest pose 只做呼吸微动
      let tz = App.armZ(
        Math.sin(t * 1.2) * 0.012 + idleArmSwing
        + poseValAxis('leftUpperArm', 'z') * blend
        + (App.currentState === App.State.SPEAKING ? Math.sin(t * 2.5) * 0.015 : 0)
      );
      let tx = Math.sin(t * 1.2 + 0.3) * 0.015 + Math.sin(t * 0.7) * 0.008 * energy + leftArmSwing;
      tx += poseValAxis('leftUpperArm', 'x') * blend;
      if (App.currentState === App.State.SPEAKING) {
        tx += Math.sin(t * 3) * 0.04;
      }
      B.leftUpperArm.rotation.z = App.lerp(B.leftUpperArm.rotation.z, tz, 0.07);
      B.leftUpperArm.rotation.x = App.lerp(B.leftUpperArm.rotation.x, tx, 0.07);
    }
    if (B.leftLowerArm) {
      // 肘部弯曲已取消
      let elbowBend = 0;
      let tx = -0.15 + Math.sin(t * 1.2) * 0.015 + elbowBend + idleArmSwing * 0.5;
      tx += poseValAxis('leftLowerArm', 'x') * blend;
      if (App.currentState === App.State.SPEAKING) tx += Math.sin(t * 3 + 0.5) * 0.03;
      B.leftLowerArm.rotation.x = App.lerp(B.leftLowerArm.rotation.x, tx, 0.07);
    }

    // --- 右臂 + 姿态 + 行走摆臂（与左臂反相） + idle 活力微摆 ---
    if (B.rightUpperArm) {
      let tz = -App.armZ(
        -Math.sin(t * 1.2 + 0.5) * 0.012 + idleArmSwing
        - poseValAxis('rightUpperArm', 'z') * blend
        + (App.currentState === App.State.SPEAKING ? Math.sin(t * 2.5) * 0.015 : 0)
      );
      let tx = Math.sin(t * 1.2) * 0.015 + Math.sin(t * 0.65) * 0.008 * energy + rightArmSwing;
      tx += poseValAxis('rightUpperArm', 'x') * blend;
      if (App.currentState === App.State.SPEAKING) {
        tx += -Math.sin(t * 3) * 0.04;
      }
      B.rightUpperArm.rotation.z = App.lerp(B.rightUpperArm.rotation.z, tz, 0.07);
      B.rightUpperArm.rotation.x = App.lerp(B.rightUpperArm.rotation.x, tx, 0.07);
    }
    if (B.rightLowerArm) {
      let elbowBend = 0;
      let tx = -0.15 + Math.sin(t * 1.2) * 0.015 + elbowBend - idleArmSwing * 0.5;
      tx += poseValAxis('rightLowerArm', 'x') * blend;
      B.rightLowerArm.rotation.x = App.lerp(B.rightLowerArm.rotation.x, tx, 0.07);
    }

    // --- 手部 (姿态驱动 + 呼吸微动) ---
    // 姿态可同时控制 rotation.x(屈伸) / rotation.y(内外转) / rotation.z(旋转)
    if (B.leftHand) {
      let tx = Math.sin(t * 1.3) * 0.015 + poseValAxis('leftHand', 'x') * blend;
      let ty = Math.sin(t * 0.7) * 0.01 + poseValAxis('leftHand', 'y') * blend;
      let tz = Math.sin(t * 1.5) * 0.025 + poseValAxis('leftHand', 'z') * blend;
      B.leftHand.rotation.x = App.lerp(B.leftHand.rotation.x, tx, 0.08);
      B.leftHand.rotation.y = App.lerp(B.leftHand.rotation.y, ty, 0.08);
      B.leftHand.rotation.z = App.lerp(B.leftHand.rotation.z, tz, 0.08);
    }
    if (B.rightHand) {
      let tx = Math.sin(t * 1.3 + 0.5) * 0.015 + poseValAxis('rightHand', 'x') * blend;
      let ty = Math.sin(t * 0.7) * 0.01 + poseValAxis('rightHand', 'y') * blend;
      let tz = -Math.sin(t * 1.5) * 0.025 + poseValAxis('rightHand', 'z') * blend;
      B.rightHand.rotation.x = App.lerp(B.rightHand.rotation.x, tx, 0.08);
      B.rightHand.rotation.y = App.lerp(B.rightHand.rotation.y, ty, 0.08);
      B.rightHand.rotation.z = App.lerp(B.rightHand.rotation.z, tz, 0.08);
    }

    // --- 髋部 (行走时轻微摇摆 + 前倾) ---
    const walkHipSway = walkProgress > 0 ? Math.sin(walkCyclePhase) * 0.050 * sf : 0;
    const walkHipForward = walkProgress > 0 ? Math.cos(walkCyclePhase) * 0.040 * sf : 0;
    if (B.hips) {
      let hy = Math.sin(t * 0.35) * 0.01 + walkHipSway;
      B.hips.rotation.y = App.lerp(B.hips.rotation.y, hy, 0.06);
      // 髋部侧移（重心左右转移）
      if (B.hips.position) {
        let hx = walkProgress > 0 ? Math.cos(walkCyclePhase) * 0.025 * sf : 0;
        B.hips.position.x = App.lerp(B.hips.position.x || 0, hx, 0.06);
      }
      let hz = walkHipForward + Math.sin(t * 1.2) * 0.004;
      // 髋部 rotation.x = 前后倾斜
      B.hips.rotation.x = App.lerp(B.hips.rotation.x || 0, hz, 0.06);
    }

    // --- 行走时躯干轻微反旋（平衡步态） ---
    const walkTorsoCounter = walkProgress > 0 ? -Math.cos(walkCyclePhase) * 0.040 * sf : 0;
    if (B.spine) B.spine.rotation.y += walkTorsoCounter;
    } // end if (walkProgress <= 0) —— 行走骨骼动画守卫

    // ==================== 行走腿部动画 ====================
    // 行走时由 applyFullBodyWalkAnimation 接管，非行走时归零
    if (walkProgress <= 0) {
    const walkActive = walkProgress > 0 && walkProgress < 1;
    const LEG_SWING_AMP = 0.60 * sf; // 大腿根部，最大两脚夹角90°（1.5倍）
      const KNEE_BEND_AMP = 0; // 膝盖弯曲已取消
    const FOOT_LIFT_AMP = 0.20 * sf * sf; // 脚尖抬起同频

    // 左腿
    if (B.leftUpperLeg) {
      let lx = walkActive ? Math.sin(walkCyclePhase) * LEG_SWING_AMP : 0;
      lx += poseValAxis('leftUpperLeg', 'x') * blend;
      B.leftUpperLeg.rotation.x = App.lerp(B.leftUpperLeg.rotation.x, lx, 0.08);
    }
    if (B.leftLowerLeg) {
      // 膝盖弯曲已取消
      const leftSwingBend = 0;
      let lx = leftSwingBend + poseValAxis('leftLowerLeg', 'x') * blend;
      B.leftLowerLeg.rotation.x = App.lerp(B.leftLowerLeg.rotation.x, lx, 0.08);
    }
    if (B.leftFoot) {
      // 脚掌：左腿摆动期抬脚尖，支撑期放平
      const leftFootLift = walkActive ? Math.max(0, Math.sin(walkCyclePhase)) * FOOT_LIFT_AMP : 0;
      B.leftFoot.rotation.x = App.lerp(B.leftFoot.rotation.x || 0, leftFootLift, 0.08);
    }

    // 右腿（相位 +π）
    if (B.rightUpperLeg) {
      let rx = walkActive ? Math.sin(walkCyclePhase + Math.PI) * LEG_SWING_AMP : 0;
      rx += poseValAxis('rightUpperLeg', 'x') * blend;
      B.rightUpperLeg.rotation.x = App.lerp(B.rightUpperLeg.rotation.x, rx, 0.08);
    }
    if (B.rightLowerLeg) {
      // 膝盖弯曲已取消
      const rightSwingBend = 0;
      let rx = rightSwingBend + poseValAxis('rightLowerLeg', 'x') * blend;
      B.rightLowerLeg.rotation.x = App.lerp(B.rightLowerLeg.rotation.x, rx, 0.08);
    }
    if (B.rightFoot) {
      // 脚掌：右腿摆动期抬脚尖，支撑期放平
      const rightFootLift = walkActive ? Math.max(0, Math.sin(walkCyclePhase + Math.PI)) * FOOT_LIFT_AMP : 0;
      B.rightFoot.rotation.x = App.lerp(B.rightFoot.rotation.x || 0, rightFootLift, 0.08);
    }
    }

    // --- VRM 表情 + 口型同步 ---
    if (App.modelType === 'vrm' && App.vrm && App.vrm.expressionManager) {
      const exp = App.vrm.expressionManager;

      // 口型（说话时由音频驱动，mouthOpen 范围 0~1；非说话时可播放情绪嘴型）
      // 不同 VRM 模型对 aa 表情敏感度不同，用 mouthScale 可适配
      const mouthScale = App.vrmMouthScale || 0.6;
      const targetMouth = App.currentState === App.State.SPEAKING ? Math.min(0.7, mouthOpen * mouthScale) : (App.emotionMouth || 0);
      // 快速衰减：张嘴慢一点，闭嘴快（确保字间闭合）
      const lerpK = targetMouth > (App.smoothMouth || 0) ? 0.25 : 0.4;
      App.smoothMouth = App.lerp(App.smoothMouth || 0, targetMouth, lerpK);
      if (Math.abs(App.smoothMouth - targetMouth) < 0.001) App.smoothMouth = targetMouth;
      if (App.smoothMouth < 0.005) App.smoothMouth = 0;
      if (App.exprNames.mouth) exp.setValue(App.exprNames.mouth, App.smoothMouth);

      // 调试：每2秒打印一次口型数据，确认分析器工作状态
      App._mouthLogTimer = (App._mouthLogTimer || 0) + dt;
      if (App.currentState === App.State.SPEAKING && App._mouthLogTimer > 2) {
        App._mouthLogTimer = 0;
        let rawVal = 0;
        if (App.analyserData && App.analyser) {
          App.analyser.getByteTimeDomainData(App.analyserData);
          let s = 0;
          for (let i = 0; i < App.analyserData.length; i++) { const v = (App.analyserData[i] - 128) / 128; s += v * v; }
          rawVal = Math.sqrt(s / App.analyserData.length);
        }
        console.log('[Mouth] raw=', rawVal.toFixed(4), 'mouthOpen=', mouthOpen.toFixed(3), 'smoothMouth=', App.smoothMouth.toFixed(3), 'hasAnalyser=', !!(App.analyserData && App.analyser));
      }

      // 眨眼（增强：普通/慢眨/双连眨/挤眼/全闭眼）
      // 挤眼和全闭眼由 updateIdleExpression 触发时设置 blinkType，这里统一处理
      App.blinkTimer += dt;
      // 挤眼/全闭眼优先——它们由 winkTimer/closeHoldTimer 触发，需要打断普通眨眼
      const isWink = (App.blinkType === 'winkLeft' || App.blinkType === 'winkRight');
      const isCloseHold = (App.blinkType === 'closeHold');
      // 眨眼抑制（惊讶瞪大眼/含情注视时暂停普通眨眼），挤眼类不受影响
      const blinkBlocked = !!(App.blinkSuppressed && App.blinkSuppressed()) && !isWink && !isCloseHold;
      if (!blinkBlocked && (isWink || isCloseHold || App.blinkTimer > App.nextBlinkAt)) {
        const duration = App.blinkDuration;
        let elapsed = App.blinkPhase * duration;
        // 如果是挤眼/全闭眼首次触发，重置计时
        if ((isWink || isCloseHold) && App.blinkPhase === 0) {
          App.blinkTimer = App.nextBlinkAt; // 对齐普通眨眼计时，防止立即又触发普通眨眼
        }
        elapsed += dt;
        App.blinkPhase = Math.min(1, elapsed / duration);
        const p = App.blinkPhase;

        if (App.blinkType === 'double') {
          // 双连眨：闭→开→闭→开，两段完整的余弦波
          const t = p * 2; // 映射到 0~2
          let s = 0;
          if (t < 1) s = Math.abs(Math.cos(t * Math.PI)); // 第一段：0→闭→开
          else if (t < 2) s = Math.abs(Math.cos((t - 0.15) * Math.PI)); // 第二段：稍延迟再闭→开
          s = Math.max(0, Math.min(1, s));
          if (App.exprNames.blink) exp.setValue(App.exprNames.blink, s);
        } else if (App.blinkType === 'winkLeft') {
          // 左眼挤眼：仅左眼闭合，右眼保持睁开
          const s = Math.abs(Math.cos(p * Math.PI));
          if (App.exprNames.blinkLeft) exp.setValue(App.exprNames.blinkLeft, s);
          else if (App.exprNames.blink) exp.setValue(App.exprNames.blink, s);
        } else if (App.blinkType === 'winkRight') {
          // 右眼挤眼
          const s = Math.abs(Math.cos(p * Math.PI));
          if (App.exprNames.blinkRight) exp.setValue(App.exprNames.blinkRight, s);
          else if (App.exprNames.blink) exp.setValue(App.exprNames.blink, s);
        } else {
          // normal / slow / closeHold: 统一的单次睁闭
          const s = Math.abs(Math.cos(p * Math.PI));
          if (App.exprNames.blink) {
            exp.setValue(App.exprNames.blink, s);
          } else {
            // 回退：分别驱动左眼/右眼
            if (App.exprNames.blinkLeft) exp.setValue(App.exprNames.blinkLeft, s);
            if (App.exprNames.blinkRight) exp.setValue(App.exprNames.blinkRight, s);
          }
        }

        // 眨眼结束
        if (p >= 1) {
          // 清除所有眼睛表达式
          if (App.exprNames.blink) exp.setValue(App.exprNames.blink, 0);
          if (App.exprNames.blinkLeft) exp.setValue(App.exprNames.blinkLeft, 0);
          if (App.exprNames.blinkRight) exp.setValue(App.exprNames.blinkRight, 0);
          App.blinkPhase = 0;
          App.blinkTimer = 0;
          App.scheduleNextBlink();
          App.nextBlinkAt = App.blinkTimer + 2 + Math.random() * 4;
        }
      }

      // 丰富表情（根据状态自动平滑切换）
      App.updateExpressions(dt);
    } else if (App.modelType === 'gltf' && App.morphTargets.length > 0) {
      const v = App.currentState === App.State.SPEAKING ? mouthOpen : 0;
      for (const mt of App.morphTargets) {
        mt.mesh.morphTargetInfluences[mt.index] = v;
      }
    }

    // vrm.update 更新 humanoid + expressionManager + springBone
    // 跳舞动作覆盖：在姿态应用之后直接控制骨骼（不由姿态系统干扰）
    App.updateDanceAction(dt);
    // 表情动作引擎：在姿态/行走/跳舞之上叠加微动作偏移（转头/低头/弯腰/挥手/耸肩等）
    if (App.motionOffsets) {
      for (const bn in App.motionOffsets) {
        const b = B[bn];
        const off = App.motionOffsets[bn];
        if (!b || !off) continue;
        // head/neck 已在头部段处理（并入钳制通道 + 平滑写入），跳过避免二次叠加
        if (bn === 'head' || bn === 'neck') continue;
        if (off.x) b.rotation.x += off.x;
        if (off.y) b.rotation.y += off.y;
        if (off.z) b.rotation.z += off.z;
      }
    }
    if (App.modelType === 'vrm' && App.vrm) {
      App.vrm.update(dt);
    }
    // 点击摇摆
    App.applyClickWobble(App.modelGroup);
  };
  /* ============================================================
   *  状态切换
   * ============================================================ */
  /* ============================================================
   *  低功耗模式
   * ============================================================ */
});
