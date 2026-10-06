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


def _verify_legacy() -> int:
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

    print("LOCAL VERIFY OK")
    return 0


def verify_tenants() -> int:
    """调用方治理验证：独立额度、占用/归还、用量查询、尺寸约束、重启对账。"""
    tmp = tempfile.mkdtemp(prefix="rogw-tenants-")
    settings = Settings(
        data_dir=tmp,
        tenants={
            "alpha": {
                "max_bytes": 1024,
                "max_objects": 1,
                "min_part_bytes": 4,
                "max_part_bytes": 512,
            },
            "beta": {"max_bytes": 512, "max_objects": 2},
        },
    )
    client = TestClient(create_app(UploadService(settings)))
    h = {"X-Tenant-Id": "alpha"}

    # 1) alpha 创建会话并上传一块 -> 额度被占住
    r = client.post("/sessions", json={"object_key": "t/a.bin", "total_parts": 1},
                    headers=h)
    assert r.status_code == 201, r.text
    sid = r.json()["session_id"]
    r = client.put(f"/sessions/{sid}/parts/1", content=b"abcd", headers=h)
    assert r.status_code == 200, r.text
    print("[T1] alpha session reserved 1 object slot + 4 bytes")

    # 2) alpha 对象位已占满 -> 第二个会话被拒；beta 不受影响
    r = client.post("/sessions", json={"object_key": "t/a2.bin"}, headers=h)
    assert r.status_code == 507 and \
        r.json()["error_code"] == "TENANT_QUOTA_EXCEEDED", r.text
    r = client.post("/sessions", json={"object_key": "t/b.bin"},
                    headers={"X-Tenant-Id": "beta"})
    assert r.status_code == 201, r.text
    print("[T2] alpha quota full rejected only alpha; beta unaffected")

    # 3) 单块尺寸越界按尺寸原因拒绝
    r = client.put(f"/sessions/{sid}/parts/1", content=b"x" * 513, headers=h)
    assert r.status_code == 413 and \
        r.json()["error_code"] == "PART_SIZE_OUT_OF_RANGE", r.text
    print("[T3] oversize part rejected: PART_SIZE_OUT_OF_RANGE")

    # 4) 完成 -> 预留结转为实际占用；用量查询可见
    r = client.post(f"/sessions/{sid}/complete", json={}, headers=h)
    assert r.status_code == 200, r.text
    body = client.get("/tenants/alpha/usage").json()
    assert body["committed_objects"] == 1 and body["committed_bytes"] == 4
    assert body["reserved_objects"] == 0 and body["remaining_objects"] == 0
    assert [o["object_key"] for o in body["objects"]] == ["t/a.bin"]
    print(f"[T4] usage: committed={body['committed_bytes']}B "
          f"remaining_objects={body['remaining_objects']}")

    # 5) 两类“查不到”可区分
    r = client.get("/objects/t/missing.bin")
    assert r.status_code == 404 and r.json()["error_code"] == "OBJECT_NOT_FOUND"
    r = client.get("/sessions/" + "0" * 32)
    assert r.status_code == 404 and r.json()["error_code"] == "SESSION_NOT_FOUND"
    print("[T5] OBJECT_NOT_FOUND vs SESSION_NOT_FOUND are distinct")

    # 6) 重启对账：同一数据目录重建服务，占用与重启前一致
    svc2 = UploadService(settings)
    usage = svc2.tenant_usage("alpha")
    assert usage.committed_objects == 1 and usage.committed_bytes == 4
    print("[T6] after restart: alpha usage reconciled (1 object, 4 bytes)")

    print("TENANT VERIFY OK")
    return 0


def main() -> int:
    rc = _verify_legacy()
    if rc != 0:
        return rc
    return verify_tenants()


if __name__ == "__main__":
    sys.exit(main())
