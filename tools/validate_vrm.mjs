// 用 three-vrm 校验 VRM（ESM）。
// Node 里没有 createImageBitmap/document，GLTFLoader 解析内嵌纹理会永久挂起（表现为超时）。
// 给个假实现让流程跑完——这里验的是骨骼与表情，不看像素。
// 必须用动态 import：静态 import 会被提升到 polyfill 之前执行。
globalThis.createImageBitmap = globalThis.createImageBitmap || (async () => ({ width: 1, height: 1, close() {} }));

import fs from 'fs';

const THREE = await import('three');
const { GLTFLoader } = await import('three/examples/jsm/loaders/GLTFLoader.js');
const { VRMLoaderPlugin } = await import('@pixiv/three-vrm');

const file = process.argv[2] || 'models/莉丽拉.vrm';
const buf = fs.readFileSync(file);
const loader = new GLTFLoader();
loader.register((parser) => new VRMLoaderPlugin(parser));

// GLTFLoader.parse 是异步的：Node 事件循环里没有待处理 handle 时会在回调触发前退出，
// 表现是「无任何输出、exit=0」。用一个计时器把进程钉住，直到回调自己 exit。
setTimeout(() => { console.error('TIMEOUT'); process.exit(2); }, 30000);

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
