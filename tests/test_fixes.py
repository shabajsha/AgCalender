"""Regression tests for the 26 Sep audit (mail, alerts, dates, planner, digest, LLM guard, Telegram client).
Each failed before its fix."""
import sys
from datetime import date, datetime, time, timedelta
from types import SimpleNamespace

import pytest
from google.auth.exceptions import TransportError

import alerts
import approvals
import digest
import extractor
import google_writer
import ingest
import llm
import planner
import slots
import telegram_bot
from conftest import FakeTelegram, at, event
from dates import resolve_date, resolve_time_range
from gmail_reader import fingerprint
from ics_import import parse_ics
from test_planner import CFG as PLAN_CFG, T, world  # noqa: F401  (world is a fixture)

NOW = at(2026, 9, 26, 10)


# --- mail pre-filter and extraction ----------------------------------------------------------------

KW = ["due", "quiz", "exam", "mid sem", "mid semester", "lab", "assignment"]
WEAK = ["feedback", "form", "reminder"]


@pytest.mark.parametrize("text, expected", [
    ("Exams schedule for next week", True),
    ("Quizzes will be held on Friday", True),
    ("Mid-sem timetable released", True),
    ("Labs are cancelled tomorrow", True),
    ("Reminder: Mid semester feedback\nThe access code is active till 28th September 2026 (Monday, 11:59 pm)", True),
    ("Please fill the feedback form by Friday.", True),                 # weak word + a day in the sentence
    ("Feedback is always welcome.", False),                              # weak word alone
    ('Presentation shared with you: "Samsung"', False),
    ("Seats available now", False),                                      # "lab" inside a word
])
def test_prefilter(text, expected):
    assert extractor.matches_keywords(text, KW, WEAK) is expected


def run(**fields):
    item = {"title": "Thing", "type": "event", "date": "12 October", "start_time": "10 AM", "confidence": 0.9, **fields}
    return extractor.validate({"items": [item]}, NOW, "Asia/Kolkata", 0.6, NOW)


def test_times_without_a_clock_are_not_midnight():
    [d] = run(type="deadline", date="today", start_time="1700 hrs")      # was 00:00 -> "past" -> dropped
    assert d["due"] == at(2026, 9, 26, 17)
    [e] = run(start_time="2-4 PM")
    assert (e["start"].time(), e["end"].time()) == (time(14), time(16))
    [d] = run(type="deadline", date="Friday", start_time="at 5")         # unclear: end of day, not midnight
    assert d["due"].time() == time(23, 59)


def test_submission_window_is_due_at_its_end():
    [d] = run(type="deadline", start_time="10 AM", end_time="11:59 PM")
    assert d["due"].time() == time(23, 59)


def test_unreadable_answer_is_reported(monkeypatch):
    monkeypatch.setattr(extractor, "chat_json", lambda *a: "not json")
    msg = {"subject": "s", "body": "b", "received": NOW}
    with pytest.raises(extractor.UnreadableAnswer):
        extractor.extract_items(msg, {}, "Asia/Kolkata", 0.6, NOW)


@pytest.mark.parametrize("text, received, expected", [
    ("next Friday 5pm", date(2026, 10, 2), date(2026, 10, 9)),           # "next" kept despite the time
    ("in 2 weeks", date(2026, 9, 1), date(2026, 9, 15)),
    ("Week 7", date(2026, 9, 26), None),
    ("Quiz 1 held on 10th August", date(2026, 9, 26), date(2026, 8, 10)),   # past, not next year
    ("15.10.2026", date(2026, 9, 26), date(2026, 10, 15)),
    ("29 Feb", date(2027, 12, 20), None),                                # used to crash
])
def test_dates(text, received, expected):
    assert resolve_date(text, received) == expected


def test_time_ranges():
    assert resolve_time_range("10 AM to 12 PM") == (time(10), time(12))
    assert resolve_time_range("1100-1200 hrs") == (time(11), time(12))
    assert resolve_time_range("5th October") == (None, None)


def test_double_forward_with_different_quotes_is_one_email():
    assert fingerprint({"subject": "Students’ Parliament", "body": "Vote – Monday"}) == \
        fingerprint({"subject": "Students' Parliament", "body": "Vote - Monday"})


