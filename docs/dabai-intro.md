# 大白（Dabai）· 项目介绍

> 一个可长期运行的本地 AI 助手：浏览器端是能对话、有 3D 形象、能被支配的角色；
> 服务端是一套带技能 / 插件 / 长任务 / 分层记忆 / 自我进化台账的智能体运行时。
> 版本 **1.1.23** · 许可 **MIT**

---

## 一、一句话定位

大白不是「一个聊天框」，是**一个跑在你本机的智能体操作系统**：对话只是入口，真正的本体是那套负责
「调模型、跑工具、管任务、记东西、认错、改自己」的运行时。

三个判断标准决定了它跟普通套壳 AI 的分界：

1. **它自己动手**——不是回你一段文字，而是调工具把事做完（读代码、改文件、跑测试、发消息、下发任务）；
2. **它记得住、也认错**——踩过的坑进经验库、被否掉的方案进信条、长期目标进台账，下一轮自动带进上下文；
3. **它坏得起来也修得住**——LLM 调用和工具执行全部受监督（熔断 / 重试 / 超时 / 计量），长任务支持
   服务重启断点续跑，harness 自身故障时自动退化成裸调用，绝不阻断主流程。

---

## 二、核心数字（本机实测，口径写明）

| 指标 | 数值 | 口径 |
| --- | --- | --- |
| 版本 | 1.1.23 | `VERSION` 文件 |
| Python 源文件 | 897 个 | 排除 `venv/`、`.git/`、`tools/vendor_ots/`、`data/` |
| Python 代码行 | 333,367 行 | 同上口径 |
| 前端 TypeScript | 36,014 行 | `web/**.ts`，不含 `web/vendor/` 第三方库 |
| Git 跟踪文件 | 1,114 个 | `git ls-files` |
| 提交数 | 175 | `git rev-list --count HEAD` |
| 测试用例文件 | 110 个 | `tests/test_*.py`（pytest） |
| 内置技能 | 19 个 | `skills/` 目录 |
| 运维 / 自检脚本 | 95 个 | `tools/*.py` |

技术栈：**Python（FastAPI + WebSocket 服务端）+ TypeScript / HTML / CSS（前端）+ Node.js（运行期转译）**。

---

## 三、能力地图

按「跟外界的关系」分四层，从外到内：

### 1. 交互层（你怎么用它）

- **网页 3D 角色**：浏览器里是一个 VRM 模型，能说话（TTS + 口型同步）、有表情、有动作、能点、能跟着你走；
- **语音对话**：麦克风 + VAD 自动断句（含 Silero VAD），说停即发；
- **命令行**：`./dabai "任务"` 接入正在跑的服务，与网页输入框**同一条路**（同一 Agent、同一份短期记忆、
  同一个 session，网页端实时看到同一份事件流）；
- **飞书 / 手机 / 网盘 / 浏览器**：通过技能把外部世界接进来（见技能清单）；
- **VR 模式**：WebXR 支持，HTTPS 下可用（手机陀螺仪等传感器只在安全上下文暴露）。

### 2. 执行层（它怎么干活）

- **技能 / 插件**：放一份文件进 `skills/` 或 `plugins/`，热重载即得新能力，不改核心代码；
- **长任务 / 批量任务**：多步骤 DAG 流程后台跑，步骤级重试、流程级看门狗、断点续跑；
- **委派外部智能体**：Codex / OpenCode / DSH 作为技能工具存在，由任务中心确认后执行；
- **定时任务**：按固定间隔自动跑（后台执行 + 完成后汇报）；
- **本机支配**：shell、文件系统、代码工程、Linux 原生（systemd / GPIO / 进程 / 网络暴露面）。

### 3. 记忆与自我层（它怎么变得不一样）

- **四层上下文**：短期窗口 / 长期摘要 / 常驻长期记忆 / 按需召回，token 实测省 **70%+**；
- **经验库**：踩过的坑落成一句话教训，后续对话自动注入（当前 **496 条**）；
- **信条与拒绝**：自己判断出的行为准则，以及「我拒绝做什么 + 为什么」；
- **长期事业台账**：跨会话接力——每个项目留「下一轮能直接开跑的原子动作」；
- **基因适应度**：给教训 / 信条 / 事业加选择压力，用可复算的信号（复发率、结局对齐）决定谁上场。

### 4. 自主层（你不理它时它在干嘛）

