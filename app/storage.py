"""基于本地磁盘的会话与对象存储。

目录布局::

    <data_dir>/
      sessions/<sid>/meta.json        # 会话元数据（原子写）
      sessions/<sid>/parts/00001      # 已收分块（原子 rename）
      objects/<sha256[0:2]>/<sha256>  # 内容寻址的对象数据
      meta/objects.json               # 对象索引（key -> 元数据，原子写）
      meta/quota.json                 # 全局配额计数（原子写）

并发模型（单进程多线程）:

* 全局 RLock 保护所有元数据 JSON 的“读-改-写”；
* 每个会话一把锁，串行化同一会话的分块写入/完成/中止，
  不同会话之间互不阻塞；
* 所有文件落盘均采用“同目录临时文件 + os.replace”原子发布，
  因此任何失败路径都不会留下对外可见的残缺对象。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import threading
import time
import uuid

from app.config import Settings
from app.errors import (
    ObjectExists,
    ObjectNotFound,
    PartDigestMismatch,
    PartIndexOutOfRange,
    PartsMissing,
    QuotaExceeded,
    SessionExpired,
    SessionNotFound,
    SessionStateConflict,
    WholeDigestMismatch,
)
from app.logging_setup import get_logger
from app.models import (
    CompleteResult,
    ObjectMeta,
    PartInfo,
    SessionProgress,
    SessionStatus,
)

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_DIGEST_CHUNK = 1024 * 1024


def _validate_digest(digest: str | None, field: str) -> None:
    """校验客户端给出的摘要必须是 64 位小写十六进制 sha256。"""
    if digest is not None and _HEX64.match(digest) is None:
        raise ValueError(f"{field} 必须是 64 位十六进制 sha256")


def _atomic_write_bytes(path: str, data: bytes) -> None:
    """同目录临时文件 + replace，保证原子发布。"""
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    tmp = os.path.join(directory, f".tmp-{os.path.basename(path)}-{uuid.uuid4().hex}")
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _atomic_write_json(path: str, payload: object) -> None:
    _atomic_write_bytes(path, json.dumps(payload, ensure_ascii=False, indent=2).encode())


class _SessionMeta:
    """meta.json 的内存视图。"""

    def __init__(self, raw: dict) -> None:
        self.raw = raw

    @property
    def session_id(self) -> str:
        return self.raw["session_id"]

    @property
    def object_key(self) -> str:
        return self.raw["object_key"]

    @property
    def part_count(self) -> int:
        return self.raw["part_count"]

    @property
    def total_size(self) -> int:
        return self.raw["total_size"]

    @property
    def whole_digest(self) -> str | None:
        return self.raw.get("whole_digest")

    @property
    def content_type(self) -> str | None:
        return self.raw.get("content_type")

    @property
    def status(self) -> SessionStatus:
        return SessionStatus(self.raw["status"])

    @status.setter
    def status(self, value: SessionStatus) -> None:
        self.raw["status"] = value.value

    @property
    def parts(self) -> dict[str, dict]:
        return self.raw["parts"]

    @property
    def bytes_received(self) -> int:
        return self.raw["bytes_received"]

    @property
    def expires_at(self) -> float:
        return self.raw["expires_at"]

    def touch(self, ttl: int, now: float) -> None:
        self.raw["updated_at"] = now
        self.raw["expires_at"] = now + ttl


class LocalStorage:
    """磁盘后端：会话分块、原子合并、对象索引与配额。"""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._lock = threading.RLock()
        self._session_locks: dict[str, threading.Lock] = {}
        self.log = get_logger()
        self._objects: dict[str, dict] = {}
        self._quota_total_bytes = 0
        self._quota_object_count = 0

    # ---- 路径 ----------------------------------------------------------
    @property
    def _sessions_dir(self) -> str:
        return os.path.join(self.settings.data_dir, "sessions")

    @property
    def _objects_dir(self) -> str:
        return os.path.join(self.settings.data_dir, "objects")

    @property
    def _meta_dir(self) -> str:
        return os.path.join(self.settings.data_dir, "meta")

    def _session_dir(self, session_id: str) -> str:
        return os.path.join(self._sessions_dir, session_id)

    def _meta_path(self, session_id: str) -> str:
        return os.path.join(self._session_dir(session_id), "meta.json")

    def _parts_dir(self, session_id: str) -> str:
        return os.path.join(self._session_dir(session_id), "parts")

    def _part_path(self, session_id: str, number: int) -> str:
        return os.path.join(self._parts_dir(session_id), f"{number:05d}")

    @property
    def _objects_index_path(self) -> str:
        return os.path.join(self._meta_dir, "objects.json")

    @property
    def _quota_path(self) -> str:
        return os.path.join(self._meta_dir, "quota.json")

    def _blob_path(self, digest: str) -> str:
        return os.path.join(self._objects_dir, digest[:2], digest)

    # ---- 生命周期 ------------------------------------------------------
    def init_layout(self) -> None:
        """创建目录结构并从磁盘恢复对象索引、配额与会话状态。"""
        for d in (self._sessions_dir, self._objects_dir, self._meta_dir):
            os.makedirs(d, exist_ok=True)
        with self._lock:
            self._objects = self._load_objects_index()
            self._rebuild_quota()
            recovered = 0
            for sid in self._list_session_dirs():
                meta = self._load_meta(sid)
                if meta is None:
                    continue
                self._reconcile_parts(meta)
                recovered += 1
            self.log.info("存储初始化完成: 会话目录=%d 对象数=%d 已用字节=%d",
                          recovered, self._quota_object_count,
                          self._quota_total_bytes)

    def _list_session_dirs(self) -> list[str]:
        try:
            return [
                name
                for name in os.listdir(self._sessions_dir)
                if os.path.isfile(self._meta_path(name))
            ]
        except FileNotFoundError:
            return []

    def _load_objects_index(self) -> dict[str, dict]:
        try:
            with open(self._objects_index_path, encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            return {}

    def _rebuild_quota(self) -> None:
        """以对象索引为准重建配额计数（进程重启后的恢复依据）。"""
        self._quota_object_count = len(self._objects)
        self._quota_total_bytes = sum(
            int(item.get("size", 0)) for item in self._objects.values()
        )
        self._save_quota_locked()

    # ---- 元数据读写 ----------------------------------------------------
    def _load_meta(self, session_id: str) -> _SessionMeta | None:
        path = self._meta_path(session_id)
        try:
            with open(path, encoding="utf-8") as f:
                return _SessionMeta(json.load(f))
        except FileNotFoundError:
            return None

    def _require_meta(self, session_id: str) -> _SessionMeta:
        meta = self._load_meta(session_id)
        if meta is None:
            raise SessionNotFound(f"会话不存在: {session_id}")
        return meta

    def _save_meta_locked(self, meta: _SessionMeta) -> None:
        _atomic_write_json(self._meta_path(meta.session_id), meta.raw)

    def _save_objects_index_locked(self) -> None:
        _atomic_write_json(self._objects_index_path, self._objects)

    def _save_quota_locked(self) -> None:
        _atomic_write_json(self._quota_path, {
            "object_count": self._quota_object_count,
            "total_bytes": self._quota_total_bytes,
        })

    def _reconcile_parts(self, meta: _SessionMeta) -> None:
        """重启恢复：丢弃没有对应数据文件的分块记录。"""
        alive: dict[str, dict] = {}
        for key, info in meta.parts.items():
            if os.path.isfile(self._part_path(meta.session_id, int(key))):
                alive[key] = info
        if len(alive) != len(meta.parts):
            meta.raw["parts"] = alive
            meta.raw["bytes_received"] = sum(i["size"] for i in alive.values())
            self._save_meta_locked(meta)
            self.log.warning("会话恢复时剔除孤儿分块记录: sid=%s 保留=%d 剔除=%d",
                             meta.session_id, len(alive),
                             len(meta.parts) - len(alive))

    def _session_lock(self, session_id: str) -> threading.Lock:
        lock = self._session_locks.get(session_id)
        if lock is None:
            lock = threading.Lock()
            self._session_locks[session_id] = lock
        return lock

    def _ensure_writable(self, meta: _SessionMeta, now: float) -> None:
        """校验会话处于可写入状态；终态/过期给出可区分错误。"""
        if meta.status is SessionStatus.EXPIRED:
            raise SessionExpired(f"会话已过期: {meta.session_id}")
        if meta.status is not SessionStatus.PENDING:
            raise SessionStateConflict(
                f"会话状态为 {meta.status.value}，不允许写入: "
                f"{meta.session_id}"
            )
        if now > meta.expires_at:
            raise SessionExpired(
                f"会话已超过无活动期限(expires_at={meta.expires_at:.0f}): "
                f"{meta.session_id}"
            )

    # ---- 会话 ----------------------------------------------------------
    def create_session(
        self,
        object_key: str,
        part_count: int,
        total_size: int,
        whole_digest: str | None,
        content_type: str | None,
    ) -> tuple[str, float]:
        _validate_digest(whole_digest, "whole_digest")
        if total_size > self.settings.max_session_bytes:
            raise QuotaExceeded(
                f"声明总大小 {total_size} 超过单会话上限 "
                f"{self.settings.max_session_bytes}"
            )
        now = time.time()
        session_id = uuid.uuid4().hex
        raw = {
            "session_id": session_id,
            "object_key": object_key,
            "part_count": part_count,
            "total_size": total_size,
            "whole_digest": whole_digest,
            "content_type": content_type,
            "status": SessionStatus.PENDING.value,
            "created_at": now,
            "updated_at": now,
            "expires_at": now + self.settings.session_ttl_seconds,
            "parts": {},
            "bytes_received": 0,
        }
        with self._lock:
            os.makedirs(self._parts_dir(session_id), exist_ok=True)
            self._save_meta_locked(_SessionMeta(raw))
        self.log.info("创建会话: sid=%s key=%s parts=%d total=%d ttl=%ds",
                      session_id, object_key, part_count, total_size,
                      self.settings.session_ttl_seconds)
        return session_id, raw["expires_at"]

    def _to_progress(self, meta: _SessionMeta) -> SessionProgress:
        received = sorted(int(k) for k in meta.parts)
        received_set = set(meta.parts)
        missing = [
            n for n in range(1, meta.part_count + 1)
            if str(n) not in received_set
        ]
        return SessionProgress(
            session_id=meta.session_id,
            object_key=meta.object_key,
            status=meta.status,
            part_count=meta.part_count,
            total_size=meta.total_size,
            received_parts=received,
            missing_parts=missing,
            bytes_received=meta.bytes_received,
            expires_at=meta.expires_at,
            updated_at=meta.raw["updated_at"],
            whole_digest=meta.whole_digest,
        )

    def get_progress(self, session_id: str) -> SessionProgress:
        with self._lock:
            meta = self._require_meta(session_id)
            return self._to_progress(meta)

    def put_part(
        self,
        session_id: str,
        number: int,
        data: bytes,
        digest: str | None,
    ) -> PartInfo:
        _validate_digest(digest, "part_digest")
        if not isinstance(number, int) or number < 1:
            raise PartIndexOutOfRange(f"分块序号必须 >= 1: {number}")
        size = len(data)
        if size > self.settings.max_part_bytes:
            raise QuotaExceeded(
                f"分块 {number} 大小 {size} 超过单块上限 "
                f"{self.settings.max_part_bytes}"
            )

        sess_lock = self._session_lock(session_id)
        with sess_lock:
            now = time.time()
            # 阶段 1：元数据校验（全局锁内快速完成）
            with self._lock:
                meta = self._require_meta(session_id)
                self._ensure_writable(meta, now)
                if number > meta.part_count:
                    self.log.warning(
                        "拒绝分块(序号越界): sid=%s part=%d 上限=%d",
                        session_id, number, meta.part_count)
                    raise PartIndexOutOfRange(
                        f"分块序号 {number} 越界，会话共 {meta.part_count} 块")
                # 注意：仅对“尚未接收的新块”做累计字节检查；
                # 已存在的块走下面的幂等分支，不得重复计入。
                existing = meta.parts.get(str(number))
                if existing is None \
                        and meta.bytes_received + size > meta.total_size:
                    raise QuotaExceeded(
                        f"分块 {number} 将使累计字节 "
                        f"{meta.bytes_received + size} 超过声明总大小 "
                        f"{meta.total_size}")

            # 阶段 2：幂等判定（数据文件已存在）
            final_path = self._part_path(session_id, number)
            if existing is not None and os.path.isfile(final_path):
                return self._idempotent_part(
                    session_id, number, data, digest, existing, final_path)

            # 阶段 3：写临时文件并实时计算摘要
            tmp_path = final_path + f".tmp-{uuid.uuid4().hex}"
            os.makedirs(self._parts_dir(session_id), exist_ok=True)
            h = hashlib.sha256()
            try:
                with open(tmp_path, "wb") as f:
                    f.write(data)
                    f.flush()
                    os.fsync(f.fileno())
                h.update(data)
                actual = h.hexdigest()
                if digest is not None and actual != digest:
                    self.log.warning(
                        "拒绝分块(摘要不匹配): sid=%s part=%d 期望=%s 实际=%s",
                        session_id, number, digest, actual)
                    raise PartDigestMismatch(
                        f"分块 {number} 摘要不匹配: 期望 {digest} 实际 {actual}")

                # 阶段 4：原子发布 + 元数据读改写（全局锁）
                os.replace(tmp_path, final_path)
                with self._lock:
                    meta = self._require_meta(session_id)
                    self._ensure_writable(meta, time.time())
                    if str(number) in meta.parts:
                        # 并发场景下已被其它线程发布（同会话锁保证通常不会发生）
                        info = meta.parts[str(number)]
                        self.log.info(
                            "分块幂等返回(并发去重): sid=%s part=%d size=%d",
                            session_id, number, info["size"])
                        return PartInfo(number=number, **info)
                    new_bytes = meta.bytes_received + size
                    if new_bytes > meta.total_size:
                        # 兜底：理论上阶段 1 已拦截；触发时清理刚发布的文件
                        if os.path.isfile(final_path):
                            os.unlink(final_path)
                        raise QuotaExceeded(
                            f"分块 {number} 将使累计字节 {new_bytes} "
                            f"超过声明总大小 {meta.total_size}")
                    meta.parts[str(number)] = {
                        "size": size,
                        "digest": digest or actual,
                        "received_at": time.time(),
                    }
                    meta.raw["bytes_received"] += size
                    meta.touch(self.settings.session_ttl_seconds, time.time())
                    self._save_meta_locked(meta)
                self.log.info(
                    "接收分块: sid=%s part=%d size=%d digest=%s 累计块=%d",
                    session_id, number, size, actual, len(meta.parts))
                return PartInfo(
                    number=number, size=size, digest=digest or actual,
                    received_at=meta.parts[str(number)]["received_at"])
            finally:
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)

    def _idempotent_part(
        self,
        session_id: str,
        number: int,
        data: bytes,
        digest: str | None,
        existing: dict,
        final_path: str,
    ) -> PartInfo:
        """处理重复提交：内容一致则幂等成功，不一致则可区分地拒绝。"""
        incoming = hashlib.sha256(data).hexdigest()
        stored_digest = existing.get("digest")
        consistent = (
            (digest is not None and digest == stored_digest)
            or (digest is None and incoming == stored_digest)
        )
        if not consistent:
            self.log.warning(
                "拒绝重复分块(内容与已存数据不一致): sid=%s part=%d "
                "已存=%s 本次声明=%s 本次实际=%s",
                session_id, number, stored_digest, digest, incoming)
            raise PartDigestMismatch(
                f"重复分块 {number} 内容与已存数据不一致: 已存 {stored_digest}"
                + (f"，声明 {digest}" if digest else f"，实际 {incoming}"))
        self.log.info(
            "重复分块幂等返回: sid=%s part=%d size=%d 依据=摘要一致(%s)",
            session_id, number, existing["size"], stored_digest)
        return PartInfo(
            number=number,
            size=existing["size"],
            digest=stored_digest,
            received_at=existing["received_at"],
        )

    def complete_session(
        self, session_id: str, whole_digest: str | None
    ) -> CompleteResult:
        _validate_digest(whole_digest, "whole_digest")
        sess_lock = self._session_lock(session_id)
        with sess_lock:
            with self._lock:
                meta = self._require_meta(session_id)
                if meta.status is SessionStatus.EXPIRED:
                    raise SessionExpired(f"会话已过期: {session_id}")
                if meta.status is not SessionStatus.PENDING:
                    raise SessionStateConflict(
                        f"会话状态为 {meta.status.value}，不允许完成: "
                        f"{session_id}")
                if time.time() > meta.expires_at:
                    raise SessionExpired(f"会话已过期: {session_id}")
                missing = [
                    n for n in range(1, meta.part_count + 1)
                    if str(n) not in meta.parts
                ]
                if missing:
                    self.log.warning(
                        "拒绝完成(分块缺失): sid=%s 缺失=%s",
                        session_id, missing)
                    raise PartsMissing(
                        f"分块缺失，无法完成: {missing}")
                claimed_total = sum(
                    meta.parts[str(n)]["size"]
                    for n in range(1, meta.part_count + 1))
                if claimed_total != meta.total_size:
                    raise QuotaExceeded(
                        f"已收字节 {claimed_total} 与声明总大小 "
                        f"{meta.total_size} 不一致")
                effective_digest = whole_digest or meta.whole_digest
                if meta.object_key in self._objects:
                    raise ObjectExists(
                        f"对象键已存在: {meta.object_key}")
                # 合并前预检查配额，避免无谓 IO
                if self._quota_total_bytes + claimed_total \
                        > self.settings.max_total_bytes:
                    raise QuotaExceeded(
                        f"完成后总容量 {self._quota_total_bytes + claimed_total}"
                        f" 超过上限 {self.settings.max_total_bytes}")
                if self._quota_object_count + 1 \
                        > self.settings.max_object_count:
                    raise QuotaExceeded(
                        f"对象数量将达 {self._quota_object_count + 1}，"
                        f"超过上限 {self.settings.max_object_count}")

            # 流式合并到临时对象文件并计算整体摘要
            tmp_blob = None
            actual_digest = ""
            try:
                h = hashlib.sha256()
                tmp_blob = os.path.join(
                    self._objects_dir,
                    f".merge-{session_id}-{uuid.uuid4().hex}")
                os.makedirs(self._objects_dir, exist_ok=True)
                with open(tmp_blob, "wb") as out:
                    for n in range(1, meta.part_count + 1):
                        part_path = self._part_path(session_id, n)
                        with open(part_path, "rb") as src:
                            for chunk in iter(
                                lambda: src.read(_DIGEST_CHUNK), b""
                            ):
                                out.write(chunk)
                                h.update(chunk)
                    out.flush()
                    os.fsync(out.fileno())
                actual_digest = h.hexdigest()

                if effective_digest is not None \
                        and actual_digest != effective_digest:
                    self.log.warning(
                        "拒绝完成(整体摘要不匹配): sid=%s 期望=%s 实际=%s",
                        session_id, effective_digest, actual_digest)
                    raise WholeDigestMismatch(
                        f"整体摘要不匹配: 期望 {effective_digest} "
                        f"实际 {actual_digest}")
                blob_final = self._blob_path(actual_digest)

                # 发布：全局锁内复查配额/键，原子 rename + 索引更新
                with self._lock:
                    meta = self._require_meta(session_id)
                    if meta.status is not SessionStatus.PENDING:
                        raise SessionStateConflict(
                            f"会话状态在合并期间变更为 {meta.status.value}")
                    if meta.object_key in self._objects:
                        raise ObjectExists(
                            f"对象键已存在: {meta.object_key}")
                    final_size = os.path.getsize(tmp_blob)
                    if self._quota_total_bytes + final_size \
                            > self.settings.max_total_bytes:
                        raise QuotaExceeded("全局容量上限复查未通过")
                    if self._quota_object_count + 1 \
                            > self.settings.max_object_count:
                        raise QuotaExceeded("对象数量上限复查未通过")
                    os.makedirs(os.path.dirname(blob_final), exist_ok=True)
                    os.replace(tmp_blob, blob_final)
                    tmp_blob = None
                    created_at = time.time()
                    self._objects[meta.object_key] = {
                        "object_key": meta.object_key,
                        "size": final_size,
                        "digest": actual_digest,
                        "content_type": meta.content_type,
                        "created_at": created_at,
                    }
                    self._quota_total_bytes += final_size
                    self._quota_object_count += 1
                    self._save_objects_index_locked()
                    self._save_quota_locked()
                    meta.status = SessionStatus.COMPLETE
                    meta.touch(self.settings.session_ttl_seconds, time.time())
                    self._save_meta_locked(meta)
                shutil.rmtree(self._parts_dir(session_id), ignore_errors=True)
                self.log.info(
                    "会话完成: sid=%s key=%s size=%d digest=%s 判定=全部校验通过",
                    session_id, meta.object_key, final_size, actual_digest)
                return CompleteResult(
                    session_id=session_id,
                    object_key=meta.object_key,
                    size=final_size,
                    digest=actual_digest,
                    etag=actual_digest,
                    status=SessionStatus.COMPLETE,
                )
            finally:
                if tmp_blob and os.path.exists(tmp_blob):
                    os.unlink(tmp_blob)

    def abort_session(self, session_id: str) -> tuple[str, str]:
        sess_lock = self._session_lock(session_id)
        with sess_lock:
            with self._lock:
                meta = self._require_meta(session_id)
                if meta.status is SessionStatus.EXPIRED:
                    raise SessionExpired(f"会话已过期: {session_id}")
                if meta.status is not SessionStatus.PENDING:
                    raise SessionStateConflict(
                        f"会话状态为 {meta.status.value}，不允许中止: "
                        f"{session_id}")
                meta.status = SessionStatus.ABORTED
                meta.touch(self.settings.session_ttl_seconds, time.time())
                self._save_meta_locked(meta)
            shutil.rmtree(self._parts_dir(session_id), ignore_errors=True)
            self.log.info("会话中止: sid=%s key=%s 判定=用户主动中止，临时块已清理",
                          session_id, meta.object_key)
            return meta.object_key, SessionStatus.ABORTED.value

    def sweep_expired(self, now: float | None = None) -> list[str]:
        now = time.time() if now is None else now
        expired_ids: list[str] = []
        # 先在全局锁内只收集候选 sid，再释放全局锁去拿会话锁，
        # 避免与 put_part 的“会话锁 -> 全局锁”形成反向加锁。
        with self._lock:
            candidates = [
                sid
                for sid in self._list_session_dirs()
                if (meta := self._load_meta(sid)) is not None
                and meta.status is SessionStatus.PENDING
                and now > meta.expires_at
            ]
        for sid in candidates:
            with self._session_lock(sid):
                with self._lock:
                    meta = self._load_meta(sid)
                    if meta is None \
                            or meta.status is not SessionStatus.PENDING \
                            or now <= meta.expires_at:
                        continue
                    meta.status = SessionStatus.EXPIRED
                    meta.raw["expired_at"] = now
                    self._save_meta_locked(meta)
                shutil.rmtree(self._parts_dir(sid), ignore_errors=True)
                expired_ids.append(sid)
                self.log.info(
                    "过期清理: sid=%s key=%s 依据=now(%d)>expires_at(%d)",
                    sid, meta.object_key, int(now), int(meta.expires_at))
        return expired_ids

    # ---- 对象 ----------------------------------------------------------
    def _require_object(self, object_key: str) -> dict:
        item = self._objects.get(object_key)
        if item is None:
            raise ObjectNotFound(f"对象不存在: {object_key}")
        return item

    def get_object_meta(self, object_key: str) -> ObjectMeta:
        with self._lock:
            item = self._require_object(object_key)
            return ObjectMeta(**item)

    def read_object(self, object_key: str) -> tuple[bytes, ObjectMeta]:
        with self._lock:
            item = dict(self._require_object(object_key))
        with open(self._blob_path(item["digest"]), "rb") as f:
            data = f.read()
        return data, ObjectMeta(**item)
