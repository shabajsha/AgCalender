"""Settings, habits, heads-up, /deadlines, Undo after Add, /status toggle."""
import itertools
import time
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

import actions
import approvals
import config
import deadlines
import google_writer
import habits
import settings
import slotpicker
from conftest import TZ, FakeCalendar, FakeTasks, FakeTelegram, at, event
from state import State

IDS = itertools.count(1)
NOW = at(2026, 9, 28, 9, 0)  # a Monday
CFG = {"timezone": "Asia/Kolkata", "tasklist": "AGENT", "calendars": {"college": "COL", "planner": "PLAN", "habits": "HAB"},
       "planner": {"work_window": ["08:00", "23:00"], "sleep": ["23:30", "07:00"], "block_minutes": 90,
                   "min_block_minutes": 30, "gap_minutes": 15, "max_work_hours_per_day": 6, "default_effort_hours": 3,
                   "task_effort_hours": 1, "horizon_days": 14},
       "morning": {"heads_up_minutes": 5, "todo_tasklist": "DAILY", "todo_default_minutes": 30}}


def listener(db, cal=None, tasks=None):
    return SimpleNamespace(cfg=dict(CFG), state=db, tg=FakeTelegram(), calendar=cal, tasks=tasks or FakeTasks())


def tap(module, lis, data, message_id=500, now=NOW):
    action, _, rest = data.partition(":")
    module.handle(lis, {"id": f"t{next(IDS)}", "message": {"message_id": message_id}}, action, rest, now)


# --- settings --------------------------------------------------------------------------------------------

def test_setting_values_are_validated():
    work = settings.BY_KEY["planner.work_window"]
    assert settings.parse(work, "9am to 9pm") == ["09:00", "21:00"]
    with pytest.raises(ValueError):
        settings.parse(work, "21:00-09:00")                       # must end after it starts
    limit = settings.BY_KEY["planner.max_work_hours_per_day"]
    assert settings.parse(limit, "7") == 7.0
    with pytest.raises(ValueError):
        settings.parse(limit, "30")


def test_changed_setting_reaches_every_script():
    state = State()                                               # the real (test) state.db that load_config reads
    lis = listener(state)
    index = next(i for i, s in enumerate(settings.SETTINGS) if s.key == "planner.max_work_hours_per_day")
    tap(settings, lis, f"setv:{index}:2")                         # 6 h
    assert config.load_config()["planner"]["max_work_hours_per_day"] == 6
    tap(settings, lis, f"sett:{index}")                           # "Type a value"
    conv = state.conv(NOW)
    settings.typed(lis, conv, "7.5", NOW)
    assert config.load_config()["planner"]["max_work_hours_per_day"] == 7.5 and state.conv(NOW) is None
    tap(settings, lis, f"setr:{index}")                           # reset: back to config.yaml
    assert "planner.max_work_hours_per_day" not in state.settings()


# --- habits ----------------------------------------------------------------------------------------------

def test_new_habit_flow_and_it_is_offered_on_its_days(db):
    lis = listener(db)
    tap(habits, lis, "hbn")
    habits.typed(lis, db.conv(NOW), "Gym", NOW)
    flow = lis.tg.sent[-1]["id"]
    for data in ("hbm:60", "hbd:mon", "hbd:wed", "hbd:ok", "hbw:3", "hbs"):
        tap(habits, lis, data, message_id=flow)
    [h] = db.habits()
    assert (h["name"], h["minutes"], h["days"], h["window_start"], h["window_end"]) == ("Gym", 60, "mon,wed", "17:00", "21:00")
    [item] = habits.today_items(CFG, db, NOW, lambda key: 0)      # Monday
    assert item["key"] == f"habit:{h['id']}" and item["need"] == 60 and item["window_start"] == "17:00"
    assert habits.today_items(CFG, db, NOW + timedelta(days=1), lambda key: 0) == []   # not on Tuesday


def test_streak_counts_scheduled_days_done(db):
    hid = db.habit_add("Read", 30, "mon,tue,wed,thu,fri,sat,sun", "21:00", "23:00")
    db.db.execute("UPDATE habits SET created_at = '2026-09-01T00:00:00+00:00'")
    for days_ago in (1, 2, 3):
        start = NOW - timedelta(days=days_ago)
        b = db.block_add(None, f"e{days_ago}", f"habit:{hid}", "Read", start, start + timedelta(minutes=30))
        db.block_set(b, status="done")
    assert habits.streak(db, db.habit(hid), NOW.date()) == 3      # today not done yet: still 3