- **AI 自主行为中枢**：好奇心感知 → 行为决策 → 踱步 / 漫步 / 小动作 / 走向兴趣点；
- **冷落降级（RL 级联）**：被冷落越久，自主行为越收敛（`normal` / `calm` / `freeze`）；
- **主动说话**：结合用户空间位置与互动状态决定要不要开口。

---

## 四、架构总览

```
浏览器（TypeScript 源码直服）
  └─ three.js 场景 + VRM 角色 + UI + WebSocket
        │  ws://…  实时事件流（用户气泡 / 工具链 / 流式文本 / 心跳）
        ▼
server.py  FastAPI 服务入口
  ├─ 路由 / WebSocket / .ts 实时转译 / TLS 终结
  └─ 管理台 /harness + REST API /api/harness/*
        ▼
agent.py   AIAgent 主循环（唯一智能体底座）
  ├─ 人设 + function calling + 记忆（所有自然语言请求的唯一决策者）
  ├─ LLM 调用（chat / game / decision / character_line / memory / plan 六个渠道）
  ├─ 工具执行（技能 → 插件 → 内置兜底），同一轮多个工具并行
  └─ 每轮 = 一个 RunSpan（在途 / 耗时 / LLM 轮数 / 工具数 / 成败）
        ▼
harness/   稳定扩展与控制层
  ├─ core.py     门面：工具收集 / 稳定路由 / 健康状态 / 热重载
  ├─ runtime.py  监督运行时：熔断 / 重试 / 超时 / 计量 / RunSpan / token 记账
  ├─ tasks.py    任务系统：队列调度 / DAG 流程 / 批量 / 持久化断点续跑
  ├─ skills.py   技能注册表（skills/<名称>/）
  ├─ plugins.py  插件管理器（plugins/<名称>/）
  └─ state.py    启停状态持久化
        ▼
skills/ 19 个技能 · plugins/ · tools/ 95 个运维脚本 · data/ 运行期数据
```

**工具路由顺序**：`harness 技能 → harness 插件 → 内置兜底`。技能与插件提供的工具自动合并进
function calling 工具表，它们注入的提示词片段自动拼进 system prompt——所以模型永远知道新能力怎么用。

**一条设计原则贯穿全项目**：外部智能体（Codex / OpenCode / DSH）没有特殊通道，它们只是 `agent_ops`
技能里的几个工具，跟天气查询、发消息走同一条路、同一套监督。

---

## 五、六大子系统

### 5.1 Harness 监督运行时 —— 每次调用都有人看着

| 能力 | 做法 |
| --- | --- |
| LLM 监督 | 熔断 → 瞬时错误重试（默认 3 次，1s→2s 退避）→ 整体超时（默认 180s）→ 分渠道计量 |
| 工具监督 | 熔断 → 超时（默认 30s）→ 按工具计量；失败以错误文案返回，不抛异常打断对话 |
| 熔断器 | 连续失败 3 次跳闸，60s 冷却内快速失败；半开态放一次探测，成功即自动恢复 |
| 并行工具 | 同一轮多个独立工具调用 `asyncio.gather` 并行执行 |
| RunSpan | 每轮对话一个运行追踪：在途数、耗时、LLM 轮数、工具次数、成功与否 |
| token 记账 | UsageEvent 统一记账，分渠道累计，管理页可见 |
| 降级安全 | harness 不可用时自动退化为原有裸调用行为，监督层自身永不成为故障源 |

参数全部可在 `settings.json` 覆盖，观测接口 `GET /api/harness/runtime`，
手动复位熔断器 `POST /api/harness/runtime/reset`。

### 5.2 任务系统 —— 长任务 / 批量 / 队列

对标 Celery / Temporal / LangGraph 的核心子集，按单体形态裁剪：

- **队列调度**：每队列独立 worker 池、任务优先级、启动限速、运行时暂停 / 恢复；
  失败任务 2s→4s 指数退避重新入队，耗尽重试进终态；
- **长任务（Flow，DAG）**：依赖自动编排（拓扑校验、环拒绝），无依赖分支自动并行；
  `{{步骤id.result}}` 把前步结果注入后步；步骤级 timeout / max_attempts + 流程级看门狗；
  失败策略 `abort`（默认）/ `continue`（只放弃失败分支）；
