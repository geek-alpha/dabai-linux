"""大白 热更新守护（hot reload）

两类监听，行为不同（由 server.py 在启动时通过 start_hot_reload 接入）：

- 核心代码（项目根目录 *.py + harness/*.py）变化   → 默认只记日志（不重启），
  settings.json 的 harness.core_autorestart=true 时才自动整进程重启
- 技能 / 插件（skills/、plugins/ 内的 .py/.json/.md）→ 自动热重载（reload_all +
  agent 工具刷新），服务不重启、WebSocket 不断连

因此：
- 改技能/插件    → 1 秒内自动生效，无需刷新、无需管理台点「重载」
- 改核心 Python  → 默认不动服务（开发时不被打断），下次手动重启生效；
  需要「改完即生效」时把 harness.core_autorestart 设为 true
- 改 web/ 前端   → 由 server.py 的 no-store 静态托管保证刷新即生效

开关：settings.json 的 harness.hot_reload（默认开启）；设为 false 可禁用本守护。
"""
from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger("hot_reload")

BASE_DIR = Path(__file__).resolve().parent

# 已加载快照落盘：供 tools/reload_check.py 判断「改动是否真进了运行中的进程」。
# 不能拿进程启动时间当判据——自动重启走 os.execv 自替换，PID 与 starttime 都不变。
LOADED_STATE_FILE = BASE_DIR / "data" / "hot_reload_state.json"

POLL_INTERVAL = 1.0        # 目录扫描间隔（秒）
SETTLE_TIME = 0.5          # 检测到变化后等待的稳定时间（防编辑器半写状态）
EXT_EXTS = {".py", ".json", ".md"}   # 技能/插件文件类型（SKILL.md/skill.json/skill.py）
_SKIP_DIRS = {"__pycache__", ".git", "node_modules", ".venv", "dist", "build"}
RESTART_DEFER_INTERVAL = 5.0   # 有编程任务运行时，延迟重启的轮询间隔（秒）
RESTART_DEFER_MAX = 1800.0     # 最多延迟 30 分钟；超时后强制重启（防任务永久悬挂拖死热更新）
COALESCE_WINDOW = 8.0          # 合并节流：核心文件最后一次变化后等 8 秒无新变化才重启（多智能体并发写不互相打断）
COALESCE_MAX = 60.0            # 合并节流硬上限：从首个变化起最多收集 60 秒，之后强制重启（防持续写入饿死更新）
EXT_COALESCE_WINDOW = 2.0      # 技能/插件热重载的合并窗口（秒）
EXT_COALESCE_MAX = 30.0        # 技能/插件热重载收集硬上限（秒）
TURN_END_GRACE = 30.0          # 对话轮结束后仍视为「忙」的宽限期（秒）：覆盖收尾/落库窗口
_last_turn_active_ts = 0.0     # 最近一次观测到「有活跃对话轮」的时间戳（进程启动为 0 = 空闲）


# ---------- 扫描快照 ----------

def _file_sig(p: Path) -> tuple[int, int]:
    st = p.stat()
    return (st.st_mtime_ns, st.st_size)


def _snapshot(paths: list[Path]) -> dict[str, tuple[int, int]]:
    out: dict[str, tuple[int, int]] = {}
    for p in paths:
        try:
            out[str(p)] = _file_sig(p)
        except OSError:
            pass
    return out


def _changed(old: dict, new: dict) -> dict[str, tuple[int, int]]:
    return {k: v for k, v in new.items() if old.get(k) != v}


def _scan_core() -> list[Path]:
    """核心代码：根目录全部 *.py + harness/*.py（skills/plugins 不算核心）。"""
    files: list[Path] = [p for p in BASE_DIR.glob("*.py") if p.name not in ("__init__.py",)]
    h = BASE_DIR / "harness"
    if h.is_dir():
        files += [p for p in h.glob("*.py")]
    return files


