#!/usr/bin/env python3
"""密钥扫描器 —— 工作区 / 暂存区 / 全历史。

设计目标：既要在提交前拦住真密钥，又不能因为「max_tokens」这种
名字里带 token 的普通配置而天天误报（误报多了钩子就会被 --no-verify 绕过，
等于没有）。

判据分两类：
  1) 值形态：sk- / ghp_ / AKIA / tvly- ... 等有固定前缀的密钥
  2) 名字+长度：字段名按「下划线/驼峰分词」后含 token/key/secret 等词根，
     且值长度 >= MIN_VALUE_LEN。分词很重要 —— 直接子串匹配会把
     max_tokens / long_term_max_tokens 全判成密钥。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys

MIN_VALUE_LEN = 20

# ---------- 判据 1：有固定前缀的密钥形态 ----------
VALUE_PATTERNS = [
    ("OpenAI/DeepSeek/Moonshot 风格", re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}")),
    ("Anthropic", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}")),
    ("GitHub", re.compile(r"\b(?:ghp|gho|ghu|ghs)_[A-Za-z0-9]{20,}\b|\bgithub_pat_[A-Za-z0-9_]{20,}\b")),
    ("AWS Access Key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("Tavily", re.compile(r"\btvly-[A-Za-z0-9]{10,}\b")),
    ("Google API", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("Slack", re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}\b")),
    ("HuggingFace", re.compile(r"\bhf_[A-Za-z0-9]{20,}\b")),
    ("GitLab", re.compile(r"\bglpat-[A-Za-z0-9_\-]{20,}\b")),
    ("JWT", re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}\.")),
    ("私钥文件头", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
]

# ---------- 判据 2：字段名词根 ----------
# 词根判据见下面 name_is_secretish / name_hints_secret
# 这些词根出现时说明是「数量/上限/状态/分组」类配置，不是密钥。
# 靠这个列表把 TOKEN_STATS_KEY / VIDEO_VOLUME_KEY / state_key 这类
# 「名字里带 key/token 的普通常量」挡在外面。
NON_SECRET_SEGMENTS = {
    "max", "min", "limit", "est", "budget", "count", "size",
    "num", "total", "len", "length", "ttl", "expire", "timeout",
    "stats", "stat", "volume", "level", "mode", "sort", "order",
    "index", "group", "name", "label", "tag", "type", "kind",
    "role", "state", "status", "stage", "step", "phase", "bucket",
    "slot", "region", "zone", "area", "page", "offset", "cursor",
    "version", "ver", "rev", "hash", "sha", "commit", "id", "uuid",
}

# 光有 key 段还不够 —— 必须是「密钥性质的 key」。
# TIANAPI_QUIZ_KEY 这种没有限定词，靠下面 Rule C（高熵值）兜底。
SECRET_QUALIFIERS = {
    "api", "access", "secret", "private", "auth", "authenticate",
    "authorization", "signing", "sign", "encryption", "encrypt",
    "client", "consumer", "bearer", "session", "refresh", "master",
    "root", "service", "license", "activation", "subscription",
    "webhook", "app", "application", "developer", "personal",
}

# Rule C 用的宽松名匹配：名字里含这些子串才考虑高熵值
SECRET_NAME_HINTS = ("key", "token", "secret", "pass", "cred", "auth", "api")

# 名字 + 值一起出现的赋值形态（JSON / Python / JS / shell）
NAME_ASSIGN = re.compile(
    r"""["']?([A-Za-z_][A-Za-z0-9_]*)["']?\s*[:=]\s*["']([^"'\n]{""" + str(MIN_VALUE_LEN) + r""",})["']"""
)

# 明显是占位符 / 本机地址，不算泄漏
PLACEHOLDER_HINTS = (
    "your_", "your-", "xxx", "placeholder", "changeme", "example",
    "redacted", "removed", "dummy", "fake", "test_key", "todo",
    "ollama", "localhost", "127.0.0.1", "0.0.0.0", "http://", "https://",
)
# 行内白名单标记（gitleaks 同款约定）
ALLOW_MARKERS = ("allowlist secret", "nosecret", "noqa: secret")

SKIP_EXTS = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pdf", ".zip",
    ".gz", ".tar", ".whl", ".so", ".pyc", ".bin", ".mp3", ".mp4", ".wav",
}
SKIP_DIRS = {".git", "node_modules", "__pycache__", "venv", ".venv", "dist", "build"}


