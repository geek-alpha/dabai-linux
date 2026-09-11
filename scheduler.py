"""定时任务调度器 —— 大白的长任务自动化基础能力。

职责：
- 任务登记在 data/scheduled_tasks.json（tmp + os.replace 原子写，失败静默）；
- 后台循环每 15 秒扫描一次，到期任务通过 fire 回调（server 注入）派发为
  通用子智能体后台执行——复用现有「子智能体汇报」链路，干完自动向主智能体转述；
- 无人值守（没有前端连接）时任务照常执行，结果写入 last_result，
  用户回来可用 sched_list 查看历史；
- 调度器自身绝不让服务器崩掉：任何读写/派发异常都只记录、不抛出。

任务字段：
    id / name / task / interval_sec / enabled / next_run_at / running /
    runs / last_run_at / last_result / last_error / created_at / updated_at
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from pathlib import Path

logger = logging.getLogger("scheduler")

BASE_DIR = Path(__file__).resolve().parent
SCHED_FILE = BASE_DIR / "data" / "scheduled_tasks.json"

TICK_SECONDS = 15                 # 扫描间隔
MIN_INTERVAL = 30                 # 最小间隔（秒），防止手滑把任务刷爆
STALE_RUNNING_SECONDS = 3600 * 2  # 运行标记超过 2 小时视为悬挂，允许重新派发


def _load() -> list:
    try:
        if SCHED_FILE.exists():
            data = json.loads(SCHED_FILE.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return data
    except Exception as e:
        logger.warning("读取定时任务失败（忽略）: %s", e)
    return []


def _save(jobs: list) -> bool:
    try:
        SCHED_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = str(SCHED_FILE) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(jobs, f, ensure_ascii=False, indent=2)
        os.replace(tmp, str(SCHED_FILE))
        return True
    except Exception as e:
        logger.warning("保存定时任务失败（忽略）: %s", e)
        return False


def _find(jobs: list, job_id: str) -> dict | None:
    for j in jobs:
        if j.get("id") == job_id:
            return j
    return None


def list_jobs() -> list:
    """全部定时任务（按下次执行时间排序）。"""
    jobs = _load()
    jobs.sort(key=lambda j: (j.get("next_run_at") or 0))
    return jobs


def add_job(name: str, task: str, interval_sec: int,
            enabled: bool = True) -> tuple:
    """新增定时任务。返回 (job, error)。name 重复或参数非法时 error 非空。"""
    name = str(name or "").strip()
    task = str(task or "").strip()
    try:
        interval = max(MIN_INTERVAL, int(interval_sec))
    except (TypeError, ValueError):
        return None, "interval_sec 需为数字（秒）"
    if not name:
        return None, "name 不能为空"
    if not task:
        return None, "task（定时要执行的任务描述）不能为空"
    if len(task) > 4000:
        return None, "task 过长（上限 4000 字符），请精简"
    jobs = _load()
    if any(j.get("name") == name for j in jobs):
        return None, f"已存在同名定时任务：{name}"
    now = time.time()
    job = {
        "id": "sched-" + uuid.uuid4().hex[:10],
        "name": name[:60],
        "task": task,
        "interval_sec": interval,
        "enabled": bool(enabled),
        "next_run_at": now,           # 创建即触发第一次
        "running": False,
        "runs": 0,
        "last_run_at": 0.0,
        "last_result": "",
        "last_error": "",
        "created_at": now,
        "updated_at": now,
    }
    jobs.append(job)
    if not _save(jobs):
        return None, "写入失败（已静默降级）"
    return job, None


def remove_job(job_id: str, confirm: bool = False) -> tuple:
    """删除定时任务；必须显式 confirm=True（破坏性操作）。"""
    if not confirm:
        return None, "删除定时任务需要 confirm=true"
    jobs = _load()
    hit = _find(jobs, job_id)
    if hit is None:
        return None, "定时任务不存在：%s" % job_id
    jobs = [j for j in jobs if j.get("id") != job_id]
    if not _save(jobs):
        return None, "写入失败（已静默降级）"
    return hit, None


def set_enabled(job_id: str, enabled: bool) -> tuple:
    jobs = _load()
    hit = _find(jobs, job_id)
    if hit is None:
        return None, "定时任务不存在：%s" % job_id
    hit["enabled"] = bool(enabled)
    hit["updated_at"] = time.time()
    if not _save(jobs):
        return None, "写入失败（已静默降级）"
    return hit, None


def run_now(job_id: str) -> tuple:
    """立即触发一次（下个扫描周期派发）；执行中则忽略。"""
    jobs = _load()
    hit = _find(jobs, job_id)
    if hit is None:
        return None, "定时任务不存在：%s" % job_id
    if hit.get("running"):
        return None, "该任务正在执行中，请等它跑完"
    hit["next_run_at"] = 0.0
    hit["updated_at"] = time.time()
    if not _save(jobs):
        return None, "写入失败（已静默降级）"
    return hit, None


def _mark_running(job_id: str) -> bool:
    jobs = _load()
    hit = _find(jobs, job_id)
    if hit is None:
        return False
    now = time.time()
    hit["running"] = True
    hit["last_run_at"] = now
    # 派发前就把下次时间推后：长任务执行期间不会重复触发
    hit["next_run_at"] = now + int(hit.get("interval_sec") or MIN_INTERVAL)
    hit["updated_at"] = now
    return _save(jobs)


def record_result(job_id: str, ok: bool, result: str = "") -> bool:
    """子智能体完成/出错回调：记录执行历史并解除 running 标记。"""
    jobs = _load()
    hit = _find(jobs, job_id)
    if hit is None:
        return False
    now = time.time()
    hit["running"] = False
    hit["runs"] = int(hit.get("runs") or 0) + 1
    hit["last_result"] = str(result or "")[:600]
    hit["last_error"] = "" if ok else str(result or "")[:300]
    hit["updated_at"] = now
    return _save(jobs)


def _sweep_stale(jobs: list, now: float) -> list:
    """解除长时间悬挂的 running 标记（子智能体崩溃/丢失时防卡死）。"""
    changed = False
    for j in jobs:
        if j.get("running") and j.get("last_run_at"):
            stale_after = max(int(j.get("interval_sec") or MIN_INTERVAL) * 2,
                              STALE_RUNNING_SECONDS)
            if now - float(j.get("last_run_at") or 0) > stale_after:
                j["running"] = False
                j["last_error"] = "上次运行疑似悬挂，已解除占用标记"
                changed = True
    if changed:
        _save(jobs)
    return jobs


async def start_scheduler(fire_cb, tick_seconds: int = TICK_SECONDS) -> None:
    """后台调度循环：到期任务 → fire_cb(job)（异步，派发后立即返回）。

    fire_cb 由 server 注入（把任务派发为通用子智能体并注入 job_id）。
    本协程持续运行；任何异常只记录，绝不抛出。
    """
    logger.info("[Scheduler] 调度循环启动（每 %ds 扫描）", tick_seconds)
    while True:
        try:
            now = time.time()
            jobs = _sweep_stale(_load(), now)
            due = [j for j in jobs
                   if j.get("enabled") and not j.get("running")
                   and (j.get("next_run_at") or 0) <= now]
            for job in due:
                job_id = job.get("id")
                if not _mark_running(job_id):
                    continue
                try:
                    asyncio.ensure_future(fire_cb(job))
                    logger.info("[Scheduler] 派发定时任务《%s》[%s]",
                                job.get("name"), job_id)
                except Exception as e:
                    record_result(job_id, False, f"派发失败：{e}")
        except Exception as e:
            logger.warning("[Scheduler] 调度扫描异常（忽略）: %s", e)
        await asyncio.sleep(max(5, int(tick_seconds)))
