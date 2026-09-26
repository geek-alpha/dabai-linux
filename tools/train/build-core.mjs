/* 打包浏览器物理核心给 Node 用：rolldown 一次 ~50ms，物理代码改了重跑一次即可。
 * 用法：node tools/train/build-core.mjs */
import { execFileSync } from 'node:child_process';
import { mkdirSync, statSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const repo = path.resolve(here, '../..');
const out = path.join(here, 'build', 'ragdoll-core.mjs');
mkdirSync(path.dirname(out), { recursive: true });

execFileSync(
  process.execPath,
  [
    path.join(repo, 'node_modules', 'rolldown', 'bin', 'cli.mjs'),
    'tools/train/core-entry.ts',
    '-f', 'esm',
    '--external', 'three',
    '--external', '@dimforge/rapier3d-compat',
    '-o', 'tools/train/build/ragdoll-core.mjs',
  ],
  { cwd: repo, stdio: ['ignore', 'pipe', 'pipe'] }
);

console.log('打包完成:', out, statSync(out).size, 'bytes');
