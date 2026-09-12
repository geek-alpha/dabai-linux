# Linux 原生（linux_native）

把大白与所在 Linux 系统打通。两个层次：

- **感知** —— 读硬件真实状态（温度/欠压/内存/负载/磁盘）
- **支配** —— 管服务、控 GPIO、调进程、审网络、查存储、做安全审计

触发：机器状态 / 卡不卡 / 热不热 / 内存够不够 / 服务挂了 / 派重活前自检 /
引脚 GPIO / 谁吃 CPU / 端口暴露 / 磁盘满了 / 安全审计。

## 工具

| 工具 | 用途 |
|---|---|
| `linux_senses` | SoC 温度、欠压降频、CPU 频率与调频策略、内存与 Swap、zram 压缩比、负载、磁盘余量、自身开销 + 判定与建议 |
| `linux_guard` | 派重活前的资源闸门。`need=light/normal/heavy`，返回是否放行 + 理由 |
| `linux_service` | systemd 服务管理（自动识别系统级/用户级）：list / status / logs / events / start / stop / restart / reload |
| `linux_notify` | 桌面通知（走 D-Bus，真的弹在屏幕上） |
| `linux_media` | MPRIS 当前播放 + play/pause/next/prev |
| `linux_gpio` | **硬件支配**：54 个 GPIO 引脚状态、读电平、驱动输出、闪灯、控板载 ACT/PWR 灯 |
| `linux_process` | **进程支配**：排行 / 详情 / 发信号 / 调 nice 与 CPU 亲和性 |
| `linux_net` | **网络支配**：监听端口 / 连接 / 接口流量 / 暴露面审计 |
| `linux_storage` | **存储支配**：挂载点使用率 + inode + SD 卡信息 / 目录排行 / 大文件 / 清理候选 |
| `linux_audit` | **安全审计**（全只读）：暴露面 / SSH / 账号提权 / SUID / 可疑进程 / 失败服务 |

## 什么时候用

- 问「机器怎么样/卡不卡/热不热」→ `linux_senses`，**给数据，别空口安慰**
- 要跑编译 / 批量 / 多智能体 / 模型推理 / 大下载 → 先 `linux_guard(need='heavy')`，
  被拒就分批做或告诉用户，别硬上（905MB 内存 + 树莓派，硬上就是 OOM 或热降频）
- 服务异常 / 查日志 / 重启 → `linux_service`
- 需要用户立刻知道 → `linux_notify`，**别刷屏**
- 引脚 / 传感器 / LED / 闪灯 → `linux_gpio`（先 `summary` 看支配面）
- 谁吃 CPU / 内存去哪了 / 卡住了 → `linux_process`
- 端口 / 连接 / 暴露面 → `linux_net`
- 磁盘满 / 空间去哪了 → `linux_storage`
- 有没有漏洞 / 被入侵了吗 → `linux_audit`

## 这台机器的实况（2026-09-11 实测）

| 项 | 值 |
|---|---|
| 硬件 | Raspberry Pi 3 Model B Rev 1.2（4 核 aarch64，1.2GHz，序列号 aede8d4c） |
| 系统 | Debian 13 trixie，内核 6.18.34+rpi |
| 内存 | 905MB 可用 / Swap 905MB（zram zstd，压缩比约 3.0–3.4:1） |
| 温度 | 常态 62–70°C（距 80°C 降频线约 10–18°C） |
| 电源 | `throttled=0x0` 健康，从未欠压/降频 |
| 磁盘 | SD 卡 SanDisk SC16G（2017-11 出厂）14.8GB，ext4 + noatime，用 71% |
| GPIO | 54 个物理引脚（17 可写通用 IO / 27 专用只读）；pinctrl 可用，gpiozero/RPi.GPIO/lgpio 三库已装 |
| 大白本体 | 系统级 `myservice.service`，PID 约 1000，RSS 约 282MB |
| 代理 | 用户级 `sing-box.service`，`127.0.0.1:7890` |
| 暴露面 | :80 nginx → :8001；:8000 cloudflared 隧道；:111 **rpcbind（多余）**；:22 ssh |
| 远程兜底 | `rpi-connect`（**用户级**服务，`rpi-connectd` + wayvnc 远程桌面） |

## 已知边界

- **内核未启用 memory cgroup** → `MemoryMax` 不生效、`systemd-oomd` 不可用。
  改 `/boot/firmware/cmdline.txt` 加 `cgroup_enable=memory cgroup_memory=1` 后重启（已写入，**待重启生效**）。
