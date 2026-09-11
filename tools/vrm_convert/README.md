# VRM 转换标准流程（唯一入口）

> 替代全部 `convert_luotianyi_v*.py` 历史版本（已归档至 `_archive/convert_luotianyi_old/`）。
> 原则：**一个入口、一个流程、一次验证**。不再产生 v8~v19 这种版本堆。

## 标准命令

```bash
blender --background --python tools/vrm_convert/convert_standard.py -- <pmx_path> <vrm_path> [model_name]
```

示例（洛天依）：

```bash
blender --background --python tools/vrm_convert/convert_standard.py -- \
  "C:/Users/wangxingfeng/Downloads/洛天依调_by_宛倾然_a25c464a879c354af4354840f7c01889/洛天依.pmx" \
  "D:/AI/dabai/models/洛天依_v20.vrm" 洛天依
```

## 流程（五步，固化自 v19 验证方案）

| 步骤 | 做什么 | 为什么 |
|---|---|---|
| 1 | 清场 + 启用 mmd_tools / vrm 插件 | 干净环境，避免残留状态 |
| 2 | `run_pmx_to_vrm1_setup`：导入 + humanoid 自动映射 + 表情 + meta + MToon | 五子系统一次配齐 |
| 3 | hips 修正为「腰」（骨盆） | 层级合法，humanoid 映射正确 |
| 4 | 官方导出原始 VRM | rest 是 MMD 原始姿态，但映射正确 |
| 5 | `vrm_tpose_rest.py` 后处理 | JSON 层 rest 标准化为 T-pose：humanoid 保留绕轴扭转、非 spring 清零、spring 保留（物理不乱）、重算 IBM |

## 验证（转换后必跑）

```bash
node tools/validate_vrm.mjs <vrm_path>
```

验收标准：
- `humanoid_bones=11/11`（hips/spine/chest/neck/head/双臂/双腿/双脚 全映射）
- `spring_groups>0`（弹簧物理保留）
- `expressions>0`（表情 BlendShape 保留）
- `leftUpperArm_world_Y≈0`（T-pose 标准化生效，手臂水平）

## 规则

1. **新模型转换一律走本入口**，禁止新建 `convert_xxx_vN.py`
2. 流程有缺陷 → 改 `convert_standard.py` 本身（版本号不变，git 留痕）
3. 转换产物命名：`<模型名>_v<数字>.vrm`，raw 中间文件自动清理
4. 转换后必须跑验证脚本，输出达标才算完成