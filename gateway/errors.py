"""可区分的错误码与服务异常。"""

from enum import Enum


class ErrorCode(str, Enum):
    SESSION_NOT_FOUND = "SESSION_NOT_FOUND"
    SESSION_NOT_INITIATED = "SESSION_NOT_INITIATED"
    SESSION_COMPLETED = "SESSION_COMPLETED"
    SESSION_ABORTED = "SESSION_ABORTED"
    SESSION_EXPIRED = "SESSION_EXPIRED"
    PART_NUMBER_OUT_OF_RANGE = "PART_NUMBER_OUT_OF_RANGE"
    PART_CHECKSUM_MISMATCH = "PART_CHECKSUM_MISMATCH"
    PART_MISSING = "PART_MISSING"
    PARTS_NOT_CONTIGUOUS = "PARTS_NOT_CONTIGUOUS"
    OVERALL_CHECKSUM_MISMATCH = "OVERALL_CHECKSUM_MISMATCH"
    OBJECT_SIZE_EXCEEDED = "OBJECT_SIZE_EXCEEDED"
    QUOTA_EXCEEDED = "QUOTA_EXCEEDED"
    OBJECT_ALREADY_EXISTS = "OBJECT_ALREADY_EXISTS"
    #: 查询不存在的对象（与不存在的会话可区分）
    OBJECT_NOT_FOUND = "OBJECT_NOT_FOUND"
    #: 未配置/不存在的调用方
    TENANT_NOT_FOUND = "TENANT_NOT_FOUND"
    #: 调用方专属额度不足（与全局 QUOTA_EXCEEDED 可区分）
    TENANT_QUOTA_EXCEEDED = "TENANT_QUOTA_EXCEEDED"
    #: 分块尺寸越出调用方配置的上下限
    PART_SIZE_OUT_OF_RANGE = "PART_SIZE_OUT_OF_RANGE"


class GatewayError(Exception):
    """所有业务错误的基类，携带可区分错误码与判定依据。"""

    def __init__(self, code: ErrorCode, message: str, *, session_id: str | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.session_id = session_id
