"""
无头渲染预览图：多角度环绕 + 可选线框，输出 PNG。

用法：
    blender --background <file.blend> --python render_preview.py -- <out_dir> [--views 4] [--res 800] [--wire]

参数：
    out_dir   输出目录（自动创建）
    --views N 环绕角度数（默认 4：前/右/后/左）
    --res N   分辨率边长（默认 800）
    --wire    额外渲染一张线框
    --engine  渲染引擎（默认 BLENDER_EEVEE_NEXT，可选 CYCLES）
"""
import math
import os
import sys

import bpy
from mathutils import Vector


def _argv():
    if "--" not in sys.argv:
        return []
    return sys.argv[sys.argv.index("--") + 1:]


def parse_args(args):
    opts = {"out_dir": None, "views": 4, "res": 800, "wire": False,
            "engine": "BLENDER_EEVEE"}
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--views":
            opts["views"] = int(args[i + 1]); i += 2
        elif a == "--res":
            opts["res"] = int(args[i + 1]); i += 2
        elif a == "--engine":
            opts["engine"] = args[i + 1]; i += 2
        elif a == "--wire":
            opts["wire"] = True; i += 1
        elif opts["out_dir"] is None:
            opts["out_dir"] = a; i += 1
        else:
            i += 1
    if not opts["out_dir"]:
        raise SystemExit("需要 out_dir 参数")
    return opts


def scene_bounds():
    """所有可见网格的世界包围盒。"""
    pts = []
    for o in bpy.data.objects:
        if o.type != "MESH" or o.hide_render:
            continue
        for c in o.bound_box:
            pts.append(o.matrix_world @ Vector(c))
    if not pts:
        return Vector((0, 0, 0)), 1.0
    lo = Vector((min(p.x for p in pts), min(p.y for p in pts), min(p.z for p in pts)))
    hi = Vector((max(p.x for p in pts), max(p.y for p in pts), max(p.z for p in pts)))
    center = (lo + hi) / 2
    size = max((hi - lo).length, 0.001)
    return center, size


def setup_camera(center, size, angle_deg):
    cam_data = bpy.data.cameras.new("PreviewCam")
    cam = bpy.data.objects.new("PreviewCam", cam_data)
    bpy.context.scene.collection.objects.link(cam)

    dist = size * 1.6
    rad = math.radians(angle_deg)
    cam.location = center + Vector((math.sin(rad) * dist, -math.cos(rad) * dist, size * 0.25))
    direction = center - cam.location
    cam.rotation_euler = direction.to_track_quat("-Z", "Y").to_euler()
    cam_data.lens = 50
    bpy.context.scene.camera = cam
    return cam


def setup_light(center, size):
    light_data = bpy.data.lights.new("PreviewSun", type="SUN")
    light_data.energy = 3.0
    light = bpy.data.objects.new("PreviewSun", light_data)
    light.location = center + Vector((size, -size, size * 2))
    light.rotation_euler = (math.radians(50), 0, math.radians(30))
    bpy.context.scene.collection.objects.link(light)

    world = bpy.data.worlds.get("World") or bpy.data.worlds.new("World")
    bpy.context.scene.world = world
    world.use_nodes = True
    bg = world.node_tree.nodes.get("Background")
    if bg:
        bg.inputs[0].default_value = (0.05, 0.05, 0.08, 1.0)
        bg.inputs[1].default_value = 1.0


def main():
    opts = parse_args(_argv())
    os.makedirs(opts["out_dir"], exist_ok=True)

    scene = bpy.context.scene
    scene.render.engine = opts["engine"]
    scene.render.resolution_x = opts["res"]
    scene.render.resolution_y = opts["res"]
    scene.render.film_transparent = False
    scene.render.image_settings.file_format = "PNG"

    center, size = scene_bounds()
    setup_light(center, size)

    n = max(1, opts["views"])
    for i in range(n):
        angle = 360.0 * i / n
        cam = setup_camera(center, size, angle)
        scene.render.filepath = os.path.join(opts["out_dir"], f"view_{i:02d}_{int(angle)}deg.png")
        bpy.ops.render.render(write_still=True)
        print(f"[render_preview] {scene.render.filepath}")
        bpy.data.objects.remove(cam, do_unlink=True)

    if opts["wire"]:
        for o in bpy.data.objects:
            if o.type == "MESH":
                o.show_wire = True
                o.show_all_edges = True
        cam = setup_camera(center, size, 0)
        scene.render.filepath = os.path.join(opts["out_dir"], "wire.png")
        bpy.ops.render.render(write_still=True)
        print(f"[render_preview] {scene.render.filepath}")


main()
