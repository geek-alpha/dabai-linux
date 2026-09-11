#!/usr/bin/env bash
# ============================================================
# 安装 / 更新 / 校验 / 卸载「免密特权副本」
#
#   装完后即可：  sudo -n /usr/local/sbin/dabai-linux-native all   ← 不再问密码
#
#   用法：
#     sudo bash deploy/systemd/install-privileged.sh            # 安装或更新
#     sudo bash deploy/systemd/install-privileged.sh --check    # 只校验一致性，不改动
#     sudo bash deploy/systemd/install-privileged.sh --uninstall
#
#   为什么要「装副本」而不是直接给仓库脚本免密：
#     仓库脚本在 /home/wxf/dabai（wxf 可写）。给它免密 = 任何 wxf 权限的
#     进程（恶意 npm 包 / 一次 curl|bash）改一下脚本内容，就能以 root
#     执行任意命令。所以必须让被授权的那个文件放在 root 拥有、wxf 改不动的
#     位置，并且它只消费 root 拥有的源文件（见 apply-linux-native.sh 里的
#     「特权模式守卫」）。
#
#   ⚠️ 改过仓库里的 apply-linux-native.sh 之后必须重跑本脚本，
#      否则 /usr/local/sbin 那份是旧快照（--check 会报出来）。
# ============================================================
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIB=/usr/local/lib/dabai-linux-native
SBIN=/usr/local/sbin/dabai-linux-native
SUDOERS=/etc/sudoers.d/dabai-linux-native
CONF_REL=myservice.service.d/20-linux-native.conf
TARGET_USER=${SUDO_USER:-wxf}

ok()   { printf '  \033[1;32m✓\033[0m %s\n' "$*"; }
warn() { printf '  \033[1;33m!\033[0m %s\n' "$*"; }
die()  { printf '  \033[1;31m✗ %s\033[0m\n' "$*" >&2; exit 1; }
log()  { printf '\n\033[1;36m▸ %s\033[0m\n' "$*"; }

# 仓库脚本 → 特权副本形态：强制源目录指向 root 拥有的 LIB，
# 使外部传入的 DABAI_SRC_DIR 失效（sudo env_reset 之外的第二道保险）。
transform() {
  sed 's|^SRC_DIR=${DABAI_SRC_DIR:-/home/wxf/dabai/deploy/systemd}$|DABAI_SRC_DIR=/usr/local/lib/dabai-linux-native   # 特权副本：强制，忽略外部传入\nSRC_DIR=$DABAI_SRC_DIR|' \
    "$REPO_DIR/apply-linux-native.sh"
}

[ "$(id -u)" -eq 0 ] || die "需要 root：sudo bash $0"

