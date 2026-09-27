"""You choose when: free-slot messages per task, booking, "Did you finish?", one reminder, the evening check."""
import itertools
from datetime import timedelta
from types import SimpleNamespace

import pytest

import google_writer
import planner
import slotpicker
from conftest import FakeTasks, FakeTelegram, at, event

NOW = at(2026, 9, 27, 10, 56)
CFG = {"timezone": "Asia/Kolkata", "calendars": {"college": "COL", "planner": "PLAN", "habits": "HAB"},
       "planner": {"work_window": ["08:00", "23:00"], "sleep": ["23:30", "07:00"], "block_minutes": 90,
                   "min_block_minutes": 30, "gap_minutes": 15, "max_work_hours_per_day": 6, "task_effort_hours": 1,
                   "horizon_days": 14,
                   "exam_prep_hours": {"quiz": 2, "midsem": 8, "endsem": 12, "exam": 6}},
       "morning": {"remind_after_minutes": 120, "evening_check": "21:30"}}
IDS = itertools.count(1)


def T(h, m=0):
    return NOW.replace(hour=h, minute=m)


@pytest.fixture
def day(db, monkeypatch):
    """Two to-dos and a free day from 11:00; work_context is recomputed from what's been booked."""
    tg, tasks, booked = FakeTelegram(), FakeTasks(), []
    work = [{"key": "task:a", "title": "Study SDET MidSem", "due": T(23, 59), "kind": "task", "list_id": "DAILY",
             "today_min": 120},
            {"key": "task:b", "title": "Do LeetCode Daily", "due": T(23, 59), "kind": "task", "list_id": "DAILY",
             "today_min": 30}]

    def context(cfg, state, now, cal, tasks_api, rank=False):
        free = [(max(slotpicker.slots.round_up(now), T(11)), T(23))]
        taken = [(s, e) for s, e, _ in booked]
        free = slotpicker.slots.subtract(free, slotpicker.slots.pad(taken, 15))
        items = []
        for w in work:
            got = sum((e - s).total_seconds() / 60 for s, e, k in booked if k == w["key"])
            items.append({**w, "need": max(0, w["today_min"] - got)})
        return free, items, 360 - sum((e - s).total_seconds() / 60 for s, e, _ in booked)

    monkeypatch.setattr(planner, "work_context", context)
    monkeypatch.setattr(google_writer, "create_block", lambda cal, cid, title, s, e, tz, kind, work_key=None, note=None:
                        booked.append((s, e, work_key)) or f"ev{len(booked)}")
    listener = SimpleNamespace(cfg=CFG, state=db, tg=tg, tasks=tasks, calendar=None)
    return SimpleNamespace(tg=tg, tasks=tasks, booked=booked, db=db, listener=listener, work=work)


def start_day(d, now=NOW):
    _, work_free, cap = slotpicker.prepare(CFG, d.db, now, None, None)
    slotpicker.send_waiting(CFG, d.db, now, d.tg, work_free)


def tap(d, data, message_id, now=NOW):
    action, _, rest = data.partition(":")
    slotpicker.handle(d.listener, {"id": f"t{next(IDS)}", "message": {"message_id": message_id}}, action, rest, now)


def buttons(message):
    return [b for row in message["buttons"] for b in row]


def test_slots_are_spread_over_the_day():
    opts = slotpicker.options([(T(11), T(23))], 90, T(23, 59), 15)
    assert [s.hour for s, _ in opts] == [11, 12, 17]                  # morning, afternoon, evening
    assert all(e - s == timedelta(minutes=90) for s, e in opts)
    assert slotpicker.options([(T(11), T(23))], 60, T(13), 15)[-1][1] <= T(12, 45)   # never after it's due


def test_one_message_per_task_and_nothing_booked_without_a_tap(day):
    start_day(day)
    assert len(day.tg.sent) == 2 and not day.booked
    first = day.tg.sent[0]
    assert first["text"].startswith("Study SDET MidSem\n2 h to do today") and "first 1 h 30 min" in first["text"]
    assert ("Not today", "sgn:1") in buttons(first)


def test_booking_a_slot_updates_the_others(day):
    start_day(day)
    sdet, leet = day.tg.sent
    slot = buttons(sdet)[0]                                           # 11:00-12:30
    tap(day, slot[1], sdet["id"])
    assert day.booked[0][:2] == (T(11), T(12, 30))
    assert "Booked 11:00-12:30" in day.tg.edits[sdet["id"]]["text"]
    assert "30 min" in day.tg.edits[sdet["id"]]["text"]               # the rest of its 2 h still to pick
    assert not any(b[0].startswith("11:") for b in buttons(day.tg.edits[leet["id"]]))  # taken: not offered
    tap(day, slot[1], sdet["id"])                                     # an old button for a time now taken
    assert "isn't free any more" in day.tg.edits[sdet["id"]]["text"] and len(day.booked) == 1


