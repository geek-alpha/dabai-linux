# godot-dabai —— 规格（自动生成，勿手改）

**目标**：把树莓派上的大白安卓版（WebView+three.js+TS，68模块34200行）迁移到 Godot 4.7 原生 Android APK。

核心模块：
1. 3D场景渲染：three.js → Godot SceneTree/Camera3D/WorldEnvironment，含陀螺仪、低功耗模式、性能分级
2. VRM模型加载：VRMLoaderPlugin → Godot GLTF加载+VRM骨骼/表情/SpringBone/MToon着色器自定义实现
3. WebSocket通信：浏览器WebSocket → Godot WebSocketPeer，流式对话、音频分块传输、任务推送
4. TTS口型同步：WebAudio Analyser → Godot AudioStreamPlayer+骨骼驱动口型
5. 语音录制：MediaRecorder → Godot AudioStreamRecord+VAD自动对话
6. 背景场景：GLTF背景加载、相机预设、地板射线检测
7. 角色动画：Mixamo动作重定向、动作混合器、情绪驱动
8. UI系统：消息区、Toast、模型选择、背景选择、角色卡片、LLM设置、任务中心、大屏、工具链、舞台转盘、游戏特效、直播室、全息舞台
9. VR模式：WebXR → Godot XR，VR HUD
10. 音频系统：BGM播放器、音乐UI、视频UI、工作区UI、脚步声SFX
11. RL系统：行为克隆、约会关系模型、RL智能体、神经网络
12. AI自主：路径规划、自主行为控制器
13. Codex运行器：远程任务执行
14. Android导出：Godot Android导出模板+keystore签名

资源文件：VRM模型(10个)、GLB背景(4个)、音频、动画库

**进度**：📊 覆盖率 0.0% —— 功能 0/18 已验证（实现未验收 0，未开始 18）
模块 0/6 完成

## 模块

- 🔄 `M-001` 数据层（data） 依赖：无
  - 文件：data/m-001.py
  - 承诺功能：F-005, F-011, F-014, F-018
- ⬜ `M-002` 核心逻辑（core） 依赖：M-001
  - 文件：core/m-002.py
  - 承诺功能：F-001, F-002, F-003, F-004
- ⬜ `M-003` 核心逻辑（core） 依赖：M-001
  - 文件：core/m-003.py
  - 承诺功能：F-007, F-008, F-009, F-010
- ⬜ `M-004` 核心逻辑（core） 依赖：M-001
  - 文件：core/m-004.py
  - 承诺功能：F-012, F-015, F-016, F-017
- ⬜ `M-005` 接口层（api） 依赖：M-002, M-003, M-004
  - 文件：api/m-005.py
  - 承诺功能：F-006
- ⬜ `M-006` 界面层（ui） 依赖：M-005
  - 文件：ui/m-006.py
  - 承诺功能：F-013

## 功能清单

- ⬜ `F-005` [M-001] VRM模型加载：VRMLoaderPlugin → Godot GLTF加载+VRM骨骼/表情/SpringBone/MToon着色器自定义实现
  - 验收：data/m-001.py 存在且 py_compile 通过
  - 验收：F-005 行为断言：VRM模型加载：VRMLoaderPlugin → Godot GLTF加载+V —— 写明执行命令与期望输出，实际跑通
- ⬜ `F-011` [M-001] UI系统：消息区、Toast、模型选择、背景选择、角色卡片、LLM设置、任务中心、大屏、工具链、舞台转盘、游戏特效、直播室、全息舞台
  - 验收：data/m-001.py 存在且 py_compile 通过
  - 验收：F-011 行为断言：UI系统：消息区、Toast、模型选择、背景选择、角色卡片、LLM设置、任务中心 —— 写明执行命令与期望输出，实际跑通
- ⬜ `F-014` [M-001] RL系统：行为克隆、约会关系模型、RL智能体、神经网络
  - 验收：data/m-001.py 存在且 py_compile 通过
  - 验收：F-014 行为断言：RL系统：行为克隆、约会关系模型、RL智能体、神经网络 —— 写明执行命令与期望输出，实际跑通
- ⬜ `F-018` [M-001] 资源文件：VRM模型(10个)、GLB背景(4个)、音频、动画库
  - 验收：data/m-001.py 存在且 py_compile 通过
  - 验收：F-018 行为断言：资源文件：VRM模型(10个)、GLB背景(4个)、音频、动画库 —— 写明执行命令与期望输出，实际跑通
