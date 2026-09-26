"""The morning routine: ask for today's to-dos, then plan the day, then send one morning message.

    python morning.py --tick     # timer, every 15 min: does the next step when it's due
    python morning.py --finish   # plan + morning message right now (used when you tap Done)
    python morning.py --start    # send the check-in now, even if today's morning already ran (for testing)
    python morning.py --review   # the calendar review now (/review)

Steps, once a day, starting the first time the laptop is on after planner.plan_after:
  1. Telegram asks "What do you want to get done today?"; replies become to-dos in Google Tasks (todos.py).
  2. When you tap Done or Nothing today, or after morning.wait_minutes without an answer:
     plan the rest of the day (planner.py), then send ONE message: today's plan + the digest.
The day only counts as done once Telegram has the message; if it couldn't be reached (no Wi-Fi yet after
wake-up), the same message is retried at the next ticks. After morning.latest_checkin (default 18:00) there
is no check-in: the laptop was off all day, so it goes straight to the (evening) plan.
The first tick of each day also backs up state.db (backups/, 7 kept).
"""
import argparse
import json
import logging
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import alerts
import calwatch
import digest
import logsetup
import planner
import slots
from config import load_config
from notifiers import deliver
from state import State
from telegram_bot import Telegram, TelegramError

LOG_DIR = logsetup.LOG_DIR
BACKUP_DIR = Path(__file__).parent / "backups"
KEEP_BACKUPS = 7
IN_PROGRESS_STALE = timedelta(minutes=15)
CHECKIN_GIVE_UP_TRIES = 4  # ~1 h of failed check-in sends (Telegram down): plan without asking
log = logging.getLogger("morning")

# state.meta keys; values are ISO dates/times
CHECKIN_SENT = "checkin_sent_at"    # when today's question went out
CHECKIN_ANSWERED = "checkin_done"   # date you tapped Done / Nothing today
MORNING_SENT = "morning_sent"       # date the morning message reached Telegram
MORNING_TEXT = "morning_pending"    # today's morning message, while it still has to reach Telegram (JSON)
IN_PROGRESS = "morning_in_progress" # when a finish() run started (guards against two at once)
REVIEW_SENT = "calendar_review_sent" # date the daily calendar review went out
CHECKIN_FAILS = "checkin_failures"  # "<date>:<count>" of failed check-in sends
LAST_BACKUP = "last_backup"


def checkin_buttons(day):
    """The check-in's Done / Nothing today, dated so an old one can't start another day's morning."""
    return [("Done", f"checkin:done:{day.isoformat()}"), ("Nothing today", f"checkin:none:{day.isoformat()}")]


def checkin_open(state, today):
    """True while today's check-in is waiting for your to-dos."""
    sent = state.get_meta(CHECKIN_SENT)
    return bool(sent and sent[:10] == today.isoformat()
                and state.get_meta(CHECKIN_ANSWERED) != today.isoformat()
                and state.get_meta(MORNING_SENT) != today.isoformat())


def send_checkin(cfg, state, now, late):
    mc = cfg["morning"]
    go_at = slots.round_up(now + timedelta(minutes=mc["wait_minutes"]), 15)
    text = ("Good morning! What do you want to get done today?\n\n"
            "Send them one per line, with a time if you like:\n"
            "Finish lab report 2h\nCall bank 15m\n"
            f"(no time = {mc['todo_default_minutes']} min)\n\n"
            f"Tap Done (or type done) when you're finished; otherwise I'll plan the day at about {go_at:%H:%M}.")
    if late:
        text = f"(The laptop was off or asleep at {cfg['planner']['plan_after']}, so the morning starts now.)\n\n" + text
    Telegram.from_file().send(text, checkin_buttons(now.date()))
    state.set_meta(CHECKIN_SENT, now.isoformat())
    log.info("check-in sent")


def _clear_button(today):
    return [("Clear today's plan", f"clear:{today.isoformat()}")]


def _retry_telegram(cfg, state, today):
    """Today's message is written but hasn't reached Telegram yet: send just that (no re-planning).
    Returns True if there was such a message."""
    data = json.loads(state.get_meta(MORNING_TEXT) or "{}")
    if data.get("date") != today.isoformat():
        return False
    if deliver(data["title"], data["text"] + "\n\n(Sent late: Telegram couldn't be reached earlier.)", ["telegram"],
               buttons=_clear_button(today)):
        state.set_meta(MORNING_SENT, today.isoformat())
        state.set_meta(MORNING_TEXT, "")
        log.info("morning message reached Telegram on a retry")
    return True