def test_not_today_moves_the_todo(day):
    start_day(day)
    leet = day.tg.sent[1]
    tap(day, "sgn:2", leet["id"])
    assert day.db.plan_item(2)["status"] == "skipped"
    assert day.tasks.patched == [("DAILY", "b", {"due": "2026-09-28T00:00:00.000Z"})]


def finish_slot(day):
    start_day(day)
    leet = day.tg.sent[1]
    tap(day, next(b for b in buttons(leet) if b[1].startswith("sgb"))[1], leet["id"])
    slotpicker.tick(CFG, day.db, day.booked[0][1] + timedelta(minutes=1), day.tg, lambda: (None, None))
    ask = day.tg.sent[-1]
    assert ask["text"].startswith("Did you finish Do LeetCode Daily")
    return ask


def test_done_ticks_off_the_todo(day):
    ask = finish_slot(day)
    day.db.set_effort("task:b", 0.5)
    tap(day, buttons(ask)[0][1], ask["id"])                           # Done
    assert day.db.blocks()[0]["status"] == "done"
    assert ("DAILY", "b", {"status": "completed"}) in day.tasks.patched


def test_not_done_offers_new_times(day):
    ask = finish_slot(day)
    day.booked.clear()                                                # not done: the time is needed again
    tap(day, buttons(ask)[2][1], ask["id"])
    edited = day.tg.edits[ask["id"]]
    assert edited["text"].startswith("Not done: Do LeetCode Daily") and any(b[1].startswith("sgb") for b in buttons(edited))


def test_one_reminder_then_quiet(day):
    start_day(day)
    later = NOW + timedelta(hours=2, minutes=5)
    slotpicker.tick(CFG, day.db, later, day.tg, lambda: (None, None))
    reminders = [m for m in day.tg.sent if m["text"].startswith("Still no time picked")]
    assert len(reminders) == 2
    slotpicker.tick(CFG, day.db, later + timedelta(hours=3), day.tg, lambda: (None, None))
    assert len([m for m in day.tg.sent if m["text"].startswith("Still no time picked")]) == 2


def test_evening_check_moves_unfinished_todos(day):
    start_day(day)
    slotpicker.tick(CFG, day.db, T(21, 35), day.tg, lambda: (None, None))
    evening = day.tg.sent[-1]
    assert evening["text"].startswith("Evening check") and "No time picked: Study SDET MidSem, Do LeetCode Daily" in evening["text"]
    tap(day, buttons(evening)[0][1], evening["id"], now=T(21, 36))
    assert {t for _, t, _ in day.tasks.patched} == {"a", "b"} and "Moved 2" in day.tg.edits[evening["id"]]["text"]
    slotpicker.tick(CFG, day.db, T(22, 0), day.tg, lambda: (None, None))
    assert day.tg.sent[-1] is evening                                 # once a day


def test_no_questions_while_asleep(day):
    ask_time = T(23, 45)
    day.db.block_add(None, "ev9", "task:a", "Late work", T(22, 0), T(23, 0))
    slotpicker.tick(CFG, day.db, ask_time, day.tg, lambda: (None, None))
    assert not any(m["text"].startswith("Did you finish") for m in day.tg.sent)


@pytest.mark.parametrize("title, kind", [("SDET midsem", "midsem"), ("Mid-Sem exam: OS", "midsem"), ("Quiz 2", "quiz"),
                                         ("End Semester Examination", "endsem"), ("Viva for DSA", "exam"),
                                         ("Robotics lecture", None), ("Unit testing workshop", None)])
def test_exam_kinds(title, kind):
    assert planner.exam_kind(title) == kind


def test_exam_gets_prep_before_it(monkeypatch, db):
    cfg = {**CFG, "tasklist": None, "planner": {**CFG["planner"], "default_effort_hours": 3}}
    monkeypatch.setattr(planner, "fetch_events", lambda cal, cid, s, e: [
        event("mid", at(2026, 9, 30, 15), at(2026, 9, 30, 17), "SDET midsem"),
        event("cls", at(2026, 9, 28, 11, 40), at(2026, 9, 28, 13), "EC4.401 - Robotics")])
    monkeypatch.setattr(planner.gtasks, "open_dated_tasks", lambda api, skip, before: [])
    [item] = planner.open_work(None, None, cfg, db, NOW, NOW + timedelta(days=14))
    assert item["title"] == "Prepare for SDET midsem" and item["effort_h"] == 8 and item["due"] == at(2026, 9, 30, 15)
    db.set_effort("event:mid", 0)                                     # "No prep"
    assert planner.needed_today(None, None, cfg, db, NOW, {}) == []
