#!/usr/bin/env bash
# 大白 Linux / macOS 启动脚本（与 dabai.bat 等价）
#
# 用法：
#   ./dabai.sh              # 启动 server.py
#   ./dabai.sh --setup      # 一键：建 venv + 装依赖 + 自检 + 启动（首次用这条）
#   ./dabai.sh --check      # 只做环境自检，不启动
#   ./dabai.sh --diag       # 打印环境诊断（起不来 / 卡住时先跑这条）
#   ./dabai.sh --deps       # 依赖审计：启动路径依赖 vs 清单，标出漏网 / 冗余
#   ./dabai.sh --deps --export              # 一键导出本机实际依赖（带版本）
#   ./dabai.sh --deps --freeze -o lock.txt  # 一键导出完整环境锁定（pip freeze）
#
# 环境变量：
#   DABAI_PYTHON  指定解释器（默认：venv/bin/python → python3）
#   DABAI_PORT    覆盖端口（默认沿用 settings.json 配置）
set -euo pipefail

cd "$(dirname "$(readlink -f "$0" 2>/dev/null || echo "$0")")"
ROOT="$(pwd)"

# ---- 参数分流：--setup / --check / --diag / --help 自己处理，其余原样透传给 server.py ----
SETUP=0
CHECKONLY=0
DIAG=0
DEPS=0
PASS_ARGS=()
for a in "$@"; do
  case "$a" in
    --setup) SETUP=1 ;;
    --check) CHECKONLY=1 ;;
    --diag) DIAG=1 ;;
    --deps) DEPS=1 ;;
    -h|--help) sed -n '2,13p' "$0"; exit 0 ;;
    *) PASS_ARGS+=("$a") ;;
  esac
done
if [ ${#PASS_ARGS[@]} -gt 0 ]; then set -- "${PASS_ARGS[@]}"; else set --; fi

# ---- 首次运行：没 venv 就自动装（直跑即可，不必先手敲 --setup）----
# 例外一：显式设了 DABAI_PYTHON —— 那是刻意不用 venv 的人，强建几百 MB 环境属于越界。
# 例外二：--check / --diag / --deps 是只读诊断，不该顺手下依赖。
if [ "$SETUP" = "0" ] && [ "$CHECKONLY" = "0" ] && [ "$DIAG" = "0" ] && [ "$DEPS" = "0" ]; then
  if [ ! -x "$ROOT/venv/bin/python" ] && [ -z "${DABAI_PYTHON:-}" ]; then
    SETUP=1
    echo "== 首次运行：未找到虚拟环境，自动开始安装（等价于 ./dabai.sh --setup）=="
  fi
fi

# ---- 一键引导：venv 不在、或启动必需依赖不全，都补装（幂等）----
# 只看 venv/bin/python 存在与否是不够的：上次装到一半（磁盘满 / 网络断）会留下一个
# 半成品 venv，再跑 --setup 会直接跳过，问题拖到启动时才炸。
if [ "$SETUP" = "1" ]; then
  # 依赖清单只有 tools/check_deps.py 一处（与 requirements-core.txt 同源），这里不内联抄。
  if [ ! -x "$ROOT/venv/bin/python" ] || \
     ! "$ROOT/venv/bin/python" "$ROOT/tools/check_deps.py" --gate >/dev/null 2>&1; then
    echo "== 环境缺失或依赖不全：创建虚拟环境并安装依赖 =="
    "$ROOT/tools/linux_setup.sh" --venv || {
      echo "✗ 环境创建失败。若缺系统包，先跑：$ROOT/tools/linux_setup.sh --install-system" >&2
      exit 1
    }
  fi
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

# ---- 依赖审计 / 导出：--deps 后的参数原样透传给 deps_audit.py ----
# 无参数 = 报告；--export = 代码 import 反推的直接依赖；--freeze = pip freeze 全量。
if [ "$DEPS" = "1" ]; then
  exec "$PY" "$ROOT/tools/deps_audit.py" ${PASS_ARGS[@]+"${PASS_ARGS[@]}"}
fi

# ---- Node.js：前端 .ts 实时转译的硬依赖 ----
# server.py 的 TSTranspileMiddleware 用 Node 的 module.stripTypeScriptTypes 把 /static
# 下的 .ts 实时转成 JS。缺 Node 或版本过低时服务照常启动、网页也打得开，但浏览器把 TS
# 源码当 JS 跑 → 模块图整个崩 → 页面永远停在「连接中…」，而服务端一行错都不报 ——
# 最容易被当成「这程序坏了」的故障。这里只警告不退出：有人只跑 CLI 不开网页。
if [ "$CHECKONLY" = "0" ] && [ "$DIAG" = "0" ] && [ "$DEPS" = "0" ]; then
  NODE_MSG="$("$PY" "$ROOT/tools/install_node.py" --check 2>&1)" || {
    echo "⚠ Node.js 不可用（前端 .ts 转译依赖它）—— 网页会永远停在「连接中…」"
    printf '%s\n' "$NODE_MSG" | sed 's/^/  /'
  }
fi

# ---- 环境自检（--check 只自检不启动）----
if [ "$CHECKONLY" = "1" ]; then
  exec "$PY" "$ROOT/tools/selfcheck.py"
fi

# ---- 环境诊断：起不来 / 卡住时先跑 --diag，把输出整段贴出来即可定位 ----
if [ "$DIAG" = "1" ]; then
  echo "===== 大白环境诊断 ====="
  echo "工作目录 : $ROOT"
  echo "系统     : $(uname -srm)"
  echo
  echo "[解释器]"
  echo "  PY : $PY"
  "$PY" -c "import sys;print('  版本:', sys.version.split()[0])" 2>&1 | sed 's/^/  /'
  echo "  PATH 里的 python3：$(command -v python3 || echo '未找到')"
  echo
  echo "[依赖]"
  "$PY" "$ROOT/tools/check_deps.py" || true
  echo
  echo "[端口 8001]"
  ss -ltnp 2>/dev/null | grep ":8001" || echo "  没有进程监听 8001"
  echo
  echo "[外部工具]"
  for t in ffmpeg git rg node; do
    printf '  %-8s %s\n' "$t" "$(command -v "$t" || echo '未找到')"
  done
  echo
  echo "[Node.js（前端 .ts 转译；缺了网页永远停在「连接中…」）]"
  "$PY" "$ROOT/tools/install_node.py" --check || true
  echo
  echo "[服务托管]"
  systemctl is-active myservice.service 2>/dev/null || echo "  未由 systemd 托管"
  echo "  日志：journalctl -u myservice.service -n 50 --no-pager"
  echo "=============================="
  exit 0
fi

# 依赖清单只在 tools/check_deps.py 里一份（与 requirements-core.txt 同源）。
# 以前这里内联抄了 5 个包，于是 uvloop / httptools / python-multipart / websockets
# 四个「缺了就崩或就哑」的包全在盲区，问题留到启动才暴露。
if ! "$PY" "$ROOT/tools/check_deps.py"; then
  echo "  或一键补齐：$ROOT/dabai.sh --setup" >&2
  exit 1
fi

export PYTHONUTF8=1
export PYTHONIOENCODING=utf-8
exec "$PY" server.py "$@"
