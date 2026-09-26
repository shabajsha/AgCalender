"""SQLite state (state.db): processed emails, created items, items waiting for your approval, sender choices."""
import json
import os
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

DB_PATH = Path(__file__).parent / "state.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS processed_messages (
    msg_id       TEXT PRIMARY KEY,
    fingerprint  TEXT,
    outcome      TEXT,          -- created / no-items / no-keyword / duplicate
    processed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_fingerprint ON processed_messages(fingerprint);
CREATE TABLE IF NOT EXISTS created_items (
    dedupe_key TEXT PRIMARY KEY,  -- normalised title | start
    kind       TEXT,
    event_id   TEXT,
    task_id    TEXT,
    title      TEXT,
    start      TEXT,
    msg_id     TEXT,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS pending_items (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    dedupe_key    TEXT UNIQUE,
    item_json     TEXT,
    msg_id        TEXT,
    msg_subject   TEXT,
    sender        TEXT,          -- email address of the real sender
    status        TEXT,          -- pending / added / skipped / expired
    tg_message_id INTEGER,
    created_at    TEXT,
    decided_at    TEXT
);
CREATE TABLE IF NOT EXISTS efforts (
    work_key   TEXT PRIMARY KEY,  -- "event:<DUE event id>" or "task:<task id>"
    hours      REAL,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS plans (
    plan_date  TEXT PRIMARY KEY,  -- YYYY-MM-DD
    planned_at TEXT,
    how        TEXT               -- auto / manual / cleared
);
CREATE TABLE IF NOT EXISTS failures (
    msg_id     TEXT PRIMARY KEY,  -- an email that raised an error; retried until MAX tries, then given up
    count      INTEGER,
    last_error TEXT,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS todos (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    batch      INTEGER,           -- one bot message = one batch (what its Undo button removes)
    task_id    TEXT,
    title      TEXT,
    minutes    INTEGER,
    day        TEXT,
    status     TEXT,              -- active / undone
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS sender_prefs (
    sender     TEXT PRIMARY KEY,
    pref       TEXT,             -- asked / blocked / keep
    updated_at TEXT
);
"""


def item_to_json(item):
    def enc(v):
        if isinstance(v, datetime):
            return {"datetime": v.isoformat()}
        if isinstance(v, date):
            return {"date": v.isoformat()}
        return v
    return json.dumps({k: enc(v) for k, v in item.items()})


def item_from_json(text):
    def dec(v):
        if isinstance(v, dict) and "datetime" in v:
            return datetime.fromisoformat(v["datetime"])
        if isinstance(v, dict) and "date" in v:
            return date.fromisoformat(v["date"])
        return v
    return {k: dec(v) for k, v in json.loads(text).items()}


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class State:
    def __init__(self, dry_run=False, path=None):
        path = path or DB_PATH  # looked up at call time so tests can point DB_PATH at a temp file
        if dry_run:
            # Work on an in-memory copy so dedupe still behaves, but nothing touches disk.
            self.db = sqlite3.connect(":memory:")
            if Path(path).exists():
                with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as src:
                    src.backup(self.db)
        else:
            self.db = sqlite3.connect(path, timeout=30)  # several processes share it
            if path != ":memory:":
                os.chmod(path, 0o600)  # it holds email subjects and senders
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    def is_processed(self, msg_id):
        return self.db.execute("SELECT 1 FROM processed_messages WHERE msg_id = ?", (msg_id,)).fetchone() is not None

    def fingerprint_seen(self, fingerprint, within_days=2):
        """Same content seen recently? Double forwards arrive seconds apart; an identical weekly reminder a week
        later is a new email and must not be dropped."""
        since = (datetime.now(timezone.utc) - timedelta(days=within_days)).isoformat(timespec="seconds")
        return self.db.execute("SELECT 1 FROM processed_messages WHERE fingerprint = ? AND processed_at >= ?",
                               (fingerprint, since)).fetchone() is not None

    def record_failure(self, msg_id, error):
        """Counts an error for this email; returns how many times it has failed."""
        self.db.execute("INSERT INTO failures VALUES (?, 1, ?, ?) ON CONFLICT(msg_id) DO UPDATE SET "
                        "count = count + 1, last_error = excluded.last_error, updated_at = excluded.updated_at",
                        (msg_id, str(error)[:300], _now()))
        self.db.commit()
        return self.db.execute("SELECT count FROM failures WHERE msg_id = ?", (msg_id,)).fetchone()[0]

    def mark_processed(self, msg_id, fingerprint, outcome):
        self.db.execute("INSERT OR REPLACE INTO processed_messages VALUES (?, ?, ?, ?)",
                        (msg_id, fingerprint, outcome, _now()))
        self.db.commit()

    def item_exists(self, dedupe_key):
        return self.db.execute("SELECT 1 FROM created_items WHERE dedupe_key = ?", (dedupe_key,)).fetchone() is not None

    def record_item(self, dedupe_key, kind, event_id, task_id, title, start, msg_id):
        self.db.execute("INSERT OR REPLACE INTO created_items VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (dedupe_key, kind, event_id, task_id, title, start, msg_id, _now()))
        self.db.commit()

    def get_last_run(self):
        row = self.db.execute("SELECT value FROM meta WHERE key = 'last_run'").fetchone()
        return datetime.fromisoformat(row[0]) if row else None

    def set_last_run(self, when):
        self.db.execute("INSERT OR REPLACE INTO meta VALUES ('last_run', ?)", (when.isoformat(),))
        self.db.commit()

    # --- /pause and /resume from Telegram ---------------------------------------------

    def paused_since(self):
        row = self.db.execute("SELECT value FROM meta WHERE key = 'paused_since'").fetchone()
        return datetime.fromisoformat(row[0]) if row else None

    def set_paused(self, paused):
        if paused:
            self.db.execute("INSERT OR IGNORE INTO meta VALUES ('paused_since', ?)", (_now(),))
        else:
            self.db.execute("DELETE FROM meta WHERE key = 'paused_since'")
        self.db.commit()

    def count_open_pending(self):
        return self.db.execute("SELECT COUNT(*) FROM pending_items WHERE status = 'pending'").fetchone()[0]

    # --- approval queue -------------------------------------------------------------

    def pending_exists(self, dedupe_key):
        """True if this item was ever sent for approval (whatever you answered)."""
        return self.db.execute("SELECT 1 FROM pending_items WHERE dedupe_key = ?", (dedupe_key,)).fetchone() is not None

    def add_pending(self, dedupe_key, item, msg, sender):
        cur = self.db.execute(
            "INSERT INTO pending_items (dedupe_key, item_json, msg_id, msg_subject, sender, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, 'pending', ?)",
            (dedupe_key, item_to_json(item), msg["id"], msg["subject"], sender, _now()))
        self.db.commit()
        return cur.lastrowid

    def delete_pending(self, pending_id):
        self.db.execute("DELETE FROM pending_items WHERE id = ?", (pending_id,))
        self.db.commit()

    def set_pending_message(self, pending_id, tg_message_id):
        self.db.execute("UPDATE pending_items SET tg_message_id = ? WHERE id = ?", (tg_message_id, pending_id))
        self.db.commit()

    def get_pending(self, pending_id):
        row = self.db.execute("SELECT * FROM pending_items WHERE id = ?", (pending_id,)).fetchone()
        if row is None:
            return None
        return {**dict(row), "item": item_from_json(row["item_json"])}

    def open_pending(self):
        rows = self.db.execute("SELECT id FROM pending_items WHERE status = 'pending'").fetchall()
        return [self.get_pending(r["id"]) for r in rows]

    def set_pending_status(self, pending_id, status):
        self.db.execute("UPDATE pending_items SET status = ?, decided_at = ? WHERE id = ?", (status, _now(), pending_id))
        self.db.commit()

    # --- what you think of each sender ----------------------------------------------

    def sender_counts(self, sender):
        """(items you added, items you skipped) from this sender."""
        row = self.db.execute(
            "SELECT SUM(status = 'added'), SUM(status = 'skipped') FROM pending_items WHERE sender = ?", (sender,)).fetchone()
        return (row[0] or 0, row[1] or 0)

    def get_sender_pref(self, sender):
        row = self.db.execute("SELECT pref FROM sender_prefs WHERE sender = ?", (sender,)).fetchone()
        return row["pref"] if row else None

    def set_sender_pref(self, sender, pref):
        self.db.execute("INSERT OR REPLACE INTO sender_prefs VALUES (?, ?, ?)", (sender, pref, _now()))
        self.db.commit()

    def blocked_senders(self):
        return {r["sender"] for r in self.db.execute("SELECT sender FROM sender_prefs WHERE pref = 'blocked'")}

    def task_for_event(self, event_id):
        """The Google Task created together with a DUE event, if any."""
        row = self.db.execute("SELECT task_id FROM created_items WHERE event_id = ?", (event_id,)).fetchone()
        return row["task_id"] if row else None

    def event_id_for(self, dedupe_key):
        row = self.db.execute("SELECT event_id FROM created_items WHERE dedupe_key = ?", (dedupe_key,)).fetchone()
        return row["event_id"] if row else None

    # --- planner ------------------------------------------------------------------------

    def get_effort(self, work_key):
        row = self.db.execute("SELECT hours FROM efforts WHERE work_key = ?", (work_key,)).fetchone()
        return row["hours"] if row else None

    def set_effort(self, work_key, hours):
        self.db.execute("INSERT OR REPLACE INTO efforts VALUES (?, ?, ?)", (work_key, hours, _now()))
        self.db.commit()

    def plan_status(self, day):
        """None if `day` hasn't been planned, else 'auto' / 'manual' / 'cleared'."""
        row = self.db.execute("SELECT how FROM plans WHERE plan_date = ?", (day.isoformat(),)).fetchone()
        return row["how"] if row else None

    def record_plan(self, day, how):
        self.db.execute("INSERT OR REPLACE INTO plans VALUES (?, ?, ?)", (day.isoformat(), _now(), how))
        self.db.commit()

    # --- small key/value flags (morning check-in etc.) ---------------------------------------

    def get_meta(self, key):
        row = self.db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key, value):
        self.db.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, value))
        self.db.commit()

    # --- to-dos added from Telegram ------------------------------------------------------------

    def next_todo_batch(self):
        return (self.db.execute("SELECT COALESCE(MAX(batch), 0) FROM todos").fetchone()[0] or 0) + 1

    def add_todo(self, batch, task_id, title, minutes, day):
        """Recorded right after each task is created, so Undo covers it even if a later one fails."""
        self.db.execute("INSERT INTO todos (batch, task_id, title, minutes, day, status, created_at) "
                        "VALUES (?, ?, ?, ?, ?, 'active', ?)", (batch, task_id, title, minutes, day.isoformat(), _now()))
        self.db.commit()

    def todo_batch(self, batch):
        return [dict(r) for r in self.db.execute("SELECT * FROM todos WHERE batch = ? AND status = 'active'", (batch,))]

    def mark_todos_undone(self, batch):
        self.db.execute("UPDATE todos SET status = 'undone' WHERE batch = ?", (batch,))
        self.db.commit()
