#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""热重载守护的存活契约：一次失败不许把守护线程杀死。

背景（2026-09-13）：_watch_loop 检测到核心文件变化后调用 _restart_process，
旧代码在它返回后无条件 `return` —— 而 _restart_process 在「语法检查没过」时
静默返回 None。于是编辑器的一次半写状态（或任何一次语法错误）就能让守护线程
永久退出：日志无异常、服务照常跑、前端无感，唯一症状是「改了核心代码没反应」，
而且此后每一次改动都没反应。这类故障没有外部症状，只能靠测试锁住。

契约（三条）：
  1. 语法没过 → 跳过本次，守护继续监听（不能 return、不能退出线程）
  2. 重启成功（execv 已替换进程）→ _restart_process 返回 True，_watch_loop 让位
  3. 重启阶段抛异常 → 异常不被吞成「成功」，但守护也不死

时序构造：用假时钟 + 版本序列的快照替身驱动 _watch_loop，不真等合并节流窗口。
"""
import importlib.util
import os
import sys
import tempfile
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

import hot_reload as hr  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate_loaded_state(tmp_path, monkeypatch):
    """假快照绝不能写进 data/hot_reload_state.json。

    那是 tools/reload_check.py 判「核心改动是否真进了运行中的进程」的唯一可信判据，
    被夹具覆盖后它会产出一整片假警报：实测 2026-09-14 06:40 跑完全套测试，快照变成
    {"pid": 36576, "core": {"/fake/core_x.py": [1, 10]}}，reload_check 随即报「44 个
    核心文件未生效」。_watch_loop 开头就落盘一次快照，所以必须 autouse。
    """
    monkeypatch.setattr(hr, "LOADED_STATE_FILE", tmp_path / "hot_reload_state.json")



class _StopLoop(BaseException):
    """保险丝：守护没按预期退出时把测试从死循环里救出来。

    必须继承 BaseException —— _watch_loop 的 `except Exception` 会把普通异常
    吞成一条日志警告，保险丝被吞掉就变成死循环。
    """


class _FakeClock:
    """把 time.sleep 变 no-op、time.time 每次前进 10s 的假时钟。

    替换的是 hot_reload 模块内的 time 名字，不是全局 time 模块 —— 后者会连带
    影响 pytest/threading 自己的计时。
    """

    def __init__(self, max_sleeps=200):
        self.t = 1_000_000.0
        self.sleeps = 0
        self.max_sleeps = max_sleeps

    def sleep(self, _secs=0.0):
        self.sleeps += 1
        if self.sleeps > self.max_sleeps:
            raise _StopLoop("保险丝熔断：守护循环次数超出预期（多半是没退出）")

    def time(self):
        self.t += 10.0  # 每次查询前进 10s → 合并节流窗口立刻满足
        return self.t


class _FakeFS:
    """按版本序列回答核心文件快照，用来构造「又变了 / 没变」的时序。"""

    CORE = "/fake/core_x.py"
    EXT = "/fake/ext_x.py"

    def __init__(self, core_versions):
        self._versions = list(core_versions)
        self.core_snapshots = 0

    def snapshot(self, paths):
        names = {str(p) for p in paths}
        if self.CORE in names:
            i = min(self.core_snapshots, len(self._versions) - 1)
            self.core_snapshots += 1
            return {self.CORE: (self._versions[i], 10)}
        return {self.EXT: (1, 10)}


class _OsProxy:
    """替身 os：只覆盖 name/chdir/execv/_exit，其余委托真 os。

    必须委托 —— _safe_to_compile 内部要用 os.path.join；替身漏了它，
    语法检查会全部失败，看起来像产品代码坏了（实测踩过）。
    """

    def __init__(self, raises=False):
        self._real = os
        self.name = "posix"
        self.execv_calls = []
        self.chdir_to = None
        self._raises = raises

    def __getattr__(self, item):
        return getattr(self._real, item)

    def chdir(self, path):
        self.chdir_to = path

    def execv(self, path, argv):
        self.execv_calls.append((path, list(argv)))
        if self._raises:
            raise OSError("execv 失败（模拟权限/路径问题）")

    def _exit(self, code):  # pragma: no cover - 只有 nt 分支会走
        raise AssertionError("posix 分支不该调用 os._exit")


def _run_watch_loop(monkeypatch, core_versions, restart_results,
                    autorestart=True, max_sleeps=200):
    """驱动 _watch_loop 到自然退出（或保险丝熔断），返回 (异常, 重启调用, FS, 时钟)。"""
    clock = _FakeClock(max_sleeps)
    fs = _FakeFS(core_versions)
    calls = []

    def fake_restart(changed, reason):
        calls.append((sorted(p.name for p in changed), reason))
        if not autorestart:
            return False
        i = len(calls) - 1
        r = restart_results[i] if i < len(restart_results) else restart_results[-1]
        if isinstance(r, BaseException):
            raise r
        return r

    monkeypatch.setattr(hr, "time", clock)
    monkeypatch.setattr(hr, "_scan_core", lambda: [Path(_FakeFS.CORE)])
    monkeypatch.setattr(hr, "_scan_ext", lambda: [Path(_FakeFS.EXT)])
    monkeypatch.setattr(hr, "_snapshot", fs.snapshot)
    monkeypatch.setattr(hr, "_core_autorestart_enabled", lambda: autorestart)
    monkeypatch.setattr(hr, "_busy_work_running", lambda: False)
    monkeypatch.setattr(hr, "_active_turns_running", lambda: False)
    monkeypatch.setattr(hr, "_restart_process", fake_restart)

    err = None
    try:
        hr._watch_loop(None)
    except BaseException as e:  # noqa: BLE001 - 保险丝 _StopLoop 也在这里被接住
        err = e
    return err, calls, fs, clock


# ---------- 契约 1：语法没过，守护必须活着 ----------

def test_syntax_failure_keeps_watchdog_alive(monkeypatch):
    """返回 False 后守护要继续监听：第二次变化必须仍被检测到。"""
    err, calls, fs, _ = _run_watch_loop(
        monkeypatch,
        core_versions=[1, 2, 2, 3, 3],   # 第一轮变、第二轮再变
        restart_results=[False, True],   # 第一次语法没过、第二次改好了
    )
    assert not isinstance(err, _StopLoop), "守护没按预期退出（循环失控）"
    assert err is None
    assert len(calls) == 2, (
        "语法失败后守护再没检测到后续变化 —— 线程已死，"
        "此后所有核心改动静默不生效（calls=%r）" % (calls,)
    )


def test_restart_exception_keeps_watchdog_alive(monkeypatch):
    """重启阶段抛异常（权限/路径问题）不能把守护带走。"""
    err, calls, fs, _ = _run_watch_loop(
        monkeypatch,
        core_versions=[1, 2, 2, 3, 3, 4, 4],
        restart_results=[OSError("execv boom"), True],
    )
    assert err is None
    assert len(calls) == 2, "重启抛异常后守护没再醒来"


def test_autorestart_disabled_keeps_polling(monkeypatch):
    """core_autorestart=false 时只记日志：守护持续轮询，绝不调重启。"""
    err, calls, fs, _ = _run_watch_loop(
        monkeypatch,
        core_versions=[1, 2, 3, 4, 5, 6, 7, 8],
        restart_results=[False],
        autorestart=False,
        max_sleeps=6,
    )
    assert isinstance(err, _StopLoop), "autorestart 关闭时守护反而退出了"
    assert calls == [], "autorestart 关闭却调用了重启"
    assert fs.core_snapshots >= 3, "守护没有继续扫描核心文件"


# ---------- 契约 2：重启成功 → 让位给 execv ----------

def test_restart_success_makes_loop_yield(monkeypatch):
    """execv 已替换进程时 _watch_loop 直接返回，不再重复扫描。"""
    err, calls, fs, _ = _run_watch_loop(
        monkeypatch, core_versions=[1, 2, 2, 3, 3], restart_results=[True])
    assert err is None
    assert len(calls) == 1, "重启成功后守护还在跑（execv 之后不该有第二次）"
    assert fs.core_snapshots == 3, (
        "快照次数不对：初始 + 变化检测 + 合并窗口确认 = 3，实际 %d" % fs.core_snapshots)


# ---------- 契约 3：_restart_process 的返回值语义 ----------

def test_restart_process_skips_execv_on_bad_syntax(monkeypatch):
    proxy = _OsProxy()
    monkeypatch.setattr(hr, "_safe_to_compile", lambda paths: False)
    monkeypatch.setattr(hr, "os", proxy)
    assert hr._restart_process([Path("/fake/core_x.py")], "bad") is False
    assert proxy.execv_calls == [], "语法没过却 execv 了 —— 服务可能起不来"


def test_restart_process_execv_replaces_process(monkeypatch):
    proxy = _OsProxy()
    monkeypatch.setattr(hr, "_safe_to_compile", lambda paths: True)
    monkeypatch.setattr(hr, "os", proxy)
    assert hr._restart_process([Path("/fake/core_x.py")], "ok") is True
    assert proxy.execv_calls == [
        (sys.executable, [sys.executable, str(hr.BASE_DIR / "server.py")])
    ]


def test_restart_process_propagates_execv_failure(monkeypatch):
    """execv 失败必须冒泡，不能返回 True 假装重启成功。"""
    proxy = _OsProxy(raises=True)
    monkeypatch.setattr(hr, "_safe_to_compile", lambda paths: True)
    monkeypatch.setattr(hr, "os", proxy)
    with pytest.raises(OSError):
        hr._restart_process([Path("/fake/core_x.py")], "boom")


# ---------- 真身编译检查（端到端，只换掉 os） ----------

def test_safe_to_compile_real_files(tmp_path):
    broken = tmp_path / "broken_core.py"
    broken.write_text("def f(:\n    pass\n", encoding="utf-8")
    assert hr._safe_to_compile([broken]) is False

    good = tmp_path / "good_core.py"
    good.write_text("x = 1\n", encoding="utf-8")
    assert hr._safe_to_compile([good]) is True


def test_restart_process_end_to_end_with_real_compile(tmp_path, monkeypatch):
    """真 _safe_to_compile + 真 _restart_process：语法错不 execv，改好后 execv。"""
    proxy = _OsProxy()
    monkeypatch.setattr(hr, "os", proxy)

    broken = tmp_path / "broken_e2e.py"
    broken.write_text("def f(:\n", encoding="utf-8")
    assert hr._restart_process([broken], "broken") is False
    assert proxy.execv_calls == []

    good = tmp_path / "good_e2e.py"
    good.write_text("x = 1\n", encoding="utf-8")
    assert hr._restart_process([good], "good") is True
    assert len(proxy.execv_calls) == 1


# ---------- 自检：测试本身能不能抓到旧 bug ----------

def test_regression_guard_catches_old_bug(monkeypatch):
    """把失败分支改回旧行为（return），本套测试必须能抓到。

    测测试本身很容易退化成同义反复（断言只描述了实现）。这里真去加载一份
    「旧行为模块」，跑同一时序：旧行为下重启只会被调 1 次（守护已死）。
    源码锚点变了就显式 skip —— 宁可跳过，也不假装通过。
    """
    src = (BASE / "hot_reload.py").read_text(encoding="utf-8")
    needle = ("                # 这里直接 return 会让守护线程永久退出，"
              "之后所有核心改动静默不生效。\n"
              "                continue\n")
    if needle not in src:
        pytest.skip("hot_reload.py 的失败分支结构已变，反例锚点需同步")

    tmp = Path(tempfile.mkdtemp()) / "hr_broken_mod.py"
    tmp.write_text(src.replace(needle, needle.replace("continue", "return")),
                   encoding="utf-8")
    spec = importlib.util.spec_from_file_location("hr_broken_mod", tmp)
    broken = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(broken)

    monkeypatch.setattr(sys.modules[__name__], "hr", broken)
    err, calls, fs, _ = _run_watch_loop(
        monkeypatch, core_versions=[1, 2, 2, 3, 3], restart_results=[False, True])

    assert err is None
    assert len(calls) == 1, (
        "旧行为（return）下重启被调了 %d 次 —— 驱动逻辑没走到失败分支，"
        "本套测试对旧 bug 实际是盲的" % len(calls)
    )



# ---------- 轮内保护：active_turns()==0 不等于「可以重启」 ----------
#
# 背景（2026-09-14 19:33）：写 agent.py 后 39 秒进程就 execv 自替换，而
# RESTART_DEFER_MAX 是 1800 秒 —— 说明那一刻 _active_turns_running() 判成了
# 「空闲」。两个漏洞都在判据本身，不在延迟逻辑：
#   1. agent.py 的 _turn_end() 在 finally 首行，落库/清断点还在后面跑，
#      计数归零的瞬间 execv 会把收尾连同正文一起丢（用户看到「没有产出正文」）；
#   2. except Exception: return False 把「判不出来」当成了「空闲」。
# 修法：TURN_END_GRACE 宽限期 + 异常分支保守返回 True。


def _install_fake_agent(monkeypatch, count: int):
    """替身 agent 模块：active_turns 由返回的 state 字典驱动。"""
    import types
    mod = types.ModuleType("agent")
    state = {"n": count}
    mod.active_turns = lambda: state["n"]
    monkeypatch.setitem(sys.modules, "agent", mod)
    return state


def test_active_turn_blocks_restart(monkeypatch):
    monkeypatch.setattr(hr, "_last_turn_active_ts", 0.0)
    _install_fake_agent(monkeypatch, 1)
    assert hr._active_turns_running() is True
    assert hr._last_turn_active_ts > 0, "观测到活跃轮必须刷新时间戳"


def test_turn_end_grace_covers_persist_window(monkeypatch):
    """★ 计数刚归零仍算忙：收尾/落库还在 finally 里跑，这时重启会丢正文。"""
    import time
    monkeypatch.setattr(hr, "_last_turn_active_ts", time.time())
    _install_fake_agent(monkeypatch, 0)
    assert hr._active_turns_running() is True


def test_turn_end_grace_expires(monkeypatch):
    """宽限期过后必须放行，否则改了核心代码永远不生效。"""
    import time
    monkeypatch.setattr(hr, "_last_turn_active_ts", time.time() - hr.TURN_END_GRACE - 1)
    _install_fake_agent(monkeypatch, 0)
    assert hr._active_turns_running() is False


def test_unknown_state_treated_as_busy(monkeypatch):
    """判不出来时按忙处理：延迟重启只是晚几秒，误判空闲是不可逆的。"""
    monkeypatch.setitem(sys.modules, "agent", None)
    assert hr._active_turns_running() is True
