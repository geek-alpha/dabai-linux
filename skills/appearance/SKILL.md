# 形象与 3D（appearance）

形象交互 + 3D 资产，二合一。触发：换装/换形象/换场景/调音色/切模式/模型转换/动作库。

## 外观
- `get_available_models` / `get_available_backgrounds` 查可用列表（切换前先查）
- `switch_character_model(name)` 换装（精确文件名含扩展名；'default' 恢复默认；模型只是皮肤，身份不变）
- `switch_background_scene(bg)` 换场景（'default' 恢复默认星空）

## 声音
- `switch_tts_settings(voice?, rate?, engine?)` 切音色/语速/引擎（不传的参数保持当前值）
  - 音色：XiaoxiaoNeural 温柔女声 / YunxiNeural 阳光男声 / XiaoyiNeural 活泼女声 / YunjianNeural 沉稳男声
  - 语速：'+10%' 加快 / '-10%' 放慢 / '+0%' 正常；引擎：edge_tts / gpt_sovits

## 界面
- `switch_app_mode(mode)` 切模式：auto_voice / press_voice / lock_screen / normal
- `show_screen_toast(msg)` 屏幕短暂提示（≤30 字）

## 3D 资产
- `pmx_to_vrm(pmx_path, vrm_path?)` PMX/PMD 转 VRM（Blender 后台；v19 流程：导入+VRM1 setup+hips=腰 → 导出原始 → JSON 层 T-pose rest 标准化；输出默认 models/）
  - 流程细节：`pmx_tools/blender_pmx_to_vrm.py`（Blender）导出原始 VRM → `pmx_tools/vrm_tpose_rest.py`（anaconda python）做 rest 标准化
  - humanoid 骨骼转标准 T-pose 方向、非 humanoid 非 spring 骨骼 rest 清零、spring 骨骼保留、重算 IBM 网格外观不变
  - 自包含：所有执行代码在 `pmx_tools/` 目录（Blender 脚本 + T-pose 后处理 + 高手 setup 工具），仅依赖 Blender + anaconda python 两个外部环境
- `anim_status` 综合状态（下载服务+动作库统计+最近日志）
- `anim_start(proto?)` 启动代理浏览器 / `anim_stop` 关闭并中止批量下载
- `anim_check_login` 检测登录 / `anim_save_cookies` 保存登录态（重启免登录）
- `anim_download(name)` 下载单个动作（自动 Without Skin，存 web/anim/）
- `anim_batch(names)` 批量下载（后台异步，未登录拒绝）
- `anim_library(action)` 动作库管理：stats/list/scan/verify/categorize
- `anim_optimize(action)` 优化：validate/emotions/fix

## 规则
- 只有用户明确要求换装/换形象时才调用，不擅自更换
- 切换前先查可用列表，文件名精确匹配不编造
- 登录是唯一需用户参与的环节：anim_start 后让用户弹窗登录一次，anim_check_login 确认 + anim_save_cookies 保存
- 转换完成后可用本技能切换加载该模型

详细文档：references/guide.md
