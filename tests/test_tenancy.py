"""调用方治理单测：额度占住/转正/归还、并发完成边界、重启对账、
调用方隔离、用量查询、两类"查不到"与单块尺寸上下限。

断言重点：
1. 额度不足一律归类为 QUOTA_EXCEEDED，与其他失败原因可区分；
2. 占住/归还幂等，重复或并发操作不重复扣减、不漏还；
3. 重启后占用与重启前对得上，未完成会话可续传。
"""

import logging
import threading
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from gateway.app import create_app
from gateway.config import CallerPolicy, Settings
from gateway.errors import ErrorCode, GatewayError
from gateway.hashing import sha256_hex
from gateway.models import SessionStatus
from gateway.service import UploadService
from gateway.tenancy import DEFAULT_CALLER_ID


def _settings(tmp_path, callers: dict[str, CallerPolicy], **kw) -> Settings:
    base = dict(
        data_dir=str(tmp_path / "data"),
        session_ttl_seconds=3600,
        sweep_interval_seconds=3600,
        max_parts_per_upload=8,
        max_object_bytes=10**6,
        max_total_object_bytes=10**6,
        max_object_count=100,
        min_part_bytes=1,
        caller_policies=callers,
    )
    base.update(kw)
    return Settings(**base)


def _upload_one(
    svc: UploadService, caller: str, key: str, payload: bytes
) -> None:
    """工具：单个分块的对象从创建到完成。"""
    s = svc.initiate(
        key, total_parts=1, overall_checksum=sha256_hex(payload), caller_id=caller
    )
    svc.upload_part(s.session_id, 1, payload)
    svc.complete(s.session_id)


# ----------------------------------------------------------- caller resolve
class TestCallerResolve:
    def test_unknown_caller_rejected_at_initiate(self, tmp_path):
        svc = UploadService(_settings(tmp_path, {}))
        with pytest.raises(GatewayError) as ei:
            svc.initiate("k", caller_id="ghost")
        assert ei.value.code is ErrorCode.CALLER_NOT_FOUND

    def test_legacy_usage_without_caller_still_works(self, tmp_path):
        """老用法（不带 caller_id）归属默认调用方，行为不变。"""
        svc = UploadService(_settings(tmp_path, {}))
        _upload_one(svc, None, "legacy", b"hello")
        assert svc.objects.read_object("legacy") == b"hello"
        usage = svc.usage()  # 默认调用方
        assert usage.caller_id == DEFAULT_CALLER_ID
        assert usage.used_bytes == 5
        assert usage.used_objects == 1


# --------------------------------------------------------------- isolation
class TestCallerIsolation:
    def test_quota_exhaustion_only_blocks_that_caller(self, tmp_path, caplog):
        callers = {
            "alice": CallerPolicy(max_bytes=100, max_objects=1),
            "bob": CallerPolicy(max_bytes=100, max_objects=1),
        }
        svc = UploadService(_settings(tmp_path, callers))
        _upload_one(svc, "alice", "a1", b"x" * 10)

        # alice 对象数额度已满：第二个会话创建即被拒绝，原因是 QUOTA_EXCEEDED
        with caplog.at_level(logging.INFO, logger="gateway.tenancy"):
            with pytest.raises(GatewayError) as ei:
                svc.initiate("a2", caller_id="alice")
        assert ei.value.code is ErrorCode.QUOTA_EXCEEDED
        assert "QUOTA_EXCEEDED" in caplog.text
        assert "alice" in caplog.text

        # bob 与默认调用方完全不受影响
        _upload_one(svc, "bob", "b1", b"y" * 10)
        _upload_one(svc, None, "d1", b"z" * 10)
        assert svc.usage("bob").used_objects == 1
        assert svc.usage(DEFAULT_CALLER_ID).used_objects == 1
        assert svc.usage("alice").remaining_objects == 0

    def test_bytes_quota_is_per_caller(self, tmp_path):
        callers = {
            "alice": CallerPolicy(max_bytes=10, max_objects=10),
            "bob": CallerPolicy(max_bytes=1000, max_objects=10),
        }
        svc = UploadService(_settings(tmp_path, callers))
        _upload_one(svc, "alice", "a1", b"x" * 10)
        # alice 容量已满：完成时差额越界 -> QUOTA_EXCEEDED
        s = svc.initiate("a2", total_parts=1, caller_id="alice")
        svc.upload_part(s.session_id, 1, b"y")
        with pytest.raises(GatewayError) as ei:
            svc.complete(s.session_id)
        assert ei.value.code is ErrorCode.QUOTA_EXCEEDED
        assert not svc.objects.object_exists("a2")
        # bob 容量独立，不受 alice 占满影响
        _upload_one(svc, "bob", "b1", b"y" * 500)