def _scan_ext() -> list[Path]:
    """扩展代码：skills/、plugins/ 下的 .py/.json/.md（含子目录）+ 根目录 tools.json。"""
    out: list[Path] = []
    tj = BASE_DIR / "tools.json"
    if tj.is_file():
        out.append(tj)  # 工具定义变化也触发 agent 工具刷新
    for sub in ("skills", "plugins"):
        d = BASE_DIR / sub
        if not d.is_dir():
            continue
        for p in d.rglob("*"):
            if p.is_file() and p.suffix in EXT_EXTS \
                    and not any(part in _SKIP_DIRS for part in p.parts):
                out.append(p)
    return out


# ---------- 核心变化 → 自动重启 ----------

def _safe_to_compile(changed_paths: list[Path]) -> bool:
    """重启前编译检查：改动文件语法必须能过，否则不重启（防止服务起不来）。"""
    try:
        import py_compile
        import tempfile
        for p in changed_paths:
            if p.suffix == ".py" and p.exists():
                # 编译产物写到系统临时目录，避免在项目根目录堆积 *.pyc-check 垃圾文件
                cfile = os.path.join(
                    tempfile.gettempdir(),
                    "dabai_pycheck_" + p.stem + ".pyc",
                )
                py_compile.compile(str(p), doraise=True, cfile=cfile)
    except Exception as e:
        logger.error("核心文件语法检查未通过，跳过自动重启: %s", e)
        return False
    return True


def _busy_work_running() -> bool:
    """是否有正在执行的、跨重启不可恢复的工作（进程内子智能体）。

    角色自身对话轮（含工具调用）不再延迟热重启：每轮工具执行前会把对话状态
    落盘为断点，重启后由 agent.resume_turn() 自主续跑——验证/迭代不再被
    「延迟 30 分钟重启」卡住。

    codex/opencode 独立进程本身杀不掉、重启后会被任务中心接管恢复，因此这里
    只对进程内执行的子智能体做保护（它们没有跨重启恢复能力）。
    """
    try:
        from sub_agents import get_sub_agents
        if get_sub_agents().active():
            return True
    except Exception:
        pass
    return False


def _active_turns_running() -> bool:
    """角色对话轮（含工具调用）是否正在执行。

    大白自己在工具执行中修改核心代码（agent.py/server.py/harness 等）时，
    不能立刻重启——否则正在跑的工具被掐断、前端闪现『已连接到 AI』、
    执行链路被打断。这里把重启推迟到本轮对话结束（由断点续跑兜底保底）。

    两处防漏（实测踩过：19:33:32 写 agent.py → 19:34:11 就 execv，39 秒的
    窗口里 active_turns() 已归零，保护形同虚设）：
    - 计数归零不等于「可以重启」：agent.py 的 _turn_end() 在 finally 首行，
      落库/清断点还在后面跑，此时 execv 会把收尾连同正文一起丢。用
      TURN_END_GRACE 宽限期兜住这段尾巴。
    - 判不出来时按「忙」处理：延迟重启只是晚几秒生效，误判空闲却是不可逆的。
    """
    global _last_turn_active_ts
    try:
        from agent import active_turns
        n = active_turns()
    except Exception:
        return True
    if n > 0:
        _last_turn_active_ts = time.time()
        return True
    return (time.time() - _last_turn_active_ts) < TURN_END_GRACE


def _restart_process(changed_paths: list[Path], reason: str) -> bool:
    """Returns: True=已 execv 自替换（不返回）；False=语法检查未过，本次跳过。"""
    if not _safe_to_compile(changed_paths):
        return False
    logger.warning("核心代码变化（%s）→ 自动重启服务进程…", reason)
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except Exception:
        pass
    os.chdir(str(BASE_DIR))
    # 自替换进程：新进程重新加载全部模块，等价于 uvicorn --reload，且避开 Windows
    # multiprocessing spawn 的坑（无需 import 字符串、无双重初始化）。
    # Windows 的 os.execv 不会自动引用含空格的路径（如 "TRAE SOLO CN"），
    # 会拆成多个参数导致重启失败 → 用 subprocess 启动新进程后退出当前进程。
    if os.name == "nt":
        import subprocess
        subprocess.Popen([sys.executable, str(BASE_DIR / "server.py")],
                         cwd=str(BASE_DIR))
        os._exit(0)
    os.execv(sys.executable, [sys.executable, str(BASE_DIR / "server.py")])
    return True  # 不可达：execv 成功即替换当前进程


