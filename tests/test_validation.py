"""请求参数与摘要格式校验。"""

from __future__ import annotations

import pytest

from fastapi.testclient import TestClient


def test_invalid_digest_format_http(client: TestClient):
    sid = client.post("/sessions", json={
        "object_key": "k", "part_count": 1, "total_size": 3}).json()["session_id"]
    r = client.put(f"/sessions/{sid}/parts/1", content=b"abc",
                   headers={"x-part-digest": "not-hex"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_request"

    r = client.post(f"/sessions/{sid}/complete",
                    json={"whole_digest": "XYZ"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_request"


def test_empty_part_allowed(client: TestClient):
    """零字节块是合法边界：空内容 sha256 确定。"""
    import hashlib
    empty = hashlib.sha256(b"").hexdigest()
    sid = client.post("/sessions", json={
        "object_key": "k/empty", "part_count": 1, "total_size": 0,
        "whole_digest": empty}).json()["session_id"]
    assert client.put(f"/sessions/{sid}/parts/1", content=b"",
                      headers={"x-part-digest": empty}).status_code == 200
    r = client.post(f"/sessions/{sid}/complete", json={})
    assert r.status_code == 200
    assert client.get("/objects/k/empty/content").content == b""
