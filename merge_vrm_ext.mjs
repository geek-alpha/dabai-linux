// 把原版 VRM 的 extensions.VRM 合并进 draco 压缩版（CLI draco 会剥掉 VRM 扩展）
// 用法: node merge_vrm_ext.mjs <原版.vrm> <压缩版.vrm> <输出.vrm>
import fs from 'fs';

const [,, srcFile, dracoFile, outFile] = process.argv;
if (!srcFile || !dracoFile || !outFile) {
  console.error('用法: node merge_vrm_ext.mjs <原版.vrm> <压缩版.vrm> <输出.vrm>');
  process.exit(1);
}

function readGLB(p) {
  const b = fs.readFileSync(p);
  if (b.toString('ascii', 0, 4) !== 'glTF') throw new Error(`${p} 不是 GLB`);
  const jsonLen = b.readUInt32LE(12);
  const json = JSON.parse(b.subarray(20, 20 + jsonLen).toString());
  const binStart = 20 + jsonLen;
  const binLen = b.readUInt32LE(binStart);
  const bin = b.subarray(binStart + 8, binStart + 8 + binLen);
  return { json, bin };
}

function writeGLB(p, json, bin) {
  const jsonBuf = Buffer.from(JSON.stringify(json), 'utf8');
  const header = Buffer.alloc(12);
  header.write('glTF', 0, 'ascii');
  header.writeUInt32LE(2, 4);
  header.writeUInt32LE(12 + 8 + jsonBuf.length + 8 + bin.length, 8);
  const jsonChunk = Buffer.alloc(8 + jsonBuf.length);
  jsonChunk.writeUInt32LE(jsonBuf.length, 0);
  jsonChunk.write('JSON', 4, 'ascii');
  jsonBuf.copy(jsonChunk, 8);
  const binChunk = Buffer.alloc(8 + bin.length);
  binChunk.writeUInt32LE(bin.length, 0);
  binChunk.write('BIN\0', 4, 'ascii');
  bin.copy(binChunk, 8);
  fs.writeFileSync(p, Buffer.concat([header, jsonChunk, binChunk]));
}

const src = readGLB(srcFile);
const draco = readGLB(dracoFile);

// 校验：原版必须有 VRM 扩展
if (!src.json.extensions?.VRM) {
  console.error(`原版 ${srcFile} 没有 VRM 扩展，跳过`);
  process.exit(1);
}

// 合并
const extUsed = draco.json.extensionsUsed || [];
if (!extUsed.includes('VRM')) extUsed.push('VRM');
draco.json.extensionsUsed = extUsed;
draco.json.extensions = draco.json.extensions || {};
draco.json.extensions.VRM = src.json.extensions.VRM;

writeGLB(outFile, draco.json, draco.bin);

const before = fs.statSync(srcFile).size;
const after = fs.statSync(outFile).size;
console.log(`✅ ${srcFile} (${(before/1048576).toFixed(1)}MB) → ${outFile} (${(after/1048576).toFixed(1)}MB)  [VRM 扩展已合并]`);
console.log(`   压缩率: ${(100 - after/before*100).toFixed(1)}%`);