"""Typed plan changes: understood, resolved in Python, confirmed before anything moves."""
import itertools
import json

from types import SimpleNamespace

import pytest

import actions
import google_writer
import nlcommands
from conftest import FakeTasks, FakeTelegram, at, event
from llm import LLMUnavailable

NOW = at(2026, 9, 28, 10, 0)
CFG = {"timezone": "Asia/Kolkata", "calendars": {"college": "COL", "planner": "PLAN", "habits": "HAB"}, "ollama": {},
       "planner": {"block_minutes": 90, "work_window": ["08:00", "23:00"]},
       "morning": {"todo_tasklist": "DAILY", "todo_default_minutes": 30}}
IDS = itertools.count(1)


@pytest.mark.parametrize("text, action, fields", [
    ("move SDET study to 7pm", "move", {"task": "SDET study", "when": "7pm"}),
    ("leetcode not today", "not_today", {"task": "leetcode"}),
    ("drop the SegFuser paper today", "not_today", {"task": "the SegFuser paper"}),
    ("busy 2-5pm", "busy", {"when": "2-5pm"}),
    ("I can't do anything 2pm-5pm tomorrow", "busy", {}),
    ("make midsem prep 10 hours", "effort", {"task": "midsem", "hours": "10 hours"}),
    ("add gym at 6pm for 1h", "add", {"task": "gym", "when": "at 6pm for 1h"}),
    ("swap SMAI and SDET A2", "swap", {"task": "SMAI", "task2": "SDET A2"}),
    ("push leetcode by 30 min", "push", {"task": "leetcode", "duration": "30 min"}),
    ("make SDET study 45 min", "resize", {"task": "SDET study", "duration": "45 min"}),
])
def test_rules(text, action, fields):
    parsed = nlcommands.parse_rules(text)
    assert parsed["action"] == action and all(parsed[k] == v for k, v in fields.items())


def test_small_talk_is_not_a_command():
    assert nlcommands.parse_rules("thanks") is None


@pytest.fixture
def plan(db, monkeypatch):
    free = [[(NOW, at(2026, 9, 28, 14)), (at(2026, 9, 28, 17), at(2026, 9, 28, 23))]]
    moved, created, deleted = [], [], []
    monkeypatch.setattr(actions, "free_on", lambda cfg, state, cal, day, now, ignore_ids=frozenset(): free[0])
    monkeypatch.setattr(google_writer, "list_events", lambda cal, cid, s, e: [
        event("mid", at(2026, 9, 30, 15), at(2026, 9, 30, 17), "SDET midsem")])
    monkeypatch.setattr(google_writer, "move_event", lambda cal, cid, eid, s, e, tz: moved.append((eid, s, e)))
    monkeypatch.setattr(google_writer, "create_block", lambda cal, cid, title, s, e, tz, kind, work_key=None, note=None:
                        created.append((cid, title, s, e, kind)) or f"new{len(created)}")
    monkeypatch.setattr(google_writer, "delete_event", lambda cal, cid, eid: deleted.append(eid))
    monkeypatch.setattr(actions, "sync_if_stale", lambda *a, **k: [])       # sync has its own tests
    b1 = db.block_add(None, "ev1", "task:a", "Study SDET MidSem", at(2026, 9, 28, 11), at(2026, 9, 28, 12, 30))
    b2 = db.block_add(None, "ev2", "task:b", "Study SMAI last Lecture", at(2026, 9, 28, 17), at(2026, 9, 28, 18, 30))
    item = db.plan_item_upsert(NOW.date(), "task:c", "Do LeetCode Daily", "task", at(2026, 9, 28, 23, 59).isoformat(), "DAILY", 30)
    lis = SimpleNamespace(cfg=CFG, state=db, tg=FakeTelegram(), calendar=object(), tasks=FakeTasks())
    return SimpleNamespace(lis=lis, free=free, moved=moved, created=created, deleted=deleted, b1=b1, b2=b2, item=item)


def say(plan, text):
    assert nlcommands.handle_text(plan.lis, text, NOW)
    return plan.lis.tg.sent[-1]


