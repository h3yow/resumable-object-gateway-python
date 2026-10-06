"""本地磁盘对象存储：分块暂存与对象原子落盘。

目录布局（根目录由 Settings.data_dir 指定）::

    <root>/sessions/<session_id>/part_<n>      # 会话暂存分块
    <root>/objects/<sha256(object_key)>/data   # 已发布对象内容
    <root>/objects/<sha256(object_key)>/meta.json
    <root>/objects/.staging/<tmp>/             # 发布前的暂存目录

对象发布通过“整目录 rename”完成，因此任何时刻对外可见的对象要么完整、
要么不存在，不会出现残缺对象。
"""

import json
import os
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

from gateway.config import Settings
from gateway.errors import ErrorCode, GatewayError
from gateway.hashing import checksums_equal, sha256_file, sha256_hex

_PART_PREFIX = "part_"


def _key_dir_name(object_key: str) -> str:
    """以对象 key 的 SHA-256 作为目录名，规避路径分隔符等非法字符。"""
    return sha256_hex(object_key.encode("utf-8"))


def _fsync_dir(path: Path) -> None:
    """fsync 目录，保证 rename/创建在崩溃后仍然持久。"""
    fd = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_write(target: Path, data: bytes, sync: bool = True) -> None:
    """同目录临时文件 + fsync + rename 的原子写。"""
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(target.parent), prefix=".tmp-")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            if sync:
                fh.flush()
                os.fsync(fh.fileno())
        os.replace(tmp_name, target)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


