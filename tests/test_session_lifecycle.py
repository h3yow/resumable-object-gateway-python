"""会话创建、分块写入、完成合并、主动中止与状态机拒绝路径。"""

from __future__ import annotations

import pytest

from app.errors import (
    ObjectExists,
    PartIndexOutOfRange,
    PartsMissing,
    SessionNotFound,
    SessionStateConflict,
)
from tests.conftest import sha


def test_create_and_progress(service):
    resp = service.create_session("a/b.bin", 3, 30, None, None)
    assert resp.status.value == "pending"
    prog = service.get_progress(resp.session_id)
    assert prog.part_count == 3
    assert prog.received_parts == []
    assert prog.missing_parts == [1, 2, 3]
    assert prog.bytes_received == 0
    assert prog.expires_at > prog.updated_at


def test_happy_path_complete_and_readback(service):
    parts = [b"aaa", b"bbb", b"ccc"]
    sid, total = service.create_session(
        "data/ok.bin", 3, 9, sha(b"aaabbbccc"), None).session_id, b"aaabbbccc"
    for i, p in enumerate(parts, 1):
        info = service.put_part(sid, i, p, sha(p))
        assert info.number == i and info.size == len(p) and info.digest == sha(p)

    prog = service.get_progress(sid)
    assert prog.received_parts == [1, 2, 3]
    assert prog.missing_parts == []

    result = service.complete_session(sid, None)
    assert result.status.value == "complete"
    assert result.digest == sha(total) and result.size == 9
    data, meta = service.read_object("data/ok.bin")
    assert data == total and meta.digest == sha(total)


def test_part_index_out_of_range(service):
    sid = service.create_session("k", 2, 6, None, None).session_id
    with pytest.raises(PartIndexOutOfRange):
        service.put_part(sid, 0, b"x", None)
    with pytest.raises(PartIndexOutOfRange):
        service.put_part(sid, 3, b"xxx", None)
    # 未越界的块不受影响
    service.put_part(sid, 1, b"aaa", None)
    assert service.get_progress(sid).received_parts == [1]


def test_complete_rejected_when_parts_missing(service):
    sid = service.create_session("k", 3, 9, None, None).session_id
    service.put_part(sid, 1, b"aaa", None)
    with pytest.raises(PartsMissing) as ei:
        service.complete_session(sid, None)
    # 错误原因可区分且指出缺失序号
    assert "2" in ei.value.message and "3" in ei.value.message
    # 失败后会话仍可继续写（未被污染）
    service.put_part(sid, 2, b"bbb", None)
    service.put_part(sid, 3, b"ccc", None)
    result = service.complete_session(sid, None)
    assert result.status.value == "complete"


def test_abort_cleans_and_locks_session(service):
    sid = service.create_session("k/abort", 2, 6, None, None).session_id
    service.put_part(sid, 1, b"aaa", None)
    object_key, status = service.abort_session(sid)
    assert object_key == "k/abort" and status == "aborted"

    import os
    parts_dir = service.storage._parts_dir(sid)
    assert not os.path.exists(parts_dir) or os.listdir(parts_dir) == []

    with pytest.raises(SessionStateConflict):
        service.put_part(sid, 2, b"bbb", None)
    with pytest.raises(SessionStateConflict):
        service.complete_session(sid, None)
    with pytest.raises(SessionStateConflict):
        service.abort_session(sid)


def test_complete_then_write_rejected(service):
    sid = service.create_session("k", 1, 3, sha(b"abc"), None).session_id
    service.put_part(sid, 1, b"abc", sha(b"abc"))
    service.complete_session(sid, None)
    with pytest.raises(SessionStateConflict):
        service.put_part(sid, 1, b"abc", None)


def test_unknown_session(service):
    with pytest.raises(SessionNotFound):
        service.get_progress("does-not-exist")
    with pytest.raises(SessionNotFound):
        service.put_part("does-not-exist", 1, b"x", None)


def test_duplicate_object_key_rejected(service):
    sid = service.create_session("dup/key", 1, 3, sha(b"abc"), None).session_id
    service.put_part(sid, 1, b"abc", sha(b"abc"))
    service.complete_session(sid, None)

    sid2 = service.create_session("dup/key", 1, 3, sha(b"abc"), None).session_id
    service.put_part(sid2, 1, b"abc", sha(b"abc"))
    with pytest.raises(ObjectExists):
        service.complete_session(sid2, None)
    # 被拒后原对象仍可读，第二个会话保持 pending
    assert service.get_progress(sid2).status.value == "pending"
    data, _ = service.read_object("dup/key")
    assert data == b"abc"


def test_unsafe_object_key_rejected(service):
    for bad in ["../escape", "a/../../b", "/leading", "a//b", "a/./b"]:
        with pytest.raises(ValueError):
            service.create_session(bad, 1, 0, None, None)
