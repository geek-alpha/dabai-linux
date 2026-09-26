/* ============================================================
 * 从 VRM 文件 dump 骨架局部变换 —— 喂给 tools/train/skeleton.mjs
 * ------------------------------------------------------------
 * 每项 = 该段骨骼相对「SEGMENTS 里定义的父段骨骼」的局部 pos/quat，
 * 模型原始尺度、不含任何整体缩放（训练侧再乘 scale）。
 * 父段骨骼与目标骨骼之间若有中间节点（如 shoulder），沿途累乘。
 *
 * 用法：node tools/train/dump_vrm_skeleton.mjs models/白头凤.vrm
 * ============================================================ */

import * as THREE from 'three';
import { readFileSync } from 'node:fs';
import { SEGMENTS } from './build/ragdoll-core.mjs';

/** SEGMENTS 里没有、但训练侧要量段长用的末端节点 */
const END_NODES = [
  { bone: 'leftHand', parent: 'leftLowerArm' },
  { bone: 'rightHand', parent: 'rightLowerArm' },
  { bone: 'leftToes', parent: 'leftFoot' },
  { bone: 'rightToes', parent: 'rightFoot' },
];

function parseVrm(file) {
  const buf = readFileSync(file);
  const jsonLen = buf.readUInt32LE(12);
  const gltf = JSON.parse(buf.subarray(20, 20 + jsonLen).toString('utf8'));
  const vrm = gltf.extensions?.VRM || gltf.extensions?.VRMC_vrm;
  const raw = vrm?.humanoid?.humanBones;
  const map = {};
  if (Array.isArray(raw)) for (const b of raw) map[b.bone] = b.node;
  else if (raw && typeof raw === 'object') for (const [k, v] of Object.entries(raw)) map[k] = v.node;

  const nodes = gltf.nodes || [];
  const objs = nodes.map((n) => {
    const o = new THREE.Object3D();
    o.name = n.name || '';
    if (n.matrix) o.applyMatrix4(new THREE.Matrix4().fromArray(n.matrix));
    else {
      if (n.translation) o.position.fromArray(n.translation);
      if (n.rotation) o.quaternion.fromArray(n.rotation);
      if (n.scale) o.scale.fromArray(n.scale);
    }
    return o;
  });
  nodes.forEach((n, i) => (n.children || []).forEach((c) => objs[i].add(objs[c])));
  const parentOf = new Array(nodes.length).fill(-1);
  nodes.forEach((n, i) => (n.children || []).forEach((c) => (parentOf[c] = i)));
  const isChild = new Set();
  nodes.forEach((n) => (n.children || []).forEach((c) => isChild.add(c)));
  const sceneIdx = gltf.scene ?? 0;
  const roots = [];
  if (nodes[sceneIdx]) roots.push(sceneIdx);
  for (let i = 0; i < nodes.length; i++) if (!isChild.has(i) && !roots.includes(i)) roots.push(i);
  for (const r of roots) objs[r].updateMatrixWorld(true);
  return { gltf, nodes, objs, parentOf, map, roots, sceneIdx };
}

/** 从 bone 沿父链累乘到 parent 的局部矩阵；parent=null 时累乘到所在根节点 */
function relMatrix(v, boneIdx, parentIdx) {
  const chain = [];
  let cur = boneIdx;
  const stop = parentIdx == null ? -1 : parentIdx;
  while (cur !== -1 && cur !== stop) {
    chain.push(cur);
    cur = v.parentOf[cur];
  }
  if (parentIdx != null && cur !== parentIdx) return null;
  const m = new THREE.Matrix4();
  for (const idx of chain.reverse()) {
    v.objs[idx].updateMatrix();
    m.multiply(v.objs[idx].matrix);
  }
  return m;
}

function meshBounds(v) {
  let minY = Infinity, maxY = -Infinity;
  (v.gltf.meshes || []).forEach((m, mi) => {
    const nodeIdx = v.nodes.findIndex((n) => n.mesh === mi);
    const mo = nodeIdx >= 0 ? v.objs[nodeIdx] : v.objs[v.roots[0]];
    for (const p of m.primitives || []) {
      const acc = v.gltf.accessors?.[p.attributes?.POSITION];
      if (!acc?.min || !acc?.max) continue;
      for (const x of [acc.min[0], acc.max[0]]) {
        for (const y of [acc.min[1], acc.max[1]]) {
          for (const z of [acc.min[2], acc.max[2]]) {
            const wy = new THREE.Vector3(x, y, z).applyMatrix4(mo.matrixWorld).y;
            if (wy < minY) minY = wy;
            if (wy > maxY) maxY = wy;
          }
        }
      }
    }
  });
  return { minY, maxY, height: maxY - minY };
}

