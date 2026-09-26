from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

import calwatch
from conftest import TZ, FakeCalendar, FakeTasks, FakeTelegram, event

NOW = datetime(2026, 9, 28, 9, 0, tzinfo=TZ)
TLE, OUTLOOK, MOODLE, COLLEGE = "tle@group", "outlook@import", "moodle@import", "college@group"
CFG = {"timezone": "Asia/Kolkata", "tasklist": "AGENT",
       "calendars": {"college": COLLEGE, "planner": "plan@group", "habits": "hab@group"},
       "planner": {"effort_choices_hours": [2, 4, 8, 12, 20, 30]},
       "calendar_watch": {"enabled": True, "days_ahead": 14, "bulk_after": 5, "default": "ask",
                          "calendars": {TLE: "ask", OUTLOOK: "ask", MOODLE: "ask", "primary": "show"}}}
CALENDARS = [
    {"id": "me@gmail.com", "summary": "me@gmail.com", "primary": True, "selected": True},
    {"id": TLE, "summary": "TLE Contest Tracker", "selected": True},
    {"id": OUTLOOK, "summary": "Outlook", "selected": True},
    {"id": MOODLE, "summary": "https://courses.iiit.ac.in/calendar/export_execute.php?userid=1&authtoken=SECRET",
     "selected": True},
    {"id": COLLEGE, "summary": "College", "selected": True},
]


def h(hours, minutes=0):
    return NOW + timedelta(hours=hours, minutes=minutes)


def contests(n):
    return [event(f"c{i}", h(24 * (i + 1)), h(24 * (i + 1) + 2), f"Codeforces Round {900 + i}") for i in range(n)]


@pytest.fixture
def setup(db):
    cal = FakeCalendar(CALENDARS, events={TLE: contests(7), OUTLOOK: [
        event("m1", h(6), h(7), "Project meeting"),
        event("s1_a", h(26), h(27), "DSA tutorial", recurringEventId="s1"),
        event("s1_b", h(26 + 168), h(27 + 168), "DSA tutorial", recurringEventId="s1")]},
        masters={OUTLOOK: {"s1": {"id": "s1", "summary": "DSA tutorial", "start": {"dateTime": h(26).isoformat()},
                                   "end": {"dateTime": h(27).isoformat()}, "recurrence": ["RRULE:FREQ=WEEKLY;BYDAY=TU"]}}})
    tg, tasks = FakeTelegram(), FakeTasks()
    listener = SimpleNamespace(cfg=CFG, state=db, calendar=cal, tasks=tasks, tg=tg)
    return SimpleNamespace(cal=cal, tg=tg, tasks=tasks, db=db, listener=listener)


def watch(s, now=NOW):
    return calwatch.Watcher(CFG, s.db, s.cal, s.tasks, s.tg, now).run()


def tap(s, data, message_id=500):
    action, _, rest = data.partition(":")
    calwatch.handle_callback(s.listener, {"id": "cb", "data": data, "message": {"message_id": message_id}}, action, rest, NOW)


def row_for(s, cal_id, key):
    return s.db.watch_get(cal_id, key)


def test_moodle_token_never_shown():
    assert calwatch.label(CALENDARS[3]) == "courses.iiit.ac.in"
    assert calwatch.label(CALENDARS[0]) == "your calendar"


def test_first_run_summary_card_and_single_cards(setup):
    stats = watch(setup)
    texts = [m["text"] for m in setup.tg.sent]
    assert texts[0].startswith("TLE Contest Tracker: 7 new events")          # > bulk_after -> one card
    assert setup.tg.sent[0]["buttons"][0][0] == ("Track all", "calb:t:1")
    outlook = [t for t in texts if t.startswith("New on Outlook")]
    assert len(outlook) == 2                                                  # the series is asked about once
    assert any("Repeats weekly on Tue" in t for t in outlook)
    assert stats["new"] == 9 and setup.db.watch_statuses()[(OUTLOOK, "s1")] == "pending"
    assert all("SECRET" not in t for t in texts)


def test_pending_blocks_time_until_ignored():
    assert calwatch.counts_as_busy("ask", "pending") and calwatch.counts_as_busy("ask", None)
    assert not calwatch.counts_as_busy("ask", "ignored") and not calwatch.counts_as_busy("ignore", None)
    assert calwatch.shown_today("ask", "pending") and not calwatch.shown_today("ask", "tracked")


