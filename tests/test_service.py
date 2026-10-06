"""领域服务单测：会话状态机、分块规则、校验与配额。

断言重点：
1. 错误必须被正确归类为对应的 ErrorCode；
2. caplog 中必须能看到会话标识、分块序号与判定依据。
"""

import logging
from datetime import datetime, timedelta, timezone

import pytest

from gateway.errors import ErrorCode, GatewayError
from gateway.hashing import sha256_hex
from gateway.models import SessionStatus
from gateway.service import UploadService

from .conftest import upload_parts_concurrently


# --------------------------------------------------------------- initiate
class TestInitiate:
    def test_create_session_has_id_and_expiry(self, service: UploadService):
        session = service.initiate("k1", total_parts=2)
        assert session.status is SessionStatus.INITIATED
        assert session.session_id
        assert session.expires_at > session.created_at

    def test_empty_key_rejected(self, service: UploadService):
        with pytest.raises(GatewayError) as ei:
            service.initiate("  ")
        assert ei.value.code is ErrorCode.SESSION_NOT_INITIATED

    def test_total_parts_over_limit_rejected(self, service: UploadService):
        with pytest.raises(GatewayError) as ei:
            service.initiate("k", total_parts=99)
        assert ei.value.code is ErrorCode.SESSION_NOT_INITIATED

    def test_bad_checksum_format_rejected(self, service: UploadService):
        with pytest.raises(GatewayError) as ei:
            service.initiate("k", overall_checksum="not-a-hash")
        assert ei.value.code is ErrorCode.SESSION_NOT_INITIATED

    def test_duplicate_object_key_rejected(
        self, service: UploadService, make_multipart
    ):
        make_multipart("dup", [b"a", b"b"])
        with pytest.raises(GatewayError) as ei:
            service.initiate("dup")
        assert ei.value.code is ErrorCode.OBJECT_ALREADY_EXISTS


# ------------------------------------------------------------ upload part
class TestUploadPart:
    def test_part_checksum_mismatch_is_classified(
        self, service: UploadService, caplog
    ):
        s = service.initiate("k", total_parts=2)
        with caplog.at_level(logging.INFO, logger="gateway.service"):
            with pytest.raises(GatewayError) as ei:
                service.upload_part(s.session_id, 1, b"abc", checksum="f" * 64)
        assert ei.value.code is ErrorCode.PART_CHECKSUM_MISMATCH
        assert ei.value.session_id == s.session_id
        # 日志中必须有会话标识、块序号与判定依据
        joined = "\n".join(r.getMessage() for r in caplog.records)
        assert s.session_id in joined and "part=1" in joined
        assert "PART_CHECKSUM_MISMATCH" in joined

    def test_part_number_out_of_range(
        self, service: UploadService, caplog: pytest.LogCaptureFixture
    ):
        s = service.initiate("k", total_parts=2)
        with caplog.at_level(logging.INFO):
            with pytest.raises(GatewayError) as ei:
                service.upload_part(s.session_id, 3, b"x")
        assert ei.value.code is ErrorCode.PART_NUMBER_OUT_OF_RANGE
        assert "part_number" in ei.value.message

    def test_part_zero_rejected(self, service: UploadService):
        s = service.initiate("k")
        with pytest.raises(GatewayError) as ei:
            service.upload_part(s.session_id, 0, b"x")
        assert ei.value.code is ErrorCode.PART_NUMBER_OUT_OF_RANGE

    def test_duplicate_part_is_idempotent(self, service: UploadService):
        s = service.initiate("k", total_parts=2)
        _, rec1, dup1 = service.upload_part(s.session_id, 1, b"aa")
        _, rec2, dup2 = service.upload_part(
            s.session_id, 1, b"aa", checksum=sha256_hex(b"aa")
        )
        assert dup1 is False and dup2 is True
        assert rec1.checksum_sha256 == rec2.checksum_sha256
        _, progress = service.progress(s.session_id)
        assert progress.received_parts == 1

    def test_duplicate_part_with_different_content_rejected(
        self, service: UploadService
    ):
        s = service.initiate("k", total_parts=2)
        service.upload_part(s.session_id, 1, b"aa")
        with pytest.raises(GatewayError) as ei:
            service.upload_part(s.session_id, 1, b"bb")
        assert ei.value.code is ErrorCode.PART_CHECKSUM_MISMATCH
        # 首次内容不得被覆盖损坏
        assert service.objects.read_part(s.session_id, 1) == b"aa"

    def test_write_after_abort_rejected(self, service: UploadService):
        s = service.initiate("k", total_parts=1)
        service.upload_part(s.session_id, 1, b"a")
        service.abort(s.session_id)
        with pytest.raises(GatewayError) as ei:
            service.upload_part(s.session_id, 1, b"a")
        assert ei.value.code is ErrorCode.SESSION_ABORTED

    def test_write_after_complete_rejected(self, service: UploadService):
        s = service.initiate("k", total_parts=1, overall_checksum=sha256_hex(b"a"))
        service.upload_part(s.session_id, 1, b"a")
        service.complete(s.session_id)
        with pytest.raises(GatewayError) as ei:
            service.upload_part(s.session_id, 1, b"a")
        assert ei.value.code is ErrorCode.SESSION_COMPLETED

    def test_session_not_found(self, service: UploadService):
        with pytest.raises(GatewayError) as ei:
            service.upload_part("nope", 1, b"a")
        assert ei.value.code is ErrorCode.SESSION_NOT_FOUND


