#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""安全发布闸门的守卫测试。

闸门本身是「防止把密钥推出去」的最后一道，它自己坏掉比没有更危险 —— 你以为查过了。
所以这里测的不是「能不能推」，而是判据本身：token 从哪来、硬雷清单有没有被删空、
排除集是否包含自己（否则闸门永远卡在自己的正则字面量上）。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location(
    "safe_push", ROOT / "deploy" / "gitguard" / "safe_push.py")
sp = importlib.util.module_from_spec(SPEC)
sys.modules["safe_push"] = sp
SPEC.loader.exec_module(sp)


def test_token_prefers_env(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_fromenv")
    token, src = sp.find_token()
    assert token == "ghp_fromenv"
    assert "环境变量" in src


def test_token_reads_secrets_file_and_strips_quotes(tmp_path, monkeypatch):
    f = tmp_path / "secrets.env"
    f.write_text("# 注释\nGITHUB_TOKEN='ghp_quoted'\nOTHER=x\n", encoding="utf-8")
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setattr(sp, "secrets_env_candidates", lambda: [f])
    token, src = sp.find_token()
    assert token == "ghp_quoted"
    assert src == str(f)


def test_token_handles_utf8_bom(tmp_path, monkeypatch):
    """PowerShell 的 Set-Content -Encoding UTF8 会写 BOM；按 utf-8 读会让第一个键
    变成 '\\ufeffGITHUB_TOKEN'，token 静默找不到。"""
    f = tmp_path / "secrets.env"
    f.write_bytes(b"\xef\xbb\xbfGITHUB_TOKEN=ghp_bom\r\n")
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setattr(sp, "secrets_env_candidates", lambda: [f])
    token, _ = sp.find_token()
    assert token == "ghp_bom"


def test_token_missing_returns_empty(monkeypatch, tmp_path):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setattr(sp, "secrets_env_candidates", lambda: [tmp_path / "nope.env"])
    assert sp.find_token() == ("", "")


def test_platform_secrets_paths(monkeypatch):
    import pathlib

    # 钉 os.name 的同时必须把 Path 钉成 PosixPath：os.name == "nt" 时
    # Path.__new__ 会分派到 WindowsPath，而 WindowsPath 在非 Windows 上直接抛
    # UnsupportedOperation（只验路径拼接逻辑，不需要真的模拟 Windows 文件系统）。
    monkeypatch.setattr(sp, "Path", pathlib.PosixPath)

    monkeypatch.setattr(sp.os, "name", "nt")
    monkeypatch.setenv("APPDATA", r"C:\Users\x\AppData\Roaming")
    got = [str(p) for p in sp.secrets_env_candidates()]
    assert any("dabai" in p and "secrets.env" in p for p in got)
    assert not any(p.startswith("/etc/") for p in got)

    monkeypatch.setattr(sp.os, "name", "posix")
    got = [str(p) for p in sp.secrets_env_candidates()]
    assert "/etc/dabai/secrets.env" in got


def test_hard_files_cover_the_incident():
    """2025 那次泄漏的正是 settings.json；这份清单被删空等于闸门失效。"""
    for must in ("settings.json", "codex_config.json", "key.pem", "cert.pem"):
        assert must in sp.HARD_FILES


def test_exclude_covers_self():
    """自身含正则字面量，不排除就会永远卡红 —— 这类静默失效比误报更危险。"""
    assert sp.SELF in sp.EXCLUDE
    assert "deploy/gitguard/secretscan.py" in sp.EXCLUDE


def test_patterns_are_valid_for_git_grep():
    """PATTERNS 是喂给 ``git grep -E``（POSIX ERE）的，不能用 Python 的 re 去验 ——
    ``[[:space:]]`` 在 Python re 里只是个 FutureWarning，验了等于没验。让 git 自己试：
    正则非法时 git grep 返回 2，正常无命中返回 1、有命中返回 0。
    """
    for pat in sp.PATTERNS:
        rc, _ = sp.git("grep", "-nIE", "-e", pat, "HEAD", "--", ".")
        assert rc in (0, 1), f"git grep 拒绝了这条正则（rc={rc}）：{pat}"


def test_wrapper_delegates_to_python():
    """bash 入口必须是薄包装：留着旧实现就会与 Python 版漂移。"""
    sh = (ROOT / "deploy" / "gitguard" / "safe-push.sh").read_text(encoding="utf-8")
    assert "safe_push.py" in sh
    assert "exec " in sh
    # 旧实现的特征串不该还在：git grep / base64 -w0 都在 Python 里
    assert "base64 -w0" not in sh


def test_launch_env_parser_matches_sync_secrets():
    """launch.py 与 sync_secrets.py 读的是同一个 secrets.env。

    两个解析器对同一行给出不同结果，会出现「同步器认为写进去了、启动器没注入」
    这种只在服务侧复现的差异 —— 所以逐字对齐解析规则，并用同一份样本钉住。
    """
    spec = importlib.util.spec_from_file_location(
        "sync_secrets_for_parse", ROOT / "deploy" / "secrets" / "sync_secrets.py")
    ss = importlib.util.module_from_spec(spec)
    sys.modules["sync_secrets_for_parse"] = ss
    spec.loader.exec_module(ss)

    spec2 = importlib.util.spec_from_file_location(
        "win_launch_for_parse", ROOT / "deploy" / "windows" / "launch.py")
    lp = importlib.util.module_from_spec(spec2)
    sys.modules["win_launch_for_parse"] = lp
    spec2.loader.exec_module(lp)

    sample = (
        "# 注释\n"
        "GITHUB_TOKEN='ghp_abc'\n"
        "EXA_API_KEY=plain\n"
        "lower_case=should_be_ignored\n"
        "1LEADING=ignored\n"
        "bad line\n"
    )
    assert ss.parse_env_file(sample) == lp.parse_env_file(sample)
    assert "lower_case" not in lp.parse_env_file(sample)
