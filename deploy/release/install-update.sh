#!/usr/bin/env bash
# 把自动更新装到这台机器上。
#
# 装什么：
#   /usr/local/lib/dabai-update/update.py        更新器（root 拥有，跑在仓库之外）
#   /etc/dabai/update.conf                       本机配置
#   /etc/sudoers.d/dabai-update                  窄口径免密：只允许停/起/查这一个服务
#   /etc/systemd/system/dabai-update.{service,timer}
#
# 为什么更新器要有一份 root 拥有的副本，而不是直接跑仓库里那份：
#   仓库正是被更新的对象。跑仓库里那份，会在替换到一半时把正在执行的脚本换掉 ——
#   更新器的可信度不能建立在被更新物之上。
#
# 为什么用 sudoers 免密而不是让更新器整个跑成 root：
#   更新器要往 /home/<user> 里写文件，跑成 root 会把文件属主全变成 root。
#   所以它按普通用户跑，只在「重启服务」这一件事上升权，而升权范围被钉死到具体命令。
#
# 用法：
#   sudo bash deploy/release/install-update.sh                # 安装
#   sudo bash deploy/release/install-update.sh --dry-run      # 只打印计划，不动系统
#   sudo bash deploy/release/install-update.sh --uninstall    # 卸载（保留状态与备份）
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ROOT="$REPO_ROOT"
STATE="/var/lib/dabai-update"
SERVICE="myservice"
RUN_USER="wxf"
PORT="8000"
REPO_SLUG="wangxingfen/dabai-linux"
LIB_DIR="/usr/local/lib/dabai-update"
CONF="/etc/dabai/update.conf"
SUDOERS="/etc/sudoers.d/dabai-update"
UNIT_DIR="/etc/systemd/system"
DRY=0
UNINSTALL=0

while [ $# -gt 0 ]; do
  case "$1" in
    --root)    shift; ROOT="${1:?--root 后面要跟路径}" ;;
    --state)   shift; STATE="${1:?}" ;;
    --service) shift; SERVICE="${1:?}" ;;
    --user)    shift; RUN_USER="${1:?}" ;;
    --port)    shift; PORT="${1:?}" ;;
    --repo)    shift; REPO_SLUG="${1:?}" ;;
    --dry-run) DRY=1 ;;
    --uninstall) UNINSTALL=1 ;;
    -h|--help) sed -n '2,22p' "$0"; exit 0 ;;
    *) echo "未知参数：$1"; exit 2 ;;
  esac
  shift
done

ok()   { echo "  ✓ $*"; }
step() { echo; echo "$*"; }
run()  { if [ "$DRY" = "1" ]; then echo "    [演练] $*"; else "$@"; fi; }

[ "$(id -u)" = "0" ] || { echo "✘ 需要 root：sudo bash $0"; exit 1; }

SYSTEMCTL="$(command -v systemctl || true)"
[ -n "$SYSTEMCTL" ] || { echo "✘ 找不到 systemctl，这台机器不是 systemd"; exit 1; }

echo "=== 大白自动更新安装（安装目录 $ROOT，服务 $SERVICE，用户 $RUN_USER）==="
[ "$DRY" = "1" ] && echo "（演练模式：只打印计划，不动系统）"

# ── 卸载 ────────────────────────────────────────────────────────────────
if [ "$UNINSTALL" = "1" ]; then
  step "① 停并禁用定时器"
  run "$SYSTEMCTL" disable --now dabai-update.timer || true
  step "② 删除单元文件"
  run rm -f "$UNIT_DIR/dabai-update.service" "$UNIT_DIR/dabai-update.timer"
  step "③ 删除免密规则"
  run rm -f "$SUDOERS"
  step "④ 删除更新器副本"
  run rm -rf "$LIB_DIR"
  step "⑤ 重新加载 systemd"
  run "$SYSTEMCTL" daemon-reload
  echo
  echo "✓ 已卸载。保留：$CONF、$STATE（回滚日志与备份还在，删不删你自己定）"
  exit 0
fi

# ── 前置检查 ────────────────────────────────────────────────────────────
step "① 前置检查"
[ -f "$ROOT/server.py" ] || { echo "✘ $ROOT 里没有 server.py，--root 给错了？"; exit 1; }
ok "安装目录看起来对"
id "$RUN_USER" >/dev/null 2>&1 || { echo "✘ 用户 $RUN_USER 不存在"; exit 1; }
ok "用户 $RUN_USER 存在"
[ -f "$REPO_ROOT/deploy/release/update.py" ] || { echo "✘ 找不到 update.py"; exit 1; }
ok "更新器源码在"

# token 有没有着落：更新器要读私有仓库的发行版
if [ -r /etc/dabai/secrets.env ] && grep -q '^GITHUB_TOKEN=' /etc/dabai/secrets.env; then
  ok "GITHUB_TOKEN 在 /etc/dabai/secrets.env"
elif [ -r "/home/$RUN_USER/.config/dabai/secrets.env" ] && \
     grep -q '^GITHUB_TOKEN=' "/home/$RUN_USER/.config/dabai/secrets.env"; then
  ok "GITHUB_TOKEN 在 ~/.config/dabai/secrets.env"
else
  echo "  ! 没找到 GITHUB_TOKEN —— 仓库是私有的，拉发行版必须有它。"
  echo "    系统级：sudo dabai-secrets set GITHUB_TOKEN github_pat_xxx"
  echo "    用户级：umask 077 && printf 'GITHUB_TOKEN=%s\\n' github_pat_xxx > /home/$RUN_USER/.config/dabai/secrets.env"
fi

