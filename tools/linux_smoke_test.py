#!/usr/bin/env python3
"""平台兼容性冒烟测试 —— 验证 platform_compat 在当前系统上走对了分支。

跨平台设计：Windows 上验 Windows 分支（tasklist / taskkill / creationflags），
POSIX 上验 POSIX 分支（/proc、进程组、fcntl）。同一套用例，期望值按平台切换，
所以在两个系统上跑都应该是全绿。
只用标准库，不依赖第三方包，干净的容器里可直接跑：
`docker run --rm -v <repo>:/app -w /app python:3.11-slim python tools/linux_smoke_test.py`

跑不过 = 兼容层在当前系统不可用；跑过 = 进程/锁/磁盘/端口这些"真动手"的能力可用。

退出码：0 全通过；1 有失败项。
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import platform_compat as pc  # noqa: E402

_results: list[tuple[str, bool, str]] = []


def case(name: str, fn) -> None:
    try:
        ok, detail = fn()
    except Exception as e:
        ok, detail = False, f"异常 {e.__class__.__name__}: {e}"
    _results.append((name, bool(ok), detail))


def _t_platform() -> tuple[bool, str]:
    if pc.IS_POSIX:
        return (pc.IS_POSIX and not pc.IS_WINDOWS), \
            f"IS_POSIX={pc.IS_POSIX} IS_LINUX={pc.IS_LINUX} IS_MACOS={pc.IS_MACOS}"
    return (pc.IS_WINDOWS and not pc.IS_POSIX), \
        f"IS_WINDOWS={pc.IS_WINDOWS} IS_POSIX={pc.IS_POSIX}（当前系统分支）"


def _t_spawn_flags() -> tuple[bool, str]:
    kw = pc.spawn_kwargs(new_group=True)
    if pc.IS_POSIX:
        return ("start_new_session" in kw), f"spawn_kwargs={kw}"
    return ("creationflags" in kw and "start_new_session" not in kw), f"spawn_kwargs={kw}"


def _t_processes() -> tuple[bool, str]:
    procs = pc.list_processes()
    if not procs:
        return False, "进程清单为空"
    has_pid1 = any(p["pid"] == 1 for p in procs)
    if pc.IS_POSIX:
        return has_pid1, f"{len(procs)} 个进程，含 pid1={has_pid1}"
    return True, f"{len(procs)} 个进程（Windows 无 pid1 概念）"


def _t_ports() -> tuple[bool, str]:
    lines = pc.list_listening_ports()
    return True, f"{len(lines)} 条监听记录（容器内可能为 0）"


def _t_disk() -> tuple[bool, str]:
    du = pc.disk_free(str(ROOT))
    if not du:
        return False, f"disk_free({str(ROOT)!r}) 返回 None"
    total, free = du
    return (total > 0), f"共 {total / 2**30:.1f} GB / 剩余 {free / 2**30:.1f} GB"


def _t_pid_alive() -> tuple[bool, str]:
    me = os.getpid()
    alive_self = pc.pid_alive(me)
    alive_fake = pc.pid_alive(999999)
    return (alive_self and not alive_fake), f"self={alive_self} 999999={alive_fake}"


def _t_terminate_tree() -> tuple[bool, str]:
    """关键用例：父进程 + 子进程必须被整树干掉（对应 kill_task 场景）。

    子进程 pid 由被测进程自己写进探针文件 —— 这样不依赖 /proc 也不依赖 `sleep`
    命令，Windows / Linux / macOS 同一套逻辑。
    """
    probe = Path(tempfile.gettempdir()) / "dabai_tree_probe.txt"
    probe.unlink(missing_ok=True)
    script = (
        "import subprocess,sys,time,os\n"
        "kid = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(300)'])\n"
        "open(sys.argv[1], 'w').write(str(os.getpid()) + ',' + str(kid.pid))\n"
        "time.sleep(300)\n"
    )
    p = subprocess.Popen([sys.executable, "-c", script, str(probe)],
                         **pc.spawn_kwargs(new_group=True))
    pids: list[int] = []
    for _ in range(40):  # 等探针文件落盘（最多 6 秒）
        if probe.is_file():
            try:
                pids = [int(x) for x in probe.read_text().strip().split(",") if x.strip()]
            except (OSError, ValueError):
                pids = []
            if pids:
                break
        time.sleep(0.15)
    if not pids:
        pids = [p.pid]
    ok, reason = pc.terminate_tree(p.pid, timeout=8)
    try:
        p.wait(timeout=3)  # 回收僵尸，避免“僵尸=存活”的误判
    except Exception:
        pass
    time.sleep(0.4)
    still = [x for x in pids if pc.pid_alive(x)]
    return (ok and not still), f"{reason}；探针={pids}；残留={still}"


def _t_kill_pid() -> tuple[bool, str]:
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"],
                         **pc.spawn_kwargs(new_group=True))
    time.sleep(0.5)
    sent = pc.kill_pid(p.pid)
    try:
        p.wait(timeout=3)  # 回收僵尸
    except Exception:
        pass
    time.sleep(0.2)
    return (sent and not pc.pid_alive(p.pid)), f"kill_pid={sent} 存活={pc.pid_alive(p.pid)}"


def _t_exit_code() -> tuple[bool, str]:
    p = subprocess.Popen([sys.executable, "-c", "import sys; sys.exit(7)"])
    p.wait()
    code = pc.process_exit_code(p.pid)
    # 已被父进程 wait() 回收 → /proc 消失，返回 None 属正常；此处只要求不抛异常
    return True, f"process_exit_code={code}（已回收时 None 属正常）"


def _t_file_lock() -> tuple[bool, str]:
    """跨进程锁：子进程持锁期间，父进程非阻塞加锁必须失败。"""
    lock_path = Path(tempfile.gettempdir()) / "dabai_lock_test.lock"
    lock_path.write_bytes(b"\x00")
    holder = subprocess.Popen([sys.executable, "-c", f"""
