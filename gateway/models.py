"""枚举、会话模型与 API 请求/响应模型。"""

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field


class SessionStatus(str, Enum):
    INITIATED = "INITIATED"
    COMPLETED = "COMPLETED"
    ABORTED = "ABORTED"
    EXPIRED = "EXPIRED"


class PartRecord(BaseModel):
    number: int
    size: int
    checksum_sha256: str
    uploaded_at: datetime


class Session(BaseModel):
    session_id: str
    object_key: str
    status: SessionStatus
    part_size: int
    total_parts: int | None = None
    overall_checksum_sha256: str | None = None
    caller_id: str = "default"
    parts: dict[int, PartRecord] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime
    expires_at: datetime


class CreateSessionRequest(BaseModel):
    object_key: str
    part_size: int = 0
    total_parts: int | None = None
    overall_checksum_sha256: str | None = None
    caller_id: str | None = None


class CreateSessionResponse(BaseModel):
    session_id: str
    status: SessionStatus
    expires_at: datetime


class PartUploadResponse(BaseModel):
    session_id: str
    part_number: int
    size: int
    checksum_sha256: str
    etag: str
    duplicated: bool


class SessionProgress(BaseModel):
    received_part_numbers: list[int]
    received_parts: int
    total_parts: int | None
    bytes_received: int
    next_expected_part: int | None
    missing_part_numbers: list[int]


class CompleteSessionRequest(BaseModel):
    total_parts: int | None = None
    overall_checksum_sha256: str | None = None


class CompleteSessionResponse(BaseModel):
    object_key: str
    size: int
    checksum_sha256: str


class ErrorResponse(BaseModel):
    error_code: str
    message: str
    session_id: str | None = None
    detail: dict | None = None


class ObjectMetaView(BaseModel):
    """治理查询中单个对象的元信息视图。"""

    object_key: str
    size: int
    checksum_sha256: str
    caller_id: str | None = None
    created_at: str | None = None


class CallerUsageResponse(BaseModel):
    """某个调用方的额度占用视图（committed=已落盘，reserved=会话占住）。"""

    caller_id: str
    used_bytes: int
    used_objects: int
    reserved_bytes: int
    reserved_objects: int
    limit_bytes: int
    limit_objects: int
    remaining_bytes: int
    remaining_objects: int
    min_part_bytes: int
    max_part_bytes: int | None
    objects: list[ObjectMetaView]
