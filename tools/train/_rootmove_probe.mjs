/* 探针：root 平移到底有没有传导到髋的驱动目标（targetP）—— 三档结果逐位相同说明没生效 */
import { RagdollEnv, KIND_FLAT } from './env.mjs';
import { ACT_DIM } from './build/ragdoll-core.mjs';

const env = new RagdollEnv({ randomize: false });
await env.init();
env.forceTerrain = KIND_FLAT;
env.forceLying = false;
env.difficulty = 1;
const obs = env.reset(900001);
env.rootSpeed = 1.0;
env.legSwayScale = 1.0;
env.swayScale = 1.0;

const act = new Float32Array(ACT_DIM);
const hip = env.rd.getParts()[0];
for (let s = 0; s < 6; s++) {
  env.step(act);
  console.log(
    `帧${s} rootSpeed=${env.rootSpeed} root.x=${env.root.position.x.toFixed(4)}` +
    ` travel=${env.rootTravel.x.toFixed(4)}` +
    ` 髋targetP.x=${hip.targetP.x.toFixed(4)}` +
    ` 髋body.x=${hip.body.translation().x.toFixed(4)}`
  );
}
console.log('髋 bone.position =', hip.bone.position.toArray().map((v) => v.toFixed(4)).join(','));
console.log('髋 bone.parent =', hip.bone.parent ? hip.bone.parent.name : 'null');
