"""跨平台兼容层 —— 大白在 Windows / Linux / macOS 上的唯一平台出口。

设计原则（第一性原理）：
- 平台差异只允许存在于这一个文件。业务代码一律通过本模块调用，
  不再自己写 ``os.name == 'nt'`` 分支（历史分支已逐步收敛到这里）。
- Windows 行为与改造前保持等价（零回归）；POSIX 给出语义等价实现；
  确实无等价实现的，明确降级并返回可诊断信息，绝不让调用方
  拿到一个"看起来成功、实际没生效"的静默结果。
- 只依赖标准库，不引入第三方包。

对外接口速查：
    进程：pid_alive / process_exit_code / kill_pid / terminate_tree / list_processes
    系统：list_listening_ports / disk_free / find_executable
    路径：home / user_dir / search_roots / project_root
    子进程：no_window_flags / spawn_kwargs
    文件锁：lock_file / unlock_file
    控制台：console_utf8
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Iterable, Optional

IS_WINDOWS = os.name == "nt"
IS_POSIX = not IS_WINDOWS
IS_MACOS = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")

__all__ = [
    "IS_WINDOWS", "IS_POSIX", "IS_MACOS", "IS_LINUX",
    "project_root", "home", "user_dir", "search_roots", "browse_roots",
    "no_window_flags", "spawn_kwargs",
    "pid_alive", "process_exit_code", "kill_pid", "terminate_tree", "list_processes",
    "list_listening_ports", "disk_free", "find_executable",
    "lock_file", "unlock_file", "console_utf8",
]


# ---------------------------------------------------------------- 路径 / 目录

def project_root() -> Path:
    """大白项目根目录（本文件所在目录）。"""
    return Path(__file__).resolve().parent


def home() -> Path:
    return Path(os.path.expanduser("~"))


# 用户常见目录：Windows 用固定英文名，Linux/macOS 优先 XDG / 本地化名
_USER_DIR_WIN = {
    "desktop": "Desktop", "downloads": "Downloads", "videos": "Videos",
    "music": "Music", "documents": "Documents", "pictures": "Pictures",
}
_USER_DIR_XDG = {
    "desktop": "DESKTOP", "downloads": "DOWNLOAD", "videos": "VIDEOS",
    "music": "MUSIC", "documents": "DOCUMENTS", "pictures": "PICTURES",
}


def user_dir(kind: str) -> Optional[Path]:
    """返回用户目录（desktop/downloads/videos/music/documents/pictures），不存在则 None。"""
    kind = (kind or "").strip().lower()
    if IS_WINDOWS:
        p = home() / _USER_DIR_WIN.get(kind, kind)
        return p if p.is_dir() else None
    # POSIX：先看 XDG 用户目录配置，再看本地化名，最后看英文名
    cfg = home() / ".config" / "user-dirs.dirs"
    env_key = _USER_DIR_XDG.get(kind)
    if cfg.is_file() and env_key:
        try:
            for line in cfg.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if line.startswith(f"XDG_{env_key}_DIR"):
                    raw = line.split("=", 1)[1].strip().strip('"')
                    raw = raw.replace("$HOME", str(home()))
                    p = Path(raw)
                    if p.is_dir():
                        return p
        except OSError:
            pass
    candidates = []
    if IS_MACOS:
        candidates = [home() / name for name in ("Desktop", "Downloads", "Movies",
                                                 "Music", "Documents", "Pictures")]
        idx = {"desktop": 0, "downloads": 1, "videos": 2, "music": 3,
               "documents": 4, "pictures": 5}.get(kind)
        if idx is not None:
            p = candidates[idx]
            return p if p.is_dir() else None
        return None
    # Linux：xdg-user-dir 命令最准，其次本地化常见名，最后英文名
    try:
        r = subprocess.run(["xdg-user-dir", kind.upper()], capture_output=True,
                           text=True, timeout=5)
        if r.returncode == 0:
            p = Path(r.stdout.strip())
            if p.is_dir():
                return p
    except (OSError, subprocess.SubprocessError):
        pass
    localized = {
        "desktop": ("桌面",), "downloads": ("下载",), "videos": ("视频", "影片"),
        "music": ("音乐", "音樂"), "documents": ("文档", "文件"), "pictures": ("图片", "圖片"),
    }.get(kind, ())
    for name in localized + (kind.capitalize(), kind.title()):
        p = home() / name
        if p.is_dir():
            return p
    return None


def search_roots(extra: Iterable[str] = ()) -> list[tuple[str, int]]:
    """文件搜索根目录 + 最大深度（Windows=各盘符，POSIX=家目录/常见挂载点）。"""
    roots: list[tuple[str, int]] = []
    for kind in ("desktop", "downloads", "videos", "music", "documents"):
        p = user_dir(kind)
        if p is not None:
            roots.append((str(p), 4))
    for base in list(extra) + [os.getcwd(), str(project_root())]:
        if base and os.path.isdir(base) and base not in [r[0] for r in roots]:
            roots.append((base, 4))
    if IS_WINDOWS:
        for drv in ("C:\\", "D:\\", "E:\\"):
            if os.path.isdir(drv):
                roots.append((drv, 2))
    else:
        roots.append((str(home()), 4))
        if IS_LINUX:
            for mnt in ("/mnt", "/media", "/opt", "/srv"):
                if os.path.isdir(mnt):
                    roots.append((mnt, 3))
    return roots


def browse_roots() -> list[tuple[str, str]]:
    """可选“起点”目录（path, label）：Windows 为各盘符，POSIX 为主目录 + 常见挂载点。

    与 search_roots 的区别：这里给工作区面板 / 手机端逐级下钻用，
    只给起点，不展开一级子目录、不带搜索深度。
    """
    out: list[tuple[str, str]] = []
    if IS_WINDOWS:
        for drv in ("C:\\", "D:\\", "E:\\", "F:\\"):
            if os.path.isdir(drv):
                out.append((drv, drv.rstrip("\\") + "盘"))
        return out
    h = home()
    if h.is_dir():
        out.append((str(h), "主目录"))
    for mnt in ("/mnt", "/media", "/run/media", "/opt", "/srv"):
        if os.path.isdir(mnt):
            out.append((mnt, mnt))
    return out


# ------------------------------------------------------------ 子进程创建参数

def no_window_flags(extra: int = 0) -> int:
    """隐藏控制台窗口的标志（POSIX 返回 0，等价于无此需求）。"""
    if IS_WINDOWS:
        return getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) | extra
    return 0


def spawn_kwargs(extra_flags: int = 0, new_group: bool = False) -> dict:
    """Popen 的平台参数：Windows 用 creationflags，POSIX 用 start_new_session。"""
    if IS_WINDOWS:
        flags = no_window_flags(extra_flags)
        if new_group:
            flags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        return {"creationflags": flags}
    return {"start_new_session": True} if new_group else {}


# ---------------------------------------------------------------- 进程管理

def pid_alive(pid: int) -> bool:
    """进程是否仍在运行（不创建新进程）。

    注意：Linux 上僵尸进程（Z）视为已退出——进程已经终止，只是父进程还没 wait()。
    不排除僵尸会导致“杀完了还认为活着”，进而反复重试终止或误报任务仍在跑。
    """
    if not pid or pid <= 0:
        return False
    if IS_WINDOWS:
        import ctypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        SYNCHRONIZE = 0x00100000
        h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE, False, int(pid))
        if not h:
            # ERROR_ACCESS_DENIED(5)=进程存在但无权打开；ERROR_INVALID_PARAMETER(87)=不存在
            return ctypes.get_last_error() == 5
        try:
            code = ctypes.c_ulong()
            if not k32.GetExitCodeProcess(h, ctypes.byref(code)):
                return True
            return code.value == 259  # STILL_ACTIVE
        finally:
            k32.CloseHandle(h)
    stat = Path(f"/proc/{int(pid)}/stat")
    if stat.is_file():
        try:
            state = stat.read_text(encoding="utf-8", errors="replace").rsplit(")", 1)[-1].split()[0]
            if state == "Z":
                return False
        except (OSError, IndexError):
            pass
    try:
        os.kill(int(pid), 0)
        return True
    except OSError:
        return False


def process_exit_code(pid: int) -> Optional[int]:
    """读取已退出进程的退出码；仍在运行或无法读取时返回 None。"""
    if not pid or pid <= 0:
        return None
    if IS_WINDOWS:
        import ctypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        h = k32.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return None
        try:
            code = ctypes.c_ulong()
            if not k32.GetExitCodeProcess(h, ctypes.byref(code)):
                return None
            v = code.value
            return None if v == 259 else v
        finally:
            k32.CloseHandle(h)
    # POSIX：僵尸进程可读 /proc/<pid>/stat 的退出码；已回收则无法获取（返回 None）
    stat = Path(f"/proc/{int(pid)}/stat")
    if stat.is_file():
        try:
            fields = stat.read_text(encoding="utf-8", errors="replace").rsplit(")", 1)[-1].split()
            return int(fields[1]) if len(fields) > 1 else None
        except (OSError, ValueError, IndexError):
            return None
    return None


def kill_pid(pid: int, force: bool = True) -> bool:
    """终止单个进程（不含子进程）。返回是否已发出终止信号。"""
    if not pid or pid <= 0:
        return False
    if IS_WINDOWS:
        import ctypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        h = k32.OpenProcess(0x0001, False, int(pid))  # PROCESS_TERMINATE
        if not h:
            return False
        try:
            return bool(k32.TerminateProcess(h, 1))
        finally:
            k32.CloseHandle(h)
    import signal
    try:
        os.kill(int(pid), signal.SIGKILL if force else signal.SIGTERM)
        return True
    except OSError:
        return False


def terminate_tree(pid: int, timeout: int = 15) -> tuple[bool, str]:
    """整树终止进程（Windows: taskkill /T /F；POSIX: 先进程组后单进程兜底）。

    返回 (是否成功, 说明)。POSIX 上优先 killpg（配合 start_new_session=True 的
    独立进程组），失败再逐层 kill，最后 SIGKILL 兜底。
    """
    if not pid or pid <= 0:
        return False, "pid 无效"
    if IS_WINDOWS:
        try:
            r = subprocess.run(["taskkill", "/PID", str(int(pid)), "/T", "/F"],
                               capture_output=True, timeout=timeout)
            ok = r.returncode == 0
            if ok:
                return True, "taskkill /T /F 成功"
            out = (r.stderr or r.stdout or b"").decode("utf-8", "replace").strip()
            # 进程可能已自然退出，按"已不存在"视为成功
            if not pid_alive(int(pid)):
                return True, f"进程已退出（taskkill: {out or 'no such process'}）"
            return False, f"taskkill 失败：{out or '未知原因'}"
        except (OSError, subprocess.SubprocessError) as e:
            return False, f"taskkill 异常：{e.__class__.__name__}: {e}"
    import signal
    import time
    pid = int(pid)
    sent = False
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
        sent = True
    except OSError:
        pass
    if not sent:
        try:
            os.kill(pid, signal.SIGTERM)
            sent = True
        except OSError as e:
            return (True, "进程已退出") if not pid_alive(pid) else (False, f"终止失败：{e}")
    deadline = time.time() + max(1, timeout)
    while time.time() < deadline:
        if not pid_alive(pid):
            return True, "SIGTERM 终止成功"
        time.sleep(0.2)
    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL)
    except OSError:
        pass
    kill_pid(pid)
    time.sleep(0.3)
    return (True, "SIGKILL 兜底终止成功") if not pid_alive(pid) else (False, "进程仍存活（可能需 root 权限）")


def list_processes() -> list[dict]:
    """进程清单：[{pid, name, cmdline}]。Windows 用 tasklist，POSIX 用 ps。/proc 优先。"""
    out: list[dict] = []
    if IS_WINDOWS:
        try:
            r = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True,
                               timeout=30, creationflags=no_window_flags())
            text = r.stdout.decode("gbk", "replace")
        except (OSError, subprocess.SubprocessError):
            return out
        import csv as _csv
        import io
        for row in _csv.reader(io.StringIO(text)):
            if len(row) >= 2 and row[1].strip().isdigit():
                out.append({"pid": int(row[1]), "name": row[0], "cmdline": ""})
        return out
    # Linux：直接读 /proc（不依赖 procps，最小容器/精简系统里也有）
    proc_root = Path("/proc")
    if proc_root.is_dir():
        for entry in proc_root.iterdir():
            if not entry.name.isdigit():
                continue
            pid = int(entry.name)
            name = cmdline = ""
            try:
                name = (entry / "comm").read_text(encoding="utf-8", errors="replace").strip()
            except OSError:
                pass
            try:
                raw = (entry / "cmdline").read_bytes()
                cmdline = raw.replace(b"\x00", b" ").decode("utf-8", "replace").strip()
            except OSError:
                pass
            if not name and not cmdline:
                continue
            out.append({"pid": pid, "name": name or cmdline.split(" ")[0],
                        "cmdline": cmdline})
        if out:
            return out
    # 其它 POSIX（macOS / 无 /proc 的 Linux）：ps 一次拿全
    try:
        r = subprocess.run(["ps", "-eo", "pid=,comm=,args="], capture_output=True,
                           text=True, timeout=30)
        for line in r.stdout.splitlines():
            parts = line.strip().split(None, 2)
            if len(parts) >= 2 and parts[0].isdigit():
                out.append({"pid": int(parts[0]), "name": parts[1],
                            "cmdline": parts[2] if len(parts) > 2 else ""})
    except (OSError, subprocess.SubprocessError):
        pass
    return out


def list_listening_ports() -> list[str]:
    """监听中的端口原始行（Windows: netstat -ano -n；POSIX: ss/netstat）。"""
    if IS_WINDOWS:
        try:
            r = subprocess.run(["netstat", "-ano", "-n"], capture_output=True,
                               timeout=30, creationflags=no_window_flags())
            text = r.stdout.decode("gbk", "replace")
        except (OSError, subprocess.SubprocessError):
            return []
        return [ln.strip() for ln in text.splitlines() if "LISTENING" in ln]
    for cmd in (["ss", "-ltnp"], ["netstat", "-ltnp"], ["netstat", "-an"]):
        if not find_executable(cmd[0]):
            continue
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            continue
        lines = [ln.strip() for ln in (r.stdout or "").splitlines() if ln.strip()]
        head = [ln for ln in lines if ln.lower().startswith(("netid", "proto", "state", "active"))]
        body = [ln for ln in lines if ("LISTEN" in ln.upper() or "UNCONN" not in ln)]
        return head[:1] + [ln for ln in body if ln not in head]
    return []


def disk_free(path: str) -> Optional[tuple[int, int]]:
    """磁盘 (总字节, 可用字节)；失败返回 None。全平台走 shutil，无 ctypes 依赖。"""
    try:
        u = shutil.disk_usage(path)
        return int(u.total), int(u.free)
    except (OSError, ValueError):
        return None


def find_executable(name: str) -> Optional[str]:
    """在 PATH 中定位可执行程序（Windows 会按 PATHEXT 补 .exe/.cmd/.bat）。"""
    return shutil.which(name)


# ------------------------------------------------------------------ 文件锁

def lock_file(fh, blocking: bool = True) -> bool:
    """对已打开的二进制/文本文件加跨进程排它锁。返回是否成功加锁。"""
    if fh is None:
        return False
    try:
        if IS_WINDOWS:
            import msvcrt
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except Exception:
        return False


def unlock_file(fh) -> bool:
    """释放 lock_file 加的锁。"""
    if fh is None:
        return False
    try:
        if IS_WINDOWS:
            import msvcrt
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------- 控制台

def console_utf8() -> None:
    """让控制台输出 UTF-8 容错（Windows 默认 GBK 会在 emoji 上崩；POSIX 无需处理）。"""
    if not IS_WINDOWS:
        return
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
