"""Morning digest: today's events, what's due soon, deadlines with no work planned, and items waiting for your
Add / Skip. Written to logs/digest.md and sent through the channels in config.yaml (digest.channels).

    python digest.py            # build, save and send
    python digest.py --print    # only print it: nothing sent, nothing written
"""
import argparse
import logging
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from googleapiclient.discovery import build

from auth import get_credentials
import calwatch
import google_writer
import gtasks
import logsetup
import alerts
from config import load_config
from google_writer import DUE_PREFIX, PLANNER_TAG
from notifiers import deliver
from state import State

LOG_DIR = logsetup.LOG_DIR
log = logging.getLogger("digest")


def _event_times(event, tz):
    """(start, end, all_day); datetimes are local, all-day values are dates."""
    s, e = event["start"], event["end"]
    if "dateTime" in s:
        return datetime.fromisoformat(s["dateTime"]).astimezone(tz), datetime.fromisoformat(e["dateTime"]).astimezone(tz), False
    return date.fromisoformat(s["date"]), date.fromisoformat(e["date"]), True


fetch_events = google_writer.list_events  # kept under the old name for callers and tests


def fetch_tasks(tasks, skip_list_id, due_before):
    """Open tasks with a due date before `due_before` from every list except the agent's own. -> [(due, title, list)]"""
    return [(t["due"], t["title"], t["list_title"]) for t in gtasks.open_dated_tasks(tasks, skip_list_id, due_before)]


def has_work_block(deadline, blocks):
    """A planner block linked to this deadline (the planner tags blocks with deadline_event_id). Matching by
    title is gone: a deadline called "AI" counted as planned by any block with "ai" in its name."""
    return any(b.get("extendedProperties", {}).get("private", {}).get("deadline_event_id") == deadline["id"]
               for b in blocks)


def _due_datetime(event, tz):
    """When a DUE event is due; an all-day one (made by hand) is due at the end of its day."""
    start, end, all_day = _event_times(event, tz)
    return datetime.combine(start, time(23, 59), tz) if all_day else end


