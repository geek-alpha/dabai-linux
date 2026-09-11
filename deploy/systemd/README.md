# 大白 × Linux 集成（deploy/systemd）

让大白不只是「跑在 Linux 上」，而是**成为系统的一部分**：被 systemd 守护、
能读硬件状态、能操作服务、能跟桌面会话对话。

## 0. 关键事实（先看这个，别踩重复的坑）

**大白已经由系统级 systemd unit 托管**，不是裸进程：

```
/etc/systemd/system/myservice.service   →  Description=dabai face to face
                                           User=wxf  Group=wxf
                                           ExecStart=/home/wxf/dabai/venv/bin/python server.py
                                           Restart=always（由 drop-in 10-recovery.conf 覆盖）
                                           enabled + active
```

所以**不要**再建一个用户级 `dabai.service` —— 它会和现有服务抢 `8000/8001` 端口，
变成双实例互踢。正确做法是给现有 unit 加 **drop-in**（只叠加差异，原文件不动）。

只查 `systemctl --user` 是看不见大白的（它是系统级 unit）。这是本项目最早踩的坑。

---

## 1. 本目录内容

| 文件 | 作用 | 安装方式 |
|---|---|---|
| `myservice.service.d/20-linux-native.conf` | 大白主服务的增量增强（日志标识/PATH/CPU 权重/加固） | **需 sudo**，见 §2 |
| `dabai-health.service` + `.timer` | 定期体检：写 journald + 异常弹通知 | 免 sudo（用户级），见 §3 |

---

## 2. 主服务增强（需 sudo，一次性）

```bash
# 应用
sudo install -Dm644 /home/wxf/dabai/deploy/systemd/myservice.service.d/20-linux-native.conf \
     /etc/systemd/system/myservice.service.d/20-linux-native.conf
sudo systemctl daemon-reload
sudo systemctl restart myservice
```

**改了什么：**

| 项 | 原值 | 新值 | 为什么 |
|---|---|---|---|
| `SyslogIdentifier` | （未设，日志 tag 是 `python`） | `dabai` | 系统日志里分得清是大白还是别的 python |
| `PATH` | `/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin` | 前面加 `~/.local/bin` | `sb-proxy`/`sb-node` 装在那里 |
| `CPUWeight` / `IOWeight` | 未设（=100） | `200` | 与 sing-box 争 CPU 时大白优先（对话延迟敏感，代理不敏感） |
| `TasksMax` | 806 | 512 | 收紧线程上限（实测当前 30，留 17 倍余量） |
| `OOMPolicy` | `stop` | `continue` | 子进程被 OOM 杀掉时不连坐整个服务 |
| `NoNewPrivileges` 等 12 项加固 | 全部关闭 | 开启 | 见下 |
| `UMask` | 0022 | `0077` | 新建文件仅属主可读（内存里有对话与凭据） |

**刻意没有启用**（会让大白残废，别手贱加）：
`ProtectSystem=strict` / `ProtectHome` / `PrivateTmp` / `SystemCallFilter` /
`IPAddressDeny` —— 大白是通用智能体，要读写任意工作区路径、执行任意命令、
spawn 子智能体、访问局域网。锁死文件系统 = 直接废掉核心能力。

**验证：**
```bash
systemd-analyze verify /etc/systemd/system/myservice.service     # 无输出 = 全部指令合法
systemd-analyze security /etc/systemd/system/myservice.service   # 看加固暴露分
systemctl show myservice.service -p SyslogIdentifier,CPUWeight,NoNewPrivileges,UMask
journalctl -u myservice.service -n 5                             # tag 应为 dabai
```

**撤销：**
```bash
sudo rm /etc/systemd/system/myservice.service.d/20-linux-native.conf
sudo systemctl daemon-reload && sudo systemctl restart myservice
```

---

## 3. 定期体检（免 sudo）

```bash
ln -sf /home/wxf/dabai/deploy/systemd/dabai-health.service ~/.config/systemd/user/
ln -sf /home/wxf/dabai/deploy/systemd/dabai-health.timer   ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now dabai-health.timer
```

**为什么用 timer 而不是后台线程轮询**：timer 由系统调度，开机自动拉起、
关机期间错过的会补跑（`Persistent=true`）、进程不常驻（零常驻内存）、
日志自动进 journal。比 `while True: sleep(600)` 省一个常驻进程。

