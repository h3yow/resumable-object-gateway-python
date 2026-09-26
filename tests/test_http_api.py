"""HTTP 入口的端到端行为与错误码归类。"""

from __future__ import annotations

import hashlib

from fastapi.testclient import TestClient


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def test_full_flow_over_http(client: TestClient):
    parts = [b"one-", b"two-", b"three"]
    total = b"".join(parts)
    r = client.post("/sessions", json={
        "object_key": "img/a.bin", "part_count": 3,
        "total_size": len(total), "whole_digest": _sha(total),
        "content_type": "application/octet-stream"})
    assert r.status_code == 201, r.text
    sid = r.json()["session_id"]

    # 重复 PUT 同一分块
    for _ in range(2):
        rr = client.put(
            f"/sessions/{sid}/parts/1", content=parts[0],
            headers={"x-part-digest": _sha(parts[0])})
        assert rr.status_code == 200, rr.text

    client.put(f"/sessions/{sid}/parts/2", content=parts[1],
               headers={"x-part-digest": _sha(parts[1])})
    client.put(f"/sessions/{sid}/parts/3", content=parts[2],
               headers={"x-part-digest": _sha(parts[2])})

    prog = client.get(f"/sessions/{sid}").json()
    assert prog["received_parts"] == [1, 2, 3] and not prog["missing_parts"]

    r = client.post(f"/sessions/{sid}/complete", json={})
    assert r.status_code == 200, r.text
    assert r.json()["digest"] == _sha(total)

    # 内容下载与元数据
    r = client.get(f"/objects/img/a.bin/content")
    assert r.status_code == 200 and r.content == total
    r = client.get("/objects/img/a.bin")
    assert r.json()["size"] == len(total)


def test_error_classification_over_http(client: TestClient):
    # 会话不存在
    assert client.get("/sessions/nope").status_code == 404
    r = client.put("/sessions/nope/parts/1", content=b"x")
    assert r.status_code == 404 and r.json()["error"]["code"] == "session_not_found"

    sid = client.post("/sessions", json={
        "object_key": "k", "part_count": 1, "total_size": 3,
        "whole_digest": _sha(b"abc")}).json()["session_id"]

    # 序号越界
    r = client.put(f"/sessions/{sid}/parts/0", content=b"x")
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "part_index_out_of_range"
    r = client.put(f"/sessions/{sid}/parts/2", content=b"xxxx")
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "part_index_out_of_range"

    # 单块摘要不匹配
    r = client.put(f"/sessions/{sid}/parts/1", content=b"abc",
                   headers={"x-part-digest": _sha(b"zzz")})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "part_digest_mismatch"

    # 分块缺失时完成
    r = client.post(f"/sessions/{sid}/complete", json={})
    assert r.status_code == 409 and r.json()["error"]["code"] == "parts_missing"

    # 正确完成
    client.put(f"/sessions/{sid}/parts/1", content=b"abc",
               headers={"x-part-digest": _sha(b"abc")})
    r = client.post(f"/sessions/{sid}/complete",
                    json={"whole_digest": _sha(b"abc")})
    assert r.status_code == 200, r.text

    # 终态再写 -> 409
    r = client.put(f"/sessions/{sid}/parts/1", content=b"abc")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "session_state_conflict"


def test_abort_and_expire_over_http(client: TestClient):
    sid = client.post("/sessions", json={
        "object_key": "k2", "part_count": 1, "total_size": 1}).json()["session_id"]
    r = client.post(f"/sessions/{sid}/abort")
    assert r.status_code == 200 and r.json()["status"] == "aborted"
    r = client.put(f"/sessions/{sid}/parts/1", content=b"x")
    assert r.status_code == 409

    # 管理接口注入过期时间
    sid2 = client.post("/sessions", json={
        "object_key": "k3", "part_count": 1, "total_size": 1}).json()["session_id"]
    r = client.post("/admin/sweep",
                    json={"now": 9_999_999_999})
    assert r.status_code == 200 and sid2 in r.json()["expired"]
    r = client.put(f"/sessions/{sid2}/parts/1", content=b"x")
    assert r.status_code == 410
    assert r.json()["error"]["code"] == "session_expired"


def test_invalid_request_body(client: TestClient):
    r = client.post("/sessions", json={
        "object_key": "", "part_count": 0, "total_size": -1})
    assert r.status_code == 422
