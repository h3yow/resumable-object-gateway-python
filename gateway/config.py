"""运行期可配置项（环境变量 + 数据类）。

所有配置均可通过大写环境变量覆盖，例如：
    GATEWAY_DATA_DIR=/tmp/demo GATEWAY_SESSION_TTL_SECONDS=60
"""

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class CallerPolicy:
    """单个调用方的治理策略。

    - ``max_bytes`` / ``max_objects``：该调用方独立的容量与对象数量额度
      （committed 已落盘 + reserved 会话占住 合计不得越过上界）；
    - ``min_part_bytes`` / ``max_part_bytes``：单块大小上下限，
      ``max_part_bytes=None`` 表示不设上限。
    """

    max_bytes: int
    max_objects: int
    min_part_bytes: int = 1
    max_part_bytes: int | None = None


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
    #: 默认调用方（老用法，不显式携带 caller_id）的容量额度（字节）
    max_total_object_bytes: int = 1024 * 1024 * 1024
    #: 默认调用方的对象数量额度
    max_object_count: int = 10_000
    #: 默认调用方的单块最小字节数（最后一块不受此限，在服务层判定）
    min_part_bytes: int = 1
    #: 具名调用方策略表；缺省为空，即只有默认调用方
    caller_policies: dict[str, CallerPolicy] = field(default_factory=dict)


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return int(raw)


def _env_caller_policies() -> dict[str, CallerPolicy]:
    """从环境变量解析具名调用方策略。

    格式：``GATEWAY_CALLERS=alice,bob`` 声明调用方名单；每个调用方的额度经
    ``GATEWAY_CALLER_<NAME>_MAX_BYTES`` / ``_MAX_OBJECTS`` /
    ``_MIN_PART_BYTES`` / ``_MAX_PART_BYTES`` 覆盖（NAME 为大写、非字母数字
    转下划线）。未显式给出的项回退到默认调用方对应配置。
    """
    raw = os.environ.get("GATEWAY_CALLERS", "").strip()
    if not raw:
        return {}
    base_max_bytes = _env_int("GATEWAY_MAX_TOTAL_OBJECT_BYTES", 1024 * 1024 * 1024)
    base_max_objects = _env_int("GATEWAY_MAX_OBJECT_COUNT", 10_000)
    base_min_part = _env_int("GATEWAY_MIN_PART_BYTES", 1)
    policies: dict[str, CallerPolicy] = {}
    for name in (n.strip() for n in raw.split(",")):
        if not name:
            continue
        env = "GATEWAY_CALLER_" + "".join(
            c if c.isalnum() else "_" for c in name.upper()
        )
        max_part_raw = os.environ.get(f"{env}_MAX_PART_BYTES")
        policies[name] = CallerPolicy(
            max_bytes=_env_int(f"{env}_MAX_BYTES", base_max_bytes),
            max_objects=_env_int(f"{env}_MAX_OBJECTS", base_max_objects),
            min_part_bytes=_env_int(f"{env}_MIN_PART_BYTES", base_min_part),
            max_part_bytes=(
                int(max_part_raw)
                if max_part_raw and max_part_raw.strip()
                else None
            ),
        )
    return policies


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
        caller_policies=_env_caller_policies(),
    )


def data_root(settings: Settings) -> Path:
    root = Path(settings.data_dir)
    return root
