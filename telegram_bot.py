"""Minimal Telegram Bot API client: send messages with buttons, edit them, and long-poll for taps.

Credentials live in telegram.json ({"token": ..., "chat_id": ...}), created by telegram_setup.py. It is secret
and gitignored. Errors raised here never include the token (it is part of every request URL).
"""
import json
import time
from pathlib import Path

import requests

CRED_FILE = Path(__file__).parent / "telegram.json"
API = "https://api.telegram.org/bot{token}/{method}"
MAX_TEXT = 4000        # Telegram's limit is 4096 characters per message; leave room
MAX_RETRY_AFTER_S = 30  # wait this long at most when Telegram says "too many requests"
# Editing a message that no longer exists (you deleted it, or cleared the chat) must not stop anything.
GONE_MESSAGE = ("message to edit not found", "message can't be edited", "message is not modified")


class TelegramError(Exception):
    pass


class Telegram:
    def __init__(self, token, chat_id=None):
        self.token = token
        self.chat_id = chat_id

    @classmethod
    def from_file(cls):
        if not CRED_FILE.exists():
            raise TelegramError("telegram.json not found - run: python telegram_setup.py")
        data = json.loads(CRED_FILE.read_text())
        return cls(data["token"], data["chat_id"])

    def call(self, method, params=None, http_timeout=30, _retried=False):
        try:
            resp = requests.post(API.format(token=self.token, method=method), json=params or {}, timeout=http_timeout)
            data = resp.json()
        except (requests.RequestException, ValueError) as e:
            # requests' messages contain the URL, and therefore the token: never pass them on.
            raise TelegramError(f"{method}: network error ({type(e).__name__})") from None
        if not data.get("ok"):
            wait = (data.get("parameters") or {}).get("retry_after")
            if data.get("error_code") == 429 and wait and wait <= MAX_RETRY_AFTER_S and not _retried:
                time.sleep(wait)  # sent many cards at once: Telegram asks us to slow down
                return self.call(method, params, http_timeout, _retried=True)
            raise TelegramError(f"{method}: {data.get('description', 'unknown error')}")
        return data["result"]

    @staticmethod
    def _inline(buttons):
        """[(label, data), ...] is one row; [[(label, data), ...], [...]] is several rows."""
        rows = buttons if buttons and isinstance(buttons[0], list) else [buttons]
        return {"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in row] for row in rows]}

    def send(self, text, buttons=None, keyboard=None):
        """buttons: inline buttons under the message. keyboard: rows of labels for the permanent button bar
        at the bottom of the chat (tapping one sends its label as a message). Returns the message id (of the
        last part: a text longer than Telegram allows is split at line breaks, buttons go on the last part)."""
        parts = split_text(text)
        for part in parts[:-1]:
            self.call("sendMessage", {"chat_id": self.chat_id, "text": part})
        params = {"chat_id": self.chat_id, "text": parts[-1]}
        if buttons:
            params["reply_markup"] = self._inline(buttons)
        elif keyboard:
            params["reply_markup"] = {"keyboard": [[{"text": label} for label in row] for row in keyboard],
                                      "resize_keyboard": True, "is_persistent": True}
        return self.call("sendMessage", params)["message_id"]

    def edit(self, message_id, text, buttons=None):
        """Replaces a message's text; its buttons are removed unless new ones are given. Returns False if the
        message is gone (deleted in the chat) instead of raising: an old message must never block new work."""
        if len(text) > MAX_TEXT:
            text = text[:MAX_TEXT - 20].rsplit("\n", 1)[0] + "\n..."
        params = {"chat_id": self.chat_id, "message_id": message_id, "text": text}
        if buttons:
            params["reply_markup"] = self._inline(buttons)
        try:
            self.call("editMessageText", params)
        except TelegramError as e:
            if not any(g in str(e) for g in GONE_MESSAGE):
                raise
            return "not modified" in str(e)
        return True

    def answer(self, callback_id, text=""):
        """Stops the spinner on a tapped button (and shows a short toast). The listener answers every tap
        immediately, so a later answer for the same tap fails; that's harmless and ignored."""
        try:
            self.call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text})
        except TelegramError:
            pass

    def updates(self, offset=None, wait=50):
        """Long-polls: returns as soon as there is a tap, or after `wait` seconds with []."""
        return self.call("getUpdates", {"offset": offset, "timeout": wait, "allowed_updates": ["callback_query", "message"]},
                         http_timeout=wait + 15)

    def set_commands(self, commands):
        """commands: [(name, description), ...] shown in the bot's menu button."""
        self.call("setMyCommands", {"commands": [{"command": c, "description": d} for c, d in commands]})


def split_text(text, limit=MAX_TEXT):
    """Pieces of at most `limit` characters, cut at line breaks where possible."""
    parts = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        cut = cut if cut > limit // 2 else limit
        parts.append(text[:cut].rstrip("\n"))
        text = text[cut:].lstrip("\n")
    return parts + [text]
