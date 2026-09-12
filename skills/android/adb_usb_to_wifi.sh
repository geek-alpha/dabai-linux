#!/usr/bin/env bash
# 手机 USB 一插入就自动切成无线 ADB（adb tcpip 5555 + connect）。
# 由 99-android-adb-wifi.rules 触发，解决「手机重启后 5555 失效」这个唯一断点：
# 用户只要插一次线，之后又是无线。
set -uo pipefail

DATA=/home/wxf/dabai/data/android
LOG="$DATA/keepalive.log"
HOSTS="$DATA/wifi_host.txt"
ADB="$(command -v adb)"

mkdir -p "$DATA"
log() { printf '[%s] %s\n' "$(date '+%F %T')" "$1" >> "$LOG"; }

# USB 枚举到 adb 可用有延迟，轮询等设备（最多 45 秒）
usb=""
for _ in $(seq 1 45); do
    usb="$("$ADB" devices 2>/dev/null | awk 'NR>1 && $2=="device" && $1 !~ /:/ {print $1; exit}')"
    [ -n "$usb" ] && break
    sleep 1
done

if [ -z "$usb" ]; then
    log "等待 45 秒未见 adb 设备（USB 未插，或手机端 USB 调试没开）"
    exit 1
fi

"$ADB" -s "$usb" tcpip 5555 >/dev/null 2>&1
sleep 2

ip="$("$ADB" -s "$usb" shell ip -f inet addr show wlan0 2>/dev/null \
      | grep -o 'inet [0-9.]*' | awk '{print $2}' | head -1)"

if [ -z "$ip" ]; then
    log "USB 已连上 $usb，但读不到 wlan0 IP（手机 WiFi 没连？）"
    exit 1
fi

host="$ip:5555"
if "$ADB" connect "$host" 2>&1 | grep -qiE 'connected|already'; then
    printf '%s\n' "$host" > "$HOSTS"
    log "USB 接入 → 已自动转无线 $host"
    echo 0 > "$DATA/keepalive.fails"
    rm -f "$DATA/NEED_ATTENTION"
else
    log "USB 接入 → 转无线失败（$host 连不上）"
    exit 1
fi
