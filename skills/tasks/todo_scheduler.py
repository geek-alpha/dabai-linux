"""提醒调度器 —— 后台线程定时触发任务提醒与逾期提醒。

能力：
1. 定时提醒：到任务提醒时刻自动触发一次（reminder.type=once）；
2. 重复提醒：按 每小时/每天/每周（含周期倍数 repeat_every）滚动安排下一次触发；
3. 逾期提醒：截止时间已到且任务未完成时触发一次「任务已逾期」；
4. 可扩展：注册 delivery_callback 即可把提醒推送给大白前端/语音等任意通道；
   未触发回调时提醒仍会进入 service 的事件缓冲，REST 轮询可消费。

调度器是常驻后台线程，start() 后由插件生命周期（on_load/on_unload）管理。
"""
from __future__ import annotations

import logging
import threading
import time

logger = logging.getLogger('todo_list.scheduler')


class ReminderScheduler:
    """给给定 TodoService 挂常驻提醒线程。"""

    def __init__(self, service, poll_interval: float = 10.0):
        self.service = service
        self.poll_interval = max(1.0, float(poll_interval))
        self._stop_event = threading.Event()
        self._thread = None
        self._callbacks: list = []

    # ---------- 生命周期 ----------

    def start(self) -> None:
        """启动后台提醒线程（幂等；已在运行则忽略）。"""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run, name='todo-reminder-scheduler', daemon=True)
        self._thread.start()
        logger.info('todo_list 提醒调度器已启动（每 %.1fs 检查）', self.poll_interval)

    def stop(self) -> None:
        """停止后台提醒线程并等待退出。"""
        if self._thread is None:
            return
        self._stop_event.set()
        self._thread.join(timeout=self.poll_interval + 2.0)
        self._thread = None
        logger.info('todo_list 提醒调度器已停止')

    # ---------- 投递回调（扩展点） ----------

    def add_delivery_callback(self, callback) -> None:
        """注册提醒投递回调 callback(task, event)。

        event 结构：{'kind': 'reminder'|'overdue', 'message': str, 'ts': float}
        可注册多个回调；投递异常被吞掉并记日志，不影响调度主流程。
        """
        if callable(callback) and callback not in self._callbacks:
            self._callbacks.append(callback)

    def remove_delivery_callback(self, callback) -> None:
        """移除已注册的投递回调。"""
        if callback in self._callbacks:
            self._callbacks.remove(callback)

    # ---------- 主循环 ----------

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.check_once()
            except Exception as e:      # noqa: BLE001 —— 调度循环必须保活
                logger.warning('todo_list 提醒检查异常：%s', e)
            self._stop_event.wait(self.poll_interval)

    def check_once(self, now=None) -> int:
        """跑一轮提醒检查，返回本轮触发的事件数。

        也对外暴露：无后台线程时（例如仅用 API）可手动周期调用。
        """
        now = now or time.time()

        def on_fire(task, event):
            for cb in list(self._callbacks):
                try:
                    cb(task, event)
                except Exception as e:
                    logger.warning('todo_list 提醒投递回调失败：%s', e)

        events = self.service.fire_reminder(now=now, on_fire=on_fire)
        if events:
            logger.info('todo_list 本轮触发提醒 %d 条', len(events))
        return len(events)