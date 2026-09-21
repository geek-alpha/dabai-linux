#!/usr/bin/env python3
"""用真 git 当 oracle，找 GitPython config 解析跟 git 不一致的地方。

思路：不猜维护者想要什么行为——git 本身就是权威。同一份 config 文件，
同一份 config 文件，`git config -f X --list --null` 和 GitConfigParser 各读一遍，值不一样就是 bug。

   GITPYTHON_SRC=/path/to/GitPython venv/bin/python tools/gitconfig_diff.py

零不一致说明这份用例集下两边语义相同；有差异就是候选 bug，再去核 git 的
退出码，判断是「git 拒绝而 GitPython 接受」还是「值不同」。
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

_GITPYTHON_SRC = os.environ.get("GITPYTHON_SRC", "/home/wxf/oss-work/GitPython")
sys.path.insert(0, _GITPYTHON_SRC)
import git  # noqa: E402

CASES: dict[str, str] = {
    "simple": "[core]\n\tbare = true\n",
    "inline_comment_hash": "[core]\n\tbare = true # comment\n",
    "inline_comment_semicolon": "[core]\n\tbare = true ; comment\n",
    "quoted_hash": '[core]\n\tbare = "true # not comment"\n',
    "continuation": "[core]\n\tbare = tru\\\n\te\n",
    "continuation_in_comment": "[core]\n\tbare = true # com\\\nment\n",
    "escape_n": '[core]\n\tbare = "a\\nb"\n',
    "escape_t": '[core]\n\tbare = "a\\tb"\n',
    "escape_backslash": '[core]\n\tbare = "a\\\\b"\n',
    "escape_quote": '[core]\n\tbare = "a\\"b"\n',
    "unknown_escape": '[core]\n\tbare = "a\\qb"\n',
    "implicit_bool": "[core]\n\tbare\n",
    "implicit_bool_comment": "[core]\n\tbare # c\n",
    "empty_value": "[core]\n\tbare =\n",
    "whitespace_value": "[core]\n\tbare =    true   \n",
    "quoted_whitespace": '[core]\n\tbare = "  true  "\n',
    "case_insensitive_key": "[CORE]\n\tBARE = true\n",
    "subsection_case": '[remote "Origin"]\n\turl = x\n',
    "old_style_subsection": "[remote.Origin]\n\turl = x\n",
    "subsection_with_dot": '[remote "a.b"]\n\turl = x\n',
    "subsection_with_quote": '[remote "a\\"b"]\n\turl = x\n',
    "no_trailing_newline": "[core]\n\tbare = true",
    "crlf": "[core]\r\n\tbare = true\r\n",
    "bom": "\ufeff[core]\n\tbare = true\n",
    "dup_key": "[core]\n\tbare = true\n\tbare = false\n",
    "section_with_dash": "[my-section]\n\tkey = v\n",
    "section_with_digit": "[section1]\n\tkey = v\n",
    "key_with_dash": "[core]\n\tmy-key = v\n",
    "empty_section": "[core]\n[user]\n\tname = x\n",
    "comment_only": "# comment\n[core]\n\tbare = true\n",
    "value_with_equals": "[core]\n\tbare = a=b\n",
    "tab_in_value": "[core]\n\tbare = a\tb\n",
    "no_space_around_eq": "[core]\n\tbare=true\n",
    "section_padded": "[ core ]\n\tbare = true\n",
    "key_padded": "[core]\n\tbare   = true\n",
    "quote_in_middle": '[core]\n\tbare = a"b"c\n',
    "single_quote": "[core]\n\tbare = 'true'\n",
    "unquoted_backslash_n": "[core]\n\tbare = a\\nb\n",
    "double_continuation": "[core]\n\tbare = a\\\n\\\nb\n",
    "continuation_then_comment": "[core]\n\tbare = a\\\n# c\n",
    "empty_section_body_then_key": "[core]\n\n\tbare = true\n",
    "subsection_empty": '[remote ""]\n\turl = x\n',
    "value_only_spaces": "[core]\n\tbare =    \n",
    "cr_only_lineend": "[core]\r\tbare = true\r",
    "utf8_value": "[user]\n\tname = 张伟\n",
    "value_with_trailing_backslash": '[core]\n\tbare = "a\\\\"\n',
}


def git_read(path: Path):
    proc = subprocess.run(
        ["git", "config", "-f", str(path), "--list", "--null"],
        capture_output=True,
    )
    if proc.returncode != 0:
        return None, proc.stderr.decode("utf-8", "replace").strip()
    pairs = []
    for chunk in proc.stdout.decode("utf-8", "surrogateescape").split("\0"):
        if not chunk:
            continue
        key, _, value = chunk.partition("\n")
        pairs.append((key, value))
    return pairs, None


def _norm_key(section: str, option: str) -> str:
    """GitPython 的 section 名 → git 的点分键名。"""
    section = section.strip()
    if '"' in section:
        head, _, tail = section.partition('"')
        sub = tail.rstrip('"').replace('\\"', '"').replace("\\\\", "\\")
        return f"{head.strip().lower()}.{sub}.{option.lower()}"
    return f"{section.lower()}.{option.lower()}"


def gp_read(path: Path):
    try:
        with git.GitConfigParser(str(path), read_only=True) as cfg:
            pairs = []
            for section in cfg.sections():
                for option, values in cfg.items_all(section):
                    for value in values:
                        pairs.append((_norm_key(section, option), value))
            return pairs, None
    except Exception as exc:  # noqa: BLE001
        return None, f"{type(exc).__name__}: {exc}"


def main() -> int:
    tmpdir = Path(tempfile.mkdtemp(prefix="cfgdiff-"))
    differ = 0
    for name, text in CASES.items():
        path = tmpdir / f"{name}.gitconfig"
        path.write_text(text, encoding="utf-8", newline="")
        want, want_err = git_read(path)
        got, got_err = gp_read(path)
        same = (want is not None and got is not None
                and sorted(want) == sorted(got))
        if same and want_err is None and got_err is None:
            continue
        differ += 1
        print(f"✗ {name}")
        print(f"    git      : {want if want is not None else 'ERROR ' + (want_err or '')}")
        print(f"    gitpython: {got if got is not None else 'ERROR ' + (got_err or '')}")
    print(f"\n共 {len(CASES)} 个用例，不一致 {differ} 个（临时目录 {tmpdir}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