def build_digest(cfg, now, skip_planner_blocks=False, state=None):
    """(title, sections). skip_planner_blocks leaves the planner's own blocks out of "Today" (morning.py lists
    the plan separately)."""
    state = state or State()
    tz = now.tzinfo
    days = cfg.get("digest", {}).get("days_ahead", 7)
    today = now.date()
    day_start = datetime.combine(today, time(), tz)
    horizon = day_start + timedelta(days=days + 1)
    creds = get_credentials()
    cal = build("calendar", "v3", credentials=creds)
    tasks = build("tasks", "v1", credentials=creds)
    calendars = {"primary": "primary", **{k: v for k, v in cfg["calendars"].items()}}

    # Today, across every calendar you have switched on. Tracked events appear through their College copy;
    # events still waiting for Track/Ignore are marked; ignored ones and ignored calendars are left out.
    today_lines, statuses = [], state.watch_statuses()
    for c in calwatch.load_calendars(cal, cfg, state):
        if not c["selected"] or c["policy"] == "ignore":
            continue
        for ev in fetch_events(cal, c["id"], day_start, day_start + timedelta(days=1)):
            if skip_planner_blocks and ev.get("extendedProperties", {}).get("private", {}).get("source") == PLANNER_TAG:
                continue
            status = statuses.get((c["id"], calwatch.event_key(ev)))
            if not calwatch.shown_today(c["policy"], status):
                continue
            start, end, all_day = _event_times(ev, tz)
            when = "all day" if all_day else f"{start:%H:%M}-{end:%H:%M}"
            sort_key = "" if all_day else f"{start:%H:%M}"
            undecided = "  - not decided yet" if c["policy"] in ("ask", "copy") else ""
            today_lines.append((sort_key, f"- {when}  {ev.get('summary', '(no title)')}  ({c['label']}){undecided}"))
    today_lines = [line for _, line in sorted(today_lines)] or ["- Nothing scheduled"]

    # Deadlines the agent created (DUE: events end at the due time), and work blocks on the planner
    deadlines = [ev for ev in fetch_events(cal, calendars["college"], now, horizon)
                 if ev.get("summary", "").startswith(DUE_PREFIX)]
    blocks = google_writer.list_blocks(cal, calendars["planner"], now - timedelta(days=60), horizon) if deadlines else []
    blocks = [b for b in blocks if not _event_times(b, tz)[2]]
    finished = gtasks.completed_ids(tasks, cfg["tasklist"]) if deadlines and cfg.get("tasklist") else set()
    pc = cfg.get("planner", {})
    due_lines, unplanned = [], []
    for ev in deadlines:
        due = _due_datetime(ev, tz)
        all_day = _event_times(ev, tz)[2]
        stamp = f"{due:%a %d %b}" if all_day else f"{due:%a %d %b %H:%M}"
        name = ev["summary"][len(DUE_PREFIX):]
        if state.task_for_event(ev["id"]) in finished:  # you ticked its task off: done, not "unplanned"
            due_lines.append(f"- {stamp}  {name}  (done)")
            continue
        due_lines.append(f"- {stamp}  {name}")
        linked = [b for b in blocks if _event_times(b, tz)[0] < due and has_work_block(ev, [b])]
        worked = sum((_event_times(b, tz)[1] - _event_times(b, tz)[0]).total_seconds() / 3600
                     for b in linked if _event_times(b, tz)[0] < now)
        effort = state.get_effort(f"event:{ev['id']}") or pc.get("default_effort_hours", 3)
        if not any(_event_times(b, tz)[0] >= now for b in linked) and worked < effort:
            unplanned.append(f"- {name} (due {stamp})")

    # Tasks from your own lists, including overdue ones
    task_lines = []
    for due, title, list_name in fetch_tasks(tasks, cfg.get("tasklist"), today + timedelta(days=days + 1)):
        label = f"OVERDUE since {due:%a %d %b}" if due < today else f"{due:%a %d %b}"
        task_lines.append(f"- {label}  {title}  ({list_name})")

    # Items still waiting for Add / Skip
    waiting = []
    for row in state.open_pending():
        item = row["item"]
        when = item["due"] or item["start"]
        if (when if isinstance(when, datetime) else datetime.combine(when, time(23, 59), tz)) >= now:
            stamp = f"{when:%a %d %b %H:%M}" if isinstance(when, datetime) else f"{when:%a %d %b}"
            waiting.append(f"- {item['type'].capitalize()}: {item['title']} ({stamp})")

    skipped = state.skipped_messages(limit=50, since=now - timedelta(days=1))

    sections = []
    paused = state.paused_since()
    if paused:
        sections.append(("Mail reading is paused", [f"- since {paused.astimezone(tz):%a %d %b %H:%M}. Send /resume in Telegram to continue."]))
    sections += [("Today", today_lines),
                (f"Deadlines in the next {days} days", due_lines or ["- None"])]
    if unplanned:
        sections.append(("No work time planned yet", unplanned))
    if task_lines:
        sections.append(("Your tasks due soon", task_lines))
    if waiting:
        sections.append((f"Waiting for your Add / Skip ({len(waiting)})", waiting))
    if skipped:
        sections.append(("Emails I skipped (no deadline words)",
                         [f"- {len(skipped)} since yesterday. Send /skipped to see them and read one anyway."]))
    return f"Calendar digest - {now:%a %d %b}", sections


def render(title, sections, markdown=False):
    if markdown:
        return f"# {title}\n\n" + "\n\n".join(f"## {h}\n" + "\n".join(lines) for h, lines in sections) + "\n"
    return "\n\n".join(f"{h}\n" + "\n".join(lines) for h, lines in sections)


@alerts.guard("digest")
def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--print", action="store_true", help="print the digest only; send and write nothing")
    args = parser.parse_args()

    logsetup.setup("digest")
    cfg = load_config()
    now = datetime.now(ZoneInfo(cfg["timezone"]))
    title, sections = build_digest(cfg, now)
    if args.print:
        print(title + "\n\n" + render(title, sections))
        return
    (LOG_DIR / "digest.md").write_text(render(title, sections, markdown=True))
    sent = deliver(title, render(title, sections), cfg.get("digest", {}).get("channels", ["desktop"]))
    log.info("digest written to logs/digest.md; sent via %s", ", ".join(sent) or "nothing")


if __name__ == "__main__":
    main()
