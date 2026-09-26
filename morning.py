"""The morning routine: ask for today's to-dos, then plan the day, then send one morning message.

    python morning.py --tick     # timer, every 15 min: does the next step when it's due
    python morning.py --finish   # plan + morning message right now (used when you tap Done)
    python morning.py --start    # send the check-in now, even if today's morning already ran (for testing)

Steps, once a day, starting the first time the laptop is on after planner.plan_after:
  1. Telegram asks "What do you want to get done today?"; replies become to-dos in Google Tasks (todos.py).
  2. When you tap Done or Nothing today, or after morning.wait_minutes without an answer:
     plan the rest of the day (planner.py), then send ONE message: today's plan + the digest.
"""
import argparse
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import alerts
import digest
import logsetup
import planner
import slots
from config import load_config
from notifiers import deliver
from state import State
from telegram_bot import Telegram, TelegramError

LOG_DIR = logsetup.LOG_DIR
IN_PROGRESS_STALE = timedelta(minutes=15)
CHECKIN_GIVE_UP_TRIES = 4  # ~1 h of failed check-in sends (Telegram down): plan without asking
log = logging.getLogger("morning")

# state.meta keys; values are ISO dates/times
CHECKIN_SENT = "checkin_sent_at"    # when today's question went out
CHECKIN_ANSWERED = "checkin_done"   # date you tapped Done / Nothing today
MORNING_SENT = "morning_sent"       # date the morning message went out
IN_PROGRESS = "morning_in_progress" # when a finish() run started (guards against two at once)
REVIEW_SENT = "calendar_review_sent" # date the daily calendar review went out
CHECKIN_FAILS = "checkin_failures"  # "<date>:<count>" of failed check-in sends


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
            f"Tap Done when you're finished; otherwise I'll plan the day at about {go_at:%H:%M}.")
    if late:
        text = f"(The laptop was off or asleep at {cfg['planner']['plan_after']}, so the morning starts now.)\n\n" + text
    Telegram.from_file().send(text, [("Done", "checkin:done"), ("Nothing today", "checkin:none")])
    state.set_meta(CHECKIN_SENT, now.isoformat())
    log.info("check-in sent")


def finish(cfg, state, now, note=None):
    """Plan + morning message. Marked as done only once the message is out, so a failure (no network yet
    after wake-up, Google hiccup) is retried at the next tick instead of losing the day."""
    today = now.date()
    if state.get_meta(MORNING_SENT) == today.isoformat():
        return  # Done tapped twice, or the timer got here first
    started = state.get_meta(IN_PROGRESS)
    if started and now - datetime.fromisoformat(started) < IN_PROGRESS_STALE:
        log.info("another morning run is in progress; leaving it to finish")
        return
    state.set_meta(IN_PROGRESS, now.isoformat())
    try:
        plan_title, plan_text, _ = planner.plan_today(cfg, state, now, how="auto")
        _, sections = digest.build_digest(cfg, now, skip_planner_blocks=True)
        sections.insert(1, (plan_title.replace("Plan for", "Your plan for"), plan_text.splitlines()))
        title = f"Good morning - {now:%a %d %b}"
        text = ((note + "\n\n") if note else "") + digest.render(title, sections)
        (LOG_DIR / "digest.md").write_text(digest.render(title, sections, markdown=True))
        sent = deliver(title, text, cfg.get("digest", {}).get("channels", ["desktop"]),
                       buttons=[("Clear today's plan", f"clear:{today.isoformat()}")])
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

    import calwatch
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
    except Exception as e:
        log.exception("calendar review failed")
        alerts.alert("review", f"The daily calendar review failed ({type(e).__name__}: {e}).", state)
        return
    state.set_meta(REVIEW_SENT, now.date().isoformat())
    log.info("calendar review: %s", "sent" if sent else "nothing new")


def tick(cfg, state, now):
    today = now.date()
    plan_after = slots.at(today, cfg["planner"]["plan_after"], now.tzinfo)
    if now < plan_after:
        return
    maybe_review(cfg, state, now)
    if state.get_meta(MORNING_SENT) == today.isoformat():
        return
    mc = cfg["morning"]
    if not mc.get("checkin", True):
        finish(cfg, state, now)
        return
    sent = state.get_meta(CHECKIN_SENT)
    if not sent or sent[:10] != today.isoformat():
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
        if not send_review(cfg, state, now):
            Telegram.from_file().send("Calendar review: nothing new or changed on your calendars since the last one.")
    else:
        for key in (CHECKIN_ANSWERED, MORNING_SENT):
            state.set_meta(key, "")
        send_checkin(cfg, state, now, late=False)


if __name__ == "__main__":
    main()
