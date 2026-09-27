import bpy
import bmesh

SRC = r"D:\AI\dabai\models\白头凤_print.glb"
OUT_STL = r"D:\AI\dabai\data\baifengfeng_100mm.stl"
TARGET_H = 0.1
VOXEL = 0.0004
DROP = {"Icosphere"}

bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.import_scene.gltf(filepath=SRC)

for name in DROP:
    o = bpy.data.objects.get(name)
    if o:
        bpy.data.objects.remove(o, do_unlink=True)
        print("DROPPED", name)

meshes = [o for o in bpy.data.objects if o.type == "MESH"]
bpy.ops.object.select_all(action="DESELECT")
for o in meshes:
    o.select_set(True)
bpy.context.view_layer.objects.active = meshes[0]
bpy.ops.object.convert(target="MESH")
bpy.ops.object.join()
obj = bpy.context.view_layer.objects.active
print("JOINED", obj.name, "verts", len(obj.data.vertices), "polys", len(obj.data.polygons))

bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)
bb = [obj.matrix_world @ v.co for v in obj.data.vertices]
mnz = min(v.z for v in bb)
mxz = max(v.z for v in bb)
s = TARGET_H / (mxz - mnz)
obj.scale = (s, s, s)
bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)

bb = [obj.matrix_world @ v.co for v in obj.data.vertices]
mn = [min(v[i] for v in bb) for i in range(3)]
mx = [max(v[i] for v in bb) for i in range(3)]
obj.location = (-(mn[0] + mx[0]) / 2, -(mn[1] + mx[1]) / 2, -mn[2])
bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)

bb = [obj.matrix_world @ v.co for v in obj.data.vertices]
mn = [min(v[i] for v in bb) for i in range(3)]
mx = [max(v[i] for v in bb) for i in range(3)]
print("SCALED_BBOX_MM min=%s max=%s size=%s" % (
    ["%.2f" % (v * 1000) for v in mn],
    ["%.2f" % (v * 1000) for v in mx],
    ["%.2f" % ((mx[i] - mn[i]) * 1000) for i in range(3)]))

mod = obj.modifiers.new("remesh", "REMESH")
mod.mode = "VOXEL"
mod.voxel_size = VOXEL
mod.adaptivity = 0.0
bpy.ops.object.modifier_apply(modifier=mod.name)
print("AFTER_REMESH verts", len(obj.data.vertices), "polys", len(obj.data.polygons))

bm = bmesh.new()
bm.from_mesh(obj.data)
nonman_e = sum(1 for e in bm.edges if not e.is_manifold)
bound_e = sum(1 for e in bm.edges if e.is_boundary)
print("MANIFOLD_CHECK nonmanifold_edges=%d boundary_edges=%d faces=%d" % (nonman_e, bound_e, len(bm.faces)))
bm.free()

bpy.ops.wm.stl_export(filepath=OUT_STL, export_selected_objects=False, global_scale=1.0)
print("EXPORTED", OUT_STL)
