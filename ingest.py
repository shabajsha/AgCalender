"""Reads new forwarded IIITH emails and creates calendar events / tasks from them.

    python ingest.py              # normal run: emails since the last run
    python ingest.py --dry-run    # show what would be created, write nothing
    python ingest.py --since 14   # backfill the last 14 days

With approval.enabled in config.yaml, new items are sent to Telegram for Add / Skip instead of being
created directly; approvals.py (which must be running) acts on your taps.
"""
import argparse
import fnmatch
import logging
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests
from googleapiclient.discovery import build

import alerts
import approvals
import calwatch
import google_writer
import logsetup
from auth import get_credentials
from config import load_config
from extractor import LLMUnavailable, extract_items, matches_keywords
from gmail_reader import fetch_messages, fingerprint, sender_address
from ics_import import is_past, parse_ics
from state import State, dedupe_key
from telegram_bot import Telegram, TelegramError

log = logging.getLogger("ingest")
MAX_TRIES = 3        # an email that errors this many times is given up on (with an alert) so it can't block progress
MAX_ICS_ITEMS = 5    # same cap the LLM path has
ERROR_RUNS_ALERT = 3


def ping_heartbeat(url):
    """Optional 'I'm alive' ping (e.g. healthchecks.io). If pings stop, that service can alert you that the
    laptop is off - the one thing the laptop can't tell you itself."""
    if not url:
        return
    try:
        requests.get(url, timeout=10)
    except requests.RequestException as e:
        log.warning("heartbeat ping failed (%s)", type(e).__name__)


def describe(item):
    when = item["due"] or item["start"]
    stamp = when.strftime("%a %Y-%m-%d %H:%M") if isinstance(when, datetime) else when.strftime("%a %Y-%m-%d (all day)")
    extra = " (+task)" if item["type"] == "deadline" else ""
    return f"[{item['type']}] {google_writer.event_title(item)} @ {stamp}{extra}"


def is_skipped_sender(msg, patterns):
    """Patterns from config.skip_senders match anywhere in the forwarded From: line (which includes the list address)."""
    line = msg.get("from_line", "").lower()
    return any(fnmatch.fnmatch(line, f"*{p.lower()}*") for p in patterns or [])


def items_for(msg, cfg, now):
    """Returns (items, outcome_if_none). .ics -> parser; otherwise keyword filter + LLM."""
    if msg["ics"]:
        items = []
        for raw in msg["ics"]:
            items += [it for it in parse_ics(raw, cfg["timezone"]) if not is_past(it, now)]
        if len(items) > MAX_ICS_ITEMS:
            log.warning("invite had %d events; keeping the first %d", len(items), MAX_ICS_ITEMS)
        return items[:MAX_ICS_ITEMS], "no-items"
    if not matches_keywords(msg["subject"] + "\n" + msg["body"], cfg["keywords"]):
        return [], "no-keyword"
    log.info("asking LLM about: %s", msg["subject"])
    return extract_items(msg, cfg["ollama"], cfg["timezone"], cfg["min_confidence"], now), "no-items"


def watch_calendars(cfg, state, calendar, tasks, tg, dry_run):
    """New events on your other calendars -> Track/Ignore cards (calwatch.py). Runs with every mail check."""
    if not cfg.get("calendar_watch", {}).get("enabled"):
        return
    if tg is None and not dry_run:
        log.info("calendar watch needs Telegram (approval.enabled); skipped")
        return
    try:
        stats = calwatch.Watcher(cfg, state, calendar, tasks, tg, datetime.now(ZoneInfo(cfg["timezone"])),
                                 dry_run=dry_run).run()
        log.info("calendars: %d new, %d asked, %d changed, %d cancelled", stats["new"], stats["asked"],
                 stats["changed"], stats["gone"])
        if not dry_run:
            alerts.resolved("calwatch", state)
    except Exception as e:  # a calendar problem must not stop mail from being read
        log.exception("calendar watch failed")
        if not dry_run:
            alerts.alert("calwatch", f"Checking your other calendars failed ({type(e).__name__}: {e}).", state)


