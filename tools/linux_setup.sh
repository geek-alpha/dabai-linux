#!/usr/bin/env bash
# 大白 Linux 一键环境准备（Debian/Ubuntu 为主，兼顾 Fedora/Arch）
#
#   ./tools/linux_setup.sh                 # 只检查并打印需要执行的系统命令（默认，安全）
#   ./tools/linux_setup.sh --install-system # 用 sudo 安装系统包（会打印命令后执行）
#   ./tools/linux_setup.sh --venv          # 建 venv 并安装 Python 依赖
#   ./tools/linux_setup.sh --all           # 系统包 + venv + 自检
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PY="${DABAI_PYTHON:-python3}"
SYS_PKGS=(python3-venv python3-dev ffmpeg ripgrep)
DO_SYSTEM=0
DO_VENV=0
for a in "$@"; do
  case "$a" in
    --install-system) DO_SYSTEM=1 ;;
    --venv) DO_VENV=1 ;;
    --all) DO_SYSTEM=1; DO_VENV=1 ;;
    -h|--help) sed -n '2,9p' "$0"; exit 0 ;;
  esac
done

# ---- 识别包管理器 ----
if command -v apt-get >/dev/null 2>&1; then
  PM="apt-get"; INSTALL="sudo apt-get install -y"
elif command -v dnf >/dev/null 2>&1; then
  PM="dnf"; INSTALL="sudo dnf install -y"
elif command -v pacman >/dev/null 2>&1; then
  PM="pacman"; INSTALL="sudo pacman -S --noconfirm"
elif command -v apk >/dev/null 2>&1; then
  PM="apk"; INSTALL="sudo apk add"
else
  PM=""; INSTALL=""
fi

echo "== 大白 Linux 环境准备 =="
echo "项目根：$ROOT"
echo "包管理器：${PM:-未识别（请手动安装依赖）}"
echo

missing=()
for p in "${SYS_PKGS[@]}"; do
  case "$p" in
    ffmpeg|ripgrep) command -v "${p/ripgrep/rg}" >/dev/null 2>&1 || missing+=("$p") ;;
    python3-venv) "$PY" -c "import venv" >/dev/null 2>&1 || missing+=("$p") ;;
    python3-dev) "$PY" -c "import sysconfig; print(sysconfig.get_paths()['include'])" >/dev/null 2>&1 || missing+=("$p") ;;
  esac
done

if [ ${#missing[@]} -gt 0 ]; then
  echo "缺少系统包：${missing[*]}"
  if [ -n "$INSTALL" ]; then
    echo "  建议执行：$INSTALL ${missing[*]}"
    if [ "$DO_SYSTEM" = "1" ]; then
      echo "  正在执行…"
      $INSTALL "${missing[@]}"
    fi
  fi
else
  echo "系统包齐全 ✓"
fi

# ---- venv + Python 依赖 ----
if [ "$DO_VENV" = "1" ]; then
  if [ ! -d "$ROOT/venv" ]; then
    echo "创建虚拟环境 venv/ …"
    "$PY" -m venv "$ROOT/venv"
  fi
  VP="$ROOT/venv/bin/python"
  echo "升级 pip…"
  "$VP" -m pip install --upgrade pip -q
  echo "安装 requirements-linux.txt（失败的包会被跳过并汇总）…"
  failed=()
  while IFS= read -r line; do
    pkg="${line%%#*}"
    pkg="$(echo "$pkg" | xargs)"
    [ -z "$pkg" ] && continue
    "$VP" -m pip install -q "$pkg" || failed+=("$pkg")
  done < requirements-linux.txt
  if [ ${#failed[@]} -gt 0 ]; then
    echo "以下包安装失败（多为需要编译或平台不支持，通常不影响核心）："
    printf '  - %s\n' "${failed[@]}"
  fi
  echo "venv 就绪：$VP"
fi

echo
echo "== 自检 =="
VP="$ROOT/venv/bin/python"
[ -x "$VP" ] || VP="$PY"
exec "$VP" "$ROOT/tools/linux_selfcheck.py"
