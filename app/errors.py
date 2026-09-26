"""可区分的业务错误类型与错误码。

所有业务失败都抛出 :class:`GatewayError` 的子类，
HTTP 层统一翻译成结构化响应 ``{"error": {"code", "message"}}``。
"""

from __future__ import annotations


class GatewayError(Exception):
    """所有网关业务错误的基类。"""

    code: str = "gateway_error"
    status_code: int = 400

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class SessionNotFound(GatewayError):
    code = "session_not_found"
    status_code = 404


class SessionStateConflict(GatewayError):
    code = "session_state_conflict"
    status_code = 409


class PartIndexOutOfRange(GatewayError):
    code = "part_index_out_of_range"
    status_code = 400


class PartDigestMismatch(GatewayError):
    code = "part_digest_mismatch"
    status_code = 422


class PartsMissing(GatewayError):
    code = "parts_missing"
    status_code = 409


class WholeDigestMismatch(GatewayError):
    code = "whole_digest_mismatch"
    status_code = 422


class QuotaExceeded(GatewayError):
    code = "quota_exceeded"
    status_code = 507


class SessionExpired(GatewayError):
    code = "session_expired"
    status_code = 410


class ObjectNotFound(GatewayError):
    code = "object_not_found"
    status_code = 404


class ObjectExists(GatewayError):
    code = "object_exists"
    status_code = 409
