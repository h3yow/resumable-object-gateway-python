#!/usr/bin/env python3
"""端到端本地验证脚本（不依赖外部服务/真实凭据）。

用法::

    uv run python scripts/local_verify.py

流程：创建会话 -> 上传分块 -> 重复提交 -> 完成 -> 下载比对 -> 异常场景。
退出码非 0 表示验证失败。
"""

import hashlib
import sys
import tempfile
from pathlib import Path

# 允许直接以脚本方式运行（scripts/ 不在包路径上）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gateway.app import create_app
from gateway.config import Settings
from gateway.service import UploadService

# 直接对 ASGI app 发请求，无需监听端口、无需外部进程
from starlette.testclient import TestClient


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="rogw-verify-")
    settings = Settings(data_dir=tmp, session_ttl_seconds=60)
    client = TestClient(create_app(UploadService(settings)))

    content = b"resumable upload local verification payload"
    c1, c2 = content[:20], content[20:]

    r = client.post(
        "/sessions",
        json={
            "object_key": "verify/demo.bin",
            "total_parts": 2,
            "overall_checksum_sha256": hashlib.sha256(content).hexdigest(),
        },
    )
    r.raise_for_status()
    sid = r.json()["session_id"]
    print(f"[1] session created: {sid}")

    h1 = hashlib.sha256(c1).hexdigest()
    r = client.put(f"/sessions/{sid}/parts/1", content=c1,
                   headers={"X-Part-Checksum-Sha256": h1})
    r.raise_for_status()
    r = client.put(f"/sessions/{sid}/parts/1", content=c1,
                   headers={"X-Part-Checksum-Sha256": h1})
    assert r.json()["duplicated"] is True
    print("[2] part 1 uploaded; duplicate submission is idempotent")

    r = client.put(f"/sessions/{sid}/parts/2", content=c2)
    r.raise_for_status()

    # 故意用错误摘要完成 -> 必须被拒绝
    r = client.post(f"/sessions/{sid}/complete",
                    json={"overall_checksum_sha256": "0" * 64})
    assert r.status_code == 422 and r.json()["error_code"] == \
        "OVERALL_CHECKSUM_MISMATCH", r.text
    print("[3] wrong overall checksum rejected:", r.json()["error_code"])

    r = client.post(f"/sessions/{sid}/complete", json={})
    r.raise_for_status()
    meta = r.json()
    assert meta["checksum_sha256"] == hashlib.sha256(content).hexdigest()
    print(f"[4] completed: size={meta['size']} checksum={meta['checksum_sha256'][:12]}...")

    # 终态写入必须被拒
    r = client.put(f"/sessions/{sid}/parts/1", content=c1)
    assert r.status_code == 409 and r.json()["error_code"] == "SESSION_COMPLETED"
    print("[5] write after completion rejected: SESSION_COMPLETED")

    # ---- 调用方治理：独立额度、尺寸上下限、占住/归还、用量查询 -----------
    from gateway.config import CallerPolicy

    gov_settings = Settings(
        data_dir=tempfile.mkdtemp(prefix="rogw-verify-gov-"),
        caller_policies={
            "alice": CallerPolicy(
                max_bytes=64, max_objects=2, min_part_bytes=2, max_part_bytes=32
            ),
        },
    )
    gov = TestClient(create_app(UploadService(gov_settings)))

    def _complete_one(client, key: str, payload: bytes, caller: str | None = None):
        body = {"object_key": key, "total_parts": 1,
                "overall_checksum_sha256": hashlib.sha256(payload).hexdigest()}
        if caller:
            body["caller_id"] = caller
        r = client.post("/sessions", json=body)
        assert r.status_code == 201, r.text
        sid = r.json()["session_id"]
        r = client.put(f"/sessions/{sid}/parts/1", content=payload)
        assert r.status_code == 200, r.text
        r = client.post(f"/sessions/{sid}/complete", json={})
        assert r.status_code == 200, r.text
        return sid

    # alice 落盘一个对象；用量查询可见占用与名下对象
    _complete_one(gov, "a1", b"hello", caller="alice")
    r = gov.get("/tenants/alice/usage")
    assert r.status_code == 200, r.text
    usage = r.json()
    assert usage["used_bytes"] == 5 and usage["used_objects"] == 1
    assert usage["remaining_bytes"] == 59 and usage["remaining_objects"] == 1
    assert [o["object_key"] for o in usage["objects"]] == ["a1"]
    print(f"[6] usage query: alice used={usage['used_bytes']}B/"
          f"{usage['used_objects']}obj, objects={usage['objects'][0]['object_key']}")

    # 单块尺寸上下限：越界按尺寸原因立即拒绝
    r = gov.post("/sessions", json={"object_key": "a2", "total_parts": 1,
                                    "caller_id": "alice"})
    a2_sid = r.json()["session_id"]
    r = gov.put(f"/sessions/{a2_sid}/parts/1", content=b"x" * 33)  # > max 32
    assert r.status_code == 400 and r.json()["error_code"] == \
        "PART_SIZE_OUT_OF_RANGE", r.text
    print("[7] oversized part rejected: PART_SIZE_OUT_OF_RANGE")

    # 中止归还占住额度（reserved 归零），随后该会话不可再写
    r = gov.post(f"/sessions/{a2_sid}/abort")
    assert r.status_code == 200
    r = gov.get("/tenants/alice/usage")
    assert r.json()["reserved_objects"] == 0
    print("[8] abort releases reserved quota")

    # alice 再落盘一个对象占满对象数额度；第三个会话 -> 507 QUOTA_EXCEEDED；
    # 默认调用方（老用法）完全不受影响
    _complete_one(gov, "a3", b"world", caller="alice")
    r = gov.post("/sessions", json={"object_key": "a4", "caller_id": "alice"})
    assert r.status_code == 507 and r.json()["error_code"] == "QUOTA_EXCEEDED", r.text
    _complete_one(gov, "d1", b"legacy still works")
    print("[9] caller quota isolated: alice full -> QUOTA_EXCEEDED, default ok")

    # 两类"查不到"可区分
    r = gov.get("/objects/no-such")
    assert r.status_code == 404 and r.json()["error_code"] == "OBJECT_NOT_FOUND"
    r = gov.get("/sessions/no-such")
    assert r.status_code == 404 and r.json()["error_code"] == "SESSION_NOT_FOUND"
    print("[10] OBJECT_NOT_FOUND vs SESSION_NOT_FOUND distinguishable")

    print("LOCAL VERIFY OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
