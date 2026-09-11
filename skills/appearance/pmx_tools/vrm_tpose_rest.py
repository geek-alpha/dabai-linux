"""vrm_tpose_rest.py —— 把导出后的 VRM 在 JSON 层直接改节点 rotation，把 rest 姿态标准化为 T-pose。

由 v19 转换流程调用（见 convert_luotianyi_v19.py）：
    1. humanoid 骨骼 -> 标准 T-pose 方向（保留绕轴扭转）
    2. 非 humanoid 非 spring 骨骼 -> rest 清零（rotation -> identity）
    3. spring 骨骼 -> 保留（物理不乱）
    4. 重算 IBM（inverseBindMatrices，网格绑定矩阵）+ 调整平移保持世界位置

用法：
    python vrm_tpose_rest.py <in.vrm> <out.vrm>
"""
from __future__ import annotations

import json
import math
import os
import struct
import sys
import time

GLB_MAGIC = 0x46546C67
CHUNK_JSON = 0x4E4F534A
CHUNK_BIN = 0x004E4942

CT_FLOAT = 5126


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
    return json_dict, bin_chunk, data


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


# ---------- 四元数 / 矩阵 ----------

def qmul(a, b):
    ax, ay, az, aw = a
    bx, by, bz, bw = b
    return (
        aw * bx + ax * bw + ay * bz - az * by,
        aw * by - ax * bz + ay * bw + az * bx,
        aw * bz + ax * by - ay * bx + az * bw,
        aw * bw - ax * bx - ay * by - az * bz,
    )


def qconj(q):
    return (-q[0], -q[1], -q[2], q[3])


def qnorm(q):
    n = math.sqrt(sum(v * v for v in q))
    if n < 1e-9:
        return (0.0, 0.0, 0.0, 1.0)
    return tuple(v / n for v in q)


def qrot(q, v):
    x, y, z = v
    qx, qy, qz, qw = q
    # v' = q v q^-1
    qv = (x, y, z, 0.0)
    r = qmul(qmul(q, qv), qconj(q))
    return (r[0], r[1], r[2])


def quat_from_to(a, b):
    """最短弧：把方向 a 转到方向 b（绕垂直于 a、b 的轴旋转）。"""
    na = math.sqrt(sum(v * v for v in a))
    nb = math.sqrt(sum(v * v for v in b))
    if na < 1e-9 or nb < 1e-9:
        return (0.0, 0.0, 0.0, 1.0)
    a = tuple(v / na for v in a)
    b = tuple(v / nb for v in b)
    ax, ay, az = a
    bx, by, bz = b
    dot = ax * bx + ay * by + az * bz
    if dot > 1.0 - 1e-9:
        return (0.0, 0.0, 0.0, 1.0)
    if dot < -1.0 + 1e-9:
        # 完全相反：绕任意垂直轴转 180 度
        axis = (1.0, 0.0, 0.0) if abs(ax) < 0.9 else (0.0, 1.0, 0.0)
        cross = (
            ay * axis[2] - az * axis[1],
            az * axis[0] - ax * axis[2],
            ax * axis[1] - ay * axis[0],
        )
        cl = math.sqrt(sum(v * v for v in cross))
        if cl < 1e-9:
            return (0.0, 0.0, 0.0, 1.0)
        axis = tuple(v / cl for v in cross)
        return qnorm((axis[0], axis[1], axis[2], 0.0))
    cross = (
        ay * bz - az * by,
        az * bx - ax * bz,
        ax * by - ay * bx,
    )
    cl = math.sqrt(sum(v * v for v in cross))
    axis = tuple(v / cl for v in cross)
    ang = math.acos(dot)
    s = math.sin(ang / 2.0)
    return qnorm((axis[0] * s, axis[1] * s, axis[2] * s, math.cos(ang / 2.0)))


# ---------- 主逻辑 ----------

