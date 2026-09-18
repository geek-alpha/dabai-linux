#!/usr/bin/env bash
# 安全发布闸门：把本仓库推到 GitHub 之前，强制过三道密钥检查。
#
# 背景：2025-06 ~ 2026-09 期间，Windows 侧的 github_auto_push 工具把多个项目的
# settings.json（含明文 API key）自动建仓推送到了公开仓库，泄漏 14 个月。
# 本脚本是那条路径的替代品：先验证、再推送；token 只在运行时注入，不落盘。
#
# 用法：
#   bash deploy/gitguard/safe-push.sh wangxingfen/dabai-linux            # 默认 private
#   bash deploy/gitguard/safe-push.sh wangxingfen/dabai-linux --public
#   bash deploy/gitguard/safe-push.sh wangxingfen/dabai-linux --dry-run  # 只预检
#   bash deploy/gitguard/safe-push.sh wangxingfen/dabai-linux --proxy http://127.0.0.1:7890
#
# token 来源（按优先级）：
#   1) 环境变量 GITHUB_TOKEN
#   2) /etc/dabai/secrets.env 里的 GITHUB_TOKEN
# 注入方式：git -c http.https://github.com/.extraheader=Authorization: Basic ...
#   token 不进命令行参数、不进 remote URL、不进 .git/config。
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$ROOT" || { echo "无法进入仓库根目录"; exit 1; }

TARGET=""
VIS="private"
DRY=0
PROXY="${https_proxy:-${HTTPS_PROXY:-}}"

while [ $# -gt 0 ]; do
  case "$1" in
    --public)  VIS="public" ;;
    --private) VIS="private" ;;
    --dry-run) DRY=1 ;;
    --proxy)   shift; PROXY="${1:-}" ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    -*)        echo "未知参数：$1"; exit 2 ;;
    *)         TARGET="$1" ;;
  esac
  shift
done

ok()  { echo "  ✓ $*"; }
die() { echo; echo "✗ 中止：$*"; exit 1; }

echo "=== 安全发布闸门（仓库：$ROOT）==="
echo

echo "① 工作区必须干净"
if [ -n "$(git status --porcelain)" ]; then
  git status --short | head -20 | sed 's/^/    /'
  die "有未提交改动 —— 先提交或 stash，否则校验的不是待发布内容"
fi
ok "干净"

echo
echo "② 密钥扫描（三道：暂存区 / 跟踪文件 / 全历史）"
for m in --staged --tree --history; do
  timeout 900 python3 deploy/gitguard/secretscan.py "$m" >/tmp/dabai-safepush-scan.out 2>&1
  ec=$?
  if [ "$ec" = 0 ]; then
    ok "$m 通过"
  else
    sed 's/^/    /' /tmp/dabai-safepush-scan.out
    # 超时（124）不是「发现密钥」：全历史扫描本机约 180s，仓库越大越慢，
    # 掐在 180 会让闸门把「太慢」报成「不安全」，而且被 kill 时输出没 flush，
    # 报错一片空白，看着像抓到了密钥却不说在哪。
    [ "$ec" = 124 ] && die "$m 扫描超时（900s）—— 是闸门太慢，不是发现密钥"
    die "$m 未通过"
  fi
done

echo
echo "③ 独立验证（不依赖自家扫描器）"
HITS="$(git grep -nIE 'sk-[A-Za-z0-9]{20,}|ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY' HEAD -- . 2>/dev/null \
        | grep -v 'deploy/gitguard/secretscan.py' || true)"
if [ -n "$HITS" ]; then
  echo "$HITS" | head -10 | sed 's/^/    /'
  die "HEAD 里发现密钥形态"
fi
ok "HEAD 无密钥形态（git grep 独立复核）"

for f in settings.json codex_config.json stt_config.json tts_config.json \
         cards.json character_cards.json nodes.json key.pem cert.pem data/mixamo_cookies.json; do
  if git ls-files --error-unmatch "$f" >/dev/null 2>&1; then
    die "硬雷文件被跟踪：$f"
  fi
done
ok "10 类硬雷文件均未被跟踪"

echo
if [ -z "$TARGET" ]; then
  die "未指定目标仓库，例：bash deploy/gitguard/safe-push.sh wangxingfen/dabai-linux"
fi
echo "④ 目标：$TARGET（$VIS）"

echo
echo "⑤ 取 token"
TOKEN="${GITHUB_TOKEN:-}"
if [ -z "$TOKEN" ] && [ -r /etc/dabai/secrets.env ]; then
  TOKEN="$(grep -E '^GITHUB_TOKEN=' /etc/dabai/secrets.env | head -1 | cut -d= -f2- || true)"
fi
if [ -z "$TOKEN" ] && [ -r "$HOME/.config/dabai/secrets.env" ]; then
  TOKEN="$(grep -E '^GITHUB_TOKEN=' "$HOME/.config/dabai/secrets.env" | head -1 | cut -d= -f2- || true)"
fi
if [ -z "$TOKEN" ]; then
  echo "  ✗ 没找到 GITHUB_TOKEN（按序找：环境变量 → /etc/dabai/secrets.env → ~/.config/dabai/secrets.env）"
  echo "    系统级持久化： sudo dabai-secrets set GITHUB_TOKEN ghp_xxx"
  echo "    用户级持久化： umask 077 && printf 'GITHUB_TOKEN=%s\\n' ghp_xxx > ~/.config/dabai/secrets.env"
  echo "    或临时一次： export GITHUB_TOKEN=ghp_xxx"
  if [ "$DRY" != "1" ]; then die "缺少 token，无法建仓/推送"; fi