def _segments(name: str) -> list[str]:
    """把字段名切成词根：api_key -> [api, key]；maxTokens -> [max, tokens]。"""
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name).lower()
    return [p for p in re.split(r"[^a-z0-9]+", s) if p]


def name_is_secretish(name: str) -> bool:
    """严格判据：用于 Rule B。"""
    segs = _segments(name)
    if not segs:
        return False
    if any(s in NON_SECRET_SEGMENTS for s in segs):
        return False
    if any(s in SECRET_QUALIFIERS for s in segs):
        return True
    # token / secret / password 这类词根本身就是密钥性质
    if any(s in ("token", "tokens", "secret", "secrets", "password",
                 "passwd", "credential", "credentials") for s in segs):
        return True
    # apikey / accesstoken 这类连写形式
    joined = "".join(segs)
    if any(w in joined for w in ("apikey", "accesskey", "secretkey", "accesstoken")):
        return True
    # 剩下的裸 key 段：必须带密钥限定词（api_key / access_key / …）
    return False


def name_hints_secret(name: str) -> bool:
    """宽松判据：用于 Rule C（高熵值）。只要求名字里含密钥词根，
    且不带 NON_SECRET 词根 —— 这样 sha / commit / data 这类不会误报。"""
    segs = _segments(name)
    if not segs or any(s in NON_SECRET_SEGMENTS for s in segs):
        return False
    low = name.lower()
    return any(h in low for h in SECRET_NAME_HINTS)


def _entropy(s: str) -> float:
    import math
    from collections import Counter
    if not s:
        return 0.0
    n = len(s)
    return -sum((c / n) * math.log2(c / n) for c in Counter(s).values())


# Rule C：看起来就是随机串的长值（覆盖「无固定前缀」的自建 key，
# 例如 TIANAPI_QUIZ_KEY 那种 32 位 hex）。
def is_high_entropy_secret(value: str) -> bool:
    v = value.strip()
    if len(v) < 32 or len(v) > 512:
        return False
    if any(c in _FORBIDDEN_CHARS for c in v):
        return False
    if "${" in v or "$(" in v:
        return False
    if not _SECRET_VALUE_SHAPE.fullmatch(v):
        return False
    if not (any(c.isalpha() for c in v) and any(c.isdigit() for c in v)):
        return False
    return _entropy(v) >= 3.3


def looks_placeholder(value: str) -> bool:
    low = value.lower()
    if not value.strip():
        return True
    if low.startswith(("${", "$(", "\\${", "{{", "<")):
        return True
    return any(h in low for h in PLACEHOLDER_HINTS)


# 值必须「长得像密钥」—— 光看字段名会把 state_key="2|2|20260909" 这种
# 复合 ID 判成密钥。误报多了钩子就会被 --no-verify 绕过，等于没有。
_SECRET_VALUE_SHAPE = re.compile(r"[A-Za-z0-9_\-+/=.~:]+")
_FORBIDDEN_CHARS = set(" \t|,;<>(){}[]\"'")


def looks_like_secret(value: str) -> bool:
    v = value.strip()
    if len(v) < MIN_VALUE_LEN:
        return False
    if any(c in _FORBIDDEN_CHARS for c in v):
        return False
    if "${" in v or "$(" in v:
        return False
    if not _SECRET_VALUE_SHAPE.fullmatch(v):
        return False
    # 真密钥几乎总是字母 + 数字混合；纯字母长串多为标识符/单词
    return any(c.isalpha() for c in v) and any(c.isdigit() for c in v)


def mask(value: str) -> str:
    """只露前 4 个字符 + 长度 —— 报告里绝不出现完整密钥。"""
    v = value.strip()
    if len(v) <= 4:
        return f"<{len(v)}字符>"
    return f"{v[:4]}…<{len(v)}字符>"


