"""HTTP 入口集成测试。

覆盖：创建/分块/完成/中止/进度的正常流程与错误归类；
并发分块；重启续传；摘要不匹配；过期会话清理。
"""

import threading
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from gateway.config import Settings
from gateway.hashing import sha256_hex
from gateway.service import UploadService

from .conftest import upload_parts_concurrently


def _create(client: TestClient, object_key: str, **kw):
    body = {"object_key": object_key, **kw}
    r = client.post("/sessions", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def _put_part(client: TestClient, sid: str, n: int, data: bytes):
    return client.put(
        f"/sessions/{sid}/parts/{n}",
        content=data,
        headers={"X-Part-Checksum-Sha256": sha256_hex(data)},
    )


class TestHappyPath:
    def test_full_flow_over_http(self, client: TestClient, service: UploadService):
        content = b"the quick brown fox"
        c1, c2 = b"the quick ", b"brown fox"
        sess = _create(
            client,
            "docs/fox.txt",
            total_parts=2,
            overall_checksum_sha256=sha256_hex(content),
        )
        sid = sess["session_id"]

        r = _put_part(client, sid, 1, c1)
        assert r.status_code == 200 and r.json()["duplicated"] is False
        r = _put_part(client, sid, 2, c2)
        assert r.status_code == 200

        r = client.get(f"/sessions/{sid}")
        assert r.status_code == 200
        assert r.json()["progress"]["received_parts"] == 2

        r = client.post(
            f"/sessions/{sid}/complete",
            json={"overall_checksum_sha256": sha256_hex(content)},
        )
        assert r.status_code == 200, r.text
        assert r.json() == {
            "object_key": "docs/fox.txt",
            "size": len(content),
            "checksum_sha256": sha256_hex(content),
        }
        assert service.objects.read_object("docs/fox.txt") == content

        r = client.get(f"/objects/docs/fox.txt")
        assert r.status_code == 200 and r.json()["size"] == len(content)

    def test_unknown_total_parts_completed_later(
        self, client: TestClient, service: UploadService
    ):
        sess = _create(client, "lazy")
        sid = sess["session_id"]
        _put_part(client, sid, 1, b"ab")
        _put_part(client, sid, 2, b"cd")
        r = client.post(
            f"/sessions/{sid}/complete",
            json={"total_parts": 2, "overall_checksum_sha256": sha256_hex(b"abcd")},
        )
        assert r.status_code == 200, r.text
        assert service.objects.read_object("lazy") == b"abcd"


class TestErrorClassification:
    def test_error_codes_map_to_distinct_http_statuses(self, client: TestClient):
        # SESSION_NOT_FOUND -> 404
        r = client.get("/sessions/does-not-exist")
        assert r.status_code == 404
        assert r.json()["error_code"] == "SESSION_NOT_FOUND"

        # SESSION_NOT_INITIATED -> 400
        r = client.post("/sessions", json={"object_key": "", "total_parts": 1})
        assert r.status_code == 400
        assert r.json()["error_code"] == "SESSION_NOT_INITIATED"

        sess = _create(client, "err", total_parts=2)
        sid = sess["session_id"]

        # PART_NUMBER_OUT_OF_RANGE -> 416
        r = client.put(f"/sessions/{sid}/parts/3", content=b"x")
        assert r.status_code == 416
        assert r.json()["error_code"] == "PART_NUMBER_OUT_OF_RANGE"

        # PART_CHECKSUM_MISMATCH -> 422
        r = client.put(
            f"/sessions/{sid}/parts/1",
            content=b"hello",
            headers={"X-Part-Checksum-Sha256": "f" * 64},
        )
        assert r.status_code == 422
        body = r.json()
        assert body["error_code"] == "PART_CHECKSUM_MISMATCH"
        assert body["session_id"] == sid
        assert "hello" not in body["message"] or "checksum" in body["message"]

        # PART_MISSING -> 409
        _put_part(client, sid, 1, b"a")
        r = client.post(f"/sessions/{sid}/complete", json={})
        assert r.status_code == 409
        assert r.json()["error_code"] == "PART_MISSING"

        # SESSION_ABORTED -> 409
        client.post(f"/sessions/{sid}/abort")
        r = _put_part(client, sid, 2, b"b")
        assert r.status_code == 409
        assert r.json()["error_code"] == "SESSION_ABORTED"

    def test_completed_session_conflicts(self, client: TestClient):
        sess = _create(
            client, "done", total_parts=1,
            overall_checksum_sha256=sha256_hex(b"z"),
        )
        sid = sess["session_id"]
        _put_part(client, sid, 1, b"z")
        client.post(f"/sessions/{sid}/complete", json={})
        r = _put_part(client, sid, 1, b"z")
        assert r.status_code == 409
        assert r.json()["error_code"] == "SESSION_COMPLETED"
        r = client.post(f"/sessions/{sid}/abort")
        assert r.status_code == 409
        assert r.json()["error_code"] == "SESSION_COMPLETED"

    def test_overall_checksum_mismatch(self, client: TestClient, service: UploadService):
        sess = _create(client, "bad-whole", total_parts=2)
        sid = sess["session_id"]
        _put_part(client, sid, 1, b"aa")
        _put_part(client, sid, 2, b"bb")
        r = client.post(
            f"/sessions/{sid}/complete",
            json={"overall_checksum_sha256": "0" * 64},
        )
        assert r.status_code == 422
        assert r.json()["error_code"] == "OVERALL_CHECKSUM_MISMATCH"
        # 失败不产生可见残缺对象
        assert not service.objects.object_exists("bad-whole")

    def test_object_size_and_quota_statuses(self, tmp_path):
        svc = UploadService(
            Settings(
                data_dir=str(tmp_path / "q"),
                max_object_bytes=5,
                max_total_object_bytes=100,
                max_object_count=100,
            )
        )
        client = TestClient(__import__("gateway.app", fromlist=["create_app"]).create_app(svc))
        sess = _create(client, "big", total_parts=1)
        _put_part(client, sess["session_id"], 1, b"x" * 6)
        r = client.post(f"/sessions/{sess['session_id']}/complete", json={})
        assert r.status_code == 413
        assert r.json()["error_code"] == "OBJECT_SIZE_EXCEEDED"

    def test_duplicate_object_key_conflict(self, client: TestClient):
        sess = _create(
            client,
            "uniq",
            total_parts=1,
            overall_checksum_sha256=sha256_hex(b"u"),
        )
        sid = sess["session_id"]
        _put_part(client, sid, 1, b"u")
        r = client.post(f"/sessions/{sid}/complete", json={})
        assert r.status_code == 200, r.text

        # 对象已存在，再为同一 key 创建会话必须被拒绝
        r = client.post("/sessions", json={"object_key": "uniq"})
        assert r.status_code == 409
        assert r.json()["error_code"] == "OBJECT_ALREADY_EXISTS"


class TestIdempotency:
    def test_repeated_put_is_idempotent_over_http(self, client: TestClient):
        sess = _create(client, "idem", total_parts=1)
        sid = sess["session_id"]
        r1 = _put_part(client, sid, 1, b"same")
        r2 = _put_part(client, sid, 1, b"same")
        r3 = _put_part(client, sid, 1, b"same")
        assert r1.json()["duplicated"] is False
        assert r2.json()["duplicated"] is True
        assert r3.json()["duplicated"] is True

    def test_repeated_put_with_other_checksum_rejected(self, client: TestClient):
        sess = _create(client, "idem2", total_parts=1)
        sid = sess["session_id"]
        _put_part(client, sid, 1, b"aaaa")
        r = client.put(
            f"/sessions/{sid}/parts/1",
            content=b"bbbb",
            headers={"X-Part-Checksum-Sha256": sha256_hex(b"bbbb")},
        )
        assert r.status_code == 422
        assert r.json()["error_code"] == "PART_CHECKSUM_MISMATCH"


class TestConcurrency:
    def test_concurrent_parts_over_http(self, service: UploadService):
        # 一部分线程走 service 直接调用（覆盖文件原子写），一部分走 HTTP
        sess = service.initiate(
            "multi",
            total_parts=6,
            overall_checksum=sha256_hex(b"".join(b"part%d" % i for i in range(1, 7))),
        )
        sid = sess.session_id
        chunks = {i: b"part%d" % i for i in range(1, 7)}
        errors = upload_parts_concurrently(service, sid, chunks)
        assert errors == []
        session = service.complete(sid)
        assert session.status.value == "COMPLETED"
        assert service.objects.read_object("multi") == b"".join(
            chunks[i] for i in range(1, 7)
        )

    def test_http_concurrent_puts(self, client: TestClient, service: UploadService):
        sess = _create(client, "http-conc", total_parts=4)
        sid = sess["session_id"]
        chunks = {i: b"chunk-%d|" % i for i in range(1, 5)}
        content = b"".join(chunks[i] for i in range(1, 5))
        errors: list[Exception] = []

        def worker(n: int) -> None:
            try:
                r = _put_part(client, sid, n, chunks[n])
                assert r.status_code == 200, r.text
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(1, 5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == []
        r = client.post(
            f"/sessions/{sid}/complete",
            json={"overall_checksum_sha256": sha256_hex(content)},
        )
        assert r.status_code == 200, r.text
        assert service.objects.read_object("http-conc") == content


class TestRestart:
    def test_resume_after_restart_over_http(
        self, settings: Settings, tmp_path
    ):
        svc = UploadService(settings)
        client = TestClient(__import__("gateway.app", fromlist=["create_app"]).create_app(svc))
        sess = _create(
            client,
            "resume-http",
            total_parts=3,
            overall_checksum_sha256=sha256_hex(b"xyz"),
        )
        sid = sess["session_id"]
        _put_part(client, sid, 1, b"x")
        _put_part(client, sid, 2, b"y")

        # 进程重启：新实例 + 新 client，复用同一 data_dir
        svc2 = UploadService(settings)
        client2 = TestClient(__import__("gateway.app", fromlist=["create_app"]).create_app(svc2))
        r = client2.get(f"/sessions/{sid}")
        assert r.status_code == 200
        assert r.json()["progress"]["received_part_numbers"] == [1, 2]

        _put_part(client2, sid, 3, b"z")
        r = client2.post(f"/sessions/{sid}/complete", json={})
        assert r.status_code == 200, r.text
        assert svc2.objects.read_object("resume-http") == b"xyz"


class TestExpiry:
    def test_sweep_endpoint_expires_old_session(
        self, client: TestClient, service: UploadService
    ):
        sess = _create(client, "stale", total_parts=2)
        sid = sess["session_id"]
        _put_part(client, sid, 1, b"a")
        doc = service.sessions.require(sid)
        doc.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        service.sessions.save(doc)

        r = client.post("/internal/sweep")
        assert r.status_code == 200 and r.json()["expired"] == 1

        # 过期后再写入 -> 410 GONE，错误码可区分
        r = _put_part(client, sid, 2, b"b")
        assert r.status_code == 410
        assert r.json()["error_code"] == "SESSION_EXPIRED"

        # 终态会话不会被重复 sweep
        assert client.post("/internal/sweep").json()["expired"] == 0
