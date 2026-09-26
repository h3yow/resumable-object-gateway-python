"""pytest 公共夹具。

- 每个用例使用独立临时数据目录，互不干扰；
- 配置日志，单测输出中可看到会话标识、分块序号与判定依据；
- 提供 ``service`` / ``client`` 夹具与上传小工具。
"""

import logging
import threading
from collections.abc import Iterable

import pytest
from fastapi.testclient import TestClient

from gateway.app import create_app
from gateway.config import Settings
from gateway.hashing import sha256_hex
from gateway.service import UploadService


@pytest.fixture(autouse=True)
def _service_logs(caplog: pytest.LogCaptureFixture) -> Iterable[None]:
    """捕获 gateway 日志，便于断言“判定依据”确实被打印。"""
    caplog.set_level(logging.INFO, logger="gateway")
    yield


@pytest.fixture
def settings(tmp_path: pytest.TempPathFactory) -> Settings:
    return Settings(
        data_dir=str(tmp_path / "data"),
        session_ttl_seconds=3600,
        sweep_interval_seconds=3600,
        max_parts_per_upload=8,
        max_object_bytes=4096,
        max_total_object_bytes=5000,
        max_object_count=10,
        min_part_bytes=1,
    )


@pytest.fixture
def service(settings: Settings) -> UploadService:
    return UploadService(settings)


@pytest.fixture
def client(service: UploadService) -> TestClient:
    return TestClient(create_app(service))


@pytest.fixture
def make_multipart(service: UploadService):
    """工厂：创建会话并按给定字节块列表完成上传，返回 (session, content)。"""

    def _make(object_key: str, chunks: list[bytes], declare_total: bool = True,
              declare_checksum: bool = True):
        content = b"".join(chunks)
        session = service.initiate(
            object_key,
            total_parts=len(chunks) if declare_total else None,
            overall_checksum=sha256_hex(content) if declare_checksum else None,
        )
        for i, chunk in enumerate(chunks, start=1):
            service.upload_part(
                session.session_id, i, chunk, checksum=sha256_hex(chunk)
            )
        service.complete(session.session_id)
        # 重新读取，避免返回完成前的过期内存对象
        return service.sessions.require(session.session_id), content

    return _make


def upload_parts_concurrently(
    service: UploadService,
    session_id: str,
    chunks: dict[int, bytes],
    *,
    repeat_each: int = 1,
) -> list[Exception]:
    """多线程并发上传分块；repeat_each>1 时每块重复提交（验证幂等）。"""
    errors: list[Exception] = []
    errors_lock = threading.Lock()

    def _worker(number: int, payload: bytes) -> None:
        try:
            for _ in range(repeat_each):
                service.upload_part(
                    session_id, number, payload, checksum=sha256_hex(payload)
                )
        except Exception as exc:  # noqa: BLE001
            with errors_lock:
                errors.append(exc)

    threads = []
    for number, payload in chunks.items():
        t = threading.Thread(target=_worker, args=(number, payload))
        threads.append(t)
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return errors
