"""Regression tests for the calendar-watch bugs found in the 26 Sep audit (each failed before its fix)."""
import copy
import itertools
import json
from datetime import timedelta
from types import SimpleNamespace

import pytest

import calwatch
from conftest import FakeCalendar, FakeTasks, FakeTelegram, event
from telegram_bot import TelegramError
from test_calwatch import CALENDARS, CFG as BASE_CFG, COLLEGE, NOW, OUTLOOK, TLE, h

REVIEW_CFG = copy.deepcopy(BASE_CFG)
REVIEW_CFG["calendar_watch"].update(daily_review=True, urgent_hours=24)
TT = "tt@import"
IDS = itertools.count(1)


def course(key, first, title="CS1.234 - New Course"):
    return [event(f"{key}_{w}", first + timedelta(days=7 * w), first + timedelta(days=7 * w, minutes=90), title,
                  recurringEventId=key) for w in range(2)]


def master(key, first, title="CS1.234 - New Course", rule="RRULE:FREQ=WEEKLY"):
    return {"id": key, "summary": title, "recurrence": [rule],
            "start": {"dateTime": first.isoformat()}, "end": {"dateTime": (first + timedelta(minutes=90)).isoformat()}}


@pytest.fixture
def s(db):
    cal = FakeCalendar(copy.deepcopy(CALENDARS) + [{"id": TT, "summary": "My IIIT App Timetable", "selected": True}],
                       events={OUTLOOK: [event("m1", h(6), h(7), "Project meeting")]})
    tg, tasks = FakeTelegram(), FakeTasks()
    calwatch._ENTRY_CACHE.clear()
    return SimpleNamespace(cal=cal, tg=tg, tasks=tasks, db=db, cfg=BASE_CFG,
                           listener=SimpleNamespace(cfg=BASE_CFG, state=db, calendar=cal, tasks=tasks, tg=tg))


def watch(s, now=NOW, cfg=None):
    return calwatch.Watcher(cfg or s.cfg, s.db, s.cal, s.tasks, s.tg, now).run()


def tap(s, data, message_id=500):
    action, _, rest = data.partition(":")
    cq = {"id": f"t{next(IDS)}", "data": data, "message": {"message_id": message_id}}
    (calwatch.handle_review if action in ("crv", "crva") else calwatch.handle_callback)(s.listener, cq, action, rest, NOW)


def row(s, cal_id, key):
    return s.db.watch_get(cal_id, key)


def test_undecided_series_survives_a_check_during_its_class(s):
    first = h(50)
    s.cal.evs[TT] = course("a", first)
    s.cal.masters[TT] = {"a": master("a", first)}
    watch(s, cfg=REVIEW_CFG)
    watch(s, now=first + timedelta(minutes=30), cfg=REVIEW_CFG)          # a mail check mid-class
    r = row(s, TT, "a")
    assert r["status"] == "pending" and r["review"] == "new"              # still asked, still blocks time
    assert r["start"] == s.cal.evs[TT][1]["start"]["dateTime"]            # next class that hasn't started


def test_series_expires_once_its_rule_has_ended(s):
    first = h(-24 * 20)
    s.cal.masters[TT] = {"a": master("a", first, rule="RRULE:FREQ=WEEKLY;UNTIL=20260920T000000Z")}
    s.cal.evs[TT] = course("a", h(30))
    watch(s)
    s.cal.evs[TT] = []                                                    # no more classes in the window
    watch(s)
    assert row(s, TT, "a")["status"] == "expired"


def test_double_tap_track_makes_one_copy(s):
    watch(s)
    r = row(s, OUTLOOK, "m1")
    tap(s, f"cal:t:{r['id']}", message_id=r["tg_message_id"])
    tap(s, f"cal:t:{r['id']}", message_id=r["tg_message_id"])
    assert len(s.cal.inserted) == 1 and row(s, OUTLOOK, "m1")["copy_id"] == "copy1"


def test_double_tap_deadline_makes_one_deadline(s):
    s.cal.evs[OUTLOOK] = [event("a2", h(6), h(6), "Assignment 2 submission")]
    watch(s)
    r = row(s, OUTLOOK, "a2")
    tap(s, f"cal:d:{r['id']}")
    tap(s, f"cal:d:{r['id']}")
    assert len(s.cal.inserted) == 1 and len(s.tasks.store) == 1


def test_switching_decisions_leaves_no_orphans(s):
    s.cal.evs[OUTLOOK] = [event("a2", h(6), h(7), "Assignment 2 submission")]
    watch(s)
    r = row(s, OUTLOOK, "a2")
    w = calwatch.Watcher(s.cfg, s.db, s.cal, s.tasks, s.tg, NOW)
    w.track(r)
    w.as_deadline(r)                                                      # Track, then "It's a deadline"
    assert (COLLEGE, "copy1") in s.cal.deleted
    due_id = row(s, OUTLOOK, "a2")["copy_id"]
    w.ignore(r)                                                           # then Ignore
    assert (COLLEGE, due_id) in s.cal.deleted and s.tasks.store == {}


def test_deadline_refused_for_a_cancelled_event(s):
    s.cal.evs[OUTLOOK] = [event("a2", h(6), h(7), "Assignment 2 submission", status="cancelled")]
    s.cal.masters[OUTLOOK] = {}
    watch(s)
    assert row(s, OUTLOOK, "a2") is None                                  # cancelled events aren't even listed
    s.cal.evs[OUTLOOK] = [event("a3", h(6), h(7), "Assignment 3 submission")]
    watch(s)
    s.cal.evs[OUTLOOK] = []
    w = calwatch.Watcher(s.cfg, s.db, s.cal, s.tasks, s.tg, NOW)
    assert w.as_deadline(row(s, OUTLOOK, "a3")) is None and not s.tasks.store


