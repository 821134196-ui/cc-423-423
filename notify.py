"""进程内任务：到点提醒发送、失败重试退避、超时未取扫描。

纯后台守护线程，无需外部队列。tick 间隔可通过环境变量 GROOM_TICK_SECONDS
调整（测试时调小）。停止时通知线程退出即可。
"""
from __future__ import annotations

import logging
import os
import threading

import services
from db import connect

log = logging.getLogger("grooming.notify")


class NotificationWorker(threading.Thread):
    """周期性扫描 notifications 表，到期即发送（失败自动退避重试）。

    每 4 个 tick 扫描一次"超时未取"。
    """

    def __init__(self, tick_seconds: float | None = None):
        super().__init__(name="notify-worker", daemon=True)
        self.tick = tick_seconds or float(os.environ.get("GROOM_TICK_SECONDS", "5"))
        self._stop_event = threading.Event()
        self.sent_count = 0
        self.failed_count = 0
        self.retried_count = 0
        self.overdue_events = 0
        self._tick_no = 0

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        log.info("通知任务已启动，tick=%ss", self.tick)
        while not self._stop_event.is_set():
            try:
                self._tick_once()
            except Exception:  # 守护线程不能因单次异常退出
                log.exception("通知任务 tick 异常")
            self._stop_event.wait(self.tick)
        log.info("通知任务已停止")

    def _tick_once(self) -> None:
        conn = connect()
        try:
            due = services.due_notifications(conn)
        finally:
            conn.close()
        # attempt_send 各自开独立事务，避免长事务互相阻塞
        for n in due:
            try:
                before = n["attempts"]
                result = services.attempt_send(n["id"])
                self._record_result(result, before)
            except Exception:
                log.exception("发送通知 #%s 失败", n["id"])

        self._tick_no += 1
        if self._tick_no % 4 == 0:
            try:
                ids = services.scan_overdue()
            except Exception:
                log.exception("超时未取扫描异常")
                return
            if ids:
                self.overdue_events += len(ids)
                log.warning("生成超时未取事件：%r", ids)

    def _record_result(self, result: dict, before: int) -> None:
        status = result.get("status")
        attempts = result.get("attempts", 0)
        if status == "sent":
            self.sent_count += 1
            if attempts > 1:
                self.retried_count += 1
        elif status == "pending" and attempts > before:
            self.retried_count += 1
        elif status == "failed":
            self.failed_count += 1
