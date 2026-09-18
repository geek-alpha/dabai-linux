# 大白在 Linux 上运行

> 目标：**核心能力在 Linux 上可跑**（Web 界面 + 3D 角色 + 对话 + 技能 + agent/harness + 任务中心）。
> 依赖 Windows 专有软件的能力（汉化流水线、便携 Chrome、Blender 便携版）自动降级或明确报错，
> 不会把整个服务拖崩。

## 1. 快速开始

```bash
# 真·一键（首次跑这条就够：建 venv → 装依赖 → 自检 → 启动；幂等，重复跑安全）
./dabai.sh --setup
```

想分步、或排查问题：

```bash
# 0) 系统依赖（Debian/Ubuntu）
sudo apt-get install -y python3-venv python3-dev ffmpeg ripgrep

# 1) 建 venv + 装依赖（逐包安装，装不上的会汇总提示，不中断）
./tools/linux_setup.sh --venv

# 2) 环境自检（推荐每次启动前跑）
./dabai.sh --check        # 或 python3 tools/linux_selfcheck.py

# 3) 启动
./dabai.sh
```

系统包也想自动装：`./tools/linux_setup.sh --all`（sudo 装系统包 + venv + 自检）。

## 2. 环境变量

| 变量 | 作用 | 默认 |
|---|---|---|
| `DABAI_PYTHON` | 指定解释器 | `venv/bin/python` → `python3` |
| `DABAI_BLENDER` | Blender 可执行文件（PMX→VRM 技能用） | 自动探测 `PATH` / 常见路径 |
| `DABAI_CHROME` | Chrome/Chromium（网页深挖用） | 自动探测 `PATH` / 常见路径 |
| `DABAI_HANHUA_ROOT` | 外部汉化项目根目录 | `D:\AI\油管视频汉化`（仅 Windows 存在） |
| `DABAI_SEARCH_ENGINES` | anysearch/exa 搜索引擎脚本根目录 | `skills/search/engines/`（缺失时给可诊断提示） |
| `DABAI_FQ_ROOT` | fq 翻墙启动器所在目录 | Windows 默认 `D:\AI\Chrome141_AllNew_2025.10.3`；POSIX 探测 `/opt/fq` 等 |
| `DISPLAY` / `WAYLAND_DISPLAY` | 图形会话（截屏用） | 无则截屏明确报错 |

## 3. 平台差异都收敛在哪

**唯一平台出口：`platform_compat.py`**（项目根）。业务代码不再自己判断 `os.name`，
需要平台分支时调它：

| 能力 | 函数 | Windows | Linux/macOS |
|---|---|---|---|
| 隐藏控制台 | `no_window_flags` / `spawn_kwargs` | `CREATE_NO_WINDOW` | 返回 0 / `start_new_session` |
| 整树终止 | `terminate_tree` | `taskkill /T /F` | `killpg(SIGTERM→SIGKILL)` |
| 单进程强杀 | `kill_pid` | `TerminateProcess` | `SIGKILL` |
| 退出码 | `process_exit_code` | `GetExitCodeProcess` | `/proc/<pid>/stat` |
| 进程清单 | `list_processes` | `tasklist /FO CSV` | `ps -eo pid,comm,args` |
| 监听端口 | `list_listening_ports` | `netstat -ano -n` | `ss -ltnp` → `netstat -ltnp` |
| 磁盘空间 | `disk_free` | `GetDiskFreeSpaceExW` | `shutil.disk_usage` |
| 跨进程文件锁 | `lock_file` / `unlock_file` | `msvcrt.locking` | `fcntl.flock` |
| 用户目录 | `user_dir` / `search_roots` | `Desktop/Downloads/...` + 盘符 | XDG / 本地化名 + `/mnt` |

## 4. 各能力的平台状态

