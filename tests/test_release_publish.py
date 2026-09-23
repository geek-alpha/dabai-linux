# -*- coding: utf-8 -*-
"""deploy/release/publish.py 的闸门测试。

真发布要动 GitHub，这里只测「该拒绝时会不会拒绝」和几个纯函数 ——
闸门写松了比写紧了危险得多：松了会发出版本不对、代码不对、或没测过的包，
而且不会有任何报错。所以每条拒绝路径都配一个用例。
"""

import argparse
import base64
import importlib.util
import json
import re
import subprocess
import sys
import time
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


# --- watch_release：push 完立刻查 run 会查不到 ---
#
# v1.0.10 首发就栽在这：publish.py 推完 tag 马上调 watch_release，GitHub 还没把
# 这条 run 登记进 actions/runs，脚本报「可能 workflow 没触发」退出码 2 —— 假失败，
# 重跑同一条命令立刻成功。所以「查不到」必须重试到窗口结束才算数。

watch = _load("dabai_watch", "deploy/release/watch_release.py")


def test_wait_run_retries_before_giving_up(monkeypatch):
    """前两次查不到（还没登记）、第三次命中 —— 不能第一次就判死。"""
    calls = []

    def fake_find(repo, sha, token):
        calls.append(sha)
        if len(calls) < 3:
            return None, "还没登记"
        return {"id": 42}, None

    monkeypatch.setattr(watch, "find_run", fake_find)
    run, err = watch.wait_run("o/r", "abc", "tok", window=5, interval=0.01)
    assert run == {"id": 42}
    assert err is None
    assert len(calls) == 3


def test_wait_run_reports_last_error_after_window(monkeypatch):
    """窗口耗尽后返回的是「最后一次的原因」，不是空错误 —— 报错文案要能定位。"""
    monkeypatch.setattr(watch, "find_run", lambda *a: (None, "push 事件里没找到该 commit"))
    run, err = watch.wait_run("o/r", "abc", "tok", window=0.05, interval=0.01)
    assert run is None
    assert "没找到该 commit" in err


def test_wait_run_returns_immediately_on_hit(monkeypatch):
    """命中时不空转：只查一次，窗口再长也不等。"""
    calls = []
    monkeypatch.setattr(watch, "find_run",
                        lambda *a: (calls.append(1), {"id": 7}, None)[1:])
    run, err = watch.wait_run("o/r", "abc", "tok", window=30, interval=5)
    assert run == {"id": 7}
    assert len(calls) == 1


# --- watch_release：run 成功 ≠ 资产已可读 ---
#
# v1.1.19 首发实测：run 转 completed 的同一时刻，releases/{id} 已见 2 个资产、
# releases/tags/{tag} 还是空 —— by-tags 有缓存延迟，而资产本身也是逐个上传的。
# 查一次就判「缺资产」退出 1 是假失败；「release 压根没建出来」是超时（2），不是失败（1）。


def _seq_api(monkeypatch, replies):
    """按调用顺序返回预设响应；用完后一直重复最后一个。"""
    calls = []

    def fake(url, token, timeout=30):
        calls.append(url)
        return replies[min(len(calls) - 1, len(replies) - 1)]

    monkeypatch.setattr(watch, "api_get", fake)
    return calls


def _quiet(*a, **k):
    return None


def test_wait_assets_switches_to_id_endpoint(monkeypatch):
    """by-tags 只用来换 id，之后按 id 查 —— 资产逐个上传时也不能第一次就返回。"""
    calls = _seq_api(monkeypatch, [
        (200, {"id": 77, "assets": []}),
        (200, {"id": 77, "assets": [{"name": "a.tar.gz"}]}),
        (200, {"id": 77, "assets": [{"name": "a.tar.gz"}, {"name": "a.tar.gz.sha256"}]}),
    ])
    rel_id, assets = watch.wait_assets(
        "o/r", "v1.0.0", "tok", ("a.tar.gz", "a.tar.gz.sha256"),
        deadline=time.time() + 5, interval=0.01, log=_quiet)
    assert (rel_id, assets) == (77, ["a.tar.gz", "a.tar.gz.sha256"])
    assert calls[0].endswith("/releases/tags/v1.0.0")
    assert "/releases/77" in calls[1]


def test_wait_assets_waits_out_tag_endpoint_lag(monkeypatch):
    """by-tags 还在 404 时别放弃：缓存一过期拿到 id，就按 id 查出资产。"""
    _seq_api(monkeypatch, [
        (404, {}),
        (404, {}),
        (200, {"id": 5, "assets": [{"name": "a.tar.gz"}, {"name": "a.tar.gz.sha256"}]}),
    ])
    rel_id, assets = watch.wait_assets(
        "o/r", "v1.0.0", "tok", ("a.tar.gz", "a.tar.gz.sha256"),
        deadline=time.time() + 5, interval=0.01, log=_quiet)
    assert rel_id == 5
    assert assets == ["a.tar.gz", "a.tar.gz.sha256"]


