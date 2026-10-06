"""调用方（租户）治理：配额配置、用量账本与预留语义。

模型
----
- 每个命名调用方拥有独立的容量（字节）与对象数量额度，以及单块尺寸上下限；
  不带调用方标识的老用法映射到内置默认调用方（``DEFAULT_TENANT``），其额度
  即原来的全局限额，行为与改造前完全一致。
- 命名调用方的额度在会话期间“占住”：创建会话占 1 个对象位，上传分块时按
  实际字节数占容量；完成时预留原样转为实际占用；中止/过期清理时原样归还。
- 账本不单独落盘，而是以“对象元信息 + 会话元信息”为唯一事实来源，进程
  启动时全量重建（对账），因此重启后既不丢占用也不多算。

并发
----
账本内所有检查-修改均为持锁原子操作；同一会话的操作另由会话锁串行化，
重复提交/并发完成/并发清理不会造成重复扣减或漏还。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

from gateway.config import Settings
from gateway.errors import ErrorCode, GatewayError
from gateway.models import Session, SessionStatus

#: 老用法（不区分调用方）映射到的内置调用方标识
DEFAULT_TENANT = ""


@dataclass(frozen=True)
class TenantQuota:
    """单个调用方的配额与上传约束。"""

    #: 容量额度（字节，含预留）
    max_bytes: int
    #: 对象数量额度（含预留）
    max_objects: int
    #: 单块最小字节数（最后一块不受此限）
    min_part_bytes: int = 1
    #: 单块最大字节数；None 表示不限
    max_part_bytes: int | None = None

    @classmethod
    def from_dict(cls, raw: dict) -> "TenantQuota":
        try:
            return cls(
                max_bytes=int(raw["max_bytes"]),
                max_objects=int(raw["max_objects"]),
                min_part_bytes=int(raw.get("min_part_bytes", 1)),
                max_part_bytes=(
                    None if raw.get("max_part_bytes") is None
                    else int(raw["max_part_bytes"])
                ),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"invalid tenant quota config: {raw!r}") from exc


@dataclass
class TenantUsage:
    """某调用方的实时用量（committed=已落盘，reserved=会话预留）。"""

    committed_bytes: int = 0
    committed_objects: int = 0
    reserved_bytes: int = 0
    reserved_objects: int = 0

    def snapshot(self) -> "TenantUsage":
        return TenantUsage(
            committed_bytes=self.committed_bytes,
            committed_objects=self.committed_objects,
            reserved_bytes=self.reserved_bytes,
            reserved_objects=self.reserved_objects,
        )


class TenantRegistry:
    """解析 tenant_id -> 配额；未配置的命名调用方一律拒绝。"""

    def __init__(self, settings: Settings):
        self._default = TenantQuota(
            max_bytes=settings.max_total_object_bytes,
            max_objects=settings.max_object_count,
            min_part_bytes=settings.min_part_bytes,
            max_part_bytes=None,
        )
        self._tenants: dict[str, TenantQuota] = {
            name: TenantQuota.from_dict(cfg)
            for name, cfg in (settings.tenants or {}).items()
        }

    def resolve(self, tenant_id: str | None) -> TenantQuota:
        """返回该调用方的配额；空标识走默认调用方，未知命名调用方报错。"""
        if not tenant_id:
            return self._default
        quota = self._tenants.get(tenant_id)
        if quota is None:
            raise GatewayError(
                ErrorCode.TENANT_NOT_FOUND,
                f"tenant {tenant_id!r} is not configured",
            )
        return quota

    @staticmethod
    def is_named(tenant_id: str | None) -> bool:
        return bool(tenant_id)

    def known_tenants(self) -> list[str]:
        return sorted(self._tenants)


class UsageLedger:
    """进程内用量账本：所有“检查 + 记账”均为持锁原子操作。

    ``_sessions`` 记录每个进行中的会话为哪个调用方预留了多少字节，
    使得完成/中止/过期清理可以幂等地转换或归还（重复调用安全）。
    """

    def __init__(self, registry: TenantRegistry):
        self.registry = registry
        self._lock = threading.Lock()
        self._usage: dict[str, TenantUsage] = {}
        #: session_id -> (tenant_id, reserved_bytes)
        self._sessions: dict[str, tuple[str, int]] = {}

    # ------------------------------------------------------------ internal
    def _usage_of(self, tenant_id: str) -> TenantUsage:
        usage = self._usage.get(tenant_id)
        if usage is None:
            usage = TenantUsage()
            self._usage[tenant_id] = usage
        return usage

    # ------------------------------------------------------------- rebuild
    def rebuild(
        self,
        sessions: list[Session],
        object_metas: list[dict],
    ) -> None:
        """启动对账：由磁盘上的会话与对象元信息全量重建用量。"""
        with self._lock:
            self._usage.clear()
            self._sessions.clear()
            for meta in object_metas:
                tenant_id = meta.get("tenant_id") or DEFAULT_TENANT
                usage = self._usage_of(tenant_id)
                usage.committed_bytes += int(meta.get("size", 0))
                usage.committed_objects += 1
            for session in sessions:
                if session.status is not SessionStatus.INITIATED:
                    continue
                tenant_id = session.tenant_id or DEFAULT_TENANT
                size = sum(p.size for p in session.parts.values())
                usage = self._usage_of(tenant_id)
                usage.reserved_objects += 1
                usage.reserved_bytes += size
                self._sessions[session.session_id] = (tenant_id, size)

    # ---------------------------------------------- named-tenant lifecycle
    def reserve_session(self, tenant_id: str, session_id: str) -> None:
        """会话创建时占住 1 个对象位（幂等：同会话重复调用不重复占）。"""
        with self._lock:
            if session_id in self._sessions:
                return
            quota = self.registry.resolve(tenant_id)
            usage = self._usage_of(tenant_id)
            if usage.committed_objects + usage.reserved_objects + 1 > quota.max_objects:
                raise GatewayError(
                    ErrorCode.TENANT_QUOTA_EXCEEDED,
                    f"tenant {tenant_id!r} object count quota exceeded: "
                    f"{usage.committed_objects + usage.reserved_objects + 1} > "
                    f"{quota.max_objects}",
                    session_id=session_id,
                )
            usage.reserved_objects += 1
            self._sessions[session_id] = (tenant_id, 0)

    def add_reserved_bytes(
        self, tenant_id: str, session_id: str, delta: int
    ) -> None:
        """分块写入前原子地“检查 + 占住”容量；超额即拒绝。"""
        if delta <= 0:
            return
        with self._lock:
            quota = self.registry.resolve(tenant_id)
            usage = self._usage_of(tenant_id)
            projected = usage.committed_bytes + usage.reserved_bytes + delta
            if projected > quota.max_bytes:
                raise GatewayError(
                    ErrorCode.TENANT_QUOTA_EXCEEDED,
                    f"tenant {tenant_id!r} byte quota exceeded: "
                    f"{usage.committed_bytes}+{usage.reserved_bytes}+{delta} > "
                    f"{quota.max_bytes}",
                    session_id=session_id,
                )
            usage.reserved_bytes += delta
            if session_id in self._sessions:
                tid, size = self._sessions[session_id]
                self._sessions[session_id] = (tid, size + delta)

    def refund_reserved_bytes(
        self, tenant_id: str, session_id: str, delta: int
    ) -> None:
        """分块落盘失败时回滚本次占住的容量。"""
        if delta <= 0:
            return
        with self._lock:
            usage = self._usage_of(tenant_id)
            usage.reserved_bytes = max(0, usage.reserved_bytes - delta)
            if session_id in self._sessions:
                tid, size = self._sessions[session_id]
                self._sessions[session_id] = (tid, max(0, size - delta))

    def commit_session(
        self, tenant_id: str, session_id: str, final_size: int
    ) -> None:
        """完成时把该会话的预留原样转为实际占用（幂等）。"""
        with self._lock:
            entry = self._sessions.pop(session_id, None)
            usage = self._usage_of(tenant_id)
            if entry is not None:
                _, reserved = entry
                usage.reserved_objects = max(0, usage.reserved_objects - 1)
                usage.reserved_bytes = max(0, usage.reserved_bytes - reserved)
            usage.committed_objects += 1
            usage.committed_bytes += final_size

    def release_session(self, tenant_id: str, session_id: str) -> None:
        """中止/过期清理时把该会话的预留原样归还（幂等，未知会话为无操作）。"""
        with self._lock:
            entry = self._sessions.pop(session_id, None)
            if entry is None:
                return
            tid, reserved = entry
            usage = self._usage_of(tid)
            usage.reserved_objects = max(0, usage.reserved_objects - 1)
            usage.reserved_bytes = max(0, usage.reserved_bytes - reserved)

    # --------------------------------------------------- legacy (default)
    def commit_default_tenant(self, session_id: str, size: int) -> None:
        """老用法的完成记账：持锁原子“检查 + 占用”，超额抛 QUOTA_EXCEEDED。

        与改造前的全局限额语义一致（按已落盘对象统计），但检查与记账在
        同一把锁内完成，并发完成不会越过额度。
        """
        with self._lock:
            quota = self.registry.resolve(DEFAULT_TENANT)
            usage = self._usage_of(DEFAULT_TENANT)
            if usage.committed_objects + 1 > quota.max_objects:
                raise GatewayError(
                    ErrorCode.QUOTA_EXCEEDED,
                    f"object count quota exceeded: "
                    f"{usage.committed_objects + 1} > {quota.max_objects}",
                    session_id=session_id,
                )
            if usage.committed_bytes + size > quota.max_bytes:
                raise GatewayError(
                    ErrorCode.QUOTA_EXCEEDED,
                    f"total bytes quota exceeded: "
                    f"{usage.committed_bytes}+{size} > {quota.max_bytes}",
                    session_id=session_id,
                )
            usage.committed_objects += 1
            usage.committed_bytes += size

    def rollback_default_commit(self, session_id: str, size: int) -> None:
        """默认调用方完成记账后对象发布失败时的回滚。"""
        with self._lock:
            usage = self._usage_of(DEFAULT_TENANT)
            usage.committed_objects = max(0, usage.committed_objects - 1)
            usage.committed_bytes = max(0, usage.committed_bytes - size)

    # --------------------------------------------------------------- query
    def usage(self, tenant_id: str) -> TenantUsage:
        with self._lock:
            return self._usage_of(tenant_id).snapshot()