| 能力 | Linux | 说明 |
|---|---|---|
| Web 服务 / 3D 角色 / 对话 | ✅ 完整 | 纯 Python + 浏览器 |
| 代码工程技能（检索/改码/git/工作树） | ✅ 完整 | 子进程标志与进程终止走兼容层 |
| 任务中心 / 独立进程任务 | ✅ 完整 | 跨进程文件锁已支持 `fcntl` |
| 系统体检（进程/端口/磁盘） | ✅ 完整 | 换 `ps` / `ss` / `shutil` |
| 系统文件搜索（sys_find/sys_recent/sys_locate） | ✅ 完整 | 扫主目录 + `/mnt`、`/media` 挂载点；`sys_locate` 按可执行位判定，不依赖 `where` |
| 工作区面板 / 手机端目录下钻 | ✅ 完整 | 常用目录走 XDG + 本地化名，根目录为主目录 + 挂载点（`platform_compat.browse_roots`） |
| 联网搜索 | ⚠️ 视引擎 | `web_search`/`read_web` 自带实现可用；anysearch/exa 引擎脚本需放到 `skills/search/engines/`，tavily 需 `tvly` CLI；JS 深挖需系统 Chrome |
| 翻墙代理（fq_ctl / proxy_test） | ⚠️ 需自备 fq | 设 `DABAI_FQ_ROOT` 或把 `fq` 放进 PATH；未配置时明确报错 |
| 截屏 | ⚠️ 需图形会话 | `mss` → `PIL.ImageGrab` → `pyautogui` 三级回退 |
| 语音（TTS/ASR） | ⚠️ 视依赖 | `edge-tts` / `faster-whisper` 均支持 Linux；麦克风需 PulseAudio |
| PMX→VRM 模型转换 | ⚠️ 需系统 Blender | 装 Blender 后设 `DABAI_BLENDER` |
| 油管视频汉化 | ❌ 仅 Windows | 依赖外部 Windows 项目，需 `DABAI_HANHUA_ROOT` 才有意义 |
| Minecraft 陪玩 | 视模块 | 纯网络协议，与平台无关 |

## 5. 验证记录（真实执行结果）

**环境**：Docker `python:3.11-slim`（Linux 内核，挂载本仓库）＋ Windows 主机回归。

| 验证项 | 命令 | 结果 |
|---|---|---|
| 兼容层冒烟 14 项（双平台同一套用例） | `python tools/linux_smoke_test.py` | Windows 11 **14/14 PASS**（验 `taskkill` / `tasklist` / `creationflags` 分支）；Linux（`python:3.11-slim` 容器）**14/14 PASS**（验 `/proc` / 进程组 / `fcntl` 分支） |
| 启动脚本语法 | `bash -n dabai.sh` / `bash -n tools/linux_setup.sh` | 均通过 |
| 环境自检 | `bash dabai.sh --check` | 20 项检查；缺依赖时正确报阻塞并退出码 1 |
| 全项目语法 | `python -m compileall`（排除归档/资源目录） | `COMPILE_OK` |
| Windows 回归 | `list_processes` / `list_listening_ports` / `disk_free` / `terminate_tree` / `system_check` | 373 进程、88 端口、`taskkill /T /F 成功`（探针父子进程残留为空），与改造前一致 |

冒烟明细（当前平台分支逐条真跑，不是“能 import”就算过）：
平台识别、子进程参数、进程清单、监听端口、磁盘空间、进程存活探测、整树终止进程、
单进程强杀、退出码读取、跨进程文件锁、搜索根目录、Windows 常量隔离、
技能层 `system_check`、技能层 `find_file`。

跨平台说明：用例期望值按当前系统切换（Windows 不要求 pid1、不要求 `start_new_session`），
所以两个平台都应该是全绿；只在一个平台上绿 = 兼容层走了错分支。

调试中发现并修掉的两个真问题（值得记住）：
1. `ps` 在精简镜像里不存在 → 进程清单改为**优先读 `/proc`**，`ps` 仅作 macOS 回退；
2. **僵尸进程被误判为存活** → `pid_alive` 把 `/proc/<pid>/stat` 的 `Z` 状态视为已退出，
   否则“杀完还认为活着”，会导致反复重试终止、任务中心误报任务仍在运行。

### 第二轮：技能层全量适配

技能层不再出现盘符与 Windows 专有命令，差异继续收敛到 `platform_compat.py`。

| 改动点 | 文件 | 实测结果 |
|---|---|---|
| 文本搜索兜底 | `skills/code_ops/shell_impl.py` | Windows `findstr` / POSIX `grep -rnI`；并过滤 `$ cmd` 回显行 |
| 可执行程序定位 | `skills/code_ops/sys_search_impl.py` | 弃用 `where` → 枚举 PATH + 可执行位：`python3` → `/usr/bin/python3`、`/bin/python3` |
| 全盘搜索根目录 | `skills/code_ops/sys_search_impl.py` | 原 `os.listdrives()` 在 Linux 恒为空（功能等于不可用）→ 改为主目录 + `/mnt`、`/media`：实测返回 6 个真实目录 |
| 搜索引擎路径 | `skills/search/skill.py` | 去掉 `D:\AI\...` 硬编码 → 环境变量/技能目录探测；缺失时提示不再指向 Windows 路径 |
| fq 翻墙 | `skills/search/fq_impl.py` | `cmd /c fq.cmd` → 平台分派；未配置时返回明确降级提示 |
| ffmpeg 枚举 | `skills/media/video_lib.py` | POSIX 按可执行位判定、路径区分大小写：`/usr/bin/ffmpeg`、`/bin/ffmpeg` |
| 工作区根目录 | `server.py` + `platform_compat.browse_roots` | 常用目录走 XDG/本地化名，根目录为主目录 + 挂载点：实测 `['/home/wxf', '/mnt', '/media', '/opt', '/srv']` |
| 工具描述文案 | `skills/*/skill.json`、`references/*.md` | 去掉“Windows 命令 / dir / tasklist / 盘符”表述，避免模型在 Linux 上瞎用 Windows 命令 |