def tap(plan, data):
    msg = plan.lis.tg.sent[-1]
    nlcommands.handle(plan.lis, {"id": f"n{next(IDS)}", "message": {"message_id": msg["id"]}}, data.partition(":")[2], NOW)
    return plan.lis.tg.edits[msg["id"]]["text"]


def test_move_to_a_free_time_after_yes(plan):
    msg = say(plan, "move SDET study to 7pm")
    assert msg["text"] == "Move Study SDET MidSem to Mon 19:00-20:30?" and not plan.moved     # nothing yet
    assert tap(plan, msg["buttons"][0][0][1]).startswith("Moved Study SDET MidSem to Mon 19:00-20:30")
    assert plan.moved == [("ev1", at(2026, 9, 28, 19), at(2026, 9, 28, 20, 30))]


def test_move_to_a_busy_time_offers_the_nearest_free_ones(plan):
    msg = say(plan, "move SDET study to 3pm")
    assert "15:00-16:30 isn't free" in msg["text"]
    first = msg["buttons"][0][0]
    tap(plan, first[1])
    assert plan.moved and plan.moved[0][1].strftime("%H:%M") == first[0]


def test_cancel_changes_nothing(plan):
    msg = say(plan, "move SDET study to 7pm")
    assert tap(plan, msg["buttons"][0][1][1]) == "OK, nothing changed." and not plan.moved


def test_not_today_moves_the_todo(plan):
    msg = say(plan, "leetcode not today")
    assert msg["text"].startswith("Not today: Do LeetCode Daily?")
    tap(plan, msg["buttons"][0][0][1])
    assert plan.lis.state.plan_item(plan.item["id"])["status"] == "skipped"
    assert plan.lis.tasks.patched == [("DAILY", "c", {"due": "2026-09-29T00:00:00.000Z"})]


def test_busy_blocks_out_time_and_names_what_is_in_the_way(plan):
    msg = say(plan, "busy 5-7pm")
    assert msg["text"].startswith("Block out Mon 17:00-19:00 as busy?")
    text = tap(plan, msg["buttons"][0][0][1])
    assert plan.created[0][4] == "busy" and "In the way: Study SMAI last Lecture" in text


def test_exam_prep_hours(plan):
    msg = say(plan, "make midsem prep 10 hours")
    assert msg["text"] == "Plan 10 h of work in total for SDET midsem?"
    tap(plan, msg["buttons"][0][0][1])
    assert plan.lis.state.get_effort("event:mid") == 10


def test_add_at_a_time(plan):
    msg = say(plan, "add gym at 6pm for 1h")
    assert msg["text"] == "Add gym at Mon 18:00-19:00?"
    tap(plan, msg["buttons"][0][0][1])
    assert plan.lis.tasks.store and plan.created[0][1:4] == ("Work: gym", at(2026, 9, 28, 18), at(2026, 9, 28, 19))


def test_swap(plan):
    msg = say(plan, "swap SMAI and SDET study")
    tap(plan, msg["buttons"][0][0][1])
    assert plan.lis.state.block(plan.b1)["start"] == at(2026, 9, 28, 17).isoformat()
    assert plan.lis.state.block(plan.b2)["start"] == at(2026, 9, 28, 11).isoformat()


def test_other_phrasings_go_to_the_model(plan, monkeypatch):
    monkeypatch.setattr(nlcommands, "chat_json", lambda *a: json.dumps(
        {"action": "move", "task": "SDET", "when": "8pm", "task2": None, "duration": None, "hours": None}))
    msg = say(plan, "can we do the SDET thing later, around 8pm")
    assert msg["text"] == "Move Study SDET MidSem to Mon 20:00-21:30?"


def test_gpu_off_falls_back_to_examples(plan, monkeypatch):
    monkeypatch.setattr(nlcommands, "chat_json", lambda *a: (_ for _ in ()).throw(LLMUnavailable("GPU unavailable")))
    msg = say(plan, "can we do the SDET thing later, around 8pm")
    assert "These always work" in msg["text"] and "move SDET study to 7pm" in msg["text"]


def test_unknown_task_is_explained(plan):
    msg = say(plan, "move basketball to 7pm")
    assert "couldn't tell which task" in msg["text"] and msg["buttons"] is None
