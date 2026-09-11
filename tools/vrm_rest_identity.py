"""
vrm_rest_identity.py —— MMD → VRM 手臂自然下垂（最终方案 PlanB）。

机制（实测蔚蓝妖姬 + 数学推导）：
- skinning: final = Σ w * (jointMatrix * IBM) * bindVertex。
- IBM 对应原始骨骼 rest（world_old）。若把骨骼 rest 改为 world_new（手臂绕肩
  转 ±90° 下垂）而 IBM 不变，则 rest 渲染 = world_new * inv(world_old) * bindVertex：
    * 手臂顶点自动跟随骨骼旋转（下垂），弯曲/拓扑自然保持（无重绑变形）
    * 躯干顶点（骨骼未变）不受影响
    * 肩部混合权重顶点平滑过渡（无断裂）
- 关键：不要重绑网格顶点、不要重算 IBM（双重变换是之前变形的根因）。

步骤：
1. 手臂链（upperArm 及后代，不含 spring）骨骼世界矩阵绕肩关节（shoulder 位置）
   绕 Z 旋转 ±90°（左臂 -90°、右臂 +90°，MMD 手臂局部 +Y 竖直 → 世界下垂）。
2. 自顶向下写回骨骼局部变换（translation/rotation）。
3. 网格顶点与 IBM 保持不变。

效果：加载即手在肩下（自然下垂）；前端 applyVrmRestPose（z=±1.35）后水平张开。
"""
from __future__ import annotations

import json
import math
import struct
import sys

GLB_MAGIC = 0x46546C67
CHUNK_JSON = 0x4E4F534A
CHUNK_BIN = 0x004E4942


def read_glb(path):
    with open(path, 'rb') as f:
        data = f.read()
    magic, version, length = struct.unpack_from('<III', data, 0)
    if magic != GLB_MAGIC:
        raise ValueError(f'{path} 不是 GLB')
    offset = 12
    json_dict = None
    bin_chunk = b''
    while offset + 8 <= length:
        chunk_len, chunk_type = struct.unpack_from('<II', data, offset)
        offset += 8
        chunk = data[offset:offset + chunk_len]
        offset += chunk_len
        if chunk_type == CHUNK_JSON:
            json_dict = json.loads(chunk.decode('utf-8'))
        elif chunk_type == CHUNK_BIN:
            bin_chunk = chunk
    if json_dict is None:
        raise ValueError('GLB 缺少 JSON chunk')
    return json_dict, bin_chunk


def write_glb(path, gltf, bin_chunk):
    json_bytes = json.dumps(gltf, separators=(',', ':'), ensure_ascii=False).encode('utf-8')
    json_pad = (4 - (len(json_bytes) % 4)) % 4
    json_bytes += b' ' * json_pad
    bin_pad = (4 - (len(bin_chunk) % 4)) % 4
    bin_out = bin_chunk + b'\x00' * bin_pad
    total = 12 + 8 + len(json_bytes) + 8 + len(bin_out)
    header = struct.pack('<III', GLB_MAGIC, 2, total)
    json_header = struct.pack('<II', len(json_bytes), CHUNK_JSON)
    bin_header = struct.pack('<II', len(bin_out), CHUNK_BIN)
    with open(path, 'wb') as f:
        f.write(header + json_header + json_bytes + bin_header + bin_out)


def mat4_from_trs(t, r, s):
    qx, qy, qz, qw = r
    m = [
        [1 - 2*(qy*qy+qz*qz), 2*(qx*qy-qz*qw), 2*(qx*qz+qy*qw), t[0]],
        [2*(qx*qy+qz*qw), 1-2*(qx*qx+qz*qz), 2*(qy*qz-qx*qw), t[1]],
        [2*(qx*qz-qy*qw), 2*(qy*qz+qx*qw), 1-2*(qx*qx+qy*qy), t[2]],
        [0, 0, 0, 1],
    ]
    for r_ in range(3):
        for c_ in range(3):
            m[r_][c_] *= s[c_]
    return m


