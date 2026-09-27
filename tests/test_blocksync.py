"""Blocks moved or deleted in the Google Calendar app are followed by the bot."""
import itertools
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

import actions
import nlcommands
import slotpicker
from conftest import TZ, FakeCalendar, FakeTelegram, event

NOW = datetime.now(TZ).replace(second=0, microsecond=0)
CFG = {"timezone": "Asia/Kolkata", "calendars": {"college": "COL", "planner": "PLAN", "habits": "HAB"}, "ollama": {},
       "planner": {"work_window": ["00:00", "23:59"], "sleep": ["03:00", "03:01"], "block_minutes": 90},
       "morning": {"heads_up_minutes": 5}}
IDS = itertools.count(1)


def tagged(event_id, start, end, title, key="task:a"):
    return event(event_id, start, end, f"Work: {title}",
                 extendedProperties={"private": {"source": "calendar-agent-planner", "work_key": key}})


@pytest.fixture
def world(db):
    start = NOW + timedelta(minutes=4)
    cal = FakeCalendar([], events={"PLAN": [tagged("ev1", start, start + timedelta(minutes=90), "Discuss with harsha")]})
    bid = db.block_add(None, "ev1", "task:a", "Discuss with harsha", start, start + timedelta(minutes=90), calendar="PLAN")
    return SimpleNamespace(cal=cal, db=db, bid=bid, start=start)


def test_a_block_moved_in_the_calendar_app_is_followed(world):
    later = world.start + timedelta(hours=8)
    world.cal.evs["PLAN"][0] = tagged("ev1", later, later + timedelta(minutes=90), "Discuss with harsha")
    notes = actions.sync_blocks(CFG, world.db, world.cal, NOW)
    b = world.db.block(world.bid)
    assert datetime.fromisoformat(b["start"]) == later and b["status"] == "booked" and notes[0].startswith("Discuss with harsha: moved")


def test_moved_to_next_week_is_followed_too(world):
    far = world.start + timedelta(days=7)
    world.cal.masters["PLAN"] = {"ev1": tagged("ev1", far, far + timedelta(hours=1), "Discuss with harsha")}
    world.cal.evs["PLAN"] = []                                   # not in the 3-day window any more; found by id
    actions.sync_blocks(CFG, world.db, world.cal, NOW)
    assert datetime.fromisoformat(world.db.block(world.bid)["start"]) == far


def test_a_block_deleted_in_the_app_gets_no_questions(world):
    world.cal.evs["PLAN"] = []
    actions.sync_blocks(CFG, world.db, world.cal, NOW)
    assert world.db.block(world.bid)["status"] == "cleared"
    tg = FakeTelegram()
    slotpicker.tick(CFG, world.db, world.start + timedelta(hours=3), tg, lambda: (world.cal, None))
    assert not tg.sent                                           # no "Did you finish?" for a deleted block


def test_a_question_already_asked_is_asked_again_after_moving_later(world):
    world.db.block_set(world.bid, status="asked")
    later = NOW + timedelta(hours=5)
    world.cal.evs["PLAN"][0] = tagged("ev1", later, later + timedelta(hours=1), "Discuss with harsha")
    actions.sync_blocks(CFG, world.db, world.cal, NOW)
    assert world.db.block(world.bid)["status"] == "booked"


def test_unknown_agent_blocks_are_picked_up(world):
    s = NOW + timedelta(hours=2)
    world.cal.evs["PLAN"].append(tagged("ev2", s, s + timedelta(hours=1), "Old plan block", key="task:b"))
    actions.sync_blocks(CFG, world.db, world.cal, NOW)
    b = world.db.block_by_event("ev2")
    assert b and b["title"] == "Old plan block" and b["work_key"] == "task:b" and b["status"] == "booked"


def test_no_heads_up_at_the_old_time(world):
    later = world.start + timedelta(hours=2)
    world.cal.evs["PLAN"][0] = tagged("ev1", later, later + timedelta(minutes=90), "Discuss with harsha")
    lis = SimpleNamespace(cfg=CFG, state=world.db, calendar=world.cal, tg=FakeTelegram())
    slotpicker.heads_up(lis, NOW)
    assert not lis.tg.sent


def test_today_and_the_question_show_the_new_time(world, monkeypatch):
    import digest
    monkeypatch.setattr(digest, "events_today", lambda *a, **k: ["- Nothing scheduled"])
    later = world.start + timedelta(hours=2)
    world.cal.evs["PLAN"][0] = tagged("ev1", later, later + timedelta(minutes=90), "Discuss with harsha")
    lis = SimpleNamespace(cfg=CFG, state=world.db, calendar=world.cal, tg=FakeTelegram(), tasks=None)
    assert nlcommands.handle_text(lis, "what's scheduled today?", NOW)
    text = lis.tg.sent[-1]["text"]
    assert text.startswith("Today - ") and f"{later:%H:%M}-" in text and "Discuss with harsha" in text


def test_not_done_then_moved_later_in_the_app_is_booked_again(world):
    world.db.block_set(world.bid, status="notdone")
    later = NOW + timedelta(hours=6)
    world.cal.evs["PLAN"][0] = tagged("ev1", later, later + timedelta(hours=1), "Discuss with harsha")
    notes = actions.sync_blocks(CFG, world.db, world.cal, NOW)
    b = world.db.block(world.bid)
    assert b["status"] == "booked" and datetime.fromisoformat(b["start"]) == later and "rescheduled by you" in notes[0]


def test_not_done_and_left_alone_stays_not_done(world):
    world.db.block_set(world.bid, status="notdone")
    actions.sync_blocks(CFG, world.db, world.cal, NOW)
    assert world.db.block(world.bid)["status"] == "notdone"
