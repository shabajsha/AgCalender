"""One-time Telegram setup: checks your bot token, finds your chat, saves both to telegram.json.

    python telegram_setup.py

Before running: in Telegram, message @BotFather, send /newbot, pick a name, and copy the token it gives you.
The token is typed in hidden and only ever written to telegram.json (chmod 600, gitignored).
"""
import getpass
import json
import os
import time

from telegram_bot import CRED_FILE, Telegram, TelegramError


def main():
    token = getpass.getpass("Paste the bot token from @BotFather (input is hidden): ").strip()
    tg = Telegram(token)
    try:
        bot = tg.call("getMe")
    except TelegramError as e:
        raise SystemExit(f"Token rejected ({e}). Copy it again from @BotFather.")

    print(f"Bot OK: @{bot['username']}")
    print(f"Now open Telegram, search for @{bot['username']}, press Start (or send any message). Waiting up to 3 minutes...")
    offset, deadline = None, time.time() + 180
    while time.time() < deadline:
        for update in tg.call("getUpdates", {"offset": offset, "timeout": 20}, http_timeout=35):
            offset = update["update_id"] + 1
            chat = (update.get("message") or {}).get("chat")
            if not chat:
                continue
            tg.call("getUpdates", {"offset": offset, "timeout": 0})  # mark it as read
            fd = os.open(CRED_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump({"token": token, "chat_id": chat["id"]}, f)
            Telegram(token, chat["id"]).send("Calendar agent connected. I'll ask you here before adding anything "
                                             "to your calendar.")
            name = f"{chat.get('first_name', '')} {chat.get('last_name', '')}".strip()
            print(f"Connected to {name}. Saved telegram.json - check Telegram for a test message.")
            return
    raise SystemExit("No message arrived in 3 minutes. Run this again and message the bot while it waits.")


if __name__ == "__main__":
    main()
