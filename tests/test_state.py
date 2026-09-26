from datetime import date, datetime, timedelta, timezone


def test_fingerprint_only_counts_recent_duplicates(db):
    db.mark_processed("m1", "fp", "items")
    assert db.fingerprint_seen("fp")
    old = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat(timespec="seconds")
    db.db.execute("UPDATE processed_messages SET processed_at = ? WHERE msg_id = 'm1'", (old,))
    assert not db.fingerprint_seen("fp")      # same text a week later = a new email (weekly reminder)


def test_failure_counter(db):
    assert [db.record_failure("m", "boom") for _ in range(3)] == [1, 2, 3]


def test_meta_efforts_plans_pause(db):
    db.set_meta("k", "v")
    assert db.get_meta("k") == "v" and db.get_meta("missing") is None
    db.set_effort("event:e1", 20)
    assert db.get_effort("event:e1") == 20
    assert db.plan_status(date(2026, 9, 28)) is None
    db.record_plan(date(2026, 9, 28), "auto")
    assert db.plan_status(date(2026, 9, 28)) == "auto"
    db.set_paused(True)
    assert db.paused_since() is not None
    db.set_paused(False)
    assert db.paused_since() is None


def test_task_for_event(db):
    db.record_item("k", "deadline", "EV1", "T1", "DSA", "2026-10-02", "m")
    assert db.task_for_event("EV1") == "T1" and db.task_for_event("nope") is None


def test_db_file_is_private(db, tmp_path):
    assert (tmp_path / "test.db").stat().st_mode & 0o077 == 0
