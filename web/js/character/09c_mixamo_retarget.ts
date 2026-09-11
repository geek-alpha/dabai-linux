import * as THREE from 'three';
import { FBXLoader } from 'three/addons/loaders/FBXLoader.js';
import type { AppKernel, MixamoClipInfo } from '../types/app-kernel.js';

export default (function init(App: AppKernel) {
  /* ============================================================
   *  Mixamo 重定向管线 —— FBX 动作 → VRM 可用 AnimationClip
   *
   *  定位：数据层。把 Mixamo 免费动作库（FBX）重定向为 three-vrm
   *  可直接播放的 AnimationClip，按情绪标签建库索引。
   *  - 骨骼映射：mixamoVRMRigMap（Mixamo 命名 → VRM humanoid 命名）
   *  - 坐标/姿态校正：以骨骼 rest 世界旋转为基准做四元数变换，
   *    消除 Mixamo 与 VRM 的 T/A-pose 与朝向差异
   *  - 比例缩放：按 hips 高度比缩放位移轨道，适配不同体型
   *  - VRM 0.x 兼容：x/z 轴取反（本项目为 VRM 1.0，不触发）
   *
   *  用法：App.loadMixamoAnimation('/anim/walk.fbx', 'walk')
   *        → App.playMixamoClip('walk')
   *        → 渲染循环内 App.updateMixamoMixer(dt)
   * ============================================================ */

  // Mixamo 骨骼名 → VRM humanoid 骨骼名
  const mixamoVRMRigMap: Record<string, string> = {
    mixamorigHips: 'hips',
    mixamorigSpine: 'spine',
    mixamorigSpine1: 'chest',
    mixamorigSpine2: 'upperChest',
    mixamorigNeck: 'neck',
    mixamorigHead: 'head',
    mixamorigLeftShoulder: 'leftShoulder',
    mixamorigLeftArm: 'leftUpperArm',
    mixamorigLeftForeArm: 'leftLowerArm',
    mixamorigLeftHand: 'leftHand',
    mixamorigLeftHandThumb1: 'leftThumbMetacarpal',
    mixamorigLeftHandThumb2: 'leftThumbProximal',
    mixamorigLeftHandThumb3: 'leftThumbDistal',
    mixamorigLeftHandIndex1: 'leftIndexProximal',
    mixamorigLeftHandIndex2: 'leftIndexIntermediate',
    mixamorigLeftHandIndex3: 'leftIndexDistal',
    mixamorigLeftHandMiddle1: 'leftMiddleProximal',
    mixamorigLeftHandMiddle2: 'leftMiddleIntermediate',
    mixamorigLeftHandMiddle3: 'leftMiddleDistal',
    mixamorigLeftHandRing1: 'leftRingProximal',
    mixamorigLeftHandRing2: 'leftRingIntermediate',
    mixamorigLeftHandRing3: 'leftRingDistal',
    mixamorigLeftHandPinky1: 'leftLittleProximal',
    mixamorigLeftHandPinky2: 'leftLittleIntermediate',
    mixamorigLeftHandPinky3: 'leftLittleDistal',
    mixamorigRightShoulder: 'rightShoulder',
    mixamorigRightArm: 'rightUpperArm',
    mixamorigRightForeArm: 'rightLowerArm',
    mixamorigRightHand: 'rightHand',
    mixamorigRightHandThumb1: 'rightThumbMetacarpal',
    mixamorigRightHandThumb2: 'rightThumbProximal',
    mixamorigRightHandThumb3: 'rightThumbDistal',
    mixamorigRightHandIndex1: 'rightIndexProximal',
    mixamorigRightHandIndex2: 'rightIndexIntermediate',
    mixamorigRightHandIndex3: 'rightIndexDistal',
    mixamorigRightHandMiddle1: 'rightMiddleProximal',
    mixamorigRightHandMiddle2: 'rightMiddleIntermediate',
    mixamorigRightHandMiddle3: 'rightMiddleDistal',
    mixamorigRightHandRing1: 'rightRingProximal',
    mixamorigRightHandRing2: 'rightRingIntermediate',
    mixamorigRightHandRing3: 'rightRingDistal',
    mixamorigRightHandPinky1: 'rightLittleProximal',
    mixamorigRightHandPinky2: 'rightLittleIntermediate',
    mixamorigRightHandPinky3: 'rightLittleDistal',
    mixamorigLeftUpLeg: 'leftUpperLeg',
    mixamorigLeftLeg: 'leftLowerLeg',
    mixamorigLeftFoot: 'leftFoot',
    mixamorigLeftToeBase: 'leftToes',
    mixamorigRightUpLeg: 'rightUpperLeg',
    mixamorigRightLeg: 'rightLowerLeg',
    mixamorigRightFoot: 'rightFoot',
    mixamorigRightToeBase: 'rightToes'
  };

  // ==================== 运行时状态 ====================
  App.mixamoClips = {} as Record<string, MixamoClipInfo>;
  App.mixamoMixer = null;
  App._mixamoActiveClip = null;
  App._mixamoActiveAction = null; // 当前正在播放的 AnimationAction（供 stop 直接停）
  // —— 取消动作混合：动作间直接切换，不再做 crossfade/淡入淡出/尾收回落 ——
  App.ANIM_PLAY_PARAMS = {
    blend: 0,       // 动作间交叠过渡 → 0：直接硬切
    start: 0,       // 冷启动淡入 → 0：直接进场
    tail: 0,        // 单次动作尾收回落 → 0：演完立即释放
    stopFade: 0     // 主动 stop 淡出 → 0：直接停
  };
  App._mixamoTailActive = false;   // 尾收进行中（保留字段，恒 false）
  App._mixamoTailName = null;      // 正在"尾收回落"的动作名（保留字段，恒 null）
  App._mixamoTailRem = 0;          // 尾收回落剩余秒数（保留字段，恒 0）
  App._mixamoTailTotal = 0;        // 本次尾收回落总时长（保留字段，恒 0）

  // ==================== hips 位移治理：动作一律原位播放 ====================
  // 规则：除非动作明确带速度（行走系统 WALK_SPEED / AI_LOBBY_WALK_SPEED /
  // 游戏移动速度），任何动作都不得搬动角色位置。Mixamo FBX 自带 hips 根位移
  // 轨道（走/跑/舞/惊吓后退等），直接播放会在动作期间扯着身体滑行，停止后
  // hips 位移残留，下一个动作开局又被 mixer 硬拉回自己的首帧 → 位置乱跳、
  // 前后不协调。治理：重定向时丢弃 hips 位移轨道；播放/停止/播完时把 hips
  // 复位到本模型的静息位（双保险）。
  App._mixamoHipsRestPos = null; // 当前模型 normalized hips 静息位置（首次加载片段时捕获）
  App.captureMixamoHipsRest = function captureMixamoHipsRest() {
    try {
      const hips = App.vrm?.humanoid?.getNormalizedBoneNode('hips');
      if (hips && hips.position) {
        App._mixamoHipsRestPos = { x: hips.position.x, y: hips.position.y, z: hips.position.z };
        return true;
      }
    } catch (e) { /* 非 VRM 模型无 hips，忽略 */ }
    return false;
  };
  App.resetMixamoHips = function resetMixamoHips() {
    try {
      const hips = App.vrm?.humanoid?.getNormalizedBoneNode('hips');
      if (!hips || !hips.position) return;
      const rest = App._mixamoHipsRestPos;
      if (rest) hips.position.set(rest.x, rest.y, rest.z);
      else hips.position.set(0, 0, 0);
    } catch (e) { /* 忽略 */ }
  };

  // ==================== 核心重定向 ====================
  function retargetAnimation(fbxAsset: any, vrm: any): THREE.AnimationClip | null {
    const clip = THREE.AnimationClip.findByName(fbxAsset.animations, 'mixamo.com')
      || (fbxAsset.animations && fbxAsset.animations[0]);
    if (!clip) return null;

    const tracks: THREE.KeyframeTrack[] = [];
    const restRotationInverse = new THREE.Quaternion();
    const parentRestWorldRotation = new THREE.Quaternion();
    const _quatA = new THREE.Quaternion();
    const _vec3 = new THREE.Vector3();

    // 以 hips 高度为基准做位移比例缩放
    const motionHipsHeight = fbxAsset.getObjectByName('mixamorigHips')?.position.y;
    const vrmHipsY = vrm.humanoid?.getNormalizedBoneNode('hips')?.getWorldPosition(_vec3).y;
    const vrmRootY = vrm.scene.getWorldPosition(_vec3).y;
    if (!vrmHipsY || !motionHipsHeight) {
      console.warn('[Mixamo] 无法计算 hips 高度，跳过比例缩放');
      return null;
    }
    const vrmHipsHeight = Math.abs(vrmHipsY - vrmRootY);
    const hipsPositionScale = vrmHipsHeight / motionHipsHeight;

    clip.tracks.forEach((track: any) => {
      const trackSplitted = track.name.split('.');
      const mixamoRigName = trackSplitted[0];
      const vrmBoneName = mixamoVRMRigMap[mixamoRigName];
      const vrmNodeName = vrm.humanoid?.getNormalizedBoneNode(vrmBoneName)?.name;
      const mixamoRigNode = fbxAsset.getObjectByName(mixamoRigName);
      if (vrmNodeName == null) return;

      const propertyName = trackSplitted[1];
      // 记录 rest 姿态世界旋转（消除 Mixamo 与 VRM 的 rest 差异）
      mixamoRigNode?.getWorldQuaternion(restRotationInverse).invert();
      mixamoRigNode?.parent?.getWorldQuaternion(parentRestWorldRotation);

      if (track instanceof THREE.QuaternionKeyframeTrack) {
        // 旋转重定向：q' = parentRestWorldRot * q * restWorldRot⁻¹
        for (let i = 0; i < track.values.length; i += 4) {
          const flatQuaternion = track.values.slice(i, i + 4);
          _quatA.fromArray(flatQuaternion);
          _quatA.premultiply(parentRestWorldRotation).multiply(restRotationInverse);
          _quatA.toArray(flatQuaternion);
          flatQuaternion.forEach((v: number, index: number) => { track.values[index + i] = v; });
        }
        tracks.push(new THREE.QuaternionKeyframeTrack(
          `${vrmNodeName}.${propertyName}`,
          track.times,
          track.values.map((v: number, i: number) =>
            vrm.meta?.metaVersion === '0' && i % 2 === 0 ? -v : v
          )
        ));
      } else if (track instanceof THREE.VectorKeyframeTrack) {
        // ===== 原位播放治理：动作一律不搬动身体位置 =====
        // Mixamo FBX 的 hips 位移轨道 = 烘焙根位移 + 重心高度，直接播放会让
        // 待机/舞蹈/情绪动作扯着身体滑行/漂移，停下来后 hips 位移残留、
        // 下一个动作开局又被 mixer 硬拉回自己的首帧 → 位置乱跳、前后不协调。
        // 位置只允许由带显式速度的行走系统驱动（WALK_SPEED / AI_LOBBY_WALK_SPEED /
        // 游戏移动速度），因此这里丢弃 hips 位移轨道，其余骨骼位移照旧重定向。
        if (vrmBoneName === 'hips') return;
        // 位移重定向：按 hips 高度比缩放（VRM 0.x 取反 x/z）
        const value = track.values.map((v: number, i: number) =>
          (vrm.meta?.metaVersion === '0' && i % 3 !== 1 ? -v : v) * hipsPositionScale
        );
        tracks.push(new THREE.VectorKeyframeTrack(
          `${vrmNodeName}.${propertyName}`,
          track.times,
          value
        ));
      }
    });

    return new THREE.AnimationClip('vrmAnimation', clip.duration, tracks);
  }

  // ==================== 对外 API ====================
  /** 加载 Mixamo FBX 并重定向为 VRM 可用片段，注册到动作库 */
  App.loadMixamoAnimation = async function loadMixamoAnimation(fbxUrl: string, clipName?: string) {
    if (!App.vrm || !App.vrm.humanoid) {
      console.warn('[Mixamo] VRM 未加载，无法重定向');
      return null;
    }
    // 捕获当前模型的 hips 静息位（供播放/停止时复位，配合“原位播放”规则）
    if (!App._mixamoHipsRestPos) App.captureMixamoHipsRest();
    try {
      const loader = new FBXLoader();
      const fbxAsset = await loader.loadAsync(fbxUrl);
      const clip = retargetAnimation(fbxAsset, App.vrm);
      if (!clip) return null;
      const name = clipName || 'mixamo';
      App.mixamoClips[name] = { name, clip };
      // 预绑定 AnimationAction：首次播放某片段时 AnimationMixer.clipAction 要
      // 逐轨道解析绑定（几十条轨道），若正好发生在 VR 动作切换/情绪触发的瞬间
      // 会卡顿。加载阶段（非 VR）预先建好 action 缓存，播放时直接命中零开销。
      try {
        if (!App.mixamoMixer) App.mixamoMixer = new THREE.AnimationMixer(App.vrm.scene);
        App.mixamoMixer.clipAction(clip);
      } catch (e) { /* 预绑定失败不影响注册 */ }
      console.log(`[Mixamo] 已注册动作: ${name} (${clip.duration.toFixed(2)}s, ${clip.tracks.length} 轨道)`);
      return clip;
    } catch (e) {
      console.error('[Mixamo] 加载失败:', fbxUrl, e);
      return null;
    }
  };

  /**
   * 加载离线烘焙的 Mixamo 动作缓存（bake_animations.mjs 产物）
   * 反序列化 JSON → AnimationClip，跳过 FBX 解析与骨骼重定向（秒开）。
   * 轨道名存的是 humanoid 名（如 hips.quaternion），加载时映射到
   * 当前模型 normalized 骨骼的实际节点名（Normalized_<原始骨骼名>）。
   * 位移轨道按当前模型 hips 高度比缩放（与 retargetAnimation 一致）。
   */
  App.loadBakedMixamoClip = async function loadBakedMixamoClip(name: string, bakedUrl: string) {
    if (!App.vrm || !App.vrm.humanoid) {
      console.warn('[Mixamo] VRM 未加载，无法加载烘焙动作');
      return null;
    }
    // 捕获当前模型的 hips 静息位（供播放/停止时复位，与 loadMixamoAnimation 一致；
    // 漏掉会导致 resetMixamoHips 把 hips 清零 → 角色下沉穿地板）
    if (!App._mixamoHipsRestPos) App.captureMixamoHipsRest();
    try {
      const res = await fetch(bakedUrl);
      if (!res.ok) return null; // 无烘焙缓存 → 调用方回退 FBX
      const data = await res.json();
      if (!data.tracks || data.tracks.length === 0) return null;

      // 位移比例缩放：以当前模型 hips 高度为基准
      const _vec3 = new THREE.Vector3();
      const vrmHipsY = App.vrm.humanoid.getNormalizedBoneNode('hips')?.getWorldPosition(_vec3).y;
      const vrmRootY = App.vrm.scene.getWorldPosition(_vec3).y;
      const vrmHipsHeight = Math.abs(vrmHipsY - vrmRootY);
      const hipsPositionScale = data.motionHipsHeight ? vrmHipsHeight / data.motionHipsHeight : 1;
      const vrm0 = App.vrm.meta?.metaVersion === '0';

      const tracks: THREE.KeyframeTrack[] = [];
      for (const t of data.tracks) {
        const dot = t.name.indexOf('.');
        if (dot < 0) continue;
        const humanoidName = t.name.slice(0, dot);
        const propertyName = t.name.slice(dot + 1);
        // humanoid 名 → 当前模型 normalized 骨骼实际节点名
        const node = App.vrm.humanoid.getNormalizedBoneNode(humanoidName);
        if (!node) continue;
        const trackName = `${node.name}.${propertyName}`;
        if (t.type === 'quaternion') {
          tracks.push(new THREE.QuaternionKeyframeTrack(trackName, t.times, t.values));
        } else if (t.type === 'vector') {
          // 位移缩放 + VRM 0.x x/z 取反（与 retargetAnimation 一致）
          const values = t.values.map((v: number, i: number) =>
            (vrm0 && i % 3 !== 1 ? -v : v) * hipsPositionScale
          );
          tracks.push(new THREE.VectorKeyframeTrack(trackName, t.times, values));
        }
      }
      if (tracks.length === 0) return null;

      const clip = new THREE.AnimationClip('vrmAnimation', data.duration, tracks);
      App.mixamoClips[name] = { name, clip };
      // 预绑定 AnimationAction（与 loadMixamoAnimation 一致，播放零开销）
      try {
        if (!App.mixamoMixer) App.mixamoMixer = new THREE.AnimationMixer(App.vrm.scene);
        App.mixamoMixer.clipAction(clip);
      } catch (e) { /* 预绑定失败不影响注册 */ }
      console.log(`[Mixamo] 已注册烘焙动作: ${name} (${clip.duration.toFixed(2)}s, ${clip.tracks.length} 轨道)`);
      return clip;
    } catch (e) {
      console.error('[Mixamo] 烘焙动作加载失败:', name, e);
      return null;
    }
  };

  /** 播放已注册的 Mixamo 片段 */
  App.playMixamoClip = function playMixamoClip(name: string, opts?: any) {
    const info = App.mixamoClips[name];
    if (!info || !App.vrm) return;
    // 中止尾收回落：任何播放行为都可能来自最新指令,一律打断收尾切到新目标
    if (App._mixamoTailActive) {
      App._mixamoTailActive = false;
      App._mixamoTailName = null;
      App._mixamoTailRem = 0;
      App._mixamoTailTotal = 0;
    }
    // 动作开局前把 hips 复位到静息位：任何残留位移（滑步/漂移/腾空）到此清零；
    // 动作期间 hips 位移轨道已被丢弃，身体位置完全由行走系统（显式速度）驱动
    App.resetMixamoHips();
    if (!App.mixamoMixer) App.mixamoMixer = new THREE.AnimationMixer(App.vrm.scene);
    const action = App.mixamoMixer.clipAction(info.clip);
    const prevAction = App._mixamoActiveAction;
    action.reset();
    action.setLoop(opts && opts.loop === false ? THREE.LoopOnce : THREE.LoopRepeat, 1);
    action.clampWhenFinished = true;
    // 取消动作混合：动作间直接切换，不做 crossfade 交叠——
    // 旧动作立即停掉，新动作从首帧硬切进场，响应干脆、无姿态交叠
    if (prevAction && prevAction !== action) {
      prevAction.stop();
    }
    action.play();
    App._mixamoActiveAction = action;
    App._mixamoActiveClip = name;
    // 记录循环属性与开始时间：循环动作播够最短时长后允许统一调度轮换，
    // 避免角色长期保持同一个循环动作显得呆板（单次动作播完自动释放，不打断）
    App._mixamoActiveClipLoop = !(opts && opts.loop === false);
    App._mixamoActiveClipStart = performance.now();
    App._mixamoSwitchTimer = null; // 轮换倒计时随新 clip 重置
    // 单次动作（opts.loop===false）演完（LoopOnce+clampWhenFinished 定格在末帧）
    // → 取消动作混合：不做尾收回落，演完立即硬切下一个随机动作，不留静息空窗
    if (opts && opts.loop === false) {
      const n = name;
      const onFinished = () => {
        if (App._mixamoActiveClip !== n) return;
        // 直接接下一个随机动作（保持一直在动）；库无可播动作才交回程序微动作
        if (App.tryStartLibraryAction && App.tryStartLibraryAction()) return;
        App.stopMixamoClip();
      };
      action.removeEventListener('finished', onFinished);
      action.addEventListener('finished', onFinished);
    }
  };

  /** 停止 Mixamo 播放（主动释放；取消动作混合后直接停，不做淡出软着陆） */
  App.stopMixamoClip = function stopMixamoClip(fadeMs?) {
    const name = App._mixamoActiveClip;
    if (App.mixamoMixer && App._mixamoActiveAction) {
      App._mixamoActiveAction.stop();
    }
    const release = () => {
      // 淡出窗口内若已切换新动作（_mixamoActiveClip 已不再等于本动作），
      // 绝不能 stopAllAction 误杀新动作 —— 只在确认“仍是本动作”时才交还控制权
      if (App._mixamoActiveClip === name || !App._mixamoActiveClip) {
        if (App.mixamoMixer) App.mixamoMixer.stopAllAction();
        App._mixamoActiveClip = null;
        App._mixamoActiveAction = null;
        App._mixamoActiveClipLoop = false;
        App._mixamoActiveClipStart = 0;
        // 尾收状态一并清零：用户主动 stop 优先级最高，命令即刻交权
        App._mixamoTailActive = false;
        App._mixamoTailName = null;
        App._mixamoTailRem = 0;
        App._mixamoTailTotal = 0;
        if (App.clearAnimState) App.clearAnimState(); // 上报“当前无库动作”
        App.resetMixamoHips(); // 释放接管权时复位 hips 位移，杜绝残留
      }
    };
    release();
  };

  /** 渲染循环内调用：取消动作混合后无尾收回落窗口，直接推进动画时间 */
  App.updateMixamoMixer = function updateMixamoMixer(dt: number) {
    const m = App.mixamoMixer;
    if (!m) return;
    m.update(dt);
  };
});
