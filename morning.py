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
from pathlib import Path
from zoneinfo import ZoneInfo

import digest
import planner
import slots
from config import load_config
from notifiers import deliver
from state import State
from telegram_bot import Telegram, TelegramError

LOG_DIR = Path(__file__).parent / "logs"
log = logging.getLogger("morning")

# state.meta keys; values are ISO dates/times
CHECKIN_SENT = "checkin_sent_at"    # when today's question went out
CHECKIN_ANSWERED = "checkin_done"   # date you tapped Done / Nothing today
MORNING_SENT = "morning_sent"       # date the morning message went out


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
    today = now.date()
    if state.get_meta(MORNING_SENT) == today.isoformat():
        return  # Done tapped twice, or the timer got here first
    state.set_meta(MORNING_SENT, today.isoformat())  # before the slow part, so a parallel run stops above

    plan_title, plan_text, _ = planner.plan_today(cfg, state, now, how="auto")
    _, sections = digest.build_digest(cfg, now, skip_planner_blocks=True)
    sections.insert(1, (plan_title.replace("Plan for", "Your plan for"), plan_text.splitlines()))
    title = f"Good morning - {now:%a %d %b}"
    text = ((note + "\n\n") if note else "") + digest.render(title, sections)
    (LOG_DIR / "digest.md").write_text(digest.render(title, sections, markdown=True))
    sent = deliver(title, text, cfg.get("digest", {}).get("channels", ["desktop"]),
                   buttons=[("Clear today's plan", f"clear:{today.isoformat()}")])
    log.info("morning message sent via %s", ", ".join(sent) or "nothing")


def tick(cfg, state, now):
    today = now.date()
    plan_after = slots.at(today, cfg["planner"]["plan_after"], now.tzinfo)
    if now < plan_after or state.get_meta(MORNING_SENT) == today.isoformat():
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
            log.error("couldn't send the check-in (%s); planning without it", e)
            finish(cfg, state, now)
        return
    if state.get_meta(CHECKIN_ANSWERED) == today.isoformat():
        finish(cfg, state, now)
    elif now - datetime.fromisoformat(sent) >= timedelta(minutes=mc["wait_minutes"]):
        finish(cfg, state, now, note="No reply to the check-in, so I planned with what's already in your calendar "
                                     "and tasks. Add to-dos any time with /todo, then tap 'Plan rest of today'.")


def setup_logging():
    LOG_DIR.mkdir(exist_ok=True)
    handler = logging.FileHandler(LOG_DIR / "morning.log")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[handler, logging.StreamHandler()])
    for noisy in ("googleapiclient", "urllib3", "httpx"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--tick", action="store_true", help="timer mode: do the next step if it's due")
    group.add_argument("--finish", action="store_true", help="plan and send the morning message now")
    group.add_argument("--start", action="store_true", help="send today's check-in now (testing)")
    args = parser.parse_args()

    setup_logging()
    cfg = load_config()
    state = State()
    now = datetime.now(ZoneInfo(cfg["timezone"]))
    if args.tick:
        tick(cfg, state, now)
    elif args.finish:
        finish(cfg, state, now)
    else:
        for key in (CHECKIN_ANSWERED, MORNING_SENT):
            state.set_meta(key, "")
        send_checkin(cfg, state, now, late=False)


if __name__ == "__main__":
    main()
