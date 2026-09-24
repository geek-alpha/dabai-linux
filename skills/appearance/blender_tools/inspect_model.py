"""
模型体检：统计当前 .blend / 导入模型的物体、网格、骨骼、材质、贴图。

用法：
    blender --background <file.blend> --python inspect_model.py -- [out.json]
    blender --background --python inspect_model.py -- --import <model.vrm> [out.json]

输出 JSON 到 stdout（或指定文件），字段：
    objects / meshes / armatures / materials / images / summary
"""
import json
import os
import sys

import bpy


def _argv():
    if "--" in sys.argv:
        return sys.argv[sys.argv.index("--") + 1:]
    return []


def _import_any(path):
    ext = os.path.splitext(path)[1].lower()
    if ext in (".vrm", ".glb", ".gltf"):
        bpy.ops.import_scene.gltf(filepath=path)
    elif ext in (".pmx", ".pmd"):
        bpy.ops.mmd_tools.import_model(filepath=path, scale=0.08,
                                       types={"MESH", "ARMATURE", "MORPHS"})
    elif ext == ".fbx":
        bpy.ops.import_scene.fbx(filepath=path)
    elif ext == ".obj":
        bpy.ops.wm.obj_import(filepath=path)
    else:
        raise SystemExit(f"不支持的格式: {ext}")


def collect():
    objs = []
    for o in bpy.data.objects:
        item = {
            "name": o.name,
            "type": o.type,
            "parent": o.parent.name if o.parent else None,
            "location": [round(v, 4) for v in o.location],
            "scale": [round(v, 4) for v in o.scale],
        }
        if o.type == "MESH":
            item["verts"] = len(o.data.vertices)
            item["polys"] = len(o.data.polygons)
            item["tris"] = sum(len(p.vertices) - 2 for p in o.data.polygons)
            item["materials"] = [m.name for m in o.data.materials if m]
            item["shape_keys"] = len(o.data.shape_keys.key_blocks) if o.data.shape_keys else 0
            item["vertex_groups"] = len(o.vertex_groups)
        elif o.type == "ARMATURE":
            item["bones"] = len(o.data.bones)
            item["bone_names"] = [b.name for b in o.data.bones]
        objs.append(item)

    mats = []
    for m in bpy.data.materials:
        entry = {"name": m.name, "use_nodes": m.use_nodes}
        if m.use_nodes:
            entry["nodes"] = [n.type for n in m.node_tree.nodes]
        mats.append(entry)

    imgs = [{"name": i.name, "size": list(i.size), "packed": bool(i.packed_file)}
            for i in bpy.data.images]

    meshes = [o for o in objs if o["type"] == "MESH"]
    arms = [o for o in objs if o["type"] == "ARMATURE"]
    return {
        "objects": objs,
        "materials": mats,
        "images": imgs,
        "summary": {
            "object_count": len(objs),
            "mesh_count": len(meshes),
            "armature_count": len(arms),
            "total_verts": sum(m.get("verts", 0) for m in meshes),
            "total_tris": sum(m.get("tris", 0) for m in meshes),
            "total_bones": sum(a.get("bones", 0) for a in arms),
            "material_count": len(mats),
            "image_count": len(imgs),
        },
    }


def main():
    args = _argv()
    out_path = None
    if args and args[0] == "--import":
        if len(args) < 2:
            raise SystemExit("--import 需要文件路径")
        _import_any(args[1])
        if len(args) > 2:
            out_path = args[2]
    elif args:
        out_path = args[0]

    data = collect()
    text = json.dumps(data, ensure_ascii=False, indent=2)
    if out_path:
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(text)
        print(f"[inspect_model] 已写入 {out_path}")
    else:
        print(text)


main()
