#!/usr/bin/env bash
# ============================================================
# 大白 × Linux 原生集成 —— 系统级增强应用脚本（需 sudo）
#
#   用法：  sudo bash deploy/systemd/apply-linux-native.sh [1|2|3|all]
#
#   1   = 主服务 drop-in（日志标识 / PATH / CPU 权重 / 12 项加固）  立即生效
#   2   = 开启内存 cgroup（改 cmdline.txt）                          需重启生效
#   3   = 内存调优 sysctl（zram 场景：swappiness / vfs_cache_pressure）立即生效
#   all = 1 + 2（默认；即用户点名要的两项）
#
#   环境变量 NO_RESTART=1 = 跳过重启 myservice（因为它的 MainPID 就是
#   大白本体 server.py，在本会话里重启会杀掉脚本自己的父进程）。
#   配合 `sudo -n env NO_RESTART=1 bash $0 all` 使用。
#
#   特性：幂等（重复执行安全）、每次改动前自动备份带时间戳、
#         重启服务前先校验 unit 可解析（避免把服务弄成 failed）、
#         cmdline.txt 写入前校验仍是单行（vfat 单行约束）。
#
#   回滚：见 deploy/systemd/README.md §6
# ============================================================
set -euo pipefail

# 源目录可用 DABAI_SRC_DIR 覆盖：安装到 /usr/local/sbin 的特权副本会指向
# /usr/local/lib/dabai-linux-native（root 拥有），绝不消费用户可写路径。
SRC_DIR=${DABAI_SRC_DIR:-/home/wxf/dabai/deploy/systemd}
DROPIN_SRC="$SRC_DIR/myservice.service.d/20-linux-native.conf"
DROPIN_DST=/etc/systemd/system/myservice.service.d/20-linux-native.conf
CMDLINE=/boot/firmware/cmdline.txt
TS=$(date +%Y%m%d-%H%M%S)
STEP="${1:-all}"
NEED_REBOOT=0

