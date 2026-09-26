"""Pydantic 请求/响应模型与会话状态定义。"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class SessionStatus(str, Enum):
    """会话状态机。

    PENDING:  已创建，等待分块写入。
    COMPLETE: 合并成功，对象已原子落盘（终态）。
    ABORTED:  主动中止，临时文件已清理（终态）。
    EXPIRED:  超过无活动 TTL 被清理（终态）。
    """

    PENDING = "pending"
    COMPLETE = "complete"
    ABORTED = "aborted"
    EXPIRED = "expired"


TERMINAL_STATUSES = frozenset(
    {SessionStatus.COMPLETE, SessionStatus.ABORTED, SessionStatus.EXPIRED}
)


class CreateSessionRequest(BaseModel):
    object_key: str = Field(min_length=1, max_length=1024)
    part_count: int = Field(ge=1, le=100_000)
    total_size: int = Field(ge=0)
    whole_digest: str | None = Field(default=None, description="整体 sha256（hex）")
    content_type: str | None = None


class CreateSessionResponse(BaseModel):
    session_id: str
    object_key: str
    part_count: int
    total_size: int
    expires_at: float
    status: SessionStatus


class PartInfo(BaseModel):
    number: int
    size: int
    digest: str | None = None
    received_at: float


class SessionProgress(BaseModel):
    session_id: str
    object_key: str
    status: SessionStatus
    part_count: int
    total_size: int
    received_parts: list[int]
    missing_parts: list[int]
    bytes_received: int
    expires_at: float
    updated_at: float
    whole_digest: str | None = None


class CompleteResult(BaseModel):
    session_id: str
    object_key: str
    size: int
    digest: str
    etag: str
    status: SessionStatus


class AbortResult(BaseModel):
    session_id: str
    object_key: str
    status: SessionStatus


class ObjectMeta(BaseModel):
    object_key: str
    size: int
    digest: str
    content_type: str | None = None
    created_at: float
