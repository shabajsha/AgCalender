"""Asks you on Telegram before anything is added, and acts on your Add / Skip taps.

    python approvals.py     # normally runs as the calendar-approvals systemd service

ingest.py calls ask() for each new item. This listener long-polls Telegram (near-zero CPU while idle),
creates the items you approve, marks the ones you skip, and expires items whose date has passed.
After `approval.learn_after_skips` skips (and no adds) from one sender, it offers to stop asking about them.
It also answers commands (only from your chat): /todo, /check, /plan, /clear, /pause, /resume, /status, the same
actions from the button bar, and the morning check-in (plain replies become to-dos while it's open).
"""
import logging
import subprocess
import sys
import time
from datetime import datetime, time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

from googleapiclient.discovery import build

import google_writer
import morning
import todos
from auth import get_credentials
from config import load_config
from state import State
from telegram_bot import Telegram, TelegramError

LOG_DIR = Path(__file__).parent / "logs"
EXPIRE_EVERY_S = 600
COMMANDS = [("todo", "Add to-dos for today, e.g. /todo Lab report 2h"),
            ("check", "Check mail now instead of waiting for the next 30-min run"),
            ("plan", "Plan the rest of today (habits + work blocks)"),
            ("clear", "Remove today's planned blocks"),
            ("pause", "Stop reading mail until /resume"),
            ("resume", "Start reading mail again (checks right away)"),
            ("status", "Is mail reading on? Last check, waiting cards")]
# Permanent button bar at the bottom of the chat; each label maps to a command.
KEYBOARD = [["Check mail now", "Plan rest of today"], ["Status", "Pause", "Resume"]]
LABELS = {"check mail now": "/check", "plan rest of today": "/plan", "status": "/status",
          "pause": "/pause", "resume": "/resume"}
OFFLINE_AFTER_S = 300  # a command older than this was sent while the laptop was off or asleep
log = logging.getLogger("approvals")


def _when(item):
    return item["due"] or item["start"]


def format_item(item, subject, sender):
    when = _when(item)
    stamp = when.strftime("%a %d %b, %H:%M") if isinstance(when, datetime) else when.strftime("%a %d %b (all day)")
    kind = {"deadline": "Deadline", "meeting": "Meeting", "event": "Event"}[item["type"]]
    lines = [f"{kind}: {item['title']}", stamp]
    if item.get("course"):
        lines.append(f"Course: {item['course']}")
    lines += [f"From: {sender}", f"Email: {subject}"]
    if item["type"] == "deadline":
        lines.append("Add = calendar event + task")
    return "\n".join(lines)


def ask(tg, state, key, item, msg, sender):
    """Queues an item and sends it to Telegram with Add / Skip buttons."""
    pending_id = state.add_pending(key, item, msg, sender)
    try:
        message_id = tg.send(format_item(item, msg["subject"], sender),
                             [("Add", f"add:{pending_id}"), ("Skip", f"skip:{pending_id}")])
    except TelegramError:
        state.delete_pending(pending_id)  # so the next run asks again
        raise
    state.set_pending_message(pending_id, message_id)


def _is_past(item, now):
    when = _when(item)
    if not isinstance(when, datetime):
        when = datetime.combine(when, dtime(23, 59), now.tzinfo)
    return when < now


