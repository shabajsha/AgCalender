"""The daily calendar review: non-urgent findings wait for one message; changes are asked about again."""
import copy
from types import SimpleNamespace

import pytest

import calwatch
from conftest import FakeCalendar, FakeTasks, FakeTelegram, event
from test_calwatch import CALENDARS, CFG as BASE_CFG, COLLEGE, NOW, OUTLOOK, TLE, contests, h

CFG = copy.deepcopy(BASE_CFG)
CFG["calendar_watch"].update(daily_review=True, urgent_hours=24)


@pytest.fixture
def s(db):
    cal = FakeCalendar(copy.deepcopy(CALENDARS), events={TLE: contests(7), OUTLOOK: [
        event("m1", h(6), h(7), "Project meeting"),                                    # in 6 h: urgent
        event("s1_a", h(26), h(27), "DSA tutorial", recurringEventId="s1"),
        event("s1_b", h(26 + 168), h(27 + 168), "DSA tutorial", recurringEventId="s1")]},
        masters={OUTLOOK: {"s1": {"id": "s1", "summary": "DSA tutorial", "start": {"dateTime": h(26).isoformat()},
                                   "end": {"dateTime": h(27).isoformat()}, "recurrence": ["RRULE:FREQ=WEEKLY;BYDAY=TU"]}}})
    tg, tasks = FakeTelegram(), FakeTasks()
    return SimpleNamespace(cal=cal, tg=tg, tasks=tasks, db=db,
                           listener=SimpleNamespace(cfg=CFG, state=db, calendar=cal, tasks=tasks, tg=tg))


def watch(s, now=NOW):
    return calwatch.Watcher(CFG, s.db, s.cal, s.tasks, s.tg, now).run()


def review(s, now=NOW):
    return calwatch.daily_review(CFG, s.db, s.cal, s.tasks, s.tg, now)


def tap(s, data, message_id=None):
    action, _, rest = data.partition(":")
    cq = {"id": "cb", "data": data, "message": {"message_id": message_id or 999}}
    if action in ("crv", "crva"):
        calwatch.handle_review(s.listener, cq, action, rest, NOW)
    else:
        calwatch.handle_callback(s.listener, cq, action, rest, NOW)


def last_review(s):
    return next(m for m in reversed(s.tg.sent) if m["text"].startswith("Calendar review"))


def edited(s, message):
    return s.tg.edits.get(message["id"], message)


def row(s, cal_id, key):
    return s.db.watch_get(cal_id, key)


def buttons_of(message):
    return [b for r in (message["buttons"] or []) for b in r]


def test_non_urgent_events_wait_for_the_review(s):
    watch(s)
    assert [m["text"].split("\n")[1] for m in s.tg.sent] == ["Project meeting"]        # only the one in 6 h
    assert row(s, OUTLOOK, "s1")["review"] == "new" and row(s, TLE, "c0")["review"] == "new"


def test_review_is_one_numbered_message(s):
    assert review(s)
    msg = last_review(s)
    lines = msg["text"].split("\n")
    assert lines[:3] == ["Calendar review - Mon 28 Sep", "", "New"]
    number = next(l.split(".")[0] for l in lines if "DSA tutorial (Outlook): weekly on Tue" in l)   # sorted by date
    assert (f"{number} Track", f"crv:t:{row(s, OUTLOOK, 's1')['id']}") in buttons_of(msg)
    assert ("Track all new", "crva:t:1") in buttons_of(msg)
    assert len(msg["buttons"]) <= calwatch.REVIEW_BUTTONS + 1                          # + the "all new" row


def test_tapping_an_item_updates_the_review_in_place(s):
    review(s)
    msg = last_review(s)
    tap(s, f"crv:t:{row(s, OUTLOOK, 's1')['id']}")
    after = edited(s, msg)
    assert row(s, OUTLOOK, "s1")["status"] == "tracked" and row(s, OUTLOOK, "s1")["review"] is None
    line = next(l for l in after["text"].split("\n") if "DSA tutorial (Outlook): weekly on Tue, with no end date" in l)
    assert line.endswith("[tracked]")
    assert not any(b[1] == f"crv:t:{row(s, OUTLOOK, 's1')['id']}" for b in buttons_of(after))


def test_track_all_new_and_undo(s):
    review(s)
    msg = last_review(s)
    tap(s, "crva:t:1")
    assert len(s.cal.inserted) == 8                                        # 7 contests + the tutorial series
    undo = next(b for b in buttons_of(edited(s, msg)) if b[0] == "Undo")
    tap(s, undo[1])
    assert len(s.cal.deleted) == 8
    assert row(s, TLE, "c0")["status"] == "pending" and row(s, TLE, "c0")["review"] == "new"
    assert ("Track all new", "crva:t:1") in buttons_of(edited(s, msg))


