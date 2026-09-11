#!/usr/bin/env bash
# 大白 Linux / macOS 启动脚本（与 dabai.bat 等价）
#
# 用法：
#   ./dabai.sh              # 启动 server.py
#   ./dabai.sh --setup      # 一键：建 venv + 装依赖 + 自检 + 启动（首次用这条）
#   ./dabai.sh --check      # 只做环境自检，不启动
#
# 环境变量：
#   DABAI_PYTHON  指定解释器（默认：venv/bin/python → python3）
#   DABAI_PORT    覆盖端口（默认沿用 settings.json 配置）
set -euo pipefail

cd "$(dirname "$(readlink -f "$0" 2>/dev/null || echo "$0")")"
ROOT="$(pwd)"

# ---- 参数分流：--setup / --help 自己处理，其余原样透传给 server.py ----
SETUP=0
PASS_ARGS=()
for a in "$@"; do
  case "$a" in
    --setup) SETUP=1 ;;
    -h|--help) sed -n '2,9p' "$0"; exit 0 ;;
    *) PASS_ARGS+=("$a") ;;
  esac
done
if [ ${#PASS_ARGS[@]} -gt 0 ]; then set -- "${PASS_ARGS[@]}"; else set --; fi

# ---- 一键引导：venv 不在就先建环境（幂等，已存在则跳过）----
if [ "$SETUP" = "1" ] && [ ! -x "$ROOT/venv/bin/python" ]; then
  echo "== 首次运行：创建虚拟环境并安装依赖 =="
  "$ROOT/tools/linux_setup.sh" --venv || {
    echo "✗ 环境创建失败。若缺系统包，先跑：$ROOT/tools/linux_setup.sh --install-system" >&2
    exit 1
  }
fi

# ---- 选解释器：优先项目内 venv，其次 PATH ----
PY="${DABAI_PYTHON:-}"
if [ -z "$PY" ]; then
  if [ -x "$ROOT/venv/bin/python" ]; then
    PY="$ROOT/venv/bin/python"
  elif [ -x "$ROOT/.venv/bin/python" ]; then
    PY="$ROOT/.venv/bin/python"
  elif command -v python3 >/dev/null 2>&1; then
    PY="$(command -v python3)"
  else
    echo "✗ 找不到 python3，请先安装 Python 3.10+ 或设置 DABAI_PYTHON" >&2
    exit 1
  fi
fi

# ---- 依赖自检（缺关键包时给出可执行命令，而不是让 server 崩在 import）----
if [ "${1:-}" = "--check" ]; then
  exec "$PY" "$ROOT/tools/linux_selfcheck.py"
fi

MISSING="$("$PY" - <<'EOF' 2>/dev/null || true
import importlib.util as u
need = ["fastapi", "uvicorn", "aiohttp", "requests", "starlette"]
print(" ".join(m for m in need if u.find_spec(m) is None))
EOF
)"
if [ -n "$MISSING" ]; then
  echo "✗ 缺少依赖：$MISSING"
  echo "  安装：$PY -m pip install -r requirements-linux.txt"
  echo "  或先自检：$ROOT/dabai.sh --check"
  exit 1
fi

export PYTHONUTF8=1
export PYTHONIOENCODING=utf-8
exec "$PY" server.py "$@"
