"""Asks you on Telegram before anything is added, and acts on your Add / Skip taps.

    python approvals.py     # normally runs as the calendar-approvals systemd service

ingest.py calls ask() for each new item. This listener long-polls Telegram (near-zero CPU while idle),
creates the items you approve, marks the ones you skip, and expires items whose date has passed.
After `approval.learn_after_skips` skips (and no adds) from one sender, it offers to stop asking about them.
It also answers commands (only from your chat): /todo, /check, /skipped, /plan, /clear, /pause, /resume, /status,
the same actions from the button bar, and the morning check-in (plain replies become to-dos while it's open).

Every tap is answered exactly once: Telegram shows only the first answer, so slow taps (anything that talks to
Google) are answered straight away and report back by editing their message; quick ones answer with a short note.
"""
import logging
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

from google.auth.exceptions import RefreshError
from googleapiclient.discovery import build

import alerts
import calwatch
import deadlines
import google_writer
import habits
import llm
import logsetup
import morning
import planner
import settings
import slotpicker
import todos
from auth import AuthExpired, get_credentials
from ics_import import describe_recurrence
from config import load_config
from state import State
from telegram_bot import Telegram, TelegramError

LOG_DIR = Path(__file__).parent / "logs"
EXPIRE_EVERY_S = 600
COMMANDS = [("today", "Today at a glance: events, your blocks, tasks without a time"),
            ("todo", "Add to-dos for today, e.g. /todo Lab report 2h"),
            ("deadlines", "Upcoming deadlines: done, effort, move the date, not doing"),
            ("habits", "Your habits and streaks; add a new one"),
            ("check", "Check mail (and your other calendars) now instead of waiting for the next 30-min run"),
            ("skipped", "Emails I skipped (no deadline words), with a button to read one anyway"),
            ("exams", "Exams coming up and how much preparation each gets"),
            ("calendars", "Choose which calendars I ask about, copy, show or ignore"),
            ("review", "Calendar review now: what's new or changed on your calendars"),
            ("plan", "Rest of today: free slots to pick for each task"),
            ("clear", "Remove today's planned blocks that haven't started"),
            ("pause", "Stop reading mail until /resume (calendar checks continue)"),
            ("resume", "Start reading mail again (checks right away)"),
            ("settings", "Change work hours, daily limit, morning time, reminders..."),
            ("status", "Is everything working? Last checks, login, GPU, waiting cards")]
# Permanent button bar at the bottom of the chat; each label maps to a command.
KEYBOARD = [["Check mail now", "Plan rest of today"], ["Today", "Deadlines", "Habits"], ["Settings", "Status"]]
LABELS = {"check mail now": "/check", "plan rest of today": "/plan", "status": "/status", "today": "/today",
          "deadlines": "/deadlines", "habits": "/habits", "settings": "/settings", "pause": "/pause", "resume": "/resume"}
OFFLINE_AFTER_S = 300  # a command older than this was sent while the laptop was off or asleep
# Taps that talk to Google (seconds): answered before the work starts, so the button stops spinning at once.
SLOW_ACTIONS = {"cal", "calb", "cale", "calc", "calu", "calp", "crv", "crva", "add", "undo", "addundo",
                "sgb", "sgm", "sgn", "bkd", "eve", "prep", "hu", "mvb", "dl", "dle", "dlm", "dlt", "dlx", "dlb", "tdp"}
DEADLINE_ACTIONS = ("dl", "dle", "dlm", "dlt", "dlx")
SETTING_ACTIONS = ("set", "setv", "sett", "setr", "setb")
HABIT_ACTIONS = ("hbn", "hbm", "hbd", "hbw", "hbs", "hbx", "hbp", "hbr", "hby", "hbl")
UNDO_ADD_WINDOW = timedelta(minutes=10)
# slotpicker.py: times, "Did you finish?", evening check, exams, heads-up, moving a block
SLOT_ACTIONS = ("sgb", "sgm", "sgn", "bkd", "eve", "prep", "hu", "mvb")
HEADS_UP_EVERY_S = 60
# Typed instead of tapping the check-in buttons (a whole message, after lowercasing and trimming punctuation).
DONE_WORDS = {"done", "finished", "that's all", "thats all", "that's it", "thats it", "all done", "ok", "okay"}
NONE_WORDS = {"nothing", "nothing today", "no", "none", "nope", "no tasks", "nil"}
ERROR_REPLY_EVERY_S = 60
SKIPPED_SHOWN = 8
log = logging.getLogger("approvals")


