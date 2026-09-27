"""The web page: security checks, the day's state, drag-to-move (409 when not free), booking, settings, habits."""
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "web"))

import actions  # noqa: E402
import google_writer  # noqa: E402
from conftest import TZ, FakeCalendar, FakeTasks  # noqa: E402

CFG = {"timezone": "Asia/Kolkata", "tasklist": "AGENT", "calendars": {"college": "COL", "planner": "PLAN", "habits": "HAB"},
       "planner": {"work_window": ["08:00", "23:00"], "sleep": ["23:30", "07:00"], "block_minutes": 90, "min_block_minutes": 30,
                   "gap_minutes": 15, "max_work_hours_per_day": 6, "default_effort_hours": 3, "task_effort_hours": 1,
                   "horizon_days": 14},
       "morning": {"todo_tasklist": "DAILY", "todo_default_minutes": 30}, "digest": {"channels": ["telegram"]},
       "web": {"allowed_hosts": ["laptop.tail1234.ts.net"], "tailscale_user": "me@example.com"}}


@pytest.fixture
def web(db, monkeypatch):
    import app as webapp
    import slotpicker
    moved = []
    monkeypatch.setattr(webapp.calwatch, "load_calendars", lambda cal, cfg, state: [])
    monkeypatch.setattr(webapp.deadlines, "upcoming", lambda cfg, cal, now, days=14: [])
    monkeypatch.setattr(slotpicker, "prepare", lambda cfg, state, now, cal, tasks, rank=True: ([], [(now, now + timedelta(hours=6))], 360))
    monkeypatch.setattr(google_writer, "move_event", lambda cal, cid, eid, s, e, tz: moved.append((eid, s, e)))
    monkeypatch.setattr(actions, "free_on", lambda cfg, state, cal, day, now, ignore_ids=frozenset(): free[0])
    now = datetime.now(TZ).replace(second=0, microsecond=0)
    free = [[(now, now + timedelta(hours=8))]]
    app = webapp.create_app(services=lambda: (FakeCalendar([]), FakeTasks()), state_factory=lambda: db, config=lambda: CFG)
    client = app.test_client()
    csrf = app.config["CSRF"]
    start = now + timedelta(hours=1)
    block = db.block_add(None, "ev1", "task:a", "Study SDET", start, start + timedelta(minutes=90))
    return type("W", (), {"client": client, "csrf": csrf, "db": db, "moved": moved, "free": free, "now": now,
                          "block": block, "start": start})


def post(w, path, body=None, **headers):
    return w.client.post(path, json=body or {}, headers={"X-CSRF-Token": w.csrf, "Host": "localhost:8765", **headers})


def test_page_has_the_csrf_token_and_strict_headers(web):
    r = web.client.get("/", headers={"Host": "localhost:8765"})
    assert r.status_code == 200 and web.csrf in r.get_data(as_text=True)
    assert "frame-ancestors 'none'" in r.headers["Content-Security-Policy"] and r.headers["X-Frame-Options"] == "DENY"


@pytest.mark.parametrize("headers, status", [
    ({"Host": "evil.example"}, 403),                                    # DNS rebinding
    ({"Host": "laptop.tail1234.ts.net"}, 200),                          # your Tailscale name
    ({"Host": "localhost:8765", "Tailscale-User-Login": "someone@else.com"}, 403),
    ({"Host": "localhost:8765", "Tailscale-User-Login": "me@example.com"}, 200),
])
def test_who_may_open_it(web, headers, status):
    assert web.client.get("/", headers=headers).status_code == status


def test_changes_need_the_token_and_the_same_origin(web):
    start = (web.start + timedelta(hours=2)).isoformat()
    body = {"start": start, "end": (web.start + timedelta(hours=3, minutes=30)).isoformat()}
    assert web.client.post(f"/api/blocks/{web.block}/move", json=body, headers={"Host": "localhost:8765"}).status_code == 403
    assert post(web, f"/api/blocks/{web.block}/move", body, Origin="https://evil.example").status_code == 403
    assert not web.moved


def test_state_lists_blocks_and_settings(web):
    r = web.client.get("/api/state", headers={"Host": "localhost:8765"})
    data = r.get_json()
    assert r.status_code == 200 and data["blocks"][0]["title"] == "Study SDET" and data["blocks"][0]["kind"] == "work"
    assert any(s["label"] == "Daily work limit (h)" for s in data["settings"])


def test_drag_to_a_free_time_moves_the_block(web):
    new = web.start + timedelta(hours=2)
    r = post(web, f"/api/blocks/{web.block}/move", {"start": new.isoformat(), "end": (new + timedelta(minutes=90)).isoformat()})
    assert r.status_code == 200 and web.moved == [("ev1", new, new + timedelta(minutes=90))]


def test_drag_into_busy_time_is_refused_with_alternatives(web):
    web.free[0] = [(web.now + timedelta(hours=5), web.now + timedelta(hours=9))]
    new = web.start + timedelta(hours=1)
    r = post(web, f"/api/blocks/{web.block}/move", {"start": new.isoformat(), "end": (new + timedelta(minutes=90)).isoformat()})
    assert r.status_code == 409 and "isn't free" in r.get_json()["message"] and r.get_json()["alternatives"]
    assert not web.moved


def test_answer_the_done_check(web):
    assert post(web, f"/api/blocks/{web.block}/answer", {"answer": "done"}).status_code == 200
    assert web.db.block(web.block)["status"] == "done"
    assert post(web, f"/api/blocks/{web.block}/answer", {"answer": "done"}).status_code == 409   # only once


def test_book_a_waiting_task(web, monkeypatch):
    created = []
    monkeypatch.setattr(google_writer, "create_block", lambda cal, cid, title, s, e, tz, kind, work_key=None, note=None:
                        created.append((title, s, e)) or "new1")
    item = web.db.plan_item_upsert(web.now.date(), "task:b", "Do LeetCode Daily", "task",
                                   (web.now + timedelta(hours=10)).isoformat(), "DAILY", 30)
    at = web.start + timedelta(hours=3)
    r = post(web, f"/api/items/{item['id']}/book", {"start": at.isoformat(), "minutes": 30})
    assert r.status_code == 200 and created == [("Work: Do LeetCode Daily", at, at + timedelta(minutes=30))]


def test_settings_and_habits(web):
    import settings
    index = next(i for i, s in enumerate(settings.SETTINGS) if s.key == "planner.max_work_hours_per_day")
    assert post(web, f"/api/settings/{index}", {"value": "7"}).status_code == 200
    assert web.db.settings()["planner.max_work_hours_per_day"] == 7.0
    assert post(web, f"/api/settings/{index}", {"value": "99"}).status_code == 400
    assert post(web, "/api/habits", {"name": "Gym", "minutes": 60, "days": ["mon", "fri"], "window_start": "17:00",
                                     "window_end": "21:00"}).status_code == 200
    assert post(web, "/api/habits", {"name": "", "minutes": 60, "days": [], "window_start": "21:00",
                                     "window_end": "17:00"}).status_code == 400
    [h] = web.db.habits()
    assert (h["name"], h["days"]) == ("Gym", "mon,fri")
