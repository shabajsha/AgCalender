"""Regression tests for the review of the 27 Sep features (each reproduced the bug before its fix)."""
import itertools
import time
from datetime import timedelta
from types import SimpleNamespace

import pytest

import actions
import approvals
import google_writer
import nlcommands
import planner
import settings
import slotpicker
from conftest import FakeTasks, FakeTelegram, at, event

NOW = at(2026, 9, 28, 10, 5)
IDS = itertools.count(1)
CFG = {"timezone": "Asia/Kolkata", "tasklist": "AGENT", "ollama": {},
       "calendars": {"college": "COL", "planner": "PLAN", "habits": "HAB"},
       "planner": {"work_window": ["08:00", "23:00"], "sleep": ["23:30", "07:00"], "block_minutes": 90,
                   "min_block_minutes": 30, "gap_minutes": 15, "max_work_hours_per_day": 6, "default_effort_hours": 3,
                   "task_effort_hours": 1, "horizon_days": 14, "effort_choices_hours": [2, 4]},
       "morning": {"todo_tasklist": "DAILY", "todo_default_minutes": 30, "heads_up_minutes": 5}}


@pytest.fixture
def bot(db, monkeypatch):
    lis = approvals.Listener.__new__(approvals.Listener)
    lis.cfg, lis.state, lis.tg, lis.tasks, lis.calendar, lis.learn_after = CFG, db, FakeTelegram(), FakeTasks(), None, 3
    lis.runs, lis._last_error_reply = [], 0.0
    return lis


def tap(bot, data):
    bot.handle({"id": f"r{next(IDS)}", "data": data, "message": {"chat": {"id": 42}, "message_id": 101}})


def say(bot, text):
    bot.handle_message({"chat": {"id": 42}, "text": text, "date": int(time.time())})


# 1 --------------------------------------------------------------------------------------------------------

def test_tapping_another_button_ends_the_type_a_value_prompt(bot, monkeypatch):
    typed = []
    monkeypatch.setattr(nlcommands, "handle_text", lambda lis, text, now: typed.append(text) or True)
    work = next(i for i, s in enumerate(settings.SETTINGS) if s.key == "planner.work_window")
    tap(bot, f"sett:{work}")                                   # "Type a value"...
    tap(bot, f"setv:{work}:1")                                 # ...then a button instead
    say(bot, "busy 2-5pm")
    assert typed == ["busy 2-5pm"] and bot.state.settings()["planner.work_window"] == ["09:00", "21:00"]


def test_a_command_ends_the_prompt_too(bot):
    tap(bot, "sett:0")
    say(bot, "/status")
    assert bot.state.conv(approvals.datetime.now(approvals.ZoneInfo("Asia/Kolkata"))) is None


# 2 --------------------------------------------------------------------------------------------------------

def test_a_booked_task_stays_booked_while_its_block_runs(db, monkeypatch):
    due = NOW + timedelta(days=3, hours=13)                    # 6 h over 4 days -> 90 min today
    block = event("b1", at(2026, 9, 28, 10), at(2026, 9, 28, 11, 30), "Work: Report",
                  extendedProperties={"private": {"work_key": "event:D"}})
    monkeypatch.setattr(planner, "free_today", lambda *a, **k: [])
    monkeypatch.setattr(google_writer, "list_blocks", lambda cal, cid, s, e: [block] if cid == "PLAN" else [])
    monkeypatch.setattr(planner, "open_work", lambda *a: [{"key": "event:D", "title": "Report", "due": due, "effort_h": 6,
                                                             "kind": "deadline"}])
    for now in (NOW, at(2026, 9, 28, 11, 40)):                 # during the block, and after it
        _, items, _ = planner.work_context(CFG, db, now, None, None)
        assert [it["need"] for it in items if it["key"] == "event:D"] in ([], [0])


# 3 --------------------------------------------------------------------------------------------------------

def test_new_times_after_not_done_restart_the_reminder_clock(db, monkeypatch):
    item = {"key": "task:a", "title": "Lab", "due": at(2026, 9, 28, 23, 59), "kind": "task", "need": 60, "list_id": "DAILY"}
    monkeypatch.setattr(planner, "work_context", lambda *a, **k: ([(NOW, at(2026, 9, 28, 20))], [item], 360))
    monkeypatch.setattr(actions, "sync_if_stale", lambda *a, **k: [])
    lis = SimpleNamespace(cfg=CFG, state=db, tg=FakeTelegram(), calendar=None, tasks=FakeTasks())
    block = db.block(db.block_add(None, "e1", "task:a", "Lab", at(2026, 9, 28, 9), at(2026, 9, 28, 10)))
    slotpicker._reoffer(lis, 55, block, NOW, "Not done")
    row = db.plan_items(NOW.date())[0]
    assert row["sent_at"] == NOW.isoformat() and row["reminded"] == 0
    slotpicker.tick(CFG, db, NOW + timedelta(minutes=15), lis.tg, lambda: (None, None))
    assert not any(m["text"].startswith("Still no time picked") for m in lis.tg.sent)