def _when(item):
    return item["due"] or item["start"]


def format_item(item, subject, sender):
    when = _when(item)
    stamp = when.strftime("%a %d %b, %H:%M") if isinstance(when, datetime) else when.strftime("%a %d %b (all day)")
    kind = {"deadline": "Deadline", "meeting": "Meeting", "event": "Event"}[item["type"]]
    if item["type"] != "deadline" and planner.exam_kind(item["title"]):
        kind = "Exam"
    lines = [f"{kind}: {item['title']}", stamp]
    if item.get("course"):
        lines.append(f"Course: {item['course']}")
    if item.get("recurrence"):
        lines.append(f"Repeats {describe_recurrence(item['recurrence'])}")
    lines += [f"From: {sender}", f"Email: {subject}"]
    if item["type"] == "deadline":
        lines.append("Add = calendar event + task")
    elif kind == "Exam":
        lines.append("Add = calendar event, with preparation time planned before it")
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
    if item.get("recurrence"):
        return False  # a repeating invite that began earlier still has occurrences ahead
    when = _when(item)
    if not isinstance(when, datetime):
        when = datetime.combine(when, dtime(23, 59), now.tzinfo)
    return when < now


class Tap:
    """Answers one button tap exactly once; later answers are ignored (Telegram would drop them anyway)."""

    def __init__(self, tg, callback_id, answered=False):
        self.tg, self.id, self.done = tg, callback_id, answered

    def answer(self, text=""):
        if not self.done:
            self.done = True
            self.tg.answer(self.id, text)