回归：`python tools/linux_smoke_test.py` **14/14 PASS**（含技能层 `system_check`、`find_file`）；
`server.py` / `platform_compat.py` 语法通过；技能模块 import 冒烟 4/4 通过。

## 6. Linux 原生集成（让大白成为系统的一部分）

> 目标升级：不只是「能跑」，而是**与系统一体** —— 被 systemd 守护、能感知硬件、
> 能操作服务、能跟桌面会话对话。工具入口 `skills/linux_native/`，
> 部署件在 `deploy/systemd/`（详见 `deploy/systemd/README.md`）。

### 6.1 三个实测发现（不是推测，都会改变做法）

| 发现 | 后果 |
|---|---|
| 大白**已经**由**系统级** `/etc/systemd/system/myservice.service` 托管（enabled + active，User=wxf） | 不要再建用户级 unit —— 会抢 `8000/8001` 端口造成双实例互踢。只查 `systemctl --user` 是看不见自己的 |
| `/sys/fs/cgroup/cgroup.controllers` = `cpuset cpu io pids`，**没有 `memory`** | `MemoryMax`/`MemoryHigh` 不生效，`systemd-oomd` 装了也没用；905MB 机器上无法给大白设内存天花板 |
| `journalctl --user -u <unit>` 恒返回 `No journal files were found` | 用户服务日志**落在系统 journal**，必须用 `journalctl --user-unit=<unit>`；系统级 unit 用 `-u` |

另：`journalctl --since=2h` 解析失败，须写 `--since=-2h`（`linux_service` 已自动规范化）。

### 6.2 新技能 linux_native（5 个工具）

| 工具 | 作用 |
|---|---|
| `linux_senses` | SoC 温度、欠压/降频、CPU 频率与调频策略、内存与 Swap、**zram 压缩比**、负载、磁盘、自身开销 → 判定（healthy/notice/strained/critical）+ 可执行建议 |
| `linux_guard` | 派重活前的资源闸门：`need=heavy` 时检查温度 ≥72°C / 可用内存 <150MB / 负载饱和，任一不满足则拒绝并说明理由 |
| `linux_service` | systemd **双 scope** 服务管理：list / status / logs / events（全系统近期错误）/ start / stop / restart |
| `linux_notify` | 桌面通知（`notify-send` → portal D-Bus 回退） |
| `linux_media` | MPRIS 当前播放 + play/pause/next/prev |

设计要点：
- **感知失败报 unknown，不报 healthy** —— 把读不到当正常是最危险的假阳性（与 §5 的过滤器兜底同类问题）；
- **数据源全部只读、零特权、零依赖**（`/sys`、`/proc`、`vcgencmd`），任何一项读不到只标记该项，不整体失败；
- **服务控制区分系统级/用户级**：系统级需 sudo，工具会明确提示并给出可执行命令；
- **自停闸门**：对大白自己的 unit 执行 `stop` 会被拒绝（显式 stop 不触发 Restart，等于永久下线），需 `confirm=true`。

### 6.3 系统级增强（drop-in，需 sudo 一次）

`deploy/systemd/myservice.service.d/20-linux-native.conf` —— 对现有 unit 的最小增量：

| 项 | 原值 | 新值 |
|---|---|---|
| `SyslogIdentifier` | 未设（日志 tag 是 `python`） | `dabai` |
| `PATH` | 不含 `~/.local/bin` | 前置 `~/.local/bin`（sb-proxy 所在） |
| `CPUWeight` / `IOWeight` | 100 | 200（对话优先于代理） |
| `TasksMax` | 806 | 512 |
| `OOMPolicy` | `stop` | `continue`（子进程被 OOM 不连坐） |
| 12 项加固 + `UMask=0077` | 全关 | 开启 |

**刻意不加** `ProtectSystem=strict` / `ProtectHome` / `PrivateTmp` / `SystemCallFilter`：
大白要读写任意工作区、执行任意命令、spawn 子智能体，锁死文件系统等于废掉核心能力。
`RestrictAddressFamilies` **必须含 `AF_NETLINK`**，否则 `system_check` 的 `ss -ltnp` 会静默失败。

### 6.4 定期体检（用户级 timer，免 sudo）

`deploy/systemd/dabai-health.{service,timer}` —— 开机 2 分钟后首跑，此后每 10 分钟：
把一行式状态写进 journald，`strained`/`critical` 时弹桌面通知（同档位 1 小时冷却，升级档位立即提醒）。