# -------------------------------------------------------------- complete
class TestComplete:
    def test_missing_part_rejected_and_no_object(self, service: UploadService):
        s = service.initiate("k", total_parts=3)
        service.upload_part(s.session_id, 1, b"a")
        service.upload_part(s.session_id, 3, b"c")
        with pytest.raises(GatewayError) as ei:
            service.complete(s.session_id)
        assert ei.value.code is ErrorCode.PART_MISSING
        assert ei.value.message and "2" in ei.value.message
        assert not service.objects.object_exists("k")

    def test_non_contiguous_parts_rejected(self, service: UploadService):
        # 未在创建时声明总数：上传 1、2、3，但声明 total_parts=2 -> 存在越界块
        s = service.initiate("k")
        for i, payload in enumerate((b"a", b"b", b"c"), start=1):
            service.upload_part(s.session_id, i, payload)
        with pytest.raises(GatewayError) as ei:
            service.complete(s.session_id, total_parts=2)
        assert ei.value.code is ErrorCode.PARTS_NOT_CONTIGUOUS

    def test_overall_checksum_mismatch_leaves_no_object(
        self, service: UploadService, caplog
    ):
        s = service.initiate("k", total_parts=1)
        service.upload_part(s.session_id, 1, b"abc")
        with caplog.at_level(logging.INFO):
            with pytest.raises(GatewayError) as ei:
                service.complete(s.session_id, overall_checksum="0" * 64)
        assert ei.value.code is ErrorCode.OVERALL_CHECKSUM_MISMATCH
        assert not service.objects.object_exists("k")
        assert "OVERALL_CHECKSUM_MISMATCH" in caplog.text
        # 摘要失败后仍可修正并重新完成
        service.complete(s.session_id, overall_checksum=sha256_hex(b"abc"))
        assert service.objects.object_exists("k")
        assert service.objects.read_object("k") == b"abc"

    def test_object_size_limit_rejected(self, tmp_path):
        from gateway.config import Settings

        svc = UploadService(
            Settings(data_dir=str(tmp_path / "small"), max_object_bytes=10)
        )
        s = svc.initiate("big", total_parts=1)
        svc.upload_part(s.session_id, 1, b"x" * 11)
        with pytest.raises(GatewayError) as ei:
            svc.complete(s.session_id)
        assert ei.value.code is ErrorCode.OBJECT_SIZE_EXCEEDED
        assert not svc.objects.object_exists("big")

    def test_total_bytes_quota_rejected(self, tmp_path):
        from gateway.config import Settings

        svc = UploadService(
            Settings(
                data_dir=str(tmp_path / "quota"),
                max_object_bytes=10**9,
                max_total_object_bytes=4096,
                max_object_count=100,
            )
        )
        # 占用 3500 字节后，再来 1024 将超过 4096
        s1 = svc.initiate("q1", total_parts=1)
        svc.upload_part(s1.session_id, 1, b"x" * 3500)
        svc.complete(s1.session_id, overall_checksum=sha256_hex(b"x" * 3500))
        s2 = svc.initiate("q2", total_parts=1)
        svc.upload_part(s2.session_id, 1, b"y" * 1024)
        with pytest.raises(GatewayError) as ei:
            svc.complete(s2.session_id)
        assert ei.value.code is ErrorCode.QUOTA_EXCEEDED
        assert not svc.objects.object_exists("q2")

    def test_object_count_quota_rejected(
        self, tmp_path, make_multipart
    ):
        from gateway.config import Settings

        svc = UploadService(
            Settings(data_dir=str(tmp_path / "d2"), max_object_count=1,
                     max_total_object_bytes=10**9, max_object_bytes=10**9)
        )
        s1 = svc.initiate("c1", total_parts=1, overall_checksum=sha256_hex(b"a"))
        svc.upload_part(s1.session_id, 1, b"a")
        svc.complete(s1.session_id)
        # 对象数量额度在会话创建时即占住：第二个会话直接创建失败，
        # 错误码仍是可区分的 QUOTA_EXCEEDED
        with pytest.raises(GatewayError) as ei:
            svc.initiate("c2", total_parts=1)
        assert ei.value.code is ErrorCode.QUOTA_EXCEEDED
        assert not svc.objects.object_exists("c2")

    def test_success_content_matches_declared_checksum(
        self, service: UploadService, make_multipart
    ):
        session, content = make_multipart("ok", [b"he", b"ll", b"o"])
        assert service.objects.object_meta("ok")["checksum_sha256"] == sha256_hex(
            content
        )
        assert service.objects.read_object("ok") == content
        assert session.status is SessionStatus.COMPLETED

    def test_completed_session_parts_are_cleaned(self, service: UploadService):
        s = service.initiate("clean", total_parts=1, overall_checksum=sha256_hex(b"z"))
        service.upload_part(s.session_id, 1, b"z")
        service.complete(s.session_id)
        assert not service.objects.has_part(s.session_id, 1)


