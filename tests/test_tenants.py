"""调用方治理单测：独立额度、预留/归还、重启对账、用量查询与尺寸约束。

覆盖业务：
1. 命名调用方独立额度与相互隔离（含与老用法的隔离）；
2. 会话开始占额度、完成结转、中止/过期归还、失败回滚；
3. 并发完成时的额度边界与幂等（不重复扣、不漏还）；
4. 进程重启后的占用对账与续传；
5. 用量查询、对象/会话两类“查不到”的可区分结论；
6. 单块尺寸上下限按尺寸原因明确拒绝。
"""

import logging
import threading
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from gateway.app import create_app
from gateway.config import Settings
from gateway.errors import ErrorCode, GatewayError
from gateway.hashing import sha256_hex
from gateway.service import UploadService

logger = logging.getLogger("gateway.testcase")

TENANTS = {
    "alpha": {
        "max_bytes": 1000,
        "max_objects": 2,
        "min_part_bytes": 4,
        "max_part_bytes": 100,
    },
    "beta": {"max_bytes": 500, "max_objects": 1},
    # 无单块尺寸限制、对象位充足，专用于容量额度测试
    "gamma": {"max_bytes": 250, "max_objects": 5},
}


@pytest.fixture(autouse=True)
def _case_banner(request):
    """让单测日志能看到每条用例覆盖的业务与起止。"""
    logger.info("case=%s phase=START", request.node.nodeid)
    yield
    logger.info("case=%s phase=END", request.node.nodeid)


@pytest.fixture
def tsettings(tmp_path) -> Settings:
    return Settings(
        data_dir=str(tmp_path / "data"),
        session_ttl_seconds=3600,
        sweep_interval_seconds=3600,
        max_parts_per_upload=8,
        max_object_bytes=4096,
        max_total_object_bytes=5000,
        max_object_count=10,
        min_part_bytes=1,
        tenants=TENANTS,
    )


@pytest.fixture
def tsvc(tsettings) -> UploadService:
    return UploadService(tsettings)


@pytest.fixture
def tclient(tsvc) -> TestClient:
    return TestClient(create_app(tsvc))


def _upload(tsvc: UploadService, session_id: str, number: int, data: bytes):
    return tsvc.upload_part(session_id, number, data, checksum=sha256_hex(data))


# ------------------------------------------------------- tenant resolution
class TestTenantResolution:
    def test_unknown_tenant_rejected_at_initiate(self, tsvc):
        with pytest.raises(GatewayError) as ei:
            tsvc.initiate("k", total_parts=1, tenant_id="ghost")
        assert ei.value.code is ErrorCode.TENANT_NOT_FOUND

    def test_legacy_usage_without_tenant_still_works(self, tsvc):
        s = tsvc.initiate("legacy/k", total_parts=1)
        _upload(tsvc, s.session_id, 1, b"abc")
        tsvc.complete(s.session_id)
        assert tsvc.objects.read_object("legacy/k") == b"abc"
        usage = tsvc.tenant_usage("")
        assert usage.committed_objects == 1
        assert usage.committed_bytes == 3