class Finding:
    __slots__ = ("where", "line_no", "kind", "masked", "name")

    def __init__(self, where, line_no, kind, masked, name=""):
        self.where, self.line_no, self.kind = where, line_no, kind
        self.masked, self.name = masked, name

    def __str__(self):
        loc = f"{self.where}:{self.line_no}" if self.line_no else self.where
        who = f" [{self.name}]" if self.name else ""
        return f"{loc}{who}  {self.kind}  {self.masked}"


def scan_text(text: str, where: str = "<text>") -> list[Finding]:
    """扫一段文本，返回发现。同一行同一密钥只报一次。"""
    out: list[Finding] = []
    seen: set[tuple[int, str]] = set()
    for line_no, line in enumerate(text.splitlines(), 1):
        low = line.lower()
        if any(m in low for m in ALLOW_MARKERS):
            continue
        # 判据 1：值形态
        for kind, pat in VALUE_PATTERNS:
            for m in pat.finditer(line):
                k = (line_no, m.group(0))
                if k in seen:
                    continue
                seen.add(k)
                out.append(Finding(where, line_no, kind, mask(m.group(0))))
        # 判据 2：字段名 + 长度
        for m in NAME_ASSIGN.finditer(line):
            name, value = m.group(1), m.group(2)
            if looks_placeholder(value):
                continue
            if name_is_secretish(name):
                if not looks_like_secret(value):
                    continue
                kind = "可疑密钥字段"
            elif name_hints_secret(name) and is_high_entropy_secret(value):
                kind = "高熵长串（疑似自建密钥）"
            else:
                continue
            k = (line_no, value)
            if k in seen:
                continue
            seen.add(k)
            out.append(Finding(where, line_no, kind, mask(value), name))
    return out


def scan_file(path: str) -> list[Finding]:
    ext = os.path.splitext(path)[1].lower()
    if ext in SKIP_EXTS or any(d in path.split(os.sep) for d in SKIP_DIRS):
        return []
    try:
        if os.path.getsize(path) > 4 * 1024 * 1024:
            return []
        with open(path, encoding="utf-8", errors="replace") as f:
            return scan_text(f.read(), path)
    except (OSError, UnicodeError):
        return []


def iter_tree(root: str = "."):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            yield os.path.join(dirpath, fn)


