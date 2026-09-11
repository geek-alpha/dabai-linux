"""定时任务（scheduler）—— 大白的长任务自动化：按固定间隔自动执行并汇报。

薄封装 scheduler.py 的任务登记/查询/启停/删除；实际派发由 server 启动的
调度循环负责（到期 → 通用子智能体后台执行 → 完成自动汇报主智能体）。
"""
from __future__ import annotations

import re
import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent.parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from scheduler import (  # noqa: E402
    add_job,
    list_jobs,
    remove_job,
    run_now,
    set_enabled,
)

PROMPT = "【定时任务】用户要求定期/定时/盯着/到时提醒做某事时：sched_add 创建 → sched_list 查看 → sched_toggle 启停 → sched_remove 删除（须 confirm=true）。"

_INTERVAL_RE = re.compile(
    r"^\s*(\d+)\s*(秒|分钟|分|小时|时|天|日|周|s|m|h|d|w|sec|min|hour|day|week)?\s*$",
    re.IGNORECASE,
)
_UNIT_SECONDS = {
    "秒": 1, "s": 1, "sec": 1,
    "分钟": 60, "分": 60, "min": 60,
    "小时": 3600, "时": 3600, "h": 3600, "hour": 3600,
    "天": 86400, "日": 86400, "d": 86400, "day": 86400,
    "周": 604800, "w": 604800, "week": 604800,
}


def _parse_interval(value) -> tuple:
    """把「60 / 5分钟 / 1小时 / 1天」解析成秒；非法返回 (None, 错误)。"""
    s = str(value or "").strip()
    m = _INTERVAL_RE.match(s)
    if not m:
        return None, ("interval 格式不识别：%r（可用 60、30秒、5分钟、1小时、1天）"
                      % (s,))
    n = int(m.group(1))
    unit = (m.group(2) or "秒").lower()
    return n * _UNIT_SECONDS.get(unit, 1), None


def _human_sec(seconds) -> str:
    seconds = max(0, int(seconds or 0))
    if seconds <= 0:
        return "已到期（立即）"
    if seconds < 60:
        return "%d 秒后" % seconds
    if seconds < 3600:
        return "%d 分钟后" % (seconds // 60)
    if seconds < 86400:
        return "%d 小时后" % (seconds // 3600)
    return "%d 天后" % (seconds // 86400)


async def sched_add(args: dict) -> str:
    name = str(args.get("name") or "").strip()
    task = str(args.get("task") or "").strip()
    interval_s, err = _parse_interval(args.get("interval"))
    if err:
        return "✘ " + err
    job, err = add_job(name, task, interval_s,
                       enabled=bool(args.get("enabled", True)))
    if err:
        return "✘ " + err
    return ("✔ 已创建定时任务《%s》[%s]\n"
            "  任务：%s\n"
            "  间隔：每 %d 秒执行一次（创建即触发第一次）\n"
            "  说明：由子智能体后台执行，完成后自动汇报；sched_list 可随时查看。"
            % (job["name"], job["id"], job["task"][:200], job["interval_sec"]))


async def sched_list(args: dict) -> str:
    jobs = list_jobs()
    if not jobs:
        return "暂无定时任务。需要定期/定时做某件事时用 sched_add 创建。"
    lines = ["定时任务共 %d 个：" % len(jobs)]
    now = time.time()
    for j in jobs:
        name = j.get("name") or "（无名称）"
        job_id = j.get("id")
        enabled = "启用" if j.get("enabled") else "暂停"
        interval = j.get("interval_sec") or 0
        if j.get("running"):
            when = "执行中……"
        else:
            when = _human_sec((j.get("next_run_at") or now) - now)
        runs = j.get("runs") or 0
        last = ""
        if j.get("last_result"):
            last = "｜最近：%s" % str(j.get("last_result"))[:100]
        elif j.get("last_error"):
            last = "｜最近出错：%s" % str(j.get("last_error"))[:100]
        lines.append("- %s [%s]｜%s｜每 %d 秒｜%s｜已执行 %d 次%s"
                     % (name, job_id, enabled, interval, when, runs, last))
    lines.append("提示：sched_run_now 立即触发；sched_toggle 启停；sched_remove 删除。")
    return "\n".join(lines)


async def sched_run_now(args: dict) -> str:
    job_id = str(args.get("job_id") or "").strip()
    if not job_id:
        return "✘ 需要 job_id（sched_list 可查）"
    job, err = run_now(job_id)
    if err:
        return "✘ " + err
    return "✔ 已安排《%s》立即执行（下个扫描周期派发）。" % (job.get("name") or job_id)


async def sched_toggle(args: dict) -> str:
    job_id = str(args.get("job_id") or "").strip()
    enabled = bool(args.get("enabled"))
    if not job_id:
        return "✘ 需要 job_id（sched_list 可查）"
    job, err = set_enabled(job_id, enabled)
    if err:
        return "✘ " + err
    return "✔ 定时任务《%s》已%s。" % (job.get("name") or job_id,
                                     "启用" if enabled else "暂停")


async def sched_remove(args: dict) -> str:
    job_id = str(args.get("job_id") or "").strip()
    if not job_id:
        return "✘ 需要 job_id（sched_list 可查）"
    job, err = remove_job(job_id, confirm=bool(args.get("confirm")))
    if err:
        return "✘ " + err
    return "✔ 已删除定时任务《%s》[%s]。" % (job.get("name") or job_id, job_id)


HANDLERS = {
    "sched_add": sched_add,
    "sched_list": sched_list,
    "sched_run_now": sched_run_now,
    "sched_toggle": sched_toggle,
    "sched_remove": sched_remove,
}
