# -*- coding: utf-8 -*-
"""油管视频汉化流水线技能 —— 大白直接操作用户的 D:/AI/油管视频汉化 项目 CLI。

设计要点（与 harness 底座对齐）：
- 每个工具都是 cli.py（.venv/Scripts/python.exe cli.py）的薄封装，CLI 与 Web
  控制台共享同一份 pipeline/data/state.json，所以状态查询永远准确；
- 调度器（run）是长前台任务，这里改为后台 Popen 启动 + pid 文件记录，
  主 Agent 启动后立刻返回，再用 status/jobs/run_log 轮询进度；
- 所有执行经 asyncio.to_thread + subprocess 超时保护 + 输出截断；
- 环境变量固定 PYTHONUTF8=1，保证中文输出经管道按 UTF-8 解码不乱码。
"""
from __future__ import annotations

import asyncio
import os
import re
import subprocess
import sys
from pathlib import Path

try:
    import platform_compat as pc
except ImportError:  # 技能模块被单独加载时兜底
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))))
    import platform_compat as pc

# 外部汉化项目位置：默认 Windows 路径，其他平台用环境变量 DABAI_HANHUA_ROOT 指定
PROJECT_ROOT = Path(os.environ.get("DABAI_HANHUA_ROOT") or r"D:\AI\油管视频汉化")
CLI = PROJECT_ROOT / "cli.py"
# 虚拟环境解释器：Windows 在 Scripts/，POSIX 在 bin/
PYTHON = (PROJECT_ROOT / ".venv" / "Scripts" / "python.exe") if os.name == "nt" \
    else (PROJECT_ROOT / ".venv" / "bin" / "python3")
