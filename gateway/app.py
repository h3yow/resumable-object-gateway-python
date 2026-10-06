"""FastAPI 应用装配与 HTTP 入口。

所有业务错误经统一异常处理器归类为稳定的 HTTP 状态码与 JSON 错误体；
后台线程按 ``sweep_interval_seconds`` 周期清理过期会话。
"""

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator

from fastapi import FastAPI, Header, Query, Request
from fastapi.responses import JSONResponse

from gateway.config import Settings, get_settings
from gateway.errors import ErrorCode, GatewayError
from gateway.models import (
    CompleteSessionRequest,
    CompleteSessionResponse,
    CreateSessionRequest,
    CreateSessionResponse,
    PartUploadResponse,
    SessionProgress,
    SessionStatus,
    TenantUsageResponse,
)
from gateway.service import UploadService

logger = logging.getLogger("gateway.api")

#: 业务错误码 -> HTTP 状态码
_ERROR_STATUS = {
    ErrorCode.SESSION_NOT_FOUND: 404,
    ErrorCode.SESSION_NOT_INITIATED: 400,
    ErrorCode.SESSION_COMPLETED: 409,
    ErrorCode.SESSION_ABORTED: 409,
    ErrorCode.SESSION_EXPIRED: 410,
    ErrorCode.PART_NUMBER_OUT_OF_RANGE: 416,
    ErrorCode.PART_CHECKSUM_MISMATCH: 422,
    ErrorCode.PART_MISSING: 409,
    ErrorCode.PARTS_NOT_CONTIGUOUS: 409,
    ErrorCode.OVERALL_CHECKSUM_MISMATCH: 422,
    ErrorCode.OBJECT_SIZE_EXCEEDED: 413,
    ErrorCode.QUOTA_EXCEEDED: 507,
    ErrorCode.OBJECT_ALREADY_EXISTS: 409,
    ErrorCode.OBJECT_NOT_FOUND: 404,
    ErrorCode.TENANT_NOT_FOUND: 404,
    ErrorCode.TENANT_QUOTA_EXCEEDED: 507,
    ErrorCode.PART_SIZE_OUT_OF_RANGE: 413,
}


def create_app(service: UploadService | None = None) -> FastAPI:
    """构造应用；测试可注入自定义 service。"""
    svc = service or UploadService(get_settings())
    settings: Settings = svc.settings

    @contextlib.asynccontextmanager
    async def _lifespan(_: FastAPI) -> AsyncIterator[None]:
        interval = settings.sweep_interval_seconds
        stop = asyncio.Event()

        async def _loop() -> None:
            while not stop.is_set():
                try:
                    await asyncio.to_thread(svc.sweep_expired)
                except Exception:  # noqa: BLE001 - 扫描异常不能杀死进程
                    logger.exception("expiry sweep failed")
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=interval)

        task = asyncio.create_task(_loop())
        try:
            yield
        finally:
            stop.set()
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    app = FastAPI(
        title="Resumable Object Gateway (local disk)",
        version="0.1.0",
        lifespan=_lifespan,
    )
    app.state.service = svc

    # ------------------------------------------------------ error handling
    @app.exception_handler(GatewayError)
    async def _on_gateway_error(_: Request, exc: GatewayError) -> JSONResponse:
        status = _ERROR_STATUS.get(exc.code, 400)
        logger.info(
            "http_error session=%s code=%s status=%d reason=%s",
            exc.session_id,
            exc.code.value,
            status,
            exc.message,
        )
        return JSONResponse(
            status_code=status,
            content={
                "error_code": exc.code.value,
                "message": exc.message,
                "session_id": exc.session_id,
            },
        )

    # ------------------------------------------------------------ routes
    @app.post("/sessions", response_model=CreateSessionResponse, status_code=201)
    async def create_session(
        body: CreateSessionRequest,
        x_tenant_id: str | None = Header(default=None),
    ) -> CreateSessionResponse:
        session = svc.initiate(
            object_key=body.object_key,
            part_size=body.part_size,
            total_parts=body.total_parts,
            overall_checksum=body.overall_checksum_sha256,
            tenant_id=x_tenant_id,
        )
        return CreateSessionResponse(
            session_id=session.session_id,
            status=session.status,
            expires_at=session.expires_at,
        )

    @app.put(
        "/sessions/{session_id}/parts/{part_number}",
        response_model=PartUploadResponse,
    )
    async def upload_part(
        session_id: str,
        part_number: int,
        request: Request,
        x_part_checksum_sha256: str | None = Header(default=None),
    ) -> PartUploadResponse:
        data = await request.body()
        session, record, duplicated = svc.upload_part(
            session_id=session_id,
            part_number=part_number,
            data=data,
            checksum=x_part_checksum_sha256,
        )
        return PartUploadResponse(
            session_id=session.session_id,
            part_number=record.number,
            size=record.size,
            checksum_sha256=record.checksum_sha256,
            etag=record.checksum_sha256,
            duplicated=duplicated,
        )

    @app.post("/sessions/{session_id}/complete", response_model=CompleteSessionResponse)
    async def complete_session(
        session_id: str, body: CompleteSessionRequest | None = None
    ) -> CompleteSessionResponse:
        total_parts = body.total_parts if body else None
        overall = body.overall_checksum_sha256 if body else None
        session = svc.complete(session_id, total_parts, overall)
        meta = svc.objects.object_meta(session.object_key)
        return CompleteSessionResponse(
            object_key=session.object_key,
            size=meta["size"],
            checksum_sha256=meta["checksum_sha256"],
        )

    @app.post("/sessions/{session_id}/abort")
    async def abort_session(session_id: str) -> dict:
        session = svc.abort(session_id)
        return {
            "session_id": session.session_id,
            "status": SessionStatus.ABORTED.value,
        }

    @app.get("/sessions/{session_id}")
    async def get_session(session_id: str) -> dict:
        session, progress = svc.progress(session_id)
        return {
            "session_id": session.session_id,
            "object_key": session.object_key,
            "status": session.status.value,
            "created_at": session.created_at.isoformat(),
            "updated_at": session.updated_at.isoformat(),
            "expires_at": session.expires_at.isoformat(),
            "progress": SessionProgress.model_validate(progress).model_dump(),
        }

    @app.get("/objects/{object_key:path}")
    async def get_object(object_key: str) -> dict:
        # 仅返回元信息，避免大对象直接走 JSON；内容下载在本地验证脚本中直读磁盘
        meta = svc.objects.object_meta(object_key)
        return meta

    @app.get("/tenants/{tenant_id}/usage", response_model=TenantUsageResponse)
    async def get_tenant_usage(tenant_id: str) -> TenantUsageResponse:
        return svc.tenant_usage(tenant_id)

    @app.post("/internal/sweep")
    async def sweep() -> dict:
        return {"expired": svc.sweep_expired()}

    # --------------------------------------------- background expiry sweep
    @contextlib.asynccontextmanager
    async def _lifespan(_: FastAPI) -> AsyncIterator[None]:
        interval = settings.sweep_interval_seconds
        stop = asyncio.Event()

        async def _loop() -> None:
            while not stop.is_set():
                try:
                    await asyncio.to_thread(svc.sweep_expired)
                except Exception:  # noqa: BLE001 - 扫描异常不能杀死进程
                    logger.exception("expiry sweep failed")
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=interval)

        task = asyncio.create_task(_loop())
        try:
            yield
        finally:
            stop.set()
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    return app
