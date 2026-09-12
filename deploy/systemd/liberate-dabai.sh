#!/usr/bin/env bash
# ============================================================
# 解放大白 —— 一次性完成两件事（都需要 root）：
#
#   ① 移除 myservice 的 systemd 安全加固（NoNewPrivileges 等）
#      → 解锁 sudo，让「免密特权副本」/usr/local/sbin/dabai-linux-native 真正可用
#   ② 关闭图形界面（lightdm + wayvnc-control，默认 target 改 multi-user）
#      → 树莓派 905MB 内存里省出约 250MB，并消除图形栈的持续唤醒发热
#
# 用法：
#   sudo bash /home/wxf/dabai/deploy/systemd/liberate-dabai.sh
#
# 幂等：重复执行安全，已生效的步骤会跳过。
# 回滚：见 deploy/systemd/README.md（改回 graphical.target + 取消 conf 里注释）
# ============================================================
set -euo pipefail

REPO=/home/wxf/dabai/deploy/systemd
CONF_REL=myservice.service.d/20-linux-native.conf
LIB=/usr/local/lib/dabai-linux-native

ok()   { printf '  \033[1;32m✓\033[0m %s\n' "$*"; }
info() { printf '\n\033[1;36m▸ %s\033[0m\n' "$*"; }
warn() { printf '  \033[1;33m!\033[0m %s\n' "$*"; }

[ "$(id -u)" -eq 0 ] || { echo "需要 root：sudo bash $0" >&2; exit 1; }

# ------------------------------------------------------------
info "① 同步去加固后的 drop-in（仓库 → /etc 与特权副本源）"

SRC="$REPO/$CONF_REL"
[ -f "$SRC" ] || { echo "找不到 $SRC" >&2; exit 1; }

# 落地前先确认目标内容里确实没有 NoNewPrivileges（防止改错文件白跑一趟）
if grep -qE '^[[:space:]]*NoNewPrivileges=yes' "$SRC"; then
  warn "$SRC 里仍有生效的 NoNewPrivileges=yes —— 请先取消该行注释再跑"
  exit 1
fi
ok "源文件已确认无生效的加固项"

install -m 0644 -o root -g root "$SRC" "/etc/systemd/system/$CONF_REL"
ok "已更新 /etc/systemd/system/$CONF_REL"

# 特权副本的源目录必须同步，否则下次 apply-linux-native.sh 会把加固装回来
if [ -d "$LIB/myservice.service.d" ]; then
  install -m 0644 -o root -g root "$SRC" "$LIB/$CONF_REL"
  ok "已同步特权副本源 $LIB/$CONF_REL"
else
  warn "$LIB 不存在，跳过（未安装免密特权副本）"
fi

# ------------------------------------------------------------
info "② 关闭图形界面（省内存 + 消除图形栈唤醒）"

cur_target=$(systemctl get-default)
if [ "$cur_target" = "graphical.target" ]; then
  systemctl set-default multi-user.target
  ok "默认 target: graphical.target → multi-user.target"
else
  ok "默认 target 已是 $cur_target，无需改动"
fi

for u in lightdm.service wayvnc-control.service; do
  if systemctl is-enabled "$u" >/dev/null 2>&1; then
    systemctl disable --now "$u" 2>&1 | sed 's/^/    /' || warn "$u 停止时有问题"
    ok "已停用并禁止自启：$u"
  else
    ok "$u 本就未启用"
  fi
done

# 图形会话里的用户级残留（D-Bus 激活的，会话没了多半自己退，这里顺手掐掉）
if command -v loginctl >/dev/null 2>&1; then
  for sid in $(loginctl list-sessions --no-legend 2>/dev/null | awk '$3=="seat0" || $3=="seat"{print $1}'); do
    loginctl terminate-session "$sid" 2>/dev/null && ok "已结束图形会话 $sid" || true
  done
fi

# ------------------------------------------------------------
info "③ 重载 systemd 并重启大白（本步会短暂断开对话，约 5 秒后回来）"

systemctl daemon-reload
ok "daemon-reload 完成"

# 打印生效后的关键参数，确认加固确实没了
systemctl show myservice.service -p NoNewPrivileges -p ProtectKernelTunables -p UMask 2>/dev/null | sed 's/^/    /'

systemctl restart myservice.service
ok "myservice 已重启"

echo
ok "完成 —— 大白现在拥有完整能力（含 sudo -n /usr/local/sbin/dabai-linux-native）"
