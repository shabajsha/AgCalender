"""SQLite state (state.db): processed emails, created items, items waiting for your approval, sender choices."""
import json
import os
import re
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

DB_PATH = Path(__file__).parent / "state.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS processed_messages (
    msg_id       TEXT PRIMARY KEY,
    fingerprint  TEXT,
    outcome      TEXT,          -- items / created / no-items / no-keyword / unreadable / duplicate / skipped-...
    processed_at TEXT,
    subject      TEXT           -- kept for emails the filter skipped, so /skipped can list them
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
CREATE TABLE IF NOT EXISTS watched_events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    cal_id        TEXT,
    event_key     TEXT,           -- series id for a repeating event, else the event id
    is_series     INTEGER,
    title         TEXT,
    start         TEXT,           -- next occurrence (ISO datetime or date)
    snapshot      TEXT,           -- JSON of the source fields, to notice when it changes
    status        TEXT,           -- pending / tracked / ignored / deadline / linked / gone / expired
    copy_id       TEXT,           -- its copy on College (tracked) or its DUE event (deadline)
    tg_message_id INTEGER,
    batch         INTEGER,        -- summary card it was announced on, if any
    review        TEXT,           -- waiting for the daily calendar review: new / changed / cancelled / copied
    review_batch  INTEGER,        -- the review message it was last shown in
    created_at    TEXT,
    decided_at    TEXT,
    UNIQUE (cal_id, event_key)
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
CREATE TABLE IF NOT EXISTS series_copies (
    row_id       INTEGER,         -- watched_events row of a tracked repeating event
    instance_key TEXT,            -- the source occurrence's event id
    copy_id      TEXT,            -- its one-off copy on College
    start        TEXT,
    snapshot     TEXT,            -- JSON of the occurrence as copied (to notice it moving)
    missing      INTEGER DEFAULT 0,  -- scans in a row it wasn't found at the source
    PRIMARY KEY (row_id, instance_key)
);
CREATE TABLE IF NOT EXISTS plan_items (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_date     TEXT,
    work_key      TEXT,           -- "event:<DUE or exam event id>" or "task:<task id>"
    title         TEXT,
    kind          TEXT,           -- deadline / exam / task
    due           TEXT,
    list_id       TEXT,           -- the task's Google Tasks list (to tick it off or move it)
    minutes       INTEGER,        -- still to be given a time today
    tg_message_id INTEGER,        -- its "when?" message
    status        TEXT,           -- open / booked / skipped
    reminded      INTEGER DEFAULT 0,
    sent_at       TEXT,           -- when its "when?" message went out (the reminder counts from here)
    created_at    TEXT,
    UNIQUE (plan_date, work_key)
);
CREATE TABLE IF NOT EXISTS booked_blocks (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id       INTEGER,
    event_id      TEXT,           -- the block on the Planner calendar
    work_key      TEXT,
    title         TEXT,
    start         TEXT,
    end           TEXT,
    status        TEXT,           -- booked / asked / done / partly / notdone / cleared
    tg_message_id INTEGER,        -- the "Did you finish?" message
    created_at    TEXT
);
CREATE TABLE IF NOT EXISTS settings (
    key        TEXT PRIMARY KEY,  -- dotted config key, e.g. "planner.max_work_hours_per_day"
    value      TEXT,              -- JSON; laid over config.yaml by config.load_config()
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS habits (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT,
    minutes      INTEGER,
    days         TEXT,            -- "mon,wed,fri"
    window_start TEXT,            -- "17:00"
    window_end   TEXT,
    active       INTEGER DEFAULT 1,
    created_at   TEXT
);
CREATE TABLE IF NOT EXISTS sender_prefs (
    sender     TEXT PRIMARY KEY,
    pref       TEXT,             -- asked / blocked / keep
    updated_at TEXT
);
"""


def dedupe_key(item):
    """Same title (ignoring case/punctuation) at the same minute = the same item, wherever it came from."""
    title = re.sub(r"[^a-z0-9]+", " ", item["title"].lower()).strip()
    when = item.get("due") or item["start"]
    stamp = when.isoformat(timespec="minutes") if isinstance(when, datetime) else when.isoformat()
    return f"{title}|{stamp}"


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
        self._migrate()

    def _migrate(self):
        """Adds columns introduced after a table was first created (CREATE TABLE IF NOT EXISTS won't)."""
        added = {"watched_events": [("review", "TEXT"), ("review_batch", "INTEGER")],
                 "processed_messages": [("subject", "TEXT")],
                 "plan_items": [("sent_at", "TEXT"), ("window_start", "TEXT")],
                 "booked_blocks": [("headsup", "INTEGER DEFAULT 0"), ("calendar", "TEXT")]}
        for table, columns in added.items():
            have = {r[1] for r in self.db.execute(f"PRAGMA table_info({table})")}
            for name, kind in columns:
                if name not in have:
                    self.db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {kind}")
        self.db.commit()

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

    def mark_processed(self, msg_id, fingerprint, outcome, subject=None):
        self.db.execute("INSERT OR REPLACE INTO processed_messages (msg_id, fingerprint, outcome, processed_at, subject) "
                        "VALUES (?, ?, ?, ?, ?)", (msg_id, fingerprint, outcome, _now(), subject))
        self.db.commit()

    SKIPPED_OUTCOMES = ("no-keyword", "unreadable")

    def skipped_messages(self, limit=10, since=None):
        """Emails the filter (or an unreadable model answer) left out, newest first: [{msg_id, subject, outcome, ...}]."""
        sql = (f"SELECT * FROM processed_messages WHERE outcome IN ({','.join('?' * len(self.SKIPPED_OUTCOMES))}) "
               "AND subject IS NOT NULL")
        args = list(self.SKIPPED_OUTCOMES)
        if since:
            sql, args = sql + " AND processed_at >= ?", args + [since.astimezone(timezone.utc).isoformat(timespec="seconds")]
        return [dict(r) for r in self.db.execute(sql + " ORDER BY processed_at DESC LIMIT ?", args + [limit])]

    def forget_processed(self, msg_id):
        """So "Read anyway" can process an email again."""
        self.db.execute("DELETE FROM processed_messages WHERE msg_id = ?", (msg_id,))
        self.db.execute("DELETE FROM failures WHERE msg_id = ?", (msg_id,))
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

    def todo_task_ids(self):
        return {r["task_id"] for r in self.db.execute("SELECT task_id FROM todos WHERE status = 'active'")}

    def mark_todos_undone(self, batch):
        self.db.execute("UPDATE todos SET status = 'undone' WHERE batch = ?", (batch,))
        self.db.commit()

    # --- events watched on your other calendars (calwatch.py) ---------------------------------

    WATCH_FIELDS = {"title", "start", "snapshot", "status", "copy_id", "tg_message_id", "batch", "decided_at",
                    "review", "review_batch"}

    def watch_get(self, cal_id, event_key):
        row = self.db.execute("SELECT * FROM watched_events WHERE cal_id = ? AND event_key = ?", (cal_id, event_key)).fetchone()
        return dict(row) if row else None

    def watch_row(self, row_id):
        row = self.db.execute("SELECT * FROM watched_events WHERE id = ?", (row_id,)).fetchone()
        return dict(row) if row else None

    def watch_add(self, cal_id, event_key, is_series, title, start, snapshot, status, batch=None):
        cur = self.db.execute(
            "INSERT INTO watched_events (cal_id, event_key, is_series, title, start, snapshot, status, batch, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (cal_id, event_key, int(is_series), title, start, json.dumps(snapshot), status, batch, _now()))
        self.db.commit()
        return cur.lastrowid

    def watch_set(self, row_id, **fields):
        bad = set(fields) - self.WATCH_FIELDS
        if bad:
            raise ValueError(f"unknown watched_events fields: {bad}")
        if "snapshot" in fields and not isinstance(fields["snapshot"], str):
            fields["snapshot"] = json.dumps(fields["snapshot"])
        cols = ", ".join(f"{k} = ?" for k in fields)
        self.db.execute(f"UPDATE watched_events SET {cols} WHERE id = ?", (*fields.values(), row_id))
        self.db.commit()

    def watch_set_if(self, row_id, expected_status, **fields):
        """watch_set, but only if the row's status is still `expected_status` (a tap in the listener may have
        decided it while a scan was running). Returns True if it was updated."""
        bad = set(fields) - self.WATCH_FIELDS
        if bad:
            raise ValueError(f"unknown watched_events fields: {bad}")
        if "snapshot" in fields and not isinstance(fields["snapshot"], str):
            fields["snapshot"] = json.dumps(fields["snapshot"])
        cols = ", ".join(f"{k} = ?" for k in fields)
        cur = self.db.execute(f"UPDATE watched_events SET {cols} WHERE id = ? AND status = ?",
                              (*fields.values(), row_id, expected_status))
        self.db.commit()
        return cur.rowcount == 1

    def watch_rows(self, cal_id=None, statuses=None, batch=None, review=None, review_batch=None, message_id=None):
        """review=True: anything waiting for the daily review; a string: that kind only."""
        sql, args = "SELECT * FROM watched_events WHERE 1=1", []
        if message_id is not None:
            sql, args = sql + " AND tg_message_id = ?", args + [message_id]
        if review is True:
            sql += " AND review IS NOT NULL AND review != ''"
        elif review:
            sql, args = sql + " AND review = ?", args + [review]
        if review_batch is not None:
            sql, args = sql + " AND review_batch = ?", args + [review_batch]
        if cal_id is not None:
            sql, args = sql + " AND cal_id = ?", args + [cal_id]
        if batch is not None:
            sql, args = sql + " AND batch = ?", args + [batch]
        if statuses:
            sql += f" AND status IN ({','.join('?' * len(statuses))})"
            args += list(statuses)
        return [dict(r) for r in self.db.execute(sql + " ORDER BY start", args)]

    # --- one-off copies of a tracked repeating event's upcoming occurrences ---------------------

    def series_copies(self, row_id):
        return [dict(r) for r in self.db.execute("SELECT * FROM series_copies WHERE row_id = ? ORDER BY start", (row_id,))]

    def series_copy_set(self, row_id, instance_key, copy_id, start, snapshot, missing=0):
        self.db.execute("INSERT OR REPLACE INTO series_copies VALUES (?, ?, ?, ?, ?, ?)",
                        (row_id, instance_key, copy_id, start, json.dumps(snapshot), missing))
        self.db.commit()

    def series_copy_missing(self, row_id, instance_key, missing):
        self.db.execute("UPDATE series_copies SET missing = ? WHERE row_id = ? AND instance_key = ?",
                        (missing, row_id, instance_key))
        self.db.commit()

    def series_copy_delete(self, row_id, instance_key):
        self.db.execute("DELETE FROM series_copies WHERE row_id = ? AND instance_key = ?", (row_id, instance_key))
        self.db.commit()

    # --- today's tasks waiting for a time, and the slots you booked (slotpicker.py) ----------------

    PLAN_ITEM_FIELDS = {"title", "due", "list_id", "minutes", "tg_message_id", "status", "reminded", "kind", "sent_at",
                        "window_start"}
    BLOCK_FIELDS = {"status", "tg_message_id", "event_id", "headsup", "start", "end", "calendar"}

    def plan_item_upsert(self, day, work_key, title, kind, due, list_id, minutes):
        """Today's row for a task: created open, or its title/due/minutes refreshed. Returns the row."""
        row = self.db.execute("SELECT * FROM plan_items WHERE plan_date = ? AND work_key = ?",
                              (day.isoformat(), work_key)).fetchone()
        if row is None:
            self.db.execute("INSERT INTO plan_items (plan_date, work_key, title, kind, due, list_id, minutes, status, "
                            "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'open', ?)",
                            (day.isoformat(), work_key, title, kind, due, list_id, minutes, _now()))
        else:
            self.db.execute("UPDATE plan_items SET title = ?, due = ?, minutes = ? WHERE id = ?",
                            (title, due, minutes, row["id"]))
        self.db.commit()
        return dict(self.db.execute("SELECT * FROM plan_items WHERE plan_date = ? AND work_key = ?",
                                    (day.isoformat(), work_key)).fetchone())

    def plan_item(self, item_id):
        row = self.db.execute("SELECT * FROM plan_items WHERE id = ?", (item_id,)).fetchone()
        return dict(row) if row else None

    def plan_items(self, day, statuses=None):
        sql, args = "SELECT * FROM plan_items WHERE plan_date = ?", [day.isoformat()]
        if statuses:
            sql += f" AND status IN ({','.join('?' * len(statuses))})"
            args += list(statuses)
        return [dict(r) for r in self.db.execute(sql + " ORDER BY due, id", args)]

    def plan_item_set(self, item_id, **fields):
        bad = set(fields) - self.PLAN_ITEM_FIELDS
        if bad:
            raise ValueError(f"unknown plan_items fields: {bad}")
        self.db.execute(f"UPDATE plan_items SET {', '.join(f'{k} = ?' for k in fields)} WHERE id = ?",
                        (*fields.values(), item_id))
        self.db.commit()

    def block_add(self, item_id, event_id, work_key, title, start, end, calendar=None):
        cur = self.db.execute("INSERT INTO booked_blocks (item_id, event_id, work_key, title, start, end, status, "
                              "created_at, calendar) VALUES (?, ?, ?, ?, ?, ?, 'booked', ?, ?)",
                              (item_id, event_id, work_key, title, start.isoformat(), end.isoformat(), _now(), calendar))
        self.db.commit()
        return cur.lastrowid

    def block(self, block_id):
        row = self.db.execute("SELECT * FROM booked_blocks WHERE id = ?", (block_id,)).fetchone()
        return dict(row) if row else None

    def blocks(self, statuses=None, item_id=None, work_key=None):
        sql, args = "SELECT * FROM booked_blocks WHERE 1=1", []
        if statuses:
            sql += f" AND status IN ({','.join('?' * len(statuses))})"
            args += list(statuses)
        if item_id is not None:
            sql, args = sql + " AND item_id = ?", args + [item_id]
        if work_key is not None:
            sql, args = sql + " AND work_key = ?", args + [work_key]
        return [dict(r) for r in self.db.execute(sql + " ORDER BY start", args)]

    def block_set(self, block_id, **fields):
        bad = set(fields) - self.BLOCK_FIELDS
        if bad:
            raise ValueError(f"unknown booked_blocks fields: {bad}")
        self.db.execute(f"UPDATE booked_blocks SET {', '.join(f'{k} = ?' for k in fields)} WHERE id = ?",
                        (*fields.values(), block_id))
        self.db.commit()

    def block_answers(self):
        """{planner event id: done / partly / notdone} for blocks you've answered "Did you finish?" about."""
        return {r["event_id"]: r["status"] for r in self.db.execute(
            "SELECT event_id, status FROM booked_blocks WHERE status IN ('done', 'partly', 'notdone')")}

    def block_by_event(self, event_id):
        row = self.db.execute("SELECT * FROM booked_blocks WHERE event_id = ? ORDER BY id DESC", (event_id,)).fetchone()
        return dict(row) if row else None

    # --- settings changed from Telegram / the web page (settings.py) ---------------------------------

    def settings(self):
        return {r["key"]: json.loads(r["value"]) for r in self.db.execute("SELECT key, value FROM settings")}

    def set_setting(self, key, value):
        self.db.execute("INSERT OR REPLACE INTO settings VALUES (?, ?, ?)", (key, json.dumps(value), _now()))
        self.db.commit()

    def clear_setting(self, key):
        self.db.execute("DELETE FROM settings WHERE key = ?", (key,))
        self.db.commit()

    # --- habits (habits.py) --------------------------------------------------------------------------

    def habits(self, active_only=False):
        sql = "SELECT * FROM habits" + (" WHERE active = 1" if active_only else "") + " ORDER BY id"
        return [dict(r) for r in self.db.execute(sql)]

    def habit(self, habit_id):
        row = self.db.execute("SELECT * FROM habits WHERE id = ?", (habit_id,)).fetchone()
        return dict(row) if row else None

    def habit_add(self, name, minutes, days, window_start, window_end):
        cur = self.db.execute("INSERT INTO habits (name, minutes, days, window_start, window_end, active, created_at) "
                              "VALUES (?, ?, ?, ?, ?, 1, ?)", (name, minutes, days, window_start, window_end, _now()))
        self.db.commit()
        return cur.lastrowid

    def habit_set(self, habit_id, **fields):
        allowed = {"name", "minutes", "days", "window_start", "window_end", "active"}
        if set(fields) - allowed:
            raise ValueError(f"unknown habits fields: {set(fields) - allowed}")
        self.db.execute(f"UPDATE habits SET {', '.join(f'{k} = ?' for k in fields)} WHERE id = ?",
                        (*fields.values(), habit_id))
        self.db.commit()

    def habit_delete(self, habit_id):
        self.db.execute("DELETE FROM habits WHERE id = ?", (habit_id,))
        self.db.commit()

    # --- a multi-step conversation (typing a value after a button) ------------------------------------

    CONV_MINUTES = 15

    def conv(self, now):
        """The conversation waiting for your next message ({"flow", ...}), or None (none, or it expired)."""
        raw = self.get_meta("conv")
        if not raw:
            return None
        data = json.loads(raw)
        if datetime.fromisoformat(data["expires"]) < now:
            self.set_meta("conv", "")
            return None
        return data

    def set_conv(self, now, flow, **data):
        data = {k: v for k, v in data.items() if k not in ("flow", "expires")}
        self.set_meta("conv", json.dumps({"flow": flow, "expires": (now + timedelta(minutes=self.CONV_MINUTES)).isoformat(),
                                          **data}))

    def clear_conv(self):
        self.set_meta("conv", "")

    def watch_statuses(self):
        """{(cal_id, event_key): status} for everything the watcher knows about."""
        return {(r["cal_id"], r["event_key"]): r["status"] for r in self.db.execute("SELECT cal_id, event_key, status FROM watched_events")}

    def next_watch_batch(self):
        return (self.db.execute("SELECT COALESCE(MAX(batch), 0) FROM watched_events").fetchone()[0] or 0) + 1

    def pending_or_created(self, dedupe_key):
        """True if this item already came in by email (card sent or added)."""
        return self.item_exists(dedupe_key) or self.pending_exists(dedupe_key)

    def delete_item_by_event(self, event_id):
        """Forget a created item (used when a deadline is undone), so it could be added again later."""
        self.db.execute("DELETE FROM created_items WHERE event_id = ?", (event_id,))
        self.db.commit()


def read_settings(path=None):
    """Settings overrides without creating or migrating anything (config.load_config uses this)."""
    path = Path(path or DB_PATH)
    if not path.exists():
        return {}
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
            return {k: json.loads(v) for k, v in db.execute("SELECT key, value FROM settings")}
    except sqlite3.Error:
        return {}