# ----------------------------------------------------------------- abort
class TestAbort:
    def test_abort_discards_parts_and_is_idempotent(self, service: UploadService):
        s = service.initiate("k", total_parts=2)
        service.upload_part(s.session_id, 1, b"a")
        service.abort(s.session_id)
        assert service.objects.part_path(s.session_id, 1)  # 路径可生成
        assert not service.objects.has_part(s.session_id, 1)
        again = service.abort(s.session_id)
        assert again.status is SessionStatus.ABORTED

    def test_abort_completed_rejected(self, service: UploadService):
        s = service.initiate("k", total_parts=1, overall_checksum=sha256_hex(b"a"))
        service.upload_part(s.session_id, 1, b"a")
        service.complete(s.session_id)
        with pytest.raises(GatewayError) as ei:
            service.abort(s.session_id)
        assert ei.value.code is ErrorCode.SESSION_COMPLETED


# --------------------------------------------------------------- progress
class TestProgress:
    def test_progress_reports_missing_and_next(self, service: UploadService):
        s = service.initiate("k", total_parts=3)
        service.upload_part(s.session_id, 1, b"aa")
        service.upload_part(s.session_id, 2, b"bb")
        _, progress = service.progress(s.session_id)
        assert progress.received_part_numbers == [1, 2]
        assert progress.bytes_received == 4
        assert progress.next_expected_part == 3
        assert progress.missing_part_numbers == [3]

    def test_progress_after_gap(self, service: UploadService):
        s = service.initiate("k", total_parts=3)
        service.upload_part(s.session_id, 1, b"a")
        service.upload_part(s.session_id, 3, b"c")
        _, progress = service.progress(s.session_id)
        assert progress.next_expected_part == 2
        assert progress.missing_part_numbers == [2]


