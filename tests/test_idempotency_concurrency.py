"""重复分块幂等、同会话高并发写入的数据一致性。"""

from __future__ import annotations

import concurrent.futures as cf
import logging

import pytest

from app.errors import PartDigestMismatch
from tests.conftest import assert_log_contains, sha


def test_duplicate_part_idempotent(service, caplog):
    sid = service.create_session("k/dup", 2, 6, None, None).session_id
    info1 = service.put_part(sid, 1, b"aaa", sha(b"aaa"))
    info2 = service.put_part(sid, 1, b"aaa", sha(b"aaa"))  # 重复提交
    assert info1.received_at == info2.received_at
    assert info1.digest == info2.digest
    prog = service.get_progress(sid)
    assert prog.received_parts == [1]
    assert prog.bytes_received == 3
    caplog.set_level(logging.INFO, logger="resumable_object_gateway")
    # 再触发一次以便断言日志
    service.put_part(sid, 1, b"aaa", sha(b"aaa"))
    assert_log_contains(caplog.records, sid, 1, "幂等")


def test_duplicate_part_with_conflicting_digest_rejected(service):
    sid = service.create_session("k/conflict", 2, 6, None, None).session_id
    service.put_part(sid, 1, b"aaa", sha(b"aaa"))
    # 同序号但声明不同摘要 -> 可区分拒绝，不得覆盖已存数据
    with pytest.raises(PartDigestMismatch):
        service.put_part(sid, 1, b"aaa", sha(b"bbb"))
    with pytest.raises(PartDigestMismatch):
        service.put_part(sid, 1, b"bbb", None)
    prog = service.get_progress(sid)
    assert prog.bytes_received == 3 and prog.received_parts == [1]


def test_concurrent_parts_no_corruption(service, caplog):
    n = 24
    parts = [f"part-{i:04d}-".encode() * 13 for i in range(n)]
    total = b"".join(parts)
    sid = service.create_session(
        "k/concurrent", n, len(total), sha(total), None).session_id

    def write(i):
        # 每个分块随机重复 1~2 次，验证并发下幂等
        service.put_part(sid, i + 1, parts[i], sha(parts[i]))
        service.put_part(sid, i + 1, parts[i], sha(parts[i]))

    with cf.ThreadPoolExecutor(max_workers=16) as pool:
        list(pool.map(write, range(n)))

    prog = service.get_progress(sid)
    assert prog.received_parts == list(range(1, n + 1))
    assert prog.bytes_received == len(total)

    result = service.complete_session(sid, None)
    assert result.digest == sha(total)
    data, _ = service.read_object("k/concurrent")
    assert data == total


def test_concurrent_duplicate_same_part(service):
    """多个线程同时提交同一个块，只允许一个发布，其余幂等返回。"""
    sid = service.create_session("k/race", 1, 5, None, None).session_id
    payload = b"abcde"
    with cf.ThreadPoolExecutor(max_workers=8) as pool:
        infos = list(pool.map(
            lambda _: service.put_part(sid, 1, payload, sha(payload)),
            range(16)))
    assert all(i.size == 5 and i.digest == sha(payload) for i in infos)
    assert service.get_progress(sid).bytes_received == 5