else
  ok "已取到 token（${TOKEN:0:4}***，不落盘、不进命令行）"
fi

if [ "$DRY" = "1" ]; then
  echo
  echo "✓ 预检全通过（--dry-run：未建仓、未推送）"
  echo "  正式推送：bash deploy/gitguard/safe-push.sh $TARGET --$VIS"
  exit 0
fi

echo
echo "⑥ 确保远程仓库存在"
DABAI_TARGET="$TARGET" DABAI_VIS="$VIS" DABAI_TOKEN="$TOKEN" python3 - <<'PY'
import json, os, sys, urllib.request, urllib.error

target = os.environ["DABAI_TARGET"]
want_public = os.environ["DABAI_VIS"] == "public"
tok = os.environ["DABAI_TOKEN"]


def call(url, data=None, method=None):
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, method=method, headers={
        "Authorization": "Bearer " + tok,
        "Accept": "application/vnd.github+json",
        "User-Agent": "dabai-safe-push",
    })
    if body:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw or "{}")
        except json.JSONDecodeError:
            return e.code, {"message": raw[:200]}
    except Exception as e:  # 网络问题
        return 0, {"message": str(e)}


st, d = call("https://api.github.com/repos/" + target)
if st == 200:
    print("  ✓ 仓库已存在（private=%s，默认分支=%s）" % (d.get("private"), d.get("default_branch")))
    if want_public and d.get("private"):
        print("  ! 你要 public 但现有仓库是 private —— 脚本不改可见性，请手动改")
elif st == 404:
    name = target.split("/", 1)[1]
    st, d = call("https://api.github.com/user/repos", {
        "name": name,
        "private": not want_public,
        "description": "大白 Linux 原生版 —— 树莓派上的 AI 伙伴（已剥离全部密钥）",
        "has_issues": True,
    })
    if st in (200, 201):
        print("  ✓ 已创建 %s（private=%s）" % (d.get("full_name"), d.get("private")))
    else:
        print("  ✗ 创建失败 HTTP=%s %s" % (st, d.get("message")))
        sys.exit(1)
else:
    print("  ✗ 查询失败 HTTP=%s %s" % (st, d.get("message")))
    sys.exit(1)
PY
if [ $? -ne 0 ]; then die "仓库准备失败"; fi

echo
echo "⑦ 推送"
git remote remove origin >/dev/null 2>&1
git remote add origin "https://github.com/$TARGET.git"
BRANCH="$(git rev-parse --abbrev-ref HEAD)"
ENC="$(printf 'x-access-token:%s' "$TOKEN" | base64 -w0)"
GIT_ARGS=(-c "http.https://github.com/.extraheader=Authorization: Basic $ENC")
if [ -n "$PROXY" ]; then
  GIT_ARGS+=(-c "http.proxy=$PROXY")
  echo "  经代理：$PROXY"
fi
if ! git "${GIT_ARGS[@]}" push -u origin "$BRANCH" 2>&1 | sed 's/^/    /'; then
  die "推送失败"
fi

echo
echo "⑧ 推送后自检"
DABAI_TARGET="$TARGET" DABAI_TOKEN="$TOKEN" DABAI_SHA="$(git rev-parse HEAD)" \
DABAI_BRANCH="$BRANCH" python3 - <<'PY'
import json, os, sys, urllib.request, urllib.error

target = os.environ["DABAI_TARGET"]
tok = os.environ["DABAI_TOKEN"]
sha = os.environ["DABAI_SHA"]
branch = os.environ["DABAI_BRANCH"]


def get(url):
    req = urllib.request.Request(url, headers={
        "Authorization": "Bearer " + tok,
        "Accept": "application/vnd.github+json",
        "User-Agent": "dabai-safe-push",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, {}


rc = 0
st, d = get("https://api.github.com/repos/" + target)
if st == 200:
    print("  ✓ 仓库可见性：%s" % ("public ⚠ 任何人可见" if not d.get("private") else "private"))
    print("  ✓ 体积：%s KB" % d.get("size"))
else:
    print("  ✗ 查仓库失败 HTTP=%s" % st); rc = 1

st, d = get("https://api.github.com/repos/%s/git/ref/heads/%s" % (target, branch))
remote_sha = (d.get("object") or {}).get("sha")
if remote_sha == sha:
    print("  ✓ 远端 %s = 本地 HEAD（%s）" % (branch, sha[:8]))
else:
    print("  ✗ 远端 %s=%s 与本地 %s 不一致" % (branch, str(remote_sha)[:8], sha[:8])); rc = 1
sys.exit(rc)
PY
SELFCHECK=$?

echo
echo "  远端 URL 里不含凭据：$(git remote get-url origin | grep -q '@' && echo '✗ 含凭据' || echo '✓')"
unset TOKEN

if [ "$SELFCHECK" -ne 0 ]; then
  echo
  echo "✗ 自检未通过 —— 请人工确认后再对外公布地址"
  exit 1
fi

echo
echo "✓ 完成：https://github.com/$TARGET"
echo "  可见性：$VIS"
echo
echo "  ⚠ Windows 侧的 github_auto_push 若仍把本项目目录列为目标，它会按旧路径再推一遍"
echo "    （并可能把 settings.json 带上去）—— 先改它或停掉，再发布地址。"
