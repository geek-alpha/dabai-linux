#!/usr/bin/env bash
# ==============================================================================
# DABAI 密钥环境变量化 —— 安装 / 卸载（需 root）
#
# 装了什么：
#   1. /usr/local/lib/dabai-secrets/sync_secrets.py   同步器本体（root:root 755）
#   2. /usr/local/sbin/dabai-secrets                  CLI 包装（烧入仓库路径）
#   3. /etc/dabai/secrets.env                         派生环境变量（root:wxf 0640）
#   4. /etc/systemd/system/dabai-secrets-sync.service 一次性同步单元
#      /etc/systemd/system/dabai-secrets.path         监听 JSON → 实时同步
#      /etc/systemd/system/dabai-secrets-sync.timer   低频兜底（幂等，无变化不写盘）
#   5. /etc/profile.d/00-dabai-secrets.sh             登录 shell 注入（带权限判断）
#   6. myservice drop-in：EnvironmentFile=-/etc/dabai/secrets.env
#   7. /etc/sudoers.d/dabai-secrets                   只给 sync 免密
#
# 安全边界：
#   - 用 EnvironmentFile= 而不是 Environment= —— 后者会被 `systemctl show` 明文暴露
#   - 绝不写 /etc/environment —— 那是 0644 全局可读
#   - profile.d 脚本先判 `[ -r ]`，非 wxf 组用户加载不到任何密钥
#   - 改 myservice 配置后**不重启**服务：它的 MainPID 就是大白本体，
#     重启会杀掉调用方自己。daemon-reload 足够，下次启动自然生效。
#
# 用法：
#   sudo bash install-secrets.sh              安装 / 更新
#   sudo bash install-secrets.sh --check      只体检，不改动
#   sudo bash install-secrets.sh --uninstall  卸载（保留 secrets.env）
#   sudo bash install-secrets.sh --purge      卸载并删除 secrets.env
# ==============================================================================
set -euo pipefail

REPO_DEFAULT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
REPO="${DABAI_REPO:-$REPO_DEFAULT}"

LIB_DIR=/usr/local/lib/dabai-secrets
BIN=/usr/local/sbin/dabai-secrets
ENV_DIR=/etc/dabai
ENV_FILE="$ENV_DIR/secrets.env"
PROFILE_HOOK=/etc/profile.d/00-dabai-secrets.sh
SYSTEMD_DIR=/etc/systemd/system
DROPIN_DIR="$SYSTEMD_DIR/myservice.service.d"
DROPIN="$DROPIN_DIR/30-secrets.conf"
SUDOERS=/etc/sudoers.d/dabai-secrets
SVC_USER=wxf

C_OK=$'\033[1;32m'; C_NO=$'\033[1;31m'; C_WARN=$'\033[1;33m'; C_DIM=$'\033[2m'; C_END=$'\033[0m'

ok()   { printf '  %s✓%s %s\n' "$C_OK" "$C_END" "$1"; }
bad()  { printf '  %s✗%s %s\n' "$C_NO" "$C_END" "$1"; }
warn() { printf '  %s!%s %s\n' "$C_WARN" "$C_END" "$1"; }
info() { printf '  %s%s%s\n' "$C_DIM" "$1" "$C_END"; }
step() { printf '\n\033[1;36m▸ %s\033[0m\n' "$1"; }

need_root() {
  if [ "$(id -u)" -ne 0 ]; then
    bad "需要 root：sudo bash $0 $*"
    exit 1
  fi
}

backup() {
  local f="$1"
  if [ -f "$f" ] && [ ! -f "$f.bak-secrets" ]; then
    cp -a "$f" "$f.bak-secrets"
    info "备份 $f → $f.bak-secrets"
  fi
}

