"""上传领域服务：会话创建 / 分块写入 / 完成 / 中止 / 进度 / 过期清理。

所有判定均通过 ``GatewayError`` 携带可区分的 ``ErrorCode``，并以统一日志
格式打印会话标识、分块序号与判定依据，便于单测与线上排查。
"""

import logging
import threading
from datetime import datetime, timezone

from gateway.config import Settings, get_settings
from gateway.errors import ErrorCode, GatewayError
from gateway.hashing import checksums_equal, is_sha256_hex, sha256_hex
from gateway.models import (
    CallerUsageResponse,
    PartRecord,
    Session,
    SessionProgress,
    SessionStatus,
)
from gateway.sessions import SessionStore
from gateway.storage import LocalObjectStore
from gateway.tenancy import DEFAULT_CALLER_ID, TenantStore

logger = logging.getLogger("gateway.service")


class UploadService:
    """编排会话仓库、本地对象存储与调用方额度账本，实施全部业务规则。"""

    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self.objects = LocalObjectStore(self.settings)
        self.sessions = SessionStore(self.settings, self.objects)
        self.tenants = TenantStore(self.settings, self.objects)
        # 重启对账：以磁盘上的对象/会话为权威重建各调用方占用
        self.tenants.reconcile(self.sessions.all_sessions())

    # -------------------------------------------------------------- helpers
    def _refresh_expiry(self, session: Session) -> None:
        """每次有效活动都顺延过期时间（滑动过期）。"""
        from datetime import timedelta

        now = datetime.now(timezone.utc)
        session.updated_at = now
        session.expires_at = now + timedelta(
            seconds=self.settings.session_ttl_seconds
        )

    def _guard_active(self, session: Session) -> None:
        """终态/过期会话上的写操作一律拒绝，错误原因可区分。"""
        if session.status == SessionStatus.COMPLETED:
            raise GatewayError(
                ErrorCode.SESSION_COMPLETED,
                f"session {session.session_id!r} already completed",
                session_id=session.session_id,
            )
        if session.status == SessionStatus.ABORTED:
            raise GatewayError(
                ErrorCode.SESSION_ABORTED,
                f"session {session.session_id!r} has been aborted",
                session_id=session.session_id,
            )
        if session.status == SessionStatus.EXPIRED or self.sessions.is_expired(
            session
        ):
            raise GatewayError(
                ErrorCode.SESSION_EXPIRED,
                f"session {session.session_id!r} expired at {session.expires_at.isoformat()}",
                session_id=session.session_id,
            )

    # ------------------------------------------------------------- initiate
    def initiate(
        self,
        object_key: str,
        part_size: int = 0,
        total_parts: int | None = None,
        overall_checksum: str | None = None,
        caller_id: str | None = None,
    ) -> Session:
        cid = self.tenants.resolve(caller_id)
        if not object_key or not object_key.strip():
            raise GatewayError(
                ErrorCode.SESSION_NOT_INITIATED,
                "object_key must not be empty",
            )
        if part_size < 0:
            raise GatewayError(
                ErrorCode.SESSION_NOT_INITIATED,
                f"part_size must be >= 0, got {part_size}",
            )
        if total_parts is not None:
            if total_parts <= 0:
                raise GatewayError(
                    ErrorCode.SESSION_NOT_INITIATED,
                    f"total_parts must be > 0, got {total_parts}",
                )
            if total_parts > self.settings.max_parts_per_upload:
                raise GatewayError(
                    ErrorCode.SESSION_NOT_INITIATED,
                    f"total_parts {total_parts} exceeds limit "
                    f"{self.settings.max_parts_per_upload}",
                )
        if overall_checksum is not None and not is_sha256_hex(overall_checksum):
            raise GatewayError(
                ErrorCode.SESSION_NOT_INITIATED,
                "overall_checksum_sha256 must be 64 hex chars when provided",
            )
        if self.objects.object_exists(object_key):
            raise GatewayError(
                ErrorCode.OBJECT_ALREADY_EXISTS,
                f"object {object_key!r} already exists",
            )

        session = self.sessions.create(
            object_key, part_size, total_parts, overall_checksum, caller_id=cid
        )
        # 会话创建后立即为这次上传占住额度；占住失败则回滚会话，不留痕迹
        estimated_bytes = (
            part_size * total_parts
            if part_size > 0 and total_parts is not None
            else 0
        )
        try:
            self.tenants.reserve(cid, session.session_id, estimated_bytes, 1)
        except GatewayError:
            self.sessions.discard(session.session_id)
            raise
        logger.info(
            "session=%s caller=%s action=initiate object_key=%s part_size=%d "
            "total_parts=%s verdict=CREATED expires_at=%s",
            session.session_id,
            cid,
            object_key,
            part_size,
            total_parts,
            session.expires_at.isoformat(),
        )
        return session

    # ---------------------------------------------------------- upload part
    def upload_part(
        self,
        session_id: str,
        part_number: int,
        data: bytes,
        checksum: str | None = None,
    ) -> tuple[Session, PartRecord, bool]:
        """写入一个分块。

        返回 ``(会话, 分块记录, 是否重复提交)``。重复提交同一块时：
        摘要与大小与首次完全一致 -> 幂等成功（duplicated=True）；
        任一字段不一致 -> 拒绝（PART_CHECKSUM_MISMATCH）。
        """
        with self.sessions.lock(session_id):
            session = self.sessions.require(session_id)
            self._guard_active(session)

            upper = (
                session.total_parts
                if session.total_parts is not None
                else self.settings.max_parts_per_upload
            )
            if not isinstance(part_number, int) or part_number < 1 or part_number > upper:
                logger.info(
                    "session=%s part=%s action=upload verdict=REJECTED reason=%s",
                    session_id,
                    part_number,
                    ErrorCode.PART_NUMBER_OUT_OF_RANGE.value,
                )
                raise GatewayError(
                    ErrorCode.PART_NUMBER_OUT_OF_RANGE,
                    f"part_number {part_number} out of range [1, {upper}]",
                    session_id=session_id,
                )

            # 调用方单块尺寸上下限：越界按尺寸原因立即拒绝，不写盘
            policy = self.tenants.policy_for(session.caller_id)
            is_last_part = (
                session.total_parts is not None
                and part_number == session.total_parts
            )
            if policy.max_part_bytes is not None and len(data) > policy.max_part_bytes:
                logger.info(
                    "session=%s caller=%s part=%d action=upload verdict=REJECTED "
                    "reason=PART_SIZE_OUT_OF_RANGE size=%d max=%d",
                    session_id,
                    session.caller_id,
                    part_number,
                    len(data),
                    policy.max_part_bytes,
                )
                raise GatewayError(
                    ErrorCode.PART_SIZE_OUT_OF_RANGE,
                    f"part {part_number} size {len(data)} exceeds caller "
                    f"{session.caller_id!r} max_part_bytes {policy.max_part_bytes}",
                    session_id=session_id,
                    caller_id=session.caller_id,
                )
            if not is_last_part and len(data) < policy.min_part_bytes:
                logger.info(
                    "session=%s caller=%s part=%d action=upload verdict=REJECTED "
                    "reason=PART_SIZE_OUT_OF_RANGE size=%d min=%d",
                    session_id,
                    session.caller_id,
                    part_number,
                    len(data),
                    policy.min_part_bytes,
                )
                raise GatewayError(
                    ErrorCode.PART_SIZE_OUT_OF_RANGE,
                    f"part {part_number} size {len(data)} below caller "
                    f"{session.caller_id!r} min_part_bytes {policy.min_part_bytes}",
                    session_id=session_id,
                    caller_id=session.caller_id,
                )

            actual_checksum = sha256_hex(data)
            if checksum is not None:
                if not is_sha256_hex(checksum):
                    raise GatewayError(
                        ErrorCode.PART_CHECKSUM_MISMATCH,
                        "client checksum must be 64 hex chars",
                        session_id=session_id,
                    )
                if not checksums_equal(actual_checksum, checksum):
                    logger.info(
                        "session=%s part=%d action=upload verdict=REJECTED "
                        "reason=PART_CHECKSUM_MISMATCH expected=%s actual=%s size=%d",
                        session_id,
                        part_number,
                        checksum,
                        actual_checksum,
                        len(data),
                    )
                    raise GatewayError(
                        ErrorCode.PART_CHECKSUM_MISMATCH,
                        f"part {part_number} checksum mismatch: "
                        f"expected={checksum} actual={actual_checksum}",
                        session_id=session_id,
                    )

            existing = session.parts.get(part_number)
            duplicated = existing is not None
            if duplicated:
                if existing.size != len(data) or not checksums_equal(
                    existing.checksum_sha256, actual_checksum
                ):
                    logger.info(
                        "session=%s part=%d action=upload verdict=REJECTED "
                        "reason=PART_CHECKSUM_MISMATCH duplicate_with_different_content "
                        "first_size=%d this_size=%d",
                        session_id,
                        part_number,
                        existing.size,
                        len(data),
                    )
                    raise GatewayError(
                        ErrorCode.PART_CHECKSUM_MISMATCH,
                        f"part {part_number} already uploaded with different content",
                        session_id=session_id,
                    )
                logger.info(
                    "session=%s part=%d action=upload verdict=IDEMPOTENT size=%d "
                    "checksum=%s",
                    session_id,
                    part_number,
                    existing.size,
                    existing.checksum_sha256,
                )
                self._refresh_expiry(session)
                self.sessions.save(session)
                return session, existing, True

            self.objects.write_part(session_id, part_number, data)
            record = PartRecord(
                number=part_number,
                size=len(data),
                checksum_sha256=actual_checksum,
                uploaded_at=datetime.now(timezone.utc),
            )
            session.parts[part_number] = record
            self._refresh_expiry(session)
            self.sessions.save(session)
            logger.info(
                "session=%s part=%d action=upload verdict=ACCEPTED size=%d "
                "checksum=%s received=%s",
                session_id,
                part_number,
                len(data),
                actual_checksum,
                len(session.parts),
            )
            return session, record, False

    # -------------------------------------------------------------- complete
    def complete(
        self,
        session_id: str,
        total_parts: int | None = None,
        overall_checksum: str | None = None,
    ) -> Session:
        with self.sessions.lock(session_id):
            session = self.sessions.require(session_id)
            self._guard_active(session)

            declared_total = total_parts
            if declared_total is None:
                declared_total = session.total_parts
            if declared_total is None:
                raise GatewayError(
                    ErrorCode.PART_MISSING,
                    "total_parts is required because it was not declared at initiate",
                    session_id=session_id,
                )
            if declared_total <= 0 or declared_total > self.settings.max_parts_per_upload:
                raise GatewayError(
                    ErrorCode.PART_NUMBER_OUT_OF_RANGE,
                    f"total_parts {declared_total} out of range "
                    f"[1, {self.settings.max_parts_per_upload}]",
                    session_id=session_id,
                )

            # 1) 分块连续且无缺失
            numbers = sorted(session.parts)
            expected = list(range(1, declared_total + 1))
            missing = [n for n in expected if n not in session.parts]
            extra = [n for n in numbers if n > declared_total]
            if missing or extra or numbers[:declared_total] != expected:
                if any(n > declared_total for n in numbers):
                    reason = ErrorCode.PARTS_NOT_CONTIGUOUS
                    detail = f"parts beyond total_parts {declared_total} were uploaded: {extra}"
                else:
                    reason = ErrorCode.PART_MISSING
                    detail = f"missing parts {missing}"
                logger.info(
                    "session=%s action=complete verdict=REJECTED reason=%s %s",
                    session_id,
                    reason.value,
                    detail,
                )
                raise GatewayError(reason, detail, session_id=session_id)

            # 2) 整体大小上限
            total_size = sum(p.size for p in session.parts.values())
            if total_size > self.settings.max_object_bytes:
                logger.info(
                    "session=%s action=complete verdict=REJECTED "
                    "reason=OBJECT_SIZE_EXCEEDED size=%d limit=%d",
                    session_id,
                    total_size,
                    self.settings.max_object_bytes,
                )
                raise GatewayError(
                    ErrorCode.OBJECT_SIZE_EXCEEDED,
                    f"object size {total_size} exceeds limit "
                    f"{self.settings.max_object_bytes}",
                    session_id=session_id,
                )

            # 3) 整体摘要：以本次请求优先，其次用创建时声明的
            wanted_checksum = overall_checksum or session.overall_checksum_sha256
            if overall_checksum is not None and not is_sha256_hex(overall_checksum):
                raise GatewayError(
                    ErrorCode.OVERALL_CHECKSUM_MISMATCH,
                    "overall_checksum_sha256 must be 64 hex chars",
                    session_id=session_id,
                )

            # 4) 调用方额度：校验 → 原子发布 → 落账 全程持有调用方锁，
            #    同一调用方任意数量的并发完成都不会越过自己的额度；
            #    额度不足抛 QUOTA_EXCEEDED，与其他失败原因明确区分。
            with self.tenants.lock(session.caller_id):
                self.tenants.convert(session.caller_id, session_id, total_size)

                # 5) 原子合并 + 发布（内部做整体摘要校验，失败不发布）
                try:
                    actual_checksum, size = self.objects.commit_object(
                        session_id,
                        session.object_key,
                        declared_total,
                        wanted_checksum,
                        caller_id=session.caller_id,
                    )
                except GatewayError as exc:
                    if exc.code is ErrorCode.OVERALL_CHECKSUM_MISMATCH:
                        logger.info(
                            "session=%s action=complete verdict=REJECTED "
                            "reason=OVERALL_CHECKSUM_MISMATCH expected=%s parts=%d",
                            session_id,
                            wanted_checksum,
                            declared_total,
                        )
                    raise

                # 6) 发布成功：占住额度转为实际占用
                self.tenants.finalize(session.caller_id, session_id, size)

            # 7) 发布成功后置终态并清理暂存分块
            session.status = SessionStatus.COMPLETED
            session.total_parts = declared_total
            session.overall_checksum_sha256 = actual_checksum
            self._refresh_expiry(session)
            self.sessions.save(session)
            self.objects.remove_session_parts(session_id)
            logger.info(
                "session=%s action=complete verdict=COMPLETED object_key=%s "
                "parts=%d size=%d checksum=%s",
                session_id,
                session.object_key,
                declared_total,
                size,
                actual_checksum,
            )
            return session

    # ----------------------------------------------------------------- abort
    def abort(self, session_id: str) -> Session:
        with self.sessions.lock(session_id):
            session = self.sessions.require(session_id)
            if session.status == SessionStatus.ABORTED:
                logger.info(
                    "session=%s action=abort verdict=IDEMPOTENT already aborted",
                    session_id,
                )
                return session
            if session.status == SessionStatus.COMPLETED:
                raise GatewayError(
                    ErrorCode.SESSION_COMPLETED,
                    f"session {session_id!r} already completed, cannot abort",
                    session_id=session_id,
                )
            session.status = SessionStatus.ABORTED
            self._refresh_expiry(session)
            self.sessions.save(session)
            self.objects.remove_session_parts(session_id)
            # 归还该会话占住的调用方额度（幂等，重复中止不会重复归还）
            self.tenants.release(session.caller_id, session_id)
            logger.info(
                "session=%s caller=%s action=abort verdict=ABORTED parts_discarded=%d",
                session_id,
                session.caller_id,
                len(session.parts),
            )
            return session

    # --------------------------------------------------------------- progress
    def progress(self, session_id: str) -> tuple[Session, SessionProgress]:
        with self.sessions.lock(session_id):
            session = self.sessions.require(session_id)
            numbers = sorted(session.parts)
            total = session.total_parts
            next_expected = None
            missing: list[int] = []
            if total is not None:
                missing = [n for n in range(1, total + 1) if n not in session.parts]
                next_expected = missing[0] if missing else None
            elif numbers:
                # 未声明总数时：找到从 1 开始的第一段连续区间之后的缺口
                next_expected = None
                cursor = 1
                have = set(numbers)
                while cursor in have:
                    cursor += 1
                next_expected = cursor
            bytes_received = sum(p.size for p in session.parts.values())
            progress = SessionProgress(
                received_part_numbers=numbers,
                received_parts=len(numbers),
                total_parts=total,
                bytes_received=bytes_received,
                next_expected_part=next_expected,
                missing_part_numbers=missing,
            )
            logger.info(
                "session=%s action=progress status=%s received=%s total=%s "
                "bytes=%d missing=%s",
                session_id,
                session.status.value,
                numbers,
                total,
                bytes_received,
                missing,
            )
            return session, progress

    # ---------------------------------------------------------------- usage
    def usage(self, caller_id: str | None = None) -> CallerUsageResponse:
        """查询某调用方的额度占用、剩余与名下对象。"""
        cid = self.tenants.resolve(caller_id)
        return self.tenants.usage(cid)

    # ---------------------------------------------------------------- sweep
    def sweep_expired(self, now: datetime | None = None) -> int:
        def _release(session: Session) -> None:
            # 过期清理必须原样归还占住的额度，且不留半个可见对象
            self.tenants.release(session.caller_id, session.session_id)

        n = self.sessions.expire_sessions(now, on_expired=_release)
        if n:
            logger.info("action=sweep verdict=EXPIRED count=%d", n)
        return n