# ── 装更新器副本 ────────────────────────────────────────────────────────
step "② 装更新器副本到 $LIB_DIR"
run install -d -m 0755 -o root -g root "$LIB_DIR"
run install -m 0755 -o root -g root "$REPO_ROOT/deploy/release/update.py" "$LIB_DIR/update.py"
ok "update.py 已就位（root:root 0755，普通用户可读可执行、不可改）"

# ── 写配置 ──────────────────────────────────────────────────────────────
step "③ 写配置 $CONF"
CONF_BODY="# 大白自动更新配置（由 deploy/release/install-update.sh 生成，手改也行）
# 改完不用重启任何东西：更新器每次运行都会重新读这个文件。
ROOT=$ROOT
STATE=$STATE
REPO=$REPO_SLUG
SERVICE=$SERVICE
PORT=$PORT
ENTRY=server.py
KEEP_BACKUPS=3
HTTP_TIMEOUT=60
"
if [ "$DRY" = "1" ]; then
  echo "    [演练] 会写入 $CONF："
  echo "$CONF_BODY" | sed 's/^/      /'
else
  install -d -m 0755 /etc/dabai
  printf '%s' "$CONF_BODY" > "$CONF"
  chmod 0644 "$CONF"
fi
run install -d -m 0750 -o "$RUN_USER" -g "$RUN_USER" "$STATE"
ok "配置与状态目录就位"

# ── 窄口径免密 ──────────────────────────────────────────────────────────
step "④ 写免密规则 $SUDOERS（只允许停/起/查 $SERVICE）"
SUDO_BODY="# 大白自动更新专用。范围被钉死到「这一个服务」的具体动作上：
# 不给 systemctl 通配、不给 shell、不给包管理器。更新器能做的仅此一件事。
# 更新会停机重启，所以 stop 也在列 —— 但它只能停这一个服务。
$RUN_USER ALL=(root) NOPASSWD: $SYSTEMCTL stop $SERVICE
$RUN_USER ALL=(root) NOPASSWD: $SYSTEMCTL start $SERVICE
$RUN_USER ALL=(root) NOPASSWD: $SYSTEMCTL restart $SERVICE
$RUN_USER ALL=(root) NOPASSWD: $SYSTEMCTL is-active $SERVICE
$RUN_USER ALL=(root) NOPASSWD: $SYSTEMCTL stop $SERVICE.service
$RUN_USER ALL=(root) NOPASSWD: $SYSTEMCTL start $SERVICE.service
$RUN_USER ALL=(root) NOPASSWD: $SYSTEMCTL restart $SERVICE.service
$RUN_USER ALL=(root) NOPASSWD: $SYSTEMCTL is-active $SERVICE.service
"
if [ "$DRY" = "1" ]; then
  echo "$SUDO_BODY" | sed 's/^/    [演练] /'
else
  printf '%s' "$SUDO_BODY" > "$SUDOERS"
  chmod 0440 "$SUDOERS"
  if command -v visudo >/dev/null 2>&1; then
    # 先校验再落地 —— 写坏 sudoers 会把 sudo 整个锁死
    if ! visudo -cf "$SUDOERS" >/dev/null 2>&1; then
      rm -f "$SUDOERS"
      echo "✘ sudoers 语法没过，已撤回"
      exit 1
    fi
    ok "visudo 校验通过"
  else
    echo "  ! 没有 visudo，无法预校验 —— 请自行确认 $SUDOERS 无误"
  fi
fi

# ── 装 systemd 单元 ─────────────────────────────────────────────────────
step "⑤ 装 systemd 单元"
run install -m 0644 "$REPO_ROOT/deploy/systemd/dabai-update.service" "$UNIT_DIR/dabai-update.service"
run install -m 0644 "$REPO_ROOT/deploy/systemd/dabai-update.timer" "$UNIT_DIR/dabai-update.timer"
run "$SYSTEMCTL" daemon-reload
run "$SYSTEMCTL" enable --now dabai-update.timer
ok "定时器已启用（每天 04:30 前后，随机错开最多 30 分钟）"

# ── 接线自检 ────────────────────────────────────────────────────────────
step "⑥ 接线自检"
if [ "$DRY" = "1" ]; then
  echo "    [演练] 跳过自检"
  echo
  echo "✓ 演练完成，未动系统。正式安装：sudo bash $0"
  exit 0
fi

echo "  跑一次 --check（只查版本，不写盘）："
if sudo -u "$RUN_USER" /usr/bin/python3 "$LIB_DIR/update.py" --check 2>&1 | sed 's/^/    /'; then
  ok "更新器能跑起来"
else
  echo "  ! --check 没成功。常见原因：GITHUB_TOKEN 没配、或网络到不了 api.github.com"
fi

echo "  验证免密范围（只该允许那几个命令）："
if sudo -u "$RUN_USER" sudo -n "$SYSTEMCTL" is-active "$SERVICE" >/dev/null 2>&1; then
  ok "免密规则生效：$RUN_USER 可以查 $SERVICE 状态"
else
  echo "  ! 免密规则没生效，更新器将无法重启服务"
fi
if sudo -u "$RUN_USER" sudo -n "$SYSTEMCTL" restart nonexistent-svc >/dev/null 2>&1; then
  echo "  ✘ 免密范围过宽！$RUN_USER 能操作任意服务，请检查 $SUDOERS"
  exit 1
else
  ok "免密范围没有越界：操作其它服务仍被拒绝"
fi

echo
echo "✓ 安装完成"
echo "  手动更新一次： sudo -u $RUN_USER /usr/bin/python3 $LIB_DIR/update.py --apply"
echo "  演练一次：     sudo -u $RUN_USER /usr/bin/python3 $LIB_DIR/update.py --dry-run"
echo "  看定时器：     systemctl list-timers dabai-update.timer"
echo "  看日志：       journalctl -u dabai-update.service -n 50"
