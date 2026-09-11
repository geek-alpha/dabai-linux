# appearance 详细文档

## 工具

- `get_available_models()` —— 列出可选角色模型（models/ 目录）
- `get_available_backgrounds()` —— 列出可选背景场景（backgrounds/ 目录）
- `switch_character_model(model_name)` —— 换装/换形象（精确文件名，含扩展名；"default" 恢复）
- `switch_background_scene(bg_name)` —— 换环境场景（精确文件名；"default" 恢复星空）

## PMX → VRM 转换流程（v19 终极方案）

调用 `pmx_to_vrm(pmx_path, vrm_path?)` 后按以下两步执行（已用洛天依/莉丽拉验证）。

### 自包含结构

本技能内 `pmx_tools/` 目录包含全部执行代码，迁移时整体拷贝即可：

```
skills/appearance/pmx_tools/
├── blender_pmx_to_vrm.py      # Blender 转换脚本（入口）
├── vrm_tpose_rest.py          # T-pose rest 后处理（anaconda python 运行）
├── run_pmx_to_vrm1_setup.py   # 高手 setup 编排（以下为其依赖）
├── import_pmx.py              # mmd_tools 导入 PMX
├── setup_vrm1.py              # VRM1 humanoid/表情/meta/MToon 配置
├── mmd_vrm1_bone_map.py       # humanoid 骨骼映射 fallback
├── audit_vrm1_setup.py        # 转换前审计
├── rename_mmd_bones_en.py     # 骨骼英文 gloss
└── rename_mmd_materials_en.py # 材质英文 gloss
```

仅两项外部环境依赖（跨平台，均可自动探测）：
- Blender（需装 mmd_tools + VRM Add-on 插件）：Windows 默认 `D:\blender\blender.exe`；
  Linux/macOS 探测 PATH，也可用 `DABAI_BLENDER` 指定
- 带 numpy 的 python（IBM 重算用）：Windows 历史路径为 anaconda；
  Linux/macOS 用系统 python3（`pip install numpy`），缺失时回退系统 python

### 执行步骤

1. **Blender 后台导出原始 VRM**（`pmx_tools/blender_pmx_to_vrm.py`）：
   - `wm.read_factory_settings` + 启用 mmd_tools / vrm 插件
   - `run_pmx_to_vrm1_setup`（pmx_tools 目录内）：mmd_tools 导入 PMX + VRM1 humanoid/表情/meta/MToon
   - **hips = 腰**（骨盆）：auto 算子默认把 hips 填成 センター（脚底根骨），会导致前端 hipsPositionScale≈0 → 腿位移全被乘 0 → 腿僵
   - `export_scene.vrm` 导出到 `<vrm_path>.raw.tmp`

2. **JSON 层 T-pose rest 标准化**（`pmx_tools/vrm_tpose_rest.py`，anaconda python + numpy）：
   - humanoid 骨骼 → 标准 T-pose 方向（脊柱/脖子/头 +Y、左臂 +X、右臂 -X、腿 -Y），保留绕轴扭转
   - 非 humanoid 非 spring 骨骼 → rest 清零（rotation = identity）
   - spring 骨骼 → 保留（物理不乱）
   - 重算 IBM（inverseBindMatrices）→ 网格外观不变
   - 输出标记 `TPOSE_REST_OK`

### 关键历史（为什么不用别的方式）

- **v13/v14 armature_apply 方案失败**：Blender 里 pose 摆 T-pose 再 armature_apply，glTF 导出器只写 rest（bone.matrix_local），pose 不落盘，rest 永远歪
- **v18 手动 T-pose + apply 失败**：armature_apply 链式叠加，rest 仍 120° 斜举
- **v19（本方案）成功**：官方导出（humanoid 映射正确、rest 是 MMD 原始姿态）→ JSON 层直接改节点 rotation，最稳

### 输出验证

- gltf-transform / 前端 three-vrm 均可加载
- humanoid 骨骼世界方向应为标准 T-pose（spine=+Y、左臂=+X、右臂=-X、腿=-Y）
- IBM × rest 世界矩阵 ≈ 单位阵（误差 ~1e-6），网格不变形

## 规则

1. 只有用户明确要求换装/换形象/变身时才调用，不要擅自更换；
2. 切换前先查询可用列表，文件名必须精确匹配，不要编造；
3. 模型文件名只是外观皮肤，不是身份——回复中不要提文件名。