# --------------------------------------------------------------- isolation
class TestTenantIsolation:
    def test_object_quota_full_only_blocks_that_tenant(self, tsvc):
        # alpha 对象数额度为 2：占满后第三个会话被拒
        tsvc.initiate("a/1", total_parts=1, tenant_id="alpha")
        tsvc.initiate("a/2", total_parts=1, tenant_id="alpha")
        with pytest.raises(GatewayError) as ei:
            tsvc.initiate("a/3", total_parts=1, tenant_id="alpha")
        assert ei.value.code is ErrorCode.TENANT_QUOTA_EXCEEDED
        # beta 与默认调用方不受影响
        tsvc.initiate("b/1", total_parts=1, tenant_id="beta")
        tsvc.initiate("legacy/1", total_parts=1)

    def test_byte_quota_full_only_blocks_that_tenant(self, tsvc):
        s1 = tsvc.initiate("g/big", total_parts=1, tenant_id="gamma")
        _upload(tsvc, s1.session_id, 1, b"x" * 100)  # gamma 已占 100/250
        s2 = tsvc.initiate("g/big2", total_parts=1, tenant_id="gamma")
        with pytest.raises(GatewayError) as ei:
            _upload(tsvc, s2.session_id, 1, b"y" * 151)
        assert ei.value.code is ErrorCode.TENANT_QUOTA_EXCEEDED
        # 越界的字节没有占住额度
        assert tsvc.ledger.usage("gamma").reserved_bytes == 100
        # beta 有自己的 500 字节额度，不受影响
        s3 = tsvc.initiate("b/ok", total_parts=1, tenant_id="beta")
        _upload(tsvc, s3.session_id, 1, b"z" * 400)

    def test_quota_error_distinct_from_other_failures(self, tsvc):
        tsvc.initiate("a/1", total_parts=1, tenant_id="alpha")
        tsvc.initiate("a/2", total_parts=1, tenant_id="alpha")
        with pytest.raises(GatewayError) as ei:
            tsvc.initiate("a/3", total_parts=1, tenant_id="alpha")
        assert ei.value.code is ErrorCode.TENANT_QUOTA_EXCEEDED
        assert ei.value.code is not ErrorCode.QUOTA_EXCEEDED
        assert ei.value.code is not ErrorCode.SESSION_NOT_INITIATED


# --------------------------------------------------- reservation lifecycle
class TestReservationLifecycle:
    def test_complete_converts_reservation_to_committed(self, tsvc):
        s = tsvc.initiate("a/obj", total_parts=2, tenant_id="alpha")
        _upload(tsvc, s.session_id, 1, b"abcd")
        _upload(tsvc, s.session_id, 2, b"efgh")
        before = tsvc.ledger.usage("alpha")
        assert before.reserved_objects == 1
        assert before.reserved_bytes == 8
        tsvc.complete(s.session_id)
        after = tsvc.ledger.usage("alpha")
        assert after.reserved_objects == 0
        assert after.reserved_bytes == 0
        assert after.committed_objects == 1
        assert after.committed_bytes == 8

    def test_abort_releases_reservation(self, tsvc):
        s1 = tsvc.initiate("a/1", total_parts=1, tenant_id="alpha")
        tsvc.initiate("a/2", total_parts=1, tenant_id="alpha")
        _upload(tsvc, s1.session_id, 1, b"abcd")
        tsvc.abort(s1.session_id)
        usage = tsvc.ledger.usage("alpha")
        assert usage.reserved_objects == 1  # 只剩 a/2
        assert usage.reserved_bytes == 0
        # 归还后可以再开新会话
        tsvc.initiate("a/3", total_parts=1, tenant_id="alpha")

    def test_double_abort_does_not_double_release(self, tsvc):
        s = tsvc.initiate("a/1", total_parts=1, tenant_id="alpha")
        _upload(tsvc, s.session_id, 1, b"abcd")
        tsvc.abort(s.session_id)
        tsvc.abort(s.session_id)  # 幂等
        usage = tsvc.ledger.usage("alpha")
        assert usage.reserved_objects == 0
        assert usage.reserved_bytes == 0

    def test_expire_releases_reservation(self, tsvc):
        s = tsvc.initiate("a/1", total_parts=1, tenant_id="alpha")
        _upload(tsvc, s.session_id, 1, b"abcd")
        future = datetime.now(timezone.utc) + timedelta(seconds=7200)
        assert tsvc.sweep_expired(now=future) == 1
        usage = tsvc.ledger.usage("alpha")
        assert usage.reserved_objects == 0
        assert usage.reserved_bytes == 0
        # 额度归还后可以再开满两个会话
        tsvc.initiate("a/2", total_parts=1, tenant_id="alpha")
        tsvc.initiate("a/3", total_parts=1, tenant_id="alpha")

    def test_failed_part_write_refunds_bytes(self, tsvc, monkeypatch):
        s = tsvc.initiate("a/1", total_parts=1, tenant_id="alpha")

        def _boom(*args, **kwargs):
            raise RuntimeError("disk full")

        monkeypatch.setattr(tsvc.objects, "write_part", _boom)
        with pytest.raises(RuntimeError):
            _upload(tsvc, s.session_id, 1, b"abcd")
        assert tsvc.ledger.usage("alpha").reserved_bytes == 0