# ------------------------------------------------------------
case "${1:-install}" in

  --uninstall)
    log "卸载免密特权副本"
    rm -f "$SUDOERS" "$SBIN"
    rm -rf "$LIB"
    ok "已移除：$SUDOERS / $SBIN / $LIB"
    echo "  之后请用（会问密码）：sudo bash $REPO_DIR/apply-linux-native.sh all"
    ;;

  --check)
    log "一致性校验（不改动任何文件）"
    rc=0
    for f in "$SBIN" "$LIB/$CONF_REL" "$SUDOERS"; do
      if [ -e "$f" ]; then ok "存在：$f"; else warn "缺失：$f"; rc=1; fi
    done
    tmp=$(mktemp); trap 'rm -f "$tmp"' EXIT
    transform > "$tmp"
    if [ -e "$SBIN" ] && cmp -s "$tmp" "$SBIN"; then
      ok "特权副本 = 仓库脚本的当前形态（无快照漂移）"
    else
      warn "特权副本与仓库脚本不一致 —— 仓库改过但没重装，跑一次安装即可"
      rc=1
    fi
    if [ -e "$LIB/$CONF_REL" ] && cmp -s "$REPO_DIR/$CONF_REL" "$LIB/$CONF_REL"; then
      ok "drop-in 源与仓库一致"
    else
      warn "drop-in 源与仓库不一致"
      rc=1
    fi
    # 属主/权限必须严格
    for f in "$SBIN" "$LIB" "$SUDOERS"; do
      [ -e "$f" ] || continue
      o=$(stat -c '%U:%G %a' "$f")
      case "$f" in
        "$SUDOERS") [ "$o" = "root:root 440" ] || { warn "$f 权限应为 root:root 440，实际 $o"; rc=1; } ;;
        *)          [ "$o" = "root:root 755" ] || [ "$o" = "root:root 644" ] || { warn "$f 权限异常：$o"; rc=1; } ;;
      esac
    done
    # 注意：不能写成 `[ -n ... ] && { ...; }` —— set -e 下条件为假会让整脚本退出
    if [ -n "$(find "$LIB" -perm /022 -print -quit 2>/dev/null || true)" ]; then
      warn "$LIB 下存在他人可写文件 —— 守卫会拒绝执行"
      rc=1
    fi
    echo
    [ "$rc" -eq 0 ] && ok "全部一致" || warn "有不一致项（见上）"
    exit "$rc"
    ;;

  install)
    log "安装 / 更新免密特权副本"

    # 0) 语法自检
    bash -n "$REPO_DIR/apply-linux-native.sh" || die "仓库脚本语法错误，已中止"
    ok "仓库脚本语法 OK"

    # 1) root 拥有的源目录
    install -d -m 0755 -o root -g root "$LIB/myservice.service.d"
    install -m 0644 -o root -g root "$REPO_DIR/$CONF_REL" "$LIB/$CONF_REL"
    ok "源目录就绪：$LIB"

    # 2) 特权副本
    tmp=$(mktemp); trap 'rm -f "$tmp"' EXIT
    transform > "$tmp"
    bash -n "$tmp" || die "变换后语法错误，已中止（原副本未改动）"
    install -m 0755 -o root -g root "$tmp" "$SBIN"
    ok "特权副本就绪：$SBIN"

    # 3) sudoers 规则（先校验，再落地 —— 写坏 sudoers 会锁死 sudo）
    tmp2=$(mktemp); trap 'rm -f "$tmp" "$tmp2"' EXIT
    cat > "$tmp2" <<EOF
# 大白 × Linux 原生集成 —— 免密入口（由 install-privileged.sh 生成）
#
# 安全边界（三点，缺一不可）：
#   1. 只授权这一个绝对路径，无通配符；
#   2. 该脚本 root:root 0755，$TARGET_USER 不可写 —— 改不了它做什么；
#   3. 脚本强制只消费 $LIB/ 下 root 拥有的源文件
#      （外部传入的 DABAI_SRC_DIR 会被覆盖，非 root 属主直接拒绝执行）。
# 因此即使 $TARGET_USER 权限被完全控制，也只能做脚本里写死的事，
# 无法借此执行任意 root 命令。
$TARGET_USER ALL=(root) NOPASSWD: $SBIN
EOF
    visudo -c -f "$tmp2" >/dev/null || die "sudoers 语法校验失败，已中止（未落地）"
    ok "sudoers 语法校验通过"
    install -m 0440 -o root -g root "$tmp2" "$SUDOERS"
    visudo -c >/dev/null 2>&1 || { rm -f "$SUDOERS"; die "全局 sudoers 校验失败，已回滚删除"; }
    ok "sudoers 规则已落地：$SUDOERS"

    echo
    echo "  自检："
    sudo -n -l -U "$TARGET_USER" "$SBIN" 2>&1 | sed 's/^/    /' \
      || warn "规则未被识别（检查 sudoers 文件权限是否 root:root 0440）"
    echo
    ok "完成 —— 之后可用：sudo -n $SBIN all"
    ;;

  *)
    die "未知参数：$1（可选 install / --check / --uninstall）"
    ;;
esac