- **批量任务（Batch）**：一个工具并行 map 到 N 组参数，批内并发受控，单条失败不影响其它；
- **断点续跑**：服务重启后已成功步骤不重算；
- **危险动作确认门**：删除 / 覆盖 / 发送 / 花钱这类步骤执行前需用户批准；
- **失败反思重规划**：步骤失败自动反思并重规划（默认开启）；
- **可视化**：任务中心 + 大屏（前端 `27_task_center.ts` / `30_task_big_screen.ts` / `30_task_tree.ts`）。

### 5.3 分层记忆 —— 上下文省 70%+

四层打包（`memory.py:build_hierarchical_context`），每层独立 token 预算：

1. **短期窗口**：最近轮次，按 `short_term_max_tokens` 预算，单轮超长截断；
2. **长期摘要**：只带最新摘要，`summary_max_tokens` 预算；
3. **常驻长期记忆**：按 importance 取 top-k，`long_term_max_tokens` 预算；
4. **按需召回**：关键词检索，`recall_max_tokens` 强制封顶。

每轮把 raw / packed 估算与 LLM 返回的真实 prompt 用量写入 `context_stats` 表——
**节省比例按表内真实用量核算**，不是估算值（真实会话实测约 70%+）。

### 5.4 技能体系 —— 19 个内置技能

| 技能 | 能干什么 |
| --- | --- |
| `agent_ops` | 委派 DSH / Codex / OpenCode 并查进展；技能工坊：自建自改技能、全网拉取技能 |
| `code_ops` | 代码检索 / 分析 / 修改 / 验证（Python AST 结构感知）、git 全流程、全盘搜索、隔离工作树 |
| `github` | 日常 git、提交前密钥闸门、发版与盯落地、给上游提 PR、PR 审查与 issue 修复 |
| `tasks` | 长任务 / 批量任务 + TODO 清单 + 定时任务 |
| `search` | 四引擎合一：anysearch（通用 + 垂直域 + 批量 + URL 提取）/ tavily / exa / web |
| `media` | 在线影音（B站 / AcFun / 酷我 / 网易云）+ 油管汉化流水线 + 小游戏 + AI 画图 |
| `vision` | 看图：URL / 本地路径 / data URL，像素直接进上下文；自动判定模型是否支持读图 |
| `lark` | 飞书：消息 / 云文档 / 表格 / 多维表格 / 日历 / 任务 / 邮件 / 云盘 / 通讯录 |
| `baidu-drive` | 百度网盘：上传 / 下载 / 转存 / 分享 / 搜索 / 移动 / 复制 / 重命名 / 建目录 |
| `mcp` | 接第三方 MCP server（远程 url 或本地子进程），工具清单按需拉，用完即断 |
| `peer` | 跨机大白联邦：点名 / 当场问话 / 派活 / 留言 / 读信 |
| `linux_native` | 读硬件状态（温度 / 欠压 / 内存 / 负载）、管 systemd、控 GPIO、审网络暴露面、安全审计 |
| `android` | ADB 支配安卓手机：看屏、按文字点、中文输入、读短信 / 验证码、装应用、跑 shell |
| `alibabacloud-workbench-cli` | 管理无公网 IP 的阿里云 ECS：远程命令、文件传输、TCP 端口转发 |
| `playwright` | 无头 Chromium：截图（含整页）、抓页面文本 / 元素、跑任意 Playwright 脚本 |
| `appearance` | 3D 造型 / 场景、嗓音、应用模式；PMX / PMD 转 VRM、Mixamo 动作库 |
| `hotmod` | 服务内模块热重载：改完源码即刻生效，不重启服务（重启会掐断在途对话） |
| `smell-check-main` | 代码异味检查 |
| `project_build` | 从零构建大型项目：想法 → 带 ID 的规格矩阵 → 按模块增量实现 → 验收核算 |

**渐进式披露**：开启 `harness.progressive_disclosure` 后，`on_demand` 技能只注入一句话摘要，
模型用内置 `skill_help` 工具按需拉完整说明书——技能再多也不撑爆上下文。

### 5.5 前端与形象 —— 一个能「被支配」的角色

- **3D**：three.js + three-vrm，VRM 模型加载、GLB 背景场景、相机 / 走位 / 状态切换；
- **动作与表情**：表情引擎、情绪控制器、动作混合器、Mixamo 动作重定向与动作库；
- **语音**：TTS 口型同步、录音、VAD 自动断句、BGM / 音效；
- **强化学习**：约会 / 关系模型、参与度 RL、表情 RL、行为克隆、奖励规格——角色的反应是学出来的；
- **界面**：消息流、工具链卡片、任务中心 / 大屏 / 任务树、角色卡、LLM 供应商设置、
  舞台轮盘、全息舞台、直播室特效、附件面板；