选 timer 不选后台线程：开机自动拉起、错过补跑（`Persistent=true`）、**零常驻内存**。
写 journald 不写文件：本机 `Storage=volatile`（内存），**不碰 SD 卡**，且自带时间索引与轮转。

```bash
journalctl --user-unit=dabai-health.service -n 20          # 看历史趋势
```

### 6.5 验证记录（真实执行结果）

| 验证项 | 命令 | 结果 |
|---|---|---|
| linux_native 工具全量 | 19 用例（含双 scope、非法参数、自停闸门、时间格式） | **19/19 通过，0 抛异常** |
| 系统级 unit 可见性 | `linux_service(status, myservice)` | ✓ 识别为 system 级并提示「这是大白自己的服务」 |
| 用户级 unit 日志 | `linux_service(logs, sing-box)` | ✓ 正确走 `--user-unit=` |
| 未知 unit | `linux_service(status, no-such-unit)` | ✓ 明确报 not-found（不再伪装成「存在但没跑」） |
| zram 压缩比 | 对照 `zramctl` | ✓ 3.38:1（278.5MB → 82.3MB），**逐位一致** |
| 体检 timer | `list-timers` + 手动触发 | ✓ 已排程，日志进 journal（tag `dabai-health`） |
| unit 语法 | `systemd-analyze verify`（3 个文件） | ✓ 全部无告警 |

调试中发现并修掉的真问题（值得记住）：
1. **`StartLimitIntervalSec` 放错 section** —— 必须在 `[Unit]`，放 `[Service]` 会被 systemd
   **静默忽略**（`verify` 才报 `Unknown key`），等于崩溃循环保护根本没生效。dabai 与 sing-box 两处同错。
2. **zram 读的 sysfs 节点不存在** —— `/sys/block/zram0/orig_data_size` 实测不存在，
   真实数据源是 `mm_stat`（空格分隔），原实现压缩比**从来没读到过**（恒 None）。
3. **感知失败报 healthy** —— 所有数据源失效时 verdict 仍返回 `healthy` + 「各项正常」，
   假阳性比没感知更危险；已改为 `unknown`。
4. **行号编辑打偏** —— 用 `line_start/line_end` 改代码时行号估算错误，误删了 `storage()` 的挂载解析；
   教训：改已有文件用 `replace` 精确锚点，别用行号猜。


### 6.6 支配能力扩展（从「感知」到「支配」，5 个新工具）

§6.2 的 5 个工具偏「感知 + 服务」。这一轮补齐**支配**：控硬件、管进程、审网络、查存储、做安全审计。
技能升到 v1.1.0，共 10 个工具。

| 新工具 | 实现文件 | 能力 |
|---|---|---|
| `linux_gpio` | `gpio_impl.py` | 54 个物理 GPIO 引脚：读功能/电平/上下拉、驱动输出、闪灯、控板载 LED |
| `linux_process` | `sysadmin_impl.py` | 排行 / 详情 / 信号 / nice + CPU 亲和性 |
| `linux_net` | 同上 | 监听端口 / 连接 / 接口流量 / 暴露面审计 |
| `linux_storage` | 同上 | 挂载点 + inode + SD 卡信息 / 目录排行 / 大文件 / 清理候选 |
| `linux_audit` | 同上 | 暴露面 / SSH / 账号提权 / SUID / 可疑进程 / 失败服务 |

**为什么 GPIO 是重点**：这是树莓派相对普通 Linux 的本质差异 —— 唯一能伸进物理世界的入口。
本机三个 Python GPIO 库（gpiozero / RPi.GPIO / lgpio）都已装、用户在 `gpio` 组、`/dev/gpiomem` 可读写，
**零配置即可用**。最终实现选了 `pinctrl`（官方调试工具）而非 Python 库：芯片映射正确、
输出可解析、无库版本兼容风险。

**安全闸门（三类）**：

1. **GPIO 写闸门** —— 54 个引脚只放行 17 个「通用 IO」。HAT EEPROM(0/1)、I2C、SPI、UART、
   PWM、**电源管理(46/47)**、**SD 控制器(48-53)** 共 27 个专用脚一律拒绝。写前还检查该脚是否
   已被别的驱动占用（alt function）—— 抢过来会让对方功能失效。
2. **进程信号闸门** —— 拒绝向 PID 1、大白自己、**自己的祖先链**、内核线程、关键服务发信号。
   祖先链那条最要紧：大白从自己拉起的 shell 里操作时，杀祖先 = 回复断在半路。
3. **清理只报告不执行** —— `storage clean` 只统计候选，删不删由用户决定。

**实况数据（2026-09-11 实测）**：