# ------------------------------------------------------------ concurrency
class TestConcurrency:
    def test_concurrent_distinct_parts_no_corruption(self, service: UploadService):
        chunks = {i: (b"p%d-" % i) * 25 for i in range(1, 9)}
        content = b"".join(chunks[i] for i in range(1, 9))
        s = service.initiate(
            "conc", total_parts=8, overall_checksum=sha256_hex(content)
        )
        errors = upload_parts_concurrently(service, s.session_id, chunks)
        assert errors == []
        session = service.complete(s.session_id)
        assert session.status is SessionStatus.COMPLETED
        assert service.objects.read_object("conc") == content

    def test_concurrent_duplicate_submissions_idempotent(
        self, service: UploadService
    ):
        chunks = {1: b"same", 2: b"data"}
        s = service.initiate(
            "dup-conc",
            total_parts=2,
            overall_checksum=sha256_hex(b"samedata"),
        )
        errors = upload_parts_concurrently(
            service, s.session_id, chunks, repeat_each=5
        )
        assert errors == []
        service.complete(s.session_id)
        assert service.objects.read_object("dup-conc") == b"samedata"
        _, progress = service.progress(s.session_id)
        assert progress.received_parts == 2


# ------------------------------------------------------------- restart
class TestRestart:
    def test_unfinished_session_survives_restart(
        self, settings, service: UploadService
    ):
        s = service.initiate(
            "resume", total_parts=3, overall_checksum=sha256_hex(b"abc")
        )
        sid = s.session_id
        service.upload_part(sid, 1, b"a")
        service.upload_part(sid, 2, b"b")

        # 模拟进程重启：用相同数据目录构造全新的服务实例
        restarted = UploadService(settings)
        session = restarted.sessions.require(sid)
        assert session.status is SessionStatus.INITIATED
        assert set(session.parts) == {1, 2}
        assert restarted.objects.read_part(sid, 1) == b"a"
        # 继续上传剩余分块并完成
        restarted.upload_part(sid, 3, b"c")
        restarted.complete(sid)
        assert restarted.objects.read_object("resume") == b"abc"

    def test_completed_session_remains_terminal_after_restart(
        self, settings, service: UploadService
    ):
        s = service.initiate("done", total_parts=1, overall_checksum=sha256_hex(b"x"))
        sid = s.session_id
        service.upload_part(sid, 1, b"x")
        service.complete(sid)

        restarted = UploadService(settings)
        with pytest.raises(GatewayError) as ei:
            restarted.upload_part(sid, 1, b"x")
        assert ei.value.code is ErrorCode.SESSION_COMPLETED

    def test_aborted_session_remains_terminal_after_restart(
        self, settings, service: UploadService
    ):
        s = service.initiate("dead", total_parts=1)
        sid = s.session_id
        service.upload_part(sid, 1, b"x")
        service.abort(sid)

        restarted = UploadService(settings)
        with pytest.raises(GatewayError) as ei:
            restarted.upload_part(sid, 1, b"x")
        assert ei.value.code is ErrorCode.SESSION_ABORTED


# -------------------------------------------------------------- expiry
class TestExpiry:
    def test_idle_session_is_swept_and_write_rejected(
        self, service: UploadService
    ):
        s = service.initiate("old", total_parts=2)
        service.upload_part(s.session_id, 1, b"a")
        # 把会话人为调整为已过期
        stale = service.sessions.require(s.session_id)
        stale.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        service.sessions.save(stale)

        n = service.sweep_expired()
        assert n == 1
        assert not service.objects.has_part(s.session_id, 1)
        with pytest.raises(GatewayError) as ei:
            service.upload_part(s.session_id, 2, b"b")
        assert ei.value.code is ErrorCode.SESSION_EXPIRED

    def test_activity_extends_expiry(self, service: UploadService):
        s = service.initiate("active", total_parts=2)
        before = s.expires_at
        service.upload_part(s.session_id, 1, b"a")
        refreshed = service.sessions.require(s.session_id)
        assert refreshed.expires_at > before
        now = datetime.now(timezone.utc)
        assert service.sweep_expired(now) == 0

    def test_completed_and_aborted_sessions_are_not_swept(
        self, service: UploadService
    ):
        s1 = service.initiate("c", total_parts=1, overall_checksum=sha256_hex(b"a"))
        service.upload_part(s1.session_id, 1, b"a")
        service.complete(s1.session_id)
        s2 = service.initiate("a", total_parts=1)
        service.abort(s2.session_id)
        # 无论 expires_at 如何，终态会话都不应被 sweep 改动
        far_past = datetime.now(timezone.utc) - timedelta(days=1)
        for sid in (s1.session_id, s2.session_id):
            doc = service.sessions.require(sid)
            doc.expires_at = far_past
            service.sessions.save(doc)
        assert service.sweep_expired() == 0
