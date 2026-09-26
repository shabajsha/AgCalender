from datetime import timedelta

import pytest

import morning
from conftest import at
from telegram_bot import TelegramError

CFG = {"timezone": "Asia/Kolkata", "planner": {"plan_after": "06:45"}, "digest": {"channels": ["telegram"]},
       "morning": {"checkin": True, "wait_minutes": 45, "todo_default_minutes": 30}}


def T(h, m=0, d=0):
    return at(2026, 9, 28, h, m) + timedelta(days=d)


@pytest.fixture
def calls(monkeypatch, tmp_path):
    log = {"checkins": [], "plans": [], "sent": [], "deliver_ok": True, "checkin_error": False}

    def send_checkin(cfg, state, now, late):
        if log["checkin_error"]:
            raise TelegramError("sendMessage: network error")
        log["checkins"].append((now, late))
        state.set_meta(morning.CHECKIN_SENT, now.isoformat())

    monkeypatch.setattr(morning, "send_checkin", send_checkin)
    monkeypatch.setattr(morning.planner, "plan_today",
                        lambda cfg, st, now, how: log["plans"].append(now) or ("Plan for x", "Work\n- 09:00  DSA", 1))
    monkeypatch.setattr(morning.digest, "build_digest",
                        lambda cfg, now, skip_planner_blocks: ("t", [("Today", ["- class"]), ("Deadlines", ["- None"])]))
    monkeypatch.setattr(morning, "deliver",
                        lambda title, text, ch, buttons=None: (log["sent"].append(text) or ch) if log["deliver_ok"] else [])
    monkeypatch.setattr(morning, "LOG_DIR", tmp_path)
    return log


def test_normal_morning(calls, db):
    morning.tick(CFG, db, T(6, 30))
    assert not calls["checkins"]                                # before 06:45
    morning.tick(CFG, db, T(6, 45))
    assert calls["checkins"] == [(T(6, 45), False)]
    morning.tick(CFG, db, T(7))
    assert not calls["plans"]                                   # waiting for you
    db.set_meta(morning.CHECKIN_ANSWERED, T(6).date().isoformat())
    morning.tick(CFG, db, T(7, 15))
    assert calls["plans"] == [T(7, 15)] and len(calls["sent"]) == 1
    morning.finish(CFG, db, T(7, 20))
    assert len(calls["sent"]) == 1                              # never twice


def test_no_reply_goes_ahead_after_wait(calls, db):
    morning.tick(CFG, db, T(11, 10, 1))
    assert calls["checkins"][0][1] is True                      # laptop was off at 06:45 -> late note
    morning.tick(CFG, db, T(11, 45, 1))
    assert not calls["sent"]
    morning.tick(CFG, db, T(12, 0, 1))
    assert calls["sent"] and calls["sent"][0].startswith("No reply to the check-in")


def test_telegram_down_at_wakeup_retries_then_goes_ahead(calls, db):
    calls["checkin_error"] = True
    for minute in (0, 15, 30):
        morning.tick(CFG, db, T(8, minute))
    assert not calls["plans"] and not calls["sent"]            # retried, day not lost
    morning.tick(CFG, db, T(8, 45))
    assert calls["plans"] and calls["sent"][0].startswith("Couldn't reach Telegram")


def test_failed_delivery_is_retried(calls, db):
    db.set_meta(morning.CHECKIN_SENT, T(6, 45).isoformat())
    db.set_meta(morning.CHECKIN_ANSWERED, T(6).date().isoformat())
    calls["deliver_ok"] = False
    morning.tick(CFG, db, T(7))
    assert db.get_meta(morning.MORNING_SENT) != T(6).date().isoformat()
    calls["deliver_ok"] = True
    morning.tick(CFG, db, T(7, 15))
    assert db.get_meta(morning.MORNING_SENT) == T(6).date().isoformat() and len(calls["sent"]) == 1


def test_error_while_planning_does_not_lose_the_day(calls, db, monkeypatch):
    db.set_meta(morning.CHECKIN_SENT, T(6, 45).isoformat())
    db.set_meta(morning.CHECKIN_ANSWERED, T(6).date().isoformat())
    monkeypatch.setattr(morning.planner, "plan_today", lambda *a, **k: (_ for _ in ()).throw(OSError("no network")))
    with pytest.raises(OSError):
        morning.tick(CFG, db, T(7))
    assert db.get_meta(morning.MORNING_SENT) in (None, "")       # next tick will try again
    assert db.get_meta(morning.IN_PROGRESS) == ""