- GPIO：54 个物理引脚（17 可写 / 27 专用只读）；LED `ACT`/`PWR` 是物理灯，`default-on`/`mmc0` 是虚拟触发器
- SD 卡：SanDisk `SC16G`，**2017-11 出厂**（快 9 年），累计读 111,272 次（2.9GB）/ 写 11,094 次（0.7GB）
- 暴露面：对外 5 个 —— :22 ssh、:80 nginx、:111 **rpcbind（多余）**、:8000/:8001 大白
- 远程兜底：`rpi-connect`（**用户级**服务）在线，WiFi 断不是彻底失联
- 安全审计：高危 0 / 中危 1（rpcbind 暴露）/ 提示 4 / 正常 4

**这一轮挖出的真 bug（全是「不报错但结果是错的」）**：

1. **`/etc/passwd` 解析静默失败** —— `for line in open(f)` 后直接 `split(":")`，最后一个字段带
   `"\n"`，shell 永远匹配不上 `/etc/shells`，返回空列表却看不出错。已加 `.strip()`，并把
   「结果为空」升级成 MED 项而不是静默跳过。
2. **SD 卡磨损节点不存在，却被报成「需 root」** —— 节点不存在 ≠ 权限不足。已改为如实报
   「本卡不上报」，并补上型号/出厂日期/累计读写量。
3. **`pinctrl` 输出格式不一致** —— 输入脚 `17: ip -- | lo`，输出脚 `47: op -- -- | hi`（多一列）。
   写死字段数的正则会漏掉 GPIO47 整行。
4. **FWGPIO 混进引脚表** —— `pinctrl get` 会吐 100+ 的固件内部信号（BT_ON/WL_ON/LAN_RUN），
   不对应排针。不滤掉会让引脚数从 54 变 61。
5. **用户级服务漏判** —— `rpi-connect` 是用户级 unit，系统级 `systemctl is-active` 返回 inactive，
   导致「无兜底通道」的错误结论。改用 `pgrep -x`。
6. **双栈监听重复报** —— `0.0.0.0:80` 与 `[::]:80` 是同一服务，按端口合并后才不刷屏。

**一个工具行为要记住**：同一参数重复调用 `linux_*` 工具会返回**缓存结果**（harness 行为）。
所以**改完代码要用 shell 直接跑验证**，不能靠再调一次工具 —— 那会拿到旧结果，误以为改动没生效。

### 6.7 API 密钥环境变量化（`deploy/secrets/`）

把散落在 3 个 JSON 里的 7 个唯一 API Key（展开 34 个变量）汇聚成一份
系统环境变量文件，并在配置变化时**实时同步**。

**架构：单向派生，不是双向同步**（双真源必然产生「改了哪边才算数」的歧义）

```
真源   settings.json / codex_config.json / stt_config.json / tts_config.json
  │
  │  sync_secrets.py      ← JSON 一变就重新生成
  ▼
派生   /etc/dabai/secrets.env        root:wxf 0640
  ├── systemd  EnvironmentFile=      → myservice 及其全部子进程
  └── login    /etc/profile.d/*.sh   → wxf 的交互式 shell
```

**实时同步的实现**：`dabai-secrets.path`（systemd path unit）监听 4 个 JSON 的
`PathModified`，变化即触发 `dabai-secrets-sync.service` 重新生成。再加 10 分钟
timer 兜底（inotify 在网络文件系统上可能漏事件）。同步器**幂等**——内容无变化
时不写盘，否则 path unit 会被自己触发成死循环。

**变量命名**（用供应商 `id` 而非中文名派生，改显示名不会断掉变量）

| 变量 | 来源 |
|---|---|
| `DABAI_API_KEY` / `_BASE_URL` / `_MODEL` | `settings.json` 顶层 |
| `DABAI_IMAGES_API_KEY` / `_BASE_URL` | `settings.json` 图像生成 |
| `DABAI_PROV_<ID>_API_KEY` / `_BASE_URL` / `_MODEL` | `llm_providers[]`（`prov-5cd04264` → `5CD04264`） |
| `DABAI_ACTIVE_*` | 当前激活供应商（`llm_provider_id` 指向的那个） |
| `DABAI_CODEX_*` / `DABAI_STT_API_KEY` / `DABAI_TTS_API_KEY` | 对应配置文件 |

**手工变量共存**：`EXA_API_KEY` / `TAVILY_API_KEY` / `GITHUB_TOKEN` 这类没有 JSON
真源的密钥，写在 `MANAGED` 标记块**之外**，同步器永不触碰。变量名有白名单
（`DABAI_`/`EXA_`/`TAVILY_`/`GITHUB_`/…），`PATH`/`LD_PRELOAD`/`IFS` 一律拒绝。

#### 安全设计（每条都有实测依据）

