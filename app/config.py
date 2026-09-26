"""运行期可配置项。

所有配置均可通过构造参数或环境变量覆盖，默认值面向本地验证，
不依赖任何外部服务或真实凭据。

环境变量统一使用 ``ROG_`` 前缀，例如 ``ROG_DATA_DIR``、
``ROG_SESSION_TTL_SECONDS``、``ROG_MAX_PART_BYTES``、
``ROG_MAX_TOTAL_BYTES``、``ROG_MAX_OBJECT_COUNT``、
``ROG_ENABLE_SWEEPER``、``ROG_SWEEPER_INTERVAL_SECONDS``。
"""

from __future__ import annotations

import os
from dataclasses import dataclass


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw not in (None, "") else default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    return float(raw) if raw not in (None, "") else default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    """网关配置。

    属性:
        data_dir: 本地磁盘根目录（模拟对象存储）。
        session_ttl_seconds: 会话无活动过期时间。
        max_session_bytes: 单个上传会话允许的总字节数。
        max_part_bytes: 单个分块允许的最大字节数。
        max_total_bytes: 所有已落盘对象总容量上限。
        max_object_count: 已落盘对象数量上限。
        enable_sweeper: 是否启动后台过期会话清理线程。
        sweeper_interval_seconds: 后台清理扫描间隔。
    """

    data_dir: str = "./.local-data"
    session_ttl_seconds: int = 3600
    max_session_bytes: int = 100 * 1024 * 1024
    max_part_bytes: int = 16 * 1024 * 1024
    max_total_bytes: int = 1024 * 1024 * 1024
    max_object_count: int = 10000
    enable_sweeper: bool = True
    sweeper_interval_seconds: float = 30.0

    @staticmethod
    def from_env() -> "Settings":
        """从 ``ROG_`` 前缀环境变量加载配置，缺省值与数据类一致。"""
        return Settings(
            data_dir=os.environ.get("ROG_DATA_DIR", "./.local-data"),
            session_ttl_seconds=_env_int("ROG_SESSION_TTL_SECONDS", 3600),
            max_session_bytes=_env_int(
                "ROG_MAX_SESSION_BYTES", 100 * 1024 * 1024
            ),
            max_part_bytes=_env_int("ROG_MAX_PART_BYTES", 16 * 1024 * 1024),
            max_total_bytes=_env_int(
                "ROG_MAX_TOTAL_BYTES", 1024 * 1024 * 1024
            ),
            max_object_count=_env_int("ROG_MAX_OBJECT_COUNT", 10000),
            enable_sweeper=_env_bool("ROG_ENABLE_SWEEPER", True),
            sweeper_interval_seconds=_env_float(
                "ROG_SWEEPER_INTERVAL_SECONDS", 30.0
            ),
        )