def test_wait_assets_reports_partial_after_deadline(monkeypatch):
    """窗口耗尽时把「已建出来但资产不全」如实交回，由调用方判 1。"""
    _seq_api(monkeypatch, [(200, {"id": 9, "assets": [{"name": "a.tar.gz"}]})])
    rel_id, assets = watch.wait_assets(
        "o/r", "v1.0.0", "tok", ("a.tar.gz", "a.tar.gz.sha256"),
        deadline=time.time() - 1, interval=0.01, log=_quiet)
    assert (rel_id, assets) == (9, ["a.tar.gz"])


def test_wait_assets_none_id_when_release_absent(monkeypatch):
    """release 一直没建出来：id 为 None —— 调用方据此报超时（2），不是失败（1）。"""
    _seq_api(monkeypatch, [(404, {})])
    rel_id, assets = watch.wait_assets(
        "o/r", "v1.0.0", "tok", ("a.tar.gz",),
        deadline=time.time() - 1, interval=0.01, log=_quiet)
    assert rel_id is None
    assert assets == []


def _wire_watch_main(monkeypatch, wait_result):
    monkeypatch.setattr(watch, "get_token", lambda: "tok")
    monkeypatch.setattr(watch, "tag_commit", lambda root, tag: "abc123")
    monkeypatch.setattr(watch, "wait_run", lambda *a, **k: (
        {"id": 42, "display_title": "release", "status": "completed"}, None))
    monkeypatch.setattr(watch, "api_get", lambda *a, **k: (
        200, {"status": "completed", "conclusion": "success"}))
    monkeypatch.setattr(watch, "wait_assets", lambda *a, **k: wait_result)


def test_main_exit2_when_release_never_appears(monkeypatch):
    """workflow 成功但 release 没建出来 = 还没落地（超时），退出码 2 —— 不是失败。"""
    _wire_watch_main(monkeypatch, (None, []))
    monkeypatch.setattr(sys, "argv", ["watch_release.py", "v1.0.0", "--assets-window", "0"])
    assert watch.main() == 2


def test_main_exit1_when_release_truly_missing_assets(monkeypatch):
    """release 建出来了但资产真缺 —— 这才是失败，退出码 1。"""
    _wire_watch_main(monkeypatch, (9, ["dabai-1.0.0.tar.gz"]))
    monkeypatch.setattr(sys, "argv", ["watch_release.py", "v1.0.0", "--assets-window", "0"])
    assert watch.main() == 1


# --- 发布端不碰联邦：更新归每台机器自己的定时器 ---
#
# 联邦留言从来不是「通知」，它是自动更新的闹钟；而地址簿里只有入过联邦的机器 ——
# 没入联邦的机器装了定时器照样更新。所以闹钟这一环去掉：发布只负责把包发出去，
# 谁什么时候装上新版，由每台机器的 dabai-update.timer 自己查、自己校验、自己回滚。


def _wire_main(monkeypatch, watch_rc):
    """把 main 的六步全换成桩，只观察它有没有去碰联邦链路。"""
    calls = {"run": []}
    monkeypatch.setattr(publish, "read_token", lambda *a, **k: "tok")
    monkeypatch.setattr(publish, "preflight", lambda a, t: {
        "version": "1.0.11", "branch": "main", "dirty": [], "ahead": 1})
    monkeypatch.setattr(publish, "do_build", lambda part: "1.0.11")
    monkeypatch.setattr(publish, "do_tests", lambda t: None)
    monkeypatch.setattr(publish, "do_commit", lambda *a: None)
    monkeypatch.setattr(publish, "do_push", lambda *a: None)
    monkeypatch.setattr(publish, "do_log", lambda e: None)
    monkeypatch.setattr(publish, "do_watch", lambda *a, **k: watch_rc)
    monkeypatch.setattr(publish, "git", _FakeGit({"rev-parse HEAD": _cp("abc123\n")}))

    def fake_run(cmd, timeout=None, env=None):
        calls["run"].append(list(cmd))
        return _cp("{}")

    monkeypatch.setattr(publish, "_run", fake_run)
    return calls


def test_main_never_messages_peers(monkeypatch):
    """发布端不再给任何人留言：留言只覆盖入过联邦的机器，装定时器的机器自己会更新。"""
    calls = _wire_main(monkeypatch, watch_rc=0)
    monkeypatch.setattr(sys, "argv", ["publish.py", "-m", "test"])
    assert publish.main() == 0
    assert [c for c in calls["run"] if "peer_mesh" in " ".join(str(x) for x in c)] == []


def test_main_still_reports_failed_watch(monkeypatch):
    """盯落地失败仍然返回非 0 —— 去掉通知不该顺手把发布结果也放松。"""
    _wire_main(monkeypatch, watch_rc=2)
    monkeypatch.setattr(sys, "argv", ["publish.py", "-m", "test"])
    assert publish.main() == 2


def test_publish_has_no_notify_flag(monkeypatch):
    """--no-notify 随通知一起退役 —— 留着它等于留着一条没人走的路。"""
    monkeypatch.setattr(sys, "argv", ["publish.py", "--no-notify"])
    with pytest.raises(SystemExit):
        publish.main()
