/* 临时：同一 env（新骨架 STAND_HIP_Y=0.80023、scale=1.371）下横向评估多个权重的
 * 站姿与躺姿，用来回答「v3 相对 v1 是涨还是跌」。用法：
 *   node tools/train/_cmp13.mjs <权重1> [权重2 ...] */
import { RagdollEnv, KIND_NAMES } from './env.mjs';
import { evaluate, loadWeights } from './train.mjs';

const files = process.argv.slice(2);
const env = new RagdollEnv({ randomize: true });
await env.init();
env.difficulty = 1;

const f3 = (x) => (x == null ? ' -  ' : x.toFixed(3));
const f0 = (x) => (x == null ? ' -  ' : x.toFixed(0));

for (const f of files) {
  const tag = f.split('/').pop();
  const { net } = loadWeights(f);
  const r = evaluate(env, net, 16, 900001);
  for (const k of KIND_NAMES) {
    const s = r[k].standPolicy, p = r[k].standPd, l = r[k].lyingPolicy, lp = r[k].lyingPd;
    console.log(`[${tag}] ${k} 站 策略 hip${f3(s.hip)} up${f3(s.up)} ang${f3(s.ang)} ret${f0(s.ret)} | PD hip${f3(p.hip)} up${f3(p.up)} ang${f3(p.ang)} ret${f0(p.ret)}`);
    console.log(`[${tag}] ${k} 躺 策略 hip${f3(l.hip)} up${f3(l.up)} ang${f3(l.ang)} ret${f0(l.ret)} | PD hip${f3(lp.hip)} up${f3(lp.up)} ang${f3(lp.ang)} ret${f0(lp.ret)}`);
  }
}
env.dispose();
