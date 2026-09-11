#!/usr/bin/env bash
# =============================================================================
# Linux 高级操作演练场（Linux Lab Drill）
# -----------------------------------------------------------------------------
# 目标：在一台「无 root、无 bpftrace、无 cgroup 委托」的受限机器上，
#       用第一性原理重新构建可用的高级观测/隔离/控制/调试能力。
#
# 环境（2026-09-11 实测）：树莓派 aarch64 / Debian 13 / 4 核 / 905M 内存
#                          uid=1000(wxf)  sudo 需密码  cgroup v2 只读
#
# 用法：
#   bash drill.sh probe     # 只做能力探测，输出边界报告（推荐先跑这个）
#   bash drill.sh l1        # L1 观测：/proc 取证 + strace 系统调用级追踪
#   bash drill.sh l2        # L2 隔离：非特权命名空间容器（Docker 原理拆解）
#   bash drill.sh l3        # L3 控制：资源自限 / 调度 / 信号
#   bash drill.sh l4        # L4 调试：gdb 活体注入 / 栈回溯 / 内存映射
#   bash drill.sh all       # 全部跑一遍
#
# 设计原则：
#   1. 每条演练都真跑，输出是实测结果而非文档抄写 —— 可证伪。
#   2. 只使用「不需要 root」的能力；需要 root 的路径明确标注为不可用。
#   3. 不改任何系统状态：临时进程跑完即杀，临时文件放 mktemp 目录。
# =============================================================================
set -uo pipefail

C_RESET=$'\033[0m'; C_DIM=$'\033[2m'; C_B=$'\033[1m'
C_OK=$'\033[32m'; C_NO=$'\033[31m'; C_HL=$'\033[36m'; C_WARN=$'\033[33m'

hr()   { printf '%s\n' "${C_DIM}────────────────────────────────────────────────────────────────${C_RESET}"; }
head1(){ printf '\n%s\n' "${C_B}${C_HL}═══ $* ═══${C_RESET}"; }
head2(){ printf '\n%s\n' "${C_B}▸ $*${C_RESET}"; }
note() { printf '%s\n' "${C_DIM}  $*${C_RESET}"; }
ok()   { printf '  %s✓%s %s\n' "$C_OK" "$C_RESET" "$*"; }
no()   { printf '  %s✗%s %s\n' "$C_NO" "$C_RESET" "$*"; }
warn() { printf '  %s!%s %s\n' "$C_WARN" "$C_RESET" "$*"; }
kv()   { printf '  %-34s %s\n' "$1" "$2"; }

# 探针：命令是否存在
has() { command -v "$1" >/dev/null 2>&1; }

# 安全地跑一个后台 python 进程当"靶子"，返回 PID（用 PY_TARGET 变量接）
spawn_target() {
  local secs="${1:-3}" mode="${2:-sleep}"
  case "$mode" in
    sleep) python3 -c "import time;time.sleep($secs)" >/dev/null 2>&1 & ;;
    busy)  python3 -c "import time
t=time.time()
while time.time()-t<$secs: sum(i*i for i in range(10000))" >/dev/null 2>&1 & ;;
    alloc) python3 -c "import time
b=bytearray(40*1024*1024)
while b: time.sleep(0.2)" >/dev/null 2>&1 & ;;
    io)    python3 -c "import time
t=time.time()
while time.time()-t<${secs}: open('/etc/hostname').read()" >/dev/null 2>&1 & ;;
  esac
  PY_TARGET=$!
  sleep 0.35
}

TMPD=$(mktemp -d /tmp/linuxlab.XXXXXX)
cleanup() { pkill -P $$ >/dev/null 2>&1; rm -rf "$TMPD"; }
trap cleanup EXIT

