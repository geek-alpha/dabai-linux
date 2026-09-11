# Linux 原生（linux_native）

把大白与所在 Linux 系统打通：读硬件状态、管 systemd 服务、发桌面通知、控媒体播放。
触发：问机器状态 / 卡不卡 / 热不热 / 内存够不够 / 服务挂了 / 要看日志 / 派重活前自检。

## 工具

| 工具 | 用途 |
|---|---|
| `linux_senses` | 一次拿到 SoC 温度、欠压降频、CPU 频率与调频策略、内存与 Swap、zram 压缩比、负载、磁盘余量、自身开销 + 判定与建议 |
| `linux_guard` | 派重活前的资源闸门。`need=light/normal/heavy`，返回是否放行 + 理由 |
| `linux_service` | systemd 服务管理（自动识别系统级/用户级）：list / status / logs / events / start / stop / restart / reload |
| `linux_notify` | 桌面通知（走 D-Bus，真的弹在屏幕上） |
| `linux_media` | MPRIS 当前播放 + play/pause/next/prev |

## 什么时候用

- 用户问「机器怎么样/卡不卡/热不热/风扇响不响」→ `linux_senses`，**给数据，别空口安慰**
- 要跑编译 / 批量任务 / 多智能体并行 / 模型推理 / 大文件下载 → 先 `linux_guard(need='heavy')`，
  被拒就分批做或告诉用户，别硬上（905MB 内存 + 树莓派，硬上就是 OOM 或热降频）
- 服务异常、要查日志、要重启 → `linux_service`
- 需要用户立刻知道（任务完成 / 需要决策 / 系统告警）→ `linux_notify`，**别刷屏**
- 用户在听什么、要暂停切歌 → `linux_media`

## 这台机器的实况（2026-09-11 实测）

| 项 | 值 |
|---|---|
| 硬件 | Raspberry Pi 3 Model B Rev 1.2（4 核 aarch64，1.2GHz） |
| 系统 | Debian 13 trixie，内核 6.18.34+rpi |
| 内存 | 905MB 可用 / Swap 905MB（zram zstd，压缩比约 3.4:1） |
| 温度 | 常态 67–70°C（距 80°C 降频线约 10°C） |
| 电源 | `throttled=0x0` 健康，从未欠压/降频 |
| 磁盘 | SD 卡 14.8GB，ext4 + noatime |
| 大白本体 | 系统级 `myservice.service`，PID 约 1000，RSS 约 350MB |
| 代理 | 用户级 `sing-box.service`，`127.0.0.1:7890` |
| 暴露面 | nginx :80 → :8001；cloudflared 隧道 `battlephoenix.tech` → :8000 |

## 已知边界

- **内核未启用 memory cgroup** → `MemoryMax` 不生效、`systemd-oomd` 不可用。
  要根治得改 `/boot/firmware/cmdline.txt` 加 `cgroup_enable=memory cgroup_memory=1` 后重启（需 sudo）。
- **PSI 不可用**（`/proc/pressure/*` 不存在）→ 没有内核级阻塞时长，只能看 load 与 swap。
- **系统级服务控制需 sudo**：大白自己是系统级 unit，`restart` 会明确提示需要 root，不会假装成功。
- **I2C / SPI 未启用** → 暂时接不了外设传感器（用户组权限已就绪，只差内核开关）。
- **无 MPRIS 播放器时** `linux_media` 返回明确提示，不报错。

## 安全设计

- **自停闸门**：对大白自己的 unit 执行 `stop` 会被拒绝 —— 显式 stop 不触发 `Restart=always`，
  等于永久下线。确实要下线须传 `confirm=true`。
- **感知失败报 `unknown`，不报 `healthy`** —— 把「读不到」当「正常」是最危险的假阳性。
- **只读优先**：感知层全部只读（`/sys`、`/proc`、`vcgencmd`），不改任何系统状态；操作与感知分离。
- **任何一项数据源失效只标记该项**，不整体失败（部分感知 > 全盘失败）。

## 踩坑记录（别重踩）

1. `journalctl --user -u <unit>` 在本机**恒返回 `No journal files were found`** ——
   用户服务日志落在**系统 journal**，必须用 `journalctl --user-unit=<unit>`。
2. `journalctl --since=2h` 解析失败，必须 `--since=-2h`（工具内已自动规范化，直接传 `2h` 即可）。
3. `/sys/block/zram0/orig_data_size` **不存在**，真实数据源是 `/sys/block/zram0/mm_stat`（空格分隔）。
4. 不存在的 unit，`systemctl show` 仍返回默认值（PID 0 / 退出码 0）——
   必须查 `LoadState` 识别 `not-found`，否则会伪装成「服务存在但没跑」。
5. `StartLimitIntervalSec` 必须在 `[Unit]` 段，放 `[Service]` 会被 systemd **静默忽略**。

## 相关文件

- `senses_impl.py` —— 只读感知层（温度/内存/负载/存储/自身）
- `system_impl.py` —— 服务与桌面操作层（systemd 双 scope / D-Bus / MPRIS）
- `deploy/systemd/` —— 部署件（drop-in + 体检 timer），详见其 README
- `tools/linux_health.py` —— 体检脚本（由 timer 驱动）
- `LINUX.md` §6 —— 完整设计与验证记录