# --------------------------------------------------- reservation lifecycle
class TestReservationLifecycle:
    def test_reserve_at_initiate_visible_in_usage(self, tmp_path):
        callers = {"alice": CallerPolicy(max_bytes=1000, max_objects=10)}
        svc = UploadService(_settings(tmp_path, callers))
        s = svc.initiate(
            "k", part_size=100, total_parts=3, caller_id="alice"
        )
        usage = svc.usage("alice")
        assert usage.reserved_bytes == 300  # part_size * total_parts 上界估计
        assert usage.reserved_objects == 1
        assert usage.remaining_objects == 9
        svc.abort(s.session_id)

    def test_complete_converts_reservation_to_committed(self, tmp_path):
        callers = {"alice": CallerPolicy(max_bytes=1000, max_objects=10)}
        svc = UploadService(_settings(tmp_path, callers))
        s = svc.initiate(
            "k", part_size=100, total_parts=1, caller_id="alice",
            overall_checksum=sha256_hex(b"ab"),
        )
        svc.upload_part(s.session_id, 1, b"ab")
        svc.complete(s.session_id)
        usage = svc.usage("alice")
        assert usage.reserved_bytes == 0 and usage.reserved_objects == 0
        assert usage.used_bytes == 2 and usage.used_objects == 1

    def test_abort_releases_reservation_idempotently(self, tmp_path):
        callers = {"alice": CallerPolicy(max_bytes=1000, max_objects=10)}
        svc = UploadService(_settings(tmp_path, callers))
        s = svc.initiate("k", part_size=100, total_parts=2, caller_id="alice")
        svc.upload_part(s.session_id, 1, b"x" * 100)
        svc.abort(s.session_id)
        svc.abort(s.session_id)  # 重复中止：归还必须幂等
        usage = svc.usage("alice")
        assert usage.reserved_bytes == 0 and usage.reserved_objects == 0
        assert usage.used_bytes == 0 and usage.used_objects == 0

    def test_expiry_releases_reservation(self, tmp_path):
        callers = {"alice": CallerPolicy(max_bytes=1000, max_objects=10)}
        svc = UploadService(_settings(tmp_path, callers))
        s = svc.initiate("k", part_size=100, total_parts=2, caller_id="alice")
        svc.upload_part(s.session_id, 1, b"x" * 100)
        stale = svc.sessions.require(s.session_id)
        stale.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        svc.sessions.save(stale)
        assert svc.sweep_expired() == 1
        usage = svc.usage("alice")
        assert usage.reserved_bytes == 0 and usage.reserved_objects == 0
        assert not svc.objects.object_exists("k")

    def test_failed_complete_keeps_reservation_then_abort_releases(
        self, tmp_path
    ):
        """完成失败（如摘要不符）会话仍存活，占住保留；中止后归还。"""
        callers = {"alice": CallerPolicy(max_bytes=1000, max_objects=10)}
        svc = UploadService(_settings(tmp_path, callers))
        s = svc.initiate("k", total_parts=1, caller_id="alice")
        svc.upload_part(s.session_id, 1, b"abc")
        with pytest.raises(GatewayError) as ei:
            svc.complete(s.session_id, overall_checksum="0" * 64)
        assert ei.value.code is ErrorCode.OVERALL_CHECKSUM_MISMATCH
        assert svc.usage("alice").reserved_objects == 1  # 占住仍在
        svc.abort(s.session_id)
        assert svc.usage("alice").reserved_objects == 0