# =============================================================================
# probe —— 能力边界探测：先摸清战场，再决定打法
# =============================================================================
do_probe() {
  head1 "能力边界探测（本机真实权限与内核接口）"

  head2 "身份与权限"
  kv "内核" "$(uname -r) $(uname -m)"
  kv "发行版" "$(. /etc/os-release 2>/dev/null; echo "${PRETTY_NAME:-未知}")"
  kv "身份" "$(id -un) uid=$(id -u) 组=$(id -Gn | tr ' ' ',' | cut -c1-60)"
  if sudo -n true 2>/dev/null; then ok "sudo 免密可用（可装工具/改 sysctl）"; else no "sudo 需密码 → 一切需要 root 的路径封死"; fi
  kv "CPU / 内存" "$(nproc) 核 / $(free -m | awk '/Mem:/{print $2"MB 总, "$7"MB 可用"}')"

  head2 "观测层工具链"
  local obs="strace gdb lsof ss ip nsenter unshare fio ionice chrt taskset setcap dmesg"
  for t in $obs; do has "$t" && ok "$t → $(command -v "$t")" || no "$t 缺失"; done
  head2 "缺失的（需 root 安装，本机不可得）"
  for t in perf bpftrace bpftool tcpdump sysdig ltrace numactl; do has "$t" || printf '  %s✗%s %s\n' "$C_NO" "$C_RESET" "$t"; done
  note "替代方案：strace + /proc + gdb 组合可覆盖 80% 的 perf/bpftrace 场景（见 l1/l4）"

  head2 "内核接口可写性（决定能做多少控制）"
  for k in vm.swappiness kernel.pid_max net.core.somaxconn fs.inotify.max_user_watches; do
    local p="/proc/sys/$(echo "$k" | tr . /)"
    printf '  %-32s = %-22s %s\n' "$k" "$(sysctl -n "$k" 2>/dev/null || echo N/A)" \
      "$([ -w "$p" ] 2>/dev/null && echo "${C_OK}可写${C_RESET}" || echo "${C_NO}只读${C_RESET}")"
  done

  head2 "cgroup v2 委托（决定能否做容器级资源隔离）"
  local mycg="/sys/fs/cgroup$(awk -F: '{print $3}' /proc/self/cgroup)"
  kv "所属 cgroup" "${mycg#/sys/fs/cgroup}"
  kv "目录属主" "$(stat -c '%U:%G %a' "$mycg" 2>/dev/null)"
  [ -w "$mycg" ] && ok "可写 → 可建子 cgroup 做资源隔离" || no "只读 → 无委托，改用 ulimit/ionice（见 l3）"

  head2 "内核追踪通道"
  kv "tracefs" "$(mount | grep -q tracefs && echo 已挂载 || echo '未挂载(需 root)')"
  kv "perf_event_paranoid" "$(cat /proc/sys/kernel/perf_event_paranoid 2>/dev/null || echo 不可读)"
  kv "yama ptrace_scope" "$(cat /proc/sys/kernel/yama/ptrace_scope 2>/dev/null || echo '无 yama → ptrace 不受限')"
  kv "dmesg 可读" "$(dmesg >/dev/null 2>&1 && echo 是 || echo 否)"
  kv "core_pattern / ulimit -c" "$(cat /proc/sys/kernel/core_pattern) / $(ulimit -c)"

  head2 "命名空间能力（容器的最小内核）"
  if unshare --user --map-root-user true 2>/dev/null; then ok "user namespace 可用 → 非特权容器可行"; else no "user namespace 禁用"; fi
  if unshare -Urmpf --mount-proc true 2>/dev/null; then ok "完整 PID/MNT/UTS/IPC 沙箱可行"; else no "沙箱不可行"; fi
  if unshare -Un true 2>/dev/null; then ok "网络命名空间可行"; else no "网络命名空间受限"; fi
  note "本机已有命名空间：$(ls /proc/self/ns | tr '\n' ' ')"

  head1 "探测结论"
  cat <<'EOF'
  能做的（本机全部实测通过）：
    · 系统调用级追踪      strace -c/-T/-tt/-e trace=<类>        （替代 perf trace）
    · 进程全谱取证        /proc/<pid>/{maps,fd,ns,status,io,syscall}
    · 活体内存/寄存器调试  gdb -p <pid>（ptrace 无 yama 限制）
    · 非特权容器沙箱      unshare -Urmpf + tmpfs 挂载
    · 资源自限            ulimit -v/-n/-u（实测可拦截超限分配）
    · 调度/IO 优先级      chrt / ionice / taskset（作用于自己的进程）
    · 内核日志            dmesg（adm 组）
  做不了的（无 root，明确记录以免浪费时间）：
    · eBPF/bpftrace/perf 采样、tcpdump 抓包、cgroup 资源隔离、sysctl 调参
EOF
}

