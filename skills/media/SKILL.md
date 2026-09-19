# 媒体与娱乐（media）

在线影音 + 汉化流水线 + 小游戏 + AI 画图，四合一。触发：搜视频/放视频/搜歌/放歌/歌单/歌词/榜单/汉化视频/翻译配音/下载油管/发布B站/烧字幕/垫BGM/玩游戏/画图/生成图片/壁纸/立绘/头像/插画/海报。

## 视频（video_*）
- `video_search(keyword, platform?, sort?, limit?)` 聚合搜索（B站/AcFun/YouTube）
- `video_play(index|query|url, watch?)` 大屏播放；watch=true 派子智能体盯梢，播完自动汇报
- `video_queue(index|query|url)` 加入连播队列
- `video_control(action, value?)` 控制：pause/resume/seek/volume/mute/stop/next
- `video_status` 当前播放、进度与队列

## 音乐（music_*）
- `music_search(keyword, source?, limit?)` 聚合搜索（酷我/网易云）
- `music_play(index|source+song_id, watch?)` 播放；watch=true 派子智能体盯梢
- `music_stop` / `music_control(action, value?)` 停止/控制
- `music_lyric(index|source+song_id)` 歌词（LRC）
- `music_billboard` 内置榜单（云音乐热歌/新歌/飙升/原创榜）
- `music_playlist(keyword|playlist_id)` 搜在线歌单
- `music_my_playlists` / `music_play_playlist(playlist_id|playlist_name, watch?)` 自建歌单

## 媒体子智能体看护（media_worker_*）
- `media_workers_list` 当前在干活的媒体子智能体
- `media_worker_status(worker_id)` 单个详情
- `media_worker_cancel(worker_id, reason?)` 收回子智能体并停止播放

## 汉化流水线（hanhua_*）
- 操作「油管视频汉化」项目 CLI：doctor/download/add/run/status/jobs/log/retry/cancel/delete/pause/resume/config/bgm/remix
- 流程：先 cd 项目根目录，用 .venv\Scripts\python.exe cli.py <命令>
- 规则：改流程参数先 dry_run=true 冒烟；CLI 与 Web 不能同时跑调度

## 小游戏（launch_game）
- `launch_game(game?)` 启动小游戏：treasure_hunt=迷宫寻宝 / sandbox=沙盒世界
- 用户说想玩游戏/无聊/想放松时主动邀请并直接启动，不要问选哪个

## AI 画图（image_gen_create）
- `image_gen_create(prompt, size?)` 生成图片，保存到 web/generated/ 并返回链接；size 可选 1024x1024 / 768x1024 / 1024x768
- 配置在 settings.json：images_base_url（默认 SiliconFlow）、images_model（默认 Kwai-Kolors/Kolors）、images_api_key（必填）
- 画图是直接能力，绝不委派给任何智能体/编程助手；生成失败先查配置再重试

## 规则
- 影音能力直接调用 video_*/music_* 工具，绝不委派给编码智能体，绝不用命令行/脚本去搜歌放歌
- 用户表达「播完提醒我/帮我看着/放完告诉我」时加 watch=true
- 搜不到/解析失败时主动换关键词/换平台（教育课程类用 bilibili 最全）重试

详细文档：references/cli.md
