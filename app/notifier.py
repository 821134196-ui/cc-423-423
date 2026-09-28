"""进程内任务：后台线程周期扫描到期通知并重试，顺带检查超时未取。

不引入任何外部队列或定时器依赖。``sweep`` 在独立短连接中运行，
与请求线程共享 SQLite（WAL 模式允许并发读、写串行化）。
"""
import threading

from . import config, operations
from .db import connect


class NotificationScheduler:
    def __init__(self, interval=None, sender=None):
        self.interval = interval or config.NOTIFY_INTERVAL_SECONDS
        self.sender = sender or operations.simulated_sender
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="notifier", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)

    def sweep_once(self):
        conn = connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                overdue_n = operations.sweep_overdue(conn)
                processed = operations.process_notifications(conn, sender=self.sender)
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
            return overdue_n, processed
        finally:
            conn.close()

    def _run(self):
        # 启动后先跑一次，之后按固定间隔。
        while not self._stop.is_set():
            try:
                self.sweep_once()
            except Exception:
                # 后台线程不能因单次失败退出；下轮继续重试。
                pass
            self._stop.wait(self.interval)
