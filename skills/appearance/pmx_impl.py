"""PMX 转 VRM 模型转换（pmx_impl）—— 调用 Blender 后台把 PMX/PMD 转成标准 VRM。

流程（v34 最终方案，实测验证：莉丽拉 正面朝向、手臂自然下垂、Mixamo 动作正常）：
1. Blender 后台跑 pmx_tools/blender_pmx_to_vrm.py：
   mmd_tools 导入 PMX + VRM1 setup（humanoid/表情/meta/MToon）+ hips=腰
   + **绕 Z 180° 转身**（Blender Z 旋转导出后 = glTF 绕 Y 180°：
     面向 -Z、左臂 -X，与 VRM0 好模型文件同构）+ 导出原始 VRM
2. JSON 层后处理（pmx_tools/vrm_rest_final.py）：
   - humanoid 骨骼清 I + 位置保持：前端每帧驱动直接生效（走路/动作正常）
   - 非 humanoid 骨骼（twist/手指/装饰/发饰）重定向保持世界变换：无变形
   - 重算 IBM：REST 渲染 = 原始网格（零变形）

⚠️ 前端要求（重要）：加载后需与 VRM0 相同朝向修正（03_model_load_gltf_vrm.ts 已加）：
    VRMUtils.rotateVRM0(App.vrm);
    if (App.vrm.meta?.metaVersion === '1') {
      App.vrm.scene.rotation.y = Math.PI;
    }
    否则前端驱动的旋转方向相反（applyVrmRestPose 手上举、Mixamo 动作反向）。

效果：加载即手在肩同高水平（T-pose bind）；前端 applyVrmRestPose 后手自然下垂；
走路/动作驱动正常；手指/装饰跟随骨骼无扭曲。

⚠️ 前端要求（重要）：
    MMD 转换的 VRM1 模型需要与 VRM0 相同的朝向修正，否则前端驱动的旋转
    方向相反（applyVrmRestPose 手上举、Mixamo 动作反向）。
    前端 03_model_load_gltf_vrm.ts 已加：
        VRMUtils.rotateVRM0(App.vrm);
        if (App.vrm.meta?.metaVersion === '1') {
          App.vrm.scene.rotation.y = Math.PI;
        }
    该改动对项目内全部在用模型（11 个 VRM0 + 莉丽拉 VRM1）安全。

表情说明：若源 PMX 只有「材质切换型 morph」（无顶点 morph，如莉丽拉用
FACE BLINK/HAPPY 等材质），VRM 无法表达材质切换表情——VRM1 会注册表情名
（preset 空绑定），但无视觉变化。这是源模型固有限制。

本技能自包含：所有执行代码在 skills/appearance/pmx_tools/ 下，不依赖外部仓库。

依赖（外部环境，仅一项）:
    - Blender 可执行文件（需装 mmd_tools 与 VRM Add-on 插件）：
      Windows 默认探测 D:\\blender\\blender.exe，Linux/macOS 探测 PATH；
      也可用环境变量 DABAI_BLENDER 指定

用法（工具）:
    pmx_to_vrm(pmx_path="C:/xx/模型.pmx", vrm_path="D:/AI/dabai/models/模型.vrm")
    vrm_path 省略时默认输出到 D:/AI/dabai/models/ 下同名 .vrm。
"""
from __future__ import annotations

import glob
import os
import shutil
import subprocess


def _find_blender() -> str:
    """定位 Blender：环境变量 DABAI_BLENDER → 常见安装位置 → PATH（跨平台）。"""
    for key in ("DABAI_BLENDER", "BLENDER_EXE"):
        env = (os.environ.get(key) or "").strip().strip('"')
        if env and os.path.isfile(env):
            return env
    cands = []
    if os.name == "nt":
        cands += [r"D:\blender\blender.exe"]
        cands += sorted(glob.glob(r"C:\Program Files\Blender Foundation\Blender*\blender.exe"))
        cands += sorted(glob.glob(r"D:\blender*\blender.exe"))
    else:
        p = shutil.which("blender")
        if p:
            cands.append(p)
        cands += ["/usr/bin/blender", "/usr/local/bin/blender", "/snap/bin/blender",
                  "/Applications/Blender.app/Contents/MacOS/Blender"]
    for c in cands:
        if c and os.path.isfile(c):
            return c
    return ""


