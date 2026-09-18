#!/usr/bin/env bash
# 安装「提交前密钥防线」。
#
#   默认         安装 pre-commit 钩子 + 生成脱敏模板
#   --check      只体检，不改动任何东西（退出码 1 = 有问题）
#   --check --deep  体检里加上全历史扫描（慢，几十秒）
#   --uninstall  移除钩子
#
# 设计原则：任何一步失败都不留下半成品；体检项与安装项一一对应。
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(git -C "$HERE" rev-parse --show-toplevel 2>/dev/null || echo "$HERE")"
SCANNER="$ROOT/deploy/gitguard/secretscan.py"
RULES="$ROOT/deploy/gitguard/rules_regress.py"
SRC_HOOK="$HERE/pre-commit"

SECRET_FILES=(settings.json codex_config.json stt_config.json tts_config.json cards.json character_cards.json)
IGNORE_NEED=(/settings.json /codex_config.json /stt_config.json /tts_config.json /cards.json /character_cards.json /nodes.json)

C_OK=$'\033[1;32m'; C_NO=$'\033[1;31m'; C_WN=$'\033[1;33m'; C_HD=$'\033[1;36m'; C_Z=$'\033[0m'
ok() { printf '  %s✓%s %s\n' "$C_OK" "$C_Z" "$1"; }
no() { printf '  %s✗%s %s\n' "$C_NO" "$C_Z" "$1"; }
wn() { printf '  %s!%s %s\n' "$C_WN" "$C_Z" "$1"; }
hd() { printf '\n%s▸ %s%s\n' "$C_HD" "$1" "$C_Z"; }

MODE="install"; DEEP=0
for a in "$@"; do
  case "$a" in
    --check)     MODE="check" ;;
    --uninstall) MODE="uninstall" ;;
    --deep)      DEEP=1 ;;
    *) echo "未知参数：$a"; exit 2 ;;
  esac
done