| 决定 | 依据 |
|---|---|
| 用 `EnvironmentFile=`，**不用** `Environment=` | `systemctl show` 把 `Environment=` 明文列给任何用户；`EnvironmentFile=` 只暴露路径（已实测对比） |
| **不写** `/etc/environment` | 那是 0644 全局可读 |
| `profile.d` 钩子先判 `[ -r ]` | 文件 0640 `root:wxf`，非 wxf 组用户判 false 静默跳过（已用 `nobody` 实测拒绝） |
| 值只接受安全字符集，越界直接报错 | systemd `EnvironmentFile` 与 POSIX shell 的转义规则**不同**，硬转义就是在赌 |
| 原子写 + 先落权限 | `mkstemp`→`fsync`→`chmod 0640`→`os.replace`，消除「先写后 chmod」的 0644 窗口期 |
| `apply_perms` **不碰目录权限** | 目标路径可由 `DABAI_SECRETS_FILE` 指向任意位置；实测隔离测试时它试图 `chmod /tmp` 被拒 |
| 改 myservice 配置后**不重启** | `MainPID` 就是大白本体，重启 = 杀掉调用方自己 |

#### 验证记录（真实执行）

```
安装体检          6 个文件 + 权限 + 服务 + 一致性 + 注入 → 全部通过
行为测试          24 项（幂等/手工变量存活/值校验/名白名单/边界）→ 全绿
实时同步端到端    改 stt_config.json → 5 秒内 secrets.env 自动更新 → 恢复后自动跟上
权限边界          nobody 读不到 ✓   wxf 能读 ✓
shell 兼容        bash 34 个变量 ✓   dash 34 个变量 ✓
profile.d 注入    登录 shell 拿到 ✓   nobody 登录 shell 拿不到 ✓
泄漏检查          systemctl show 无明文 ✓
```

#### 已知边界

- **已运行的 myservice 进程不会热更新环境变量**。Linux 没有「给运行中进程加环境
  变量」的接口（`/proc/PID/environ` 只影响后续 exec 的子进程，且有长度限制）。
  新变量下次重启生效；要立刻用：`dabai-secrets env` 或 `. /etc/profile.d/00-dabai-secrets.sh`。
- **JSON 真源里仍是明文**（`settings.json` 等 0644）。要彻底消除需清空 JSON 并改
  `server.py` 的 `load_config()` 改为只从环境变量读 —— 风险更高（加载失败即瘫痪），单独评估。

常用命令：

```bash
sudo bash deploy/secrets/install-secrets.sh [--check|--uninstall|--purge]
sudo -n /usr/local/sbin/dabai-secrets sync     # 免密（只给 sync 免密，set 仍要密码）
/usr/local/sbin/dabai-secrets list|check|show|env
```


**源配置快照**：`settings.json` / `nodes.json` 既含密钥又**未被 git 跟踪** ——
git 救不了它们。实测事故：一条 `>` 重定向覆盖 + 一次 `rm`，`settings.json` 就彻底没了
（靠清理前的 `~/dabai-backup/configs-*/` 才捞回来，逐字节一致）。

现在每次同步顺带做一份滚动快照到 `/var/backups/dabai-configs/`（0700 / 文件 0600，
最近 20 份，仅在内容或文件集合变化时新增，目录名 = 时间戳-内容摘要前 8 位）。
回退一份：

```bash
sudo ls -1t /var/backups/dabai-configs/
sudo cp /var/backups/dabai-configs/<份>/settings.json ~/dabai/settings.json
sudo -n /usr/local/sbin/dabai-secrets sync
```

**教训**：测试「文件被误加进暂存区」这类场景时，我用 `>` 重定向和 `rm -f` 去构造现场，
直接毁掉了活的配置文件。构造破坏性场景必须在临时副本上做，绝不动真实文件。


### 6.8 仓库密钥防线（`deploy/gitguard/`）

**背景**：仓库 11 个提交里一直带着明文 API key。`.gitignore` 只能拦
「还没被跟踪的文件」，对**已在历史里的**毫无作用 —— 而 `git push`
推的是全部历史，不是当前快照。

**三层处置**

| 层 | 动作 | 工具 |
|---|---|---|
| 止血 | 6 个含密钥配置停止跟踪（磁盘保留），`.gitignore` 补 7 条 | `git rm --cached` |
| 清史 | 值替换 + 整文件移除，改写全部提交 | `git filter-repo` |
| 防复发 | 提交前扫暂存区，命中即拒绝 | `pre-commit` 钩子 |

**判据精度是刻意调过的** —— 误报多了钩子就会被 `--no-verify` 绕过，等于没有。
19 项双向回归：真密钥 6 类全中；`max_tokens` / `state_key` /
`TOKEN_STATS_KEY` / `sha` / `commit` / `api_base` URL / 中文长文本 /
文件路径 全部不误报。

