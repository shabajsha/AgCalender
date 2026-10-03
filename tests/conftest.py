"""Shared test fixtures. The autouse `isolated` fixture guarantees no test touches the real state.db, logs,
Telegram, Google, Ollama, systemd or the desktop: any such call raises instead of quietly happening."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

TZ = ZoneInfo("Asia/Kolkata")


def at(y, mo, d, h=0, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=TZ)


class Blocked(RuntimeError):
    pass


def _blocked(*args, **kwargs):
    raise Blocked("outside access is disabled in tests - mock it")


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    import subprocess

    import httplib2
    import httpx
    import requests

    import auth
    import calwatch
    import logsetup
    import morning
    import planner
    import state
    import telegram_bot

    monkeypatch.setattr(state, "DB_PATH", tmp_path / "state.db")
    monkeypatch.setattr(logsetup, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(planner, "LOCK_FILE", tmp_path / ".planner.lock")
    monkeypatch.setattr(calwatch, "LOCK_FILE", tmp_path / ".calwatch.lock")
    monkeypatch.setattr(morning, "BACKUP_DIR", tmp_path / "backups")
    monkeypatch.setattr(auth, "TOKEN_FILE", tmp_path / "no-token.json")   # never the real login
    monkeypatch.setattr(telegram_bot.Telegram, "call", _blocked)
    monkeypatch.setattr(requests, "post", _blocked)
    monkeypatch.setattr(requests, "get", _blocked)
    monkeypatch.setattr(httplib2.Http, "request", _blocked)                # Google APIs
    monkeypatch.setattr(httpx.Client, "send", _blocked)                     # Ollama
    monkeypatch.setattr(subprocess, "run", _blocked)
    monkeypatch.setattr(subprocess, "Popen", _blocked)
    return tmp_path


@pytest.fixture
def db(tmp_path):
    from state import State
    return State(path=tmp_path / "test.db")


class FakeTelegram:
    chat_id = 42

    def __init__(self):
        self.sent, self.edits, self.answers, self._next, self._answered = [], {}, [], 100, set()

    def send(self, text, buttons=None, keyboard=None):
        self._next += 1
        self.sent.append({"id": self._next, "text": text, "buttons": buttons, "keyboard": keyboard})
        return self._next

    def edit(self, message_id, text, buttons=None):
        self.edits[message_id] = {"text": text, "buttons": buttons}
        return True

    def answer(self, callback_id, text=""):
        # Telegram accepts one answer per tap; a second one is silently lost, so tests must never need one.
        assert callback_id not in self._answered, f"tap {callback_id!r} answered twice ({text!r})"
        self._answered.add(callback_id)
        self.answers.append(text)


@pytest.fixture
def tg():
    return FakeTelegram()


class FakeTasks:
    """Just enough of the Google Tasks client for todos.py: tasks().insert/delete(...).execute()."""

    def __init__(self, fail_on=None):
        self.store, self._n, self.fail_on, self.patched, self.deleted = {}, 0, fail_on, [], []

    def tasks(self):
        return self

    def insert(self, tasklist, body):
        self._n += 1
        if self.fail_on == self._n:
            raise RuntimeError("Tasks API error")
        task_id = f"t{self._n}"
        self.store[task_id] = body
        return _Exec({"id": task_id})

    def delete(self, tasklist, task):
        self.deleted.append((tasklist, task))
        self.store.pop(task, None)
        return _Exec(None)

    def patch(self, tasklist, task, body):
        self.patched.append((tasklist, task, body))
        return _Exec({})


class _Exec:
    def __init__(self, value):
        self.value = value

    def execute(self):
        return self.value


@pytest.fixture
def fake_tasks():
    return FakeTasks()


def event(event_id, start, end, summary, **extra):
    return {"id": event_id, "summary": summary, "start": {"dateTime": start.isoformat()},
            "end": {"dateTime": end.isoformat()}, **extra}


def minutes(delta):
    return delta / timedelta(minutes=1)


class FakeCalendar:
    """Just enough of the Google Calendar client for calwatch/google_writer: calendarList().list,
    events().list/get/insert/patch/delete. `evs` (the `events=` argument) maps calendar id -> list of event dicts (instances),
    `masters` maps calendar id -> {series id: master event}."""

    def __init__(self, calendars, events=None, masters=None):
        self.calendars, self.evs, self.masters = calendars, events or {}, masters or {}
        self.inserted, self.patched, self.deleted, self._n = {}, [], [], 0

    def calendarList(self):
        return _Lister(lambda: {"items": self.calendars})

    def events(self):
        return _Events(self)


class _Lister:
    def __init__(self, fn):
        self.fn = fn

    def list(self, **kwargs):
        return _Exec(self.fn())


class _Events:
    def __init__(self, fake):
        self.fake = fake

    def list(self, calendarId, **kwargs):
        return _Exec({"items": list(self.fake.evs.get(calendarId, []))})

    def get(self, calendarId, eventId):
        from googleapiclient.errors import HttpError
        import httplib2
        pool = self.fake.evs.get(calendarId, []) + list(self.fake.masters.get(calendarId, {}).values())
        pool += [{**body, "id": cid} for cid, (cal, body) in self.fake.inserted.items()
                 if cal == calendarId and (calendarId, cid) not in self.fake.deleted]
        for ev in pool:
            if ev["id"] == eventId:
                return _Exec(ev)
        raise HttpError(httplib2.Response({"status": 404}), b"not found")

    def instances(self, calendarId, eventId, **kwargs):
        return _Exec({"items": [e for e in self.fake.evs.get(calendarId, []) if e.get("recurringEventId") == eventId]})

    def insert(self, calendarId, body):
        self.fake._n += 1
        event_id = f"copy{self.fake._n}"
        self.fake.inserted[event_id] = (calendarId, body)
        return _Exec({"id": event_id})

    def patch(self, calendarId, eventId, body):
        self.fake.patched.append((calendarId, eventId, body))
        return _Exec({})

    def delete(self, calendarId, eventId):
        self.fake.deleted.append((calendarId, eventId))
        return _Exec(None)