def finish(cfg, state, now, note=None):
    """Plan + morning message. Marked as done only once the message is on Telegram, so a failure (no network
    yet after wake-up, Google hiccup) is retried at the next tick instead of losing the day."""
    today = now.date()
    if state.get_meta(MORNING_SENT) == today.isoformat():
        return  # Done tapped twice, or the timer got here first
    started = state.get_meta(IN_PROGRESS)
    if started and now - datetime.fromisoformat(started) < IN_PROGRESS_STALE:
        log.info("another morning run is in progress; leaving it to finish")
        return
    if _retry_telegram(cfg, state, today):
        return
    state.set_meta(IN_PROGRESS, now.isoformat())
    try:
        try:
            plan_title, plan_text, _ = planner.plan_today(cfg, state, now, how="auto")
        except planner.PlannerBusy:
            log.info("a plan is being made right now (/plan); the morning message waits for the next tick")
            return
        except Exception as e:  # noqa: BLE001 - still send the rest of the morning message
            if alerts.is_offline_error(e) or isinstance(e, (alerts.RefreshError, alerts.AuthExpired)):
                raise
            log.exception("planning failed; sending the morning message without a plan")
            alerts.alert("crash:planner", f"Planning the day failed: {type(e).__name__}: {e}. Details in logs/morning.log",
                         state)
            plan_title = f"Plan for {now:%a %d %b}"
            plan_text = f"Couldn't plan today ({type(e).__name__}). Send /plan to try again."
        _, sections = digest.build_digest(cfg, now, skip_planner_blocks=True, state=state)
        sections.insert(1, (plan_title.replace("Plan for", "Your plan for"), plan_text.splitlines()))
        title = f"Good morning - {now:%a %d %b}"
        text = ((note + "\n\n") if note else "") + digest.render(title, sections)
        (LOG_DIR / "digest.md").write_text(digest.render(title, sections, markdown=True))
        channels = cfg.get("digest", {}).get("channels", ["desktop"])
        sent = deliver(title, text, channels, buttons=_clear_button(today))
        if "telegram" in channels and "telegram" not in sent:
            # A desktop popup alone used to count as "sent", so the phone never got the day's plan.
            state.set_meta(MORNING_TEXT, json.dumps({"date": today.isoformat(), "title": title, "text": text}))
            log.warning("morning message reached %s but not Telegram; retrying Telegram at the next ticks",
                        ", ".join(sent) or "nothing")
            return
        if not sent:
            log.warning("morning message couldn't be delivered on any channel; retrying at the next tick")
            return
        state.set_meta(MORNING_SENT, today.isoformat())
        log.info("morning message sent via %s", ", ".join(sent))
    finally:
        state.set_meta(IN_PROGRESS, "")


def send_review(cfg, state, now):
    """The daily calendar review (calwatch.daily_review): new / changed / cancelled events, one message."""
    from googleapiclient.discovery import build

    from auth import get_credentials
    creds = get_credentials()
    return calwatch.daily_review(cfg, state, build("calendar", "v3", credentials=creds),
                                 build("tasks", "v1", credentials=creds), Telegram.from_file(), now)


def maybe_review(cfg, state, now):
    """Once a day, just before the check-in (or when the laptop first comes on after plan_after)."""
    if not cfg.get("calendar_watch", {}).get("daily_review") or state.get_meta(REVIEW_SENT) == now.date().isoformat():
        return
    try:
        sent = send_review(cfg, state, now)
    except TelegramError as e:
        log.warning("couldn't send the calendar review (%s); trying again at the next tick", e)
        return
    except calwatch.WatchBusy:
        log.info("a calendar check is running; the review follows at the next tick")
        return
    except Exception as e:
        if alerts.is_offline_error(e):  # just woke up, Wi-Fi not back yet: not worth an alert
            log.warning("calendar review: offline (%s); trying again at the next tick", type(e).__name__)
            return
        log.exception("calendar review failed")
        alerts.alert("review", f"The daily calendar review failed ({type(e).__name__}: {e}).", state)
        return
    state.set_meta(REVIEW_SENT, now.date().isoformat())
    log.info("calendar review: %s", "sent" if sent else "nothing new")