def test_a_feed_blip_does_not_lose_tracked_events(s):
    watch(s)
    r = row(s, OUTLOOK, "m1")
    tap(s, f"cal:t:{r['id']}", message_id=r["tg_message_id"])
    saved = s.cal.evs[OUTLOOK]
    s.cal.evs[OUTLOOK] = []
    watch(s)                                                              # one empty refresh: nothing happens
    assert row(s, OUTLOOK, "m1")["status"] == "tracked" and not s.cal.deleted
    watch(s)                                                              # still gone: copy removed
    assert row(s, OUTLOOK, "m1")["status"] == "gone" and (COLLEGE, "copy1") in s.cal.deleted
    s.cal.evs[OUTLOOK] = saved
    watch(s)                                                              # it's back: tracked again
    assert row(s, OUTLOOK, "m1")["status"] == "tracked" and row(s, OUTLOOK, "m1")["copy_id"] == "copy2"
    assert "is back on Outlook" in s.tg.sent[-1]["text"]


def test_failed_card_send_is_asked_again(s, monkeypatch):
    real = s.tg.send
    monkeypatch.setattr(s.tg, "send", lambda *a, **k: (_ for _ in ()).throw(TelegramError("sendMessage: network error")))
    watch(s)
    assert row(s, OUTLOOK, "m1")["tg_message_id"] is None
    monkeypatch.setattr(s.tg, "send", real)
    watch(s)
    assert row(s, OUTLOOK, "m1")["tg_message_id"]                         # the card went out on the next check


def test_ignore_on_a_new_slot_leaves_tracked_slots_alone(s):
    s.cal.evs[TT] = course("a", h(26), "EC4.401 - Robotics") + course("b", h(50), "EC4.401 - Robotics")
    s.cal.masters[TT] = {"a": master("a", h(26), "EC4.401 - Robotics"), "b": master("b", h(50), "EC4.401 - Robotics")}
    watch(s)
    card = row(s, TT, "a")["tg_message_id"]
    tap(s, f"cal:t:{row(s, TT, 'a')['id']}", message_id=card)
    s.cal.evs[TT] += course("c", h(74), "EC4.401 - Robotics")          # the timetable adds a third slot
    s.cal.masters[TT]["c"] = master("c", h(74), "EC4.401 - Robotics")
    watch(s)
    third = row(s, TT, "c")
    tap(s, f"cal:i:{third['id']}", message_id=third["tg_message_id"])
    assert row(s, TT, "a")["status"] == "tracked" and row(s, TT, "b")["status"] == "tracked"
    assert row(s, TT, "c")["status"] == "ignored"


def test_tracked_copies_keep_following_after_never_ask(s):
    watch(s)
    r = row(s, OUTLOOK, "m1")
    tap(s, f"cal:t:{r['id']}", message_id=r["tg_message_id"])
    s.db.set_meta(f"calpolicy:{OUTLOOK}", "ignore")
    calwatch._ENTRY_CACHE.clear()
    s.cal.evs[OUTLOOK] = []
    watch(s)
    watch(s)
    assert (COLLEGE, "copy1") in s.cal.deleted                            # the cancelled meeting's copy went


def test_undo_leaves_later_decisions_alone(s):
    s.cfg = REVIEW_CFG
    s.listener.cfg = REVIEW_CFG
    s.cal.evs[TLE] = [event(f"c{i}", h(48 + 24 * i), h(50 + 24 * i), f"Codeforces Round {i}") for i in range(3)]
    assert calwatch.daily_review(REVIEW_CFG, s.db, s.cal, s.tasks, s.tg, NOW)
    tap(s, "crva:i:1")                                                    # Ignore all new
    undo = json.loads(s.db.get_meta("review:1"))["undo"]
    tap(s, f"crv:t:{row(s, TLE, 'c1')['id']}")                           # later: Track now on one of them
    tap(s, f"calu:{undo}")
    assert row(s, TLE, "c1")["status"] == "tracked"                       # your later choice stays
    assert row(s, TLE, "c0")["status"] == "pending"


def test_old_repeating_copy_is_ended_and_replaced_by_occurrences(s):
    s.cal.evs[TT] = course("a", h(26))
    s.cal.masters[TT] = {"a": master("a", h(26))}
    watch(s)
    r = row(s, TT, "a")
    s.cal.inserted["legacy"] = (COLLEGE, {**master("a", h(-24 * 30)), "summary": "CS1.234 - New Course"})
    s.db.watch_set(r["id"], status="tracked", copy_id="legacy")          # as made by the old version
    watch(s)
    assert s.cal.patched[0][1] == "legacy" and "UNTIL=" in s.cal.patched[0][2]["recurrence"][0]
    assert row(s, TT, "a")["copy_id"] is None and len(s.db.series_copies(r["id"])) == 2


def test_review_send_failure_loses_no_news(s, monkeypatch):
    s.cal.evs[TLE] = [event("c0", h(48), h(50), "Codeforces Round 0")]
    monkeypatch.setattr(s.tg, "send", lambda *a, **k: (_ for _ in ()).throw(TelegramError("sendMessage: network error")))
    with pytest.raises(TelegramError):
        calwatch.daily_review(REVIEW_CFG, s.db, s.cal, s.tasks, s.tg, NOW)
    assert s.db.get_meta("review_seq") is None and row(s, TLE, "c0")["review"] == "new"


def test_only_one_scan_at_a_time(s):
    with calwatch.watch_lock():
        with pytest.raises(calwatch.WatchBusy):
            calwatch.daily_review(REVIEW_CFG, s.db, s.cal, s.tasks, s.tg, NOW, wait_s=0)