@alerts.guard("ingest")
def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="print what would be created; write nothing")
    parser.add_argument("--since", type=float, metavar="DAYS", help="look back this many days instead of since last run")
    args = parser.parse_args()

    logsetup.setup("ingest")
    cfg = load_config()
    state = State(dry_run=args.dry_run)
    run_started = datetime.now(timezone.utc)
    now = run_started.astimezone()

    if args.since is not None:
        after = run_started - timedelta(days=args.since)
    elif state.get_last_run():
        after = state.get_last_run() - timedelta(hours=1)  # overlap is safe: message IDs are tracked
    else:
        after = run_started - timedelta(days=cfg.get("first_run_days", 3))

    ask_first = cfg.get("approval", {}).get("enabled", False)
    tg = None
    if ask_first and not args.dry_run:
        try:
            tg = Telegram.from_file()
        except TelegramError as e:
            raise SystemExit(f"approval is enabled but {e}")
    blocked = state.blocked_senders()

    paused = state.paused_since()
    if paused and not args.dry_run:
        log.info("mail reading paused since %s (Telegram /pause); nothing read. Send /resume to continue.",
                 paused.astimezone().strftime("%a %d %b %H:%M"))
        ping_heartbeat(cfg.get("heartbeat_url"))  # paused, but the laptop is alive
        return

    creds = get_credentials()
    gmail = build("gmail", "v1", credentials=creds)
    calendar = build("calendar", "v3", credentials=creds)
    tasks = build("tasks", "v1", credentials=creds)

    prefix = "DRY RUN - " if args.dry_run else ""
    log.info("%sfetching %r after %s", prefix, cfg["gmail_query"], after.astimezone().strftime("%Y-%m-%d %H:%M"))
    stats = {"seen": 0, "already": 0, "duplicate": 0, "skipped-sender": 0, "no-keyword": 0,
             "created": 0, "asked": 0, "skipped-existing": 0, "errors": 0, "given-up": 0}

    def already_processed(msg_id):  # checked before download, so old mail isn't fetched again every run
        stats["seen"] += 1
        if state.is_processed(msg_id):
            stats["already"] += 1
            return True
        return False

    messages = fetch_messages(gmail, cfg["gmail_query"], after, cfg["timezone"], skip=already_processed)
    for msg in messages:
        fp = fingerprint(msg)
        if state.fingerprint_seen(fp):
            stats["duplicate"] += 1
            state.mark_processed(msg["id"], fp, "duplicate")
            continue
        sender = sender_address(msg["sender"])
        if is_skipped_sender(msg, cfg.get("skip_senders")) or sender in blocked:
            stats["skipped-sender"] += 1
            state.mark_processed(msg["id"], fp, "skipped-sender")
            continue

        try:
            items, outcome = items_for(msg, cfg, now)
            if outcome == "no-keyword":
                stats["no-keyword"] += 1
            for item in items:
                key = dedupe_key(item)
                if state.item_exists(key) or state.pending_exists(key):
                    stats["skipped-existing"] += 1
                    log.info("already exists: %s", describe(item))
                    continue
                if args.dry_run:
                    print(f"WOULD {'ASK ABOUT' if ask_first else 'CREATE'} {describe(item)}  <- {msg['subject']}", flush=True)
                    stats["asked" if ask_first else "created"] += 1
                    continue
                if ask_first:
                    approvals.ask(tg, state, key, item, msg, sender)
                    log.info("asked on Telegram: %s  <- %s", describe(item), msg["subject"])
                    stats["asked"] += 1
                    continue
                event_id, task_id = google_writer.create_item(calendar, tasks, cfg, item, msg)
                log.info("created %s  <- %s", describe(item), msg["subject"])
                state.record_item(key, item["type"], event_id, task_id, item["title"], str(item["due"] or item["start"]), msg["id"])
                stats["created"] += 1
            state.mark_processed(msg["id"], fp, "items" if items else outcome)
        except LLMUnavailable as e:  # temporary (RAM, GPU, Ollama down): retry without counting it against the email
            stats["errors"] += 1
            log.error("Ollama unavailable (%s); will retry %r next run", e, msg["subject"])
        except Exception as e:
            tries = state.record_failure(msg["id"], f"{type(e).__name__}: {e}")
            log.exception("failed on message %s (%r), attempt %d of %d", msg["id"], msg["subject"], tries, MAX_TRIES)
            if tries >= MAX_TRIES:
                state.mark_processed(msg["id"], fp, "failed")
                stats["given-up"] += 1
                if not args.dry_run:
                    alerts.alert(f"gave-up:{msg['id']}", f"Couldn't read the email {msg['subject']!r} after {tries} tries "
                                 f"({type(e).__name__}); skipping it. Please check it yourself.", state)
            else:
                stats["errors"] += 1

    watch_calendars(cfg, state, calendar, tasks, tg, args.dry_run)

    if not args.dry_run:
        if stats["errors"] == 0:
            state.set_last_run(run_started)
        error_runs = int(state.get_meta("ingest_error_runs") or 0) + 1 if stats["errors"] else 0
        state.set_meta("ingest_error_runs", str(error_runs))
        if error_runs >= ERROR_RUNS_ALERT:
            alerts.alert("ingest-errors", f"The last {error_runs} mail checks had errors (see logs/ingest.log). "
                         "New mail may not be reaching you.", state)
        elif not error_runs:
            alerts.resolved("ingest-errors", state)
        ping_heartbeat(cfg.get("heartbeat_url"))
    verb = "would create" if args.dry_run else "created"
    log.info("%sseen %d | already processed %d | duplicate forwards %d | skipped sender %d | no keyword %d | %s %d | "
             "asked on Telegram %d | existing %d | errors %d | given up %d",
             prefix, stats["seen"], stats["already"], stats["duplicate"], stats["skipped-sender"], stats["no-keyword"], verb,
             stats["created"], stats["asked"], stats["skipped-existing"], stats["errors"], stats["given-up"])


if __name__ == "__main__":
    main()
