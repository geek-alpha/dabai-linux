# -*- coding: utf-8 -*-
"""deploy/release/publish.py 的闸门测试。

真发布要动 GitHub，这里只测「该拒绝时会不会拒绝」和几个纯函数 ——
闸门写松了比写紧了危险得多：松了会发出版本不对、代码不对、或没测过的包，
而且不会有任何报错。所以每条拒绝路径都配一个用例。
"""

import argparse
import base64
import importlib.util
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


publish = _load("dabai_publish", "deploy/release/publish.py")


def _cp(stdout="", returncode=0, stderr=""):
    return subprocess.CompletedProcess(["git"], returncode, stdout, stderr)


class _FakeGit:
    """按命令前缀返回预设结果；没预设的命令直接断言失败。

    故意不返回默认成功 —— 漏预设的命令会静默通过，闸门测试就白写了。
    """

    def __init__(self, table):
        self.table = dict(table)
        self.calls = []

    def __call__(self, *args, token="", check=False):
        cmd = " ".join(args)
        self.calls.append(cmd)
        for prefix, result in self.table.items():
            if cmd.startswith(prefix):
                if check and result.returncode != 0:
                    raise publish.Fail(f"git {cmd} 失败")
                return result
        raise AssertionError(f"测试没预设这条 git 命令：{cmd}")


def _args(**kw):
    base = dict(branch="main", commit_all=False, tag_only=False)
    base.update(kw)
    return argparse.Namespace(**base)


def _wire(monkeypatch, tmp_path, table, version="1.0.9"):
    fake = _FakeGit(table)
    monkeypatch.setattr(publish, "git", fake)
    vf = tmp_path / "VERSION"
    vf.write_text(version + "\n", encoding="utf-8")
    monkeypatch.setattr(publish, "VERSION_FILE", vf)
    return fake


BASE = {
    "rev-parse --abbrev-ref HEAD": _cp("main\n"),
    "status --porcelain": _cp(""),
    "fetch origin main --tags": _cp(""),
    "rev-list --count HEAD..origin/main": _cp("0\n"),
    "rev-list --count origin/main..HEAD": _cp("0\n"),
    "tag --sort=-v:refname --list v*": _cp("v1.0.9\n"),
}


def _table(**over):
    t = dict(BASE)
    t.update(over)
    return t


# ---------- 纯函数 ----------

@pytest.mark.parametrize("text,want", [
    ("1.0.9", (1, 0, 9)),
    ("v1.0.10", (1, 0, 10)),
    ("1.0", (1, 0, 0)),
    ("2", (2, 0, 0)),
    ("1.0.9\n", (1, 0, 9)),
    ("1.0.rc1", (1, 0, 1)),
    ("", (0, 0, 0)),
])
def test_parse_version(text, want):
    assert publish.parse_version(text) == want


def test_parse_version_orders_10_above_9():
    """字符串比较会把 1.0.10 排在 1.0.9 前面，闸门就反了。"""
    assert publish.parse_version("1.0.10") > publish.parse_version("1.0.9")


def test_repo_default_matches_update_config():
    """两份 REPO 常量漂移 = 本地推 A 仓库、更新器拉 B 仓库，且两边都不报错。"""
    src = (ROOT / "deploy" / "release" / "update.py").read_text(encoding="utf-8")
    m = re.search(r'"REPO"\s*:\s*"([^"]+)"', src)
    assert m, "update.py 里没找到 DEFAULT_CONFIG 的 REPO"
    assert m.group(1) == publish.REPO_DEFAULT


def test_read_token_prefers_env(monkeypatch, tmp_path):
    monkeypatch.setenv("GITHUB_TOKEN", "from-env")
    f = tmp_path / "secrets.env"
    f.write_text("GITHUB_TOKEN=from-file\n", encoding="utf-8")
    assert publish.read_token(paths=[f]) == "from-env"


def test_read_token_falls_back_to_file(monkeypatch, tmp_path):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    f = tmp_path / "secrets.env"
    f.write_text("OTHER=1\nGITHUB_TOKEN=from-file\n", encoding="utf-8")
    assert publish.read_token(paths=[f]) == "from-file"