# ------------------------------------------------------------------------------
# 步骤 1：同步器本体 + CLI
# ------------------------------------------------------------------------------
install_bin() {
  step "1  同步器与 CLI"
  local src="$REPO/deploy/secrets/sync_secrets.py"
  if [ ! -f "$src" ]; then
    bad "找不到 $src"
    exit 1
  fi

  install -d -m 0755 "$LIB_DIR"
  install -m 0755 "$src" "$LIB_DIR/sync_secrets.py"
  ok "同步器 → $LIB_DIR/sync_secrets.py"

  # CLI 包装：把仓库路径烧进去，避免装到 /usr/local 后找不到 settings.json
  cat > "$BIN" <<EOF
#!/bin/sh
# DABAI 密钥同步器 CLI（由 install-secrets.sh 生成，勿手改）
DABAI_REPO="\${DABAI_REPO:-$REPO}"
DABAI_SECRETS_GROUP="\${DABAI_SECRETS_GROUP:-$SVC_USER}"
export DABAI_REPO DABAI_SECRETS_GROUP
exec python3 "$LIB_DIR/sync_secrets.py" "\$@"
EOF
  chmod 0755 "$BIN"
  ok "CLI → $BIN（仓库路径已烧入：$REPO）"
}

# ------------------------------------------------------------------------------
# 步骤 2：生成 secrets.env 并摆正权限
# ------------------------------------------------------------------------------
install_envfile() {
  step "2  生成 /etc/dabai/secrets.env"
  install -d -m 0750 -o root -g "$SVC_USER" "$ENV_DIR"
  ok "目录 $ENV_DIR（root:$SVC_USER 0750）"

  if "$BIN" sync --quiet; then
    ok "已同步派生变量"
  else
    bad "同步失败（看上面的报错）"
    exit 1
  fi

  local n
  n=$(grep -cE '^[A-Z][A-Z0-9_]*=' "$ENV_FILE" || true)
  ok "文件 $ENV_FILE（root:$SVC_USER 0640，$n 个变量）"
}

# ------------------------------------------------------------------------------
# 步骤 3：systemd —— 实时同步（path unit）
# ------------------------------------------------------------------------------
install_systemd() {
  step "3  systemd 实时同步"

  cat > "$SYSTEMD_DIR/dabai-secrets-sync.service" <<EOF
[Unit]
Description=DABAI API key sync (JSON config -> /etc/dabai/secrets.env)
Documentation=file:$REPO/deploy/secrets/README.md
After=local-fs.target

[Service]
Type=oneshot
ExecStart=$BIN sync --quiet
User=root
# 同步器是幂等的：内容无变化时不写盘，因此定时/频繁触发都无副作用
Nice=10
EOF
  ok "dabai-secrets-sync.service"

  # 监听 JSON：改了配置立刻同步 = 「实时同步」的本体
  cat > "$SYSTEMD_DIR/dabai-secrets.path" <<EOF
[Unit]
Description=Watch DABAI JSON configs for API key changes
Documentation=file:$REPO/deploy/secrets/README.md

[Path]
PathModified=$REPO/settings.json
PathModified=$REPO/codex_config.json
PathModified=$REPO/stt_config.json
PathModified=$REPO/tts_config.json
Unit=dabai-secrets-sync.service

[Install]
WantedBy=paths.target
EOF
  ok "dabai-secrets.path（监听 4 个 JSON）"

  # 兜底：inotify 在某些编辑器/网络文件系统上可能漏事件
  cat > "$SYSTEMD_DIR/dabai-secrets-sync.timer" <<'EOF'
[Unit]
Description=Periodic DABAI API key sync (fallback for missed inotify events)

[Timer]
OnBootSec=2min
OnUnitActiveSec=10min
Unit=dabai-secrets-sync.service
Persistent=false

[Install]
WantedBy=timers.target
EOF
  ok "dabai-secrets-sync.timer（10 分钟兜底）"

  systemctl daemon-reload
  systemctl enable --now dabai-secrets.path >/dev/null 2>&1 || warn "启用 path unit 失败"
  systemctl enable --now dabai-secrets-sync.timer >/dev/null 2>&1 || warn "启用 timer 失败"
  ok "已启用并启动"
}

# ------------------------------------------------------------------------------
# 步骤 4：登录 shell 注入
# ------------------------------------------------------------------------------
install_profile() {
  step "4  登录 shell 注入"
  cat > "$PROFILE_HOOK" <<'EOF'
# DABAI API keys —— 由 install-secrets.sh 安装
# 只读加载：非 wxf 组用户因 0640 权限读不到，静默跳过。
if [ -r /etc/dabai/secrets.env ]; then
    set -a
    # shellcheck disable=SC1091
    . /etc/dabai/secrets.env
    set +a
fi
EOF
  chmod 0644 "$PROFILE_HOOK"
  chown root:root "$PROFILE_HOOK"
  ok "$PROFILE_HOOK"
  info "生效时机：下次登录 / 新开的 shell（当前会话用 . $PROFILE_HOOK 立即加载）"
}