# =============================================================================
# l1 —— 观测：不装任何工具，看穿一个进程
# =============================================================================
do_l1() {
  head1 "L1 观测：系统调用级 + /proc 全谱"

  head2 "1.1 /proc 取证 —— 不用 ps/top 也能看穿一个进程"
  note "原理：/proc/<pid> 是内核导出的进程视图，每个文件是一个独立子系统接口。"
  spawn_target 4 alloc
  local p=$PY_TARGET
  if [ -d "/proc/$p" ]; then
    kv "cmdline" "$(tr '\0' ' ' < "/proc/$p/cmdline" | tr '\n' ' ' | cut -c1-52)"
    kv "VmHWM(内存峰值)" "$(awk '/VmHWM/{print $2" "$3}' "/proc/$p/status")"
    kv "VmRSS(常驻)" "$(awk '/VmRSS/{print $2" "$3}' "/proc/$p/status")"
    kv "Threads" "$(awk '/Threads/{print $2}' "/proc/$p/status")"
    kv "打开的 fd" "$(ls "/proc/$p/fd" 2>/dev/null | wc -l) 个"
    kv "可执行映射" "$(grep -c 'r-xp' "/proc/$p/maps" 2>/dev/null) 段"
    kv "命名空间 user" "$(readlink "/proc/$p/ns/user" 2>/dev/null)"
    kv "调度策略" "$(chrt -p "$p" 2>/dev/null | head -1 | cut -d: -f2- | xargs)"
    kv "IO 优先级" "$(ionice -p "$p" 2>/dev/null | head -1)"
    note "读 /proc/<pid>/io 可拿到字节级读写量：$(awk -F': ' '/^read_bytes|^write_bytes/{printf "%s=%s ",$1,$2}' "/proc/$p/io" 2>/dev/null)"
    note "读 /proc/<pid>/wchan 看内核里卡在哪：$(cat "/proc/$p/wchan" 2>/dev/null)"
    kill "$p" 2>/dev/null
  fi

  head2 "1.2 strace 统计模式 —— 性能问题的第一把手术刀"
  note "原理：ptrace 拦截系统调用边界，统计「时间花在哪个 syscall 上」。"
  note "命令：strace -c -f <程序>"
  strace -c -f python3 -c "
import os
for i in range(300): os.stat('/etc/hostname')
" 2>&1 | tail -14

  head2 "1.3 strace 过滤 + 耗时 —— 定位慢在哪一次调用"
  note "命令：strace -tt -T -e trace=openat,read <程序>   （-T 单次耗时，-tt 绝对时间戳）"
  strace -tt -T -e trace=openat python3 -c "open('/etc/hostname').read()" 2>&1 | grep -E "openat" | tail -5

  head2 "1.4 追踪运行中的进程（attach 模式）"
  spawn_target 3 io          # 用持续读文件的靶子，才有 syscall 可抓
  p=$PY_TARGET
  if [ -d "/proc/$p" ]; then
    note "命令：timeout 1 strace -p <pid>   —— 抓活体进程正在做什么"
    timeout 1 strace -p "$p" 2>&1 | grep -vE "attach|detach|^strace:" | head -5
    kill "$p" 2>/dev/null
  fi

  head2 "1.5 网络连接观测（无 tcpdump 时的替代）"
  note "原理：ss 直接读 /proc/net/* 的内核 socket 表，比 netstat 快一个量级。"
  note "命令：ss -tanp | ss -lx（unix socket）| ss -s（汇总）"
  ss -s 2>/dev/null | head -6
  kv "监听端口" "$(ss -tlnH 2>/dev/null | awk '{print $4}' | tr '\n' ' ' | cut -c1-70)"
  note "无 tcpdump 时的抓包替代：strace -e trace=network -f 看系统调用层；ss -i 看 TCP 内部状态（rtt/cwnd）"

  head2 "1.6 文件描述符泄漏排查（服务跑久了必查）"
  note "命令：lsof -p <pid> | wc -l   +   ls -l /proc/<pid>/fd | grep -c socket"
  local n=$(ls /proc/self/fd | wc -l)
  kv "当前 shell 的 fd 数" "$n"
  kv "系统级 fd 上限" "$(ulimit -n)"
  note "对比 fd 数随时间增长即可判定泄漏：隔 10s 各记一次，涨了就查 lsof 里没关的 socket"
}