def test_habit_slots_stay_inside_the_habit_window():
    row = {"kind": "habit", "window_start": "17:00", "due": at(2026, 9, 28, 21).isoformat(), "minutes": 60, "id": 1,
           "title": "Gym"}
    chunk, opts = slotpicker._pick([(NOW, at(2026, 9, 28, 23))], row, CFG["planner"], NOW)
    assert opts and all(s >= at(2026, 9, 28, 17) and e <= at(2026, 9, 28, 21) for s, e in opts)


# --- heads-up --------------------------------------------------------------------------------------------

@pytest.fixture
def booked(db, monkeypatch):
    lis = listener(db, cal=object())
    moved, deleted = [], []
    monkeypatch.setattr(actions, "free_on", lambda cfg, state, cal, day, now, ignore_ids=frozenset(): [(NOW, at(2026, 9, 28, 23))])
    monkeypatch.setattr(google_writer, "move_event", lambda cal, cid, eid, s, e, tz: moved.append((eid, s, e)))
    monkeypatch.setattr(google_writer, "delete_event", lambda cal, cid, eid: deleted.append(eid))
    monkeypatch.setattr(slotpicker.planner, "work_context", lambda *a, **k: ([], [], 360))
    monkeypatch.setattr(actions, "sync_if_stale", lambda *a, **k: [])       # sync has its own tests
    bid = db.block_add(None, "ev1", "task:a", "Study SDET", NOW + timedelta(minutes=4), NOW + timedelta(minutes=94))
    return SimpleNamespace(lis=lis, moved=moved, deleted=deleted, bid=bid)


def test_heads_up_once_then_push(booked):
    slotpicker.heads_up(booked.lis, NOW)
    slotpicker.heads_up(booked.lis, NOW + timedelta(minutes=1))
    [msg] = booked.lis.tg.sent
    assert msg["text"].startswith("Next at 09:04: Study SDET (1 h 30 min)")
    tap(slotpicker, booked.lis, f"hu:{booked.bid}:p", message_id=msg["id"])
    assert booked.moved == [("ev1", NOW + timedelta(minutes=34), NOW + timedelta(minutes=124))]
    assert booked.lis.state.block(booked.bid)["start"] == (NOW + timedelta(minutes=34)).isoformat()


def test_heads_up_skip_frees_the_time(booked):
    tap(slotpicker, booked.lis, f"hu:{booked.bid}:k")
    assert booked.deleted == ["ev1"] and booked.lis.state.block(booked.bid)["status"] == "notdone"


def test_push_into_busy_time_offers_other_times(booked, monkeypatch):
    monkeypatch.setattr(actions, "free_on", lambda *a, **k: [(NOW + timedelta(hours=3), NOW + timedelta(hours=8))])
    tap(slotpicker, booked.lis, f"hu:{booked.bid}:p")
    edit = booked.lis.tg.edits[500]
    assert "isn't free" in edit["text"] and edit["buttons"][0][0][1].startswith(f"mvb:{booked.bid}:")
    assert not booked.moved


# --- /deadlines -----------------------------------------------------------------------------------------

@pytest.fixture
def dl(db):
    due = at(2026, 10, 2, 23, 59)
    cal = FakeCalendar([], events={"COL": [event("E1", due - timedelta(minutes=30), due, "DUE: DSA Assignment 2")]})
    tasks = FakeTasks()
    db.record_item("k", "deadline", "E1", "T1", "DSA Assignment 2", str(due), "m")
    return SimpleNamespace(cal=cal, tasks=tasks, lis=listener(db, cal, tasks), due=due)


def test_deadlines_list_and_done(dl):
    text, buttons = deadlines.list_message(CFG, dl.lis.state, dl.cal, NOW)
    assert "Fri 02 Oct 23:59  DSA Assignment 2 (3 h of work)" in text
    assert [b[0] for b in buttons[0]] == ["1 Done", "1 Effort", "1 Date", "1 Not doing"]
    tap(deadlines, dl.lis, "dl:d:E1")
    assert ("AGENT", "T1", {"status": "completed"}) in dl.tasks.patched
    assert "Done: DSA Assignment 2" in dl.lis.tg.edits[500]["text"]


