"""日志配置：单测日志中打印会话标识、分块序号与判定依据。"""

from __future__ import annotations

import logging

LOGGER_NAME = "resumable_object_gateway"

_DEFAULT_FORMAT = "%(asctime)s %(levelname)s [%(name)s] %(message)s"

_configured = False


def configure_logging(level: int = logging.INFO) -> logging.Logger:
    """配置并返回网关 logger（重复调用幂等）。"""
    global _configured
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(level)
    if not _configured:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter(_DEFAULT_FORMAT))
        logger.addHandler(handler)
        # 保持 propagate=True：pytest caplog 依赖向 root 传播来捕获记录。
        logger.propagate = True
        _configured = True
    return logger


def get_logger() -> logging.Logger:
    """返回网关 logger；未配置时退化为控制台 INFO 输出。"""
    logger = logging.getLogger(LOGGER_NAME)
    if not logger.handlers and not _configured:
        configure_logging()
    return logger