def test_track_copies_into_college_and_follows_changes(setup):
    watch(setup)
    row = row_for(setup, OUTLOOK, "m1")
    tap(setup, f"cal:t:{row['id']}")
    cal_id, body = setup.cal.inserted["copy1"]
    assert cal_id == COLLEGE and body["summary"] == "Project meeting"
    assert body["extendedProperties"]["private"]["mirror_of"] == f"{OUTLOOK}|m1"
    assert setup.tg.edits[500]["buttons"] == [[("Untrack", f"cal:u:{row['id']}")]]
    # the original moves: the copy is patched and you're told
    setup.cal.evs[OUTLOOK][0] = event("m1", h(8), h(9), "Project meeting")
    watch(setup)
    assert setup.cal.patched and setup.cal.patched[-1][1] == "copy1"
    assert "The College copy was updated" in setup.tg.sent[-1]["text"]
    # the original is cancelled: the copy is removed and you're told
    setup.cal.evs[OUTLOOK] = setup.cal.evs[OUTLOOK][1:]
    watch(setup)
    assert (COLLEGE, "copy1") in setup.cal.deleted
    assert "was cancelled on Outlook" in setup.tg.sent[-1]["text"]
    assert row_for(setup, OUTLOOK, "m1")["status"] == "gone"


def test_series_copy_keeps_its_recurrence(setup):
    watch(setup)
    tap(setup, f"cal:t:{row_for(setup, OUTLOOK, 's1')['id']}")
    _, body = setup.cal.inserted["copy1"]
    assert body["recurrence"] == ["RRULE:FREQ=WEEKLY;BYDAY=TU"]


def test_ignore_and_untrack(setup):
    watch(setup)
    row = row_for(setup, OUTLOOK, "m1")
    tap(setup, f"cal:t:{row['id']}")
    tap(setup, f"cal:u:{row['id']}")
    assert (COLLEGE, "copy1") in setup.cal.deleted and row_for(setup, OUTLOOK, "m1")["status"] == "ignored"


def test_bulk_track_all(setup):
    watch(setup)
    tap(setup, "calb:t:1")
    assert len(setup.cal.inserted) == 7
    assert all(s == "tracked" for (c, _), s in setup.db.watch_statuses().items() if c == TLE)


def test_never_ask_this_calendar(setup):
    watch(setup)
    tap(setup, "calb:N:1")
    assert setup.db.get_meta(f"calpolicy:{TLE}") is None                     # asks first
    assert setup.tg.edits[500]["buttons"] == [[("Yes, never ask", "calc:N:b1"), ("Cancel", "calc:x:b1")]]
    tap(setup, "calc:N:b1")
    assert setup.db.get_meta(f"calpolicy:{TLE}") == "ignore"
    assert all(s == "ignored" for (c, _), s in setup.db.watch_statuses().items() if c == TLE)
    setup.cal.evs[TLE].append(event("c99", h(200), h(202), "Codeforces Round 999"))
    before = len(setup.tg.sent)
    watch(setup)
    assert len(setup.tg.sent) == before                                       # new contests: silence


def test_always_track_calendar_copies_future_events(setup):
    watch(setup)
    row_id = row_for(setup, OUTLOOK, "m1")["id"]
    tap(setup, f"cal:A:{row_id}")
    tap(setup, f"calc:A:r{row_id}")
    assert setup.db.get_meta(f"calpolicy:{OUTLOOK}") == "copy"
    setup.cal.evs[OUTLOOK].append(event("m2", h(30), h(31), "Faculty meeting"))
    watch(setup)
    assert any(body["summary"] == "Faculty meeting" for _, body in setup.cal.inserted.values())
    assert "Copied 1 new event(s) from Outlook" in setup.tg.sent[-1]["text"]


def test_moodle_event_as_deadline(setup):
    setup.cal.evs[MOODLE] = [event("a2", h(60), h(60), "Assignment 2 is due")]   # zero length, like Moodle
    watch(setup)
    card = next(m for m in setup.tg.sent if "Assignment 2 is due" in m["text"])
    assert card["text"].startswith("New on courses.iiit.ac.in")
    row = row_for(setup, MOODLE, "a2")
    assert ("It's a deadline", f"cal:d:{row['id']}") in card["buttons"][1]
    tap(setup, f"cal:d:{row['id']}")
    due_event = next(body for _, body in setup.cal.inserted.values() if body["summary"].startswith("DUE: "))
    assert due_event["end"]["dateTime"].startswith(h(60).strftime("%Y-%m-%dT%H:%M"))
    assert setup.tasks.store                                                  # and a task
    tap(setup, f"cale:{row['id']}:20")
    assert setup.db.get_effort(f"event:{row_for(setup, MOODLE, 'a2')['copy_id']}") == 20


def test_event_already_added_from_email_is_not_asked_again(setup):
    from state import dedupe_key
    setup.db.record_item(dedupe_key({"title": "Project meeting", "start": h(6)}), "meeting", "EV", None,
                         "Project meeting", str(h(6)), "gmail-1")
    watch(setup)
    assert row_for(setup, OUTLOOK, "m1")["status"] == "linked"
    assert not any(m["text"].startswith("New on Outlook\nProject meeting") for m in setup.tg.sent)


def test_unanswered_card_expires_after_the_event(setup):
    watch(setup)
    row = row_for(setup, OUTLOOK, "m1")
    setup.cal.evs[OUTLOOK] = setup.cal.evs[OUTLOOK][1:]
    watch(setup, now=h(8))
    assert row_for(setup, OUTLOOK, "m1")["status"] == "expired"
    assert "has passed" in setup.tg.edits[row["tg_message_id"]]["text"]