class LocalObjectStore:
    """以目录结构模拟对象存储。"""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.root = Path(settings.data_dir)
        self.sessions_dir = self.root / "sessions"
        self.objects_dir = self.root / "objects"
        self.staging_dir = self.objects_dir / ".staging"
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        self.objects_dir.mkdir(parents=True, exist_ok=True)
        self.staging_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ parts
    def _session_dir(self, session_id: str) -> Path:
        d = self.sessions_dir / session_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    def part_path(self, session_id: str, part_number: int) -> str:
        return str(self._session_dir(session_id) / f"{_PART_PREFIX}{part_number}")

    def has_part(self, session_id: str, part_number: int) -> bool:
        return Path(self.part_path(session_id, part_number)).exists()

    def write_part(self, session_id: str, part_number: int, data: bytes) -> str:
        """原子写入分块；并发写同一块时后写者整体覆盖（幂等提交）。"""
        target = Path(self.part_path(session_id, part_number))
        _atomic_write(target, data)
        return sha256_hex(data)

    def read_part(self, session_id: str, part_number: int) -> bytes:
        path = Path(self.part_path(session_id, part_number))
        if not path.exists():
            raise GatewayError(
                ErrorCode.PART_MISSING,
                f"part {part_number} not found on disk",
                session_id=session_id,
            )
        return path.read_bytes()

    def remove_session_parts(self, session_id: str) -> None:
        """删除某会话的全部暂存分块。"""
        d = self.sessions_dir / session_id
        if not d.exists():
            return
        for entry in d.iterdir():
            if entry.name.startswith(_PART_PREFIX) or entry.name.startswith(".tmp-"):
                entry.unlink()
        try:
            d.rmdir()
        except OSError:
            # 目录里还有其它文件（如合并临时文件）时保留
            pass

    # ----------------------------------------------------------------- objects
    def _object_dir(self, object_key: str) -> Path:
        return self.objects_dir / _key_dir_name(object_key)

    def object_exists(self, object_key: str) -> bool:
        d = self._object_dir(object_key)
        return (d / "data").exists() and (d / "meta.json").exists()

    def commit_object(
        self,
        session_id: str,
        object_key: str,
        total_parts: int,
        expected_checksum: str | None,
        tenant_id: str = "",
    ) -> tuple[str, int]:
        """按序合并分块、计算整体摘要并原子发布对象。

        返回 ``(整体摘要, 对象字节数)``。摘要不匹配时删除合并临时文件并抛错，
        对象目录不会被创建，对外不可见。
        """
        target_dir = self._object_dir(object_key)
        if target_dir.exists():
            raise GatewayError(
                ErrorCode.OBJECT_ALREADY_EXISTS,
                f"object {object_key!r} already exists",
                session_id=session_id,
            )

        session_dir = self._session_dir(session_id)
        merge_fd, merge_name = tempfile.mkstemp(
            dir=str(session_dir), prefix=".merge-"
        )
        size = 0
        try:
            with os.fdopen(merge_fd, "wb") as out:
                for number in range(1, total_parts + 1):
                    part = session_dir / f"{_PART_PREFIX}{number}"
                    if not part.exists():
                        raise GatewayError(
                            ErrorCode.PART_MISSING,
                            f"part {number} missing when merging",
                            session_id=session_id,
                        )
                    with open(part, "rb") as src:
                        while True:
                            chunk = src.read(1024 * 1024)
                            if not chunk:
                                break
                            out.write(chunk)
                            size += len(chunk)
                out.flush()
                os.fsync(out.fileno())

            actual_checksum = sha256_file(merge_name)
            if expected_checksum is not None and not checksums_equal(
                actual_checksum, expected_checksum
            ):
                raise GatewayError(
                    ErrorCode.OVERALL_CHECKSUM_MISMATCH,
                    "overall checksum mismatch: "
                    f"expected={expected_checksum} actual={actual_checksum}",
                    session_id=session_id,
                )

            # 先在 staging 中把 data + meta.json 准备完整，再整目录 rename 发布
            staging = self.staging_dir / uuid.uuid4().hex
            staging.mkdir(parents=True)
            try:
                os.replace(merge_name, staging / "data")
                meta = {
                    "object_key": object_key,
                    "size": size,
                    "checksum_sha256": actual_checksum,
                    "tenant_id": tenant_id,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }
                _atomic_write(staging / "meta.json", json.dumps(meta).encode("utf-8"))
                os.replace(staging, target_dir)
                _fsync_dir(self.objects_dir)
            except BaseException:
                if (staging / "data").exists():
                    (staging / "data").unlink()
                if (staging / "meta.json").exists():
                    (staging / "meta.json").unlink()
                try:
                    staging.rmdir()
                except OSError:
                    pass
                raise
        finally:
            if os.path.exists(merge_name):
                os.unlink(merge_name)

        return actual_checksum, size

    def read_object(self, object_key: str) -> bytes:
        data = self._object_dir(object_key) / "data"
        if not data.exists():
            raise GatewayError(
                ErrorCode.OBJECT_NOT_FOUND,
                f"object {object_key!r} not found",
            )
        return data.read_bytes()

    def object_meta(self, object_key: str) -> dict:
        meta_path = self._object_dir(object_key) / "meta.json"
        if not meta_path.exists():
            raise GatewayError(
                ErrorCode.OBJECT_NOT_FOUND,
                f"object {object_key!r} not found",
            )
        return json.loads(meta_path.read_text())

    # ------------------------------------------------------------------ quota
    def _iter_object_dirs(self):
        for entry in self.objects_dir.iterdir():
            if entry.name == ".staging":
                continue
            if (entry / "meta.json").exists() and (entry / "data").exists():
                yield entry

    def list_object_metas(self) -> list[dict]:
        """列出全部已发布对象的元信息（供用量查询与重启对账）。"""
        metas: list[dict] = []
        for d in self._iter_object_dirs():
            try:
                metas.append(json.loads((d / "meta.json").read_text()))
            except (FileNotFoundError, ValueError):
                continue
        return metas

    def total_bytes(self) -> int:
        total = 0
        for d in self._iter_object_dirs():
            try:
                total += int(json.loads((d / "meta.json").read_text())["size"])
            except (FileNotFoundError, KeyError, ValueError):
                continue
        return total

    def object_count(self) -> int:
        return sum(1 for _ in self._iter_object_dirs())