- ⬜ `F-001` [M-002] 把树莓派上的大白安卓版（WebView+three.js+TS，68模块34200行）迁移到 Godot
  - 验收：core/m-002.py 存在且 py_compile 通过
  - 验收：F-001 行为断言：把树莓派上的大白安卓版（WebView+three.js+TS，68模块3420 —— 写明执行命令与期望输出，实际跑通
- ⬜ `F-002` [M-002] 7 原生 Android APK
  - 验收：core/m-002.py 存在且 py_compile 通过
  - 验收：F-002 行为断言：7 原生 Android APK —— 写明执行命令与期望输出，实际跑通
- ⬜ `F-003` [M-002] 核心模块：
  - 验收：core/m-002.py 存在且 py_compile 通过
  - 验收：F-003 行为断言：核心模块： —— 写明执行命令与期望输出，实际跑通
- ⬜ `F-004` [M-002] 3D场景渲染：three.js → Godot SceneTree/Camera3D/WorldEnvironment，含陀螺仪、低功耗模式、性能分级
  - 验收：core/m-002.py 存在且 py_compile 通过
  - 验收：F-004 行为断言：3D场景渲染：three.js → Godot SceneTree/Camera —— 写明执行命令与期望输出，实际跑通
- ⬜ `F-007` [M-003] TTS口型同步：WebAudio Analyser → Godot AudioStreamPlayer+骨骼驱动口型
  - 验收：core/m-003.py 存在且 py_compile 通过
  - 验收：F-007 行为断言：TTS口型同步：WebAudio Analyser → Godot AudioS —— 写明执行命令与期望输出，实际跑通
- ⬜ `F-008` [M-003] 语音录制：MediaRecorder → Godot AudioStreamRecord+VAD自动对话
  - 验收：core/m-003.py 存在且 py_compile 通过
  - 验收：F-008 行为断言：语音录制：MediaRecorder → Godot AudioStreamRe —— 写明执行命令与期望输出，实际跑通
- ⬜ `F-009` [M-003] 背景场景：GLTF背景加载、相机预设、地板射线检测
  - 验收：core/m-003.py 存在且 py_compile 通过
  - 验收：F-009 行为断言：背景场景：GLTF背景加载、相机预设、地板射线检测 —— 写明执行命令与期望输出，实际跑通
- ⬜ `F-010` [M-003] 角色动画：Mixamo动作重定向、动作混合器、情绪驱动
  - 验收：core/m-003.py 存在且 py_compile 通过
  - 验收：F-010 行为断言：角色动画：Mixamo动作重定向、动作混合器、情绪驱动 —— 写明执行命令与期望输出，实际跑通
- ⬜ `F-012` [M-004] VR模式：WebXR → Godot XR，VR HUD
  - 验收：core/m-004.py 存在且 py_compile 通过
  - 验收：F-012 行为断言：VR模式：WebXR → Godot XR，VR HUD —— 写明执行命令与期望输出，实际跑通
- ⬜ `F-015` [M-004] AI自主：路径规划、自主行为控制器
  - 验收：core/m-004.py 存在且 py_compile 通过
  - 验收：F-015 行为断言：AI自主：路径规划、自主行为控制器 —— 写明执行命令与期望输出，实际跑通
- ⬜ `F-016` [M-004] Codex运行器：远程任务执行
  - 验收：core/m-004.py 存在且 py_compile 通过
  - 验收：F-016 行为断言：Codex运行器：远程任务执行 —— 写明执行命令与期望输出，实际跑通
- ⬜ `F-017` [M-004] Android导出：Godot Android导出模板+keystore签名
  - 验收：core/m-004.py 存在且 py_compile 通过
  - 验收：F-017 行为断言：Android导出：Godot Android导出模板+keystore签名 —— 写明执行命令与期望输出，实际跑通
- ⬜ `F-006` [M-005] WebSocket通信：浏览器WebSocket → Godot WebSocketPeer，流式对话、音频分块传输、任务推送
  - 验收：api/m-005.py 存在且 py_compile 通过
  - 验收：F-006 行为断言：WebSocket通信：浏览器WebSocket → Godot WebSock —— 写明执行命令与期望输出，实际跑通
- ⬜ `F-013` [M-006] 音频系统：BGM播放器、音乐UI、视频UI、工作区UI、脚步声SFX
  - 验收：ui/m-006.py 存在且 py_compile 通过
  - 验收：F-013 行为断言：音频系统：BGM播放器、音乐UI、视频UI、工作区UI、脚步声SFX —— 写明执行命令与期望输出，实际跑通
