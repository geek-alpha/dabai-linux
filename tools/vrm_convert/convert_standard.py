"""
convert_standard.py —— 标准 PMX → VRM1 转换流程（唯一入口，替代全部 convert_luotianyi_v*.py）

流程（固化自 v19 验证方案）：
    1. 清场 + 启用 mmd_tools / vrm 插件
    2. run_pmx_to_vrm1_setup：导入 PMX + 自动 humanoid 映射 + 表情 + meta + MToon
    3. hips 修正为「腰」（骨盆，层级合法）
    4. 官方导出原始 VRM（rest 为 MMD 原始姿态，但 humanoid 映射正确）
    5. vrm_tpose_rest.py 后处理：JSON 层 rest 标准化为 T-pose
       - humanoid 骨骼 → 标准 T-pose 方向（保留绕轴扭转）
       - 非 humanoid 非 spring 骨骼 → rest 清零
       - spring 骨骼 → 保留（物理不乱）
       - 重算 IBM + 调整平移保持世界位置

用法（Blender 后台执行）：
    blender --background --python convert_standard.py -- <pmx_path> <vrm_path> [model_name]

示例：
    blender --background --python convert_standard.py -- "C:/xxx/洛天依.pmx" "D:/AI/dabai/models/洛天依_v20.vrm" 洛天依
"""
from __future__ import annotations

import os
import subprocess
import sys

# ---------- 常量 ----------
SKILL_TOOLS = r"D:\AI\dabai\_ref_miramocha\skills\mmd-pmx-to-vrm1\tools"
TPOSE_SCRIPT = r"D:\AI\dabai\tools\vrm_tpose_rest.py"
PYTHON_EXE = r"C:\Users\wangxingfeng\anaconda3\python.exe"
SCALE = 0.08


def _load_tool(name: str) -> dict:
    path = os.path.join(SKILL_TOOLS, name)
    ns = {"__file__": path}
    exec(compile(open(path, encoding="utf-8").read(), path, "exec"), ns)
    return ns


def main() -> int:
    # ---------- 参数 ----------
    args = [a for a in sys.argv if not a.startswith("--")]
    if len(args) < 3:
        print("USAGE: convert_standard.py <pmx_path> <vrm_path> [model_name]")
        return 1
    pmx_path = args[1]
    vrm_path = args[2]
    model_name = args[3] if len(args) > 3 else os.path.splitext(os.path.basename(pmx_path))[0]

    if not os.path.isfile(pmx_path):
        print(f"ERROR: PMX 不存在: {pmx_path}")
        return 1

    raw_path = vrm_path.replace(".vrm", "_raw.vrm")

    bpy = __import__("bpy")

    # ---------- 1. 清场 + 启用插件 ----------
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.preferences.addon_enable(module="mmd_tools")
    bpy.ops.preferences.addon_enable(module="vrm")

    # ---------- 2. 完整 setup（导入 + humanoid + 表情 + meta + MToon） ----------
    run_ns = _load_tool("run_pmx_to_vrm1_setup.py")
    result = run_ns["run_pmx_to_vrm1_setup"](
        filepath=pmx_path,
        dry_run=False,
        scale=SCALE,
    )
    print("SETUP_RESULT:", result)

    if not result.get("applied"):
        print("SETUP_FAILED:", result.get("error"))
        return 1

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
        return 1

    if before_hips != target_hips:
        hb.hips.node.bone_name = target_hips
        print("HIPS_FIXED:", before_hips, "->", target_hips)
    else:
        print("HIPS_OK:", target_hips)

    # ---------- 4. 官方导出原始 VRM ----------
    try:
        bpy.ops.export_scene.vrm(filepath=raw_path)
        print("EXPORT_RAW_DONE:", raw_path, "exists=", os.path.isfile(raw_path))
    except Exception as e:
        print("EXPORT_RAW_EXC:", repr(e))

    if not os.path.isfile(raw_path):
        print("EXPORT_FAILED: 文件未生成")
        return 1

    # ---------- 5. T-pose rest 标准化后处理 ----------
    if os.path.isfile(PYTHON_EXE) and os.path.isfile(TPOSE_SCRIPT):
        np = subprocess.run(
            [PYTHON_EXE, TPOSE_SCRIPT, raw_path, vrm_path],
            capture_output=True, text=True, timeout=300,
            encoding="utf-8", errors="replace",
        )
        out = (np.stdout or "") + (np.stderr or "")
        print("TPOSE_OUT:", out.strip()[-800:])
        if "TPOSE_REST_OK" in out and os.path.isfile(vrm_path):
            os.remove(raw_path)
            print(f"CONVERT_OK: {vrm_path} (model={model_name})")
            return 0
        print("TPOSE_FAILED, 保留 raw:", raw_path)
        return 1

    print("WARN: python 或 tpose 脚本缺失，直接用 raw")
    os.replace(raw_path, vrm_path)
    print(f"CONVERT_OK: {vrm_path} (model={model_name}, no-tpose)")
    return 0


if __name__ == "__main__":
    sys.exit(main())