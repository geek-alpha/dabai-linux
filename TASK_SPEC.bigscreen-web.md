# 任务规范：3D 大屏改为网页渲染（bigscreen.html 接入）

## 目标
把大白 3D 大屏（`web/js/ui/30_task_big_screen.ts`）的渲染方式从「Canvas 2D 手绘」改为「加载网页版大屏 `bigscreen.html`（隐藏 iframe + html2canvas 每帧截图贴到 Billboard 纹理）」，实现：
- 3D 大屏显示的就是真正的网页画面，与浏览器直接打开 `bigscreen.html` 完全一致
- 以后改大屏样式只改 HTML/CSS，一处改动两边生效
- 预览容易：浏览器直接开 `/static/bigscreen.html` 即可看到大屏画面

## 范围与不做
- 做：3D 大屏渲染主路径切换为网页截图；手绘代码保留为兜底
- 做：iframe 生命周期管理（创建/加载/失败回退/销毁）
- 不做：不改 bigscreen.html 本身的内容与数据流（它已自连 WS + /api/tasks）
- 不做：不改后端 server.py
- 不做：删除手绘代码（保留兜底，避免 iframe 方案在部分环境失败时大屏空白）

## 验收标准
1. 3D 大屏正常显示网页版大屏画面（iframe 加载成功 + html2canvas 截图成功）
2. iframe 加载失败 / html2canvas 不可用时自动回退手绘，大屏不空白
3. 大屏尺寸自适应逻辑（displayH 平滑插值、VR 放大）不受影响
4. 视频直播（大白影院）画面正常（视频帧走 rVFC 路径，不依赖网页截图）
5. 语法检查通过、import 冒烟通过

## 实施步骤
1. 在 `ensureBoard()` 中创建隐藏 iframe（src=/static/bigscreen.html），动态加载 html2canvas
2. 新增 `webRender` 状态机：loading → ready → failed（回退手绘）
3. 在 `updateTaskBigScreen` 绘制分支：webRender ready 时用 html2canvas 截图贴纹理；否则走原手绘 draw()
4. 视频直播（cinema）保持原路径（视频帧直接画，不经过网页截图）
5. 验证：语法 + import 冒烟

## 风险与回滚
- 风险：html2canvas 对 CSS 动画/视频元素截图可能不完整 → 兜底手绘 + 视频走原路径
- 风险：iframe 跨域/加载慢 → 超时回退手绘
- 回滚：工作树隔离，改坏直接 wt_discard