# ---------------------------------------------------------- concurrency
class TestConcurrentComplete:
    def test_default_tenant_concurrent_complete_never_exceeds_quota(
        self, tmp_path
    ):
        settings = Settings(
            data_dir=str(tmp_path / "d"),
            max_object_count=2,
            max_total_object_bytes=10**9,
            max_object_bytes=10**9,
        )
        svc = UploadService(settings)
        sessions = []
        for i in range(4):
            s = svc.initiate(f"k{i}", total_parts=1)
            _upload(svc, s.session_id, 1, b"0123456789")
            sessions.append(s)

        results: list[str] = []
        lock = threading.Lock()

        def _complete(sid: str) -> None:
            try:
                svc.complete(sid)
                outcome = "ok"
            except GatewayError as exc:
                outcome = exc.code.value
            with lock:
                results.append(outcome)

        threads = [threading.Thread(target=_complete, args=(s.session_id,))
                   for s in sessions]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert results.count("ok") == 2
        assert results.count(ErrorCode.QUOTA_EXCEEDED.value) == 2
        usage = svc.ledger.usage("")
        assert usage.committed_objects == 2  # 不多不少

    def test_named_tenant_concurrent_complete_stays_within_quota(self, tsvc):
        s1 = tsvc.initiate("a/1", total_parts=1, tenant_id="alpha")
        s2 = tsvc.initiate("a/2", total_parts=1, tenant_id="alpha")
        _upload(tsvc, s1.session_id, 1, b"aaaa")
        _upload(tsvc, s2.session_id, 1, b"bbbb")

        errors: list[Exception] = []
        threads = [
            threading.Thread(
                target=lambda sid: _run_complete(tsvc, sid, errors),
                args=(s.session_id,),
            )
            for s in (s1, s2)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == []
        usage = tsvc.ledger.usage("alpha")
        assert usage.committed_objects == 2
        assert usage.committed_bytes == 8
        assert usage.reserved_objects == 0
        assert usage.reserved_bytes == 0

    def test_complete_and_abort_race_leaves_ledger_consistent(self, tsvc):
        s = tsvc.initiate("a/race", total_parts=1, tenant_id="alpha")
        _upload(tsvc, s.session_id, 1, b"abcd")

        outcomes: list[str] = []
        lock = threading.Lock()

        def _run(fn, tag: str) -> None:
            try:
                fn(s.session_id)
                out = f"{tag}:ok"
            except GatewayError as exc:
                out = f"{tag}:{exc.code.value}"
            with lock:
                outcomes.append(out)

        t1 = threading.Thread(target=_run, args=(tsvc.complete, "complete"))
        t2 = threading.Thread(target=_run, args=(tsvc.abort, "abort"))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        assert sorted(o.split(":")[0] for o in outcomes) == ["abort", "complete"]
        assert sum(1 for o in outcomes if o.endswith(":ok")) == 1
        usage = tsvc.ledger.usage("alpha")
        # 无论谁赢：预留必须清零，且 committed 要么 0 要么恰好一次
        assert usage.reserved_objects == 0
        assert usage.reserved_bytes == 0
        assert usage.committed_objects in (0, 1)
        assert usage.committed_bytes in (0, 4)

    def test_repeated_complete_not_double_counted(self, tsvc):
        s = tsvc.initiate("a/1", total_parts=1, tenant_id="alpha")
        _upload(tsvc, s.session_id, 1, b"abcd")
        tsvc.complete(s.session_id)
        with pytest.raises(GatewayError) as ei:
            tsvc.complete(s.session_id)
        assert ei.value.code is ErrorCode.SESSION_COMPLETED
        assert tsvc.ledger.usage("alpha").committed_objects == 1


def _run_complete(svc: UploadService, session_id: str, errors: list) -> None:
    try:
        svc.complete(session_id)
    except Exception as exc:  # noqa: BLE001
        errors.append(exc)


# -------------------------------------------------------- restart reconcile
class TestRestartReconcile:
    def test_restart_rebuilds_usage_and_resumes(self, tsettings):
        svc1 = UploadService(tsettings)
        s = svc1.initiate("a/resume.bin", total_parts=2, tenant_id="alpha")
        _upload(svc1, s.session_id, 1, b"abcd")
        b = svc1.initiate("b/done.bin", total_parts=1, tenant_id="beta")
        _upload(svc1, b.session_id, 1, b"zzzz")
        svc1.complete(b.session_id)

        # 模拟进程重启：同一数据目录重建服务
        svc2 = UploadService(tsettings)
        ua = svc2.tenant_usage("alpha")
        assert ua.reserved_objects == 1
        assert ua.reserved_bytes == 4
        assert ua.committed_objects == 0
        ub = svc2.tenant_usage("beta")
        assert ub.committed_objects == 1
        assert ub.committed_bytes == 4
        assert ub.reserved_objects == 0

        # 未完成的会话重启后可续传并完成
        _upload(svc2, s.session_id, 2, b"efgh")
        svc2.complete(s.session_id)
        ua = svc2.tenant_usage("alpha")
        assert ua.committed_objects == 1
        assert ua.committed_bytes == 8
        assert ua.reserved_objects == 0
        assert ua.reserved_bytes == 0
        assert svc2.objects.read_object("a/resume.bin") == b"abcdefgh"

    def test_restart_does_not_double_count_committed(self, tsettings):
        svc1 = UploadService(tsettings)
        b = svc1.initiate("b/only.bin", total_parts=1, tenant_id="beta")
        _upload(svc1, b.session_id, 1, b"zzzz")
        svc1.complete(b.session_id)

        svc2 = UploadService(tsettings)
        ub = svc2.tenant_usage("beta")
        assert ub.committed_objects == 1  # 不多出一份
        assert ub.committed_bytes == 4
        # beta 额度为 1，已用满：新会话被拒，但其它调用方不受影响
        with pytest.raises(GatewayError) as ei:
            svc2.initiate("b/another", total_parts=1, tenant_id="beta")
        assert ei.value.code is ErrorCode.TENANT_QUOTA_EXCEEDED
        svc2.initiate("a/fine", total_parts=1, tenant_id="alpha")


# -------------------------------------------------------------- usage query
class TestUsageQuery:
    def test_usage_reflects_committed_reserved_and_objects(self, tsvc):
        s1 = tsvc.initiate("a/done", total_parts=1, tenant_id="alpha")
        _upload(tsvc, s1.session_id, 1, b"abcd")
        tsvc.complete(s1.session_id)
        s2 = tsvc.initiate("a/pending", total_parts=1, tenant_id="alpha")
        _upload(tsvc, s2.session_id, 1, b"wxyz")

        usage = tsvc.tenant_usage("alpha")
        assert usage.quota_max_bytes == 1000
        assert usage.quota_max_objects == 2
        assert usage.committed_bytes == 4
        assert usage.committed_objects == 1
        assert usage.reserved_bytes == 4
        assert usage.reserved_objects == 1
        assert usage.remaining_bytes == 1000 - 8
        assert usage.remaining_objects == 0
        assert [o.object_key for o in usage.objects] == ["a/done"]
        assert usage.objects[0].size == 4

    def test_unknown_tenant_usage_rejected(self, tsvc):
        with pytest.raises(GatewayError) as ei:
            tsvc.tenant_usage("ghost")
        assert ei.value.code is ErrorCode.TENANT_NOT_FOUND


# ------------------------------------------------------- part size bounds
class TestPartSizeBounds:
    def test_part_below_min_rejected_with_size_reason(self, tsvc):
        s = tsvc.initiate("a/small", total_parts=2, tenant_id="alpha")
        with pytest.raises(GatewayError) as ei:
            _upload(tsvc, s.session_id, 1, b"abc")  # min_part_bytes=4
        assert ei.value.code is ErrorCode.PART_SIZE_OUT_OF_RANGE
        assert ei.value.code is not ErrorCode.PART_CHECKSUM_MISMATCH
        # 拒绝后该块未占额度
        assert tsvc.ledger.usage("alpha").reserved_bytes == 0

    def test_part_above_max_rejected_with_size_reason(self, tsvc):
        s = tsvc.initiate("a/large", total_parts=1, tenant_id="alpha")
        with pytest.raises(GatewayError) as ei:
            _upload(tsvc, s.session_id, 1, b"x" * 101)  # max_part_bytes=100
        assert ei.value.code is ErrorCode.PART_SIZE_OUT_OF_RANGE

    def test_last_part_exempt_from_min(self, tsvc):
        s = tsvc.initiate("a/last", total_parts=2, tenant_id="alpha")
        _upload(tsvc, s.session_id, 1, b"abcd")
        _upload(tsvc, s.session_id, 2, b"z")  # 最后一块不受下限约束
        tsvc.complete(s.session_id)
        assert tsvc.objects.read_object("a/last") == b"abcdz"


# ---------------------------------------------------------------- HTTP API
class TestTenantApi:
    def test_full_flow_with_tenant_header(self, tclient):
        r = tclient.post(
            "/sessions",
            json={"object_key": "http/a.bin", "total_parts": 1},
            headers={"X-Tenant-Id": "alpha"},
        )
        assert r.status_code == 201
        sid = r.json()["session_id"]
        r = tclient.put(f"/sessions/{sid}/parts/1", content=b"abcd")
        assert r.status_code == 200
        r = tclient.post(f"/sessions/{sid}/complete", json={})
        assert r.status_code == 200

        r = tclient.get("/tenants/alpha/usage")
        assert r.status_code == 200
        body = r.json()
        assert body["committed_objects"] == 1
        assert body["committed_bytes"] == 4
        assert body["reserved_objects"] == 0
        assert [o["object_key"] for o in body["objects"]] == ["http/a.bin"]

    def test_unknown_tenant_rejected_via_http(self, tclient):
        r = tclient.post(
            "/sessions",
            json={"object_key": "x"},
            headers={"X-Tenant-Id": "ghost"},
        )
        assert r.status_code == 404
        assert r.json()["error_code"] == "TENANT_NOT_FOUND"
        r = tclient.get("/tenants/ghost/usage")
        assert r.status_code == 404
        assert r.json()["error_code"] == "TENANT_NOT_FOUND"

    def test_tenant_quota_exceeded_via_http(self, tclient):
        for key in ("a/1", "a/2"):
            r = tclient.post(
                "/sessions",
                json={"object_key": key, "total_parts": 1},
                headers={"X-Tenant-Id": "alpha"},
            )
            assert r.status_code == 201
        r = tclient.post(
            "/sessions",
            json={"object_key": "a/3", "total_parts": 1},
            headers={"X-Tenant-Id": "alpha"},
        )
        assert r.status_code == 507
        assert r.json()["error_code"] == "TENANT_QUOTA_EXCEEDED"

    def test_object_and_session_not_found_are_distinct(self, tclient):
        r = tclient.get("/objects/no/such-object")
        assert r.status_code == 404
        assert r.json()["error_code"] == "OBJECT_NOT_FOUND"
        r = tclient.get("/sessions/" + "0" * 32)
        assert r.status_code == 404
        assert r.json()["error_code"] == "SESSION_NOT_FOUND"
        assert r.json()["error_code"] != "OBJECT_NOT_FOUND"

    def test_part_size_violation_via_http(self, tclient):
        r = tclient.post(
            "/sessions",
            json={"object_key": "http/size.bin", "total_parts": 2},
            headers={"X-Tenant-Id": "alpha"},
        )
        sid = r.json()["session_id"]
        r = tclient.put(f"/sessions/{sid}/parts/1", content=b"ab")  # < min 4
        assert r.status_code == 413
        assert r.json()["error_code"] == "PART_SIZE_OUT_OF_RANGE"
