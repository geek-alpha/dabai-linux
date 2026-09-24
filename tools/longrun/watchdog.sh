#!/bin/sh
# 长跑引擎心跳看门狗：进程活着但不再推进（心跳过期）→ 强制重启。
# 设计依据：systemd Restart=always 只能救「进程死了」，救不了「进程卡住」。
# 心跳由 runner.py 每轮开始时 touch（长活期间每 60s 刷一次），卡住的循环不写心跳，于是被抓出来。
set -u

RUN=${LONGRUN_RUN_DIR:-/home/wxf/dabai/data/longrun}
HB="$RUN/heartbeat"
MAX_AGE=${LONGRUN_MAX_AGE:-1800}        # 30 分钟没心跳 = 卡死
UNIT=${LONGRUN_UNIT:-dabai-longrun.service}

# 主人拉了急停闸，就不许自作主张拉起来
if [ -f "$RUN/STOP" ]; then
    echo "急停文件存在（$RUN/STOP），看门狗不干预"
    exit 0
fi

# 孤儿清扫：runner 被强杀时 agent 子进程会被 init 收养（ppid=1）继续跑、继续烧 token。
# 只清 ppid=1 的，绝不碰活着的 runner 正在用的子进程。
sweep_orphans() {
    for p in $(pgrep -f "dabai_cli.py .*longrun_" 2>/dev/null); do
        ppid=$(ps -o ppid= -p "$p" 2>/dev/null | tr -d ' ')
        if [ "$ppid" = "1" ]; then
            echo "清理孤儿 agent 进程 $p（父进程已死）"
            kill -9 "$p" 2>/dev/null
        fi
    done
}

if ! systemctl --user is-active --quiet "$UNIT"; then
    echo "$UNIT 未运行 → 拉起"
    sweep_orphans
    systemctl --user start "$UNIT"
    exit 0
fi

age=999999
if [ -f "$HB" ]; then
    age=$(( $(date +%s) - $(stat -c %Y "$HB") ))
fi

if [ "$age" -gt "$MAX_AGE" ]; then
    echo "心跳过期 ${age}s（阈值 ${MAX_AGE}s）→ 重启 $UNIT"
    systemctl --user restart "$UNIT"
    sweep_orphans
else
    echo "存活，心跳 ${age}s 前（阈值 ${MAX_AGE}s）"
fi