ICS = b"""BEGIN:VCALENDAR
METHOD:%s
BEGIN:VEVENT
UID:abc@x
SUMMARY:Project sync
DTSTART:20261001T100000
DURATION:PT3H
END:VEVENT
BEGIN:VEVENT
UID:abc@x
RECURRENCE-ID:20261008T100000
SUMMARY:Project sync (moved)
DTSTART:20261008T120000
DURATION:PT1H
END:VEVENT
END:VCALENDAR
"""


def test_ics_updates_replies_and_durations():
    [item] = parse_ics(ICS % b"REQUEST", "Asia/Kolkata")                   # the changed occurrence isn't separate
    assert item["end"] - item["start"] == timedelta(hours=3) and item["uid"] == "abc@x"
    assert parse_ics(ICS % b"REPLY", "Asia/Kolkata") == []                 # "Accepted: ..." is not a new event


def test_recurring_invite_card_does_not_expire_after_its_first_week():
    item = {"due": None, "start": at(2026, 9, 1, 9), "recurrence": ["RRULE:FREQ=WEEKLY"]}
    assert not approvals._is_past(item, NOW)


# --- ingest ----------------------------------------------------------------------------------------

@pytest.fixture
def ingest_run(monkeypatch, db):
    cfg = {"timezone": "Asia/Kolkata", "gmail_query": "label:iiith", "keywords": ["due"], "weak_keywords": [],
           "skip_senders": [], "skip_subjects": ["shared with you"], "approval": {"enabled": True},
           "first_run_days": 3, "ollama": {}, "min_confidence": 0.6, "calendars": {"college": "COL"},
           "heartbeat_url": "", "calendar_watch": {"enabled": False}}
    msgs, watched, tg = [], [], FakeTelegram()
    monkeypatch.setattr(ingest, "load_config", lambda: cfg)
    monkeypatch.setattr(ingest, "State", lambda dry_run=False: db)
    monkeypatch.setattr(ingest, "get_credentials", lambda: None)
    monkeypatch.setattr(ingest, "build", lambda *a, **k: None)
    monkeypatch.setattr(ingest.Telegram, "from_file", staticmethod(lambda: tg))
    monkeypatch.setattr(ingest, "fetch_messages", lambda svc, q, after, tz, skip=None: [m for m in msgs if not skip(m["id"])])
    monkeypatch.setattr(ingest, "watch_calendars", lambda *a: watched.append(True))
    monkeypatch.setattr(alerts, "alert", lambda key, text, state=None: None)
    monkeypatch.setattr(alerts, "resolved", lambda key, state=None: None)
    monkeypatch.setattr(sys, "argv", ["ingest.py"])
    return SimpleNamespace(msgs=msgs, watched=watched, tg=tg, cfg=cfg)


def mail(msg_id, subject, body="text"):
    return {"id": msg_id, "subject": subject, "received": NOW, "from_line": "Prof <p@iiit.ac.in>",
            "sender": "Prof <p@iiit.ac.in>", "body": body, "ics": []}


def test_telegram_outage_does_not_count_against_the_email(ingest_run, db, monkeypatch):
    ingest_run.msgs += [mail("m1", "Lab due Friday"), mail("m2", "Quiz due Monday")]
    item = {"type": "deadline", "title": "Lab", "start": NOW, "end": NOW, "all_day": False, "due": NOW + timedelta(days=1),
            "course": None, "location": None, "description": None, "recurrence": None}
    calls = []
    monkeypatch.setattr(ingest, "items_for", lambda msg, cfg, now: calls.append(msg["id"]) or ([dict(item, title=msg["id"])], "no-items"))
    monkeypatch.setattr(ingest.approvals, "ask",
                        lambda *a: (_ for _ in ()).throw(telegram_bot.TelegramError("sendMessage: network error")))
    ingest.main()
    assert calls == ["m1"]                                          # stopped: no model time for m2 either
    assert db.db.execute("SELECT COUNT(*) FROM failures").fetchone()[0] == 0 and not db.is_processed("m1")