# 标准 T-pose 目标方向（VRM/glTF 世界空间，Y 向上，角色面向 +Z）：
# 脊柱/脖子/头朝上 +Y；左臂 +X；右臂 -X；腿朝下 -Y。
TPOSE_TARGETS = {
    'spine': (0, 1, 0),
    'chest': (0, 1, 0),
    'upperChest': (0, 1, 0),
    'neck': (0, 1, 0),
    'head': (0, 1, 0),
    'leftShoulder': (1, 0, 0),
    'leftUpperArm': (1, 0, 0),
    'leftLowerArm': (1, 0, 0),
    'leftHand': (1, 0, 0),
    'rightShoulder': (-1, 0, 0),
    'rightUpperArm': (-1, 0, 0),
    'rightLowerArm': (-1, 0, 0),
    'rightHand': (-1, 0, 0),
    'leftUpperLeg': (0, -1, 0),
    'leftLowerLeg': (0, -1, 0),
    'leftFoot': (0, -1, 0),
    'rightUpperLeg': (0, -1, 0),
    'rightLowerLeg': (0, -1, 0),
    'rightFoot': (0, -1, 0),
}

# 受 T-pose 影响的子骨骼（保持相对父骨骼的朝向，只是跟随链）
TPOSE_CHILD_SLOTS = {
    'leftToes': 'leftFoot',
    'rightToes': 'rightFoot',
    'leftThumbMetacarpal': 'leftHand',
    'leftThumbProximal': 'leftHand',
    'leftThumbDistal': 'leftHand',
    'leftIndexProximal': 'leftHand',
    'leftIndexIntermediate': 'leftHand',
    'leftIndexDistal': 'leftHand',
    'leftMiddleProximal': 'leftHand',
    'leftMiddleIntermediate': 'leftHand',
    'leftMiddleDistal': 'leftHand',
    'leftRingProximal': 'leftHand',
    'leftRingIntermediate': 'leftHand',
    'leftRingDistal': 'leftHand',
    'leftLittleProximal': 'leftHand',
    'leftLittleIntermediate': 'leftHand',
    'leftLittleDistal': 'leftHand',
    'rightThumbMetacarpal': 'rightHand',
    'rightThumbProximal': 'rightHand',
    'rightThumbDistal': 'rightHand',
    'rightIndexProximal': 'rightHand',
    'rightIndexIntermediate': 'rightHand',
    'rightIndexDistal': 'rightHand',
    'rightMiddleProximal': 'rightHand',
    'rightMiddleIntermediate': 'rightHand',
    'rightMiddleDistal': 'rightHand',
    'rightRingProximal': 'rightHand',
    'rightRingIntermediate': 'rightHand',
    'rightRingDistal': 'rightHand',
    'rightLittleProximal': 'rightHand',
    'rightLittleIntermediate': 'rightHand',
    'rightLittleDistal': 'rightHand',
}