# ---------- 定位钩子目录（尊重 core.hooksPath）----------
resolve_hook() {
  local hp
  hp="$(git -C "$ROOT" config --get core.hooksPath 2>/dev/null || true)"
  if [ -n "$hp" ]; then
    case "$hp" in
      /*) HOOK_DIR="$hp" ;;
      *)  HOOK_DIR="$ROOT/$hp" ;;
    esac
  else
    HOOK_DIR="$ROOT/.git/hooks"
  fi
  HOOK="$HOOK_DIR/pre-commit"
}

# ==================== 体检 ====================
run_check() {
  local bad=0
  hd "体检：密钥防线"

  if [ -f "$SCANNER" ]; then ok "扫描器存在（secretscan.py）"
  else no "扫描器缺失：$SCANNER"; bad=1; fi

  resolve_hook
  if [ -x "$HOOK" ] && grep -q secretscan "$HOOK" 2>/dev/null; then
    ok "pre-commit 钩子已装（$HOOK）"
  else
    no "pre-commit 钩子未装或不是本套钩子（$HOOK）"; bad=1
  fi

  # .gitignore 覆盖检查
  # 注意：IGNORE_NEED 里是「模式」（带前导 /），check-ignore 要的是「路径」。
  # 直接把 /settings.json 当路径传进去会被当成绝对路径 → 永远不匹配，
  # 于是体检恒报「缺 7 条规则」。必须剥掉前导斜杠。
  local miss=0 p
  for p in "${IGNORE_NEED[@]}"; do
    git -C "$ROOT" check-ignore -q "${p#/}" 2>/dev/null || { miss=$((miss + 1)); }
  done
  if [ "$miss" -eq 0 ]; then ok ".gitignore 覆盖全部含密钥文件（${#IGNORE_NEED[@]} 条）"
  else no ".gitignore 缺 $miss 条规则（含密钥文件可能被误提交）"; bad=1; fi

  # 含密钥文件不得被跟踪
  local tracked=0 f
  for f in "${SECRET_FILES[@]}"; do
    if git -C "$ROOT" ls-files --error-unmatch "$f" >/dev/null 2>&1; then
      tracked=$((tracked + 1)); wn "仍被 git 跟踪：$f"
    fi
  done
  if [ "$tracked" -eq 0 ]; then ok "含密钥文件均已停止跟踪"
  else no "$tracked 个含密钥文件仍在版本控制里"; bad=1; fi

  # 脱敏模板
  local ex=0
  for f in "${SECRET_FILES[@]}"; do
    [ -f "$ROOT/${f%.json}.example.json" ] && ex=$((ex + 1))
  done
  if [ "$ex" -eq "${#SECRET_FILES[@]}" ]; then ok "脱敏模板齐全（$ex 个 *.example.json）"
  else wn "脱敏模板只有 $ex/${#SECRET_FILES[@]} 个，可跑 install 生成"; fi

  # 被跟踪文件里是否还有密钥
  local hit
  hit="$(cd "$ROOT" && git ls-files -z | xargs -0 -r python3 "$SCANNER" --files 2>/dev/null | grep -c '^   ' || true)"
  if [ "${hit:-0}" -eq 0 ]; then ok "被跟踪文件里无密钥"
  else no "被跟踪文件里仍有 ${hit} 处疑似密钥 —— 跑：python3 deploy/gitguard/secretscan.py --files \$(git ls-files)"; bad=1; fi

  # 钩子是否真能拦住（正向功能测试，而不是只看文件在不在）
  if [ -x "$HOOK" ]; then
    local tmpf="$ROOT/.gitguard-selftest.tmp"
    # 假密钥在运行时拼出来，源码里不留任何「像密钥」的字面量 ——
    # 否则每次扫历史都会多一条噪音，久而久之就没人认真看扫描结果了。
    local fake="sk-selftest$(printf '0%.0s' $(seq 1 25))"
    printf 'api_key = "%s"\n' "$fake" > "$tmpf"
    if (cd "$ROOT" && git add -f "$tmpf" >/dev/null 2>&1); then
      if (cd "$ROOT" && python3 "$SCANNER" --staged >/dev/null 2>&1); then
        no "钩子功能测试失败：造了个假密钥却没被扫出来"
        bad=1
      else
        ok "钩子功能测试通过（假密钥被拦截）"
      fi
      git -C "$ROOT" rm --cached -q --force "$tmpf" >/dev/null 2>&1 || true
    else
      wn "钩子功能测试跳过（无法暂存临时文件）"
    fi
    rm -f "$tmpf"
  fi

  # 判据回归：真实项目里踩过的误报 + 必须仍能抓到的真密钥。
  # 放宽判据很容易，顺手把真密钥一起放过就麻烦了 —— 所以固定跑这个。
  if [ -f "$RULES" ]; then
    local ro rc
    set +e
    ro="$(python3 "$RULES" 2>&1)"; rc=$?
    set -e
    if [ "$rc" -eq 0 ]; then
      ok "判据回归通过（$(printf '%s' "$ro" | tail -1)）"
    else
      no "判据回归失败：$(printf '%s' "$ro" | grep -E '^  FAIL' | head -5 | tr '\n' ';')"
      bad=1
    fi
  fi

  if [ "$DEEP" -eq 1 ]; then
    hd "深度：全历史扫描"
    local n
    n="$(python3 "$SCANNER" --history 2>/dev/null | grep -c '^   ' || true)"
    if [ "${n:-0}" -eq 0 ]; then ok "git 历史中无密钥"
    else no "git 历史中仍有 ${n} 处疑似密钥（未清理，push 前必须处理）"; bad=1; fi
  fi

  return "$bad"
}

# ==================== 安装 ====================
run_install() {
  hd "安装密钥防线"
  resolve_hook
  mkdir -p "$HOOK_DIR"
  # 先备份已有钩子，避免覆盖用户自己的
  if [ -f "$HOOK" ] && ! grep -q secretscan "$HOOK" 2>/dev/null; then
    cp -p "$HOOK" "$HOOK.bak-$(date +%Y%m%d-%H%M%S)"
    wn "已有其它 pre-commit，已备份"
  fi
  install -m 0755 "$SRC_HOOK" "$HOOK"
  ok "pre-commit 钩子 → $HOOK"

  if [ ! -f "$ROOT/.gitignore" ]; then
    : > "$ROOT/.gitignore"; wn ".gitignore 原本不存在，已新建"
  fi
  local added=0 p
  for p in "${IGNORE_NEED[@]}"; do
    # 先 tr 掉 CR：.gitignore 里历史遗留的 CRLF 行会让 grep -qxF 永远不匹配，
    # 于是每跑一次 install 就重复追加一遍同样的规则。
    if ! tr -d '\r' < "$ROOT/.gitignore" | grep -qxF "$p"; then
      printf '%s\n' "$p" >> "$ROOT/.gitignore"; added=$((added + 1))
    fi
  done
  if [ "$added" -gt 0 ]; then ok ".gitignore 补了 $added 条规则"
  else ok ".gitignore 规则已齐全"; fi

  hd "生成脱敏模板"
  python3 "$HERE/make_examples.py" "$ROOT"

  echo
  ok "完成。提交时钩子会自动扫描暂存内容。"
  echo "     绕过：git commit --no-verify（慎用）"
  echo "     误报：行尾加 \`allowlist secret\`，或写进 .gitguard-allow"
}

run_uninstall() {
  hd "卸载"
  resolve_hook
  if [ -f "$HOOK" ] && grep -q secretscan "$HOOK" 2>/dev/null; then
    rm -f "$HOOK"; ok "已移除 $HOOK"
  else
    wn "未发现本套钩子，无需卸载"
  fi
}

case "$MODE" in
  check)     run_check && { echo; ok "体检通过"; } || { echo; no "体检未通过"; exit 1; } ;;
  install)   run_install ;;
  uninstall) run_uninstall ;;
esac
