"""进程重启后的会话恢复、断点续传与终态持久化。"""

from __future__ import annotations

import pytest

from app.errors import SessionStateConflict
from tests.conftest import sha


def test_resume_pending_session_after_restart(service, settings):
    parts = [b"aaaa", b"bbbb", b"cccc"]
    total = b"".join(parts)
    sid = service.create_session(
        "k/resume", 3, len(total), sha(total), None).session_id
    service.put_part(sid, 1, parts[0], sha(parts[0]))
    service.put_part(sid, 2, parts[1], sha(parts[1]))
    service.shutdown()

    # 模拟进程重启：同一 data_dir 新建 service
    from app.service import UploadService
    svc2 = UploadService(settings)
    svc2.start()
    prog = svc2.get_progress(sid)
    assert prog.status.value == "pending"
    assert prog.received_parts == [1, 2]
    assert prog.missing_parts == [3]

    svc2.put_part(sid, 3, parts[2], sha(parts[2]))
    result = svc2.complete_session(sid, None)
    assert result.digest == sha(total)
    data, _ = svc2.read_object("k/resume")
    assert data == total
    svc2.shutdown()


def test_terminal_sessions_remain_locked_after_restart(service, settings):
    sid = service.create_session("k/done", 1, 3, sha(b"abc"), None).session_id
    service.put_part(sid, 1, b"abc", sha(b"abc"))
    service.complete_session(sid, None)
    service.shutdown()

    from app.service import UploadService
    svc2 = UploadService(settings)
    svc2.start()
    assert svc2.get_progress(sid).status.value == "complete"
    with pytest.raises(SessionStateConflict):
        svc2.put_part(sid, 1, b"abc", None)
    data, _ = svc2.read_object("k/done")
    assert data == b"abc"
    svc2.shutdown()


def test_reconcile_orphan_part_records(tmp_path, settings):
    """人为删除一个分块文件模拟崩溃残留：重启后记录被剔除，会话可续传。"""
    import os
    from app.service import UploadService
    svc = UploadService(settings)
    svc.start()
    sid = svc.create_session("k/orphan", 2, 6, None, None).session_id
    svc.put_part(sid, 1, b"aaa", None)
    svc.put_part(sid, 2, b"bbb", None)
    # 删掉 part2 的数据文件，制造“有记录无文件”的不一致
    os.unlink(svc.storage._part_path(sid, 2))
    svc.shutdown()

    svc2 = UploadService(settings)
    svc2.start()
    prog = svc2.get_progress(sid)
    assert prog.received_parts == [1] and prog.bytes_received == 3
    svc2.put_part(sid, 2, b"bbb", None)
    result = svc2.complete_session(sid, None)
    assert result.status.value == "complete"
    data, _ = svc2.read_object("k/orphan")
    assert data == b"aaabbb"
    svc2.shutdown()
