"""FastAPI HTTP 入口：把请求翻译成 UploadService 调用。

路由概览:

* ``POST /sessions``                 创建上传会话
* ``GET  /sessions/{sid}``           查询会话进度
* ``PUT  /sessions/{sid}/parts/{n}`` 写入/幂等重传一个分块
* ``POST /sessions/{sid}/complete``  校验并合并落盘
* ``POST /sessions/{sid}/abort``     主动中止
* ``POST /sessions/{sid}/sweep``     立即触发过期清理（本地验证用）
* ``GET  /objects/{key}``            读取对象元数据
* ``GET  /objects/{key}/content``    下载对象内容
* ``GET  /healthz``                  健康检查
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

from app.errors import GatewayError
from app.logging_setup import get_logger
from app.models import CreateSessionRequest
from app.service import UploadService


class CompleteBody(BaseModel):
    whole_digest: str | None = Field(
        default=None, description="整体 sha256（hex）；可覆盖创建时声明值")


class SweepBody(BaseModel):
    now: float | None = Field(default=None, description="注入当前时间，便于测试")


def create_app(service: UploadService) -> FastAPI:
    """构造挂载了全部路由的 FastAPI 应用。"""
    app = FastAPI(
        title="Resumable Object Gateway (local disk)",
        version="0.1.0",
    )
    log = get_logger()

    @app.exception_handler(GatewayError)
    async def _on_gateway_error(_: Request, exc: GatewayError) -> JSONResponse:
        log.warning("请求被拒绝: code=%s msg=%s", exc.code, exc.message)
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": exc.code, "message": exc.message}},
        )

    @app.exception_handler(ValueError)
    async def _on_value_error(_: Request, exc: ValueError) -> JSONResponse:
        return JSONResponse(
            status_code=400,
            content={"error": {"code": "invalid_request",
                               "message": str(exc)}},
        )

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"status": "ok"}

    @app.post("/sessions", status_code=201)
    async def create_session(req: CreateSessionRequest) -> dict:
        resp = service.create_session(
            object_key=req.object_key,
            part_count=req.part_count,
            total_size=req.total_size,
            whole_digest=req.whole_digest,
            content_type=req.content_type,
        )
        return resp.model_dump(mode="json")

    @app.get("/sessions/{session_id}")
    async def get_session(session_id: str) -> dict:
        return service.get_progress(session_id).model_dump(mode="json")

    @app.put("/sessions/{session_id}/parts/{number}")
    async def put_part(
        session_id: str, number: int, request: Request
    ) -> JSONResponse:
        if number < 1:
            return JSONResponse(
                status_code=400,
                content={"error": {
                    "code": "part_index_out_of_range",
                    "message": f"分块序号必须 >= 1: {number}"}},
            )
        data = await request.body()
        digest = request.headers.get("x-part-digest")
        info = service.put_part(session_id, number, data, digest)
        return JSONResponse(
            status_code=200,
            content=info.model_dump(mode="json"),
            headers={"X-Session-Id": session_id,
                     "X-Part-Number": str(number),
                     "X-Decision": "accepted_or_idempotent"},
        )

    @app.post("/sessions/{session_id}/complete")
    async def complete_session(session_id: str, body: CompleteBody) -> dict:
        result = service.complete_session(session_id, body.whole_digest)
        return result.model_dump(mode="json")

    @app.post("/sessions/{session_id}/abort")
    async def abort_session(session_id: str) -> dict:
        object_key, status = service.abort_session(session_id)
        return {"session_id": session_id, "object_key": object_key,
                "status": status}

    @app.post("/admin/sweep")
    async def sweep(body: SweepBody) -> dict:
        expired = service.sweep_expired(body.now)
        return {"expired": expired, "count": len(expired)}

    @app.get("/objects/{object_key:path}/content")
    async def read_object_content(object_key: str) -> Response:
        data, meta = service.read_object(object_key)
        return Response(
            content=data,
            media_type=meta.content_type or "application/octet-stream",
            headers={"X-Object-Digest": meta.digest,
                     "X-Object-Size": str(meta.size)},
        )

    @app.get("/objects/{object_key:path}")
    async def get_object(object_key: str) -> dict:
        meta = service.get_object_meta(object_key)
        return meta.model_dump(mode="json")

    return app