# ---------------------------------------------------- concurrent completes
class TestConcurrentComplete:
    def test_concurrent_completes_never_exceed_bytes_quota(self, tmp_path):
        """3 个会话各 400 字节、额度 1000：并发完成最多成功 2 个，
        失败必须是 QUOTA_EXCEEDED，最终占用绝不越界。"""
        callers = {"alice": CallerPolicy(max_bytes=1000, max_objects=10)}
        svc = UploadService(_settings(tmp_path, callers))
        sessions = []
        for i in range(3):
            s = svc.initiate(f"k{i}", total_parts=1, caller_id="alice")
            svc.upload_part(s.session_id, 1, b"x" * 400)
            sessions.append(s.session_id)

        results: dict[str, str] = {}
        lock = threading.Lock()

        def _complete(sid: str) -> None:
            try:
                svc.complete(sid)
                outcome = "ok"
            except GatewayError as exc:
                outcome = exc.code.value
            with lock:
                results[sid] = outcome

        threads = [threading.Thread(target=_complete, args=(sid,)) for sid in sessions]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert sorted(results.values()) == [
            "QUOTA_EXCEEDED",
            "ok",
            "ok",
        ], results
        usage = svc.usage("alice")
        assert usage.used_bytes == 800 <= 1000
        assert usage.used_objects == 2
        # 失败者未落盘任何对象
        assert svc.objects.object_count() == 2

    def test_concurrent_duplicate_complete_counts_once(self, tmp_path):
        """同一会话的完成被并发重复提交：只转正一次额度。"""
        callers = {"alice": CallerPolicy(max_bytes=1000, max_objects=10)}
        svc = UploadService(_settings(tmp_path, callers))
        s = svc.initiate(
            "k", total_parts=1, overall_checksum=sha256_hex(b"ab"),
            caller_id="alice",
        )
        svc.upload_part(s.session_id, 1, b"ab")

        outcomes: list[str] = []
        lock = threading.Lock()

        def _complete() -> None:
            try:
                svc.complete(s.session_id)
                outcome = "ok"
            except GatewayError as exc:
                outcome = exc.code.value
            with lock:
                outcomes.append(outcome)

        threads = [threading.Thread(target=_complete) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert outcomes.count("ok") == 1
        assert outcomes.count("SESSION_COMPLETED") == 3
        usage = svc.usage("alice")
        assert usage.used_bytes == 2 and usage.used_objects == 1
        assert usage.reserved_bytes == 0 and usage.reserved_objects == 0

    def test_concurrent_complete_and_abort_no_double_release(self, tmp_path):
        """完成与中 Abort 并发：额度要么转正要么归还，绝不重复扣/漏还。"""
        callers = {"alice": CallerPolicy(max_bytes=1000, max_objects=10)}
        svc = UploadService(_settings(tmp_path, callers))

        def _swallow(fn, sid):
            # 竞争失败方的 SESSION_COMPLETED / SESSION_ABORTED 是预期业务结果
            try:
                fn(sid)
            except GatewayError:
                pass

        for i in range(6):
            s = svc.initiate(
                f"k{i}", total_parts=1, overall_checksum=sha256_hex(b"ab"),
                caller_id="alice",
            )
            svc.upload_part(s.session_id, 1, b"ab")
            t1 = threading.Thread(target=_swallow, args=(svc.complete, s.session_id))
            t2 = threading.Thread(target=_swallow, args=(svc.abort, s.session_id))
            t1.start()
            t2.start()
            t1.join()
            t2.join()
        usage = svc.usage("alice")
        # 每个会话要么完成（committed+2/1）要么中止（无占用）；占住必须清零
        assert usage.reserved_bytes == 0 and usage.reserved_objects == 0
        assert usage.used_objects == svc.objects.object_count()
        assert usage.used_bytes == 2 * usage.used_objects


# ------------------------------------------------------------------ restart
class TestRestartReconcile:
    def test_usage_and_resume_survive_restart(self, tmp_path):
        callers = {"alice": CallerPolicy(max_bytes=1000, max_objects=10)}
        settings = _settings(tmp_path, callers)
        svc = UploadService(settings)
        _upload_one(svc, "alice", "done", b"x" * 100)
        s = svc.initiate(
            "pending", part_size=50, total_parts=2, caller_id="alice",
            overall_checksum=sha256_hex(b"a" * 50 + b"b" * 50),
        )
        svc.upload_part(s.session_id, 1, b"a" * 50)
        before = svc.usage("alice")

        # 模拟进程重启：同一数据目录构造全新实例
        svc2 = UploadService(settings)
        after = svc2.usage("alice")
        assert after.used_bytes == before.used_bytes == 100
        assert after.used_objects == before.used_objects == 1
        assert after.reserved_bytes == before.reserved_bytes == 100
        assert after.reserved_objects == before.reserved_objects == 1

        # 未完成的会话接着传，完成后占用正确转正
        svc2.upload_part(s.session_id, 2, b"b" * 50)
        svc2.complete(s.session_id)
        final = svc2.usage("alice")
        assert final.used_bytes == 200 and final.used_objects == 2
        assert final.reserved_bytes == 0 and final.reserved_objects == 0

    def test_reconcile_drops_reservation_of_vanished_session(self, tmp_path):
        """崩溃窗口：会话目录丢失但账本留有占住 -> 重启对账回收。"""
        callers = {"alice": CallerPolicy(max_bytes=1000, max_objects=10)}
        settings = _settings(tmp_path, callers)
        svc = UploadService(settings)
        s = svc.initiate("k", part_size=100, total_parts=2, caller_id="alice")
        assert svc.usage("alice").reserved_bytes == 200
        # 模拟崩溃残留：直接删掉会话目录（绕过归还），账本占住仍在
        svc.sessions.discard(s.session_id)

        svc2 = UploadService(settings)
        usage = svc2.usage("alice")
        assert usage.reserved_bytes == 0 and usage.reserved_objects == 0

    def test_committed_not_doubled_after_restart(self, tmp_path):
        """已落盘对象按 meta.json 重算，重启不会重复计占用。"""
        callers = {"alice": CallerPolicy(max_bytes=1000, max_objects=10)}
        settings = _settings(tmp_path, callers)
        svc = UploadService(settings)
        _upload_one(svc, "alice", "o1", b"x" * 300)
        svc2 = UploadService(settings)
        svc3 = UploadService(settings)
        for svcN in (svc2, svc3):
            usage = svcN.usage("alice")
            assert usage.used_bytes == 300 and usage.used_objects == 1


# -------------------------------------------------------------------- usage
class TestUsageQuery:
    def test_usage_reports_objects_and_remaining(self, tmp_path):
        callers = {"alice": CallerPolicy(max_bytes=1000, max_objects=10)}
        svc = UploadService(_settings(tmp_path, callers))
        _upload_one(svc, "alice", "docs/a", b"aa")
        _upload_one(svc, "alice", "docs/b", b"bbb")
        _upload_one(svc, None, "other", b"c")
        usage = svc.usage("alice")
        assert usage.used_bytes == 5
        assert usage.remaining_bytes == 995
        assert usage.used_objects == 2
        assert usage.remaining_objects == 8
        assert [o.object_key for o in usage.objects] == ["docs/a", "docs/b"]
        # 默认调用方的对象不会混进 alice 名下
        assert all(o.caller_id == "alice" for o in usage.objects)

    def test_usage_of_unknown_caller_rejected(self, tmp_path):
        svc = UploadService(_settings(tmp_path, {}))
        with pytest.raises(GatewayError) as ei:
            svc.usage("ghost")
        assert ei.value.code is ErrorCode.CALLER_NOT_FOUND


# -------------------------------------------------------- not-found 区分
class TestNotFoundDistinction:
    def test_object_not_found_vs_session_not_found(self, tmp_path):
        svc = UploadService(_settings(tmp_path, {}))
        with pytest.raises(GatewayError) as eo:
            svc.objects.object_meta("missing-object")
        assert eo.value.code is ErrorCode.OBJECT_NOT_FOUND
        with pytest.raises(GatewayError) as es:
            svc.sessions.require("missing-session")
        assert es.value.code is ErrorCode.SESSION_NOT_FOUND
        assert eo.value.code is not es.value.code


# --------------------------------------------------------- part size bounds
class TestPartSizeBounds:
    def _svc(self, tmp_path) -> UploadService:
        callers = {
            "alice": CallerPolicy(
                max_bytes=10**6, max_objects=10,
                min_part_bytes=4, max_part_bytes=8,
            )
        }
        return UploadService(_settings(tmp_path, callers))

    def test_oversized_part_rejected_by_size_reason(self, tmp_path, caplog):
        svc = self._svc(tmp_path)
        s = svc.initiate("k", total_parts=1, caller_id="alice")
        with caplog.at_level(logging.INFO, logger="gateway.service"):
            with pytest.raises(GatewayError) as ei:
                svc.upload_part(s.session_id, 1, b"x" * 9)
        assert ei.value.code is ErrorCode.PART_SIZE_OUT_OF_RANGE
        assert "PART_SIZE_OUT_OF_RANGE" in caplog.text
        # 拒绝即不写盘，而不是先收下再报别的错
        assert not svc.objects.has_part(s.session_id, 1)

    def test_undersized_non_final_part_rejected(self, tmp_path):
        svc = self._svc(tmp_path)
        s = svc.initiate("k", total_parts=2, caller_id="alice")
        with pytest.raises(GatewayError) as ei:
            svc.upload_part(s.session_id, 1, b"ab")  # < min_part_bytes=4
        assert ei.value.code is ErrorCode.PART_SIZE_OUT_OF_RANGE
        # 最后一块不受下限约束
        svc.upload_part(s.session_id, 1, b"abcd")
        svc.upload_part(s.session_id, 2, b"z")
        svc.complete(s.session_id, overall_checksum=sha256_hex(b"abcdz"))
        assert svc.objects.read_object("k") == b"abcdz"

    def test_default_caller_has_no_size_limits_beyond_legacy(self, tmp_path):
        svc = UploadService(_settings(tmp_path, {}))
        s = svc.initiate("k", total_parts=1, overall_checksum=sha256_hex(b"a"))
        svc.upload_part(s.session_id, 1, b"a")
        svc.complete(s.session_id)
        assert svc.objects.read_object("k") == b"a"


# ------------------------------------------------------------- HTTP surface
class TestHttpGovernance:
    def _client(self, tmp_path) -> TestClient:
        callers = {
            "alice": CallerPolicy(max_bytes=100, max_objects=2),
        }
        svc = UploadService(_settings(tmp_path, callers))
        return TestClient(create_app(svc))

    def test_usage_endpoint(self, tmp_path):
        client = self._client(tmp_path)
        r = client.post(
            "/sessions",
            json={
                "object_key": "http-obj",
                "total_parts": 1,
                "overall_checksum_sha256": sha256_hex(b"hi"),
                "caller_id": "alice",
            },
        )
        assert r.status_code == 201, r.text
        sid = r.json()["session_id"]
        client.put(f"/sessions/{sid}/parts/1", content=b"hi")
        r = client.post(f"/sessions/{sid}/complete", json={})
        assert r.status_code == 200, r.text

        r = client.get("/tenants/alice/usage")
        assert r.status_code == 200
        body = r.json()
        assert body["caller_id"] == "alice"
        assert body["used_bytes"] == 2
        assert body["used_objects"] == 1
        assert body["remaining_bytes"] == 98
        assert [o["object_key"] for o in body["objects"]] == ["http-obj"]

    def test_usage_of_unknown_caller_is_404(self, tmp_path):
        client = self._client(tmp_path)
        r = client.get("/tenants/ghost/usage")
        assert r.status_code == 404
        assert r.json()["error_code"] == "CALLER_NOT_FOUND"

    def test_object_vs_session_not_found_over_http(self, tmp_path):
        client = self._client(tmp_path)
        r_obj = client.get("/objects/no-such-object")
        assert r_obj.status_code == 404
        assert r_obj.json()["error_code"] == "OBJECT_NOT_FOUND"
        r_sess = client.get("/sessions/no-such-session")
        assert r_sess.status_code == 404
        assert r_sess.json()["error_code"] == "SESSION_NOT_FOUND"

    def test_caller_via_header_and_quota_error_status(self, tmp_path):
        client = self._client(tmp_path)
        # X-Caller-Id 头同样可标识调用方
        for i in range(2):
            r = client.post(
                "/sessions",
                json={"object_key": f"h{i}", "total_parts": 1},
                headers={"X-Caller-Id": "alice"},
            )
            assert r.status_code == 201, r.text
            sid = r.json()["session_id"]
            client.put(f"/sessions/{sid}/parts/1", content=b"q")
            assert client.post(f"/sessions/{sid}/complete", json={}).status_code == 200
        # alice 对象数额度(2)已满 -> 507 QUOTA_EXCEEDED
        r = client.post(
            "/sessions",
            json={"object_key": "h2", "total_parts": 1},
            headers={"X-Caller-Id": "alice"},
        )
        assert r.status_code == 507
        assert r.json()["error_code"] == "QUOTA_EXCEEDED"
        # 默认调用方不受影响
        r = client.post("/sessions", json={"object_key": "h3", "total_parts": 1})
        assert r.status_code == 201