# Blender 路径：找不到时 convert_pmx 会明确报错，而不是拿空路径去起进程
BLENDER_EXE = _find_blender()
PMX_TOOLS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pmx_tools")
CONVERT_SCRIPT = os.path.join(PMX_TOOLS, "blender_pmx_to_vrm.py")
FINAL_SCRIPT = os.path.join(PMX_TOOLS, "vrm_rest_final.py")
# 默认输出目录：项目根 models/（不再硬编码盘符）
MODELS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "models")


def _default_vrm_path(pmx_path: str) -> str:
    base = os.path.splitext(os.path.basename(pmx_path))[0]
    return os.path.join(MODELS_DIR, base + ".vrm")


def _run(cmd: list, timeout: int) -> str:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                              encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        return "转换超时（>10 分钟），模型可能过大或 Blender 卡住，请检查后重试。"
    except Exception as e:
        return f"启动进程失败：{e.__class__.__name__}: {e}"
    return (proc.stdout or "") + (proc.stderr or "")


def convert_pmx(args: dict) -> str:
    pmx_path = str(args.get("pmx_path") or "").strip()
    if not pmx_path:
        return "请提供源 PMX/PMD 模型路径（pmx_path 参数）。"
    if not os.path.isfile(pmx_path):
        return f"PMX 文件不存在：{pmx_path}，请确认路径后重试。"

    vrm_path = str(args.get("vrm_path") or "").strip() or _default_vrm_path(pmx_path)
    vrm_dir = os.path.dirname(vrm_path)
    if vrm_dir and not os.path.isdir(vrm_dir):
        os.makedirs(vrm_dir, exist_ok=True)

    if not os.path.isfile(BLENDER_EXE):
        return f"未找到 Blender：{BLENDER_EXE}，请先安装 Blender 并确认路径。"
    if not os.path.isfile(CONVERT_SCRIPT) or not os.path.isfile(FINAL_SCRIPT):
        return f"转换脚本缺失（pmx_tools/blender_pmx_to_vrm.py 或 vrm_rest_final.py）"

    # 第 1 步：Blender 导入 + VRM1 setup + hips=腰 + 导出原始 VRM
    tmp_vrm = vrm_path + ".raw.vrm"
    out = _run([
        BLENDER_EXE, "--background", "--python", CONVERT_SCRIPT,
        "--", pmx_path, tmp_vrm,
    ], 600)
    if "EXPORT_DONE" not in out or not os.path.isfile(tmp_vrm):
        err_lines = [ln.strip() for ln in out.splitlines() if "ERROR" in ln or "Error" in ln]
        detail = err_lines[-1] if err_lines else out.strip()[-300:]
        return f"转换失败（Blender 导出阶段）：{detail}"

    # 第 2 步：JSON 后处理（X 镜像 + 清 I/重定向 + 重算 IBM）
    try:
        import subprocess as sp
        proc = sp.run([sys_executable(), FINAL_SCRIPT, tmp_vrm, vrm_path],
                      capture_output=True, text=True, timeout=300,
                      encoding="utf-8", errors="replace")
        out2 = (proc.stdout or "") + (proc.stderr or "")
    except Exception as e:
        return f"后处理失败：{e.__class__.__name__}: {e}"
    finally:
        if os.path.isfile(tmp_vrm):
            os.remove(tmp_vrm)

    if "VRM_FINAL_OK" not in out2 or not os.path.isfile(vrm_path):
        return f"后处理失败：{out2.strip()[-300:]}"

    size_mb = os.path.getsize(vrm_path) / 1024 / 1024
    return (f"转换成功：{vrm_path}（{size_mb:.1f} MB，X 镜像+骨骼清 I+IBM 重算，"
            f"手臂自然下垂、动作正常）。可用 appearance 技能切换加载该模型。")


def sys_executable() -> str:
    import sys
    return sys.executable


HANDLERS = {
    "pmx_to_vrm": convert_pmx,
}
