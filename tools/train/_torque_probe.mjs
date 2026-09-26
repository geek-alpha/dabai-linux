/* 验证 setTuning 改 maxTorquePerKg 真的生效。
 * 背景：maxTorque 在构造时按当时的 maxTorquePerKg 算死（active-ragdoll.ts:222），
 * setTuning 原先只改 tuning 字段与 kp —— 策略模式传 2.0 而身体仍按默认 40 跑。
 * 用法：node tools/train/_torque_probe.mjs */
import { RagdollEnv } from './env.mjs';

const env = new RagdollEnv({ randomize: false });
await env.init();
env.forceTerrain = 0;
env.reset(900001); // rd 只在 reset 里建，构造完还是 null
const rd = env.rd;

const dump = (tag) => {
  const p = rd.getParts()[0];
  console.log(
    `${tag}: tuning.maxTorquePerKg=${rd.tuning.maxTorquePerKg} ` +
    `hip.maxTorque=${p.maxTorque.toFixed(2)} hip.kp=${p.kp.toFixed(1)}`
  );
};

dump('构造后          ');
rd.setTuning({ maxTorquePerKg: 2.0, balance: 0.25 });
dump('setTuning(2.0) 后');
rd.setTuning({ maxTorquePerKg: 40 });
dump('setTuning(40) 后 ');

const ratio = rd.tuning.maxTorquePerKg / 2.0;
console.log(`期望：hip.maxTorque 随 maxTorquePerKg 等比缩放（2.0 时是 40 时的 1/20，即 ×${(1 / ratio).toFixed(3)}）`);
env.dispose();
