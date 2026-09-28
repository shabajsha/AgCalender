"""Adding calendar events and deadlines by typing them to the bot."""
import itertools
import time as time_module
from datetime import date
from types import SimpleNamespace

import pytest

import approvals
import nlcommands
import quickadd
from conftest import FakeCalendar, FakeTasks, FakeTelegram, at

NOW = at(2026, 9, 28, 12, 0)   # a Monday
CFG = {"timezone": "Asia/Kolkata", "tasklist": "AGENT", "calendars": {"college": "COL", "planner": "PLAN", "habits": "HAB"},
       "ollama": {}, "planner": {"effort_choices_hours": [2, 4, 8, 12, 20, 30], "exam_prep_hours": {"quiz": 2}},
       "morning": {"todo_tasklist": "DAILY", "todo_default_minutes": 30}}
IDS = itertools.count(1)


@pytest.mark.parametrize("text, kind, expect", [
    ("Meeting with Harsha tomorrow at 3pm", "event", ("meeting", at(2026, 9, 29, 15), at(2026, 9, 29, 16))),
    ("SMAI quiz on 5 Oct 10am for 2h", "event", ("event", at(2026, 10, 5, 10), at(2026, 10, 5, 12))),
    ("Megathon demo 12 Oct 2-4pm", "event", ("event", at(2026, 10, 12, 14), at(2026, 10, 12, 16))),
    ("Call with mom at 8", "event", ("meeting", at(2026, 9, 28, 20), at(2026, 9, 28, 21))),     # 8 am has passed: 8 pm
])
def test_events_from_words(text, kind, expect):
    title, when = quickadd.split(text)
    item, reason = quickadd.build(title, when, kind, NOW, "Asia/Kolkata")
    assert reason is None and (item["type"], item["start"], item["end"]) == expect


def test_all_day_event_and_deadline_defaults():
    item, _ = quickadd.build("Fest", "on 3 Oct", "event", NOW, "Asia/Kolkata")
    assert item["all_day"] and item["start"] == date(2026, 10, 3)
    item, _ = quickadd.build("OS report", "Friday", "deadline", NOW, "Asia/Kolkata")
    assert item["due"] == at(2026, 10, 2, 23, 59) and item["end"] == item["due"]


def test_past_times_are_refused():
    item, reason = quickadd.build("Seminar", "today at 9am", "event", NOW, "Asia/Kolkata")
    assert item is None and "already passed" in reason


@pytest.fixture
def lis(db):
    cal, tasks = FakeCalendar([]), FakeTasks()
    return SimpleNamespace(cfg=CFG, state=db, tg=FakeTelegram(), calendar=cal, tasks=tasks)


def yes(lis):
    msg = lis.tg.sent[-1]
    nlcommands.handle(lis, {"id": f"q{next(IDS)}", "message": {"message_id": msg["id"]}}, msg["buttons"][0][0][1].partition(":")[2], NOW)
    return lis.tg.edits[msg["id"]]


def test_typed_meeting_is_added_after_yes_and_can_be_undone(lis):
    assert nlcommands.handle_text(lis, "meeting with Harsha tomorrow at 3pm", NOW)
    assert lis.tg.sent[-1]["text"] == "Add to your calendar: Meeting with Harsha, Tue 29 Sep, 15:00-16:00?"
    assert not lis.calendar.inserted                                   # nothing until Yes
    done = yes(lis)
    [(event_id, (cal_id, body))] = lis.calendar.inserted.items()
    assert cal_id == "COL" and body["summary"] == "Meeting with Harsha" and body["start"]["dateTime"].startswith("2026-09-29T15:00")
    assert done["buttons"] == [[("Undo", f"evu:{event_id}")]] and lis.state.item_by_event(event_id)
    nlcommands.undo_event(lis, {"message": {"message_id": 7}}, event_id, NOW)
    assert ("COL", event_id) in lis.calendar.deleted and lis.state.item_by_event(event_id) is None


def test_typed_deadline_gets_a_task_and_effort_buttons(lis):
    nlcommands.handle_text(lis, "DSA assignment due Friday 11:59pm", NOW)
    done = yes(lis)
    [(event_id, (_, body))] = lis.calendar.inserted.items()
    assert body["summary"] == "DUE: DSA assignment" and lis.tasks.store
    assert done["buttons"][0][0] == ("2 h", f"dle:{event_id}:2")


def test_typed_exam_offers_prep(lis):
    nlcommands.handle_text(lis, "SMAI quiz on 5 Oct 10am", NOW)
    assert "2 h of prep will be planned" in lis.tg.sent[-1]["text"]
    done = yes(lis)
    assert done["buttons"][0][0][1].startswith("prep:")


def test_the_same_event_twice_is_not_duplicated(lis):
    for _ in range(2):
        nlcommands.handle_text(lis, "meeting with Harsha tomorrow at 3pm", NOW)
        done = yes(lis)
    assert len(lis.calendar.inserted) == 1 and "already on your calendar" in done["text"]


def test_add_gym_is_still_a_todo_with_a_block():
    assert nlcommands.parse_rules("add gym at 6pm for 1h")["action"] == "add" and not quickadd.is_event("gym")


def test_event_command(db, monkeypatch):
    bot = approvals.Listener.__new__(approvals.Listener)
    bot.cfg, bot.state, bot.tg, bot.tasks, bot.calendar, bot.learn_after, bot._last_error_reply = \
        CFG, db, FakeTelegram(), FakeTasks(), FakeCalendar([]), 3, 0.0
    bot.handle_message({"chat": {"id": 42}, "text": "/event Megathon demo 12 Oct 2-4pm", "date": int(time_module.time())})
    assert bot.tg.sent[-1]["text"].startswith("Add to your calendar: Megathon demo, Mon 12 Oct, 14:00-16:00?")
    bot.handle_message({"chat": {"id": 42}, "text": "/event", "date": int(time_module.time())})
    assert "/event Meeting with Harsha" in bot.tg.sent[-1]["text"]