def _dump_loaded_state(core_snap: dict, ext_snap: dict) -> None:
    """把「本进程启动时加载的代码版本」落盘，作为 reload_check 的生效判据。

    只在守护启动时写一次：此刻快照 = 磁盘现状 = 进程刚 import 的版本。检测到 core
    变化后虽然会更新内存快照，但那份改动还没进内存里的模块（重启才生效），跟着写
    会把「未生效」记成「已生效」。
    """
    try:
        LOADED_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "pid": os.getpid(),
            "at": time.time(),
            "core": {k: list(v) for k, v in core_snap.items()},
            "ext": {k: list(v) for k, v in ext_snap.items()},
        }
        tmp = LOADED_STATE_FILE.with_name(LOADED_STATE_FILE.name + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, LOADED_STATE_FILE)
    except Exception as e:
        logger.warning("已加载快照落盘失败（不影响热更新）: %s", e)


# ---------- 主循环 ----------

def _watch_loop(on_ext_reload: Optional[Callable[[], None]]) -> None:
    core_snap = _snapshot(_scan_core())
    ext_snap = _snapshot(_scan_ext())
    _dump_loaded_state(core_snap, ext_snap)
    while True:
        time.sleep(POLL_INTERVAL)
        try:
            # 核心变化优先：直接重启（重启后本线程随旧进程消失）
            core_new = _snapshot(_scan_core())
            core_diff = _changed(core_snap, core_new)
            core_snap = core_new
            if core_diff:
                changed_paths = {Path(k) for k in core_diff}
                if not _core_autorestart_enabled():
                    logger.info(
                        "核心代码变化（%s）→ 自动重启已关闭（harness.core_autorestart=false），"
                        "改动在下次手动重启后生效",
                        ", ".join(sorted(p.name for p in changed_paths)),
                    )
                    continue
                # 合并节流：合并窗口内连续写入的多个核心文件只触发一次重启
                # （多个智能体同时改不同核心文件时，不再互相打断：A 的写入不会被 B 的重启掐断）
                last_change = time.time()
                collect_start = time.time()
                deadline = time.time() + RESTART_DEFER_MAX
                while True:
                    time.sleep(POLL_INTERVAL)
                    core_new2 = _snapshot(_scan_core())
                    diff2 = _changed(core_snap, core_new2)
                    if diff2:
                        core_snap = core_new2
                        changed_paths |= {Path(k) for k in diff2}
                        last_change = time.time()
                        continue
                    # 进程内子智能体在执行 → 继续等待（它们无跨重启恢复能力）；
                    # 角色对话轮/工具调用不再等待（断点续跑兜底）
                    if _busy_work_running() and time.time() < deadline:
                        logger.info(
                            "检测到核心代码变化（%s），但有子智能体执行中，延迟重启…",
                            ", ".join(sorted(p.name for p in changed_paths)),
                        )
                        time.sleep(RESTART_DEFER_INTERVAL)
                        continue
                    # AI 自身对话轮（工具执行中改了自己的代码）也不立即重启：
                    # 等本轮跑完再重启，避免『已连接到 AI』式中断执行链路
                    if _active_turns_running() and time.time() < deadline:
                        logger.info(
                            "检测到核心代码变化（%s），但 AI 对话轮执行中，延迟重启…",
                            ", ".join(sorted(p.name for p in changed_paths)),
                        )
                        time.sleep(RESTART_DEFER_INTERVAL)
                        continue
                    if (time.time() - last_change >= COALESCE_WINDOW
                            or time.time() - collect_start >= COALESCE_MAX
                            or time.time() >= deadline):
                        break
                names = ", ".join(sorted(p.name for p in changed_paths))
                time.sleep(SETTLE_TIME)  # 等文件写入完成后再重启
                if _restart_process(sorted(changed_paths), names):
                    return  # 不会到达：execv 已替换当前进程
                # 语法没过：跳过本次，守护必须继续监听（文件改好后 mtime 变化会重新触发）。
                # 这里直接 return 会让守护线程永久退出，之后所有核心改动静默不生效。
                continue

            # 技能/插件变化 → 热重载（不重启）
            ext_new = _snapshot(_scan_ext())
            ext_diff = _changed(ext_snap, ext_new)
            ext_snap = ext_new
            if ext_diff:
                # 合并节流：2 秒窗口内的连续写入合并为一次热重载（多智能体并发写技能不互相刷）
                ext_changed = {Path(k) for k in ext_diff}
                ext_last = time.time()
                ext_start = time.time()
                while True:
                    time.sleep(POLL_INTERVAL)
                    ext_new2 = _snapshot(_scan_ext())
                    diff2 = _changed(ext_snap, ext_new2)
                    if diff2:
                        ext_snap = ext_new2
                        ext_changed |= {Path(k) for k in diff2}
                        ext_last = time.time()
                        continue
                    if (time.time() - ext_last >= EXT_COALESCE_WINDOW
                            or time.time() - ext_start >= EXT_COALESCE_MAX):
                        break
                time.sleep(SETTLE_TIME)
                ext_snap = _snapshot(_scan_ext())  # 重新确认最终内容
                names = ", ".join(sorted(p.name for p in ext_changed))
                logger.info("技能/插件变化（%s）→ 自动热重载…", names)
                if on_ext_reload is not None:
                    try:
                        on_ext_reload()
                    except Exception as e:
                        logger.error("技能/插件自动热重载失败: %s", e)
        except Exception as e:
            logger.warning("热更新扫描异常: %s", e)