def maybe_backup(state, now):
    """Once a day: a copy of state.db (your decisions, cards, to-dos) in backups/, the last KEEP_BACKUPS kept."""
    if (state.get_meta(LAST_BACKUP) or "")[:10] == now.date().isoformat():
        return
    try:
        BACKUP_DIR.mkdir(mode=0o700, exist_ok=True)
        target = BACKUP_DIR / f"state-{now.date().isoformat()}.db"
        with sqlite3.connect(target) as dest:
            state.db.backup(dest)
        dest.close()
        target.chmod(0o600)
        for old in sorted(BACKUP_DIR.glob("state-*.db"))[:-KEEP_BACKUPS]:
            old.unlink()
        state.set_meta(LAST_BACKUP, now.isoformat())
        log.info("backed up state.db to %s", target.name)
    except (OSError, sqlite3.Error) as e:
        log.warning("state.db backup failed: %s", e)


def tick(cfg, state, now):
    today = now.date()
    maybe_backup(state, now)
    plan_after = slots.at(today, cfg["planner"]["plan_after"], now.tzinfo)
    if now < plan_after:
        return
    maybe_review(cfg, state, now)
    if state.get_meta(MORNING_SENT) == today.isoformat():
        return
    mc = cfg["morning"]
    sent = state.get_meta(CHECKIN_SENT)
    checkin_today = bool(sent) and sent[:10] == today.isoformat()
    latest = slots.at(today, mc.get("latest_checkin", "18:00"), now.tzinfo)
    if not mc.get("checkin", True) or (now >= latest and not checkin_today):
        # no "Good morning" questions at night: the laptop was first on late, so just plan what's left
        finish(cfg, state, now, note=None if now < latest else
               f"Late start: the laptop was first on after {mc.get('latest_checkin', '18:00')}, so there was no check-in today.")
        return
    if not checkin_today:
        try:
            send_checkin(cfg, state, now, late=now > plan_after + timedelta(minutes=30))
        except TelegramError as e:
            day, _, count = (state.get_meta(CHECKIN_FAILS) or "").partition(":")
            fails = (int(count) if day == today.isoformat() else 0) + 1
            state.set_meta(CHECKIN_FAILS, f"{today.isoformat()}:{fails}")
            if fails < CHECKIN_GIVE_UP_TRIES:
                log.warning("couldn't send the check-in (%s); trying again at the next tick", e)
            else:
                log.error("couldn't reach Telegram for the check-in %d times; planning without it", fails)
                finish(cfg, state, now, note="Couldn't reach Telegram this morning, so there was no check-in.")
        return
    if state.get_meta(CHECKIN_ANSWERED) == today.isoformat():
        finish(cfg, state, now)
    elif now - datetime.fromisoformat(sent) >= timedelta(minutes=mc["wait_minutes"]):
        finish(cfg, state, now, note="No reply to the check-in, so I planned with what's already in your calendar "
                                     "and tasks. Add to-dos any time with /todo, then tap 'Plan rest of today'.")


@alerts.guard("morning")
def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--tick", action="store_true", help="timer mode: do the next step if it's due")
    group.add_argument("--finish", action="store_true", help="plan and send the morning message now")
    group.add_argument("--start", action="store_true", help="send today's check-in now (testing)")
    group.add_argument("--review", action="store_true", help="send the calendar review now (/review)")
    args = parser.parse_args()

    logsetup.setup("morning")
    cfg = load_config()
    state = State()
    now = datetime.now(ZoneInfo(cfg["timezone"]))
    if args.tick:
        tick(cfg, state, now)
    elif args.finish:
        finish(cfg, state, now)
    elif args.review:
        try:
            sent = send_review(cfg, state, now)
        except calwatch.WatchBusy:
            Telegram.from_file().send("A calendar check is running right now; send /review again in a minute.")
            return
        if not sent:
            Telegram.from_file().send("Calendar review: nothing new or changed on your calendars since the last one.")
    else:
        for key in (CHECKIN_ANSWERED, MORNING_SENT, MORNING_TEXT):
            state.set_meta(key, "")
        send_checkin(cfg, state, now, late=False)


if __name__ == "__main__":
    main()
