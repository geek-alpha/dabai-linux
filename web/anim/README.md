# Mixamo 动作表情库

## 目录结构

```
anim/
├── animation-library.json    # 动作库配置（元数据、情绪映射）
├── idle/                     # 空闲待机动作（循环播放）
├── gesture/                  # 手势动作（招手、鼓掌、指方向等）
├── emotion/                  # 情绪表达动作（开心跳、伤心、生气等）
├── walk/                     # 行走移动动作
├── dance/                    # 舞蹈动作
└── pose/                     # 静态姿势造型
```

## 如何添加新动作

### 1. 下载 Mixamo 动作

1. 访问 https://www.mixamo.com/
2. 搜索并选择动作
3. 选择格式: FBX（不带皮肤）
4. 下载到对应分类目录

### 2. 注册到配置文件

编辑 `animation-library.json`，在对应分类的 `animations` 数组中添加：

```json
{
  "name": "动作唯一标识",
  "file": "分类目录/文件名.fbx",
  "emotion": "关联情绪标签",
  "loop": true,
  "description": "动作描述"
}
```

### 3. 关联情绪（可选）

在 `emotionMap` 中把动作名添加到对应情绪的数组里，情绪变化时会自动从池中随机选择。

## 支持的情绪标签

`happy`, `excited`, `shy`, `sad`, `pout`, `angry`, `surprised`,
`thoughtful`, `calm`, `proud`, `tired`, `playful`, `love`, `neutral`

## JavaScript API

```javascript
// 加载整个动作库（VRM 加载完成后调用）
await App.loadAnimationLibrary();        // 全部预加载
await App.loadAnimationLibrary(true);    // 懒加载（先待机类，后台加载其余）

// 按名称播放
App.playLibraryClip('laugh');

// 按情绪随机播放
App.playEmotionClip('happy');

// 按分类随机播放
App.playCategoryClip('dance');

// 获取列表
App.getCategoryClips('gesture');   // 获取手势类所有动作名
App.getEmotionClips('sad');        // 获取伤心情绪关联的动作名

// 查看加载状态
App.getAnimLibraryStats();

// 开启情绪驱动（情绪变化自动切换动作）
App.enableMixamoEmotion(true);
App.setMixamoEmotionMode('idle');        // 仅待机循环
App.setMixamoEmotionMode('expressive');  // 情绪触发表达动作
```

## 统一动作调度

角色应用动作时走一条统一规则：**当前情绪 + 场景分类 → 该分类的在盘动作里随机挑一个播放**，
由 updateActionScheduler 的调度口统一接管（库可用时优先播在盘动作，播完自动放回
程序式动作；库未加载时回退手写程序式动作）：

```javascript
// 按当前情绪应景随机播一个在盘动作（updateActionScheduler 内部自动调用）
App.tryStartLibraryAction();

// 指定场景分类随机（idle/gesture/emotion/walk/dance/pose）
App.pickLibraryActionByScene("dance");

// 情绪应景分类表 / 循环动作限时区间（可按需调整）

**防呆节奏**（任何情况下角色都不会长期保持一个动作）：
- 循环动作（待机/姿势/舞姿）播 2.8s 后可轮换（再加 0.8~2.2s 余量释放），最长约 5s 必换；
- 单次表达动作有 5s 硬上限（Mixamo 部分动作原始时长 10~30s，不能按原始时长等待）；
- 动作之间留 1~3s 程序式微动作间隙，随后按情绪+场景分类加权随机播下一个；
- 场景分类带“动态感”权重（gesture/emotion/dance 权重高、idle/pose 压低），
  且不连续两次同场景——轮换里始终有看得见的动作；
- 动作库加载完成 ≥70% 前由手写程序式动作（POSE/WALK/TURN/DANCE）撑场，
  避免启动期只有 idle 分类可用、每次开始都是同一套待机。

**动作间混合过渡**：播放新动作时旧动作 0.35s 淡出、新动作淡入（crossfade），
停止动作时 0.3s 淡出后再交还程序式动画——动作切换不跳变。

**说话时知道当前动作**：前端每次播放/停止动作都会节流上报（anim_state 消息），
后端在生成回复时把它注入 LLM 上下文的【你现在的动作】，
大白说话时就知道自己正在做什么动作，回复可以自然地配合。
App.LIBRARY_EMOTION_SCENES;
App.LIBRARY_SCENE_HOLD;
```

## 调试

在浏览器控制台输入：

```javascript
_animLib      // 查看动作库配置和统计
_mixamoLib    // 查看已加载的所有 Mixamo 片段
```
