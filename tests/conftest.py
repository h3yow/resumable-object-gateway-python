"""pytest 公共夹具：每个用例独立的临时数据目录、service 与 HTTP client。"""

from __future__ import annotations

import hashlib

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.http_api import create_app
from app.logging_setup import LOGGER_NAME
from app.service import UploadService


def sha(data: bytes) -> str:
    """测试辅助：计算字节串的 sha256 hex。"""
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        data_dir=str(tmp_path / "data"),
        session_ttl_seconds=3600,
        enable_sweeper=False,
    )


@pytest.fixture
def service(settings) -> UploadService:
    svc = UploadService(settings)
    svc.start()
    yield svc
    svc.shutdown()


@pytest.fixture
def client(service) -> TestClient:
    app = create_app(service)
    with TestClient(app) as c:
        yield c


@pytest.fixture
def make_session(service):
    """创建会话的便捷工厂，返回 (sid, parts, total_digest)。"""
    import hashlib as _hl

    def _make(parts: list[bytes], object_key: str = "k/file.bin",
              declare_digest: bool = True,
              content_type: str | None = "application/octet-stream"):
        total = b"".join(parts)
        resp = service.create_session(
            object_key=object_key,
            part_count=len(parts),
            total_size=len(total),
            whole_digest=_hl.sha256(total).hexdigest()
            if declare_digest else None,
            content_type=content_type,
        )
        return resp.session_id, total

    return _make


def assert_log_contains(records, session_id: str, part: int | None,
                        keyword: str) -> None:
    """断言日志中出现包含 sid、分块序号与判定依据关键字的记录。"""
    for record in records:
        if record.name != LOGGER_NAME:
            continue
        msg = record.getMessage()
        if session_id in msg and keyword in msg:
            if part is None or f"part={part}" in msg:
                return
    formatted = "\n".join(r.getMessage() for r in records)
    raise AssertionError(
        f"未找到包含 sid={session_id} part={part} 依据='{keyword}' 的日志，"
        f"实际日志:\n{formatted}")