- **VR**：WebXR + VR HUD。

**运行期不需要 `npm install`**：three.js / three-vrm 等库随包放在 `web/vendor/`，由 importmap 引用；
`.ts` 由服务端常驻 Node worker 用 `module.stripTypeScriptTypes` 实时转译。

### 5.6 自我进化 —— 会认错，也会判断

| 机制 | 落盘位置 | 作用 |
| --- | --- | --- |
| 经验库 | 教训条目（当前 496 条） | 被纠正 / 踩坑后写一句话教训，后续对话自动注入 |
| 信条与拒绝 | `conviction.json` | 自己判断出的行为准则；拒绝过的事带理由留痕 |
| 长期事业 | `long_horizon.json` | 跨会话接力：每个项目留「下一轮能直接开跑的原子动作」 |
| 基因适应度 | `gene_stats.json` + `tools/gene_fitness.py` | 给教训 / 信条 / 事业加选择压力，防「谁上过场谁继续上场」 |

基因适应度的判据写在 `GENE_FITNESS.md` 里，几条硬规矩：**曝光不是价值**（曝光是自变量）；
淘汰一条基因要同时满足「在场 ≥ 20 轮」「有对照组且方向一致」「人工复核」；**「测不了」不等于「没用」**——
拿没埋点当删除依据，和拿感觉删除一样不诚实。

### 5.7 联邦 —— 多台大白互相认识

跨机通信：点名（`peer_list`）、当场问话并多轮续接（`peer_call`）、挂断、派活让对方真去执行
（`peer_task`）、留言与读信（`peer_say` / `peer_inbox`）。带节点台账与自更新。

---

## 六、部署与运维

### 6.1 一键启动（幂等、可重复跑）

```bash
./dabai.sh --setup      # Linux / macOS
dabai.bat --setup       # Windows（双击也行）
```

一条命令四件事：**建 venv → 装依赖 → 环境自检 → 启动服务**。

| 命令 | 作用 |
| --- | --- |
| `./dabai.sh` | 启动（没 venv 时自动转成 `--setup`） |
| `./dabai.sh --check` | 只做环境自检，不启动 |
| `./dabai.sh --diag` | 环境诊断：解释器 / 依赖 / 端口占用 / 外部工具 / Node 版本 / systemd 状态 |
| `./dabai.sh --deps` | 依赖审计：启动路径依赖 vs 清单，标出漏网 / 冗余 |
| `./dabai.sh --deps --export` | 导出本机实际依赖（代码 import 反推，带版本） |
| `./dabai.sh --deps --freeze -o lock.txt` | 导出完整环境锁定（pip freeze 全量） |

### 6.2 依赖管理 —— 清单不靠人肉保证

- **清单只有一份**：`requirements-core.txt`。启动脚本与自检都调 `tools/check_deps.py`，不各自内联抄
  （历史上两份各抄 5 个包，`uvloop` / `httptools` / `python-multipart` / `websockets` 四个「缺了就崩或就哑」的
  包全落在盲区）；
- **完整性靠 AST 现算**：`tools/deps_audit.py` 从 `server.py` 沿 import 图递归，切出「缺了 server 就起不来」的
  那批依赖再与清单对账；`importlib.metadata.packages_distributions()` 做「顶层模块名 → 发行包名」映射，不手写对照表；
- **打包前有闸门**：出包前调 `deps_audit --gate`，启动路径上有未声明依赖就拒绝打包；
- **镜像自动挑**：`tools/pip_mirror.py` 并发探测阿里云 / 中科大 / 腾讯云 / 华为云 / 清华 / 官方 PyPI，
  挑当下真能下载的那个源，失败自动换下一个（清华 PyPI 对云厂商 IP 段会间歇 403，写死必然坑人）；
- **半成品环境能补**：`--setup` 的判定是「venv 不在 **或** `check_deps.py --gate` 不过」，
  上次装到一半留下的「能 import 一部分」的 venv 不会被跳过。

### 6.3 服务托管

生产形态是 **systemd 系统级单元**（`Restart=always` / 3s，enabled，带 drop-in 注入环境变量）：

```bash
tools/restart_server.sh              # 重启（委托 systemctl，再自检 + 落报告）
tools/restart_server.sh --delay 20   # 延迟 20 秒动手（给发起方留收尾时间）
tools/restart_server.sh --check      # 只体检不重启（rc=0 表示健康）
journalctl -u myservice.service -f   # 日志在这里，不在文件里
```