# ------------------------------------------------------------------------------
# 步骤 5：myservice 注入（不重启！）
# ------------------------------------------------------------------------------
install_dropin() {
  step "5  myservice 环境注入"
  install -d -m 0755 "$DROPIN_DIR"

  local tmp
  tmp="$(mktemp)"
  cat > "$tmp" <<'EOF'
# DABAI API keys —— 由 deploy/secrets/install-secrets.sh 安装
#
# 用 EnvironmentFile= 而不是 Environment=：
#   `systemctl show` 会把 Environment= 的内容明文列给任何用户看，
#   而 EnvironmentFile= 只暴露路径，内容受文件权限（0640）保护。
#
# 前缀 `-` = 文件不存在时不报错（未安装密钥时服务照常启动）。
[Service]
EnvironmentFile=-/etc/dabai/secrets.env
EOF

  if [ -f "$DROPIN" ] && cmp -s "$tmp" "$DROPIN"; then
    ok "drop-in 已是最新，无需改动"
    rm -f "$tmp"
  else
    backup "$DROPIN"
    install -m 0644 "$tmp" "$DROPIN"
    rm -f "$tmp"
    ok "$DROPIN"
  fi

  systemctl daemon-reload
  ok "daemon-reload 完成（配置已读入 unit）"

  # 关键：绝不重启 myservice —— 它的 MainPID 就是当前正在服务用户的进程
  local mainpid
  mainpid="$(systemctl show myservice.service -p MainPID --value 2>/dev/null || echo 0)"
  if [ -n "$mainpid" ] && [ "$mainpid" != "0" ]; then
    warn "未重启 myservice（MainPID=$mainpid 就是大白本体，重启会中断当前会话）"
    info "新变量在下次重启后生效；要立刻用可跑：dabai-secrets env"
  fi
}

# ------------------------------------------------------------------------------
# 步骤 6：sudoers —— 只给 sync 免密
# ------------------------------------------------------------------------------
install_sudoers() {
  step "6  sudo 免密（仅 sync）"
  local tmp
  tmp="$(mktemp)"
  cat > "$tmp" <<EOF
# DABAI 密钥同步：只允许免密跑 sync（幂等、只读 JSON 写固定文件）
# 刻意不给 set 免密 —— 那条能写任意环境变量，留一道密码。
$SVC_USER ALL=(root) NOPASSWD: $BIN sync, $BIN sync --quiet
EOF
  chmod 0440 "$tmp"

  if ! visudo -cf "$tmp" >/dev/null 2>&1; then
    bad "sudoers 语法校验失败，已放弃写入（避免锁死 sudo）"
    rm -f "$tmp"
    return 1
  fi
  install -m 0440 -o root -g root "$tmp" "$SUDOERS"
  rm -f "$tmp"
  ok "$SUDOERS（visudo 校验通过）"
}