class Listener:
    def __init__(self, cfg, state, tg):
        self.cfg, self.state, self.tg = cfg, state, tg
        creds = get_credentials()
        self.calendar = build("calendar", "v3", credentials=creds)
        self.tasks = build("tasks", "v1", credentials=creds)
        self.learn_after = cfg.get("approval", {}).get("learn_after_skips", 3)
        self._last_error_reply = 0.0

    def _tz(self):
        return ZoneInfo(self.cfg["timezone"])

    def is_mine(self, cq):
        return cq.get("message", {}).get("chat", {}).get("id") == self.tg.chat_id

    @staticmethod
    def is_slow(cq):
        return cq.get("data", "").partition(":")[0] in SLOW_ACTIONS

    def handle(self, cq):
        if not self.is_mine(cq):
            log.warning("ignored a tap from unknown chat %s", cq.get("message", {}).get("chat", {}).get("id"))
            self.tg.answer(cq["id"], "Not allowed")
            return
        tap = cq.setdefault("_tap", Tap(self.tg, cq["id"], cq.get("_answered", False)))
        try:
            self._route(cq, tap)
        finally:
            tap.answer()  # every tap gets an answer, even if the work failed

    def _route(self, cq, tap):
        action, _, rest = cq.get("data", "").partition(":")
        if action in SLOW_ACTIONS:
            tap.answer()  # before the slow Google calls; results show by editing the message
        if action in ("crv", "crva"):  # the daily calendar review and "changed" cards
            calwatch.handle_review(self, cq, action, rest, datetime.now(self._tz()))
            return
        now = datetime.now(self._tz())
        if action in SLOT_ACTIONS:
            slotpicker.handle(self, cq, action, rest, now)
            return
        if action in DEADLINE_ACTIONS:
            deadlines.handle(self, cq, action, rest, now)
            return
        if action == "dlb":
            deadlines.back(self, cq, now)
            return
        if action in SETTING_ACTIONS:
            settings.handle(self, cq, action, rest, now)
            return
        if action in HABIT_ACTIONS:
            habits.handle(self, cq, action, rest, now)
            return
        if action == "tdp":  # "Plan rest of today" under /today
            started = self._run_planner()
            self.tg.edit(cq["message"]["message_id"], "Finding free slots for the rest of today; they follow below."
                         if started is True else "Already on it." if started == "busy" else "Couldn't start the planner.")
            return
        if action == "st":  # Pause / Resume under /status
            self.handle_message({"chat": {"id": self.tg.chat_id}, "text": "/pause" if rest == "p" else "/resume",
                                 "date": time.time()})
            self.tg.edit(cq["message"]["message_id"], *self.status_card())
            tap.answer("Paused" if rest == "p" else "Resumed")
            return
        if action in ("cal", "calb", "cale", "calc", "calu", "calp"):  # events from your other calendars
            calwatch.handle_callback(self, cq, action, rest, datetime.now(self._tz()))
            return
        if action == "checkin":  # "checkin:done:<date>" / "checkin:none:<date>" under the morning question
            self.checkin_answered(cq, tap, rest)
            return
        if action == "undo":  # "undo:<batch>" under a to-do confirmation
            self.undo_todos(cq, rest)
            return
        if action == "read":  # "read:<gmail id>" under /skipped: run the model on that email after all
            started = self._run_script("ingest.py", "--message", rest, unit=f"calendar-read-{rest[:16]}")
            tap.answer("Reading it now; a card follows if there's something to add." if started is True
                       else "Already reading it." if started == "busy" else "Couldn't start")
            return
        if action == "clear":  # "clear:<YYYY-MM-DD>" under a plan summary
            started = self._run_planner("--clear", "--date", rest)
            tap.answer("Clearing..." if started is True else "Already busy; try again in a minute"
                       if started == "busy" else "Couldn't start")
            return
        raw_id, _, arg = rest.partition(":")
        row = self.state.get_pending(int(raw_id)) if raw_id.isdigit() else None
        if row is None:
            tap.answer("This card is out of date")
            return
        if action == "effort":  # "effort:<pending id>:<hours>"
            self.effort(row, cq, tap, arg)
            return
        handler = {"add": self.add, "skip": self.skip, "block": self.block, "keep": self.keep,
                   "addundo": self.undo_add}.get(action)
        if handler is None:
            tap.answer("Unknown button")
            return
        handler(row, cq, tap)

    def handle_message(self, message):
        if message.get("chat", {}).get("id") != self.tg.chat_id:
            log.warning("ignored a message from unknown chat %s", message.get("chat", {}).get("id"))
            return
        text = (message.get("text") or "").strip()
        # a button-bar label, or "/pause@AgCalenderBot" -> "/pause"
        command = LABELS.get(text.lower()) or (text.split()[0].split("@")[0].lower() if text else "")
        sent_at = message.get("date", time.time())
        if time.time() - sent_at > OFFLINE_AFTER_S and command in ("/check", "/plan", "/clear", "/todo"):
            stamp = datetime.fromtimestamp(sent_at, self._tz())
            self.tg.send(f"Got your {command} from {stamp:%a %H:%M}. The laptop was off or asleep then; doing it now.")
        now = datetime.now(self._tz())
        today = now.date()
        typed = re.sub(r"[^\w' ]+", "", text.lower()).strip()
        checkin_open = morning.checkin_open(self.state, today)
        conv = self.state.conv(now) if text and not text.startswith("/") and not LABELS.get(text.lower()) else None
        if conv:  # a value you were asked to type (a setting, a habit, a new due date)
            {"setting": settings.typed, "habit": habits.typed, "deadline_date": deadlines.typed}.get(
                conv["flow"], lambda *a: self.state.clear_conv())(self, conv, text, now)
            return
        if command == "/todo":
            body = text.split(maxsplit=1)[1] if len(text.split(maxsplit=1)) > 1 else ""
            if body:
                self.add_todos(body, today)
            else:
                self.tg.send("Send /todo followed by the task, one per line, e.g.\n/todo Lab report 2h\nCall bank 15m")
        elif checkin_open and not text.startswith("/") and (typed in DONE_WORDS or typed in NONE_WORDS):
            started, reply = self._close_checkin("done" if typed in DONE_WORDS else "none", today)
            self.tg.send(reply)
        elif not text.startswith("/") and not LABELS.get(text.lower()) and checkin_open:
            self.add_todos(text, today)
        elif command == "/skipped":
            self.show_skipped()
        elif command == "/today":
            self.tg.send(*slotpicker.today_message(self.cfg, self.state, self.calendar, now))
        elif command == "/deadlines":
            self.tg.send(*deadlines.list_message(self.cfg, self.state, self.calendar, now))
        elif command == "/habits":
            self.tg.send(*habits.list_message(self.cfg, self.state, today))
        elif command == "/settings":
            self.tg.send(*settings.menu(self.cfg))
        elif command == "/exams":
            self.tg.send(*slotpicker.exams_message(self.cfg, self.state, datetime.now(self._tz()), self.calendar))
        elif command == "/review":
            started = self._run_script("morning.py", "--review", unit="calendar-review-now")
            self.tg.send("Looking at your calendars; the review follows shortly." if started is True else
                         "A calendar review is already running." if started == "busy" else
                         "Couldn't start the calendar review.")
        elif command == "/calendars":
            calwatch.show_menu(self, datetime.now(self._tz()))
        elif command == "/check":
            gpu_retry = bool(llm.gpu_lost_since(self.state))
            llm.clear_gpu_flag(self.state)  # you may have just fixed it (restarted Ollama, left power-saver mode)
            if self.state.paused_since():
                self.tg.send("Mail reading is paused, so no mail was read. Tap Resume (or send /resume) first.")
            elif self._start_ingest_now():
                self.tg.send("Checking mail now. Anything new will arrive here as a card."
                             + (" (Trying the GPU again.)" if gpu_retry else ""))
            else:
                self.tg.send("Couldn't start the mail check (is calendar-ingest.service installed?).")
        elif command == "/plan":
            started = self._run_planner()
            self.tg.send("Finding your free slots for the rest of today; pick a time for each task below." if started is True else
                         "Already on it; the times will arrive shortly." if started == "busy" else
                         "Couldn't start the planner.")
        elif command == "/clear":
            started = self._run_planner("--clear")
            self.tg.send("Clearing today's plan..." if started is True else
                         "Already busy with the plan; try again in a minute." if started == "busy" else
                         "Couldn't start the planner.")
        elif command == "/pause":
            already = self.state.paused_since()
            self.state.set_paused(True)
            self.tg.send("Already paused." if already else
                         "Paused. I won't read new mail until you send /resume. Your other calendars are still "
                         "checked.\nMail that arrives meanwhile is read when you resume. Buttons on existing cards still work.")
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
            self.tg.send(*self.status_card())
        else:  # /start, /help or anything else: show what's possible, with the button bar
            self.tg.send("Commands (or use the buttons below):\n" + "\n".join(f"/{c} - {d}" for c, d in COMMANDS),
                         keyboard=KEYBOARD)

    def status_card(self):
        paused = self.state.paused_since()
        return self.status_text(), [[("Resume mail reading", "st:r") if paused else ("Pause mail reading", "st:p")]]

    def status_text(self):
        tz, state = self._tz(), self.state
        now = datetime.now(tz)
        paused, last = state.paused_since(), state.get_last_run()

        def stamp(iso):
            return datetime.fromisoformat(iso).astimezone(tz).strftime("%a %d %b %H:%M") if iso else "never"

        lines = [f"Mail reading: PAUSED since {paused.astimezone(tz):%a %d %b %H:%M}" if paused else "Mail reading: on (every 30 min)",
                 f"Last mail check: {last.astimezone(tz):%a %d %b %H:%M}" if last else "Last mail check: never",
                 f"Last calendar check: {stamp(state.get_meta('last_calendar_scan'))}",
                 f"Morning routine: {'done today' if state.get_meta(morning.MORNING_SENT) == now.date().isoformat() else 'not yet today'}",
                 "Google login: " + ("EXPIRED - run auth.py in a terminal" if state.get_meta("alert:auth") else "OK")]
        lost = llm.gpu_lost_since(state)
        lines.append(f"GPU for the model: unavailable since {lost.astimezone(tz):%a %H:%M} (emails wait; send /check after fixing)"
                     if lost else "GPU for the model: OK")
        waiting = int(state.get_meta("llm_waiting") or 0)
        if waiting:
            lines.append(f"Emails waiting for the model: {waiting}")
        lines.append(f"Cards waiting for your answer: {state.count_open_pending()}")
        items = state.plan_items(now.date())
        if items:
            waiting_time = sum(1 for r in items if r["status"] == "open" and r["minutes"] > 0)
            lines.append(f"Today's tasks: {len(items) - waiting_time} with a time (or not today), {waiting_time} waiting for a time")
        skipped = state.skipped_messages(limit=50, since=now - timedelta(days=1))
        if skipped:
            lines.append(f"Emails skipped in the last 24 h (no deadline words): {len(skipped)} - /skipped")
        problems = []
        for key, value in state.db.execute("SELECT key, value FROM meta WHERE key LIKE 'alert:%' AND value != ''"):
            when = datetime.fromisoformat(value)
            if now - when < timedelta(days=1) and key != "alert:auth":
                problems.append(f"- {key[len('alert:'):]} at {when.astimezone(tz):%a %H:%M}")
        lines += (["Problems in the last 24 h:"] + problems) if problems else ["Problems in the last 24 h: none"]
        lines.append(f"Last database backup: {stamp(state.get_meta('last_backup'))}")
        return "\n".join(lines)

    def show_skipped(self):
        rows = self.state.skipped_messages(limit=SKIPPED_SHOWN)
        if not rows:
            self.tg.send("No skipped emails recently. (Emails without deadline words are listed here.)")
            return
        tz = self._tz()
        lines = ["Emails I didn't send to the model (no deadline words), newest first:"]
        buttons = []
        for n, r in enumerate(rows, 1):
            when = datetime.fromisoformat(r["processed_at"]).astimezone(tz)
            why = " (the model's answer was unreadable)" if r["outcome"] == "unreadable" else ""
            lines.append(f"{n}. {when:%a %d %b}: {r['subject'][:80]}{why}")
            buttons.append((f"Read {n}", f"read:{r['msg_id']}"))
        lines.append("\nTap Read <n> if one has a deadline or event; I'll ask the model about it.")
        self.tg.send("\n".join(lines), [buttons[i:i + 4] for i in range(0, len(buttons), 4)])

    def report_error(self, exc):
        """A tap or command failed: say so instead of leaving the card unchanged."""
        if isinstance(exc, (RefreshError, AuthExpired)):
            alerts.alert("auth", alerts.AUTH_TEXT, self.state)
            text = "That didn't work: the Google login has expired. " + alerts.AUTH_TEXT.split("Fix: ")[-1]
        elif alerts.is_offline_error(exc):
            text = "Couldn't reach Google just now, so that didn't happen. Try again in a minute."
        else:
            text = f"Sorry, that didn't work ({type(exc).__name__}). Details are in logs/approvals.log."
        if time.time() - self._last_error_reply >= ERROR_REPLY_EVERY_S:
            self._last_error_reply = time.time()
            try:
                self.tg.send(text)
            except TelegramError:
                pass

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
            tail, buttons = "Anything else? Tap Done (or type done) when you're finished.", buttons + [morning.checkin_buttons(today)[0]]
        elif self.state.get_meta(morning.MORNING_SENT) == today.isoformat():
            started = self._run_script("planner.py", "--suggest-new", unit="calendar-suggest-now")
            tail = ("Free slots to pick for it follow in a moment." if started is True else
                    "Tap 'Plan rest of today' to get free slots for it.")
        else:
            tail = "You'll get free slots for it after the morning check-in."
        self.tg.send("Added to DAILY TASKS (due today):\n" + "\n".join(lines) + "\n\n" + tail, buttons)
        log.info("added %d to-do(s)", len(added))

    def undo_todos(self, cq, raw_batch):
        if not raw_batch.isdigit():
            return
        removed = todos.undo(self.tasks, self.cfg["morning"]["todo_tasklist"], self.state, int(raw_batch))
        self.tg.edit(cq["message"]["message_id"], ("Removed: " + ", ".join(removed)) if removed else "Already removed.")

    def _close_checkin(self, answer, today):
        """Done / Nothing today (tapped or typed): plan the day now. Returns (started, reply)."""
        self.state.set_meta(morning.CHECKIN_ANSWERED, today.isoformat())
        if self.state.get_meta(morning.MORNING_SENT) == today.isoformat():
            started = self._run_planner()
            if started == "busy":
                self.state.set_meta("replan_requested", "1")  # the running planner plans again when it's done
                started = True
            reply = "Re-planning the rest of today with your to-dos."
        else:
            started = self._run_script("morning.py", "--finish", unit="calendar-morning-now")
            reply = "Got it. Planning your day now; the morning message follows shortly."
            if started == "busy":
                started, reply = True, "Got it. The morning plan is already being made; it follows shortly."
        log.info("check-in answered (%s)", answer)
        return started is True, reply if started is True else "Couldn't start the planner."

    def checkin_answered(self, cq, tap, rest):
        answer, _, day = rest.partition(":")
        today = datetime.now(self._tz()).date()
        message_id = cq["message"]["message_id"]
        sent = self.state.get_meta(morning.CHECKIN_SENT) or ""
        # Old buttons (another day's check-in, or a to-do reply from yesterday) must not start today's morning.
        stale = (day and day != today.isoformat()) or (not day and not morning.checkin_open(self.state, today))
        if stale:
            self.tg.edit(message_id, f"That was the check-in for {day or 'an earlier day'}; nothing was changed. "
                                     "Tap 'Plan rest of today' to plan now.")
            tap.answer("Old button")
            return
        if sent[:10] != today.isoformat() and self.state.get_meta(morning.MORNING_SENT) != today.isoformat():
            tap.answer("Today's check-in hasn't started yet")
            return
        started, reply = self._close_checkin(answer, today)
        self.tg.edit(message_id, reply)
        tap.answer("Planning..." if started else "Error")

    @classmethod
    def _run_planner(cls, *args):
        return cls._run_script("planner.py", *args, unit="calendar-clear-now" if "--clear" in args else "calendar-plan-now")

    @staticmethod
    def _run_script(script, *args, unit=None):
        """Starts a script as its own transient systemd unit, so restarting this listener can't kill it.
        A fixed unit name means a second tap while it's still running is refused instead of doubling up.
        Returns True (started), "busy" (that unit is still running) or False (couldn't start)."""
        here = Path(__file__).parent
        cmd = ["systemd-run", "--user", "--no-block", "--collect", f"--working-directory={here}",
               *([f"--unit={unit}"] if unit else []), sys.executable, str(here / script), *args]
        try:
            done = subprocess.run(cmd, timeout=15, capture_output=True, text=True)
            if done.returncode == 0:
                return True
            if "already" in done.stderr:  # "... already exists": that unit is running right now
                return "busy"
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

    def add(self, row, cq, tap):
        if row["status"] != "pending":
            return  # a second tap: the card already says what happened
        item, msg = row["item"], {"id": row["msg_id"], "subject": row["msg_subject"]}
        event_id, task_id = google_writer.create_item(self.calendar, self.tasks, self.cfg, item, msg)
        self.state.record_item(row["dedupe_key"], item["type"], event_id, task_id, item["title"],
                               str(_when(item)), row["msg_id"])
        self.state.set_pending_status(row["id"], "added")
        text = self._card(row) + "\n\nAdded to your calendar" + (" and tasks" if task_id else "")
        undo = [("Undo (10 min)", f"addundo:{row['id']}")]
        exam = item["type"] != "deadline" and planner.exam_kind(item["title"])
        if item["type"] == "deadline":
            pc = self.cfg["planner"]
            choices = [(f"{h:g} h", f"effort:{row['id']}:{h}") for h in pc["effort_choices_hours"]]
            self.tg.edit(row["tg_message_id"], text + f"\nHow much work does it need? (default {pc['default_effort_hours']:g} h)",
                         [choices[:3], choices[3:], undo] if len(choices) > 3 else [choices, undo])
        elif exam:
            hours = planner.exam_prep_hours(self.cfg, exam)
            choices = [(f"{h:g} h", f"effort:{row['id']}:{h}") for h in slotpicker.EXAM_CHOICES] + [("No prep", f"effort:{row['id']}:0")]
            self.tg.edit(row["tg_message_id"], text + f"\nHow much preparation? (default for a {exam}: {hours:g} h)",
                         [choices[:3], choices[3:], undo])
        else:
            self.tg.edit(row["tg_message_id"], text, [undo])
        log.info("added %r", item["title"])

    def undo_add(self, row, cq, tap):
        """Undo an Add within UNDO_ADD_WINDOW: the event (and task) go, and the card asks again."""
        decided = datetime.fromisoformat(row["decided_at"]) if row["decided_at"] else None
        if row["status"] != "added" or decided is None or datetime.now(decided.tzinfo) - decided > UNDO_ADD_WINDOW:
            self.tg.edit(row["tg_message_id"], self._card(row) + "\n\nAdded to your calendar. (Too late to undo here: "
                         "use /deadlines -> Not doing, or delete the event.)")
            return
        event_id = self.state.event_id_for(row["dedupe_key"])
        task_id = self.state.task_for_event(event_id) if event_id else None
        if event_id:
            google_writer.delete_event(self.calendar, self.cfg["calendars"]["college"], event_id)
            self.state.delete_item_by_event(event_id)
        if task_id:
            try:
                self.tasks.tasks().delete(tasklist=self.cfg.get("tasklist", "@default"), task=task_id).execute()
            except Exception as e:  # noqa: BLE001 - already gone is fine
                if getattr(getattr(e, "resp", None), "status", None) not in (404, 410):
                    raise
        self.state.set_pending_status(row["id"], "pending")
        self.tg.edit(row["tg_message_id"], self._card(row) + "\n\nUndone: removed again.",
                     [("Add", f"add:{row['id']}"), ("Skip", f"skip:{row['id']}")])
        log.info("undid add of %r", row["item"]["title"])

    def effort(self, row, cq, tap, arg):
        try:
            hours = float(arg)
        except ValueError:
            tap.answer("Unknown button")
            return
        event_id = self.state.event_id_for(row["dedupe_key"])
        if not event_id:
            tap.answer("Add it first")
            return
        self.state.set_effort(f"event:{event_id}", hours)
        work = f"Preparation: {hours:g} h" if planner.exam_kind(row["item"]["title"]) and row["item"]["type"] != "deadline" \
            else f"Work needed: {hours:g} h"
        self.tg.edit(row["tg_message_id"], self._card(row) + "\n\nAdded to your calendar"
                     + (" and tasks" if row["item"]["type"] == "deadline" else "")
                     + (f"\n{work}" if hours else "\nNo preparation planned"))
        tap.answer(f"{hours:g} h")
        log.info("effort for %r set to %g h", row["item"]["title"], hours)

    def skip(self, row, cq, tap):
        if row["status"] != "pending":
            tap.answer(f"Already {row['status']}")
            return
        self.state.set_pending_status(row["id"], "skipped")
        self.tg.edit(row["tg_message_id"], self._card(row) + "\n\nSkipped")
        tap.answer("Skipped")
        log.info("skipped %r", row["item"]["title"])
        self._maybe_offer_block(row)

    def _maybe_offer_block(self, row):
        sender = row["sender"]
        if not sender or sender == "unknown":
            return  # emails whose sender couldn't be read must never be blocked as a group
        added, skipped = self.state.sender_counts(sender)
        if skipped >= self.learn_after and added == 0 and self.state.get_sender_pref(sender) is None:
            self.state.set_sender_pref(sender, "asked")
            self.tg.send(f"You've skipped {skipped} items from {sender} and added none. "
                         "Stop asking about their emails?",
                         [("Always skip", f"block:{row['id']}"), ("Keep asking", f"keep:{row['id']}")])

    def block(self, row, cq, tap):
        self.state.set_sender_pref(row["sender"], "blocked")
        self.tg.edit(cq["message"]["message_id"], f"OK - emails from {row['sender']} will be skipped from now on.")
        tap.answer("Blocked")
        log.info("blocked sender %s", row["sender"])

    def keep(self, row, cq, tap):
        self.state.set_sender_pref(row["sender"], "keep")
        self.tg.edit(cq["message"]["message_id"], f"OK - I'll keep asking about emails from {row['sender']}.")
        tap.answer("OK")

    def expire_old(self):
        now = datetime.now(self._tz())
        for row in self.state.open_pending():
            if _is_past(row["item"], now):
                self.state.set_pending_status(row["id"], "expired")
                if row["tg_message_id"]:
                    self.tg.edit(row["tg_message_id"], self._card(row) + "\n\nDate passed - not added")
                log.info("expired %r", row["item"]["title"])