def load_allowlist(root: str = ".") -> list[re.Pattern]:
    """可选 .gitguard-allow：每行一个正则，命中则忽略。"""
    p = os.path.join(root, ".gitguard-allow")
    if not os.path.exists(p):
        return []
    pats = []
    with open(p, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                try:
                    pats.append(re.compile(line))
                except re.error:
                    pass
    return pats


# ==================== git 相关：暂存区 / 全历史 ====================

def _git(*args: str) -> str:
    return subprocess.run(
        ("git",) + args, capture_output=True, text=True,
        errors="replace", check=False,
    ).stdout


def scan_staged() -> list[Finding]:
    """只扫「将要被提交的内容」（git diff --cached），不是工作区。

    这样才不会把用户尚未 add 的本地改动误判成泄漏。
    """
    names = _git("diff", "--cached", "--name-only", "--diff-filter=ACM").split("\n")
    findings: list[Finding] = []
    for name in filter(None, names):
        if os.path.splitext(name)[1].lower() in SKIP_EXTS:
            continue
        blob = _git("show", f":{name}")
        if blob:
            findings += scan_text(blob, f"暂存:{name}")
    return findings


def scan_history(root: str = ".", all_objects: bool = False) -> list[Finding]:
    """扫历史里的 blob。

    默认只扫**可达对象**（`rev-list --all --objects`）—— 这才是「历史」，
    也才是 `git push` 会传的东西。

    为什么不用 `--batch-all-objects`：那会把**悬空对象**（比如 `git add`
    过又被 `git rm --cached` 的暂存残留）一并扫进来。那些永远不会被 push，
    却会让扫描结果长期挂着几条噪音 —— 看久了人就麻木了。
    要偏执时用 all_objects=True（对应 CLI 的 --history-all）。
    """
    if all_objects:
        listing = subprocess.run(
            ["git", "cat-file", "--batch-all-objects",
             "--batch-check=%(objectname) %(objecttype) %(objectsize)"],
            capture_output=True, text=True, errors="replace", check=False,
        ).stdout
        targets = []
        for line in listing.splitlines():
            parts = line.split()
            if len(parts) != 3 or parts[1] != "blob":
                continue
            if 0 < int(parts[2]) <= 4 * 1024 * 1024:
                targets.append(parts[0])
    else:
        out = _git("rev-list", "--all", "--objects")
        # 同一 blob 可能被多个路径引用，去重以免重复报
        targets = list(dict.fromkeys(
            line.split()[0] for line in out.splitlines() if line.strip()
        ))
    if not targets:
        return []
    # 必须走二进制解析：文本模式按行切会把「内容里本来就有的换行」
    # 当成对象边界，导致 blob 被切碎、密钥跨在碎片上扫不到。
    return _scan_history_binary(targets)


def _scan_history_binary(oids: list[str]) -> list[Finding]:
    """用二进制 batch 流精确切分对象，避免文本模式下的边界误判。"""
    proc = subprocess.run(
        ["git", "cat-file", "--batch"], input="\n".join(oids).encode(),
        capture_output=True, check=False,
    )
    data, pos, findings = proc.stdout, 0, []
    while pos < len(data):
        nl = data.find(b"\n", pos)
        if nl < 0:
            break
        header = data[pos:nl].decode("utf-8", "replace")
        parts = header.split()
        # header 形如 `<sha1> blob <size>` —— 结尾是数字不是 " blob"。
        # 之前写成 endswith(" blob") 导致每个对象都被当成「非 blob」跳过，
        # 结果全历史扫描恒返「未发现密钥」—— 假阴性比漏报更危险。
        if len(parts) != 3 or parts[1] != "blob":
            pos = nl + 1          # 非 blob / missing：跳过这一行重试
            continue
        oid, size = parts[0], int(parts[2])
        body = data[nl + 1: nl + 1 + size]
        text = body.decode("utf-8", "replace")
        findings += scan_text(text, f"历史:{oid[:8]}")
        pos = nl + 1 + size + 1  # 跳过尾随换行
    return findings


def scan_paths(paths: list[str]) -> list[Finding]:
    findings: list[Finding] = []
    for p in paths:
        if os.path.isdir(p):
            for f in iter_tree(p):
                findings += scan_file(f)
        else:
            findings += scan_file(p)
    return findings


def report(findings: list[Finding], allow: list[re.Pattern], quiet: bool = False) -> int:
    kept = [f for f in findings if not any(p.search(str(f)) for p in allow)]
    if not kept:
        if not quiet:
            print("✓ 未发现密钥")
        return 0
    if not quiet:
        print(f"✗ 发现 {len(kept)} 处疑似密钥：\n")
        for f in kept:
            print("   " + str(f))
        print("\n提示：确认不是密钥可在该行加 `allowlist secret` 注释；")
        print("      确属误报可写进 .gitguard-allow（正则，每行一条）。")
    return 1


def main(argv: list[str]) -> int:
    mode = argv[1] if len(argv) > 1 else "--tree"
    rest = argv[2:]
    root = os.environ.get("GITGUARD_ROOT", ".")
    os.chdir(root)
    allow = load_allowlist(root)

    if mode == "--staged":
        return report(scan_staged(), allow)
    if mode == "--history":
        print("扫描历史（仅可达对象）…")
        return report(scan_history(root), allow)
    if mode == "--history-all":
        print("扫描全部 git 对象，含悬空对象（可能较慢）…")
        return report(scan_history(root, all_objects=True), allow)
    if mode == "--tree":
        return report(scan_paths(rest or ["."]), allow)
    if mode == "--files":
        return report(scan_paths(rest), allow)
    if mode == "--json":
        import json as _json
        print(_json.dumps([str(f) for f in scan_paths(rest or ["."])], ensure_ascii=False, indent=2))
        return 0
    print(__doc__)
    print("用法：secretscan.py [--staged|--tree|--history|--files <路径…>|--json]")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
