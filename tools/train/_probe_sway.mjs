/* 探针：_sway_ab 的注入到底有没有进物理（四组结果逐位相同 = 注入没生效） */
import * as THREE from 'three';
import { RagdollEnv, KIND_FLAT } from './env.mjs';

const env = new RagdollEnv({ randomize: false });
await env.init();
env.forceTerrain = KIND_FLAT;
env.forceLying = false;
env.reset(900001);

const P = (bone) => {
  bone.updateWorldMatrix(true, false);
  const e = bone.matrixWorld.elements;
  return [e[12], e[13], e[14]].map((v) => +v.toFixed(4));
};

console.log('bones 里带 Leg 的键：', Object.keys(env.bones || {}).filter((k) => /Leg/.test(k)));
console.log('restorePose 类型：', typeof env.restorePose, '| 自有属性？', Object.prototype.hasOwnProperty.call(env, 'restorePose'));
console.log('rd 存在：', !!env.rd, '| rd.restorePose？', typeof env.rd?.restorePose);

const _q = new THREE.Quaternion();
const _ax = new THREE.Vector3(1, 0, 0);
const b = env.bones.leftLowerLeg;
console.log('leftLowerLeg 骨骼对象：', b ? b.name : '缺失');
if (b) {
  const before = P(b);
  _q.setFromAxisAngle(_ax, 0.6);
  b.quaternion.multiply(_q);
  env.root.updateWorldMatrix(true, true);
  const after = P(b);
  console.log('注入 34° 前后世界位置：', before, '→', after, '| 位移', Math.hypot(after[0] - before[0], after[1] - before[1], after[2] - before[2]).toFixed(4));
}

const orig = env.restorePose.bind(env);
env.restorePose = () => { orig(); console.log('   [注入钩子被调用]'); };
env.step(new Float32Array(45));
console.log('step 后钩子计数结束');
env.dispose();