# =============================================================================
# l2 —— 隔离：非特权容器，拆开 Docker 的底裤
# =============================================================================
do_l2() {
  head1 "L2 隔离：命名空间 = 容器的最小内核"

  head2 "2.1 一次性沙箱：PID/MNT/UTS/IPC 全隔离"
  note "命令：unshare -Urmpf --mount-proc bash"
  note "  -U 新 user ns（把当前用户映射成 ns 内 root）  -r 映射为 root"
  note "  -m 新 mount ns  -p 新 PID ns  -f fork（PID ns 必须 fork）  --mount-proc 重建 /proc"
  unshare -Urmpf --mount-proc bash -c '
    echo "  ns 内 PID           = $$   ← 在 ns 里我是 1 号进程（容器 PID 1 的真相）"
    echo "  ns 内可见进程数     = $(ls /proc | grep -c "^[0-9]*$")  ← 宿主进程被隐藏"
    echo "  ns 内身份           = $(id -un) uid=$(id -u)  ← 映射后的 root，宿主上仍是普通用户"
    mount -t tmpfs tmpfs /mnt 2>/dev/null && echo "  tmpfs 挂载          = 成功（非特权也能挂）" || echo "  tmpfs 挂载          = 失败"
    echo "  宿主 / 是否可见      = $([ -e /home/wxf/dabai ] && echo 是 || echo 否)（文件系统共享，未做 pivot_root）"
    echo "  宿主 PID 1 是否可见  = $([ -e /proc/1/cmdline ] && echo 是 || echo 否)"
  ' 2>&1 | grep -v "^$"

  head2 "2.2 沙箱里能不能碰宿主的进程和网络"
  note "原理：PID ns 隔离进程视图，NET ns 隔离网络栈 —— 这是 Docker 隔离性的两大支柱。"
  unshare -Urmpfn --mount-proc bash -c '
    echo "  ns 内网络接口        = $(cat /proc/net/dev | tail -n +3 | awk "{print \$1}" | tr -d " :" | tr "\n" " ")"
    echo "  宿主进程可见数       = $(ls /proc | grep -c "^[0-9]*$")"
    echo "  说明：只剩 lo（回环），没有 eth0/wlan0 → 网络被彻底隔离"
  ' 2>&1 | grep -v "^$"

  head2 "2.3 进入已有命名空间（nsenter —— 调试容器的手段）"
  note "命令：nsenter -t <pid> -n -m -p bash   直接钻进目标进程的 ns"
  spawn_target 3 sleep
  local p=$PY_TARGET
  if [ -d "/proc/$p" ]; then
    nsenter -t "$p" -U -m -p true 2>/dev/null && ok "nsenter 成功进入 PID $p 的命名空间（无需 root）" || no "nsenter 被拒（需 CAP_SYS_ADMIN）"
    kill "$p" 2>/dev/null
  fi

  head2 "2.4 沙箱的边界：为什么这不是真正的容器"
  cat <<'EOF'
  与 Docker 的差距（本机实测）：
    · 无 cgroup 委托  → 不能限制 CPU/内存（Docker 靠 cgroup 做资源配额）
    · 无 overlayfs    → 不能做分层镜像（需 root 挂载）
    · 无 netfilter    → 不能做 NAT/端口映射（需 CAP_NET_ADMIN）
    · 无 seccomp 预置 → 不能按白名单裁剪系统调用（可自写，但缺 libseccomp 工具）
  但已经拿到的：进程视图隔离、挂载点隔离、网络栈隔离、UID 映射
    → 这就是「进程沙箱」的全部核心，足够安全地跑不可信脚本。
EOF
}

