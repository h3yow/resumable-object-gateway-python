"""过期会话清理：手动 sweep、TTL 滑动、后台线程与重启后过期判定。"""

from __future__ import annotations

import logging
import os
import time

import pytest

from app.config import Settings
from app.errors import SessionExpired, SessionStateConflict
from app.service import UploadService
from tests.conftest import assert_log_contains, sha


def test_sweep_expires_inactive_sessions(tmp_path, caplog):
    svc = UploadService(Settings(
        data_dir=str(tmp_path / "d"), session_ttl_seconds=100,
        enable_sweeper=False))
    svc.start()
    sid = svc.create_session("k/exp", 2, 6, None, None).session_id
    svc.put_part(sid, 1, b"aaa", None)

    # T+50 未过期
    assert svc.sweep_expired(now=time.time() + 50) == []
    assert svc.get_progress(sid).status.value == "pending"

    # T+101 过期
    caplog.set_level(logging.INFO, logger="resumable_object_gateway")
    cleaned = svc.sweep_expired(now=time.time() + 101)
    assert cleaned == [sid]
    assert_log_contains(caplog.records, sid, None, "过期清理")

    assert svc.get_progress(sid).status.value == "expired"
    parts_dir = svc.storage._parts_dir(sid)
    assert not os.path.exists(parts_dir) or not os.listdir(parts_dir)

    # 过期后写入/完成/中止均被拒绝且原因可区分
    with pytest.raises(SessionExpired):
        svc.put_part(sid, 2, b"bbb", None)
    with pytest.raises(SessionExpired):
        svc.complete_session(sid, None)
    with pytest.raises(SessionExpired):
        svc.abort_session(sid)
    svc.shutdown()


def test_part_activity_slides_expiry(tmp_path):
    """分块写入会滑动会话过期时间。"""
    svc = UploadService(Settings(
        data_dir=str(tmp_path / "d"), session_ttl_seconds=100,
        enable_sweeper=False))
    svc.start()
    sid = svc.create_session("k/slide", 2, 6, None, None).session_id
    svc.put_part(sid, 1, b"aaa", None)
    first_progress = svc.get_progress(sid)
    assert first_progress.expires_at > first_progress.updated_at

    # 在真实时间稍后写入第二块，updated_at/expires_at 必须后移
    time.sleep(0.01)
    svc.put_part(sid, 2, b"bbb", None)
    second_progress = svc.get_progress(sid)
    assert second_progress.updated_at > first_progress.updated_at
    assert second_progress.expires_at > first_progress.expires_at

    # 以“创建后 99 秒”注入清理时间：对初始过期点已过期，
    # 但由于活动滑动了窗口，会话不应被清理
    created_at = svc.storage._load_meta(sid).raw["created_at"]
    assert svc.sweep_expired(now=created_at + 99) == []
    assert svc.get_progress(sid).status.value == "pending"
    svc.shutdown()


def test_background_sweeper_thread(tmp_path):
    svc = UploadService(Settings(
        data_dir=str(tmp_path / "d"), session_ttl_seconds=60,
        enable_sweeper=True, sweeper_interval_seconds=0.05))
    svc.start()
    sid = svc.create_session("k/bg", 1, 3, None, None).session_id
    # 直接把 expires_at 改到过去
    with svc.storage._lock:
        meta = svc.storage._load_meta(sid)
        meta.raw["expires_at"] = time.time() - 1
        svc.storage._save_meta_locked(meta)
    deadline = time.time() + 3
    while time.time() < deadline:
        if svc.get_progress(sid).status.value == "expired":
            break
        time.sleep(0.05)
    assert svc.get_progress(sid).status.value == "expired"
    svc.shutdown()


def test_expired_status_persists_across_restart(tmp_path):
    d = str(tmp_path / "d")
    svc = UploadService(Settings(data_dir=d, session_ttl_seconds=100,
                                 enable_sweeper=False))
    svc.start()
    sid = svc.create_session("k/persist", 1, 3, None, None).session_id
    svc.put_part(sid, 1, b"aaa", None)
    svc.sweep_expired(now=time.time() + 101)
    svc.shutdown()

    svc2 = UploadService(Settings(data_dir=d, session_ttl_seconds=100,
                                  enable_sweeper=False))
    svc2.start()
    with pytest.raises(SessionExpired):
        svc2.put_part(sid, 1, b"aaa", None)
    svc2.shutdown()