- **PSI 不可用**（`/proc/pressure/*` 不存在）→ 没有内核级阻塞时长，只能看 load 与 swap。
- **系统级服务控制需 sudo**：大白自己是系统级 unit，`restart` 会明确提示需要 root，不假装成功。
- **I2C / SPI / 1-Wire / 串口未启用** → 内核模块在（`i2c-dev.ko.xz`），但 `/dev/i2c-1` 等节点不存在，
  要改 config.txt 加 `dtparam=i2c_arm=on` 等开关。**GPIO 不受影响，已可直接用。**
- **板载 LED 写需 root**（`/sys/class/leds/*/brightness` 是 root:root 0644）。
- **vcgencmd 被裁减** → `measure_clock:arm` 等报 "Command not registered"，
  改用 `/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq`。只有 `measure_temp` /
  `measure_volts` / `get_throttled` 可用。
- **SD 卡不上报磨损** → `/sys/block/mmcblk0/device/life_time` 节点**不存在**（不是权限问题）。
- **无 MPRIS 播放器时** `linux_media` 返回明确提示，不报错。

## 安全设计

### 自保护（防「把自己弄死」）
- **自停闸门**：对大白自己的 unit 执行 `stop` 会被拒绝 —— 显式 stop 不触发 `Restart=always`，
  等于永久下线。确实要下线须传 `confirm=true`。
- **进程信号闸门**：拒绝向 PID 1、**大白自己**、**自己的祖先链**、内核线程、关键服务发信号。
  祖先链那一条最要紧 —— 大白从自己的 shell 里起进程时，杀祖先 = 回复断在半路。
- **GPIO 写闸门**：HAT EEPROM(0/1)、I2C、SPI、UART、PWM、**电源管理(46/47)**、
  **SD 控制器(48-53)** 等 27 个专用脚一律拒绝写入；写前还检查该脚是否已被别的驱动占用。

### 数据诚实（防「假阳性」）
- **感知失败报 `unknown`，不报 `healthy`** —— 把「读不到」当「正常」是最危险的假阳性。
- **解析结果为空要报异常，不静默跳过** —— `/etc/passwd` 那次就是典型：
  不报错但结果是错的（详见踩坑 6）。
- **节点不存在 ≠ 权限不足** —— 不要用「需 root」搪塞不存在的节点，会骗用户白跑一趟。
- **任何一项数据源失效只标记该项**，不整体失败（部分感知 > 全盘失败）。
- **只读优先**：senses / net / storage / audit 全只读；只有 signal / tune / gpio write 是写操作。
- **清理只报告不执行**：`storage clean` 只统计候选，删不删由用户决定。

## 排查方法（硬规则）

- **动手前先搜一轮**：Linux/systemd 这类问题的收尾动作是**有标准答案的**
  （如 journald 的 `journalctl --flush`），网上一查就有；自己硬试 N 遍未必撞得上。
- **先问「这些尝试彼此独立吗」**：同一个 boot 里反复重启同一个服务，只是**同一个证据重复 N 次**，
  不构成排除法。不独立的失败堆再高，也只是一次失败——别拿它下「原因不明」的结论。
- **按目标验证，不按字段验证**：`RuntimeMaxUse` 生效**不能**证明 `Storage` 生效。
  验的是目标（`/var/log/journal` 有 `.journal` 文件、`/run/log/journal` 已空），
  不是「某个我改过的字段变了」。
- **说「原因不明」之前，先确认自己搜过**。
- **「多尝试」= 换维度，不是重复同一动作**：改 A 字段不生效 → 换 B 字段、换配置层（conf.d / drop-in / 命令行）、
  换观测点（不是换措辞再试一次）。同一个动作做 N 遍，N 再大也只是 1 次尝试。
- **别把「我改了」当「它生效了」**：改配置 ≠ 生效。必须有独立的**生效标志**——
  `systemctl show <unit> -p <Property>`、`journalctl --disk-usage` 这类由系统自己吐出来的值，
  而不是「我写的文件在磁盘上」。

### 结论的证据分级（下结论前必须自评）

| 级别 | 含义 | 允许的说法 |
|---|---|---|
| **实测** | 本机命令输出直接证明 | 「已验证」+ 贴原始输出 |
| **推断** | 有机制依据但没跑过 | 「推断」+ 说明依据和未验证点 |
| **猜测** | 只是像 | 「不确定」+ 说明还差哪一步 |

- **不许把推断说成实测**：「逻辑上成立」「理论上应该」**不能**替代跑一次。
  本轮踩过：`Storage=persistent` 没实测就说「已生效」，实际差一条 `journalctl --flush`。
