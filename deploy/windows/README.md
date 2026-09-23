# 大白在 Windows 上运行

> 目标与 Linux 侧一致：**装一次，之后不用管** —— 开机自己起来、崩了自己回来、有新版自己装上、
> 密钥改完自己生效、长跑卡住自己重启。
> Linux 侧由 systemd 承担这件事（见仓库根 `LINUX.md` 与 `deploy/systemd/`），
> Windows 没有 systemd，等价物是七个计划任务（见 §2）。

## 1. 首次安装

```powershell
# 建环境（venv + 依赖），首次用这条
dabai.bat --setup

# 装成「自己会活、自己会更新」（不需要管理员权限）
powershell -ExecutionPolicy Bypass -File deploy\windows\install-windows.ps1
```

装完就结束了。之后：

| 你想要的 | 命令 |
|---|---|
| 看服务在不在 | `Get-ScheduledTask -TaskName Dabai* \| Format-Table TaskName,State` |
| 看版本与上次更新 | `python "%LOCALAPPDATA%\dabai-update\update.py" --status` |
| 立刻起服务 | `python "%LOCALAPPDATA%\dabai-update\update.py" --ensure-running` |
| 手动更新一次 | `python "%LOCALAPPDATA%\dabai-update\update.py" --apply` |
| 演练更新（不写盘不重启） | `python "%LOCALAPPDATA%\dabai-update\update.py" --dry-run` |
| 看更新日志 | `Get-Content "%LOCALAPPDATA%\dabai-update\update.log" -Tail 50` |
| 体检一次 | `python tools\linux_health.py` |
| 同步密钥 | `python deploy\secrets\sync_secrets.py sync` |
| 看长跑心跳 | `python tools\longrun\watchdog.py --dry-run` |
| 卸载（只删计划任务） | `powershell -ExecutionPolicy Bypass -File deploy\windows\install-windows.ps1 -Uninstall` |

## 2. 装了哪七个计划任务

| 任务 | 触发 | 干什么 | Linux 上的对应物 |
|---|---|---|---|
| `DabaiServer` | 登录时 | 拉起 `deploy\windows\launch.py`（注入密钥后启动 server）；进程崩了 1 分钟后重启（最多 3 次） | `myservice.service`（`Restart=always`） |
| `DabaiWatchdog` | 每 5 分钟 | `update.py --ensure-running`：端口不通就拉起 | systemd 的 `Restart=always` 兜底 |
| `DabaiUpdate` | 每小时 | `update.py --apply`：检查并装上最新版 | `dabai-update.timer` |
| `DabaiHealth` | 每 30 分钟 | `tools\linux_health.py`：磁盘 / 端口 / 进程体检 | `dabai-health.timer` |
| `DabaiSecrets` | 每 5 分钟 | `sync_secrets.py sync --quiet`：JSON 里的 key → `%APPDATA%\dabai\secrets.env` | `dabai-secrets-sync.timer` 的低频兜底 |
| `DabaiLongrun` | 登录时 | `tools\longrun\runner.py --loop`：无人值守推进长期目标 | `dabai-longrun.service` |
| `DabaiLongrunWatchdog` | 每 10 分钟 | `tools\longrun\watchdog.py`：心跳过期就重启 | `dabai-longrun-watchdog.timer` |

七个都注册在当前用户名下，跑普通权限。大白只监听本机端口，不需要提权 ——
这也是 Windows 侧比 Linux 侧简单的地方：没有 sudoers、没有属主、没有 cgroup。

长跑引擎会持续消耗模型额度，不想跑就 `install-windows.ps1 -WithoutLongrun`。

**服务入口为什么是 `launch.py` 而不是 `server.py`**：Linux 上密钥由 systemd 的
`EnvironmentFile=` 注入，而计划任务的 Action 只能给一条命令行，没有等价物。
`launch.py` 把 `secrets.env` 读进环境再原地启动 `server.py` —— 手动启动与服务启动
走同一条路径，不会出现「手动跑有密钥、服务跑没密钥」这种只在服务侧复现的差异。