**为什么写 journald 而不是自己的日志文件**：本机 journald 是 `Storage=volatile`
（内存里），写日志**不碰 SD 卡**，对树莓派卡寿命友好；且自带时间索引与轮转。

```bash
systemctl --user list-timers dabai-health.timer                   # 下次何时跑
journalctl --user-unit=dabai-health.service -n 20                 # 历史趋势
journalctl --user-unit=dabai-health.service | grep -o "temp=[0-9.]*C" | tail -20
```

---

## 4. journalctl 查日志的正确姿势（实测差异）

| 写法 | 结果 |
|---|---|
| `journalctl --user -u sing-box.service` | ✗ **`No journal files were found`** |
| `journalctl --user-unit=sing-box.service` | ✓ 正确（用户级 unit） |
| `journalctl -u myservice.service` | ✓ 正确（系统级 unit） |
| `journalctl -u myservice.service -n 5` | ✓ tag 为 `dabai`（应用 drop-in 后） |

原因：用户服务的日志**落在系统 journal**，不是独立的用户 journal。
`--user -u` 是「在用户 journal 里按 unit 过滤」，本机没有用户 journal 文件，所以恒空。

`--since` 也有坑：`--since=2h` 解析失败，必须写 `--since=-2h`（相对偏移）。
`linux_service` 工具内部已自动规范化，直接传 `2h` 即可。

---

## 5. 需要 sudo 才能拿到的收益（尚未启用）

按收益排序，都**不影响大白运行**，纯增益：

### 5.1 内存 cgroup（最大收益，但需重启）

现状：`/sys/fs/cgroup/cgroup.controllers` = `cpuset cpu io pids` —— **没有 `memory`**。
后果：`MemoryMax`/`MemoryHigh` 不生效，`systemd-oomd` 装不了，
905MB 内存的机器上无法给大白设内存天花板，只能靠 OOM killer 事后收尸。

```bash
# 在 /boot/firmware/cmdline.txt 末尾（同一行，不要换行）追加：
cgroup_enable=memory cgroup_memory=1
sudo reboot
# 重启后验证：cat /sys/fs/cgroup/cgroup.controllers  应出现 memory
```

启用后可以在 drop-in 里加：
```ini
MemoryHigh=450M      # 软限：超过则回收，不杀进程
MemoryMax=600M       # 硬限：超过则 OOM（大白会自动重启）
```

### 5.2 内存调优（zram 场景）

```bash
# zram 是压缩内存，越早换出越好 —— swappiness 默认 60 偏低
echo 'vm.swappiness=150' | sudo tee /etc/sysctl.d/99-dabai.conf
echo 'vm.vfs_cache_pressure=200' | sudo tee -a /etc/sysctl.d/99-dabai.conf
sudo sysctl --system
```
实测当前：`swappiness=60`，zram 压缩比 **3.38:1**（278.5MB → 82.3MB），swap 已用 32%。

### 5.3 systemd-oomd（用户态 OOM 守护）

```bash
sudo apt-get install -y systemd-oomd    # 候选版本 257.13-1~deb13u1
```
依赖 5.1 的 memory cgroup，没开之前装了也没用。

### 5.4 硬件接口（I2C / SPI 未启用）

现状：`/dev/gpiomem` ✓、`/dev/vchiq` ✓、`/dev/snd` ✓；`/dev/i2c-1` ✗、`/dev/spidev0.0` ✗。
用户 `wxf` 已在 `gpio i2c spi dialout audio video render` 组里，**权限已就绪，只差内核没启用总线**。

```bash
sudo raspi-config    # Interface Options → I2C / SPI → Enable
# 或直接编辑 /boot/firmware/config.txt，加：
dtparam=i2c_arm=on
dtparam=spi=on
```
启用后就能接温度传感器、OLED 状态屏、物理按钮等（用户组权限已具备，无需再 sudo）。

---

## 6. 回滚总表

| 改动 | 回滚 |
|---|---|
| 主服务 drop-in | `sudo rm /etc/systemd/system/myservice.service.d/20-linux-native.conf && sudo systemctl daemon-reload && sudo systemctl restart myservice` |
| 体检 timer | `systemctl --user disable --now dabai-health.timer && rm ~/.config/systemd/user/dabai-health.{service,timer}` |
| 代理服务 | `systemctl --user disable --now sing-box` |
| 全部改动 | `git revert`（unit 与脚本都在仓库里，见 git 历史） |
