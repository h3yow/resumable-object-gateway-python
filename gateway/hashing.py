"""SHA-256 摘要工具。"""

import hashlib
import hmac

#: 分块读取文件时的缓冲区大小
_CHUNK = 1024 * 1024


def sha256_hex(data: bytes) -> str:
    """返回字节内容的小写十六进制 SHA-256。"""
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: str) -> str:
    """以分块读取方式计算文件 SHA-256，避免大对象一次性占用内存。"""
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(_CHUNK)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def checksums_equal(actual: str, expected: str) -> bool:
    """常量时间比较两个十六进制摘要。

    使用 hmac.compare_digest 避免计时侧信道；二者长度不一致时返回 False，
    而不是向调用方抛出异常。
    """
    return hmac.compare_digest(actual.strip().lower(), expected.strip().lower())


def is_sha256_hex(value: str) -> bool:
    """判断字符串是否为 64 位十六进制 SHA-256 摘要。"""
    if len(value) != 64:
        return False
    try:
        int(value, 16)
    except ValueError:
        return False
    return True
