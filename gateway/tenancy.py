"""调用方（租户）额度治理：占住 / 转正 / 归还 / 持久化 / 重启对账。

语义约定：

- 每个调用方拥有独立的容量额度（字节）与对象数量额度，互不干扰；
  不显式携带 caller_id 的老用法统一归属 ``DEFAULT_CALLER_ID``。
- **占住（reserve）**：会话创建成功后即为该会话占住额度
  （对象数恒为 1；字节数按 ``part_size * total_parts`` 上界估计，
  未声明时为 0，完成时以实际大小结算）。
- **转正（convert）**：会话完成、对象原子发布后，占住额度转为实际占用；
  实际字节超出占住估计的部分在转正时一并校验，任一维度越界即整体拒绝，
  对象不会落盘。
- **归还（release）**：中止、过期清理及一切未成功完成的会话，
  占住额度原样归还；归还按 session_id 幂等，重复归还/并发归还
  不会多还也不会漏还。
- **持久化**：账本落盘于 ``<data>/tenants/<sha256(caller)>/usage.json``
  （临时文件 + fsync + rename 原子替换），重启后经 ``reconcile``
  与磁盘上的会话、对象对账：已落盘对象按 meta.json 重算实际占用，
  仍存活（INITIATED）会话的占住保留，其余占住一律回收——
  既不凭空丢失，也不凭空多出。
"""

import json
import logging
import threading
from pathlib import Path

from pydantic import BaseModel, Field

from gateway.config import CallerPolicy, Settings
from gateway.errors import ErrorCode, GatewayError
from gateway.hashing import sha256_hex
from gateway.models import CallerUsageResponse, ObjectMetaView, Session, SessionStatus
from gateway.storage import LocalObjectStore, _atomic_write, _fsync_dir

logger = logging.getLogger("gateway.tenancy")

#: 老用法（不显式携带 caller_id）归属的默认调用方
DEFAULT_CALLER_ID = "default"


class Reservation(BaseModel):
    """一次会话占住的额度。"""

    session_id: str
    bytes: int = 0
    objects: int = 1


class CallerLedger(BaseModel):
    """单个调用方的额度账本（持久化单元）。"""

    caller_id: str
    reserved_bytes: int = 0
    reserved_objects: int = 0
    committed_bytes: int = 0
    committed_objects: int = 0
    reservations: dict[str, Reservation] = Field(default_factory=dict)


