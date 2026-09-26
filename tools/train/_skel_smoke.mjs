/* 冒烟：换骨架后训练侧物理是否还站得住。
 * 纯 PD（动作为 0）、平地、站姿开局，打印段长 / 站立髋高 / 髋高比 / 踝离地。
 * 判据：段长与白头凤实测一致（大腿 0.402×scale）、踝离地 ≈0.097×scale、
 * 髋高比稳定在 1 附近而不是掉到 0.6。
 * 用法：node tools/train/_skel_smoke.mjs [步数] */

import * as THREE from 'three';
import { RagdollEnv, KIND_FLAT } from './env.mjs';
import { ACT_DIM } from './build/ragdoll-core.mjs';

const steps = Number(process.argv[2] || 300);
const env = new RagdollEnv({ randomize: false });
await env.init();
env.forceTerrain = KIND_FLAT;
env.forceLying = false;
env.difficulty = 1;
env.dt = 1 / 60;
let obs = env.reset(900001);
// randomize=false 时 env 给的是 balance=0（纯重力，留给策略学）—— 这里默认按前端口径
// 打开手工支撑，验证「PD 基线还能不能站住」；传 train 则用训练口径。
if (process.argv[3] !== 'train') {
  env.rd.setTuning({ balance: 0.9, follow: 1, maxTorquePerKg: 40, subSteps: 2 });
}

const W = (n) => env.bones[n].getWorldPosition(new THREE.Vector3());
const dist = (a, c) => W(a).distanceTo(W(c));
const s = env.rd.scale;
console.log(
  `scale=${s.toFixed(4)} standHipY=${env.standHipY.toFixed(3)}（骨架基准 ${(env.standHipY / s).toFixed(4)}）`
);
console.log(
  `段长(m): 大腿 ${dist('leftUpperLeg', 'leftLowerLeg').toFixed(3)} 小腿 ${dist('leftLowerLeg', 'leftFoot').toFixed(3)} ` +
    `脚 ${dist('leftFoot', 'leftToes').toFixed(3)} 髋-脊 ${dist('hips', 'spine').toFixed(3)} 上臂 ${dist('leftUpperArm', 'leftLowerArm').toFixed(3)}`
);
console.log(
  `踝离地 ${(W('leftFoot').y - env.rd.groundY).toFixed(3)}m  髋离地 ${(W('hips').y - env.rd.groundY).toFixed(3)}m`
);

const act = new Float32Array(ACT_DIM);
const hip = env.rd.getParts()[0];
const rows = [];
for (let i = 0; i < steps; i++) {
  env.step(act);
  if (i === 0 || (i + 1) % 50 === 0) {
    const t = hip.body.translation();
    const g = env.rd.groundHeightAt(t.x, t.z);
    rows.push(
      `步${i + 1} 髋高比${((t.y - g) / env.standHipY).toFixed(2)} 踝离地${(W('leftFoot').y - env.rd.groundY).toFixed(3)}`
    );
  }
  obs = env.obs;
}
console.log(rows.join(' | '));
env.dispose();