class Listener:
    def __init__(self, cfg, state, tg):
        self.cfg, self.state, self.tg = cfg, state, tg
        creds = get_credentials()
        self.calendar = build("calendar", "v3", credentials=creds)
        self.tasks = build("tasks", "v1", credentials=creds)
        self.learn_after = cfg.get("approval", {}).get("learn_after_skips", 3)

    def handle(self, cq):
        chat_id = cq.get("message", {}).get("chat", {}).get("id")
        if chat_id != self.tg.chat_id:
            log.warning("ignored a tap from unknown chat %s", chat_id)
            self.tg.answer(cq["id"], "Not allowed")
            return
        action, _, rest = cq.get("data", "").partition(":")
        if action == "checkin":  # "checkin:done" / "checkin:none" under the morning question
            self.checkin_answered(cq, rest)
            return
        if action == "undo":  # "undo:<batch>" under a to-do confirmation
            self.undo_todos(cq, rest)
            return
        if action == "clear":  # "clear:<YYYY-MM-DD>" under a plan summary
            started = self._run_planner("--clear", "--date", rest)
            self.tg.answer(cq["id"], "Clearing..." if started else "Couldn't start")
            return
        raw_id, _, arg = rest.partition(":")
        row = self.state.get_pending(int(raw_id)) if raw_id.isdigit() else None
        if row is None:
            self.tg.answer(cq["id"], "Unknown item")
            return
        if action == "effort":  # "effort:<pending id>:<hours>"
            self.effort(row, cq, arg)
            return
        {"add": self.add, "skip": self.skip, "block": self.block, "keep": self.keep}.get(
            action, lambda r, c: self.tg.answer(c["id"], "Unknown button"))(row, cq)

    def handle_message(self, message):
        if message.get("chat", {}).get("id") != self.tg.chat_id:
            log.warning("ignored a message from unknown chat %s", message.get("chat", {}).get("id"))
            return
        text = (message.get("text") or "").strip()
        # a button-bar label, or "/pause@AgCalenderBot" -> "/pause"
        command = LABELS.get(text.lower()) or (text.split()[0].split("@")[0].lower() if text else "")
        sent_at = message.get("date", time.time())
        if time.time() - sent_at > OFFLINE_AFTER_S and command in ("/check", "/plan", "/clear", "/todo"):
            stamp = datetime.fromtimestamp(sent_at, ZoneInfo(self.cfg["timezone"]))
            self.tg.send(f"Got your {command} from {stamp:%a %H:%M}. The laptop was off or asleep then; doing it now.")
        today = datetime.now(ZoneInfo(self.cfg["timezone"])).date()
        if command == "/todo":
            body = text.split(maxsplit=1)[1] if len(text.split(maxsplit=1)) > 1 else ""
            if body:
                self.add_todos(body, today)
            else:
                self.tg.send("Send /todo followed by the task, one per line, e.g.\n/todo Lab report 2h\nCall bank 15m")
        elif not text.startswith("/") and not LABELS.get(text.lower()) and morning.checkin_open(self.state, today):
            self.add_todos(text, today)
        elif command == "/check":
            if self.state.paused_since():
                self.tg.send("Mail reading is paused, so nothing was checked. Tap Resume (or send /resume) first.")
            elif self._start_ingest_now():
                self.tg.send("Checking mail now. Anything new will arrive here as a card.")
            else:
                self.tg.send("Couldn't start the mail check (is calendar-ingest.service installed?).")
        elif command == "/plan":
            self.tg.send("Planning the rest of today; the plan will arrive here shortly."
                         if self._run_planner() else "Couldn't start the planner.")
        elif command == "/clear":
            self.tg.send("Clearing today's plan..." if self._run_planner("--clear") else "Couldn't start the planner.")
        elif command == "/pause":
            already = self.state.paused_since()
            self.state.set_paused(True)
            self.tg.send("Already paused." if already else
                         "Paused. I won't read new mail until you send /resume.\n"
                         "Mail that arrives meanwhile is checked when you resume. Buttons on existing cards still work.")
            log.info("paused via Telegram")
        elif command == "/resume":
            was_paused = self.state.paused_since()
            self.state.set_paused(False)
            started = self._start_ingest_now() if was_paused else False
            self.tg.send("Mail reading wasn't paused." if not was_paused else
                         "Resumed. Checking the mail that arrived while paused now." if started else
                         "Resumed. The next mail check is within 30 minutes.")
            log.info("resumed via Telegram")
        elif command == "/status":
            self.tg.send(self.status_text())
        else:  # /start, /help or anything else: show what's possible, with the button bar
            self.tg.send("Commands (or use the buttons below):\n" + "\n".join(f"/{c} - {d}" for c, d in COMMANDS),
                         keyboard=KEYBOARD)

    def status_text(self):
        tz = ZoneInfo(self.cfg["timezone"])
        paused, last = self.state.paused_since(), self.state.get_last_run()
        lines = [f"Mail reading: PAUSED since {paused.astimezone(tz):%a %d %b %H:%M}" if paused else "Mail reading: on (every 30 min)",
                 f"Last mail check: {last.astimezone(tz):%a %d %b %H:%M}" if last else "Last mail check: never",
                 f"Cards waiting for your answer: {self.state.count_open_pending()}"]
        return "\n".join(lines)

    @staticmethod
    def _start_ingest_now():
        """Kicks off a mail check via systemd (no-op if the service isn't installed)."""
        try:
            return subprocess.run(["systemctl", "--user", "start", "--no-block", "calendar-ingest.service"],
                                  timeout=15, capture_output=True).returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False

    def add_todos(self, text, today):
        mc = self.cfg["morning"]
        parsed = todos.parse(text, mc["todo_default_minutes"])
        if not parsed:
            self.tg.send("I couldn't find a to-do in that. Send one per line, e.g. Lab report 2h")
            return
        batch, added = todos.add(self.tasks, mc["todo_tasklist"], self.state, today, parsed)
        lines = [f"- {title} ({todos.fmt_minutes(m)})" for title, m in added]
        buttons = [("Undo", f"undo:{batch}")]
        if morning.checkin_open(self.state, today):
            tail, buttons = "Anything else? Tap Done when you're finished.", buttons + [("Done", "checkin:done")]
        else:
            tail = "Tap 'Plan rest of today' to fit it into today's plan."
        self.tg.send("Added to DAILY TASKS (due today):\n" + "\n".join(lines) + "\n\n" + tail, buttons)
        log.info("added %d to-do(s)", len(added))

    def undo_todos(self, cq, raw_batch):
        if not raw_batch.isdigit():
            self.tg.answer(cq["id"], "Unknown button")
            return
        removed = todos.undo(self.tasks, self.cfg["morning"]["todo_tasklist"], self.state, int(raw_batch))
        self.tg.edit(cq["message"]["message_id"], ("Removed: " + ", ".join(removed)) if removed else "Already removed.")
        self.tg.answer(cq["id"], "Undone")

    def checkin_answered(self, cq, answer):
        today = datetime.now(ZoneInfo(self.cfg["timezone"])).date()
        self.state.set_meta(morning.CHECKIN_ANSWERED, today.isoformat())
        if self.state.get_meta(morning.MORNING_SENT) == today.isoformat():
            started, msg = self._run_planner(), "Re-planning the rest of today with your to-dos."
        else:
            started, msg = self._run_script("morning.py", "--finish"), "Got it. Planning your day now; the morning message follows shortly."
        self.tg.edit(cq["message"]["message_id"], msg if started else "Couldn't start the planner.")
        self.tg.answer(cq["id"], "Planning..." if started else "Error")
        log.info("check-in answered (%s)", answer)

    @classmethod
    def _run_planner(cls, *args):
        return cls._run_script("planner.py", *args)

    @staticmethod
    def _run_script(script, *args):
        """Starts a script as its own transient systemd unit, so restarting this listener can't kill it."""
        here = Path(__file__).parent
        cmd = ["systemd-run", "--user", "--no-block", "--collect", f"--working-directory={here}",
               sys.executable, str(here / script), *args]
        try:
            if subprocess.run(cmd, timeout=15, capture_output=True).returncode == 0:
                return True
        except (OSError, subprocess.TimeoutExpired):
            pass
        try:  # no systemd user session (e.g. run by hand): plain background process
            subprocess.Popen([sys.executable, str(here / script), *args], cwd=here, start_new_session=True,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
        except OSError:
            return False

    def _card(self, row):
        return format_item(row["item"], row["msg_subject"], row["sender"])

    def add(self, row, cq):
        if row["status"] != "pending":
            self.tg.answer(cq["id"], f"Already {row['status']}")
            return
        item, msg = row["item"], {"id": row["msg_id"], "subject": row["msg_subject"]}
        event_id, task_id = google_writer.create_item(self.calendar, self.tasks, self.cfg, item, msg)
        self.state.record_item(row["dedupe_key"], item["type"], event_id, task_id, item["title"],
                               str(_when(item)), row["msg_id"])
        self.state.set_pending_status(row["id"], "added")
        text = self._card(row) + "\n\nAdded to your calendar" + (" and tasks" if task_id else "")
        if item["type"] == "deadline":
            pc = self.cfg["planner"]
            choices = [(f"{h:g} h", f"effort:{row['id']}:{h}") for h in pc["effort_choices_hours"]]
            self.tg.edit(row["tg_message_id"], text + f"\nHow much work does it need? (default {pc['default_effort_hours']:g} h)",
                         [choices[:3], choices[3:]] if len(choices) > 3 else choices)
        else:
            self.tg.edit(row["tg_message_id"], text)
        self.tg.answer(cq["id"], "Added")
        log.info("added %r", item["title"])

    def effort(self, row, cq, arg):
        try:
            hours = float(arg)
        except ValueError:
            self.tg.answer(cq["id"], "Unknown button")
            return
        event_id = self.state.event_id_for(row["dedupe_key"])
        if not event_id:
            self.tg.answer(cq["id"], "Add it first")
            return
        self.state.set_effort(f"event:{event_id}", hours)
        self.tg.edit(row["tg_message_id"], self._card(row) + f"\n\nAdded to your calendar and tasks\n"
                     f"Work needed: {hours:g} h (send /plan to re-plan today with it)")
        self.tg.answer(cq["id"], f"{hours:g} h")
        log.info("effort for %r set to %g h", row["item"]["title"], hours)

    def skip(self, row, cq):
        if row["status"] != "pending":
            self.tg.answer(cq["id"], f"Already {row['status']}")
            return
        self.state.set_pending_status(row["id"], "skipped")
        self.tg.edit(row["tg_message_id"], self._card(row) + "\n\nSkipped")
        self.tg.answer(cq["id"], "Skipped")
        log.info("skipped %r", row["item"]["title"])
        self._maybe_offer_block(row)

    def _maybe_offer_block(self, row):
        sender = row["sender"]
        added, skipped = self.state.sender_counts(sender)
        if skipped >= self.learn_after and added == 0 and self.state.get_sender_pref(sender) is None:
            self.state.set_sender_pref(sender, "asked")
            self.tg.send(f"You've skipped {skipped} items from {sender} and added none. "
                         "Stop asking about their emails?",
                         [("Always skip", f"block:{row['id']}"), ("Keep asking", f"keep:{row['id']}")])

    def block(self, row, cq):
        self.state.set_sender_pref(row["sender"], "blocked")
        self.tg.edit(cq["message"]["message_id"], f"OK - emails from {row['sender']} will be skipped from now on.")
        self.tg.answer(cq["id"], "Blocked")
        log.info("blocked sender %s", row["sender"])

    def keep(self, row, cq):
        self.state.set_sender_pref(row["sender"], "keep")
        self.tg.edit(cq["message"]["message_id"], f"OK - I'll keep asking about emails from {row['sender']}.")
        self.tg.answer(cq["id"], "OK")

    def expire_old(self):
        now = datetime.now(ZoneInfo(self.cfg["timezone"]))
        for row in self.state.open_pending():
            if _is_past(row["item"], now):
                self.state.set_pending_status(row["id"], "expired")
                if row["tg_message_id"]:
                    self.tg.edit(row["tg_message_id"], self._card(row) + "\n\nDate passed - not added")
                log.info("expired %r", row["item"]["title"])


def setup_logging():
    LOG_DIR.mkdir(exist_ok=True)
    handler = logging.FileHandler(LOG_DIR / "approvals.log")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[handler, console])
    for noisy in ("googleapiclient", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def main():
    setup_logging()
    cfg = load_config()
    listener = Listener(cfg, State(), Telegram.from_file())
    try:
        listener.tg.set_commands(COMMANDS)
    except TelegramError as e:
        log.warning("could not register bot commands: %s", e)
    log.info("listening for taps and commands: %s (Ctrl+C to stop)", " ".join("/" + c for c, _ in COMMANDS))
    offset, last_expire = None, 0.0
    while True:
        try:
            updates = listener.tg.updates(offset)
        except TelegramError as e:
            log.warning("%s; retrying in 30 s", e)
            time.sleep(30)
            continue
        for update in updates:
            offset = update["update_id"] + 1
            try:
                if "callback_query" in update:
                    listener.handle(update["callback_query"])
                elif "message" in update:
                    listener.handle_message(update["message"])
            except Exception:
                log.exception("failed to handle an update")
        if time.time() - last_expire > EXPIRE_EVERY_S:
            try:
                listener.expire_old()
            except Exception:
                log.exception("expiry check failed")
            last_expire = time.time()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
