"""过期会话后台清理线程。"""

from __future__ import annotations

import threading

from app.logging_setup import get_logger
from app.storage import LocalStorage


class ExpirySweeper(threading.Thread):
    """周期性调用 :meth:`LocalStorage.sweep_expired` 的守护线程。"""

    def __init__(self, service: LocalStorage, interval_seconds: float) -> None:
        super().__init__(daemon=True, name="rog-expiry-sweeper")
        self.storage = service
        self.interval_seconds = max(0.01, float(interval_seconds))
        self._stop = threading.Event()
        self.log = get_logger()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        self.log.debug("清理线程进入循环: interval=%ss", self.interval_seconds)
        while not self._stop.wait(self.interval_seconds):
            try:
                cleaned = self.storage.sweep_expired()
                if cleaned:
                    self.log.info("后台清理过期会话: %s", cleaned)
            except Exception:  # pragma: no cover - 线程不能因偶发错误退出
                self.log.exception("后台清理发生异常，将在下个周期重试")
