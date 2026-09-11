// 扫描 models 下所有 VRM：扩展、图片引用方式、大小
import fs from 'fs';
import path from 'path';

const dir = 'models';
function readJson(p) {
  const b = fs.readFileSync(p);
  const len = b.readUInt32LE(12);
  return JSON.parse(b.subarray(20, 20 + len).toString());
}

const files = fs.readdirSync(dir).filter(f => f.endsWith('.vrm'));
for (const f of files) {
  const p = path.join(dir, f);
  const stat = fs.statSync(p);
  try {
    const g = readJson(p);
    const extUsed = g.extensionsUsed || [];
    const ext = Object.keys(g.extensions || {});
    const images = (g.images || []).map(img => img.uri ? `ext:${img.uri}` : 'inline');
    const humanoid = g.extensions?.VRM?.humanoid?.humanBones?.length ?? 0;
    const blends = g.extensions?.VRM?.blendShapeMaster?.blendShapeGroups?.length ?? 0;
    const springs = g.extensions?.VRM?.secondaryAnimation?.boneGroups?.length ?? 0;
    console.log(`${f}\t${(stat.size/1048576).toFixed(1)}MB\tVRM:${ext.includes('VRM')}\thumanoid:${humanoid}\tblends:${blends}\tsprings:${springs}\timages:[${images.join(',')}]`);
  } catch (e) {
    console.log(`${f}\t${(stat.size/1048576).toFixed(1)}MB\tERROR: ${e.message}`);
  }
}