const r3 = (x) => Number(x.toFixed(5));

for (const file of process.argv.slice(2)) {
  const v = parseVrm(file);
  const box = meshBounds(v);
  const p = new THREE.Vector3();
  const q = new THREE.Quaternion();
  const s = new THREE.Vector3();
  const world = (bone) => {
    const idx = v.map[bone];
    if (idx === undefined) return null;
    return v.objs[idx].getWorldPosition(new THREE.Vector3());
  };

  const lines = [];
  let badScale = 0;
  for (const def of SEGMENTS) {
    const idx = v.map[def.bone];
    if (idx === undefined) {
      console.error(`!! 缺骨骼 ${def.bone}`);
      continue;
    }
    const parentIdx = def.parent == null ? null : v.map[def.parent];
    const m = relMatrix(v, idx, parentIdx);
    if (!m) {
      console.error(`!! ${def.bone} 与父段 ${def.parent} 不在同一条父链上`);
      continue;
    }
    m.decompose(p, q, s);
    if (Math.abs(s.x - 1) > 1e-3 || Math.abs(s.y - 1) > 1e-3 || Math.abs(s.z - 1) > 1e-3) badScale++;
    lines.push(
      `  { bone: '${def.bone}', parent: ${def.parent == null ? 'null' : `'${def.parent}'`}, ` +
        `pos: [${r3(p.x)}, ${r3(p.y)}, ${r3(p.z)}], quat: [${r3(q.x)}, ${r3(q.y)}, ${r3(q.z)}, ${r3(q.w)}] },`
    );
  }
  for (const def of END_NODES) {
    const idx = v.map[def.bone];
    const parentIdx = v.map[def.parent];
    if (idx === undefined || parentIdx === undefined) {
      console.error(`!! 缺骨骼 ${def.bone}/${def.parent}`);
      continue;
    }
    const m = relMatrix(v, idx, parentIdx);
    if (!m) continue;
    m.decompose(p, q, s);
    lines.push(
      `  { bone: '${def.bone}', parent: '${def.parent}', pos: [${r3(p.x)}, ${r3(p.y)}, ${r3(p.z)}], quat: [${r3(q.x)}, ${r3(q.y)}, ${r3(q.z)}, ${r3(q.w)}] },`
    );
  }

  const hip = world('hips');
  const hipY = hip ? hip.y - box.minY : NaN;
  const head = world('head');
  const d = (a, b) => {
    const A = world(a), B = world(b);
    return A && B ? A.distanceTo(B).toFixed(3) : '?';
  };

  console.log(`\n=== ${file.split('/').pop()} ===`);
  console.log(
    `身高 ${box.height.toFixed(3)}m  网格最低 ${box.minY.toFixed(3)}  髋高 ${hipY.toFixed(3)}m = 身高 ${((hipY / box.height) * 100).toFixed(1)}%  头顶 ${box.maxY.toFixed(3)}`
  );
  console.log(
    `段长: 大腿 ${d('leftUpperLeg', 'leftLowerLeg')} 小腿 ${d('leftLowerLeg', 'leftFoot')} 脚 ${d('leftFoot', 'leftToes')} | 髋-脊 ${d('hips', 'spine')} 脊-胸 ${d('spine', 'chest')} 胸-颈 ${d('chest', 'neck')} 颈-头 ${d('neck', 'head')} | 上臂 ${d('leftUpperArm', 'leftLowerArm')} 前臂 ${d('leftLowerArm', 'leftHand')}`
  );
  console.log(`头世界高 ${head ? head.y.toFixed(3) : '?'}  踝高 ${world('leftFoot') ? (world('leftFoot').y - box.minY).toFixed(3) : '?'}`);
  if (badScale) console.log(`!! ${badScale} 个骨骼带非 1 缩放（pos/quat 已分解，缩放被丢弃）`);
  console.log('export const SKELETON = [');
  console.log(lines.join('\n'));
  console.log('];');
  console.log(`export const BASE_HEIGHT = ${Number(box.height.toFixed(5))};`);
  console.log(`export const STAND_HIP_Y = ${Number(hipY.toFixed(5))};`);
}