# ------------------------------------------------------------------------------
# 体检
# ------------------------------------------------------------------------------
do_check() {
  echo
  echo "═══ 体检 ═══"
  local fail=0

  step "文件"
  for f in "$LIB_DIR/sync_secrets.py" "$BIN" "$ENV_FILE" "$PROFILE_HOOK" \
           "$SYSTEMD_DIR/dabai-secrets.path" "$DROPIN"; do
    if [ -e "$f" ]; then
      ok "$(ls -l "$f" | awk '{print $1, $3":"$4, $NF}')"
    else
      bad "缺失 $f"; fail=1
    fi
  done

  step "权限（关键）"
  local mode
  mode="$(stat -c '%a %U:%G' "$ENV_FILE" 2>/dev/null || echo '?')"
  if [ "$mode" = "640 root:$SVC_USER" ]; then
    ok "$ENV_FILE = $mode"
  else
    bad "$ENV_FILE 权限为 $mode，应为 640 root:$SVC_USER"; fail=1
  fi

  step "服务状态"
  for u in dabai-secrets.path dabai-secrets-sync.timer; do
    if systemctl is-active --quiet "$u"; then
      ok "$u active"
    else
      bad "$u 未运行"; fail=1
    fi
  done

  step "一致性"
  if "$BIN" check >/dev/null 2>&1; then
    ok "secrets.env 与 JSON 配置一致"
  else
    bad "不一致 —— 跑：sudo -n $BIN sync"; fail=1
  fi

  step "注入验证"
  if grep -q 'EnvironmentFile=-/etc/dabai/secrets.env' "$DROPIN" 2>/dev/null; then
    ok "myservice drop-in 已挂 EnvironmentFile"
  else
    bad "drop-in 未挂 EnvironmentFile"; fail=1
  fi
  if systemctl show myservice.service -p Environment --value | grep -qE 'sk-[A-Za-z0-9]{20,}'; then
    bad "systemctl show 暴露了明文密钥！"
    fail=1
  else
    ok "systemctl show 未泄漏密钥"
  fi

  echo
  if [ "$fail" -eq 0 ]; then
    printf '%s═══ 全部通过 ═══%s\n' "$C_OK" "$C_END"
  else
    printf '%s═══ 有项目未通过 ═══%s\n' "$C_NO" "$C_END"
  fi
  return "$fail"
}

# ------------------------------------------------------------------------------
# 卸载
# ------------------------------------------------------------------------------
do_uninstall() {
  local purge="${1:-no}"
  step "卸载"
  systemctl disable --now dabai-secrets.path >/dev/null 2>&1 || true
  systemctl disable --now dabai-secrets-sync.timer >/dev/null 2>&1 || true
  rm -f "$SYSTEMD_DIR/dabai-secrets.path" \
        "$SYSTEMD_DIR/dabai-secrets-sync.service" \
        "$SYSTEMD_DIR/dabai-secrets-sync.timer"
  ok "已移除 systemd 单元"

  if [ -f "$DROPIN" ]; then
    rm -f "$DROPIN"
    ok "已移除 myservice drop-in（daemon-reload 后生效）"
  fi
  if [ -f "$DROPIN.bak-secrets" ]; then
    info "原 drop-in 备份在 $DROPIN.bak-secrets（如需恢复请手动 mv）"
  fi

  rm -f "$SUDOERS" && ok "已移除 sudoers 规则"
  rm -f "$PROFILE_HOOK" && ok "已移除 profile.d 钩子"
  rm -f "$BIN" && ok "已移除 CLI"
  rm -rf "$LIB_DIR" && ok "已移除同步器"

  systemctl daemon-reload

  if [ "$purge" = "purge" ]; then
    rm -f "$ENV_FILE"
    ok "已删除 $ENV_FILE"
  else
    info "保留 $ENV_FILE（要删用 --purge）"
  fi

  echo
  ok "卸载完成。myservice 未重启，新配置下次启动生效。"
}

# ------------------------------------------------------------------------------
# 主流程
# ------------------------------------------------------------------------------
main() {
  local mode=install
  case "${1:-}" in
    --check)     mode=check ;;
    --uninstall) mode=uninstall ;;
    --purge)     mode=purge ;;
    -h|--help)   sed -n '2,30p' "$0"; exit 0 ;;
    "")          ;;
    *)           bad "未知参数：$1"; exit 2 ;;
  esac

  need_root "$@"

  if [ "$mode" = "check" ]; then
    do_check
    exit $?
  fi
  if [ "$mode" = "uninstall" ]; then
    do_uninstall no
    exit 0
  fi
  if [ "$mode" = "purge" ]; then
    do_uninstall purge
    exit 0
  fi

  printf '\033[1mDABAI 密钥环境变量化\033[0m\n'
  info "仓库：$REPO"
  info "目标：$ENV_FILE"

  install_bin
  install_envfile
  install_systemd
  install_profile
  install_dropin
  install_sudoers || warn "sudoers 未安装（不影响其他功能）"

  echo
  ok "安装完成"
  echo
  info "接下来："
  info "  sudo -n $BIN sync      免密同步"
  info "  $BIN list              查看全部变量（脱敏）"
  info "  $BIN check             校验一致性"
  info "  . $PROFILE_HOOK        当前 shell 立即加载"
  echo
  do_check || true
}

main "$@"