def mat4_mul(a, b):
    return [[sum(a[r_][k] * b[k][c_] for k in range(4)) for c_ in range(4)] for r_ in range(4)]


def m2q(m):
    tr = m[0][0] + m[1][1] + m[2][2]
    if tr > 0:
        s = math.sqrt(tr + 1.0) * 2
        w = 0.25 * s
        x = (m[2][1] - m[1][2]) / s
        y = (m[0][2] - m[2][0]) / s
        z = (m[1][0] - m[0][1]) / s
    else:
        if m[0][0] > m[1][1] and m[0][0] > m[2][2]:
            s = math.sqrt(1.0 + m[0][0] - m[1][1] - m[2][2]) * 2
            w = (m[2][1] - m[1][2]) / s
            x = 0.25 * s
            y = (m[0][1] + m[1][0]) / s
            z = (m[0][2] + m[2][0]) / s
        elif m[1][1] > m[2][2]:
            s = math.sqrt(1.0 + m[1][1] - m[0][0] - m[2][2]) * 2
            w = (m[0][2] - m[2][0]) / s
            x = (m[0][1] + m[1][0]) / s
            y = 0.25 * s
            z = (m[1][2] + m[2][1]) / s
        else:
            s = math.sqrt(1.0 + m[2][2] - m[0][0] - m[1][1]) * 2
            w = (m[1][0] - m[0][1]) / s
            x = (m[0][2] + m[2][0]) / s
            y = (m[1][2] + m[2][1]) / s
            z = 0.25 * s
    n = math.sqrt(x*x + y*y + z*z + w*w)
    if n < 1e-9:
        return (0, 0, 0, 1)
    return (x/n, y/n, z/n, w/n)


