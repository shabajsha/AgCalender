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

    import requests

    import logsetup
    import planner
    import state
    import telegram_bot

    monkeypatch.setattr(state, "DB_PATH", tmp_path / "state.db")
    monkeypatch.setattr(logsetup, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(planner, "LOCK_FILE", tmp_path / ".planner.lock")
    monkeypatch.setattr(telegram_bot.Telegram, "call", _blocked)
    monkeypatch.setattr(requests, "post", _blocked)
    monkeypatch.setattr(requests, "get", _blocked)
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
        self.sent, self.edits, self.answers, self._next = [], {}, [], 100

    def send(self, text, buttons=None, keyboard=None):
        self._next += 1
        self.sent.append({"id": self._next, "text": text, "buttons": buttons, "keyboard": keyboard})
        return self._next

    def edit(self, message_id, text, buttons=None):
        self.edits[message_id] = {"text": text, "buttons": buttons}

    def answer(self, callback_id, text=""):
        self.answers.append(text)


@pytest.fixture
def tg():
    return FakeTelegram()


class FakeTasks:
    """Just enough of the Google Tasks client for todos.py: tasks().insert/delete(...).execute()."""

    def __init__(self, fail_on=None):
        self.store, self._n, self.fail_on = {}, 0, fail_on

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
        self.store.pop(task, None)
        return _Exec(None)


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