def main():
    src = sys.argv[1]
    dst = sys.argv[2]
    gltf, bin_chunk, _ = read_glb(src)

    nodes = gltf['nodes']
    total_nodes = len(nodes)

    # ---------- 1. 构建父子关系 ----------
    parent_of = {}  # child -> parent
    for i, n in enumerate(nodes):
        for c in n.get('children', []):
            parent_of[c] = i

    # ---------- 2. 收集 humanoid / spring 节点 ----------
    ext = gltf.get('extensions', {})
    vrm1 = ext.get('VRMC_vrm', {})
    humanoid_slots = {}  # slot -> node index
    if vrm1:
        hb = vrm1.get('humanoid', {}).get('humanBones', {})
        for slot, info in hb.items():
            if isinstance(info, dict):
                node = info.get('node')
            else:
                node = info
            if node is not None:
                humanoid_slots[slot] = node

    spring_nodes = set()
    spring = ext.get('VRMC_springBone', {})
    for s in spring.get('springs', []):
        for j in s.get('joints', []):
            n = j.get('node')
            if n is not None:
                spring_nodes.add(n)

    humanoid_node_set = set(humanoid_slots.values())

    # ---------- 3. 计算每个节点的世界旋转 ----------
    # 自顶向下：世界旋转 = 父世界旋转 * 自身局部旋转
    world_rot = {}  # node -> (qx,qy,qz,qw)

    def local_rot(i):
        r = nodes[i].get('rotation', [0, 0, 0, 1])
        return (r[0], r[1], r[2], r[3])

    def compute_world_rot(i):
        p = parent_of.get(i)
        if p is None:
            return local_rot(i)
        return qmul(compute_world_rot(p), local_rot(i))

    for i in range(total_nodes):
        world_rot[i] = compute_world_rot(i)

    # ---------- 4. 逐骨骼设置新局部旋转 ----------
    # 分类：
    #   humanoid 主骨骼（在 TPOSE_TARGETS 里）-> 对齐到 T-pose 方向（保留绕轴扭转）
    #   humanoid 子骨骼（手指/脚趾）-> 跟随父 humanoid（保持局部旋转不变）
    #   spring 骨骼 -> 保留
    #   hips（骨盆，humanoid 但不在 TPOSE_TARGETS）-> 保留
    #   其余（非 humanoid 非 spring）-> rest 清零（rotation = identity）

    # 先决定每个节点的新局部旋转
    new_local = {}

    # 用于 T-pose 的：需要父世界旋转（新值），因此按层级从根开始处理
    order = sorted(range(total_nodes), key=lambda i: (1 if i in parent_of else 0, i))

    # 先做一次简单的分类，再逐层处理 T-pose
    # 为了正确性：自顶向下处理，父的新世界旋转已知后再算子的局部旋转
    new_world_rot = {}

    def process(i):
        p = parent_of.get(i)
        r_local_old = local_rot(i)
        if p is not None:
            parent_world_new = new_world_rot[p]
        else:
            parent_world_new = (0.0, 0.0, 0.0, 1.0)

        # 找到该节点对应的 humanoid slot
        slot = None
        for s, idx in humanoid_slots.items():
            if idx == i:
                slot = s
                break

        if slot in TPOSE_TARGETS:
            # 标准 T-pose：把局部 +Y 轴（世界）转到目标方向
            target = TPOSE_TARGETS[slot]
            # 当前局部 +Y 轴在世界空间的方向（若保持旧局部旋转）
            cur_dir = qrot(qmul(parent_world_new, r_local_old), (0, 1, 0))
            delta = quat_from_to(cur_dir, target)
            new_r_world = qmul(delta, qmul(parent_world_new, r_local_old))
            # 转回局部：局部 = 父世界逆 * 新世界
            inv_parent = qconj(parent_world_new)
            r_local_new = qnorm(qmul(inv_parent, new_r_world))
            new_local[i] = r_local_new
            new_world_rot[i] = new_r_world
        elif slot in TPOSE_CHILD_SLOTS:
            # 手指/脚趾：保持局部旋转（跟随父链）
            new_local[i] = r_local_old
            new_world_rot[i] = qmul(parent_world_new, r_local_old)
        elif i in humanoid_node_set:
            # 其他 humanoid（hips、眼睛、jaw 等）：保留
            new_local[i] = r_local_old
            new_world_rot[i] = qmul(parent_world_new, r_local_old)
        elif i in spring_nodes:
            # spring：保留（物理不乱）
            new_local[i] = r_local_old
            new_world_rot[i] = qmul(parent_world_new, r_local_old)
        else:
            # aux：rest 清零
            new_local[i] = (0.0, 0.0, 0.0, 1.0)
            new_world_rot[i] = parent_world_new

    # 自顶向下处理（父在子前）
    levels = {}
    def level_of(i):
        p = parent_of.get(i)
        if p is None:
            return 0
        if i not in levels:
            levels[i] = level_of(p) + 1
        return levels[i]

    for i in range(total_nodes):
        level_of(i)
    ordered = sorted(range(total_nodes), key=lambda i: (level_of(i), i))
    for i in ordered:
        process(i)

    tpose_set = sum(1 for i in range(total_nodes) if any(humanoid_slots.get(s) == i for s in TPOSE_TARGETS))
    spring_kept = sum(1 for i in range(total_nodes) if i in spring_nodes and i not in humanoid_node_set)
    aux_cleared = 0
    for i in range(total_nodes):
        if i in humanoid_node_set or i in spring_nodes:
            continue
        aux_cleared += 1
        # 已经是 identity 了（process 里设置）

    # ---------- 5. 写回 rotation ----------
    for i in range(total_nodes):
        r = new_local[i]
        nodes[i]['rotation'] = [round(v, 6) for v in r]

    # ---------- 6. 重算 IBM（inverseBindMatrices）----------
    # 网格绑定：顶点 = Σ jointMatrix * IBM * bindVertex。
    # rest 变化后 IBM 必须 = 新 rest 世界矩阵的逆，否则网格会变形。
    # 需要世界矩阵（含平移），因此这里自顶向下算 4x4。

    def mat4_from_trs(t, r, s):
        qx, qy, qz, qw = r
        # 旋转矩阵
        m = [
            [1 - 2 * (qy * qy + qz * qz), 2 * (qx * qy - qz * qw), 2 * (qx * qz + qy * qw), t[0]],
            [2 * (qx * qy + qz * qw), 1 - 2 * (qx * qx + qz * qz), 2 * (qy * qz - qx * qw), t[1]],
            [2 * (qx * qz - qy * qw), 2 * (qy * qz + qx * qw), 1 - 2 * (qx * qx + qy * qy), t[2]],
            [0, 0, 0, 1],
        ]
        # 应用 scale
        for r_ in range(3):
            for c_ in range(3):
                m[r_][c_] *= s[c_]
        return m

    def mat4_mul(a, b):
        return [[sum(a[r_][k] * b[k][c_] for k in range(4)) for c_ in range(4)] for r_ in range(4)]

    def mat4_inv(m):
        # 高斯消元求逆
        aug = [row[:] + [1.0 if i == j else 0.0 for j in range(4)] for i, row in enumerate(m)]
        for col in range(4):
            pivot = aug[col][col]
            if abs(pivot) < 1e-12:
                # 找下面有非零的行
                for r_ in range(col + 1, 4):
                    if abs(aug[r_][col]) > 1e-12:
                        aug[col], aug[r_] = aug[r_], aug[col]
                        pivot = aug[col][col]
                        break
            if abs(pivot) < 1e-12:
                return [[1.0 if i == j else 0.0 for j in range(4)] for i in range(4)]
            inv_p = 1.0 / pivot
            aug[col] = [v * inv_p for v in aug[col]]
            for r_ in range(4):
                if r_ == col:
                    continue
                factor = aug[r_][col]
                if abs(factor) < 1e-15:
                    continue
                aug[r_] = [aug[r_][k] - factor * aug[col][k] for k in range(8)]
        return [row[4:] for row in aug]

    world_mat = {}
    for i in ordered:
        t = nodes[i].get('translation', [0, 0, 0])
        r = new_local.get(i, local_rot(i))
        s = nodes[i].get('scale', [1, 1, 1])
        local_m = mat4_from_trs(t, r, s)
        p = parent_of.get(i)
        if p is None:
            world_mat[i] = local_m
        else:
            world_mat[i] = mat4_mul(world_mat[p], local_m)

    # 重算 IBM 并写回 bin
    for skin in gltf.get('skins', []):
        ibm_acc_idx = skin.get('inverseBindMatrices')
        if ibm_acc_idx is None:
            continue
        acc = gltf['accessors'][ibm_acc_idx]
        if acc.get('componentType') != CT_FLOAT or acc.get('type') != 'MAT4':
            continue
        joints = skin.get('joints', [])
        bv = gltf['bufferViews'][acc['bufferView']]
        base_offset = bv.get('byteOffset', 0) + acc.get('byteOffset', 0)
        # 用 numpy 直接改 bin
        try:
            import numpy as np
        except ImportError:
            np = None
        if np is not None:
            buf = bytearray(bin_chunk)
            arr = np.frombuffer(buf, dtype=np.float32, count=len(joints) * 16, offset=base_offset)
            for k, j in enumerate(joints):
                m = world_mat[j]
                inv = mat4_inv(m)
                # glTF MAT4 是列主序
                col_major = [inv[c][r] for r in range(4) for c in range(4)]
                arr[k * 16:(k + 1) * 16] = col_major
            bin_chunk = bytes(buf)
        else:
            # 纯 struct 回写
            buf = bytearray(bin_chunk)
            for k, j in enumerate(joints):
                m = world_mat[j]
                inv = mat4_inv(m)
                col_major = struct.pack('<16f', *[inv[c][r] for r in range(4) for c in range(4)])
                off = base_offset + k * 64
                buf[off:off + 64] = col_major
            bin_chunk = bytes(buf)

    # ---------- 7. 写出 ----------
    write_glb(dst, gltf, bin_chunk)

    print(f'SPRING_NODES: {spring_kept} (保留 rest)')
    print(f'TPOSE_SET: {tpose_set} humanoid bones')
    print(f'CLEARED_AUX: {aux_cleared} nodes')
    print(f'TPOSE_REST_OK -> {dst} ({total_nodes} nodes, {len(gltf.get("skins", []))} skins)')


if __name__ == '__main__':
    main()