"""Drop it: not doing a task at all. Always asked first; a to-do is deleted from Tasks, a deadline loses its DUE event
and task, exam prep becomes none, and its upcoming blocks go. Habits are never dropped this way (/habits)."""
from datetime import timedelta

import actions
import nlcommands
import slotpicker
from conftest import FakeCalendar, FakeTasks, event
from test_slotpicker import CFG, NOW, T, buttons, day, start_day, tap  # noqa: F401 - `day` is a fixture

CFG = {**CFG, "tasklist": "AGENT"}


def planned(work_key, start, event_id="blk1"):
    ev = event(event_id, start, start + timedelta(minutes=30), "Work: LeetCode")
    ev["extendedProperties"] = {"private": {"source": "calendar-agent-planner", "work_key": work_key}}
    return ev


def test_drop_asks_first_then_deletes_the_todo_and_its_blocks(day):
    start_day(day)
    leet = day.tg.sent[1]
    assert ("Drop it", "dx:a:i2") in buttons(leet)
    day.listener.calendar = cal = FakeCalendar([], {"PLAN": [planned("task:b", T(20))]})
    day.db.block_add(2, "blk1", "task:b", "Do LeetCode Daily", T(20), T(20, 30), calendar="PLAN")

    tap(day, "dx:a:i2", leet["id"])
    question = day.tg.edits[leet["id"]]
    assert question["text"].startswith("Drop Do LeetCode Daily for good?")
    assert day.tasks.deleted == [] and cal.deleted == []                      # nothing happens on the first tap

    tap(day, "dx:y:i2", leet["id"])
    assert day.tasks.deleted == [("DAILY", "b")]
    assert cal.deleted == [("PLAN", "blk1")]
    assert day.db.block_by_event("blk1")["status"] == "cleared"
    assert day.db.plan_item(2)["status"] == "dropped"
    assert day.tg.edits[leet["id"]]["text"].startswith("Dropped: Do LeetCode Daily.")


def test_back_shows_the_times_again_and_old_buttons_do_nothing_after_a_drop(day):
    start_day(day)
    leet = day.tg.sent[1]
    tap(day, "dx:a:i2", leet["id"])
    tap(day, "dx:n:i2", leet["id"])
    assert any(b[1].startswith("sgb:2:") for b in buttons(day.tg.edits[leet["id"]]))
    assert day.tasks.deleted == []

    day.listener.calendar = FakeCalendar([], {"PLAN": []})
    old_time = next(b for b in buttons(leet) if b[1].startswith("sgb"))[1]
    tap(day, "dx:y:i2", leet["id"])
    tap(day, old_time, leet["id"])                                            # a time tapped on the old buttons
    assert day.booked == []
    assert "dropped" in day.tg.edits[leet["id"]]["text"]
    tap(day, "dx:y:i2", leet["id"])                                           # a second Yes
    assert day.tasks.deleted == [("DAILY", "b")]


def test_habits_have_no_drop_button():
    row = {"id": 7, "title": "Gym", "kind": "habit", "minutes": 30, "due": T(21).isoformat(), "window_start": "18:00"}
    _, rows = slotpicker.render(row, 30, [(T(18), T(18, 30))])
    assert not any(cb.startswith("dx:") for r in rows for _, cb in r)


def test_not_done_with_nothing_left_today_offers_drop(day):
    start_day(day)
    leet = day.tg.sent[1]
    tap(day, next(b for b in buttons(leet) if b[1].startswith("sgb"))[1], leet["id"])
    day.work[1]["today_min"] = 0                                              # nothing more needed today
    slotpicker.tick(CFG, day.db, day.booked[0][1] + timedelta(minutes=1), day.tg, lambda: (None, None))
    ask = day.tg.sent[-1]
    tap(day, buttons(ask)[2][1], ask["id"])                                   # Not done
    assert ("Drop it for good", "dx:a:b1") in buttons(day.tg.edits[ask["id"]])
    day.listener.calendar = FakeCalendar([], {"PLAN": []})
    tap(day, "dx:a:b1", ask["id"])
    tap(day, "dx:y:b1", ask["id"])
    assert day.tasks.deleted == [("DAILY", "b")]


def test_dropping_a_deadline_deletes_it_and_an_exam_only_loses_its_prep(db):
    due = event("due1", T(22), T(22, 30), "DUE: DSA assignment")
    quiz = event("quiz1", T(15), T(16), "SMAI Quiz 2")
    cal, tasks = FakeCalendar([], {"COL": [due, quiz], "PLAN": []}), FakeTasks()
    db.record_item("k1", "deadline", "due1", "task9", "DSA assignment", str(T(22, 30)), "m1")

    assert "deadline" in actions.discard(CFG, db, cal, tasks, "event:due1", NOW)
    assert ("COL", "due1") in cal.deleted and tasks.deleted == [("AGENT", "task9")]

    actions.discard(CFG, db, cal, tasks, "event:quiz1", NOW)
    assert ("COL", "quiz1") not in cal.deleted                               # your exam is never deleted
    assert db.get_effort("event:quiz1") == 0


def test_typed_drop_for_good_is_not_the_same_as_drop_for_today():
    assert nlcommands.parse_rules("drop leetcode for good")["action"] == "discard"
    assert nlcommands.parse_rules("I'm not doing the DSA assignment at all")["task"] == "the DSA assignment"
    assert nlcommands.parse_rules("discard leetcode")["action"] == "discard"
    assert nlcommands.parse_rules("drop leetcode")["action"] == "not_today"  # plain drop stays undoable


def test_typed_drop_asks_then_applies(day, monkeypatch):
    start_day(day)
    monkeypatch.setattr(nlcommands.google_writer, "list_events", lambda *a, **k: [])
    day.listener.calendar = FakeCalendar([], {"PLAN": []})
    text, proposal = nlcommands.propose(CFG, day.db, day.listener.calendar,
                                        nlcommands.parse_rules("drop leetcode daily completely"), NOW)
    assert text.startswith("Drop Do LeetCode Daily for good?") and day.tasks.deleted == []
    reply = nlcommands.apply(day.listener, proposal, NOW)
    assert reply.startswith("Dropped: Do LeetCode Daily") and day.tasks.deleted == [("DAILY", "b")]