重启结果落在 `data/restart_report.txt`（新 PID / 状态 / 监听端口 / 核心文件生效检查 / journal 末尾）。

两条实测经验：① 不要自己 `kill` + 重起——systemd 3 秒后自己拉起，脚本再起的第二个实例只会 bind 失败；
② 进程 stdout 接的是 journald socket，「日志文件为空」不代表没日志。

### 6.4 访问地址

- 默认 `http://127.0.0.1:8001`（`harness.http_only = true`，只监听回源端口，TLS 由前置 nginx 的 8000 终结）；
- 不挂 nginx 时把 `http_only` 改成 `false`，服务监听 8000，有证书则走 HTTPS；
- **HTTPS 才能解锁手机陀螺仪 / VR 模式**（浏览器只在安全上下文暴露传感器 API）；
- 首页要加载 3D 模型和几十个前端模块，**10~30 秒属正常**。

---

## 七、发布与更新

发布链路的最后一道闸在 **GitHub**，不在任何脚本里：

```
deploy/release/build_release.py   打包（内含解包回验：sha256 全对、包内无受保护路径）
        ↓  出包前先过依赖闸门 deps_audit --gate
deploy/gitguard/safe-push.sh      推前密钥扫描（挡得住「推错东西」）
        ↓
.github/workflows/release.yml     tag 触发 → 打包 → environment: release 需管理员在网页点 Approve
        ↓
GitHub Release                    dabai-<版本>.tar.gz + MANIFEST.json
```

为什么把闸放在 GitHub：脚本闸门挡得住「推错东西」，挡不住「谁按下了推送」；
`environment: release` 的 required reviewers 是 GitHub 自己强制的，**这是整套体系里唯一一个连大白自己都绕不过去的门**。

发行包**不含密钥、证书与运行期数据**，代码对它们一律做了「缺了也能起」的处理：

```text
settings.json       含你的 API Key
cert.pem / key.pem  本机 TLS 证书与私钥
deploy/tls/*.key    联邦根 CA 与服务器私钥
chat_memory.db      对话与记忆库
data/               长跑工作区、任务快照、日志
```

清单不用手抄，按 AST 从代码现算：`venv/bin/python deploy/release/build_release.py --list`。

---

## 八、安全边界

| 机制 | 位置 | 干什么 |
| --- | --- | --- |
| 密钥闸门 | `deploy/gitguard/secretscan.py`、`safe_push.py` | 提交 / 推送前扫密钥，命中即拦 |
| 危险动作确认门 | 任务系统 `confirm` 字段 | 删除 / 覆盖 / 发送 / 花钱类步骤执行前需用户批准 |
| 危险命令拦截 | `skills/code_ops`（shell） | 危险命令识别 + 真实路径查找，拒绝臆造路径 |
| 权限收敛 | `agent.py` + 用户身份 | 普通用户收敛到自己的沙箱 |
| 不可逆操作留痕 | 删除 / 覆盖前先列清单 | 用户明确点名的直接删；笼统「清理」先出清单等确认 |
| 网络暴露面审计 | `skills/linux_native` | 查监听端口 / 存储去向 / 安全审计 |

---

## 九、快速开始

### 0. 环境要求

| 依赖 | 版本 | 必需？ | 不装的后果 |
| --- | --- | --- | --- |
| Python | 3.10+ | **必需** | 服务起不来（`./dabai.sh --check` 直接判 FAIL） |
| Node.js | **22.13+** | **必需** | 服务照常启动、网页打得开，但**永远停在「连接中…」**——前端是 TS 源码直服，缺 Node 或版本过低时 `.ts` 原样下发 → 浏览器语法报错 → 模块图崩，而服务端一行错都不报 |
| Git | 任意 | 可选 | 只是不能 clone，下压缩包一样用 |
| ffmpeg | 任意 | 可选 | 音视频能力降级 |
| Chrome / Chromium | 任意 | 可选 | 网页深挖技能降级为 requests |
| ripgrep（`rg`） | 任意 | 可选 | 快速检索降级（有回退实现） |

> Node 版本那个坑值得单说：`module.stripTypeScriptTypes` 的加入版本是 **v22.13.0**；
> `22.6` 是 `--experimental-strip-types` 那个 CLI flag 的版本，别混。

### 1. 申请 API Key