def test_calendars_menu_cycles_policy(setup):
    calwatch.show_menu(setup.listener, NOW)
    menu = setup.tg.sent[-1]
    assert "courses.iiit.ac.in" in menu["text"] and "SECRET" not in menu["text"]
    first = menu["buttons"][0][0][1]
    tap(setup, first)
    assert "copy" in setup.tg.edits[500]["text"]


def test_two_weekly_slots_of_a_course_are_one_question(setup):
    tt = "tt@import"
    setup.cal.calendars.append({"id": tt, "summary": "My IIIT App Timetable", "selected": True})
    setup.cal.evs[tt] = [event("a_1", h(25), h(26), "EC4.401 - Robotics", recurringEventId="a"),
                         event("b_1", h(49), h(50), "EC4.401 - Robotics", recurringEventId="b")]
    setup.cal.masters[tt] = {k: {"id": k, "summary": "EC4.401 - Robotics", "start": {"dateTime": s.isoformat()},
                                 "end": {"dateTime": (s + timedelta(hours=1)).isoformat()},
                                 "recurrence": ["RRULE:FREQ=WEEKLY;UNTIL=20261130T000000Z"]}
                             for k, s in (("a", h(25)), ("b", h(49)))}
    watch(setup)
    cards = [m for m in setup.tg.sent if m["text"].startswith("New on My IIIT App Timetable")]
    assert len(cards) == 1
    assert "Repeats weekly on Tue, Wed until 30 Nov 2026 (2 weekly slots)" in cards[0]["text"]
    tap(setup, cards[0]["buttons"][0][0][1], message_id=cards[0]["id"])
    copies = [body for cid, body in setup.cal.inserted.values() if body["summary"] == "EC4.401 - Robotics"]
    assert len(copies) == 2 and all(b["recurrence"] for b in copies)


def test_undo_never_ask_restores_everything(setup):
    watch(setup)
    rows = [row_for(setup, OUTLOOK, k) for k in ("m1", "s1")]
    tap(setup, f"cal:N:{rows[0]['id']}", message_id=rows[0]["tg_message_id"])
    tap(setup, f"calc:N:r{rows[0]['id']}", message_id=rows[0]["tg_message_id"])
    assert all(row_for(setup, OUTLOOK, k)["status"] == "ignored" for k in ("m1", "s1"))
    undo = setup.tg.edits[rows[0]["tg_message_id"]]["buttons"][0][0][1]
    assert undo.startswith("calu:")
    tap(setup, undo, message_id=rows[0]["tg_message_id"])
    assert setup.db.get_meta(f"calpolicy:{OUTLOOK}") == ""                   # back to config.yaml ("ask")
    assert all(row_for(setup, OUTLOOK, k)["status"] == "pending" for k in ("m1", "s1"))
    for r in rows:                                                            # cards are live again
        assert setup.tg.edits[r["tg_message_id"]]["buttons"][0] == [("Track", f"cal:t:{r['id']}"), ("Ignore", f"cal:i:{r['id']}")]
    tap(setup, undo)
    assert setup.tg.edits[500]["text"] == "Already undone."


def test_cancel_leaves_the_card_as_it_was(setup):
    watch(setup)
    row = row_for(setup, OUTLOOK, "m1")
    tap(setup, f"cal:N:{row['id']}", message_id=row["tg_message_id"])
    tap(setup, f"calc:x:r{row['id']}", message_id=row["tg_message_id"])
    assert setup.db.get_meta(f"calpolicy:{OUTLOOK}") is None
    assert setup.tg.edits[row["tg_message_id"]]["buttons"][0][0] == ("Track", f"cal:t:{row['id']}")


def test_undo_track_all_removes_the_copies(setup):
    watch(setup)
    tap(setup, "calb:t:1")
    assert len(setup.cal.inserted) == 7
    tap(setup, setup.tg.edits[500]["buttons"][0][0][1])
    assert len(setup.cal.deleted) == 7
    assert all(s == "pending" for (c, _), s in setup.db.watch_statuses().items() if c == TLE)
    assert setup.tg.edits[setup.db.watch_rows(cal_id=TLE)[0]["tg_message_id"]]["text"].startswith("TLE Contest Tracker: 7 new")


def test_undo_deadline_removes_event_and_task(setup):
    setup.cal.evs[MOODLE] = [event("a2", h(60), h(60), "Assignment 2 is due")]
    watch(setup)
    row = row_for(setup, MOODLE, "a2")
    tap(setup, f"cal:d:{row['id']}")
    due_id = row_for(setup, MOODLE, "a2")["copy_id"]
    assert setup.tasks.store and setup.db.task_for_event(due_id)
    tap(setup, setup.tg.edits[500]["buttons"][-1][0][1])                     # the Undo row
    assert (COLLEGE, due_id) in setup.cal.deleted and setup.tasks.store == {}
    assert row_for(setup, MOODLE, "a2")["status"] == "pending" and setup.db.task_for_event(due_id) is None
