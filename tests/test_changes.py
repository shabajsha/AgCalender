"""Emails and invites that move or cancel something you already have."""
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

import changes
import ingest
from conftest import TZ, FakeCalendar, FakeTasks, FakeTelegram, event

NOW = datetime.now(TZ).replace(microsecond=0)
CFG = {"timezone": "Asia/Kolkata", "tasklist": "AGENT", "calendars": {"college": "COL", "planner": "PLAN", "habits": "HAB"}}


def deadline(title, due):
    return {"type": "deadline", "title": title, "start": due - timedelta(minutes=30), "end": due, "all_day": False,
            "due": due, "course": None, "location": None, "description": None, "recurrence": None}


@pytest.mark.parametrize("a, b, same", [("SDET midsem", "SDET Midsem exam", True), ("DSA Assignment 2", "DSA Assignment 3", False),
                                        ("OS Quiz", "OS quiz", True), ("Robotics lab", "Signals lab", False)])
def test_same_thing(a, b, same):
    assert changes.same_thing(a, b) is same


@pytest.fixture
def world(db):
    old_due = (NOW + timedelta(days=3)).replace(hour=17, minute=0, second=0)
    cal = FakeCalendar([], events={"COL": [event("E1", old_due - timedelta(minutes=30), old_due, "DUE: SDET midsem")]})
    db.record_item("sdet midsem|x", "deadline", "E1", "T1", "SDET midsem", old_due.isoformat(), "m0")
    tg, tasks = FakeTelegram(), FakeTasks()
    lis = SimpleNamespace(cfg=CFG, state=db, tg=tg, tasks=tasks, calendar=cal)
    msg = {"id": "m1", "subject": "SDET midsem postponed", "body": "The midsem is postponed to Friday.", "ics": []}
    return SimpleNamespace(cal=cal, tg=tg, tasks=tasks, lis=lis, old_due=old_due, msg=msg, db=db)


def test_moved_deadline_asks_and_moves(world):
    new_due = world.old_due + timedelta(days=2)
    rest, asked = ingest.changes_in(CFG, world.db, world.cal, world.tg, world.msg, "prof@iiit.ac.in",
                                    [deadline("SDET midsem", new_due)], NOW)
    assert rest == [] and asked == 1
    card = world.tg.sent[-1]
    assert card["text"].startswith("Changed? SDET midsem\nWas: ") and card["buttons"][0] == ("Move it", "mv:1")
    changes.handle(world.lis, world.db.get_pending(1), "mv", NOW)
    assert world.cal.patched[-1][1] == "E1" and world.cal.patched[-1][2]["end"]["dateTime"].startswith(new_due.strftime("%Y-%m-%dT%H:%M"))
    assert world.tasks.patched[-1][1] == "T1"
    assert world.db.event_id_for(world.db.get_pending(1)["dedupe_key"]) == "E1"   # now known at its new time


def test_different_assignment_is_new(world):
    world.db.record_item("dsa a2|x", "deadline", "E2", "T2", "DSA Assignment 2", world.old_due.isoformat(), "m0")
    rest, asked = ingest.changes_in(CFG, world.db, world.cal, world.tg, {**world.msg, "subject": "A3", "body": "A3 out"},
                                    "prof@x", [deadline("DSA Assignment 3", world.old_due + timedelta(days=5))], NOW)
    assert len(rest) == 1 and asked == 0


def test_cancellation_email_asks_and_removes(world):
    msg = {**world.msg, "subject": "SDET midsem cancelled", "body": "The SDET midsem stands cancelled."}
    rest, asked = ingest.changes_in(CFG, world.db, world.cal, world.tg, msg, "prof@x", [], NOW)
    assert asked == 1 and world.tg.sent[-1]["text"].startswith("Cancelled? SDET midsem")
    changes.handle(world.lis, world.db.get_pending(1), "cx", NOW)
    assert ("COL", "E1") in world.cal.deleted and world.db.item_by_event("E1") is None
    rest, asked = ingest.changes_in(CFG, world.db, world.cal, world.tg, msg, "prof@x", [], NOW)
    assert asked == 0                                                     # asked once only


def test_cancelled_invite_found_by_uid(world, monkeypatch):
    world.cal.evs["COL"].append(event("E9", NOW + timedelta(days=1), NOW + timedelta(days=1, hours=1), "Project sync",
                                      extendedProperties={"private": {"ics_uid": "abc@x"}}))
    ics = b"BEGIN:VCALENDAR\nMETHOD:CANCEL\nBEGIN:VEVENT\nUID:abc@x\nSUMMARY:Project sync\nDTSTART:20261001T100000\nEND:VEVENT\nEND:VCALENDAR\n"
    rest, asked = ingest.changes_in(CFG, world.db, world.cal, world.tg, {**world.msg, "ics": [ics]}, "x", [], NOW)
    assert asked == 1 and "Cancelled? Project sync" in world.tg.sent[-1]["text"]


def test_ignoring_a_change_card_is_not_a_sender_skip(world, monkeypatch):
    import approvals
    rest, _ = ingest.changes_in(CFG, world.db, world.cal, world.tg, world.msg, "prof@x",
                                [deadline("SDET midsem", world.old_due + timedelta(days=1))], NOW)
    lis = approvals.Listener.__new__(approvals.Listener)
    lis.cfg, lis.state, lis.tg, lis.learn_after, lis._last_error_reply = CFG, world.db, world.tg, 1, 0.0
    lis.handle({"id": "c1", "data": "skip:1", "message": {"chat": {"id": 42}, "message_id": 1}})
    assert world.tg.edits[world.db.get_pending(1)["tg_message_id"]]["text"] == "SDET midsem: left as it was."
    assert not any("Stop asking" in m["text"] for m in world.tg.sent)
