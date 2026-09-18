#!/usr/bin/env python3
"""从真实配置生成脱敏的 *.example.json —— 可安全提交进仓库的模板。

为什么需要：含密钥的配置一旦 gitignore 掉，仓库里就没有「配置长什么样」
的参考，clone 下来的人（以及未来的你）不知道该填哪些字段。
example 保留完整结构、只把密钥值清空。
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import secretscan as ss  # noqa: E402

FILES = [
    "settings.json",
    "codex_config.json",
    "stt_config.json",
    "tts_config.json",
    "character_cards.json",
]


def is_secret_pair(name: str, value: str) -> bool:
    """名字或值任一命中即视为密钥。"""
    if not isinstance(value, str) or not value:
        return False
    for _, pat in ss.VALUE_PATTERNS:
        if pat.search(value):
            return True
    if ss.name_is_secretish(name) and ss.looks_like_secret(value):
        return True
    return ss.name_hints_secret(name) and ss.is_high_entropy_secret(value)


def sanitize(obj, name: str = ""):
    if isinstance(obj, dict):
        return {k: sanitize(v, k) for k, v in obj.items()}
    if isinstance(obj, list):
        return [sanitize(v, name) for v in obj]
    if is_secret_pair(name, obj):
        return ""
    return obj


def detect_indent(text: str) -> int:
    for line in text.splitlines():
        if line.startswith(" "):
            return len(line) - len(line.lstrip(" "))
        if line.strip():
            break
    return 2


def main(argv: list[str]) -> int:
    repo = argv[1] if len(argv) > 1 else "."
    made, skipped = [], []
    for name in FILES:
        src = os.path.join(repo, name)
        if not os.path.exists(src):
            skipped.append(name)
            continue
        raw = open(src, encoding="utf-8-sig").read()
        has_bom = open(src, "rb").read(3) == b"\xef\xbb\xbf"
        data = json.loads(raw)
        clean = sanitize(data)
        # 二次自检：脱敏结果必须扫不出密钥，否则宁可报错也不落盘
        text = json.dumps(clean, ensure_ascii=False, indent=detect_indent(raw))
        left = ss.scan_text(text, name)
        if left:
            print(f"  ✗ {name}: 脱敏后仍残留 {len(left)} 处，已中止", file=sys.stderr)
            for f in left:
                print(f"      {f}", file=sys.stderr)
            return 1
        out = os.path.join(repo, name.replace(".json", ".example.json"))
        with open(out, "w", encoding="utf-8-sig" if has_bom else "utf-8") as f:
            f.write(text + "\n")
        made.append(os.path.basename(out))
    for m in made:
        print(f"  ✓ {m}")
    for s in skipped:
        print(f"  – {s}（不存在，跳过）")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
