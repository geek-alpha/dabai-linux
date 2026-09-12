#!/usr/bin/env bash
# 手机 ADB 无线连接保活：纯 shell，零 LLM 成本。
# 由 adb-keepalive.timer 每 60 秒驱动；掉线时按 缓存IP → 网段扫描 两级自愈。
set -uo pipefail

DATA=/home/wxf/dabai/data/android
LOG="$DATA/keepalive.log"
FAILS="$DATA/keepalive.fails"
NEED="$DATA/NEED_ATTENTION"
PY=/home/wxf/dabai/venv/bin/python
ADB="$(command -v adb)"
NOTIFY_MAX=3

mkdir -p "$DATA"

log() {
    printf '[%s] %s\n' "$(date '+%F %T')" "$1" >> "$LOG"
    if [ "$(wc -l < "$LOG")" -gt 500 ]; then
        tail -n 300 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
    fi
}

online() {
    "$ADB" devices 2>/dev/null | awk 'NR>1 && $2=="device" {print $1; exit}'
}

serial="$(online)"

if [ -z "$serial" ]; then
    # 掉线：交给技能的自愈逻辑（缓存 IP → 并发扫网段），失败也不抛错
    timeout 90 "$PY" - <<'EOF' >/dev/null 2>&1
import asyncio, importlib.util
spec = importlib.util.spec_from_file_location(
    "andsk", "/home/wxf/dabai/skills/android/skill.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
print(asyncio.run(m.execute("android", {"action": "auto"})))
EOF
    serial="$(online)"
fi

if [ -n "$serial" ]; then
    log "在线 $serial"
    echo 0 > "$FAILS"
    rm -f "$NEED"
    # 手机在线时确保实时层在跑（触摸流 + UI 快照缓存）
    if ! "$PY" /home/wxf/dabai/skills/android/adb_live.py status 2>/dev/null | grep -q 运行中; then
        nohup "$PY" /home/wxf/dabai/skills/android/adb_live.py start >/dev/null 2>&1 &
    fi
    exit 0
fi

n=$(( $(cat "$FAILS" 2>/dev/null || echo 0) + 1 ))
echo "$n" > "$FAILS"
log "掉线（连续第 $n 次）"

# 只在首次越过阈值时打扰用户，避免反复弹窗
if [ "$n" -eq "$NOTIFY_MAX" ] && [ ! -f "$NEED" ]; then
    date '+%F %T' > "$NEED"
    notify-send -u critical -i phone "手机连不上了" \
        "连续 ${n} 次没找回手机。请把手机用 USB 插一下树莓派（我会自动转成无线），或检查手机是否连了同一个 WiFi。" \
        2>/dev/null || true
fi

exit 1