- **主动找反证**：下结论前先问「什么现象能推翻它」，并**去查那个现象**。
  只收集支持自己的证据 = 自我确认，不是验证。
- **未验证项必须显式写出来**，宁可标注「没测过」也不许糊过去——用户有权知道哪部分是真的。
- **高风险动作（重启/覆盖/删除）先留回滚路径和基线**，再动手；基线要存成能复查的文件，别只在脑子里。

## 踩坑记录（别重踩）

1. `journalctl --user -u <unit>` 在本机**恒返回 `No journal files were found`** ——
   用户服务日志落在**系统 journal**，必须用 `journalctl --user-unit=<unit>`。
2. `journalctl --since=2h` 解析失败，必须 `--since=-2h`（工具内已自动规范化）。
3. `/sys/block/zram0/orig_data_size` **不存在**，真实数据源是 `/sys/block/zram0/mm_stat`。
4. 不存在的 unit，`systemctl show` 仍返回默认值（PID 0 / 退出码 0）——
   必须查 `LoadState` 识别 `not-found`。
5. `StartLimitIntervalSec` 必须在 `[Unit]` 段，放 `[Service]` 会被 systemd **静默忽略**。
6. **`for line in open(f)` 后直接 `split(":")`，最后一个字段会带 `"\n"`** ——
   `/etc/passwd` 的 shell 字段永远匹配不上 `/etc/shells`，静默返回空列表。
   **必须 `.strip().split()`**。这类「不报错但结果是错的」比抛异常危险。
7. **`pinctrl` 输出格式不一致**：输入脚是 `17: ip -- | lo`，输出脚是 `47: op -- -- | hi`（多一列）。
   正则必须写成 `(\d+):\s+(.+?)\s*\|\s*(\S+)`，不能写死字段数。
8. **`pinctrl get` 会吐 100+ 的 FWGPIO**（BT_ON / WL_ON / LAN_RUN…），那是 SoC 固件内部信号，
   **不对应物理排针**。不滤掉会让「引脚数」从 54 变 61。
9. **用户级服务必须用 `systemctl --user` 查** —— `rpi-connect` 是用户级，
   系统级 `is-active` 返回 inactive，直接漏判成「无兜底通道」。用 `pgrep -x` 更稳。
10. **`systemctl --user` 在非登录 shell 里会报 `Failed to connect to user scope bus`** ——
    需先设 `XDG_RUNTIME_DIR` 与 `DBUS_SESSION_BUS_ADDRESS`。
11. **`ps -eo pcpu` 对刚启动的进程虚高**（算的是生命周期均值）——
    看 `etimes` 很小的进程（如 `ps` 自己）的 CPU% 别当真。
12. **`ss` 的双栈监听会重复报**（`0.0.0.0:80` 与 `[::]:80` 是同一服务），
    必须按端口合并，否则同一个服务刷两遍。
13. **改已有文件别用行号编辑** —— 打偏一次就误删了整段函数。用 `replace` 精确锚点。
14. **journald 从 volatile 切 persistent，改配置 + 重启服务都不够，必须 `journalctl --flush`** ——
    journald 一旦在 `/run/log/journal` 建了活跃 journal 就**不会主动搬家**：`Storage=persistent`
    写进 `/etc/systemd/journald.conf.d/` 后只重启服务，`--disk-usage` 仍指向 `/run`。
    `journalctl --flush` 之后 `/var/log/journal/<machine-id>/` 才出现 `.journal`，`/run/log/journal` 变空。
    （本机 raspberrypi-sys-mods 自带 `40-rpi-volatile-storage.conf` = `Storage=volatile`，
    必须在 `/etc/systemd/journald.conf.d/` 里覆盖，不用动 `/usr/lib` 下的官方文件。）

## 相关文件

- `senses_impl.py` —— 只读感知层（温度/内存/负载/存储/自身）
- `system_impl.py` —— 服务与桌面操作层（systemd 双 scope / D-Bus / MPRIS）
- `gpio_impl.py` —— 硬件支配层（GPIO 引脚 / 板载 LED）
- `sysadmin_impl.py` —— 系统支配层（进程 / 网络 / 存储 / 安全审计）
- `deploy/systemd/` —— 部署件（drop-in + 体检 timer + 免密入口），详见其 README
- `tools/linux_health.py` —— 体检脚本（由 systemd timer 驱动）
- `LINUX.md` —— 完整设计与验证记录
