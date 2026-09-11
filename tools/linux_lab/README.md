# Linux 高级操作演练场（linux_lab）

在一台**无 root、无 bpftrace、无 cgroup 委托**的受限机器上，用第一性原理重建可用的
系统级观测 / 隔离 / 控制 / 调试能力。每条演练都真跑，输出是实测结果而非文档抄写。

```bash
bash tools/linux_lab/drill.sh probe   # 能力边界探测（先跑这个）
bash tools/linux_lab/drill.sh l1      # 观测：/proc 取证 + strace 系统调用级追踪
bash tools/linux_lab/drill.sh l2      # 隔离：非特权命名空间容器（Docker 原理拆解）
bash tools/linux_lab/drill.sh l3      # 控制：资源自限 / 调度 / 信号 / 能力位
bash tools/linux_lab/drill.sh l4      # 调试：ptrace 活体解剖 + 无 perf 的替代矩阵
bash tools/linux_lab/drill.sh all
```

设计原则：**只使用不需要 root 的能力**；不改系统状态；临时进程跑完即杀；
需要 root 的路径明确标注为「不可用」并给出替代方案——避免在死路上浪费时间。

---

## 一、本机能力边界（2026-09-11 实测）

环境：树莓派 aarch64 / Debian 13 (trixie) / 内核 6.18.34 / 4 核 / 905MB 内存
身份：`uid=1000(wxf)`，在 `sudo/adm/video/gpio` 等组，**sudo 需密码**

| 能力 | 状态 | 说明 |
|---|---|---|
| strace（含 `-c/-T/-tt/-e trace=`） | ✅ | `-k` 栈回溯不支持 |
| gdb attach / 寄存器 / 栈回溯 / 内存映射 | ✅ | 无 yama，同 uid 进程随意 attach |
| `/proc/<pid>/*` 全谱读取 | ✅ | maps/fd/ns/status/io/syscall/wchan |
| unshare（user+pid+mnt+net+uts+ipc ns） | ✅ | 非特权容器核心，实测可建沙箱 |
| ulimit 自限（`-v/-n/-u/-t`） | ✅ | 实测拦住 200MB 超限分配 |
| taskset / ionice / chrt（非实时类） | ✅ | 只作用于自己的进程 |
| dmesg | ✅ | 在 `adm` 组 |
| perf / bpftrace / tcpdump / sysdig | ❌ | 需 root 安装 |
| cgroup v2 资源隔离 | ❌ | 当前 cgroup 由 root 拥有，无委托 |
| sysctl 调参 | ❌ | `/proc/sys/*` 全只读 |
| core dump | ❌ | `core_pattern=core` 但 `ulimit -c=0` |

**结论：观测与调试能力几乎完整，控制与隔离能力受限于 root。**
所以本机的最优打法是 `strace + /proc + gdb` 三件套，
而不是去追 eBPF——那是另一台机器的故事。

## 二、无 bpftrace 时的替代矩阵

| 想知道的事 | 用不了 | 本机替代方案 |
|---|---|---|
| 某进程在等什么 | bpftrace | `cat /proc/<pid>/wchan` + `/proc/<pid>/stack` |
| 某进程在跑什么 syscall | perf trace | `strace -p <pid> -tt -T` |
| 谁在消耗 CPU | perf top | `/proc/<pid>/stat` 的 `utime+stime` 差分采样 |
| 谁在读写磁盘 | biosnoop | `/proc/<pid>/io` 的 `read_bytes/write_bytes` |
| 网络连接状态 | tcpdump | `ss -tanpi`（含 rtt/cwnd/retrans） |
| 函数级耗时 | bpftrace | gdb 断点打时间戳 / Python `cProfile` |
| 崩溃现场 | core dump | `python3 -X faulthandler` 或注册信号栈转储 |

## 三、容器原理速查（l2 实测）

`unshare -Urmpf --mount-proc bash` 一行拿到的东西：

- `-U` 新 user namespace，`-r` 把当前用户映射成 ns 内 root
  → 宿主上仍是 uid 1000，ns 内 `id` 显示 root（**这就是 rootless 容器的原理**）
- `-p` 新 PID namespace，`-f` 必须 fork
  → ns 内第一个进程 PID=1（**容器 PID 1 的真相**），宿主进程全部不可见
- `-n` 新 network namespace → 只剩 `lo`，eth0/wlan0 消失（**网络隔离的真相**）
- `-m` 新 mount namespace → 非特权也能 `mount -t tmpfs`，且不影响宿主

Docker 比这多出来的：cgroup 资源配额、overlayfs 分层镜像、netfilter NAT、
seccomp 系统调用白名单——**每一样都需要 root/CAP**，这正是 rootless 容器的边界。

## 四、开源社区学习路径（GitHub 实测 star 数）

按「先能动手，再懂原理」排序：

| 资源 | ★ | 用途 |
|---|---|---|
| `bpftrace/bpftrace` | 10.3k | 有 root 的机器上，这是最强的观测工具 |
| `brendangregg/perf-tools` | 10.5k | perf/ftrace 工具集，性能分析圣经配套 |
| `KDAB/hotspot` | 5.2k | perf 的 GUI，看火焰图不用记命令 |
| `0xAX/linux-insides` | 33.5k | 内核原理书：从启动到内存管理到系统调用 |
| `cilium/pwru` | 3.8k | eBPF 网络调试器，看包在内核里走到哪一步 |
| `d0u9/Linux-Device-Driver` | 569 | 驱动开发实战（需要另一台机器练） |

**读法**：不要顺着书读。用「遇到问题 → 去查这一块」的方式读——
本机 `drill.sh` 的每条演练都是一个可以往回追的问题入口。

## 五、和大白项目的结合点

这台机器就是跑大白的机器，上面每个能力都对应一个真实痛点：

| 大白可能的问题 | 用哪条 |
|---|---|
| `server.py` 卡住不动 | `l1.4` attach 看 syscall + `l1.1` 看 wchan |
| 内存越跑越大 | `l1.1` 的 VmRSS/VmHWM + `l4.4` 看 heap 增长 |
| TTS/推理延迟高 | `l1.3` strace `-T` 找单次慢调用 |
| fd 泄漏（长跑服务） | `l1.6` 对比 fd 数随时间变化 |
| 想安全跑不可信脚本 | `l2.1` 非特权沙箱 |
| 后台任务拖慢主服务 | `l3.3` `ionice -c3` + `l3.2` `taskset` |
| 进程杀不掉 | `l3.5` 信号排查 |
| 程序崩溃没日志 | `l4.5` faulthandler 栈转储 |

## 六、已知限制

- 本机 `awk` 是 mawk，**没有 `strtonum()`**，十六进制换算用 bash 的 `$((16#...))`
- `systemd-run --user` 不可用（无 dbus 会话总线），cgroup 隔离走不通
- `nsenter` 进他人命名空间被拒（需 `CAP_SYS_ADMIN`），只能操作自己的进程
- 演练全部在 `/tmp` 下建临时目录，`trap` 清理，不写任何持久状态
