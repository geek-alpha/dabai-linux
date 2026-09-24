# blender_tools — Blender 无头建模脚本库

Blender 5.2.2 LTS（`/usr/local/bin/blender`）的通用建模/导出脚本。
与 `pmx_tools/` 的分工：`pmx_tools/` 专做 PMX→VRM 转换，这里放通用能力。

## 调用约定

所有脚本都用 Blender 后台模式跑：

```bash
blender --background --python <script.py> -- <arg1> <arg2> ...
```

`--` 之后的参数通过 `sys.argv[sys.argv.index("--")+1:]` 取。

## 脚本清单

| 脚本 | 用途 |
|---|---|
| `new_scene.py` | 新建干净场景（清空默认物体），可选加基础网格 |
| `export_vrm.py` | 把当前场景导出为 VRM（需 VRM Add-on） |
| `render_preview.py` | 无头渲染预览图（多角度/线框） |
| `inspect_model.py` | 体检：物体/网格/骨骼/材质统计，导出 JSON |

## 环境

- Blender: `/opt/blender/blender-5.2.2-linux-x64/blender`
- 软链: `/usr/local/bin/blender`
- VRM Add-on: 见 `pmx_tools/` 的安装逻辑
