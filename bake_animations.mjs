/**
 * Mixamo 动作离线烘焙器
 * ============================================================
 * 把 web/anim/ 下的 FBX 动作一次性重定向为「标准 humanoid 骨骼轨道」JSON 缓存，
 * 前端加载时直接反序列化，跳过 FBX 解析与骨骼重定向（零运行时计算）。
 *
 * 关键洞察：
 *  - 旋转重定向 q' = parentRestWorldRot * q * restWorldRot⁻¹ 只依赖 FBX 自身
 *    骨骼的 rest 世界旋转，与 VRM 模型完全无关 → 烘焙结果对所有 VRM 通用。
 *  - 轨道名用标准 humanoid 名（Normalized_<humanoid名>），前端加载时按
 *    当前模型的 normalized 骨骼节点名做字符串映射（毫秒级，非重定向）。
 *  - 位移轨道：丢弃 hips（原位播放治理，与 09c 一致），其余骨骼位移保留。
 *
 * 用法：node bake_animations.mjs
 * 输出：web/anim/baked/<动作名>.json
 * ============================================================
 */
import * as THREE from 'three';
import { FBXLoader } from 'three/examples/jsm/loaders/FBXLoader.js';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const ANIM_DIR = path.join(__dirname, 'web', 'anim');
const OUT_DIR = path.join(ANIM_DIR, 'baked');
const CONFIG_PATH = path.join(ANIM_DIR, 'animation-library.json');

// Mixamo 骨骼名 → VRM humanoid 骨骼名（与 09c_mixamo_retarget.ts 保持一致）
const mixamoVRMRigMap = {
  mixamorigHips: 'hips', mixamorigSpine: 'spine', mixamorigSpine1: 'chest', mixamorigSpine2: 'upperChest',
  mixamorigNeck: 'neck', mixamorigHead: 'head',
  mixamorigLeftShoulder: 'leftShoulder', mixamorigLeftArm: 'leftUpperArm', mixamorigLeftForeArm: 'leftLowerArm', mixamorigLeftHand: 'leftHand',
  mixamorigLeftHandThumb1: 'leftThumbMetacarpal', mixamorigLeftHandThumb2: 'leftThumbProximal', mixamorigLeftHandThumb3: 'leftThumbDistal',
  mixamorigLeftHandIndex1: 'leftIndexProximal', mixamorigLeftHandIndex2: 'leftIndexIntermediate', mixamorigLeftHandIndex3: 'leftIndexDistal',
  mixamorigLeftHandMiddle1: 'leftMiddleProximal', mixamorigLeftHandMiddle2: 'leftMiddleIntermediate', mixamorigLeftHandMiddle3: 'leftMiddleDistal',
  mixamorigLeftHandRing1: 'leftRingProximal', mixamorigLeftHandRing2: 'leftRingIntermediate', mixamorigLeftHandRing3: 'leftRingDistal',
  mixamorigLeftHandPinky1: 'leftLittleProximal', mixamorigLeftHandPinky2: 'leftLittleIntermediate', mixamorigLeftHandPinky3: 'leftLittleDistal',
  mixamorigRightShoulder: 'rightShoulder', mixamorigRightArm: 'rightUpperArm', mixamorigRightForeArm: 'rightLowerArm', mixamorigRightHand: 'rightHand',
  mixamorigRightHandThumb1: 'rightThumbMetacarpal', mixamorigRightHandThumb2: 'rightThumbProximal', mixamorigRightHandThumb3: 'rightThumbDistal',
  mixamorigRightHandIndex1: 'rightIndexProximal', mixamorigRightHandIndex2: 'rightIndexIntermediate', mixamorigRightHandIndex3: 'rightIndexDistal',
  mixamorigRightHandMiddle1: 'rightMiddleProximal', mixamorigRightHandMiddle2: 'rightMiddleIntermediate', mixamorigRightHandMiddle3: 'rightMiddleDistal',
  mixamorigRightHandRing1: 'rightRingProximal', mixamorigRightHandRing2: 'rightRingIntermediate', mixamorigRightHandRing3: 'rightRingDistal',
  mixamorigRightHandPinky1: 'rightLittleProximal', mixamorigRightHandPinky2: 'rightLittleIntermediate', mixamorigRightHandPinky3: 'rightLittleDistal',
  mixamorigLeftUpLeg: 'leftUpperLeg', mixamorigLeftLeg: 'leftLowerLeg', mixamorigLeftFoot: 'leftFoot', mixamorigLeftToeBase: 'leftToes',
  mixamorigRightUpLeg: 'rightUpperLeg', mixamorigRightLeg: 'rightLowerLeg', mixamorigRightFoot: 'rightFoot', mixamorigRightToeBase: 'rightToes'
};