def main():
    logsetup.setup("approvals")
    cfg = load_config()
    try:
        listener = Listener(cfg, State(), Telegram.from_file())
    except AuthExpired:
        alerts.alert("auth", alerts.AUTH_TEXT)
        time.sleep(600)  # systemd restarts us afterwards; waiting keeps that from happening every 30 s
        raise SystemExit(1)
    except Exception as e:
        if not alerts.is_offline_error(e):
            raise
        log.warning("no network yet (%s); systemd will start me again in 30 s", type(e).__name__)
        raise SystemExit(1) from None
    try:
        listener.tg.set_commands(COMMANDS)
    except TelegramError as e:
        log.warning("could not register bot commands: %s", e)
    log.info("listening for taps and commands: %s (Ctrl+C to stop)", " ".join("/" + c for c, _ in COMMANDS))
    offset, last_expire, offline_since, last_heads_up = None, 0.0, None, 0.0
    while True:
        try:
            updates = listener.tg.updates(offset)
            if offline_since:
                log.info("Telegram reachable again after %d min", (time.time() - offline_since) // 60)
                offline_since = None
        except TelegramError as e:
            if offline_since is None:  # one line per outage, not one every 30 s
                offline_since = time.time()
                log.warning("%s; retrying every 30 s", e)
            time.sleep(30)
            continue
        # Answer slow taps in this batch first: a tap queued behind a slow one used to be answered too late.
        for update in updates:
            cq = update.get("callback_query")
            if cq and listener.is_mine(cq) and listener.is_slow(cq):
                cq["_tap"] = Tap(listener.tg, cq["id"])
                cq["_tap"].answer()
        for update in updates:
            offset = update["update_id"] + 1
            try:
                if "callback_query" in update:
                    listener.handle(update["callback_query"])
                elif "message" in update:
                    listener.handle_message(update["message"])
            except Exception as e:
                log.exception("failed to handle an update")
                listener.report_error(e)
        if time.time() - last_heads_up > HEADS_UP_EVERY_S:
            try:
                slotpicker.heads_up(listener, datetime.now(listener._tz()))
            except Exception:
                log.exception("heads-up check failed")
            last_heads_up = time.time()
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
