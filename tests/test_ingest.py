import sys
from datetime import datetime, timezone

import pytest

import alerts
import ingest
from conftest import at

CFG = {"timezone": "Asia/Kolkata", "gmail_query": "label:iiith", "keywords": ["due"], "skip_senders": [],
       "approval": {"enabled": False}, "first_run_days": 3, "ollama": {}, "min_confidence": 0.6,
       "calendars": {"college": "COL"}, "heartbeat_url": ""}

MSG = {"id": "m1", "subject": "Broken invite", "received": at(2026, 9, 28, 9), "from_line": "Prof <p@iiit.ac.in>",
       "sender": "Prof <p@iiit.ac.in>", "body": "text", "ics": []}


@pytest.fixture
def run(monkeypatch, db):
    sent = []
    monkeypatch.setattr(ingest, "load_config", lambda: CFG)
    monkeypatch.setattr(ingest, "State", lambda dry_run=False: db)
    monkeypatch.setattr(ingest, "get_credentials", lambda: None)
    monkeypatch.setattr(ingest, "build", lambda *a, **k: None)
    monkeypatch.setattr(ingest, "fetch_messages",
                        lambda svc, q, after, tz, skip=None: [] if skip and skip(MSG["id"]) else [dict(MSG)])
    monkeypatch.setattr(ingest, "items_for", lambda msg, cfg, now: (_ for _ in ()).throw(ValueError("bad .ics")))
    monkeypatch.setattr(alerts, "alert", lambda key, text, state=None: sent.append(key))
    monkeypatch.setattr(alerts, "resolved", lambda key, state=None: None)
    monkeypatch.setattr(sys, "argv", ["ingest.py"])
    return sent


def test_an_email_that_always_fails_is_given_up_after_three_tries(run, db):
    ingest.main()
    ingest.main()
    assert not db.is_processed("m1") and db.get_last_run() is None        # still retrying
    ingest.main()
    assert db.is_processed("m1") and "gave-up:m1" in run                   # third failure: skipped + alert
    assert db.get_last_run() is not None                                   # progress can continue
    ingest.main()
    assert run.count("gave-up:m1") == 1                                    # not downloaded or alerted again
    assert datetime.now(timezone.utc) >= db.get_last_run()