class TenantStore:
    """按调用方管理额度账本；所有变更在调用方级锁内完成并原子落盘。"""

    def __init__(self, settings: Settings, objects: LocalObjectStore):
        self.settings = settings
        self.objects = objects
        self.dir = objects.root / "tenants"
        self.dir.mkdir(parents=True, exist_ok=True)
        self._ledgers: dict[str, CallerLedger] = {}
        self._locks: dict[str, threading.RLock] = {}
        self._guard = threading.Lock()

    # ------------------------------------------------------------- callers
    def policy_for(self, caller_id: str) -> CallerPolicy:
        """返回调用方策略；默认调用方回退到全局（老）配置。"""
        if caller_id == DEFAULT_CALLER_ID:
            return CallerPolicy(
                max_bytes=self.settings.max_total_object_bytes,
                max_objects=self.settings.max_object_count,
                min_part_bytes=self.settings.min_part_bytes,
                max_part_bytes=None,
            )
        policy = self.settings.caller_policies.get(caller_id)
        if policy is None:
            raise GatewayError(
                ErrorCode.CALLER_NOT_FOUND,
                f"caller {caller_id!r} is not configured",
                caller_id=caller_id,
            )
        return policy

    def resolve(self, caller_id: str | None) -> str:
        """把可选的 caller_id 归一化为具体调用方并校验其存在。"""
        cid = caller_id or DEFAULT_CALLER_ID
        self.policy_for(cid)  # 未配置即抛 CALLER_NOT_FOUND
        return cid

    # --------------------------------------------------------------- locks
    def _get_lock(self, caller_id: str) -> threading.RLock:
        with self._guard:
            lock = self._locks.get(caller_id)
            if lock is None:
                lock = threading.RLock()
                self._locks[caller_id] = lock
            return lock

    def lock(self, caller_id: str) -> threading.RLock:
        """调用方级互斥锁（可重入）；同调用方的额度变更串行化。"""
        return self._get_lock(caller_id)

    # ---------------------------------------------------------- persistence
    def _ledger_path(self, caller_id: str) -> Path:
        return self.dir / sha256_hex(caller_id.encode("utf-8")) / "usage.json"

    def _load(self, caller_id: str) -> CallerLedger:
        ledger = self._ledgers.get(caller_id)
        if ledger is not None:
            return ledger
        path = self._ledger_path(caller_id)
        if path.exists():
            try:
                ledger = CallerLedger.model_validate_json(path.read_text())
            except ValueError:
                logger.warning(
                    "caller=%s action=load verdict=CORRUPT_LEDGER_RESET path=%s",
                    caller_id,
                    path,
                )
                ledger = CallerLedger(caller_id=caller_id)
        else:
            ledger = CallerLedger(caller_id=caller_id)
        self._ledgers[caller_id] = ledger
        return ledger

    def _persist(self, ledger: CallerLedger) -> None:
        target = self._ledger_path(ledger.caller_id)
        target.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(target, ledger.model_dump_json().encode("utf-8"))
        _fsync_dir(target.parent)

    # -------------------------------------------------------------- reserve
    def reserve(
        self,
        caller_id: str,
        session_id: str,
        reserved_bytes: int,
        reserved_objects: int = 1,
    ) -> Reservation:
        """为会话占住额度；不足时抛 QUOTA_EXCEEDED，不产生任何占住。

        同一 session_id 重复占住是幂等的（返回既有占住），不会重复扣减。
        """
        policy = self.policy_for(caller_id)
        with self._get_lock(caller_id):
            ledger = self._load(caller_id)
            existing = ledger.reservations.get(session_id)
            if existing is not None:
                logger.info(
                    "caller=%s session=%s action=reserve verdict=IDEMPOTENT "
                    "bytes=%d objects=%d",
                    caller_id,
                    session_id,
                    existing.bytes,
                    existing.objects,
                )
                return existing
            proj_bytes = ledger.committed_bytes + ledger.reserved_bytes + reserved_bytes
            proj_objects = (
                ledger.committed_objects + ledger.reserved_objects + reserved_objects
            )
            if proj_bytes > policy.max_bytes or proj_objects > policy.max_objects:
                logger.info(
                    "caller=%s session=%s action=reserve verdict=REJECTED "
                    "reason=QUOTA_EXCEEDED proj_bytes=%d/%d proj_objects=%d/%d",
                    caller_id,
                    session_id,
                    proj_bytes,
                    policy.max_bytes,
                    proj_objects,
                    policy.max_objects,
                )
                raise GatewayError(
                    ErrorCode.QUOTA_EXCEEDED,
                    f"caller {caller_id!r} quota exceeded at reserve: "
                    f"bytes {proj_bytes}/{policy.max_bytes}, "
                    f"objects {proj_objects}/{policy.max_objects}",
                    caller_id=caller_id,
                )
            reservation = Reservation(
                session_id=session_id,
                bytes=reserved_bytes,
                objects=reserved_objects,
            )
            ledger.reservations[session_id] = reservation
            ledger.reserved_bytes += reserved_bytes
            ledger.reserved_objects += reserved_objects
            self._persist(ledger)
            logger.info(
                "caller=%s session=%s action=reserve verdict=RESERVED "
                "bytes=%d objects=%d used=%d/%d used_objects=%d/%d",
                caller_id,
                session_id,
                reserved_bytes,
                reserved_objects,
                ledger.committed_bytes + ledger.reserved_bytes,
                policy.max_bytes,
                ledger.committed_objects + ledger.reserved_objects,
                policy.max_objects,
            )
            return reservation

    # -------------------------------------------------------------- convert
    def convert(self, caller_id: str, session_id: str, final_bytes: int) -> None:
        """完成时把占住转为实际占用；差额部分越界则抛 QUOTA_EXCEEDED。

        必须在对象落盘**之前**调用（校验），对象发布成功后调用
        ``finalize`` 落账。此处只校验不改账，保证校验与发布之间
        账本不被并发完成击穿——调用方需持有 ``lock(caller_id)`` 覆盖
        校验、发布与落账全过程。
        """
        policy = self.policy_for(caller_id)
        ledger = self._load(caller_id)
        reservation = ledger.reservations.get(session_id)
        held = reservation.bytes if reservation else 0
        extra = max(0, final_bytes - held)
        proj_bytes = ledger.committed_bytes + ledger.reserved_bytes + extra
        if proj_bytes > policy.max_bytes:
            logger.info(
                "caller=%s session=%s action=convert verdict=REJECTED "
                "reason=QUOTA_EXCEEDED proj_bytes=%d/%d final=%d held=%d",
                caller_id,
                session_id,
                proj_bytes,
                policy.max_bytes,
                final_bytes,
                held,
            )
            raise GatewayError(
                ErrorCode.QUOTA_EXCEEDED,
                f"caller {caller_id!r} bytes quota exceeded at complete: "
                f"{proj_bytes} > {policy.max_bytes}",
                caller_id=caller_id,
            )

    def finalize(self, caller_id: str, session_id: str, final_bytes: int) -> None:
        """对象发布成功后落账：占住移除，实际占用增加。幂等。"""
        with self._get_lock(caller_id):
            ledger = self._load(caller_id)
            reservation = ledger.reservations.pop(session_id, None)
            if reservation is not None:
                ledger.reserved_bytes -= reservation.bytes
                ledger.reserved_objects -= reservation.objects
            ledger.committed_bytes += final_bytes
            ledger.committed_objects += 1
            self._persist(ledger)
            logger.info(
                "caller=%s session=%s action=finalize verdict=COMMITTED "
                "bytes=%d committed=%d/%d objects=%d",
                caller_id,
                session_id,
                final_bytes,
                ledger.committed_bytes,
                ledger.committed_bytes + ledger.reserved_bytes,
                ledger.committed_objects,
            )

    # -------------------------------------------------------------- release
    def release(self, caller_id: str, session_id: str) -> bool:
        """归还会话占住的额度；无占住记录时幂等返回 False。"""
        with self._get_lock(caller_id):
            ledger = self._load(caller_id)
            reservation = ledger.reservations.pop(session_id, None)
            if reservation is None:
                logger.info(
                    "caller=%s session=%s action=release verdict=IDEMPOTENT "
                    "nothing_held",
                    caller_id,
                    session_id,
                )
                return False
            ledger.reserved_bytes -= reservation.bytes
            ledger.reserved_objects -= reservation.objects
            self._persist(ledger)
            logger.info(
                "caller=%s session=%s action=release verdict=RELEASED "
                "bytes=%d objects=%d reserved_left=%d/%d",
                caller_id,
                session_id,
                reservation.bytes,
                reservation.objects,
                ledger.reserved_bytes,
                ledger.reserved_objects,
            )
            return True

    # ---------------------------------------------------------------- usage
    def usage(self, caller_id: str) -> CallerUsageResponse:
        """查询某调用方当前占用、剩余与名下对象。"""
        policy = self.policy_for(caller_id)
        with self._get_lock(caller_id):
            ledger = self._load(caller_id)
            objects = [
                ObjectMetaView(**meta)
                for meta in self.objects.list_objects(caller_id)
            ]
            response = CallerUsageResponse(
                caller_id=caller_id,
                used_bytes=ledger.committed_bytes,
                used_objects=ledger.committed_objects,
                reserved_bytes=ledger.reserved_bytes,
                reserved_objects=ledger.reserved_objects,
                limit_bytes=policy.max_bytes,
                limit_objects=policy.max_objects,
                remaining_bytes=policy.max_bytes
                - ledger.committed_bytes
                - ledger.reserved_bytes,
                remaining_objects=policy.max_objects
                - ledger.committed_objects
                - ledger.reserved_objects,
                min_part_bytes=policy.min_part_bytes,
                max_part_bytes=policy.max_part_bytes,
                objects=objects,
            )
            logger.info(
                "caller=%s action=usage verdict=OK used=%d/%d objects=%d/%d "
                "reserved=%d/%d",
                caller_id,
                ledger.committed_bytes,
                policy.max_bytes,
                ledger.committed_objects,
                policy.max_objects,
                ledger.reserved_bytes,
                ledger.reserved_objects,
            )
            return response

    # ------------------------------------------------------------ reconcile
    def reconcile(self, sessions: list[Session]) -> None:
        """重启对账：以磁盘为权威重建各调用方占用。

        - committed：扫描全部已发布对象的 meta.json 重算（老对象归默认调用方）；
        - reserved：仅保留仍为 INITIATED 的会话对应的占住，其余回收。
        """
        committed: dict[str, tuple[int, int]] = {}
        for meta in self.objects.list_objects():
            owner = meta.get("caller_id") or DEFAULT_CALLER_ID
            size = int(meta.get("size", 0))
            b, n = committed.get(owner, (0, 0))
            committed[owner] = (b + size, n + 1)

        live_sessions = {
            s.session_id: s for s in sessions if s.status is SessionStatus.INITIATED
        }

        caller_ids = set(committed)
        caller_ids.update(self.settings.caller_policies)
        caller_ids.add(DEFAULT_CALLER_ID)
        # 磁盘上已有账本的调用方也纳入对账
        if self.dir.exists():
            for entry in self.dir.iterdir():
                usage = entry / "usage.json"
                if usage.exists():
                    try:
                        caller_ids.add(
                            CallerLedger.model_validate_json(
                                usage.read_text()
                            ).caller_id
                        )
                    except ValueError:
                        continue

        for caller_id in sorted(caller_ids):
            with self._get_lock(caller_id):
                ledger = self._load(caller_id)
                b, n = committed.get(caller_id, (0, 0))
                dropped = [
                    sid
                    for sid in ledger.reservations
                    if sid not in live_sessions
                ]
                for sid in dropped:
                    r = ledger.reservations.pop(sid)
                    ledger.reserved_bytes -= r.bytes
                    ledger.reserved_objects -= r.objects
                ledger.committed_bytes = b
                ledger.committed_objects = n
                self._persist(ledger)
                logger.info(
                    "caller=%s action=reconcile verdict=OK committed_bytes=%d "
                    "committed_objects=%d reserved_bytes=%d reserved_objects=%d "
                    "dropped_reservations=%s",
                    caller_id,
                    ledger.committed_bytes,
                    ledger.committed_objects,
                    ledger.reserved_bytes,
                    ledger.reserved_objects,
                    dropped,
                )
