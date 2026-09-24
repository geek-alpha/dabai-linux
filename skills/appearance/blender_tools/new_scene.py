"""
新建干净场景：清空默认物体，可选加基础网格/骨架。

用法：
    blender --background --python new_scene.py -- <out.blend> [--cube|--sphere|--plane] [--armature]

参数：
    out.blend   保存路径
    --cube      加一个立方体
    --sphere    加一个 UV 球
    --plane     加一个平面
    --armature  加一个基础人形骨架（hips/spine/chest/neck/head + 双臂）
"""
import os
import sys

import bpy


def _argv():
    if "--" not in sys.argv:
        return []
    return sys.argv[sys.argv.index("--") + 1:]


def clear_scene():
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)
    for block in (bpy.data.meshes, bpy.data.materials, bpy.data.armatures,
                  bpy.data.cameras, bpy.data.lights):
        for item in list(block):
            if item.users == 0:
                block.remove(item)


def add_primitive(kind):
    if kind == "cube":
        bpy.ops.mesh.primitive_cube_add(size=1)
    elif kind == "sphere":
        bpy.ops.mesh.primitive_uv_sphere_add(radius=0.5, segments=32, ring_count=16)
    elif kind == "plane":
        bpy.ops.mesh.primitive_plane_add(size=2)
    return bpy.context.active_object


def add_basic_armature():
    """最小人形骨架，坐标按 VRM 惯例（Y-up 场景里 Blender 是 Z-up，这里用 Z-up）。"""
    arm_data = bpy.data.armatures.new("Armature")
    arm = bpy.data.objects.new("Armature", arm_data)
    bpy.context.scene.collection.objects.link(arm)
    bpy.context.view_layer.objects.active = arm
    bpy.ops.object.mode_set(mode="EDIT")

    eb = arm_data.edit_bones
    spec = [
        ("hips",   (0, 0, 0.90), (0, 0, 1.00), None),
        ("spine",  (0, 0, 1.00), (0, 0, 1.15), "hips"),
        ("chest",  (0, 0, 1.15), (0, 0, 1.35), "spine"),
        ("neck",   (0, 0, 1.35), (0, 0, 1.45), "chest"),
        ("head",   (0, 0, 1.45), (0, 0, 1.65), "neck"),
        ("l_upper_arm", (0.15, 0, 1.32), (0.45, 0, 1.32), "chest"),
        ("l_lower_arm", (0.45, 0, 1.32), (0.72, 0, 1.32), "l_upper_arm"),
        ("l_hand",      (0.72, 0, 1.32), (0.85, 0, 1.32), "l_lower_arm"),
        ("r_upper_arm", (-0.15, 0, 1.32), (-0.45, 0, 1.32), "chest"),
        ("r_lower_arm", (-0.45, 0, 1.32), (-0.72, 0, 1.32), "r_upper_arm"),
        ("r_hand",      (-0.72, 0, 1.32), (-0.85, 0, 1.32), "r_lower_arm"),
        ("l_upper_leg", (0.09, 0, 0.90), (0.09, 0, 0.50), "hips"),
        ("l_lower_leg", (0.09, 0, 0.50), (0.09, 0, 0.08), "l_upper_leg"),
        ("l_foot",      (0.09, 0, 0.08), (0.09, -0.15, 0.02), "l_lower_leg"),
        ("r_upper_leg", (-0.09, 0, 0.90), (-0.09, 0, 0.50), "hips"),
        ("r_lower_leg", (-0.09, 0, 0.50), (-0.09, 0, 0.08), "r_upper_leg"),
        ("r_foot",      (-0.09, 0, 0.08), (-0.09, -0.15, 0.02), "r_lower_leg"),
    ]
    for name, head, tail, parent in spec:
        b = eb.new(name)
        b.head = head
        b.tail = tail
        if parent:
            b.parent = eb[parent]
            b.use_connect = False

    bpy.ops.object.mode_set(mode="OBJECT")
    return arm


def main():
    args = _argv()
    if not args:
        raise SystemExit("需要 out.blend 参数")
    out_path = args[0]
    flags = set(a for a in args[1:] if a.startswith("--"))

    clear_scene()

    if "--cube" in flags:
        add_primitive("cube")
    elif "--sphere" in flags:
        add_primitive("sphere")
    elif "--plane" in flags:
        add_primitive("plane")

    if "--armature" in flags:
        add_basic_armature()

    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    bpy.ops.wm.save_as_mainfile(filepath=out_path)
    print(f"[new_scene] 已保存 {out_path}")


main()