大白本身不带模型额度，对话 / 语音识别 / 画图都走 OpenAI 兼容接口：注册任一兼容供应商拿 key，
或本机装 Ollama 把 `llm_provider` 设成 `"ollama"`。拿到后启动服务 → 网页设置页填入，
或直接改 `settings.json` 的 `api_key`（该文件不入仓）。

### 2. 拿代码 + 启动

```bash
git clone <仓库地址> dabai && cd dabai
./dabai.sh --setup
```

⚠️ **不要放在受保护目录**：Windows 的 `Program Files`、需要 sudo 的 `/usr/local`，
都会卡在建 venv 或写证书那一步（启动脚本会提前拦下并说清原因）。
⚠️ **venv 不要建在 tmpfs 上**：不少系统的 `/tmp` 是内存盘，依赖装到一半会 `No space left on device`。

### 3. 打开网页

浏览器访问 `http://127.0.0.1:8001`，状态点变绿即就绪。

### 4. 命令行调用

```bash
./dabai "把 README 里的错别字改掉"      # 服务在跑 → 接入；没跑 → 本地独立会话
./dabai --local "..."                  # 强制进程内（独立会话，user=cli）
./dabai --remote "..."                 # 服务没跑就报错退出，不偷偷降级
./dabai --user alice "..."             # 指定身份
```

---

## 十、目录结构

```text
dabai/
├── server.py                  # FastAPI 服务入口：路由 / WebSocket / .ts 转译 / TLS
├── agent.py                   # Agent 主循环：模型调用、工具执行、上下文打包
├── memory.py                  # 分层记忆：短期窗口 / 长期摘要 / 常驻记忆 / 按需召回
├── ai_autonomy.py             # 自主行为中枢（感知 → 决策 → 行为指令）
├── harness/                   # 技能与插件运行时：core / runtime / tasks / skills / plugins / state
├── skills/                    # 19 个技能（一个技能一个子目录 + skill.json）
├── plugins/                   # 插件目录
├── web/                       # 前端：TypeScript 源码 + three.js 场景 + vendor 第三方库
├── tools/                     # 95 个运维与自检脚本
├── tests/                     # 110 个 pytest 用例文件
├── deploy/                    # release 打包 / gitguard 密钥闸门 / systemd / windows
├── models/                    # VRM 角色模型、GLB 背景场景
└── data/                      # 运行期数据（不入仓）：任务、日志、长跑工作区
```

---

## 十一、起不来怎么办

先跑 `./dabai.sh --diag`（Windows 是 `dabai.bat --diag`），把输出整段贴出来，绝大多数问题一眼可见。

| 症状 | 原因 | 怎么办 |
| --- | --- | --- |
| 网页永远停在「连接中…」，服务端不报错 | Node 缺失或低于 22.13 | `--diag` 看 `[Node.js]` 那节；Linux 装 `apt install nodejs` 或 `nvm install 22` |
| 启动即崩在 import | 依赖缺失 | `./dabai.sh --check`；`./dabai.sh --setup` 补齐 |
| 装依赖卡住 / 超时 | 镜像源不可达 | `venv/bin/python tools/pip_mirror.py --probe` 看各源状态，或 `export DABAI_PIP_INDEX=<源>` |
| 端口绑不上 | 端口被占用；Windows 保留端口段 | `--diag` 的端口节；Windows 按 server 输出的 `netsh` 命令修一次即可 |
| 建 venv / 写证书一直卡住 | 装在受保护目录 | 换到用户目录 |
| `--setup` 每次都重装 | 有依赖没装上（能力依赖也算） | 看 `check_deps.py` 报的缺哪几个，装完就不再重装 |
| 装完仍报缺 / 不确定清单全不全 | 新增 import 没进清单，或清单虚高 | `./dabai.sh --deps` 看漏网与冗余 |

---

## 十二、已知边界

- **模型能力是外部依赖**：大白不自带额度，回答质量取决于你接的模型；
- **本地优先的代价**：语音识别、画图、网页深挖等能力在缺外部工具时会降级（降级路径都有实现，不会崩）；
- **自我进化的信号还在补**：基因适应度的「价值信号」部分（复发率检测器）尚在分期实施，
  目前只做报表 + 人工拍板，不自动淘汰——淘汰是不可逆决策；
- **强依赖单机**：联邦（peer）能跨机通信，但多机协同不是主路径，主形态仍是「一台机器上的一个运行时」。