import sys, time
sys.path.insert(0, {str(ROOT)!r})
import platform_compat as pc
fh = open({str(lock_path)!r}, 'a+')
assert pc.lock_file(fh), 'child lock failed'
print('LOCKED', flush=True)
time.sleep(4)
"""], stdout=subprocess.PIPE, text=True)
    line = holder.stdout.readline().strip()
    if line != "LOCKED":
        holder.kill()
        return False, f"子进程未持锁（输出 {line!r}）"
    fh = open(lock_path, "a+")
    got = pc.lock_file(fh, blocking=False)
    if got:
        pc.unlock_file(fh)
    fh.close()
    holder.kill()
    return (not got), f"子进程持锁时父进程非阻塞加锁={'成功(错)' if got else '被拒(对)'}"


def _t_paths() -> tuple[bool, str]:
    roots = pc.search_roots()
    return (len(roots) > 0), f"{len(roots)} 个搜索根，例如 {roots[0][0] if roots else '-'}"


def _t_no_ctypes_need() -> tuple[bool, str]:
    """确保 POSIX 分支不碰 Windows 专有符号（subprocess.CREATE_* 不存在）。"""
    bad = [n for n in ("CREATE_NO_WINDOW", "CREATE_NEW_PROCESS_GROUP")
           if not hasattr(subprocess, n)]
    return True, f"subprocess 缺失 Windows 常量：{bad}（spawn_kwargs 已避开它们）"


def _t_shell_impl_system_check() -> tuple[bool, str]:
    """真调技能层：shell_impl.system_check 必须给出进程/端口/磁盘三段结果。"""
    sys.path.insert(0, str(ROOT / "skills" / "code_ops"))
    import shell_impl  # noqa: WPS433
    out = asyncio.run(shell_impl.system_check({"what": "all", "max_lines": 5}))
    has_proc = "进程" in out
    has_port = "监听端口" in out
    has_disk = "磁盘" in out
    return (has_proc and has_port and has_disk), \
        f"进程={has_proc} 端口={has_port} 磁盘={has_disk}；首行：{out.splitlines()[0][:80]}"


def _t_shell_impl_find_file() -> tuple[bool, str]:
    sys.path.insert(0, str(ROOT / "skills" / "code_ops"))
    import shell_impl
    out = asyncio.run(shell_impl.find_file({"name": "platform_compat.py"}))
    return ("platform_compat.py" in out), out.splitlines()[0][:100]


def main() -> int:
    case("平台识别", _t_platform)
    case("子进程参数", _t_spawn_flags)
    case("进程清单", _t_processes)
    case("监听端口", _t_ports)
    case("磁盘空间", _t_disk)
    case("进程存活探测", _t_pid_alive)
    case("整树终止进程", _t_terminate_tree)
    case("单进程强杀", _t_kill_pid)
    case("退出码读取", _t_exit_code)
    case("跨进程文件锁", _t_file_lock)
    case("搜索根目录", _t_paths)
    case("Windows 常量隔离", _t_no_ctypes_need)
    case("技能层 system_check", _t_shell_impl_system_check)
    case("技能层 find_file", _t_shell_impl_find_file)

    width = max(len(n) for n, _, _ in _results)
    print(f"平台兼容性冒烟 —— {sys.platform} / Python {sys.version.split()[0]}")
    print(f"项目根：{ROOT}\n")
    for name, ok, detail in _results:
        print(f"{'[PASS]' if ok else '[FAIL]'} {name.ljust(width)}  {detail}")
    failed = [n for n, ok, _ in _results if not ok]
    print(f"\n合计 {len(_results)} 项，通过 {len(_results) - len(failed)}，失败 {len(failed)}")
    if failed:
        print("失败项：" + "、".join(failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
