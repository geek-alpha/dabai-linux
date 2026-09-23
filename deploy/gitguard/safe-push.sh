#!/usr/bin/env bash
# 安全发布闸门（bash 入口）—— 判据全在 safe_push.py 里，这里只负责找到 Python 并透传。
#
# 为什么留这个包装而不是把 bash 版留着：两套实现必然漂移，而漂移的闸门比没有闸门更
# 危险（你以为查过了）。bash 版在 Windows 上也不保证存在，而大白本身必然有 Python。
#
# 用法与 safe_push.py 完全一致：
#   bash deploy/gitguard/safe-push.sh geek-alpha/dabai-linux --dry-run
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

PY="${DABAI_PYTHON:-}"
if [ -z "$PY" ]; then
  for c in "$HERE/../../venv/bin/python" "$HERE/../../.venv/bin/python" python3 python; do
    if command -v "$c" >/dev/null 2>&1; then PY="$c"; break; fi
  done
fi
if [ -z "$PY" ]; then
  echo "✗ 找不到 Python（试过 venv/bin/python、python3、python；可用 DABAI_PYTHON 指定）" >&2
  exit 127
fi

exec "$PY" "$HERE/safe_push.py" "$@"