**副产品发现**：`nodes.json`（代理节点配置，含 `auth`/`password`/
`publicKey`/`encryption` 明文凭据）既没被跟踪、**也没被 `.gitignore` 覆盖** ——
一次 `git add -A` 就会泄漏。已补规则。

**回滚**：清史前全量备份在 `~/dabai-backup/*.bundle`（0600，含旧密钥，
确认无误后应删除）。清史会改写所有提交 hash，回滚 = 从 bundle 重新 clone。

**一个必须记住的边界**：钩子只在本地。别人 clone 后要自己跑一次
`bash deploy/gitguard/install.sh`。


### 6.9 远程支配 Windows 主机（`~/.ssh/config`）

**目标**：从树莓派直接操作 Windows（`desktop-apb0bda`），不依赖图形界面、不依赖任何常驻程序。

**三层机制** —— 不是「持久化连接」，是三件独立的事叠加，**没有任何守护进程在保持连接**：

| 层 | 靠什么 | 效果 |
|---|---|---|
| 免密 | `~/.ssh/id_ed25519_win` + `IdentitiesOnly yes` | 非交互执行 |
| 连接复用 | `ControlMaster auto` + `ControlPath ~/.ssh/cm-%r@%h-%p` + `ControlPersist 5m` | 一条 TCP 扛住后续所有 ssh/scp |
| Windows 自启 | sshd 服务 `Running` + `StartType Automatic` | 重启后照样能连 |

实测：冷启动 488ms，复用 ~170ms（我一次任务发几十条命令，省的就是这个）。

**两个入口**

- `ssh win` / `ssh desktop-apb0bda` —— 走主机名（推荐，IP 变了不用改）
- `ssh win-ip` —— 走固定 IP，mDNS 异常时的备用通道

**Windows 特有的坑（必读）**

管理员组成员的公钥**不放** `~/.ssh/authorized_keys`，而放：

```
C:\ProgramData\ssh\administrators_authorized_keys
```

`wangxingfeng` 在 administrators 组 → 往家目录放密钥会**静默失效**（连得上，但一直要密码）。

**主机名解析靠两条独立路径**

- mDNS：树莓派 `avahi-daemon`(enabled+active) ↔ Windows `Bonjour Service`(Running+Automatic)
- 路由器 DNS：`192.168.31.1` 也解析这个名字

两条都随 DHCP 走 → 换 IP 不用动配置。

**边界**：`nsswitch.conf` 是 `mdns4_minimal [NOTFOUND=return]` —— avahi 一旦挂了，
主机名会**直接失败、不退回 DNS**。这就是 `win-ip` 存在的理由。

**排障表**

| 症状 | 原因 | 处置 |
|---|---|---|
| 一直要密码 | 密钥放错位置 | 挪到 `administrators_authorized_keys` |
| `Could not resolve hostname` | avahi 挂了 | `sudo systemctl restart avahi-daemon`，或改 `ssh win-ip` |
| `Connection timed out` | IP 变了 | 先试 `ssh win-ip`；通说明是名字问题，不通才是网络问题 |
| 突然全断 | Windows 重启 | 无需手动清理，下条命令自动重建 |

**陈旧套接字自愈（已实测）**：硬杀主连接、留下 `cm-*` 套接字后，下一条命令自动重建，
不需要手动 `rm`。

**验证命令**

```bash
ssh -O check win     # 主连接是否活着
ls ~/.ssh/cm-*       # 套接字文件名 = 实际连的 HostName
```

套接字名从 `cm-wangxingfeng@192.168.31.144-22` 变成 `cm-wangxingfeng@desktop-apb0bda-22`，是「真的在走主机名」的铁证。

**在 Windows 上跑 PowerShell（必读坑）**

`ssh win "<命令>"` 落地是 `cmd.exe`：双引号会被吃掉、`|` 被当管道符拆开，
任何带引号/管道/`$_` 的 PowerShell 命令都会静默走样（实测 `-match "A|B"` 直接报
`'A' 不是内部或外部命令`；`$s="$env:LOCALAPPDATA\..."; Get-ChildItem "$s$d"` 也会静默取到空路径）。

正确姿势：脚本写成 `.ps1`，用 UTF-16LE base64 传给 `-EncodedCommand`——base64 对 cmd 只是纯文本，不会被解析：

```bash
sh tools/winps.sh /tmp/xxx.ps1     # 封装好了，注释里写了原因
```

**实测用例**：在 Windows 上离线构建 Android APK（`apps/dabai-android`）——AGP 8.7.2 已在
`~/.gradle/caches/modules-2` 里，`maven.google.com` 直连超时但 `repo1.maven.org` 通，
所以 `gradle --offline assembleDebug` 31s 出包，全程零下载。