def test_skipped_emails_are_remembered_for_skipped_list(ingest_run, db):
    ingest_run.msgs += [mail("m1", "Hostel cleanliness"), mail("m2", 'Presentation shared with you: "x"')]
    ingest.main()
    [row] = db.skipped_messages()
    assert row["subject"] == "Hostel cleanliness"                    # the share notice is skipped, not listed
    assert db.db.execute("SELECT outcome FROM processed_messages WHERE msg_id='m2'").fetchone()[0] == "skipped-subject"


def test_pause_stops_mail_but_not_calendar_checks(ingest_run, db):
    db.set_paused(True)
    ingest_run.msgs.append(mail("m1", "Lab due Friday"))
    ingest.main()
    assert ingest_run.watched and not db.is_processed("m1")


def test_no_mail_for_three_days_alerts(db, monkeypatch):
    sent = []
    monkeypatch.setattr(alerts, "alert", lambda key, text, state=None: sent.append(key))
    start = datetime(2026, 9, 20, tzinfo=NOW.tzinfo)
    ingest.check_mail_flow(db, 0, start, False)                          # first run: starts the clock
    ingest.check_mail_flow(db, 0, start + timedelta(days=2), False)
    assert not sent
    ingest.check_mail_flow(db, 0, start + timedelta(days=3), False)
    assert sent == ["no-mail"]


# --- alerts ----------------------------------------------------------------------------------------

def test_being_offline_is_not_a_crash(monkeypatch):
    sent = []
    monkeypatch.setattr(alerts, "alert", lambda key, text, state=None: sent.append(key))

    @alerts.guard("demo")
    def main():
        raise TransportError("Failed to resolve 'oauth2.googleapis.com'")

    with pytest.raises(SystemExit) as exit_:
        main()
    assert exit_.value.code == 0 and sent == []


# --- LLM GPU guard ----------------------------------------------------------------------------------

def test_gpu_lost_is_remembered_instead_of_retried_every_run(monkeypatch, db):
    loaded = SimpleNamespace(models=[SimpleNamespace(model="gemma2:9b", size=100, size_vram=0)])
    chats = []
    monkeypatch.setattr(llm, "available_ram_gb", lambda: 16)
    monkeypatch.setattr(llm.ollama, "ps", lambda: loaded)
    monkeypatch.setattr(llm.ollama, "generate", lambda **k: None)
    monkeypatch.setattr(llm.ollama, "Client", lambda timeout: SimpleNamespace(
        chat=lambda **k: chats.append(1) or {"message": {"content": "{}"}}))
    monkeypatch.setattr(alerts, "alert", lambda key, text, state=None: None)
    monkeypatch.setattr(alerts, "resolved", lambda key, state=None: None)
    cfg = {"model": "gemma2:9b"}
    with pytest.raises(llm.LLMUnavailable):
        llm.chat_json(cfg, "s", "u", db)                                 # loaded on the CPU: refused, no inference
    with pytest.raises(llm.LLMUnavailable):
        llm.chat_json(cfg, "s", "u", db)                                 # next run: refused without asking Ollama
    assert chats == [] and llm.gpu_lost_since(db)
    loaded.models[0].size_vram = 100                                     # you fixed it and tapped Check mail now
    llm.clear_gpu_flag(db)
    assert llm.chat_json(cfg, "s", "u", db) == "{}" and not llm.gpu_lost_since(db)


# --- planner ---------------------------------------------------------------------------------------

def test_short_todo_is_planned(world, db):  # noqa: F811
    world["work"][:] = [{"key": "task:t1", "title": "Call bank", "due": T(23, 59), "effort_h": 0.25}]
    planner.plan_today(PLAN_CFG, db, planner_now())
    [(kind, title, s, e)] = [c for c in world["created"] if c[0] == "work"]
    assert title == "Work: Call bank" and e - s == timedelta(minutes=15)


def planner_now():
    return T(14, 7)