def _hot_reload_enabled() -> bool:
    try:
        p = BASE_DIR / "settings.json"
        if p.exists():
            import json
            with open(p, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            h = (cfg or {}).get("harness", {}) or {}
            return bool(h.get("hot_reload", True))
    except Exception:
        pass
    return True


def _core_autorestart_enabled() -> bool:
    """核心代码变化是否自动重启进程：settings.json -> harness.core_autorestart。

    默认关闭（开发体验优先）：改一行核心代码就整进程重启，会打断正在跑的任务、
    掐断前端连接，是大型项目里最影响迭代节奏的行为。关闭后核心改动只记日志，
    下次手动重启服务时生效；需要旧行为时把它设为 true 即可。
    """
    try:
        p = BASE_DIR / "settings.json"
        if p.exists():
            import json
            with open(p, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            h = (cfg or {}).get("harness", {}) or {}
            return bool(h.get("core_autorestart", False))
    except Exception:
        pass
    return False


def start_hot_reload(on_ext_reload: Optional[Callable[[], None]] = None) -> Optional[threading.Thread]:
    """启动热更新守护线程（daemon）。

    Args:
        on_ext_reload: 技能/插件发生变化后调用的回调（server 注入：
                       重载 harness + 刷新各 agent 工具列表）。

    Returns:
        守护线程；settings.json 关闭热更新时返回 None。
    """
    if not _hot_reload_enabled():
        logger.info("热更新守护未启用（settings.json 的 harness.hot_reload=false）")
        return None
    t = threading.Thread(target=_watch_loop, args=(on_ext_reload,),
                         name="hot-reload", daemon=True)
    t.start()
    logger.info(
        "热更新守护已启动：核心代码自动重启 / 技能插件自动热重载 "
        f"（轮询 {POLL_INTERVAL}s）"
    )
    return t
