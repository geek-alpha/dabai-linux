# 大白

> 一个使用 JavaScript, Python, HTML, CSS 开发的项目。

![JavaScript](https://img.shields.io/badge/JavaScript-blue) ![Python](https://img.shields.io/badge/Python-blue) ![HTML](https://img.shields.io/badge/HTML-blue)

## ✨ 项目简介

「大白」是一套可长期运行的本地 AI 助手：浏览器端是一个能对话、有 3D 形象、能被支配的角色，
服务端是一套带技能 / 插件 / 长任务 / 分层记忆的运行时。核心代码用 **Python + TypeScript +
HTML/CSS** 写成 —— **718 个源文件、约 25 万行自有代码**（不含 `web/vendor/` 里的第三方库），
仓库共 **1085 个跟踪文件**。

- **对话 / 语音 / 画图**都走 OpenAI 兼容接口：`settings.json` 的 `llm_provider` 可在 `custom`
  （任意兼容网关）与 `ollama`（本机）之间切，画图默认走硅基流动。
- **前端是 TypeScript 源码直服**：`/static` 下的 `.ts` 由服务端常驻 Node worker 用
  `module.stripTypeScriptTypes` 实时转译（`server.py:349`），磁盘上真实存在的 `.js` 原样放行 ——
  所以**运行期不需要 `npm install`**。
- **服务由 systemd 托管**（`myservice.service`，`Restart=always`），外加一层 Linux 原生支配能力：
  GPIO、systemd 服务、进程调度、网络暴露面、存储去向、安全审计。

## 🔑 第一步：申请 API Key（必需）

大白本身不带任何模型额度。**对话模型、语音识别、AI 画图**都走 OpenAI 兼容接口，
需要你自己去任一兼容供应商注册拿 key：

1. 注册 [硅基流动 SiliconFlow](https://cloud.siliconflow.cn/i/ByXrxmTh)（画图接口的默认供应商）
2. 或任意 OpenAI 兼容网关；想全本地就装 Ollama，把 `llm_provider` 设成 `"ollama"`
3. 拿到 key 后：启动服务 → 打开网页 → 设置页填入；或直接改 `settings.json` 的 `api_key`

> `settings.json` 不入仓（里面存你的 key，已进 `.gitignore`）。首次启动会从
> `settings.example.json` 生成一份，`tools/selfcheck.py` 也会顺手补。

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

### 0. 环境要求

| 依赖 | 版本 | 必需？ | 不装的后果 |
| --- | --- | --- | --- |
| **Python** | 3.10+ | **必需** | 服务起不来（`./dabai.sh --check` 直接判 FAIL） |
| **Node.js** | **22.13+**（或 23.2+） | **必需** | 服务照常启动、网页也打得开，但会**永远停在「连接中…」**：前端是 TS 源码直服，靠 Node 自带的 `module.stripTypeScriptTypes` 实时转译（`server.py:349`）；缺 Node 或版本过低时 `.ts` 原样下发 → 浏览器语法报错 → 模块图整个崩，而服务端一行错都不报。该 API 的加入版本是 **v22.13.0**（[nodejs/node#55282](https://github.com/nodejs/node/pull/55282)）——`22.6` 是 `--experimental-strip-types` 那个 CLI flag 的版本，别混 |
| Git | 任意 | 可选 | 只是不能 `git clone`，下压缩包一样用 |
| ffmpeg | 任意 | 可选 | 音视频处理能力降级 |
| Chrome / Chromium | 任意 | 可选 | 网页深挖技能降级为 requests |
| ripgrep（`rg`） | 任意 | 可选 | 快速检索降级（有回退实现） |

分平台手把手教程：**Linux → [LINUX.md](LINUX.md)**。Windows 用户看本节的 `dabai.bat` 命令即可，
起不来先跑 `dabai.bat --diag`。

### 1. 拿到代码

```bash
# 方式 A：下载发行包（不需要 Git）—— Releases 页下 dabai-<版本>.tar.gz 后解压
tar -xzf dabai-1.1.21.tar.gz && cd dabai-1.1.21

# 方式 B：克隆仓库
git clone <仓库地址> dabai && cd dabai
```

⚠️ **不要放在受保护目录**：Windows 的 `Program Files`、需要 sudo 的 `/usr/local`，都会让非管理员 /
非 root 卡在建 venv 或写证书那一步（`dabai.bat` 会提前拦下并说清原因）。Windows 建议放
`C:\Users\<你的名字>\dabai`。

### 2. 一键启动

```bash
./dabai.sh --setup      # Linux / macOS
dabai.bat --setup       # Windows（双击也行）
```

一条命令做四件事，幂等、可重复跑：**建 venv → 装依赖 → 环境自检 → 启动服务**。

| 命令 | 作用 |
| --- | --- |
| `./dabai.sh` | 启动（没 venv 时自动转成 `--setup`） |
| `./dabai.sh --setup` | 强制重跑安装：建 venv + 装依赖 + 自检 + 启动 |
| `./dabai.sh --check` | 只做环境自检，不启动 |
| `./dabai.sh --diag` | 环境诊断：解释器 / 依赖 / 端口占用 / 外部工具 / Node 版本 / systemd 状态 |
| `./dabai.sh --deps` | 依赖审计：启动路径依赖 vs 清单，标出漏网 / 冗余 |
| `./dabai.sh --deps --export` | 一键导出本机实际依赖（代码 import 反推，带版本） |
| `./dabai.sh --deps --freeze -o lock.txt` | 一键导出完整环境锁定（pip freeze 全量） |

显式设了 `DABAI_PYTHON` 的人不会被强建环境 —— 那是刻意不用 venv 的人，越界替他建几百 MB 属于多管闲事。

**依赖清单只有一份**：[requirements-core.txt](requirements-core.txt)。启动脚本与自检都调
`tools/check_deps.py`，不各自内联抄 —— 以前 `dabai.sh` 与 `dabai.bat` 各抄了 5 个包，于是
`uvloop` / `httptools` / `python-multipart` / `websockets` 四个「缺了就崩或就哑」的包全在盲区：
装完判定「齐全」，问题留到启动时才炸。

**清单完不完整不靠人肉保证**：[tools/deps_audit.py](tools/deps_audit.py) 从 `server.py` 沿
import 图递归，切出「缺了 server 就起不来」的那批依赖，再与清单对账。AST 扫 import（含
`importlib.import_module` 字符串形式），用 `importlib.metadata.packages_distributions()` 做
「顶层模块名 → 发行包名」映射，不手写对照表。

```bash
./dabai.sh --deps                            # 报告：漏网 / 未装 / 冗余 / 间接依赖
./dabai.sh --deps --export                   # 导出直接依赖（换机器部署用）
./dabai.sh --deps --freeze -o lock.txt       # 导出全量锁定（精确复现环境用）
venv/bin/python tools/deps_audit.py --gate   # 启动路径有漏网/缺包 → 退出 1，可挂 CI
```

为什么非要多一层「启动路径」：全项目扫描会把一次性工具脚本的依赖（`tools/locate_icon.py`
的 `cv2`）和历史脚本（`dabai.py` 依赖的本机私有包）一并算进来，按那份清单装是过度安装。
反向的错同样要防 —— `aiohttp` / `numpy` / `websockets` 曾被声明成「启动必需」，而 `server.py`
对它们零引用：用户白装，缺包时还报出不存在的问题。现在它们归入能力依赖（`websockets` 确实
是 uvicorn 的 WS 实现，但缺了服务照常启动、只是实时对话连不上）。

清单里还有一类「代码无静态 import 但删了会崩」的间接依赖（`uvloop` / `httptools` 由
`server.py:8517` 按名加载，`python-multipart` 由 FastAPI 加载 `File`/`UploadFile` 时按名需要）。
它们在 `deps_audit.py` 的 `INDIRECT` 表里逐条带证据 —— 填不出证据的就不是间接依赖，该从清单删。

**打包前有一道依赖闸门**：[deploy/release/build_release.py](deploy/release/build_release.py) 出包前调
`deps_audit --gate`，启动路径上有未声明的依赖就拒绝打包。判据是「新机器装完
`requirements-core.txt` 能不能起来」—— 所以只拦「清单缺声明」，不拦「构建机自己没装」
（那是本机环境问题，打出来的包没问题）。闸门跑不成时也会出声，不静默放行。

**扫描范围只有一份名单**：[tools/scan_scope.py](tools/scan_scope.py)。以前 `deps_audit.py` 的
`SKIP_DIRS` 和 code_ops 的 `NOISE_DIRS` 各写一份，一边排了另一边没排，`tools/vendor_ots` 的
gitdb / Cryptodome 就混进了影响面排名。两份名单口径有意不同：依赖扫描多排 `data/`（轮快照
里有被改文件的代码副本）与 `models/`（二进制资源），代码搜索不排 —— 用户自己的脚本可能就
放在那些目录里，挡掉就再也搜不到。

**依赖默认从国内镜像装**：`tools/pip_mirror.py` 并发探测阿里云 / 中科大 / 腾讯云 / 华为云 /
清华 / 官方 PyPI，挑当下真能下载的那个源，装失败自动换下一个。为什么不写死一个 —— 清华 PyPI
对云厂商 IP 段会间歇 403（index 页 200、包文件 403），写死必然坑掉一部分人。

```bash
venv/bin/python tools/pip_mirror.py --probe     # 看各源实测状态与耗时
export DABAI_PIP_INDEX=https://...              # 强制指定源（跳过探测）
```

**`--setup` 会补齐半成品环境**：只看 `venv/bin/python` 存不存在是不够的 —— 上次装到一半
（磁盘满 / 断网）会留下一个「能 import 一部分」的 venv，再跑 `--setup` 会直接跳过，问题拖到
启动时才炸。现在的判定是「venv 不在 **或** `check_deps.py --gate` 不过」。

⚠️ **venv 不要建在 tmpfs 上**：不少系统的 `/tmp` 是内存盘（`df -h /tmp` 看容量），依赖装到一半
会 `No space left on device`，留下一个半成品环境。

### 3. 打开网页

浏览器访问 **http://127.0.0.1:8001** —— 本机当前配置是 `harness.http_only = true`：服务只监听
回源端口 8001（纯 HTTP），TLS 由前置 nginx 的 8000 终结。

不挂 nginx 时把 `harness.http_only` 改成 `false`，服务会监听 8000 并在 `cert.pem` / `key.pem`
存在时走 HTTPS（`server.py:8536`）—— HTTPS 才能解锁手机陀螺仪 / VR 模式这类传感器 API
（浏览器只在安全上下文里暴露它们）。

首页要加载 3D 模型和几十个前端模块，**10~30 秒属正常**，状态点变绿即就绪。

### 4. 前端开发（可选，运行期不需要）

跑大白只需要 Python + Node.js，**不需要 `npm install`**：three.js / three-vrm 等库已随包放在
`web/vendor/`，由 `web/index.html` 的 importmap 引用。

```bash
npm install          # 只有改前端才需要：装 vite / tsc / three 这些构建期依赖
npm run dev          # vite 开发服务器（后端需已在跑）
npm run typecheck    # tsc 类型检查
```

### 5. 服务托管与重启（本机实际部署方式）

本机 server.py 由**系统级 systemd 单元** `myservice.service` 托管（`/etc/systemd/system/myservice.service`，`Restart=always` / 3s，enabled，另有 `10-recovery.conf`、`20-linux-native.conf`、`30-secrets.conf` 三个 drop-in 注入环境变量）。

```bash
tools/restart_server.sh              # 重启（委托 systemctl，再自检 + 落报告）
tools/restart_server.sh --delay 20   # 延迟 20 秒动手（给发起方留收尾时间）
tools/restart_server.sh --check      # 只体检不重启（rc=0 表示健康）
journalctl -u myservice.service -f   # 日志在这里，不在文件里
```

重启结果落在 `data/restart_report.txt`：新 PID / 状态 / 监听端口 / `tools/reload_check.py` 的核心文件生效检查 / journal 末尾。

两条经验（2026-09-13 实测）：① 不要自己 kill + spawn —— systemd 会在 3 秒后自己拉起，脚本再起的第二个实例只会 bind 失败；② 进程的 stdout 接的是 journald socket，所以「日志文件为空」不代表没日志，先看 `journalctl -u <unit>`。判断谁在托管只需一条命令：`cat /proc/<pid>/cgroup`。

### 6. 命令行调用（与网页输入框同一条路）

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
├── server.py                  # FastAPI 服务入口：路由 / WebSocket / .ts 转译 / TLS
├── agent.py                   # Agent 主循环：模型调用、工具执行、上下文打包
├── memory.py                  # 分层记忆：短期窗口 / 长期摘要 / 常驻记忆 / 按需召回
├── harness/                   # 技能与插件运行时：core.py / 热重载 / 渐进式披露
├── skills/                    # 技能目录（一个技能一个子目录 + skill.json）
├── plugins/                   # 插件目录
├── web/                       # 前端：TypeScript 源码 + three.js 场景
│   ├── app.ts                 # 入口（module 方式引用各 .ts）
│   ├── core/                  # 场景、模型加载、渲染循环
│   ├── character/             # 角色行为：动作、表情、点击交互
│   └── vendor/                # three.js / three-vrm 等第三方库（随包发布，运行期免 npm）
├── tools/                     # 运维与自检：check_deps / deps_audit / scan_scope / pip_mirror / install_node / selfcheck
├── tests/                     # pytest 套件
├── deploy/                    # 发布与部署：release/build_release.py、windows/launch.py、systemd
├── models/                    # VRM 角色模型、GLB 背景场景
└── data/                      # 运行期数据（不入仓）：任务、日志、长跑工作区
```

## 📦 发布包里有什么、缺什么

发行包**不含密钥、证书与运行期数据**，代码对它们一律做了「缺了也能起」的处理。

**不入仓的文件**（`.gitignore` 里，每台机器自己生成）：

```text
settings.json      # 含你的 API Key
cert.pem / key.pem # 本机 TLS 证书与私钥（.gitignore:121-122）
deploy/tls/*.key   # 联邦根 CA 与服务器私钥（.gitignore:203-206 补的规则，之前只盖了 *.pem）
chat_memory.db     # 对话与记忆库
data/              # 长跑工作区、任务快照、日志
```

清单不用手抄，按 AST 从代码里现算：

```bash
venv/bin/python deploy/release/build_release.py --list
# 版本 1.1.21：1085 个文件会进包
```

## 🩺 起不来怎么办

先跑 `./dabai.sh --diag`（Windows 是 `dabai.bat --diag`），把输出整段贴出来，绝大多数问题一眼可见。

| 症状 | 原因 | 怎么办 |
| --- | --- | --- |
| 网页永远停在「连接中…」，服务端不报错 | Node 缺失或低于 22.13 | `--diag` 看 `[Node.js]` 那节；Linux 装 `apt install nodejs` 或 `nvm install 22`，Windows 跑 `dabai.bat` 会自动装一份到用户目录 |
| 启动即崩在 import | 依赖缺失 | `./dabai.sh --check`（走 `tools/check_deps.py`）；`./dabai.sh --setup` 补齐 |
| 装依赖卡住 / 超时 | 镜像源不可达 | `venv/bin/python tools/pip_mirror.py --probe` 看各源状态，或 `export DABAI_PIP_INDEX=<源>` |
| 端口绑不上 | 端口被占用；Windows 保留端口段 | `--diag` 的端口节；Windows 按 server 输出里的 `netsh` 命令修一次即可，不必长期用管理员跑 |
| 建 venv / 写证书一直卡住 | 装在受保护目录 | 换到用户目录（Windows 建议 `C:\Users\<你>\dabai`） |
| `--setup` 每次都重装 | 有依赖没装上（能力依赖也算） | 看 `check_deps.py` 报的缺哪几个，装完就不再重装 |
| 装完仍报缺 / 不确定清单全不全 | 新增 import 没进清单，或清单虚高 | `./dabai.sh --deps` 看漏网与冗余；`--deps --export` 导出本机实际依赖 |

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
