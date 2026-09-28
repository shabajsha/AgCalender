"""Woke up late / going to bed late / up later tomorrow: one day's own times."""
import copy
import itertools
from datetime import datetime, timedelta
from types import SimpleNamespace

import config
import daytimes
import morning
import nlcommands
import slotpicker
import slots
from conftest import TZ, FakeTelegram, at
from state import State

DAY = at(2026, 9, 28).date()
CFG = {"timezone": "Asia/Kolkata",
       "planner": {"plan_after": "06:45", "sleep": ["23:30", "07:00"], "work_window": ["08:00", "23:00"]},
       "morning": {"checkin": True, "wait_minutes": 45, "todo_default_minutes": 30, "latest_checkin": "18:00"},
       "digest": {"channels": ["telegram"]}}
IDS = itertools.count(1)


def cfg_for(db, day=DAY):
    return daytimes.apply(copy.deepcopy(CFG), db.get_meta, day)


def test_usual_times_when_nothing_is_set(db):
    pc = cfg_for(db)["planner"]
    assert pc["plan_after"] == "06:45" and pc["work_window"] == ["08:00", "23:00"]
    assert daytimes.sleep_for(pc, DAY, TZ, slots.sleep_intervals) == slots.sleep_intervals(DAY, ["23:30", "07:00"], TZ)


def test_going_to_bed_at_2am(db):
    daytimes.set_time(db, DAY, "sleep", "02:00")
    pc = cfg_for(db)["planner"]
    assert pc["work_window"] == ["08:00", "23:59"]                     # slots can run to midnight
    tonight = daytimes.sleep_for(pc, DAY, TZ, slots.sleep_intervals)[1]
    assert tonight[0] == at(2026, 9, 29, 2) and tonight[1] == at(2026, 9, 29, 7)
    assert not slotpicker._asleep(cfg_for(db), at(2026, 9, 28, 23, 45))   # still awake: heads-ups continue


def test_early_night(db):
    daytimes.set_time(db, DAY, "sleep", "22:00")
    pc = cfg_for(db)["planner"]
    assert pc["work_window"] == ["08:00", "21:30"] and slotpicker._asleep(cfg_for(db), at(2026, 9, 28, 22, 30))


def test_woke_up_late(db):
    daytimes.set_time(db, DAY, "wake", "10:00")
    cfg = cfg_for(db)
    assert cfg["planner"]["plan_after"] == "10:00" and cfg["planner"]["work_window"][0] == "10:30"
    assert slotpicker._asleep(cfg, at(2026, 9, 28, 9, 0))              # no pings before you're up


def test_up_later_tomorrow_ends_tonights_sleep_later(db):
    daytimes.set_time(db, DAY + timedelta(days=1), "wake", "09:00")
    tonight = daytimes.sleep_for(cfg_for(db)["planner"], DAY, TZ, slots.sleep_intervals)[1]
    assert tonight[1] == at(2026, 9, 29, 9)


def test_check_in_waits_for_a_late_wake_up(db, monkeypatch):
    sent = []
    monkeypatch.setattr(morning, "send_checkin", lambda cfg, state, now, late: sent.append(now) or
                        state.set_meta(morning.CHECKIN_SENT, now.isoformat()))
    monkeypatch.setattr(morning, "followups", lambda *a: None)
    monkeypatch.setattr(morning, "maybe_backup", lambda *a: None)
    daytimes.set_time(db, DAY, "wake", "10:00")
    morning.tick(cfg_for(db), db, at(2026, 9, 28, 7, 0))
    assert not sent
    morning.tick(cfg_for(db), db, at(2026, 9, 28, 10, 0))
    assert sent == [at(2026, 9, 28, 10, 0)]


def test_load_config_uses_todays_times():
    state = State()                                                    # the (test) state.db load_config reads
    today = datetime.now(TZ).date()
    daytimes.set_time(state, today, "sleep", "01:30")
    assert config.load_config()["planner"]["work_window"][1] == "23:59"


def listener(db):
    lis = SimpleNamespace(cfg=copy.deepcopy(CFG), state=db, tg=FakeTelegram(), runs=[])
    lis._run_script = lambda script, *a, unit=None: lis.runs.append((script, a)) or True
    return lis


def test_typing_just_woke_up_starts_the_morning(db, monkeypatch):
    monkeypatch.setattr(config, "load_config", lambda: copy.deepcopy(CFG))
    lis = listener(db)
    now = at(2026, 9, 28, 10, 7)
    assert nlcommands.handle_text(lis, "just woke up", now)
    assert daytimes.get(db.get_meta, DAY)["wake"] == "10:07" and lis.runs == [("morning.py", ("--tick",))]
    assert lis.tg.sent[-1]["buttons"] == [[("Use the usual time", "dtc:w:2026-09-28")]]


def test_woke_up_after_the_morning_already_ran_gives_fresh_slots(db, monkeypatch):
    monkeypatch.setattr(config, "load_config", lambda: copy.deepcopy(CFG))
    db.set_meta(morning.MORNING_SENT, DAY.isoformat())
    lis = listener(db)
    nlcommands.handle_text(lis, "woke up at 11", at(2026, 9, 28, 11, 20))
    assert daytimes.get(db.get_meta, DAY)["wake"] == "11:00" and lis.runs == [("planner.py", ())]


def test_bedtime_phrases(db, monkeypatch):
    monkeypatch.setattr(config, "load_config", lambda: copy.deepcopy(CFG))
    lis = listener(db)
    nlcommands.handle_text(lis, "sleeping at 2am", at(2026, 9, 28, 20))
    assert daytimes.get(db.get_meta, DAY)["sleep"] == "02:00"
    nlcommands.handle_text(lis, "bed at 11 tonight", at(2026, 9, 28, 20))
    assert daytimes.get(db.get_meta, DAY)["sleep"] == "23:00"
    nlcommands.handle_text(lis, "going to bed late", at(2026, 9, 28, 20))
    assert lis.tg.sent[-1]["buttons"][0][0] == ("Bed 23:30", "dt:s:2026-09-28:2330")   # asks with buttons
    nlcommands.handle_text(lis, "up at 9 tomorrow", at(2026, 9, 28, 20))
    assert daytimes.get(db.get_meta, DAY + timedelta(days=1))["wake"] == "09:00"


def test_evening_check_buttons_set_tonight_and_tomorrow(db, monkeypatch):
    monkeypatch.setattr(config, "load_config", lambda: copy.deepcopy(CFG))
    rows = slotpicker.day_time_buttons(DAY)
    lis = listener(db)
    for label, data in (rows[0][2], rows[1][2]):                       # Bed 01:30, Up 09:00
        action, _, rest = data.partition(":")
        slotpicker.handle(lis, {"id": f"d{next(IDS)}", "message": {"message_id": 5}}, action, rest, at(2026, 9, 28, 21, 30))
    assert daytimes.get(db.get_meta, DAY)["sleep"] == "01:30"
    assert daytimes.get(db.get_meta, DAY + timedelta(days=1))["wake"] == "09:00"
    slotpicker.handle(lis, {"id": "x", "message": {"message_id": 5}}, "dtc", "s:2026-09-28", at(2026, 9, 28, 21, 31))
    assert "sleep" not in daytimes.get(db.get_meta, DAY)
