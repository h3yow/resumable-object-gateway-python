"""上传会话应用服务：编排存储、配额与后台过期清理。"""

from __future__ import annotations

import re

from app.config import Settings
from app.errors import SessionNotFound
from app.logging_setup import configure_logging
from app.models import (
    CompleteResult,
    CreateSessionResponse,
    ObjectMeta,
    PartInfo,
    SessionProgress,
    SessionStatus,
)
from app.storage import LocalStorage
from app.sweeper import ExpirySweeper

# 对象键安全规则：允许常见路径字符，但禁止 NUL、反斜杠、以斜杠开头或
# 包含 ".." 路径段，防止本地磁盘路径穿越。
_KEY_SAFE = re.compile(r"^[^\\\x00][^\\\x00]*$")


def _validate_object_key(object_key: str) -> None:
    if not object_key or not _KEY_SAFE.match(object_key):
        raise ValueError("object_key 非法")
    if object_key.startswith("/"):
        raise ValueError("object_key 不允许以 '/' 开头")
    parts = object_key.split("/")
    if any(part in ("", "..", ".") for part in parts):
        raise ValueError("object_key 不允许空段或 '.'/'..' 段")


class UploadService:
    """对外的高层 API，HTTP 层只依赖本类。"""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or Settings()
        self.log = configure_logging()
        self.storage = LocalStorage(self.settings)
        self._sweeper: ExpirySweeper | None = None

    def start(self) -> None:
        """初始化磁盘布局并（按需）启动后台清理线程。"""
        self.storage.init_layout()
        if self.settings.enable_sweeper:
            self._sweeper = ExpirySweeper(
                self.storage, self.settings.sweeper_interval_seconds)
            self._sweeper.start()
            self.log.info("过期清理线程已启动: interval=%ss",
                          self.settings.sweeper_interval_seconds)

    def restart(self) -> None:
        """模拟进程重启：停止后台线程并从磁盘恢复全部状态。"""
        self.shutdown()
        self._sweeper = None
        self.storage = LocalStorage(self.settings)
        self.start()

    def shutdown(self) -> None:
        """停止后台清理线程。"""
        if self._sweeper is not None:
            self._sweeper.stop()
            self._sweeper.join(timeout=2.0)
            self._sweeper = None

    def create_session(
        self,
        object_key: str,
        part_count: int,
        total_size: int,
        whole_digest: str | None = None,
        content_type: str | None = None,
    ) -> CreateSessionResponse:
        _validate_object_key(object_key)
        session_id, expires_at = self.storage.create_session(
            object_key, part_count, total_size, whole_digest, content_type)
        return CreateSessionResponse(
            session_id=session_id,
            object_key=object_key,
            part_count=part_count,
            total_size=total_size,
            expires_at=expires_at,
            status=SessionStatus.PENDING,
        )

    def put_part(
        self, session_id: str, number: int, data: bytes, digest: str | None
    ) -> PartInfo:
        return self.storage.put_part(session_id, number, data, digest)

    def complete_session(
        self, session_id: str, whole_digest: str | None = None
    ) -> CompleteResult:
        return self.storage.complete_session(session_id, whole_digest)

    def abort_session(self, session_id: str) -> tuple[str, str]:
        return self.storage.abort_session(session_id)

    def get_progress(self, session_id: str) -> SessionProgress:
        return self.storage.get_progress(session_id)

    def sweep_expired(self, now: float | None = None) -> list[str]:
        return self.storage.sweep_expired(now)

    def get_object_meta(self, object_key: str) -> ObjectMeta:
        return self.storage.get_object_meta(object_key)

    def read_object(self, object_key: str) -> tuple[bytes, ObjectMeta]:
        return self.storage.read_object(object_key)

    def session_exists(self, session_id: str) -> bool:
        try:
            self.storage.get_progress(session_id)
            return True
        except SessionNotFound:
            return False