**更新器为什么要拷一份副本**到 `%LOCALAPPDATA%\dabai-update\update.py`：
仓库正是被更新的对象。跑仓库里那份，会在替换到一半时把正在执行的脚本换掉 ——
更新器的可信度不能建立在被更新物之上。这条与 Linux 侧同源。

## 3. 依赖清单

用 `requirements-core.txt`（跨平台，Windows / Linux / macOS 通用）。

**不要用 `requirements.txt`** —— 那是早期 Windows 机器上的历史清单，里面混了三类装不上的东西：
Blender 内嵌模块（`bpy` / `bmesh` / `mathutils`）、已从标准库移除的 `imp`、
以及本机私有包（`TodoService` / `dabai_ears` / `dabai_voice` 等）。
拿它在新机器上装会成片失败，看着像环境坏了，其实是清单本身不可移植。

## 4. 平台差异收敛在哪

业务代码不自己判断平台，统一走仓库根的 `platform_compat.py`（19 个函数：进程、子进程、
文件锁、用户目录、进程清单、监听端口、磁盘空间）。更新器是唯一的例外 ——
它自带一份最小平台判定，因为它要在仓库之外运行（见上一节的理由）。

`deploy/` 下的脚本按平台分开，同名同职责：

| 职责 | Linux | Windows |
|---|---|---|
| 启动 | `dabai.sh` | `dabai.bat` |
| 装自动更新 | `deploy/release/install-update.sh` | `deploy/windows/install-windows.ps1` |
| 服务托管 | `deploy/systemd/*.service` / `.timer` | 七个计划任务（见 §2） |
| 免密升权 | `deploy/sudoers` 窄口径规则 | 不需要（用户级运行） |
| 密钥同步 | `deploy/secrets/`（systemd path unit 实时触发） | 同一份 `sync_secrets.py` + `launch.py` 注入 |
| 发布闸门 | `deploy/gitguard/safe-push.sh` | 同一份 `safe_push.py`（bash 版是薄包装） |

## 5. 验证现状（如实说明）

| 验证项 | 状态 |
|---|---|
| 更新器 Windows 分支（停/起服务、体检、自愈、通知降级） | ✅ 18 个用例，`python -m pytest tests/test_win_update.py` |
| 更新器 Linux 行为无回归 | ✅ 全量测试通过 |
| PowerShell / 批处理静态体检（括号、引号、here-string、goto 目标、cmdlet 拼写） | ✅ `python3 tools/ps_lint.py` |
| 依赖清单可解析 | ✅ `pip install --dry-run -r requirements-linux.txt` |
| 跨平台看门狗 `tools/longrun/watchdog.py` | ✅ 本机 `--dry-run` 走通；Linux 侧 service 已切到它 |
| 长跑引擎跨平台（runner / status_view 里 POSIX-only 写法清零） | ✅ `tests/test_longrun_win_parity.py` 10 个用例 + 全量 1432 通过 |
| `launch.py` 与 `sync_secrets.py` 的 env 解析一致 | ✅ `tests/test_safe_push.py` |
| 发布闸门 `deploy/gitguard/safe_push.py` | ✅ 10 个用例 + 临时仓库端到端 `--dry-run` 走通 |
| **在真 Windows 机器上跑通** | ❌ **还没做** —— 手上没有可达的 Windows 主机 |

也就是说：分支逻辑有测试兜着，**参数语义与权限行为没有真机证据**。
第一次上真机时按这个顺序验：

1. `dabai.bat --check` → 环境自检该全绿
2. `powershell -ExecutionPolicy Bypass -File deploy\windows\install-windows.ps1 -DryRun` → 只看计划
3. 去掉 `-DryRun` 真装 → 脚本末尾会自己跑 `--check`、端口探测并列出七个任务
4. 杀掉 `python.exe` 进程 → 5 分钟内应被看门狗拉起（验自愈）
5. 注销再登录 → 服务应自己起来（验自启）
6. `update.py --apply` 一次 → 验更新链路（会真的停机重启）
7. `python deploy\windows\launch.py --check` → 看密钥注入的变量数与来源（不打印值）
8. `python tools\linux_health.py` → 体检一行输出，verdict 不应是 critical
9. `python deploy\secrets\sync_secrets.py sync` → 再跑 `... check`，应报「全部一致」
10. `python tools\longrun\watchdog.py --dry-run` → 长跑看门狗报告（不动手）
11. `python tools\longrun\runner.py --once --dry-run` → 长跑一轮演练（不调模型，只打印会派什么）
12. `python tools\longrun\runner.py --status` → 引擎状态；再在任务中心点一次长跑条目的停/启（Windows 上走计划任务）

