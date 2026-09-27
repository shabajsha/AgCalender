import itertools
import time
from datetime import datetime, timedelta

import pytest

import approvals
import google_writer
import morning
from conftest import TZ, FakeTasks


@pytest.fixture
def bot(db, tg, monkeypatch):
    listener = approvals.Listener.__new__(approvals.Listener)   # skip __init__ (it talks to Google)
    listener.cfg = {"timezone": "Asia/Kolkata",
                    "planner": {"default_effort_hours": 3, "effort_choices_hours": [2, 4, 8, 12, 20, 30]},
                    "morning": {"todo_tasklist": "DAILY", "todo_default_minutes": 30}}
    listener.state, listener.tg, listener.tasks, listener.calendar, listener.learn_after = db, tg, FakeTasks(), None, 3
    listener.runs, listener._last_error_reply = [], 0.0
    monkeypatch.setattr(approvals.Listener, "_run_script",
                        staticmethod(lambda script, *a, unit=None: listener.runs.append((script, a, unit)) or True))
    monkeypatch.setattr(approvals.Listener, "_start_ingest_now", staticmethod(lambda: listener.runs.append("ingest") or True))
    monkeypatch.setattr(google_writer, "create_item", lambda *a: ("EV1", "TASK1"))
    return listener


def deadline(days=4, title="OS Midsem prep", recurrence=None):
    due = datetime.now(TZ) + timedelta(days=days)
    return {"type": "deadline", "title": title, "start": due, "end": due, "all_day": False, "due": due, "course": None,
            "location": None, "description": None, "recurrence": recurrence}


TAP_IDS = itertools.count(1)


def tap(bot, data, chat=42):
    bot.handle({"id": f"cb{next(TAP_IDS)}", "data": data, "message": {"chat": {"id": chat}, "message_id": 101}})


def say(bot, text, age=0):
    bot.handle_message({"chat": {"id": 42}, "text": text, "date": int(time.time() - age)})


def test_add_offers_effort_then_stores_it(bot, tg, db):
    approvals.ask(tg, db, "k1", deadline(), {"id": "m", "subject": "Midsem"}, "prof@iiit.ac.in")
    tap(bot, "add:1")
    assert [[label for label, _ in row] for row in tg.edits[101]["buttons"]] == [["2 h", "4 h", "8 h"], ["12 h", "20 h", "30 h"], ["Undo (10 min)"]]
    assert db.item_exists("k1") and db.get_pending(1)["status"] == "added"
    tap(bot, "effort:1:20")
    assert db.get_effort("event:EV1") == 20 and tg.answers[-1] == "20 h"
    created = []
    google_writer.create_item = lambda *a: created.append(a) or ("EV2", "T2")
    tap(bot, "add:1")                                          # a second tap: answered once, nothing created
    assert not created and tg.answers[-1] == ""


def test_strangers_are_ignored(bot, tg, db):
    approvals.ask(tg, db, "k1", deadline(), {"id": "m", "subject": "s"}, "x")
    tap(bot, "add:1", chat=999)
    assert tg.answers[-1] == "Not allowed" and db.get_pending(1)["status"] == "pending"


def test_three_skips_offer_to_block_sender(bot, tg, db):
    for i in range(3):
        approvals.ask(tg, db, f"c{i}", deadline(days=3 + i, title=f"Club {i}"), {"id": "m", "subject": "Fest"}, "club@x")
        tap(bot, f"skip:{i + 1}")
    assert "Stop asking" in tg.sent[-1]["text"]
    tap(bot, "block:3")
    assert db.blocked_senders() == {"club@x"}


def test_card_shows_recurrence(tg, db):
    approvals.ask(tg, db, "r", deadline(recurrence=["RRULE:FREQ=WEEKLY;BYDAY=TU"]), {"id": "m", "subject": "s"}, "x")
    assert "Repeats weekly on Tue" in tg.sent[-1]["text"]


def test_todo_command_and_undo(bot, tg):
    say(bot, "/todo Revise OS 1h30m")
    assert "Revise OS (1 h 30 min)" in tg.sent[-1]["text"] and tg.sent[-1]["buttons"] == [("Undo", "undo:1")]
    tap(bot, "undo:1")
    assert tg.edits[101]["text"] == "Removed: Revise OS" and bot.tasks.store == {}