# 4 --------------------------------------------------------------------------------------------------------

def test_clearing_the_plan_keeps_busy_time(monkeypatch):
    now = at(2026, 9, 28, 9)
    blocks = [event("busy", at(2026, 9, 28, 14), at(2026, 9, 28, 17), "Busy",
                    extendedProperties={"private": {"kind": "busy", "plan_date": "2026-09-28"}}),
              event("work", at(2026, 9, 28, 18), at(2026, 9, 28, 19), "Work: Lab",
                    extendedProperties={"private": {"kind": "work", "plan_date": "2026-09-27"}})]   # moved here
    deleted = []
    monkeypatch.setattr(planner, "get_credentials", lambda: None)
    monkeypatch.setattr(planner, "build", lambda *a, **k: None)
    monkeypatch.setattr(google_writer, "list_blocks", lambda cal, cid, s, e: blocks if cid == "PLAN" else [])
    monkeypatch.setattr(google_writer, "delete_event", lambda cal, cid, eid: deleted.append(eid))
    assert planner.clear_day(CFG, now.date(), now) == ["work"] and deleted == ["work"]


# 5, 6, 7 -------------------------------------------------------------------------------------------------

@pytest.fixture
def blocks(db, monkeypatch):
    moved, deleted = [], []
    monkeypatch.setattr(actions, "free_on", lambda *a, **k: [(NOW, at(2026, 9, 28, 23))])
    monkeypatch.setattr(actions, "sync_if_stale", lambda *a, **k: [])
    monkeypatch.setattr(google_writer, "move_event", lambda cal, cid, eid, s, e, tz: moved.append(eid))
    monkeypatch.setattr(google_writer, "delete_event", lambda cal, cid, eid: deleted.append(eid))
    monkeypatch.setattr(google_writer, "list_events", lambda *a: [])
    return SimpleNamespace(moved=moved, deleted=deleted)


def test_an_answered_block_cannot_be_moved_or_skipped_by_an_old_button(db, blocks):
    b = db.block(db.block_add(None, "e1", "task:a", "Study SDET A2", at(2026, 9, 28, 8), at(2026, 9, 28, 9)))
    db.block_set(b["id"], status="done")
    ok, text, _ = actions.move_block(CFG, db, None, b, at(2026, 9, 28, 19), at(2026, 9, 28, 20), NOW)
    assert not ok and "already marked done" in text
    assert actions.skip_block(CFG, db, None, b) is False
    assert not blocks.moved and not blocks.deleted and db.block(b["id"])["status"] == "done"


def test_an_old_yes_does_nothing(db, blocks):
    b = db.block_add(None, "e1", "task:a", "Study SDET MidSem", at(2026, 9, 28, 11), at(2026, 9, 28, 12))
    lis = SimpleNamespace(cfg=CFG, state=db, tg=FakeTelegram(), calendar=None, tasks=FakeTasks())
    assert nlcommands.handle_text(lis, "move SDET study to 7pm", NOW)
    msg = lis.tg.sent[-1]
    nlcommands.handle(lis, {"id": "x", "message": {"message_id": msg["id"]}}, msg["buttons"][0][0][1].partition(":")[2],
                      NOW + timedelta(hours=2))
    assert "a while ago" in lis.tg.edits[msg["id"]]["text"] and not blocks.moved
    assert db.block(b)["start"] == at(2026, 9, 28, 11).isoformat()


def test_swapping_different_lengths_never_overlaps(db, blocks):
    a = db.block_add(None, "ea", "task:a", "SMAI", at(2026, 9, 28, 11), at(2026, 9, 28, 11, 30))
    b = db.block_add(None, "eb", "task:b", "SDET A2", at(2026, 9, 28, 11, 45), at(2026, 9, 28, 13, 45))
    lis = SimpleNamespace(cfg=CFG, state=db, tg=FakeTelegram(), calendar=None, tasks=FakeTasks())
    text = nlcommands.apply(lis, {"op": "swap", "a": a, "b": b}, NOW)
    assert "Nothing changed" in text and not blocks.moved


def test_no_yes_button_when_nothing_is_free(db, blocks, monkeypatch):
    monkeypatch.setattr(actions, "free_on", lambda *a, **k: [])
    lis = SimpleNamespace(cfg=CFG, state=db, tg=FakeTelegram(), calendar=None, tasks=FakeTasks())
    assert nlcommands.handle_text(lis, "add gym at 6pm for 1h", NOW)
    msg = lis.tg.sent[-1]
    assert "isn't free" in msg["text"] and msg["buttons"] is None and not lis.tasks.store


# 9 --------------------------------------------------------------------------------------------------------

def test_recording_the_same_event_twice_keeps_one_row(db):
    first = db.block_add(None, "e1", None, "Lab", NOW, NOW + timedelta(hours=1))
    again = db.block_add(7, "e1", "task:a", "Lab", NOW, NOW + timedelta(hours=1), calendar="PLAN")
    assert first == again and len(db.blocks()) == 1 and db.block(first)["work_key"] == "task:a"
