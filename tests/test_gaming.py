"""Gaming: the model waits while another program uses the GPU; no popups over a fullscreen game; old databases upgrade."""
import sqlite3
import sys
from types import SimpleNamespace

import pytest

import alerts
import ingest
import llm
import notifiers
from conftest import at
from state import State


def nvidia(used_mb, util, ours_mb=0):
    def run(cmd, **kwargs):
        return SimpleNamespace(stdout=f"{used_mb}, {util}\n", returncode=0)
    ps = SimpleNamespace(models=[SimpleNamespace(model="gemma2:9b", size=ours_mb * 2**20 or 1, size_vram=ours_mb * 2**20)]
                         if ours_mb else [])
    return run, (lambda: ps)


@pytest.mark.parametrize("used, util, ours, busy", [(4200, 97, 0, True),     # a game holds 4 GB
                                                    (400, 14, 0, False),     # normal desktop
                                                    (500, 75, 0, True),      # something is working the GPU hard
                                                    (4300, 90, 4000, False)])  # that's our own model already loaded
def test_gpu_busy(monkeypatch, used, util, ours, busy):
    run, ps = nvidia(used, util, ours)
    monkeypatch.setattr(llm.subprocess, "run", run)
    monkeypatch.setattr(llm.ollama, "ps", ps)
    assert bool(llm.gpu_busy({})) is busy


def test_model_waits_quietly_while_gaming(monkeypatch, db):
    run, ps = nvidia(4200, 97)
    monkeypatch.setattr(llm.subprocess, "run", run)
    monkeypatch.setattr(llm.ollama, "ps", ps)
    monkeypatch.setattr(llm.ollama, "Client", lambda **k: pytest.fail("must not load the model during a game"))
    monkeypatch.setattr(alerts, "alert", lambda *a, **k: pytest.fail("no alert for a busy GPU"))
    with pytest.raises(llm.LLMDeferred):
        llm.chat_json({"model": "gemma2:9b"}, "s", "u", db)
    assert not llm.gpu_lost_since(db)                                  # not treated as a lost GPU


def test_deferred_emails_are_not_errors(monkeypatch, db):
    cfg = {"timezone": "Asia/Kolkata", "gmail_query": "label:iiith", "keywords": ["due"], "skip_senders": [],
           "approval": {"enabled": False}, "first_run_days": 3, "ollama": {}, "min_confidence": 0.6,
           "calendars": {"college": "COL"}, "heartbeat_url": ""}
    msg = {"id": "m1", "subject": "Lab due Friday", "received": at(2026, 9, 28, 9), "from_line": "p@iiit.ac.in",
           "sender": "p@iiit.ac.in", "body": "Lab due Friday", "ics": []}
    monkeypatch.setattr(ingest, "load_config", lambda: cfg)
    monkeypatch.setattr(ingest, "State", lambda dry_run=False: db)
    monkeypatch.setattr(ingest, "get_credentials", lambda: None)
    monkeypatch.setattr(ingest, "build", lambda *a, **k: None)
    monkeypatch.setattr(ingest, "fetch_messages", lambda svc, q, after, tz, skip=None: [dict(msg)])
    monkeypatch.setattr(ingest, "items_for", lambda *a: (_ for _ in ()).throw(llm.LLMDeferred("GPU busy")))
    monkeypatch.setattr(alerts, "alert", lambda key, *a, **k: pytest.fail(f"no alert while gaming ({key})"))
    monkeypatch.setattr(alerts, "resolved", lambda *a, **k: None)
    monkeypatch.setattr(sys, "argv", ["ingest.py"])
    for _ in range(4):                                                # a long gaming session: 4 runs
        ingest.main()
    assert not db.is_processed("m1") and db.get_last_run() is None     # still waiting, will be read later
    assert db.get_meta("ingest_error_runs") == "0" and db.get_meta("llm_waiting") == "1"


def test_no_desktop_popup_over_a_fullscreen_game(monkeypatch):
    monkeypatch.setattr(notifiers, "fullscreen_active", lambda: True)
    monkeypatch.setattr(notifiers.subprocess, "run", lambda *a, **k: pytest.fail("no popup during a game"))
    notifiers.desktop("Good morning", "text")


def test_fullscreen_detection_reads_the_active_window(monkeypatch):
    answers = {"-root": "_NET_ACTIVE_WINDOW(WINDOW): window id # 0x4600016\n",
               "-id": "_NET_WM_STATE(ATOM) = _NET_WM_STATE_FULLSCREEN\n"}
    monkeypatch.setattr(notifiers.subprocess, "run", lambda cmd, **k: SimpleNamespace(stdout=answers[cmd[1]]))
    assert notifiers.fullscreen_active()
    answers["-id"] = "_NET_WM_STATE(ATOM) = _NET_WM_STATE_FOCUSED\n"
    assert not notifiers.fullscreen_active()


def test_alert_waits_instead_of_popping_up_over_a_game(monkeypatch, db):
    monkeypatch.setattr(alerts.Telegram, "from_file", staticmethod(lambda: (_ for _ in ()).throw(alerts.TelegramError("down"))))
    monkeypatch.setattr(notifiers, "fullscreen_active", lambda: True)
    assert alerts.alert("demo", "something", db) is False and not db.get_meta("alert:demo")


def test_an_old_database_is_upgraded(tmp_path):
    """The 13:35 'no such column: sent_at' crash: a table made by an older version must get the new columns."""
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as old:
        old.execute("CREATE TABLE plan_items (id INTEGER PRIMARY KEY AUTOINCREMENT, plan_date TEXT, work_key TEXT, title TEXT, "
                    "kind TEXT, due TEXT, list_id TEXT, minutes INTEGER, tg_message_id INTEGER, status TEXT, "
                    "reminded INTEGER DEFAULT 0, created_at TEXT, UNIQUE (plan_date, work_key))")
        old.execute("CREATE TABLE booked_blocks (id INTEGER PRIMARY KEY AUTOINCREMENT, item_id INTEGER, event_id TEXT, "
                    "work_key TEXT, title TEXT, start TEXT, end TEXT, status TEXT, tg_message_id INTEGER, created_at TEXT)")
    state = State(path=path)
    cols = {t: {r[1] for r in state.db.execute(f"PRAGMA table_info({t})")} for t in ("plan_items", "booked_blocks")}
    assert {"sent_at", "window_start"} <= cols["plan_items"] and {"headsup", "calendar"} <= cols["booked_blocks"]
    state.plan_item_upsert(at(2026, 9, 27).date(), "task:a", "x", "task", "2026-09-27T23:59:00+05:30", None, 30)
