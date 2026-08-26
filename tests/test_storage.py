from remote_gateway.storage import Storage, utc_now


def _make_session(storage: Storage, session_id: str, driver: str, status: str, last_activity: str) -> None:
    storage.create_session({
        "id": session_id, "driver": driver, "model": "x", "working_directory": None,
        "status": status, "created_at": last_activity, "last_activity": last_activity,
    })


def test_count_active_sessions_excludes_terminal_statuses():
    storage = Storage(":memory:")
    now = utc_now()
    _make_session(storage, "sess_1", "claude-code", "idle", now)
    _make_session(storage, "sess_2", "claude-code", "processing", now)
    _make_session(storage, "sess_3", "claude-code", "interrupted", now)  # still resumable, still active
    _make_session(storage, "sess_4", "claude-code", "completed", now)
    _make_session(storage, "sess_5", "claude-code", "error", now)
    _make_session(storage, "sess_6", "claude-code", "expired", now)
    _make_session(storage, "sess_7", "gemini", "idle", now)

    assert storage.count_active_sessions() == 4  # 1, 2, 3 (claude-code) + 7 (gemini)
    assert storage.count_active_sessions("claude-code") == 3
    assert storage.count_active_sessions("gemini") == 1
    assert storage.count_active_sessions("codex") == 0


def test_expire_stale_sessions_only_touches_old_inactive_ones():
    storage = Storage(":memory:")
    old = "2020-01-01T00:00:00+00:00"
    _make_session(storage, "sess_old_idle", "claude-code", "idle", old)
    _make_session(storage, "sess_old_completed", "claude-code", "completed", old)  # already terminal, untouched
    _make_session(storage, "sess_recent", "claude-code", "idle", utc_now())

    expired_count = storage.expire_stale_sessions(cutoff="2025-01-01T00:00:00+00:00")

    assert expired_count == 1
    assert storage.get_session("sess_old_idle")["status"] == "expired"
    assert storage.get_session("sess_old_completed")["status"] == "completed"  # untouched, was already terminal
    assert storage.get_session("sess_recent")["status"] == "idle"  # untouched, not stale


def test_session_stats_reports_active_count_and_total_messages():
    storage = Storage(":memory:")
    now = utc_now()
    _make_session(storage, "sess_1", "claude-code", "idle", now)
    _make_session(storage, "sess_2", "claude-code", "completed", now)
    storage.update_session("sess_1", message_count=3)
    storage.update_session("sess_2", message_count=5)

    stats = storage.session_stats("claude-code")
    assert stats == {"sessions_active": 1, "messages_total": 8}