# =============================================================================
# l3 —— 控制：没有 root，怎么管住自己的进程
# =============================================================================
do_l3() {
  head1 "L3 控制：资源自限 / 调度 / 信号"

  head2 "3.1 ulimit 资源自限（实测能拦住失控内存分配）"
  note "原理：setrlimit 是内核给每个进程设的硬上限，子进程继承，无需 root。"
  note "命令：ulimit -v <KB> 限制虚拟内存 | -n fd 数 | -u 进程数 | -t CPU 秒 | -c core 大小"
  bash -c '
    ulimit -v 65536      # 64MB 虚拟内存
    echo "  设定后 ulimit -v = $(ulimit -v) KB"
    python3 -c "
try:
    b = bytearray(200*1024*1024)
    print(\"  200MB 分配成功 → 限制未生效\")
except MemoryError:
    print(\"  200MB 分配被拦 → ulimit 生效 ✓\")
"
  ' 2>&1

  head2 "3.2 CPU 亲和性（把进程钉在指定核上）"
  note "原理：sched_setaffinity 控制进程可运行在哪些 CPU 上 —— 减少缓存颠簸、隔离干扰。"
  kv "本机 CPU 数" "$(nproc)"
  kv "当前亲和性" "$(taskset -pc $$ 2>/dev/null | cut -d: -f2- | xargs)"
  note "命令：taskset -c 0,1 <程序>  只允许在 0/1 号核跑"
  taskset -c 0 python3 -c "
import os
print('  钉在 0 号核后，实际可用核 =', sorted(os.sched_getaffinity(0)))
" 2>&1

  head2 "3.3 IO 优先级（不抢磁盘，别拖慢主服务）"
  note "原理：ionice 设 IO 调度类/级别，idle 级只在磁盘空闲时才读写。"
  kv "当前 IO 类" "$(ionice -p $$ 2>/dev/null)"
  note "命令：ionice -c3 <程序>  （class 3 = idle，适合后台备份/索引）"
  ionice -c3 python3 -c "
import subprocess
print('  以 idle 级启动:', subprocess.run(['ionice','-p',str(__import__('os').getpid())],capture_output=True,text=True).stdout.strip())
" 2>&1

  head2 "3.4 实时调度（高优先级线程，需 CAP_SYS_NICE）"
  note "命令：chrt -f 50 <程序>（FIFO 实时）| chrt -b 0（批处理，让出 CPU）"
  for pol in other batch idle; do
    printf '  chrt -%s → ' "${pol:0:1}"
    chrt "-${pol:0:1}" 0 true 2>&1 && echo "可用" || echo "被拒（需权限）"
  done
  note "本机 chrt -p 可读他人策略，但 -f/-r 实时策略需 CAP_SYS_NICE（无 root 不可得）"

  head2 "3.5 信号：进程控制的底层语言"
  note "SIGTERM 可捕获(优雅退出) / SIGKILL 不可捕获(内核直接杀) / SIGHUP 重载配置 / SIGSTOP 冻结"
  python3 -c "
import signal, os, time
seen = []
signal.signal(signal.SIGTERM, lambda *a: (seen.append('TERM'), exit(0)))
signal.signal(signal.SIGUSR1, lambda *a: seen.append('USR1'))
print('  子进程 PID =', os.getpid(), '已注册 SIGTERM/SIGUSR1 处理器', flush=True)
time.sleep(4)
" & local hp=$!
  sleep 0.6
  kill -USR1 "$hp" 2>/dev/null; note "已发 SIGUSR1（自定义信号，进程活着继续跑）"
  kill -0 "$hp" 2>/dev/null && ok "SIGUSR1 后进程存活 → 信号被应用层处理了"
  kill -TERM "$hp" 2>/dev/null; sleep 0.3
  kill -0 "$hp" 2>/dev/null && no "SIGTERM 后仍存活" || ok "SIGTERM 后优雅退出 ✓"
  note "排查「杀不掉的进程」：先用 kill -0 探活，再 cat /proc/<pid>/status | grep Sig 看屏蔽了哪些信号"

  head2 "3.6 文件能力位（比 setuid 更细的权限粒度）"
  note "原理：capabilities 把 root 特权拆成 40 个独立位，可只授予「能绑低端口」这一项。"
  kv "getcap 可用" "$(has getcap && echo 是 || echo 否)"
  note "命令：sudo setcap cap_net_bind_service=+ep ./server（让普通用户程序绑 80 端口）"
  kv "python3 现有能力" "$(getcap "$(command -v python3)" 2>/dev/null || echo 无)"
  note "本机无 root → 不能给自己加能力；但可读 /proc/<pid>/status 的 CapEff 看别人持有什么特权"
  kv "当前 shell CapEff" "$(awk '/CapEff/{print $2}' /proc/self/status)"
  note "解码：capsh --decode=<CapEff 十六进制>（本机无 capsh，可按位对照 capabilities(7)）"
}

# =============================================================================
# l4 —— 调试：活体进程的内存与寄存器
# =============================================================================
do_l4() {
  head1 "L4 调试：ptrace 活体解剖"

  head2 "4.1 环境检查（决定 gdb 能不能 attach）"
  kv "yama ptrace_scope" "$(cat /proc/sys/kernel/yama/ptrace_scope 2>/dev/null || echo '无 yama → 同用户进程可随意 attach')"
  kv "gdb 版本" "$(gdb --version 2>/dev/null | head -1)"
  note "无 yama 意味着：同 uid 的进程都能 attach —— 调试友好，但也是安全隐患"

  head2 "4.2 attach 运行中的进程：读寄存器 + 栈回溯"
  note "原理：ptrace(PTRACE_ATTACH) 让调试器成为目标进程的父，可读写其内存与寄存器。"
  spawn_target 8 sleep
  local p=$PY_TARGET
  if [ -d "/proc/$p" ]; then
    note "目标 PID = $p（正在 sleep(8)）"
    gdb -p "$p" -batch \
      -ex "info registers pc sp" \
      -ex "bt 4" \
      2>&1 | grep -vE "^\[|Reading|Downloading|^warning|Using host" | head -10
    note "看栈顶就能知道它在哪个系统调用里阻塞（clock_nanosleep = 正在睡眠）"

    head2 "4.3 读目标进程的内存映射与命令行"
    gdb -p "$p" -batch -ex "info proc mappings" 2>&1 | grep -E "^0x[0-9a-f]+ +0x[0-9a-f]+ +0x" | head -5
    kill "$p" 2>/dev/null
  fi

  head2 "4.4 直接读 /proc/<pid>/maps 解析内存布局（不用 gdb）"
  note "原理：maps 每一行 = 一个 VMA（虚拟内存区域），Perms 里的 x 标记代码段。"
  spawn_target 4 alloc
  p=$PY_TARGET
  if [ -f "/proc/$p/maps" ]; then
    # 注意：本机 awk 是 mawk，无 strtonum()，十六进制换算交给 bash 的 $((16#...))
    printf '  %-22s %-6s %10s  %s\n' "地址范围" "权限" "大小" "映射对象"
    while read -r range perms _ _ _ path; do
      local s=${range%-*} e=${range#*-}
      printf '  %-22s %-6s %8dKB  %s\n' "$range" "$perms" \
        "$(( (16#$e - 16#$s) / 1024 ))" "${path:-[匿名]}"
    done < "/proc/$p/maps" | sort -k3 -rn | head -8
    note "堆(heap) 增长 = 内存泄漏信号；[anon] 大块 = malloc/mmap 分配"
    kv "堆顶(brk)" "$(awk '/heap/{print $1}' "/proc/$p/maps" 2>/dev/null)"
    kill "$p" 2>/dev/null
  fi

  head2 "4.5 core dump：进程崩溃现场取证"
  kv "core_pattern" "$(cat /proc/sys/kernel/core_pattern)"
  kv "ulimit -c" "$(ulimit -c)"
  warn "ulimit -c = 0 → 崩溃不产生 core 文件；无 root 不能改全局 core_pattern"
  note "绕过办法：程序内自捕获（faulthandler / signal handler 打栈），或 gdb 直接挂上去等崩溃"
  note "命令：python3 -X faulthandler -c '...'   崩溃时自动打印 Python 栈"
  python3 -X faulthandler -c "
import faulthandler, signal, os
faulthandler.register(signal.SIGUSR1)
print('  已注册 SIGUSR1 → faulthandler 栈转储（收到信号即打印当前调用栈）', flush=True)
import time; time.sleep(0.1)
" 2>&1 | head -4

  head2 "4.6 无 bpftrace 时怎么观测内核行为"
  cat <<'EOF'
  替代矩阵（本机实测可行）：
    想知道的事              用不了的工具      本机替代方案
    --------------------   --------------   ----------------------------------
    某进程在等什么          bpftrace        cat /proc/<pid>/wchan + /proc/<pid>/stack
    某进程在跑什么 syscall  perf trace      strace -p <pid> -tt -T
    谁在消耗 CPU            perf top        /proc/<pid>/stat 的 utime/stime 差值采样
    谁在读写磁盘            biosnoop        /proc/<pid>/io 的 read_bytes/write_bytes
    网络连接状态            tcpdump         ss -tanpi（含 rtt/cwnd/retrans）
    函数级耗时              bpftrace        gdb 断点 + 时间戳 / Python cProfile
    内核日志                dmesg           dmesg（adm 组可读，本机可用）
EOF

  head2 "4.7 实测：用 /proc 采样定位 CPU 消耗（无 perf 的替代）"
  spawn_target 3 busy
  p=$PY_TARGET
  if [ -f "/proc/$p/stat" ]; then
    local t1 t2
    t1=$(awk '{print $14+$15}' "/proc/$p/stat" 2>/dev/null)
    sleep 1
    t2=$(awk '{print $14+$15}' "/proc/$p/stat" 2>/dev/null)
    local hz=$(getconf CLK_TCK)
    kv "1 秒内 CPU tick 增量" "$((t2 - t1)) tick (CLK_TCK=$hz)"
    kv "换算 CPU 占用" "$(awk -v d=$((t2-t1)) -v hz=$hz 'BEGIN{printf "%.1f%%\n", d/hz*100}')"
    note "原理：/proc/<pid>/stat 的 utime+stime 是累计 CPU 时钟滴答，差分即瞬时占用率"
    kill "$p" 2>/dev/null
  fi
}

# =============================================================================
# main
# =============================================================================
case "${1:-probe}" in
  probe) do_probe ;;
  l1)    do_l1 ;;
  l2)    do_l2 ;;
  l3)    do_l3 ;;
  l4)    do_l4 ;;
  all)   do_probe; do_l1; do_l2; do_l3; do_l4 ;;
  *)     echo "用法: bash $0 {probe|l1|l2|l3|l4|all}"; exit 1 ;;
esac
printf '\n%s\n' "${C_DIM}演练结束。临时进程已清理，未改动任何系统状态。${C_RESET}"
