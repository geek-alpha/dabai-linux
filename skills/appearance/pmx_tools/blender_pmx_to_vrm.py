"""
PMX/PMD → VRM1 转换脚本（由 appearance 技能 pmx_to_vrm 工具驱动，Blender 后台执行）

流程（v34 最终方案，实测验证：莉丽拉 正面朝向、手臂自然下垂、Mixamo 动作正常）：
1. 高手 setup（mmd_tools 导入 + VRM1 humanoid/表情/meta/MToon）
2. hips = 腰（骨盆，层级合法；默认 auto 会把 hips 填成 センター，腿位移被乘 0 → 腿僵）
3. **绕 Z 180°（叠加，模型转身）**：Blender Z-up 场景的 Z 旋转导出后 = glTF 绕 Y 180°，
   MMD 模型（面向 +Z、左臂 +X）→ 面向 -Z、左臂 -X（与 VRM0 好模型文件同构）
4. 导出原始 VRM
5. 由 pmx_impl.py 调用 vrm_rest_final.py 做 JSON 层后处理：
   - humanoid 骨骼清 I + 位置保持（前端驱动直接生效）
   - 非 humanoid 骨骼重定向（世界变换不变，无变形）
   - 重算 IBM（REST 渲染零变形）

为什么绕 Z 180° 而不是 X 镜像：
- 前端所有驱动（applyVrmRestPose/Mixamo/走路）为「VRM0 + rotateVRM0」设计：
  模型文件面向 -Z、左臂 -X（构造时 P=I、boneRot=I）→ 前端 rotate（scene 绕 Y 180°）
  → 面向 +Z（朝相机）+ 左臂 +X → 驱动方向全部正确。
- X 镜像是反射（det=-1）会左右手性互换（反手/扭曲）；绕 Z 180° 是纯旋转（det=+1），
  左右正确。Blender 里绕 Y 180° 会因导出轴转换变成 glTF 绕 Z 180°（倒立），必须绕 Z。

用法：
    blender --background --python blender_pmx_to_vrm.py -- <pmx_path> <vrm_path>

说明：
    高手工具（run_pmx_to_vrm1_setup 等）位于本文件同目录（pmx_tools/），
    本技能自包含，不依赖外部仓库。
"""
import os
import sys

SKILL_TOOLS = os.path.dirname(os.path.abspath(__file__))

bpy = __import__("bpy")


def _load_tool(name):
    path = os.path.join(SKILL_TOOLS, name)
    ns = {"__file__": path}
    exec(compile(open(path, encoding="utf-8").read(), path, "exec"), ns)
    return ns


def main():
    args = sys.argv[sys.argv.index("--") + 1:]
    if len(args) < 2:
        print("USAGE: blender --background --python blender_pmx_to_vrm.py -- <pmx_path> <vrm_path>")
        sys.exit(1)
    pmx_path = args[0]
    vrm_path = args[1]

    if not os.path.isfile(pmx_path):
        print(f"ERROR: PMX 文件不存在：{pmx_path}")
        sys.exit(1)

    vrm_dir = os.path.dirname(vrm_path)
    if vrm_dir and not os.path.isdir(vrm_dir):
        os.makedirs(vrm_dir, exist_ok=True)

    # ---------- 1. 清场 + 启用插件 ----------
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.preferences.addon_enable(module="mmd_tools")
    bpy.ops.preferences.addon_enable(module="vrm")

    # ---------- 2. 高手完整 setup ----------
    run_ns = _load_tool("run_pmx_to_vrm1_setup.py")
    run_pmx_to_vrm1_setup = run_ns["run_pmx_to_vrm1_setup"]

    result = run_pmx_to_vrm1_setup(
        filepath=pmx_path,
        dry_run=False,
        scale=0.08,
    )
    print("SETUP_RESULT:", result)

    if not result.get("applied"):
        print("SETUP_FAILED:", result.get("error"))
        sys.exit(1)

    arm_name = result["armature_object_name"]
    arm = bpy.data.objects[arm_name]
    bpy.context.view_layer.objects.active = arm

    # ---------- 3. hips = 腰（骨盆，层级合法） ----------
    hb = arm.data.vrm_addon_extension.vrm1.humanoid.human_bones
    before_hips = hb.hips.node.bone_name
    print("HIPS_BEFORE:", before_hips)

    target_hips = None
    for b in arm.data.bones:
        bare = b.name.split(" (")[0].strip()
        if bare == "腰":
            target_hips = b.name
            break
    if target_hips is None:
        print("ERROR: 未找到「腰」骨骼")
        sys.exit(1)

    if before_hips != target_hips:
        hb.hips.node.bone_name = target_hips
        print("HIPS_FIXED:", before_hips, "->", target_hips)
    else:
        print("HIPS_OK:", target_hips)

    # ---------- 4. 绕 Z 180°（叠加）＝模型转身 ----------
    # Blender 场景 Z-up（模型头朝 +Z），导出 Z-up→Y-up 转换后，Blender 的 Z 旋转
    # 恰好对应 glTF 的 Y 旋转（绕模型垂直轴转身）：
    #   MMD 模型（面向 +Z、左臂 +X）→ 面向 -Z、左臂 -X（与 VRM0 好模型文件同构）
    # 前端 rotateVRM0 式处理（scene 绕 Y 180°）后 → 面向 +Z（朝相机）+ 左臂 +X，
    # 所有驱动（applyVrmRestPose/Mixamo/走路）方向与 VRM0 模型完全一致。
    # 注意：不能做 X 镜像（反射 det=-1 会左右手性互换 = 反手）；绕 Z 180° 是纯旋转（det=+1）。
    from mathutils import Euler
    objs = [arm] + [o for o in bpy.data.objects if o.type == 'MESH']
    for obj in objs:
        bpy.context.view_layer.objects.active = obj
        obj.select_set(True)
        if obj.type == 'MESH' and obj.data.users > 1:
            obj.data = obj.data.copy()
        # 叠加（不覆盖）局部 Z 180°：保留初始站立旋转，只转身
        obj.rotation_euler.rotate(Euler((0, 0, 3.141592653589793), 'XYZ'))
        bpy.ops.object.transform_apply(location=False, rotation=True, scale=False)
        obj.select_set(False)
    print("RY_180: applied to", len(objs), "objects")

    # ---------- 5. 导出原始 VRM（由 pmx_impl.py 调用 vrm_rest_final.py 后处理） ----------
    bpy.context.view_layer.objects.active = arm
    try:
        bpy.ops.export_scene.vrm(filepath=vrm_path)
        print("EXPORT_DONE:", vrm_path, "exists=", os.path.isfile(vrm_path))
    except Exception as e:
        print("EXPORT_EXC:", repr(e))

    if not os.path.isfile(vrm_path):
        print("EXPORT_FAILED: 文件未生成")
        sys.exit(1)

    print("CONVERT_OK:", vrm_path)


main()