def test_plain_text_is_a_todo_only_during_checkin(bot, tg, db):
    say(bot, "Buy milk")
    assert "Commands (or use the buttons below)" in tg.sent[-1]["text"]
    db.set_meta(morning.CHECKIN_SENT, datetime.now(TZ).isoformat())
    say(bot, "Finish lab report 2h\nCall bank 15m")
    today = datetime.now(TZ).date().isoformat()
    assert ("Done", f"checkin:done:{today}") in tg.sent[-1]["buttons"]
    tap(bot, f"checkin:done:{today}")
    assert bot.runs[-1] == ("morning.py", ("--finish",), "calendar-morning-now")
    say(bot, "Gym 1h")
    assert "Commands (or use the buttons below)" in tg.sent[-1]["text"]        # check-in closed


def test_commands(bot, tg, db):
    say(bot, "Check mail now")
    assert bot.runs[-1] == "ingest"
    db.set_paused(True)
    say(bot, "/check")
    assert "paused" in tg.sent[-1]["text"]
    say(bot, "/resume")
    assert db.paused_since() is None and bot.runs[-1] == "ingest"
    say(bot, "Plan rest of today")
    assert bot.runs[-1] == ("planner.py", (), "calendar-plan-now")
    say(bot, "/plan", age=3600)
    assert "laptop was off or asleep" in tg.sent[-2]["text"]
    say(bot, "/start")
    assert tg.sent[-1]["keyboard"] == approvals.KEYBOARD


def test_every_tap_is_answered_before_the_slow_work(bot, tg, db, monkeypatch):
    seen = []
    monkeypatch.setattr(approvals.calwatch, "handle_callback", lambda *a: seen.append(list(tg.answers)))
    tap(bot, "cal:t:1")
    assert seen == [[""]]            # the spinner was stopped before calwatch started its Google calls


def test_yesterdays_done_button_does_not_start_today(bot, tg, db):
    yesterday = (datetime.now(TZ) - timedelta(days=1)).date().isoformat()
    tap(bot, f"checkin:done:{yesterday}")
    assert not bot.runs and db.get_meta(morning.CHECKIN_ANSWERED) is None
    assert "check-in for" in tg.edits[101]["text"] and tg.answers[-1] == "Old button"


def test_typing_done_closes_the_checkin(bot, tg, db):
    db.set_meta(morning.CHECKIN_SENT, datetime.now(TZ).isoformat())
    say(bot, "Done!")
    assert bot.runs[-1] == ("morning.py", ("--finish",), "calendar-morning-now") and bot.tasks.store == {}


def test_plan_and_clear_use_their_own_units(bot, tg):
    say(bot, "/plan")
    say(bot, "/clear")
    assert [r[2] for r in bot.runs[-2:]] == ["calendar-plan-now", "calendar-clear-now"]


def test_busy_planner_is_reported_not_claimed(bot, tg, monkeypatch):
    monkeypatch.setattr(approvals.Listener, "_run_script", staticmethod(lambda script, *a, unit=None: "busy"))
    say(bot, "/clear")
    assert tg.sent[-1]["text"].startswith("Already busy")


def test_a_failed_tap_is_reported(bot, tg):
    from googleapiclient.errors import HttpError
    import httplib2
    bot.report_error(HttpError(httplib2.Response({"status": 503}), b"unavailable"))
    assert "Couldn't reach Google" in tg.sent[-1]["text"]


def test_skipped_list_and_read_anyway(bot, tg, db):
    db.mark_processed("gm1", "fp", "no-keyword", subject="Reminder: Mid semester feedback")
    say(bot, "/skipped")
    assert "Mid semester feedback" in tg.sent[-1]["text"] and tg.sent[-1]["buttons"] == [[("Read 1", "read:gm1")]]
    tap(bot, "read:gm1")
    assert bot.runs[-1] == ("ingest.py", ("--message", "gm1"), "calendar-read-gm1") and tg.answers[-1].startswith("Reading")


def test_status_shows_health(bot, tg, db):
    say(bot, "/status")
    text = tg.sent[-1]["text"]
    assert "Google login: OK" in text and "GPU for the model: OK" in text and "Problems in the last 24 h: none" in text
