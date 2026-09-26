"""单块大小、会话总大小、全局容量与对象数量配额。"""

from __future__ import annotations

import pytest

from app.config import Settings
from app.errors import QuotaExceeded
from app.service import UploadService
from tests.conftest import sha


def make_svc(tmp_path, **kw) -> UploadService:
    defaults = dict(
        data_dir=str(tmp_path / "d"), enable_sweeper=False,
        session_ttl_seconds=3600, max_part_bytes=10, max_session_bytes=100,
        max_total_bytes=30, max_object_count=100,
    )
    defaults.update(kw)
    svc = UploadService(Settings(**defaults))
    svc.start()
    return svc


def test_part_oversize_rejected(tmp_path):
    svc = make_svc(tmp_path)
    sid = svc.create_session("k", 1, 100, None, None).session_id
    with pytest.raises(QuotaExceeded) as ei:
        svc.put_part(sid, 1, b"x" * 11, None)
    assert "单块上限" in ei.value.message
    svc.shutdown()


def test_session_total_exceeded(tmp_path):
    svc = make_svc(tmp_path)
    sid = svc.create_session("k", 2, 10, None, None).session_id
    svc.put_part(sid, 1, b"aaaaaa", None)  # 6
    with pytest.raises(QuotaExceeded) as ei:
        svc.put_part(sid, 2, b"bbbbb", None)  # 6+5=11 > 10
    assert "声明总大小" in ei.value.message
    svc.shutdown()


def test_create_session_over_session_quota(tmp_path):
    svc = make_svc(tmp_path)
    with pytest.raises(QuotaExceeded):
        svc.create_session("k", 1, 101, None, None)
    svc.shutdown()


def test_global_capacity_quota(tmp_path):
    svc = make_svc(tmp_path, max_part_bytes=100, max_session_bytes=100)
    # 第一个对象 20 字节成功
    sid1 = svc.create_session("o1", 1, 20, None, None).session_id
    svc.put_part(sid1, 1, b"a" * 20, None)
    svc.complete_session(sid1, None)
    # 第二个对象 15 字节 -> 35 > 30，完成时拒绝且无残缺对象
    sid2 = svc.create_session("o2", 1, 15, None, None).session_id
    svc.put_part(sid2, 1, b"b" * 15, None)
    with pytest.raises(QuotaExceeded):
        svc.complete_session(sid2, None)
    from app.errors import ObjectNotFound
    with pytest.raises(ObjectNotFound):
        svc.get_object_meta("o2")
    svc.shutdown()


def test_object_count_quota(tmp_path):
    svc = make_svc(tmp_path, max_object_count=1, max_total_bytes=10_000)
    sid1 = svc.create_session("o1", 1, 1, None, None).session_id
    svc.put_part(sid1, 1, b"a", None)
    svc.complete_session(sid1, None)
    sid2 = svc.create_session("o2", 1, 1, None, None).session_id
    svc.put_part(sid2, 1, b"b", None)
    with pytest.raises(QuotaExceeded) as ei:
        svc.complete_session(sid2, None)
    assert "数量" in ei.value.message
    svc.shutdown()


def test_quota_persists_across_restart(tmp_path):
    svc = make_svc(tmp_path, max_total_bytes=20, max_part_bytes=100,
                   max_session_bytes=100)
    sid = svc.create_session("o1", 1, 15, None, None).session_id
    svc.put_part(sid, 1, b"a" * 15, None)
    svc.complete_session(sid, None)
    svc.shutdown()

    svc2 = make_svc(tmp_path, max_total_bytes=20, max_part_bytes=100,
                    max_session_bytes=100)
    sid2 = svc2.create_session("o2", 1, 10, None, None).session_id
    svc2.put_part(sid2, 1, b"b" * 10, None)
    with pytest.raises(QuotaExceeded):
        svc2.complete_session(sid2, None)
    svc2.shutdown()