def test_read_token_empty_when_absent(monkeypatch, tmp_path):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    assert publish.read_token(paths=[tmp_path / "nope.env"]) == ""


# ---------- token 不进命令行 ----------

def test_auth_env_keeps_token_out_of_argv(monkeypatch):
    """同机任何用户都能读别人的 /proc/<pid>/cmdline，明文 token 进去就是泄漏。"""
    seen = {}

    def fake_run(cmd, timeout=None, env=None):
        seen["cmd"] = cmd
        seen["env"] = env
        return _cp("")

    monkeypatch.setattr(publish, "_run", fake_run)
    publish.git("push", "origin", "main", token="fake-token-not-a-real-secret")

    assert "fake-token-not-a-real-secret" not in " ".join(seen["cmd"])
    assert seen["env"]["GIT_TERMINAL_PROMPT"] == "0"
    enc = seen["env"]["GIT_CONFIG_VALUE_0"].split("Basic ", 1)[1]
    assert "fake-token-not-a-real-secret" not in enc
    assert base64.b64decode(enc).decode() == "x-access-token:fake-token-not-a-real-secret"


# ---------- 前置闸门：每条拒绝路径 ----------

def test_preflight_passes_on_clean_repo(monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path, _table())
    info = publish.preflight(_args(), "tok")
    assert info["version"] == "1.0.9"
    assert info["branch"] == "main"


def test_rejects_dirty_worktree(monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path, _table(**{"status --porcelain": _cp(" M agent.py\n?? x.py\n")}))
    with pytest.raises(publish.Fail) as e:
        publish.preflight(_args(), "tok")
    assert "未提交改动" in str(e.value)


def test_commit_all_lets_dirty_through(monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path, _table(**{"status --porcelain": _cp(" M agent.py\n")}))
    info = publish.preflight(_args(commit_all=True), "tok")
    assert info["dirty"] == [" M agent.py"]


def test_rejects_wrong_branch(monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path, _table(**{"rev-parse --abbrev-ref HEAD": _cp("dev\n")}))
    with pytest.raises(publish.Fail) as e:
        publish.preflight(_args(), "tok")
    assert "dev" in str(e.value)


def test_rejects_behind_remote(monkeypatch, tmp_path):
    """落后远端还发版 = 拿旧代码打的包，而且推不上去（非快进）。"""
    _wire(monkeypatch, tmp_path, _table(**{"rev-list --count HEAD..origin/main": _cp("3\n")}))
    with pytest.raises(publish.Fail) as e:
        publish.preflight(_args(), "tok")
    assert "落后" in str(e.value)


def test_rejects_version_tag_mismatch(monkeypatch, tmp_path):
    """VERSION 1.0.10 但最新 tag 还是 v1.0.9 = 上次发布没走完，先查清。"""
    _wire(monkeypatch, tmp_path, _table(), version="1.0.10")
    with pytest.raises(publish.Fail) as e:
        publish.preflight(_args(), "tok")
    assert "不一致" in str(e.value)


def test_rejects_version_below_latest_tag(monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path, _table(), version="1.0.8")
    with pytest.raises(publish.Fail) as e:
        publish.preflight(_args(), "tok")
    assert "低于" in str(e.value)


def test_tag_only_requires_version_ahead(monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path, _table())
    with pytest.raises(publish.Fail) as e:
        publish.preflight(_args(tag_only=True), "tok")
    assert "--tag-only" in str(e.value)


def test_tag_only_passes_when_version_ahead(monkeypatch, tmp_path):
    """上次 CI 挂了但 VERSION 已升号：补推 tag 是正常路径，不该被 mismatch 拦死。"""
    _wire(monkeypatch, tmp_path, _table(), version="1.0.10")
    info = publish.preflight(_args(tag_only=True), "tok")
    assert info["version"] == "1.0.10"


def test_no_tags_yet_is_allowed(monkeypatch, tmp_path):
    """首次发布时一个 v* tag 都没有，不能因此拦住。"""
    _wire(monkeypatch, tmp_path, _table(**{"tag --sort=-v:refname --list v*": _cp("")}))
    info = publish.preflight(_args(), "tok")
    assert info["latest_tag"] == ""
