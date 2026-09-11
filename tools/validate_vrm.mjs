// 用 three-vrm 校验 莉丽拉.vrm（ESM）
import fs from 'fs';
import * as THREE from 'three';
import { GLTFLoader } from 'three/examples/jsm/loaders/GLTFLoader.js';
import { VRMLoaderPlugin } from '@pixiv/three-vrm';

const file = process.argv[2] || 'models/莉丽拉.vrm';
const buf = fs.readFileSync(file);
const loader = new GLTFLoader();
loader.register((parser) => new VRMLoaderPlugin(parser));

try {
  loader.parse(buf, '', (gltf) => {
    try {
      const vrm = gltf.userData.vrm;
      if (!vrm) throw new Error('VRM 未注册');
      const h = vrm.humanoid;
      const names = ['hips','spine','chest','neck','head','leftUpperArm','rightUpperArm','leftUpperLeg','rightUpperLeg','leftFoot','rightFoot'];
      let ok = 0;
      for (const n of names) if (h.getRawBoneNode(n)) ok++;
      const springs = vrm.springBoneManager ? vrm.springBoneManager.springBoneGroups.length : 0;
      const expressions = vrm.expressionManager ? Object.keys(vrm.expressionManager.expressionMap).length : 0;
      const la = h.getRawBoneNode('leftUpperArm');
      la.updateWorldMatrix(true, false);
      const q = new THREE.Quaternion();
      la.getWorldQuaternion(q);
      const dir = new THREE.Vector3(0,1,0).applyQuaternion(q);
      console.log('RESULT=' + JSON.stringify({ file, ok, names: names.length, springs, expressions, leftUpperArmY: dir.toArray().map(v=>+v.toFixed(3)) }));
      process.exit(0);
    } catch (e) {
      console.error('VERIFY_FAIL ' + (e && e.message));
      process.exit(1);
    }
  }, undefined, (e) => {
    console.error('LOAD_FAIL ' + (e && e.message));
    process.exit(1);
  });
} catch (e) {
  console.error('SYNC_FAIL ' + (e && e.message));
  process.exit(1);
}