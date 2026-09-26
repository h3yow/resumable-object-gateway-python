"""单块摘要、整体摘要校验与失败后的对象可见性保证。"""

from __future__ import annotations

import logging

import pytest

from app.errors import (
    PartDigestMismatch,
    WholeDigestMismatch,
)
from tests.conftest import assert_log_contains, sha


def test_part_digest_mismatch_rejected(service, caplog):
    sid = service.create_session("k/p1", 2, 6, None, None).session_id
    with pytest.raises(PartDigestMismatch):
        service.put_part(sid, 1, b"aaa", sha(b"xxx"))
    caplog.set_level(logging.INFO, logger="resumable_object_gateway")
    assert_log_contains(caplog.records, sid, 1, "摘要不匹配")
    # 被拒绝的块不算已接收
    prog = service.get_progress(sid)
    assert prog.received_parts == [] and prog.bytes_received == 0
    # 同序号可用正确摘要重新写入
    service.put_part(sid, 1, b"aaa", sha(b"aaa"))
    assert service.get_progress(sid).received_parts == [1]


def test_whole_digest_mismatch_rejected_and_no_visible_object(service):
    parts = [b"aaa", b"bbb"]
    sid = service.create_session(
        "k/whole", 2, 6, sha(b"wrong-overall"), None).session_id
    service.put_part(sid, 1, parts[0], sha(parts[0]))
    service.put_part(sid, 2, parts[1], sha(parts[1]))
    with pytest.raises(WholeDigestMismatch):
        service.complete_session(sid, None)

    from app.errors import ObjectNotFound
    with pytest.raises(ObjectNotFound):
        service.get_object_meta("k/whole")
    with pytest.raises(ObjectNotFound):
        service.read_object("k/whole")
    # 没有产生合并临时残留
    import os
    leftovers = [
        n for n in os.listdir(service.storage._objects_dir)
        if n.startswith(".merge-")
    ]
    assert leftovers == []


def test_whole_digest_override_on_complete(service):
    parts = [b"aaa"]
    total = b"".join(parts)
    sid = service.create_session("k/ov", 1, 3, None, None).session_id
    service.put_part(sid, 1, parts[0], sha(parts[0]))
    # 创建时未声明，完成时给出正确整体摘要 -> 成功
    result = service.complete_session(sid, sha(total))
    assert result.digest == sha(total)


def test_wrong_override_digest_rejected(service):
    sid = service.create_session("k/ov2", 1, 3, None, None).session_id
    service.put_part(sid, 1, b"aaa", sha(b"aaa"))
    with pytest.raises(WholeDigestMismatch):
        service.complete_session(sid, sha(b"bbb"))


def test_successful_object_content_matches_digest(service):
    payload = bytes(range(256)) * 20
    sid = service.create_session(
        "k/verified", 2, len(payload), sha(payload), None).session_id
    mid = len(payload) // 2
    service.put_part(sid, 1, payload[:mid], sha(payload[:mid]))
    service.put_part(sid, 2, payload[mid:], sha(payload[mid:]))
    service.complete_session(sid, None)
    data, meta = service.read_object("k/verified")
    assert data == payload
    assert meta.digest == sha(payload)
