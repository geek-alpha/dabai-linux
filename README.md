# dabai

> 一个使用 JavaScript, Python, HTML, CSS 开发的项目。

![JavaScript](https://img.shields.io/badge/JavaScript-blue) ![Python](https://img.shields.io/badge/Python-blue) ![HTML](https://img.shields.io/badge/HTML-blue)

## ✨ 项目简介

该项目使用 **JavaScript, Python, HTML, CSS** 编写，包含 159 个文件，62,354 行代码。

## 🧠 Harness 扩展框架（技能 / 插件）

「大白」内置稳定的 harness 运行时，通过**技能（Skill）**与**插件（Plugin）**
持续扩展能力，无需改动核心代码：

- 📖 完整文档见 [HARNESS.md](HARNESS.md)
- 🖥 管理台：浏览器打开 http://<大白地址>/harness
- 🧩 技能目录 skills/（内置：文件助手、天气、AI 画图）
- 🔌 插件目录 plugins/（内置：hello_plugin 示例）
- 🪶 渐进式披露：settings.json 开启 harness.progressive_disclosure 后，on_demand 技能按需注入
  （一句话摘要 + 内置 skill_help 工具拉取完整说明书），技能再多也不撑爆上下文
- ⚙️ 管理 API：/api/harness/status 、/api/harness/skills 、/api/harness/plugins 、/api/harness/reload

```bash
# 新增一个技能：建目录 → 写 skill.json（可加 skill.py 实现）→ 管理台热重载
mkdir skills/my_skill
```

## 🚀 快速开始

### 安装

```bash
npm install
```

### 运行

```bash
npm start
```

### 服务托管与重启（本机实际部署方式）

本机 server.py 由**系统级 systemd 单元** `myservice.service` 托管（`/etc/systemd/system/myservice.service`，`Restart=always` / 3s，enabled，另有 `10-recovery.conf`、`20-linux-native.conf`、`30-secrets.conf` 三个 drop-in 注入环境变量）。

```bash
tools/restart_server.sh              # 重启（委托 systemctl，再自检 + 落报告）
tools/restart_server.sh --delay 20   # 延迟 20 秒动手（给发起方留收尾时间）
tools/restart_server.sh --check      # 只体检不重启（rc=0 表示健康）
journalctl -u myservice.service -f   # 日志在这里，不在文件里
```

重启结果落在 `data/restart_report.txt`：新 PID / 状态 / 监听端口 / `tools/reload_check.py` 的核心文件生效检查 / journal 末尾。

两条经验（2026-09-13 实测）：① 不要自己 kill + spawn —— systemd 会在 3 秒后自己拉起，脚本再起的第二个实例只会 bind 失败；② 进程的 stdout 接的是 journald socket，所以「日志文件为空」不代表没日志，先看 `journalctl -u <unit>`。判断谁在托管只需一条命令：`cat /proc/<pid>/cgroup`。

### 命令行调用（与网页输入框同一条路）

服务在跑时，`./dabai "任务"` 会**接入**正在运行的服务，不另起独立会话——和用户在
网页输入框里敲下同一句话走的是同一条路：同一个 Agent、同一份短期记忆、同一个
session，网页端实时看到「用户气泡 + 工具链 + 流式回复」，终端看到同一份事件流。
身份默认取 settings.json 的 `agent.unified_user_id`（= 网页端身份）。

```bash
./dabai "把 README 里的错别字改掉"      # 服务在跑 → 接入；没跑 → 本地独立会话
./dabai --local "..."                  # 强制进程内（独立会话，user=cli）
./dabai --remote "..."                 # 服务没跑就报错退出，不偷偷降级
./dabai --user alice "..."             # 指定身份（普通用户则收敛到自己的沙箱）
```

`--json` 与 `--namespace` 默认走本地：长跑引擎按前者的稳定事件格式解析，后者要的是
独立会话（接进服务会污染网页那条对话线）。端到端验证：
`venv/bin/python tools/cli_remote_selftest.py` —— 真开两条 WebSocket，断言「网页」
那条收到完整一轮（user_message 回显 → thinking → stream_text → turn_text_done）。

## 📁 项目结构

```text
dabai/
    ├── audio_cache/
        ├── 14206bf6641d466d9f4b7adc9cae2b0a.mp3
        ├── f38b3bf2a13949e49fddcd43bd96c409.mp3
    ├── backgrounds/
        ├── 太空飞船走廊.glb
        ├── 失落藏宝地.glb
        ├── 测试空间.glb
        ├── 迷宫.glb
    ├── bgm/
        ├── 6695671_光年之外-G.E.M.邓紫棋.余赛亚_eyW8s.mp3
        ├── 6703711_娃娃脸.mp3
    ├── cyber-corp-scoring/
        ├── _shared/
            ├── fonts/
            ├── js/
        ├── assets/
            ├── charts.js
        ├── cyber-corp-scoring.html
    ├── models/
        ├── models/
            ├── 可莉.vrm
            ├── 女仆.vrm
            ├── 女精灵.vrm
            ├── 小蛋糕.vrm
            ├── 水仙儿.vrm
            ├── 米尤.vrm
            ├── 白头凤.vrm
        ├── Ani_Grok.vrm
        ├── 可莉.vrm
        ├── 呆萌高中生.vrm
        ├── 夜乃樱.vrm
        ├── 女仆.vrm
        ├── 女精灵.vrm
        ├── 小蛋糕.vrm
        ├── 棕发学长.vrm
        ├── 水仙儿.vrm
        ├── 泳装普拉娜.vrm
        ├── 潮流穿搭女.vrm
        ├── 热可可.vrm
        ├── 白女.vrm
        ├── 知性女.vrm
        ├── 米尤.vrm
        ├── 白头凤.vrm
        ├── 黑裙清冷高中生.vrm
    ├── swarm-game-engine/
        ├── _shared/
            ├── fonts/
            ├── js/
        ├── assets/
            ├── charts.js
        ├── swarm-game-engine.html
    ├── tools/
        ├── gpt_sovits/
            ├── gpt_sovit_v2.py
            ├── gpt_sovits.json
    ├── web/
        ├── assets/
            ├── sounds/
        ├── js/
            ├── audio/
            ├── character/
            ├── core/
            ├── game/
            ├── input/
            ├── network/
            ├── ui/
            ├── vr/
        ├── app.ts
        ├── humanBaseline.json
        ├── index.html
        ├── style.css
    ├── xq3d/
        ├── assets/
            ├── three.module.min.js
        ├── css/
            ├── style.css
        ├── js/
            ├── ai.js
            ├── analysis.js
            ├── engine.js
            ├── main.js
            ├── notation.js
            ├── scene.js
            ├── ui.js
        ├── index.html
        ├── package.json
    ├── _gen_cert.py
    ├── agent.py
    ├── ai_behavior_engine.py
    ├── ai_game_strategies.py
    ├── ai_perception_engine.py
    ├── cards.json
    ├── cert.pem
    ├── cert.pem.bak
    ├── character_cards.json
    ├── dabai.bat
    ├── dabai.py
    ├── dabai4.0.zip
    ├── game_engine.py
    ├── key.pem
    ├── key.pem.bak
    ├── memory.py
    ├── package-lock.json
    ├── package.json
    ├── perception_dispatcher.py
    ├── reward_memory.json
    ├── reward_memory.py
    ├── rl_bandit.json
    ├── rl_coordinator.py
    ├── rl_dating_system_analysis.html
    ├── rl_interval.json
    ├── rl_mode_stats.json
    ├── rl_pushpull.json
    ├── rlhf_calibrator.py
    ├── rlhf_model.json
    ├── screen_shot.py
    ├── server.py
    ├── settings.json
    ├── test.py
    ├── tools.json
    ├── tts_config.json
    ├── world_model.json
    ├── world_model_trainer.py
```

## 🖼️ 截图

> 在此处添加项目截图或演示动图。

## 🧠 上下文机制优化

### 1. 分层记忆（省 token）
- 短期窗口（最近轮次，按 `short_term_max_tokens` 预算、单轮超长截断）；
- 长期摘要（只带最新摘要，`summary_max_tokens` 预算）；
- 常驻长期记忆（按 importance 取 top-k，`long_term_max_tokens` 预算）；
- 按需召回（关键词检索，`recall_max_tokens` 强制封顶）。
- 每轮对话把 raw/packed 估算与真实 prompt 用量写入 `context_stats` 表，
  可直接按表内实际用量核算节省（实测约 70%+）。
- 详细设计见 [MEMORY_HIERARCHY.md](MEMORY_HIERARCHY.md)。

### 2. 工具参数严格校验
- 工具执行前按定义（`function.parameters`，与技能工具 inputSchema 同构）校验
  required、类型、enum、嵌套 object/array、数值/长度边界；
- 安全类型自动转换（"30"→30、"true"→True），无法转换或缺参时
  不执行工具，而是把中文错误回填给模型自行修正（`tool_validation.py`）。

### 3. 工具执行反馈 / 心跳
- 工具执行期间每 `tool_heartbeat_interval_sec`（默认 5s）向前端推送
  `tool_call_progress` 心跳事件，工具链卡片实时显示“已运行 N 秒”，
  长任务不再“静默无输出”；
- 超时以结构化错误回填给模型，不抛异常中断对话；并行工具调用同样带心跳。

## 🤝 参与贡献

欢迎提交 Issue 和 Pull Request。

## 📄 许可证

[MIT](LICENSE)
