"""运行期可配置项（环境变量 + 数据类）。

所有配置均可通过大写环境变量覆盖，例如：
    GATEWAY_DATA_DIR=/tmp/demo GATEWAY_SESSION_TTL_SECONDS=60
"""

import json
import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    """服务配置。"""

    #: 本地数据根目录（会话元数据、暂存分块、已落盘对象均在其下）
    data_dir: str = ".local-data"
    #: 会话无活动后的存活秒数，超过即视为可过期清理
    session_ttl_seconds: int = 3600
    #: 后台过期扫描间隔（秒）；测试中可直接调用 sweep_expired
    sweep_interval_seconds: int = 300
    #: 单会话允许的最大分块数量
    max_parts_per_upload: int = 1000
    #: 单个对象的最大字节数（完成合并时校验）
    max_object_bytes: int = 64 * 1024 * 1024
    #: 对象存储总容量配额（字节）
    max_total_object_bytes: int = 1024 * 1024 * 1024
    #: 对象存储总数量配额
    max_object_count: int = 10_000
    #: 单个分块的最小字节数（最后一块不受此限，在服务层判定）
    min_part_bytes: int = 1
    #: 命名调用方配额表：name -> {max_bytes, max_objects, min_part_bytes, max_part_bytes}
    #: 未在此表中的命名调用方一律拒绝；不带调用方标识的老用法走上面的全局限额
    tenants: dict = field(default_factory=dict)


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return int(raw)


def _env_tenants() -> dict:
    """解析 GATEWAY_TENANTS（JSON 对象）；未设置或为空时返回空表。"""
    raw = os.environ.get("GATEWAY_TENANTS")
    if raw is None or raw.strip() == "":
        return {}
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise ValueError("GATEWAY_TENANTS must be a JSON object")
    return parsed


def get_settings() -> Settings:
    """从环境变量读取配置；未设置时使用默认值。"""
    return Settings(
        data_dir=os.environ.get("GATEWAY_DATA_DIR", ".local-data"),
        session_ttl_seconds=_env_int("GATEWAY_SESSION_TTL_SECONDS", 3600),
        sweep_interval_seconds=_env_int("GATEWAY_SWEEP_INTERVAL_SECONDS", 300),
        max_parts_per_upload=_env_int("GATEWAY_MAX_PARTS_PER_UPLOAD", 1000),
        max_object_bytes=_env_int("GATEWAY_MAX_OBJECT_BYTES", 64 * 1024 * 1024),
        max_total_object_bytes=_env_int(
            "GATEWAY_MAX_TOTAL_OBJECT_BYTES", 1024 * 1024 * 1024
        ),
        max_object_count=_env_int("GATEWAY_MAX_OBJECT_COUNT", 10_000),
        min_part_bytes=_env_int("GATEWAY_MIN_PART_BYTES", 1),
        tenants=_env_tenants(),
    )


def data_root(settings: Settings) -> Path:
    root = Path(settings.data_dir)
    return root
