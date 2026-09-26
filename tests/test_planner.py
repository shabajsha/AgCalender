from datetime import timedelta

import pytest

import google_writer
import planner
import ranker
from conftest import at, event, minutes

NOW = at(2026, 9, 28, 14, 7)  # a Monday
CFG = {"timezone": "Asia/Kolkata", "tasklist": "AGENT", "ollama": {},
       "calendars": {"college": "COL", "planner": "PLAN", "habits": "HAB"},
       "planner": {"work_window": ["08:00", "23:00"], "sleep": ["23:30", "07:00"], "block_minutes": 90,
                   "min_block_minutes": 30, "gap_minutes": 15, "max_work_hours_per_day": 6,
                   "default_effort_hours": 3, "task_effort_hours": 1, "horizon_days": 14, "carry_over_days": 3},
       "habits": [{"name": "Gym", "minutes": 60, "window": ["17:00", "19:00"], "days": ["mon", "wed"]},
                  {"name": "Swim", "minutes": 45, "window": ["07:00", "08:00"], "days": ["tue"]}]}


def T(h, m=0, d=0):
    return NOW.replace(hour=h, minute=m) + timedelta(days=d)


def tagged(kind, key=None):
    private = {"source": google_writer.PLANNER_TAG, "kind": kind, **({"work_key": key} if key else {})}
    return {"extendedProperties": {"private": private}}


@pytest.fixture
def world(monkeypatch):
    """Fake calendars/tasks; returns what the planner created and deleted."""
    events = {"primary": [event("class", T(15), T(16), "OS class")], "COL": [], "HAB": [],
              "PLAN": [event("stale", T(18), T(19, 30), "Work: stale", **tagged("work", "event:due1")),
                       event("done", T(10), T(11), "Work: DSA", **tagged("work", "event:due1"))]}
    created, deleted = [], []
    monkeypatch.setattr(planner, "fetch_events", lambda cal, cid, s, e: list(events.get(cid, [])))
    monkeypatch.setattr(google_writer, "busy_calendar_ids", lambda cal, configured: configured)
    monkeypatch.setattr(google_writer, "list_blocks", lambda cal, cid, s, e: [
        x for x in events.get(cid, []) if "extendedProperties" in x])
    monkeypatch.setattr(google_writer, "delete_event", lambda cal, cid, eid: deleted.append(eid))
    monkeypatch.setattr(google_writer, "create_block",
                        lambda cal, cid, title, s, e, tzn, kind, work_key=None, note=None: created.append((kind, title, s, e)))
    monkeypatch.setattr(planner, "get_credentials", lambda: None)
    monkeypatch.setattr(planner, "build", lambda *a, **k: None)
    monkeypatch.setattr(ranker, "rank", lambda items, llm, today: (items, True))
    work = [{"key": "event:due1", "title": "DSA Assignment 2", "due": T(23, 59, 2), "effort_h": 3},
            {"key": "event:due2", "title": "OS Midsem prep", "due": T(9, 0, 5), "effort_h": 12}]
    monkeypatch.setattr(planner, "open_work", lambda *a: [dict(w) for w in work])
    return {"created": created, "deleted": deleted, "work": work, "events": events}


def test_full_plan(world, db):
    title, text, placed = planner.plan_today(CFG, db, NOW)
    blocks = sorted(world["created"], key=lambda c: c[2])
    assert world["deleted"] == ["stale"]                                   # only the not-yet-started block
    assert ("habit", "Gym") in [(k, t) for k, t, _, _ in blocks]
    assert "Swim" not in text                                               # Tuesday-only
    busy = [(T(15), T(16)), (T(10), T(11))]
    for _, _, s, e in blocks:
        assert s >= T(14, 15) and e <= T(23)
        assert all(not (s < be + timedelta(minutes=15) and e > bs - timedelta(minutes=15)) for bs, be in busy)
    assert all(blocks[i][3] + timedelta(minutes=15) <= blocks[i + 1][2] for i in range(len(blocks) - 1))
    dsa = sum(minutes(e - s) for k, t, s, e in blocks if t == "Work: DSA Assignment 2")
    assert dsa == 60                          # 2 h left (1 h done this morning) over 3 days -> 1 h today
    assert db.plan_status(NOW.date()) == "manual" and placed


def test_no_block_after_the_deadline(world, db):
    world["work"][:] = [{"key": "event:x", "title": "Report", "due": T(16, 30), "effort_h": 3}]
    planner.plan_today(CFG, db, NOW)
    work = [c for c in world["created"] if c[0] == "work"]
    assert work and all(e <= T(16, 15) for _, _, _, e in work)   # nothing placed after (due - gap)


def test_open_work_skips_completed_deadlines(monkeypatch, db):
    monkeypatch.setattr(planner, "fetch_events", lambda cal, cid, s, e: [
        event("E1", T(23, 29, 2), T(23, 59, 2), "DUE: Done already"), event("E2", T(23, 29, 3), T(23, 59, 3), "DUE: Open")])
    monkeypatch.setattr(planner.gtasks, "completed_ids", lambda api, tl: {"T1"})
    monkeypatch.setattr(planner.gtasks, "open_dated_tasks", lambda api, skip, before: [
        {"id": "old", "title": "Pay fee", "due": NOW.date() - timedelta(days=1)},
        {"id": "ancient", "title": "Forgotten", "due": NOW.date() - timedelta(days=30)}])
    db.record_item("k1", "deadline", "E1", "T1", "Done already", "x", "m")
    titles = [i["title"] for i in planner.open_work(None, None, CFG, db, NOW, NOW + timedelta(days=14))]
    assert titles == ["Open", "Pay fee (overdue)"]      # finished deadline and long-overdue task left out


def test_second_planner_waits_then_gives_up():
    with planner.planning_lock():
        with pytest.raises(planner.PlannerBusy):   # a separate open file: flock conflicts like another process
            with planner.planning_lock(wait_s=0.3):
                pass
    with planner.planning_lock(wait_s=0.3):        # free again afterwards
        pass
