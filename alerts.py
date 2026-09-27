"""Tells you when something needs attention - on Telegram, else as a desktop popup - at most once per problem
every few hours, so a problem that persists across many timer runs doesn't flood you.
"""
import functools
import logging
import socket
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import httplib2
import requests
from google.auth.exceptions import RefreshError, TransportError
from googleapiclient.errors import HttpError

from auth import AuthExpired
from state import State
from telegram_bot import Telegram, TelegramError

log = logging.getLogger(__name__)
REPEAT_AFTER = timedelta(hours=6)
OFFLINE_ALERT_AFTER = timedelta(hours=6)
AUTH_TEXT = ("Google login expired or was revoked, so mail, calendar and tasks are paused. "
             "Fix: open a terminal and run  cd ~/Documents/calender-agent && venv/bin/python auth.py")


def is_offline_error(exc):
    """No network / Google temporarily unavailable: retry at the next run instead of calling it a crash.
    (A laptop that just woke up, or lost Wi-Fi, used to produce 'ingest crashed' alerts.)"""
    if isinstance(exc, HttpError):
        return exc.resp.status in (429, 500, 502, 503, 504)
    if isinstance(exc, TelegramError):
        return "network error" in str(exc)
    return isinstance(exc, (TransportError, httplib2.HttpLib2Error, requests.RequestException, socket.gaierror,
                            TimeoutError, ConnectionError))


def alert(key, text, state=None):
    """Sends `text` unless the same `key` was alerted within REPEAT_AFTER. Returns True if sent."""
    state = state or State()
    meta_key = f"alert:{key}"
    now = datetime.now(timezone.utc)
    last = state.get_meta(meta_key)
    if last and now - datetime.fromisoformat(last) < REPEAT_AFTER:
        return False
    log.error("ALERT [%s] %s", key, text)
    try:
        Telegram.from_file().send(f"Calendar agent needs attention:\n{text}")
    except (TelegramError, OSError, ValueError, KeyError):
        from notifiers import fullscreen_active
        if fullscreen_active():
            return False  # no popup over a game; not marked as sent, so it's tried again next time
        try:
            subprocess.run(["notify-send", "--app-name=Calendar agent", "--urgency=critical",
                            "Calendar agent needs attention", text], timeout=15)
        except (OSError, subprocess.TimeoutExpired):
            pass
    state.set_meta(meta_key, now.isoformat())
    return True


def resolved(key, state=None):
    """Forget an alert once the problem is gone, so a new occurrence alerts right away."""
    (state or State()).set_meta(f"alert:{key}", "")


def _offline(name, exc):
    """Logs one line; only if the network has been gone for hours does it tell you (the desktop popup
    works offline)."""
    state = State()
    now = datetime.now(timezone.utc)
    since = state.get_meta(f"offline_since:{name}")
    if not since:
        state.set_meta(f"offline_since:{name}", now.isoformat())
        since = now.isoformat()
    log.warning("%s: offline or Google unavailable (%s); will retry at the next run", name, type(exc).__name__)
    if now - datetime.fromisoformat(since) >= OFFLINE_ALERT_AFTER:
        alert("offline", f"No internet (or Google unreachable) for {OFFLINE_ALERT_AFTER.seconds // 3600}+ hours, "
                         "so mail and calendar checks are waiting.", state)


def guard(name):
    """Decorator for a script's main(): login problems and crashes become an alert instead of a silent log line.
    Being offline is not a crash: it's logged and retried at the next run."""
    def wrap(main):
        @functools.wraps(main)
        def run(*args, **kwargs):
            try:
                result = main(*args, **kwargs)
            except (AuthExpired, RefreshError):
                alert("auth", AUTH_TEXT)
                sys.exit(1)
            except Exception as e:
                if is_offline_error(e):
                    _offline(name, e)
                    sys.exit(0)
                log.exception("%s crashed", name)
                alert(f"crash:{name}", f"{name} failed: {type(e).__name__}: {e}. Details in logs/{name}.log")
                raise
            state = State()
            for key in ("alert:auth", f"alert:crash:{name}", f"offline_since:{name}"):
                if state.get_meta(key):
                    state.set_meta(key, "")
            if state.get_meta("alert:offline"):
                state.set_meta("alert:offline", "")
            return result
        return run
    return wrap
