#!/usr/bin/env bash
# 大白服务重启 —— 委托 systemd（唯一权威），再自检、落报告
#
# 用法：
#   tools/restart_server.sh              # 立刻重启
#   tools/restart_server.sh --delay 20   # 延迟 20 秒动手（给发起方留收尾时间）
#   tools/restart_server.sh --check      # 只体检 + 落报告，不重启
#
# 为什么不自己 kill + spawn：server.py 由系统级单元 myservice.service 托管
# （Restart=always / 3s）。实测 2026-09-13 17:53：手动 kill 旧进程后 systemd 3 秒后
# 自己拉起新进程，脚本再 spawn 的第二个实例只会 bind 失败 —— 抢活只会打架。
# 谁托管就找谁重启：动作交给 systemd，脚本只负责下令 + 验收 + 留证据。
#
# 环境变量（测试/多实例用）：DABAI_UNIT / DABAI_REPORT / DABAI_WAIT_SECS
set -uo pipefail

UNIT="${DABAI_UNIT:-myservice.service}"
ROOT="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
PY="$ROOT/venv/bin/python"
REPORT="${DABAI_REPORT:-$ROOT/data/restart_report.txt}"
WAIT_SECS="${DABAI_WAIT_SECS:-60}"

MODE="restart"
DELAY=0
while [ $# -gt 0 ]; do
  case "$1" in
    --check) MODE="check" ;;
    --delay) DELAY="${2:-0}"; shift ;;
  esac
  shift
done
case "$DELAY" in ''|*[!0-9]*) DELAY=0 ;; esac
case "$WAIT_SECS" in ''|*[!0-9]*) WAIT_SECS=60 ;; esac
[ "$DELAY" -gt 0 ] && sleep "$DELAY"

unit_show() { systemctl show -p "$1" --value "$UNIT" 2>/dev/null; }

OLD_PID="$(unit_show MainPID)"
OLD_SINCE="$(unit_show ActiveEnterTimestamp)"
OLD_NRESTARTS="$(unit_show NRestarts)"

# 先写头部：脚本中途被打断也留下可核对的痕迹
{
  echo "操作时间: $(date '+%F %T')"
  echo "单元: $UNIT（$(unit_show FragmentPath)）"
  echo "模式: $MODE"
  echo "旧 PID: ${OLD_PID:-?}（起于 ${OLD_SINCE:-?}，累计重启 ${OLD_NRESTARTS:-?} 次）"
} > "$REPORT" 2>&1

METHOD="未重启（--check）"
if [ "$MODE" = "restart" ]; then
  ERR="$(systemctl restart "$UNIT" 2>&1)"
  if [ -z "$ERR" ]; then
    METHOD="systemctl restart"
  else
    # 没权限时退化成 kill：systemd 的 Restart=always 会在 RestartUSec 后自己拉起
    METHOD="kill + systemd 自动拉起（restart 被拒：$(echo "$ERR" | head -c 120)）"
    [ -n "$OLD_PID" ] && [ "$OLD_PID" != 0 ] && kill -TERM "$OLD_PID" 2>/dev/null
  fi
fi

NEW_PID=""; STATE=""
for _ in $(seq 1 $((WAIT_SECS * 2))); do
  STATE="$(systemctl is-active "$UNIT" 2>/dev/null)"
  NEW_PID="$(unit_show MainPID)"
  if [ "$STATE" = "active" ] && [ -n "$NEW_PID" ] && [ "$NEW_PID" != 0 ] \
     && { [ "$MODE" = "check" ] || [ "$NEW_PID" != "$OLD_PID" ]; }; then
    break
  fi
  sleep 0.5
done

[ "$NEW_PID" = 0 ] && NEW_PID=""
PORTS="$(ss -tln 2>/dev/null | grep -oE ':(8000|8001)\b' | sort -u | tr '\n' ' ' | sed 's/  */ /g')"

{
  echo "方式: $METHOD"
  echo "新 PID: ${NEW_PID:-未起来}  状态: ${STATE:-?}  起于: $(unit_show ActiveEnterTimestamp)"
  echo "监听端口: ${PORTS:-无}"
  if [ "${DABAI_NO_RECHECK:-0}" = "1" ]; then
    # reload_check 反向调本脚本体检时会设它 —— 两边互相调用会转不出来
    echo "--- 核心文件生效检查：已跳过（DABAI_NO_RECHECK=1，防递归）---"
  else
    echo "--- 核心文件生效检查（进程启动时间 vs 核心文件 mtime）---"
    "$PY" "$ROOT/tools/reload_check.py" 2>&1 | tail -20
  fi
  echo "--- 启动日志末尾（journal）---"
  journalctl -u "$UNIT" -n 12 --no-pager 2>/dev/null | tail -12
} >> "$REPORT" 2>&1

cat "$REPORT"
[ -n "$NEW_PID" ] && [ "$NEW_PID" != 0 ] && [ "$STATE" = "active" ]