第 7 条最该盯：Linux 上密钥由 systemd 的 `EnvironmentFile=` 注入，Windows 上全靠
`launch.py` 这一层。它没生效时的表现是「服务能起、但调外部 API 全部失败」——
而手动跑又是好的，最容易查错方向。

## 6. 排错

| 现象 | 原因 | 处理 |
|---|---|---|
| `--check` 失败 | 仓库私有，缺 GITHUB_TOKEN | 写 `%APPDATA%\dabai\secrets.env`，一行 `GITHUB_TOKEN=...` |
| 服务起不来 | 端口被占 / 依赖没装全 | `dabai.bat --check`，再 `update.py --ensure-running` 看日志 |
| 计划任务显示 `Ready` 但服务没起 | 任务跑成功但进程又退了 | 看 `update.log`；`DabaiWatchdog` 每 5 分钟会再试 |
| 更新装上了但功能没到 | 更新器副本落后 | 重跑 `install-windows.ps1`（它会重拷副本） |
| 中文显示乱码 | 老 PowerShell 5.1 读无 BOM 脚本 | `install-windows.ps1` 已带 BOM；`dabai.bat` 开头有 `chcp 65001` |

## 7. 与 Linux 的能力对照

| 能力 | Linux | Windows | 差异 |
|---|---|---|---|
| 启动 / 崩溃自愈 | `myservice.service`（`Restart=always`） | `DabaiServer` + `DabaiWatchdog` | 无 |
| 自动更新 | `dabai-update.timer` | `DabaiUpdate` | 无 |
| 定期体检 | `dabai-health.timer` | `DabaiHealth` | Windows 拿不到温度 / PSI / zram（无 `/sys`、`/proc`），只报磁盘 / 端口 / 进程 |
| 密钥同步 | systemd path unit（inotify，改完即生效） | `DabaiSecrets`（每 5 分钟轮询） | 延迟 ≤5 分钟；同步器幂等，无变化不写盘 |
| 密钥注入 | systemd `EnvironmentFile=` | `deploy\windows\launch.py` 读进环境再启动 | 无（交互式与服务启动走同一路径） |
| 长跑引擎 | `dabai-longrun.service` | `DabaiLongrun`（`-WithoutLongrun` 可跳过） | 无（runner.py 里的 `fcntl` / `/proc` / `killpg` / `venv/bin/python` 已换成交叉平台实现） |
| 长跑心跳看门狗 | `dabai-longrun-watchdog.timer` | `DabaiLongrunWatchdog` | 无 |
| 长跑的启停与状态（任务中心） | `systemctl --user` | `schtasks` + `Get-ScheduledTask` | 无（`stop` 会连带禁用看门狗任务，否则它下一次触发就把引擎拉回来） |
| 发布前密钥闸门 | `deploy/gitguard/safe-push.sh` | 同一份 Python 实现 | 无（bash 版是薄包装） |

仍然存在的平台差异（不是缺陷，是平台能力）：

- **通知**：Linux 用 `notify-send`；Windows 退到 `msg.exe`（家庭版常被移除），失败静默 ——
  体检的主产物是日志，通知只是锦上添花。
- **进程权限**：Linux 有 `sudoers` 窄口径免密、属主、cgroup；Windows 全部用户级运行，
  没有等价物也不需要（大白只监听本机端口）。
- **信号与停机**：Linux 用 `SIGTERM` 给整个进程组；Windows 没有信号，更新器按端口
  找到进程树后 `taskkill /T /F`（这个差异在 `deploy/release/update.py` 的 `svc()` 里）。