def main():
    src = sys.argv[1]
    dst = sys.argv[2]
    gltf, bin_chunk = read_glb(src)
    nodes = gltf['nodes']
    total_nodes = len(nodes)

    parent_of = {}
    children_of = {}
    for i, n in enumerate(nodes):
        children_of[i] = n.get('children', [])
        for c in children_of[i]:
            parent_of[c] = i

    ext = gltf.get('extensions', {})
    vrm1 = ext.get('VRMC_vrm', {})
    humanoid_slots = {}
    if vrm1:
        hb = vrm1.get('humanoid', {}).get('humanBones', {})
        for slot, info in hb.items():
            node = info.get('node') if isinstance(info, dict) else info
            if node is not None:
                humanoid_slots[slot] = node

    spring_nodes = set()
    spring = ext.get('VRMC_springBone', {})
    for s in spring.get('springs', []):
        for j in s.get('joints', []):
            n = j.get('node')
            if n is not None:
                spring_nodes.add(n)

    order, visited = [], set()

    def dfs(i):
        if i in visited:
            return
        visited.add(i)
        p = parent_of.get(i)
        if p is not None:
            dfs(p)
        order.append(i)

    for i in range(total_nodes):
        dfs(i)

    def local_mat(i):
        n = nodes[i]
        t = n.get('translation', [0, 0, 0])
        r = n.get('rotation', [0, 0, 0, 1])
        s = n.get('scale', [1, 1, 1])
        return mat4_from_trs(t, r, s)

    world_old = {}
    for i in order:
        p = parent_of.get(i)
        world_old[i] = local_mat(i) if p is None else mat4_mul(world_old[p], local_mat(i))

    # ---------- 手臂链（upperArm 及后代，不含 spring） ----------
    arm_chains = {}

    def collect_chain(root_idx):
        chain = set()

        def walk(i):
            if i in spring_nodes:
                return
            chain.add(i)
            for c in children_of.get(i, []):
                walk(c)

        walk(root_idx)
        return chain

    for slot in ('leftUpperArm', 'rightUpperArm'):
        idx = humanoid_slots.get(slot)
        if idx is not None:
            arm_chains[slot] = collect_chain(idx)

    all_arm_nodes = set()
    for c in arm_chains.values():
        all_arm_nodes |= c

    # ---------- 旋转中心 = 肩关节（upperArm 自身位置，身体外侧；不要用 shoulder，
    # 否则手臂根部被旋进身体） ----------
    import numpy as np

    pivots = {}
    for slot, angle in (('leftUpperArm', -math.pi / 2), ('rightUpperArm', math.pi / 2)):
        idx = humanoid_slots.get(slot)
        if idx is None:
            continue
        m = world_old[idx]
        pivots[idx] = (angle, (m[0][3], m[1][3], m[2][3]))

    # ---------- 骨骼世界矩阵绕肩旋转 ±90° ----------
    rotated_world = {}
    for idx, (angle, center) in pivots.items():
        # 找出该 idx 对应的 slot（leftUpperArm / rightUpperArm）
        slot = None
        for s in ('leftUpperArm', 'rightUpperArm'):
            if humanoid_slots.get(s) == idx:
                slot = s
                break
        if slot is None:
            continue
        cx, cy, cz = center
        c_mat = np.eye(4); c_mat[:3, 3] = [cx, cy, cz]
        rz = np.eye(4)
        rz[:3, :3] = [[math.cos(angle), -math.sin(angle), 0],
                      [math.sin(angle), math.cos(angle), 0],
                      [0, 0, 1]]
        negc = np.eye(4); negc[:3, 3] = [-cx, -cy, -cz]
        rot = c_mat @ rz @ negc
        for i in arm_chains[slot]:
            rotated_world[i] = rot @ np.array(world_old[i], dtype=np.float64)

    # ---------- 写回骨骼局部变换（自顶向下） ----------
    world_new = {}
    for i in order:
        n = nodes[i]
        p = parent_of.get(i)
        if i in rotated_world:
            w_new = rotated_world[i]
            if p is None:
                _write_trs(n, w_new)
                world_new[i] = np.array(local_mat(i), dtype=np.float64)
            else:
                parent_new = world_new[p]
                inv_parent = np.linalg.inv(parent_new)
                local = inv_parent @ w_new
                _write_trs(n, local)
                world_new[i] = parent_new @ np.array(local_mat(i), dtype=np.float64)
        else:
            if p is None:
                world_new[i] = np.array(local_mat(i), dtype=np.float64)
            else:
                world_new[i] = world_new[p] @ np.array(local_mat(i), dtype=np.float64)

    # 网格顶点与 IBM 保持不变（关键）

    write_glb(dst, gltf, bin_chunk)
    chains = {s: len(c) for s, c in arm_chains.items()}
    print(f'REST_IDENTITY_OK -> {dst} ({total_nodes} nodes, {len(gltf.get("skins", []))} skins, arm_chains={chains})')


def _write_trs(node, m):
    import numpy as np

    sx = float(np.linalg.norm([m[0, 0], m[1, 0], m[2, 0]]))
    sy = float(np.linalg.norm([m[0, 1], m[1, 1], m[2, 1]]))
    sz = float(np.linalg.norm([m[0, 2], m[1, 2], m[2, 2]]))
    rot = [
        [m[0, 0] / (sx or 1), m[0, 1] / (sy or 1), m[0, 2] / (sz or 1)],
        [m[1, 0] / (sx or 1), m[1, 1] / (sy or 1), m[1, 2] / (sz or 1)],
        [m[2, 0] / (sx or 1), m[2, 1] / (sy or 1), m[2, 2] / (sz or 1)],
    ]
    node['translation'] = [round(float(m[0, 3]), 6), round(float(m[1, 3]), 6), round(float(m[2, 3]), 6)]
    node['rotation'] = [round(v, 6) for v in m2q(rot)]
    if abs(sx - 1) > 1e-6 or abs(sy - 1) > 1e-6 or abs(sz - 1) > 1e-6:
        node['scale'] = [round(sx, 6), round(sy, 6), round(sz, 6)]


if __name__ == '__main__':
    main()