/** 重定向单个 FBX 的动画片段 → 可序列化轨道数据 */
function bakeClip(fbxAsset) {
  const clip = THREE.AnimationClip.findByName(fbxAsset.animations, 'mixamo.com')
    || (fbxAsset.animations && fbxAsset.animations[0]);
  if (!clip) return null;

  const tracks = [];
  const restRotationInverse = new THREE.Quaternion();
  const parentRestWorldRotation = new THREE.Quaternion();
  const _quatA = new THREE.Quaternion();

  clip.tracks.forEach((track) => {
    const trackSplitted = track.name.split('.');
    const mixamoRigName = trackSplitted[0];
    const vrmBoneName = mixamoVRMRigMap[mixamoRigName];
    if (!vrmBoneName) return;
    const mixamoRigNode = fbxAsset.getObjectByName(mixamoRigName);
    if (!mixamoRigNode) return;

    const propertyName = trackSplitted[1];
    mixamoRigNode.getWorldQuaternion(restRotationInverse).invert();
    mixamoRigNode.parent?.getWorldQuaternion(parentRestWorldRotation);

    if (track instanceof THREE.QuaternionKeyframeTrack) {
      const values = new Float32Array(track.values.length);
      for (let i = 0; i < track.values.length; i += 4) {
        _quatA.fromArray(track.values, i);
        _quatA.premultiply(parentRestWorldRotation).multiply(restRotationInverse);
        _quatA.toArray(values, i);
      }
      tracks.push({
        type: 'quaternion',
        // 轨道名存 humanoid 名（如 hips.quaternion），前端加载时映射到
        // 当前模型 normalized 骨骼的实际节点名（Normalized_<原始骨骼名>）
        name: `${vrmBoneName}.${propertyName}`,
        times: Array.from(track.times),
        values: Array.from(values)
      });
    } else if (track instanceof THREE.VectorKeyframeTrack) {
      // 原位播放治理：丢弃 hips 位移轨道（与 09c 一致）
      if (vrmBoneName === 'hips') return;
      tracks.push({
        type: 'vector',
        name: `${vrmBoneName}.${propertyName}`,
        times: Array.from(track.times),
        values: Array.from(track.values)
      });
    }
  });

  return {
    name: clip.name,
    duration: clip.duration,
    motionHipsHeight: fbxAsset.getObjectByName('mixamorigHips')?.position.y || 0,
    tracks
  };
}

// ==================== 主流程 ====================
const config = JSON.parse(fs.readFileSync(CONFIG_PATH, 'utf-8'));
const loader = new FBXLoader();

const allAnims = [];
for (const catKey in config.categories) {
  for (const anim of config.categories[catKey].animations) {
    allAnims.push({ ...anim, category: catKey });
  }
}

fs.mkdirSync(OUT_DIR, { recursive: true });
let ok = 0, fail = 0, totalTracks = 0, totalBytes = 0;

for (const anim of allAnims) {
  const fbxPath = path.join(ANIM_DIR, anim.file);
  if (!fs.existsSync(fbxPath)) {
    console.warn(`[skip] ${anim.name}: 文件不存在 ${anim.file}`);
    fail++;
    continue;
  }
  try {
    const buf = fs.readFileSync(fbxPath);
    // Node Buffer → 真正的 ArrayBuffer（FBXLoader 需要 ArrayBuffer 语义）
    const buffer = buf.buffer.slice(buf.byteOffset, buf.byteOffset + buf.byteLength);
    const fbxAsset = loader.parse(buffer, anim.file);
    const baked = bakeClip(fbxAsset);
    if (!baked || baked.tracks.length === 0) {
      console.warn(`[fail] ${anim.name}: 无可用动画轨道`);
      fail++;
      continue;
    }
    const outPath = path.join(OUT_DIR, `${anim.name}.json`);
    const json = JSON.stringify({
      name: anim.name,
      category: anim.category,
      emotion: anim.emotion || '',
      loop: anim.loop ?? false,
      duration: baked.duration,
      motionHipsHeight: baked.motionHipsHeight,
      tracks: baked.tracks
    });
    fs.writeFileSync(outPath, json);
    ok++;
    totalTracks += baked.tracks.length;
    totalBytes += json.length;
    console.log(`[ok] ${anim.name}  ${baked.duration.toFixed(2)}s  ${baked.tracks.length} 轨道`);
  } catch (e) {
    console.error(`[fail] ${anim.name}:`, e.message);
    fail++;
  }
}

console.log('\n========== 烘焙完成 ==========');
console.log(`成功 ${ok} / 失败 ${fail} / 共 ${allAnims.length}`);
console.log(`总轨道 ${totalTracks}，总大小 ${(totalBytes / 1024).toFixed(1)} KB`);