def test_morning_and_midnight_deadlines_leave_no_time_on_their_day():
    work_start = slots.hm("08:00")
    thu = date(2026, 10, 1)
    assert planner.work_days_left(at(2026, 10, 2, 8, 30), thu, work_start, 15, 30) == 1   # Fri 08:30: Thu is last
    assert planner.work_days_left(at(2026, 10, 2, 0, 0), thu, work_start, 15, 30) == 1    # Fri 00:00 likewise
    assert planner.work_days_left(at(2026, 10, 2, 23, 59), thu, work_start, 15, 30) == 2


def test_clear_keeps_started_blocks_and_refuses_past_days(monkeypatch):
    now = T(14, 7)
    blocks = [event("done", T(10), T(11), "Work: DSA", extendedProperties={"private": {"plan_date": now.date().isoformat()}}),
              event("later", T(18), T(19), "Work: DSA", extendedProperties={"private": {"plan_date": now.date().isoformat()}})]
    deleted = []
    monkeypatch.setattr(planner, "get_credentials", lambda: None)
    monkeypatch.setattr(planner, "build", lambda *a, **k: None)
    monkeypatch.setattr(google_writer, "list_blocks", lambda cal, cid, s, e: blocks if cid == "PLAN" else [])
    monkeypatch.setattr(google_writer, "delete_event", lambda cal, cid, eid: deleted.append(eid))
    assert planner.clear_day(PLAN_CFG, now.date(), now) == 1 and deleted == ["later"]
    assert planner.clear_day(PLAN_CFG, now.date() - timedelta(days=1), now) is None


def test_all_day_due_event_does_not_crash_planning(monkeypatch, db):
    monkeypatch.setattr(planner, "fetch_events", lambda cal, cid, s, e: [
        {"id": "E9", "summary": "DUE: Hand-made", "start": {"date": "2026-09-30"}, "end": {"date": "2026-10-01"}}])
    monkeypatch.setattr(planner.gtasks, "completed_ids", lambda api, tl: set())
    monkeypatch.setattr(planner.gtasks, "open_dated_tasks", lambda api, skip, before: [])
    [item] = planner.open_work(None, None, PLAN_CFG, db, T(14), T(14) + timedelta(days=14))
    assert item["due"] == datetime.combine(date(2026, 9, 30), time(23, 59), T(14).tzinfo)


# --- digest ----------------------------------------------------------------------------------------

def test_digest_marks_completed_deadlines_done(monkeypatch, db):
    now = T(8)
    college = [event("E1", T(17, 30, 1), T(18, 0, 1), "DUE: Lab 3"), event("E2", T(17, 30, 2), T(18, 0, 2), "DUE: Quiz prep")]
    monkeypatch.setattr(digest, "get_credentials", lambda: None)
    monkeypatch.setattr(digest, "build", lambda *a, **k: None)
    monkeypatch.setattr(digest.calwatch, "load_calendars", lambda cal, cfg, state: [])
    monkeypatch.setattr(digest, "fetch_events", lambda cal, cid, s, e: college if cid == "COL" else [])
    monkeypatch.setattr(google_writer, "list_blocks", lambda cal, cid, s, e: [])
    monkeypatch.setattr(digest.gtasks, "completed_ids", lambda api, tl: {"T1"})
    monkeypatch.setattr(digest, "fetch_tasks", lambda *a: [])
    db.record_item("k1", "deadline", "E1", "T1", "Lab 3", "x", "m")
    _, sections = digest.build_digest(PLAN_CFG, now, state=db)
    sections = dict(sections)
    assert any("Lab 3  (done)" in line for line in sections["Deadlines in the next 7 days"])
    assert sections["No work time planned yet"] == [f"- Quiz prep (due {T(18, 0, 2):%a %d %b %H:%M})"]


# --- Telegram client ---------------------------------------------------------------------------------

def test_long_messages_are_split():
    parts = telegram_bot.split_text("line\n" * 2000)
    assert len(parts) == 3 and all(len(p) <= telegram_bot.MAX_TEXT for p in parts)


def test_editing_a_deleted_message_does_not_raise(monkeypatch):
    tg = telegram_bot.Telegram("x", 1)
    monkeypatch.setattr(tg, "call", lambda *a, **k: (_ for _ in ()).throw(
        telegram_bot.TelegramError("editMessageText: Bad Request: message to edit not found")))
    assert tg.edit(5, "hi") is False