### 6.10 WSL 里的大白开机自启（Windows 侧，`\WSL-Dabai-KeepAlive`）

**目标**：Windows 登录后 WSL Debian 实例自动起来、`dabai.service` 常驻；`.wslconfig` 里 `networkingMode=mirrored`
让 WSL 的 `:8000` 直通主机网卡（不需要 netsh portproxy，实测 portproxy 表为空）。

三层链路，缺一层就静默失效：

1. `/etc/wsl.conf` → `[boot] systemd=true`（已配）→ 实例一启动，systemd 自动拉起 enabled 的 `dabai.service`
2. 任务动作 `wsl.exe -d Debian -u root -e sleep infinity` —— **必须有进程挂住实例**：
   WSL2 最后一个会话退出约 8 秒后 VM 关闭，服务跟着陪葬（journal 里能看到一串短命的 `-- Boot xxx --`）
3. 任务设置（旧任务死在这三条上）：
   - `ExecutionTimeLimit=PT0S` —— 默认 `PT72H`，保活进程跑满 3 天被杀，且不会自愈
   - `DisallowStartIfOnBatteries=false` / `StopIfGoingOnBatteries=false`
   - `StartWhenAvailable=true`、`MultipleInstances=IgnoreNew`
   - 触发器 = LogonTrigger（延迟 15s）+ TimeTrigger 每 5 分钟无限重复 → 实例被杀后 5 分钟内自动拉起

**验收实测（2026-09-17）**：`wsl --terminate Debian` → Stopped → `Start-ScheduledTask` → Running →
`systemctl is-active dabai` = `active`、`:8000` 在听。

**回滚**：旧定义备份在 `C:\Users\wangxingfeng\WSL-Dabai-KeepAlive.bak-<时间戳>.xml`，
`Register-ScheduledTask -TaskName 'WSL-Dabai-KeepAlive' -Xml (Get-Content <备份> -Raw) -Force`。

**边界**：WSL 需要用户会话——Windows 停在登录界面时不会起，要真正「开机即起」得配自动登录。
**硬依赖：WSL 里必须装 Node >= 22.13**（2026-09-17 踩坑）。`server.py` 的 `TSTranspileMiddleware` 用 Node 的 `module.stripTypeScriptTypes` 把 `.ts` 实时转译成 ESM JS；node 缺失时 `except` 会**静默退回原样直服**（server.py:429-430），浏览器拿到 TS 源码当 JS 跑 → 整个 module 图崩掉 → 页面永远停在 `index.html:39` 的初始「连接中…」，且公网/局域网都复现。诊断法：比 `磁盘 .ts 的 md5` 与 `HTTP 下发的 md5`，相同=原样直服（坏）。**别用 `head` 看开头判断——import 语句在 TS/JS 里长得一样，必须比 md5。**

Debian 13 apt 的 `nodejs` 只有 20.19.2，不够（`stripTypeScriptTypes` 下限 v22.13.0 / v23.2.0）。装官方二进制：

```bash
curl -L -o /tmp/node24.tar.xz https://registry.npmmirror.com/-/binary/node/v24.21.0/node-v24.21.0-linux-x64.tar.xz
cd /tmp && tar -xf node24.tar.xz && mkdir -p /usr/local/lib/nodejs
mv node-v24.21.0-linux-x64 /usr/local/lib/nodejs/
ln -sf /usr/local/lib/nodejs/node-v24.21.0-linux-x64/bin/{node,npm,npx} /usr/local/bin/
```

装完**必须重启 `dabai.service`**：`_ts_node_broken` 是进程级一次性标志（server.py:313），置位后不再重试。实测重启后 WSL 的 `/static/app.ts` 下发 5607 字节、md5 `0b8fd06d4121d6143104aa2c34372aaf`，与树莓派侧转译输出**逐字节相同**。

**已知待办**：`wsl.battlephoenix.tech` 隧道指向 `https://192.168.31.144:8002`（cloudflared config.yml），但 WSL 只监听 **8000** —— 该子域当前是坏的。

**访问方式（已查清）**


## 7. 回滚（Linux 兼容层，§1–§5）

改动在隔离工作树分支 `codex/linux-compat` 上开发，已合并回 `20260909`（merge commit `748d9d2`）。

- 整体回退：`git revert -m 1 748d9d2`
- 只撤单个文件：`git checkout 748d9d2^ -- <文件>`
- 新增文件（`platform_compat.py` / `dabai.sh` / `tools/linux_*` / `LINUX.md`）直接删除即可，不影响原有能力

注意：临时 `.bak-<时间戳>` 备份已清理，回滚请依赖 git 历史，不要依赖备份文件。
行尾已由 `.gitattributes` 固定（`*.sh` = LF），克隆到 Linux 不会出现 `bad interpreter`。