def test_changed_tracked_event_is_updated_then_asked(s):
    review(s)
    tap(s, f"crv:t:{row(s, OUTLOOK, 's1')['id']}")
    # the tutorial moves one hour later every week
    s.cal.evs[OUTLOOK][1:] = [event("s1_a", h(27), h(28), "DSA tutorial", recurringEventId="s1"),
                              event("s1_b", h(27 + 168), h(28 + 168), "DSA tutorial", recurringEventId="s1")]
    s.cal.masters[OUTLOOK]["s1"]["start"] = {"dateTime": h(27).isoformat()}
    s.cal.masters[OUTLOOK]["s1"]["end"] = {"dateTime": h(28).isoformat()}
    watch(s)
    assert s.cal.patched and s.cal.patched[-1][2]["start"]["dateTime"] == h(27).isoformat()   # copy updated at once
    assert row(s, OUTLOOK, "s1")["review"] == "changed"
    review(s)
    msg = last_review(s)
    assert "\nChanged\n" in msg["text"] and "your College copy was updated" in msg["text"]
    keep, remove = [b for b in buttons_of(msg) if b[1].endswith(f":{row(s, OUTLOOK, 's1')['id']}")]
    assert keep[0].endswith("Keep") and remove[0].endswith("Remove")
    tap(s, remove[1])
    assert row(s, OUTLOOK, "s1")["status"] == "ignored" and (COLLEGE, row(s, OUTLOOK, "s1")["copy_id"] or "copy8") \
        in s.cal.deleted or s.cal.deleted


def test_ignored_event_that_changes_is_asked_again(s):
    review(s)
    tap(s, f"crv:i:{row(s, TLE, 'c3')['id']}")
    s.cal.evs[TLE][3] = event("c3", h(24 * 4 + 3), h(24 * 4 + 5), "Codeforces Round 903")   # moved
    watch(s)
    assert row(s, TLE, "c3")["review"] == "changed"
    review(s)
    msg = last_review(s)
    track_now = next(b for b in buttons_of(msg) if b[0].endswith("Track now"))
    tap(s, track_now[1])
    assert row(s, TLE, "c3")["status"] == "tracked"
    assert any(body["summary"] == "Codeforces Round 903" for _, body in s.cal.inserted.values())


def test_urgent_change_is_asked_right_away(s):
    watch(s)
    card = next(m for m in s.tg.sent if "Project meeting" in m["text"])
    tap(s, card["buttons"][0][0][1], message_id=card["id"])                           # Track
    s.cal.evs[OUTLOOK][0] = event("m1", h(8), h(9), "Project meeting")                  # moved, still today
    watch(s)
    change = s.tg.sent[-1]
    assert change["text"].startswith("Changed on Outlook: Project meeting") and "Still want it?" in change["text"]
    tap(s, change["buttons"][0][0][1], message_id=change["id"])                        # Keep
    assert s.tg.edits[change["id"]]["text"].endswith("Kept as it is.")
    assert row(s, OUTLOOK, "m1")["status"] == "tracked" and row(s, OUTLOOK, "m1")["review"] is None


def test_cancelled_tracked_event_is_news_in_the_review(s):
    review(s)
    tap(s, f"crv:t:{row(s, TLE, 'c2')['id']}")
    copy_id = row(s, TLE, "c2")["copy_id"]
    del s.cal.evs[TLE][2]
    watch(s)
    assert (COLLEGE, copy_id) in s.cal.deleted and row(s, TLE, "c2")["review"] == "cancelled"
    review(s)
    assert "\nCancelled\n- Codeforces Round 902 (TLE Contest Tracker): its copy was removed from College" in last_review(s)["text"]
    assert row(s, TLE, "c2")["review"] is None                                        # news is shown once


def test_undecided_items_carry_over(s):
    review(s)
    first = last_review(s)
    assert review(s)                                                                  # next day, nothing decided
    second = last_review(s)
    assert second["id"] != first["id"] and "DSA tutorial (Outlook)" in second["text"]
    assert s.tg.edits[first["id"]]["text"] == "This review was replaced by a newer one below."


def test_nothing_new_sends_nothing(s):
    review(s)
    tap(s, "crva:i:1")                                   # decide everything in the review
    before = len(s.tg.sent)
    assert review(s) is False                            # the urgent meeting was asked on its own card, not here
    assert len(s.tg.sent) == before
