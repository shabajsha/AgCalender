"""Tells you when something needs attention - on Telegram, else as a desktop popup - at most once per problem
every few hours, so a problem that persists across many timer runs doesn't flood you.
"""
import functools
import logging
import subprocess
import sys
from datetime import datetime, timedelta, timezone

from google.auth.exceptions import RefreshError

from auth import AuthExpired
from state import State
from telegram_bot import Telegram, TelegramError

log = logging.getLogger(__name__)
REPEAT_AFTER = timedelta(hours=6)
AUTH_TEXT = ("Google login expired or was revoked, so mail, calendar and tasks are paused. "
             "Fix: open a terminal and run  cd ~/Documents/calender-agent && venv/bin/python auth.py")


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


def guard(name):
    """Decorator for a script's main(): login problems and crashes become an alert instead of a silent log line."""
    def wrap(main):
        @functools.wraps(main)
        def run(*args, **kwargs):
            try:
                result = main(*args, **kwargs)
            except (AuthExpired, RefreshError):
                alert("auth", AUTH_TEXT)
                sys.exit(1)
            except Exception as e:
                log.exception("%s crashed", name)
                alert(f"crash:{name}", f"{name} failed: {type(e).__name__}: {e}. Details in logs/{name}.log")
                raise
            resolved("auth")
            return result
        return run
    return wrap
