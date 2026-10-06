"""会话持久化与并发控制。

- 每个会话一个 ``<root>/sessions/<session_id>/meta.json``，进程重启后直接恢复。
- 写入采用临时文件 + fsync + rename 原子替换。
- 进程内为每个 session_id 维护一把 threading.RLock，串行化同一会话的
  状态变更；不同会话之间互不阻塞。
"""

import json
import threading
import uuid
from collections.abc import Callable
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from gateway.config import Settings
from gateway.errors import ErrorCode, GatewayError
from gateway.models import Session, SessionStatus
from gateway.storage import LocalObjectStore, _atomic_write, _fsync_dir


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class SessionStore:
    """JSON 文件持久化的会话仓库。"""

    def __init__(self, settings: Settings, objects: LocalObjectStore):
        self.settings = settings
        self.objects = objects
        self.dir = objects.sessions_dir
        self.dir.mkdir(parents=True, exist_ok=True)
        #: session_id -> RLock；受 _registry_guard 保护
        self._locks: dict[str, threading.RLock] = {}
        self._registry_guard = threading.Lock()

    # ------------------------------------------------------------------- locks
    def _get_lock(self, session_id: str) -> threading.RLock:
        with self._registry_guard:
            lock = self._locks.get(session_id)
            if lock is None:
                lock = threading.RLock()
                self._locks[session_id] = lock
            return lock

    @contextmanager
    def lock(self, session_id: str):
        """会话级互斥锁的上下文管理器（可重入）。"""
        with self._get_lock(session_id):
            yield

    # ----------------------------------------------------------------- paths
    def _meta_path(self, session_id: str) -> Path:
        return self.dir / session_id / "meta.json"

    # ------------------------------------------------------------------ crud
    def create(
        self,
        object_key: str,
        part_size: int,
        total_parts: int | None,
        overall_checksum: str | None,
        tenant_id: str = "",
    ) -> Session:
        now = _utcnow()
        # 极小概率撞 id 时重试，避免目录已存在导致脏写
        for _ in range(5):
            session_id = uuid.uuid4().hex
            if not self._meta_path(session_id).exists():
                break
        session = Session(
            session_id=session_id,
            object_key=object_key,
            status=SessionStatus.INITIATED,
            tenant_id=tenant_id,
            part_size=part_size,
            total_parts=total_parts,
            overall_checksum_sha256=overall_checksum,
            parts={},
            created_at=now,
            updated_at=now,
            expires_at=now + timedelta(seconds=self.settings.session_ttl_seconds),
        )
        (self.dir / session_id).mkdir(parents=True, exist_ok=True)
        self.save(session)
        return session

    def save(self, session: Session) -> None:
        """原子写回会话元数据。"""
        target = self._meta_path(session.session_id)
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = session.model_dump_json()
        _atomic_write(target, payload.encode("utf-8"))
        _fsync_dir(target.parent)

    def discard(self, session_id: str) -> None:
        """创建后随即失败的回滚：删除会话目录与全部暂存内容。"""
        self.objects.remove_session_parts(session_id)
        meta = self._meta_path(session_id)
        if meta.exists():
            meta.unlink()
        d = self.dir / session_id
        try:
            d.rmdir()
        except OSError:
            pass

    def get(self, session_id: str) -> Session | None:
        path = self._meta_path(session_id)
        if not path.exists():
            return None
        return Session.model_validate_json(path.read_text())

    def require(self, session_id: str) -> Session:
        session = self.get(session_id)
        if session is None:
            raise GatewayError(
                ErrorCode.SESSION_NOT_FOUND,
                f"session {session_id!r} not found",
                session_id=session_id,
            )
        return session

    def all_sessions(self) -> list[Session]:
        sessions: list[Session] = []
        if not self.dir.exists():
            return sessions
        for entry in self.dir.iterdir():
            if not entry.is_dir():
                continue
            meta = entry / "meta.json"
            if meta.exists():
                try:
                    sessions.append(Session.model_validate_json(meta.read_text()))
                except ValueError:
                    # 损坏的元数据不阻断扫描
                    continue
        return sessions

    # -------------------------------------------------------------- expiry
    def is_expired(self, session: Session, now: datetime | None = None) -> bool:
        if session.status != SessionStatus.INITIATED:
            return False
        now = now or _utcnow()
        return session.expires_at <= now

    def expire_sessions(
        self,
        now: datetime | None = None,
        on_expired: Callable[[Session], None] | None = None,
    ) -> int:
        """扫描并清理超时的 INITIATED 会话。

        清理动作：状态置为 EXPIRED、删除暂存分块、保留 meta.json 作为可解释的
        终态记录（之后对该会话的写入将收到 SESSION_EXPIRED）。
        ``on_expired(session)`` 在会话锁内、状态落盘后回调，用于归还额度等
        善后；回调异常不阻断其它会话的清理。
        返回被清理的会话数。
        """
        now = now or _utcnow()
        cleaned = 0
        for session in self.all_sessions():
            if not self.is_expired(session, now):
                continue
            with self.lock(session.session_id):
                # 双检：持锁后重新读取，避免扫描期间会话被完成/中止
                fresh = self.get(session.session_id)
                if fresh is None or not self.is_expired(fresh, now):
                    continue
                fresh.status = SessionStatus.EXPIRED
                fresh.updated_at = now
                self.save(fresh)
                self.objects.remove_session_parts(fresh.session_id)
                if on_expired is not None:
                    on_expired(fresh)
                cleaned += 1
        return cleaned