log()  { printf '\n\033[1;36m▸ %s\033[0m\n' "$*"; }
ok()   { printf '  \033[1;32m✓\033[0m %s\n' "$*"; }
warn() { printf '  \033[1;33m!\033[0m %s\n' "$*"; }
die()  { printf '  \033[1;31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "需要 root 权限，请用：sudo bash $0 $STEP"

# ------------------------------------------------------------
# 特权模式守卫（安全边界，别删）
# 一旦源目录被显式指定（= 免密副本模式），就要求它 root 拥有且非他人可写。
# 否则任何 wxf 权限的进程（一个恶意 npm 包、一次 curl|bash）只要改一下源
# 文件，就能借 root 之手往 systemd 塞任意 drop-in（如 User=root +
# 自定义 ExecStart）—— 那就是一步提权。
if [ -n "${DABAI_SRC_DIR:-}" ]; then
  _owner=$(stat -c '%U' "$SRC_DIR" 2>/dev/null || echo '?')
  [ "$_owner" = root ] || die "特权模式：$SRC_DIR 属主是 $_owner，必须 root，拒绝执行"
  if [ -n "$(find "$SRC_DIR" -perm /022 -print -quit 2>/dev/null)" ]; then
    die "特权模式：$SRC_DIR 下存在他人可写文件，拒绝执行"
  fi
fi

# ------------------------------------------------------------
# 自我保护：若 myservice 的 MainPID 是本进程的祖先，说明正跑在大白自己
# 拉起的 shell 里 —— 重启 myservice = 杀掉自己的父进程（回复断在半路），
# 自动跳过重启，不必人工记得加 NO_RESTART=1。
if [ "${NO_RESTART:-0}" != "1" ] && [ "${DABAI_NO_AUTODETECT:-0}" != "1" ]; then
  _mp=$(systemctl show myservice.service -p MainPID --value 2>/dev/null || true)
  if [ -n "${_mp:-}" ] && [ "$_mp" != "0" ]; then
    _p=$$
    while [ "${_p:-0}" -gt 1 ]; do
      _p=$(ps -o ppid= -p "$_p" 2>/dev/null | tr -d ' ' || true)
      [ -z "${_p:-}" ] && break
      if [ "$_p" = "$_mp" ]; then
        NO_RESTART=1
        warn "检测到大白本体是本进程祖先 —— 自动跳过重启（避免自杀）"
        break
      fi
    done
  fi
fi

# ------------------------------------------------------------
step1() {
  log "步骤 1  主服务 drop-in（立即生效）"
  [ -f "$DROPIN_SRC" ] || die "源文件不存在：$DROPIN_SRC"

  if [ -f "$DROPIN_DST" ] && cmp -s "$DROPIN_SRC" "$DROPIN_DST"; then
    ok "drop-in 内容已是最新，跳过写入"
  else
    if [ -f "$DROPIN_DST" ]; then
      cp -a "$DROPIN_DST" "$DROPIN_DST.bak-$TS"
      ok "已备份旧 drop-in → $(basename "$DROPIN_DST").bak-$TS"
    fi
    install -Dm644 "$DROPIN_SRC" "$DROPIN_DST"
    ok "已安装 → $DROPIN_DST"
  fi

  systemctl daemon-reload
  ok "daemon-reload 完成"

  # 重启前先确认 unit 能被解析，避免把服务弄成 failed 起不来
  if ! systemctl cat myservice.service >/dev/null 2>&1; then
    die "myservice.service 解析失败，已中止（未重启，服务仍在旧配置下运行）"
  fi
  ok "unit 解析正常"

  # ⚠️ myservice.service 的 MainPID 就是 server.py 本身（大白的大脑）。
  #    在本会话里重启它 = 杀掉正在执行本脚本的父进程，回复会断在半路。
  #    所以默认**不重启**：daemon-reload 已把新配置读进 unit，
  #    下次重启（或下面步骤 2 要求的重启）自然生效。
  if [ "${NO_RESTART:-0}" = "1" ]; then
    warn "NO_RESTART=1 —— 已跳过重启（配置已加载，下次重启生效）"
  else
    systemctl restart myservice
    sleep 3
    local st
    st=$(systemctl is-active myservice.service 2>/dev/null || true)
    if [ "$st" = "active" ]; then
      ok "服务已重启并运行中"
    else
      warn "服务状态 = $st —— 请查：journalctl -u myservice -n 50"
    fi
  fi

  echo
  echo "  生效结果（应看到 SyslogIdentifier=dabai / CPUWeight=200 / NoNewPrivileges=yes / UMask=0077）："
  systemctl show myservice.service \
    -p SyslogIdentifier -p CPUWeight -p IOWeight -p TasksMax -p UMask \
    -p NoNewPrivileges -p OOMPolicy -p RestrictAddressFamilies \
    | sed 's/^/    /'
}

# ------------------------------------------------------------
step2() {
  log "步骤 2  开启内存 cgroup（需重启生效）"
  [ -f "$CMDLINE" ] || die "找不到 $CMDLINE"

  if grep -q 'cgroup_enable=memory' "$CMDLINE"; then
    ok "已包含 cgroup_enable=memory，跳过写入"
  else
    cp -a "$CMDLINE" "$CMDLINE.bak-$TS"
    ok "已备份 → $(basename "$CMDLINE").bak-$TS"

    # cmdline.txt 必须**单行**：先去尾部换行，追加参数，再补回一个换行
    printf '%s' "$(cat "$CMDLINE")" > "$CMDLINE.tmp"
    printf ' cgroup_enable=memory cgroup_memory=1\n' >> "$CMDLINE.tmp"

    local n
    n=$(wc -l < "$CMDLINE.tmp")
    if [ "$n" -ne 1 ]; then
      rm -f "$CMDLINE.tmp"
      die "写入后 cmdline 变成 $n 行（vfat 要求单行），已中止，原文件未改动"
    fi
    mv "$CMDLINE.tmp" "$CMDLINE"
    ok "已追加：cgroup_enable=memory cgroup_memory=1"
  fi

  echo "  当前内容：$(cat "$CMDLINE")"
  echo "  行数：$(wc -l < "$CMDLINE")（必须为 1）"
  echo "  当前 controllers：$(cat /sys/fs/cgroup/cgroup.controllers)"
  NEED_REBOOT=1
}

# ------------------------------------------------------------
step3() {
  log "步骤 3  内存调优 sysctl（zram 场景，立即生效）"
  local f=/etc/sysctl.d/99-dabai.conf
  [ -f "$f" ] && { cp -a "$f" "$f.bak-$TS"; ok "已备份旧配置 → $(basename "$f").bak-$TS"; }

  cat > "$f" <<'SYSCTL_EOF'
# 大白 × Linux：zram 场景内存调优
#
# zram 是**压缩**内存（本机实测压缩比 3.38:1），换出代价远低于磁盘 swap，
# 所以应该更积极地把冷页换出去 —— 默认 swappiness=60 对 zram 偏低。
vm.swappiness=150
# 更积极地回收目录项/inode 缓存（905MB 内存的机器上，缓存留太久会挤压可用内存）
vm.vfs_cache_pressure=200
SYSCTL_EOF
  ok "已写入 $f"

  sysctl --system >/dev/null 2>&1 || warn "sysctl --system 有告警，见下方实际值"
  sysctl vm.swappiness vm.vfs_cache_pressure | sed 's/^/    /'
}

# ------------------------------------------------------------
case "$STEP" in
  1)   step1 ;;
  2)   step2 ;;
  3)   step3 ;;
  all) step1; step2 ;;
  *)   die "未知参数：$STEP（可选 1 / 2 / 3 / all）" ;;
esac

# ------------------------------------------------------------
log "完成"
if [ "$NEED_REBOOT" -eq 1 ]; then
  warn "内存 cgroup 需重启才生效 —— 重启会中断大白与视频播放，时机由你定"
  echo "    重启：sudo reboot"
  echo "    验证：cat /sys/fs/cgroup/cgroup.controllers   # 应出现 memory"
  echo "    之后可再给大白设内存天花板（MemoryHigh/MemoryMax）"
fi
echo "    回滚：见 deploy/systemd/README.md §6"