def test_deadline_date_moves_event_and_task(dl):
    tap(deadlines, dl.lis, "dlm:E1:2")
    cal_patch = dl.cal.patched[-1]
    assert cal_patch[1] == "E1" and cal_patch[2]["end"]["dateTime"].startswith("2026-10-04T23:59")
    assert dl.tasks.patched[-1] == ("AGENT", "T1", {"due": "2026-10-04T00:00:00.000Z"})


def test_typed_deadline_date(dl):
    tap(deadlines, dl.lis, "dlt:E1")
    deadlines.typed(dl.lis, dl.lis.state.conv(NOW), "Monday 5pm", NOW)
    assert dl.cal.patched[-1][2]["end"]["dateTime"].startswith("2026-09-28T17:00")


def test_not_doing_asks_first(dl):
    tap(deadlines, dl.lis, "dl:x:E1")
    assert "Yes, delete it" in str(dl.lis.tg.edits[500]["buttons"]) and not dl.cal.deleted
    tap(deadlines, dl.lis, "dlx:E1")
    assert ("COL", "E1") in dl.cal.deleted and dl.lis.state.task_for_event("E1") is None


# --- bot: Undo after Add, /status toggle, new commands ---------------------------------------------------

@pytest.fixture
def bot(db, monkeypatch):
    lis = approvals.Listener.__new__(approvals.Listener)
    lis.cfg = {**CFG, "planner": {**CFG["planner"], "effort_choices_hours": [2, 4, 8, 12, 20, 30]}}
    lis.state, lis.tg, lis.tasks, lis.calendar, lis.learn_after = db, FakeTelegram(), FakeTasks(), None, 3
    lis.runs, lis._last_error_reply = [], 0.0
    monkeypatch.setattr(approvals.Listener, "_run_script", staticmethod(lambda script, *a, unit=None: lis.runs.append(unit) or True))
    monkeypatch.setattr(approvals.Listener, "_start_ingest_now", staticmethod(lambda: True))
    monkeypatch.setattr(google_writer, "create_item", lambda *a: ("EV1", "TASK1"))
    deleted = []
    monkeypatch.setattr(google_writer, "delete_event", lambda cal, cid, eid: deleted.append(eid))
    lis.deleted = deleted
    return lis


def bot_tap(bot, data):
    bot.handle({"id": f"b{next(IDS)}", "data": data, "message": {"chat": {"id": 42}, "message_id": 101}})


def say(bot, text):
    bot.handle_message({"chat": {"id": 42}, "text": text, "date": int(time.time())})


def card(title="DSA Assignment 2"):
    due = datetime.now(TZ) + timedelta(days=3)
    return {"type": "deadline", "title": title, "start": due, "end": due, "all_day": False, "due": due, "course": None,
            "location": None, "description": None, "recurrence": None}


def test_undo_after_add(bot):
    approvals.ask(bot.tg, bot.state, "k1", card(), {"id": "m", "subject": "s"}, "prof@x")
    bot_tap(bot, "add:1")
    assert bot.tg.edits[101]["buttons"][-1] == [("Undo (10 min)", "addundo:1")]
    bot_tap(bot, "addundo:1")
    assert bot.deleted == ["EV1"] and bot.state.get_pending(1)["status"] == "pending" and not bot.state.item_exists("k1")
    assert bot.tg.edits[101]["buttons"] == [("Add", "add:1"), ("Skip", "skip:1")]


def test_status_has_a_pause_toggle(bot):
    say(bot, "Status")
    assert bot.tg.sent[-1]["buttons"] == [[("Pause mail reading", "st:p")]]
    bot_tap(bot, "st:p")
    assert bot.state.paused_since() and bot.tg.edits[101]["buttons"] == [[("Resume mail reading", "st:r")]]


def test_button_bar_labels_reach_their_commands(bot, monkeypatch):
    monkeypatch.setattr(approvals.deadlines, "list_message", lambda *a: ("deadlines", None))
    say(bot, "Deadlines")
    say(bot, "Habits")
    say(bot, "Settings")
    texts = [m["text"] for m in bot.tg.sent[-3:]]
    assert texts[0] == "deadlines" and texts[1].startswith("No habits yet") and texts[2].startswith("Settings")


def test_typed_value_goes_to_the_waiting_setting(bot, monkeypatch):
    monkeypatch.setattr(settings, "handle", settings.handle)
    index = next(i for i, s in enumerate(settings.SETTINGS) if s.key == "morning.evening_check")
    bot_tap(bot, f"sett:{index}")
    say(bot, "22:15")
    assert bot.state.settings()["morning.evening_check"] == "22:15"