PID_FILE = PROJECT_ROOT / "pipeline" / "data" / "_dabai_scheduler.pid"
RUN_LOG = PROJECT_ROOT / "pipeline" / "data" / "_dabai_run.log"
MAX_OUT = 4000
_ANSI = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07]*\x07|\x1b[>()#]")


def _env() -> dict:
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _flags(extra: int = 0) -> int:
    return pc.no_window_flags(extra)


def _cmd(*args) -> list:
    py = str(PYTHON) if PYTHON.exists() else sys.executable
    return [py, str(CLI), *[str(a) for a in args]]


def _unavailable() -> str:
    """外部汉化项目不可用时的统一提示（Linux/macOS 需先设 DABAI_HANHUA_ROOT）。"""
    return (f"汉化技能不可用：未找到外部项目 {PROJECT_ROOT}（需要其 cli.py）。"
            f"Windows 默认路径为 D:\\AI\\油管视频汉化；其他平台请设置环境变量 "
            f"DABAI_HANHUA_ROOT 指向该项目根目录。")


async def _run_cli(args: list, timeout: int = 120) -> str:
    """同步执行一条 cli.py 命令并返回（截断后）输出文本。"""
    if not CLI.is_file():
        return _unavailable()
    try:
        cp = await asyncio.to_thread(
            subprocess.run, _cmd(*args), cwd=str(PROJECT_ROOT),
            env=_env(), capture_output=True, timeout=timeout,
            creationflags=_flags())
    except subprocess.TimeoutExpired:
        return f"⏱ 命令超时（>{timeout}s）已终止：{' '.join(str(a) for a in args)}"
    except Exception as e:
        return f"执行失败：{type(e).__name__}: {e}"
    out = (cp.stdout or b"").decode("utf-8", errors="replace")
    err = (cp.stderr or b"").decode("utf-8", errors="replace")
    if err.strip():
        out = out.rstrip() + ("\n" if out.strip() else "") + "[stderr] " + err.strip()[:1500]
    if cp.returncode != 0:
        out = out.rstrip() + f"\n（注意：命令返回非零退出码 {cp.returncode}，可能执行失败）"
    if len(out) > MAX_OUT:
        out = out[: MAX_OUT - 3] + "..."
    return out


# ---------- 后台调度器 ----------

def _scheduler_pid() -> int | None:
    try:
        return int(PID_FILE.read_text(encoding="utf-8").strip())
    except Exception:
        return None


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _tail(path: Path, nlines: int) -> str:
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return f"读取运行日志失败：{e}"
    text = _ANSI.sub("", raw).rstrip()
    lines = text.splitlines()
    tail = lines[-nlines:] if len(lines) > nlines else lines
    return "\n".join(tail) if tail else "（运行日志为空）"


async def hanhua_run(args: dict) -> str:
    if not CLI.is_file():
        return _unavailable()
    stages = str(args.get("stages") or "").strip()
    pid = _scheduler_pid()
    if pid and _pid_alive(pid):
        return (f"调度器已在运行（PID {pid}），不能重复启动。"
                f"用 hanhua_status / hanhua_jobs / hanhua_run_log 查看进度，"
                f"不需要时用 hanhua_run_stop 停止。")
    cmd = _cmd("run")
    if stages:
        cmd += ["--stages", stages]
    try:
        RUN_LOG.parent.mkdir(parents=True, exist_ok=True)
        logf = open(RUN_LOG, "ab")
        proc = subprocess.Popen(
            cmd, cwd=str(PROJECT_ROOT), env=_env(),
            stdout=logf, stderr=subprocess.STDOUT,
            **pc.spawn_kwargs(new_group=True))
    except Exception as e:
        try:
            logf.close()
        except Exception:
            pass
        return f"启动失败：{type(e).__name__}: {e}"
    try:
        PID_FILE.write_text(str(proc.pid), encoding="utf-8")
    except Exception:
        pass
    return (f"✅ 流水线调度器已后台启动（PID {proc.pid}，日志 {RUN_LOG.name}）。\n"
            f"之后用 hanhua_status / hanhua_jobs 看任务进度、hanhua_run_log 看调度器输出；"
            f"停止用 hanhua_run_stop。队列清空后调度器仍会继续等待新任务,需要时手动停止。")


async def hanhua_run_stop(args: dict) -> str:
    pid = _scheduler_pid()
    if not pid or not _pid_alive(pid):
        return "当前没有由大白启动的调度器在运行（PID 记录缺失或进程已退出）。"
    try:
        ok, reason = await asyncio.to_thread(pc.terminate_tree, pid, 30)
        if not ok:
            return f"停止失败：{reason}"
    except Exception as e:
        return f"停止失败：{type(e).__name__}: {e}"
    try:
        PID_FILE.unlink()
    except Exception:
        pass
    return (f"已停止调度器（PID {pid}，连同其 worker 子进程）。"
            f"已排队的任务不会丢，下次 hanhua_run 会继续认领；心跳残留由下次 run 自动清理。")


async def hanhua_run_log(args: dict) -> str:
    n = max(1, min(int(args.get("lines") or 50), 300))
    return _tail(RUN_LOG, n)


# ---------- 流水线工具 ----------

async def hanhua_status(args: dict) -> str:
    return await _run_cli(["status"], 120)


async def hanhua_jobs(args: dict) -> str:
    cli = ["jobs"]
    stage = str(args.get("stage") or "").strip()
    if stage:
        cli += ["--stage", stage]
    status = str(args.get("status") or "").strip()
    if status:
        cli += ["--status", status]
    limit = int(args.get("limit") or 30)
    cli += ["--limit", max(1, min(limit, 100))]
    if bool(args.get("all", True)):
        cli += ["--all"]
    return await _run_cli(cli, 120)


async def hanhua_log(args: dict) -> str:
    job_id = str(args.get("job_id") or "").strip()
    if not job_id:
        return "错误：job_id 不能为空（hanhua_jobs 可查任务 ID）"
    n = max(1, min(int(args.get("lines") or 80), 300))
    return await _run_cli(["log", job_id, "-n", str(n)], 60)


async def hanhua_download(args: dict) -> str:
    topic = str(args.get("topic") or "").strip()
    if not topic:
        return "错误：topic 不能为空（YouTube 搜索关键词）"
    return await _run_cli(["download", topic], 600)


async def hanhua_add(args: dict) -> str:
    files = [str(f).strip() for f in (args.get("files") or []) if str(f).strip()]
    if not files:
        return "错误：files 不能为空（至少一个视频文件/目录路径）"
    return await _run_cli(["add", *files], 300)


async def hanhua_tasks(args: dict) -> str:
    action = str(args.get("action") or "").strip().lower()
    ids = [str(i).strip() for i in (args.get("job_ids") or []) if str(i).strip()]
    if action not in ("retry", "cancel", "delete"):
        return "错误：action 必须是 retry / cancel / delete"
    if not ids:
        return "错误：job_ids 不能为空"
    cli = [action, *ids]
    if action == "delete" and bool(args.get("force")):
        cli += ["--force"]
    return await _run_cli(cli, 300)


async def hanhua_pause(args: dict) -> str:
    action = str(args.get("action") or "").strip().lower()
    if action not in ("pause", "resume"):
        return "错误：action 必须是 pause / resume"
    return await _run_cli([action], 60)


async def hanhua_config(args: dict) -> str:
    action = str(args.get("action") or "show").strip().lower()
    key = str(args.get("key") or "").strip()
    value = str(args.get("value") or "").strip()
    if action == "set":
        if not key or not value:
            return "错误：set 需要 key 和 value（如 process.model_size / large）"
        return await _run_cli(["config", "set", f"{key}={value}"], 60)
    if action == "get":
        if not key:
            return "错误：get 需要 key（点号路径，如 process.model_size）"
        return await _run_cli(["config", "get", key], 60)
    if action == "path":
        return await _run_cli(["config", "path"], 60)
    return await _run_cli(["config", "show"], 60)


async def hanhua_bgm(args: dict) -> str:
    action = str(args.get("action") or "show").strip().lower()
    cli = ["bgm"]
    if action == "files":
        cli += ["files"]
    elif action == "global":
        cli += ["global"]
        if bool(args.get("clear")):
            cli += ["--clear"]
        else:
            p = str(args.get("path") or "").strip()
            if not p:
                return "错误：bgm global 需要 --path（音频文件）或 --clear"
            cli += ["--path", p]
            v = args.get("volume")
            if v is not None:
                cli += ["--volume", str(v)]
    elif action == "set":
        vk = str(args.get("video_key") or "").strip()
        p = str(args.get("path") or "").strip()
        if not vk or not p:
            return "错误：bgm set 需要 video_key 和 --path"
        cli += ["set", vk, "--path", p]
        v = args.get("volume")
        if v is not None:
            cli += ["--volume", str(v)]
    elif action == "clear":
        vk = str(args.get("video_key") or "").strip()
        if not vk:
            return "错误：bgm clear 需要 video_key"
        cli += ["clear", vk]
    else:
        cli += ["show"]
    return await _run_cli(cli, 60)


async def hanhua_remix(args: dict) -> str:
    video = str(args.get("video") or "").strip()
    if not video:
        return "错误：video 不能为空（成品视频路径）"
    cli = ["remix", video]
    bgm = str(args.get("bgm") or "").strip()
    if bgm:
        cli += ["--bgm", bgm]
    v = args.get("volume")
    if v is not None:
        cli += ["--volume", str(v)]
    out = str(args.get("out") or "").strip()
    if out:
        cli += ["--out", out]
    return await _run_cli(cli, 600)


async def hanhua_doctor(args: dict) -> str:
    cli = ["doctor"]
    if bool(args.get("deep")):
        cli += ["--deep"]
    return await _run_cli(cli, 600 if bool(args.get("deep")) else 300)


HANDLERS = {
    "hanhua_status": hanhua_status,
    "hanhua_jobs": hanhua_jobs,
    "hanhua_log": hanhua_log,
    "hanhua_download": hanhua_download,
    "hanhua_add": hanhua_add,
    "hanhua_run": hanhua_run,
    "hanhua_run_stop": hanhua_run_stop,
    "hanhua_run_log": hanhua_run_log,
    "hanhua_tasks": hanhua_tasks,
    "hanhua_pause": hanhua_pause,
    "hanhua_config": hanhua_config,
    "hanhua_bgm": hanhua_bgm,
    "hanhua_remix": hanhua_remix,
    "hanhua_doctor": hanhua_doctor,
}
