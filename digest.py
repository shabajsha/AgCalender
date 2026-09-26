"""Morning digest: today's events, what's due soon, deadlines with no work planned, and items waiting for your
Add / Skip. Written to logs/digest.md and sent through the channels in config.yaml (digest.channels).

    python digest.py            # build, save and send
    python digest.py --print    # only print it: nothing sent, nothing written
"""
import argparse
import logging
import re
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from googleapiclient.discovery import build

from auth import get_credentials
from config import load_config
from google_writer import PLANNER_TAG
from notifiers import deliver
from state import State

LOG_DIR = Path(__file__).parent / "logs"
DUE_PREFIX = "DUE: "
log = logging.getLogger("digest")


def _norm(text):
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def _event_times(event, tz):
    """(start, end, all_day); datetimes are local, all-day values are dates."""
    s, e = event["start"], event["end"]
    if "dateTime" in s:
        return datetime.fromisoformat(s["dateTime"]).astimezone(tz), datetime.fromisoformat(e["dateTime"]).astimezone(tz), False
    return date.fromisoformat(s["date"]), date.fromisoformat(e["date"]), True


def fetch_events(cal, calendar_id, start, end):
    events, token = [], None
    while True:
        resp = cal.events().list(calendarId=calendar_id, timeMin=start.isoformat(), timeMax=end.isoformat(),
                                 singleEvents=True, orderBy="startTime", maxResults=250, pageToken=token).execute()
        events += [e for e in resp.get("items", []) if e.get("status") != "cancelled"]
        token = resp.get("nextPageToken")
        if not token:
            return events


def fetch_tasks(tasks, skip_list_id, due_before):
    """Open tasks with a due date before `due_before` from every list except the agent's own. -> [(due, title, list)]"""
    found = []
    for tl in tasks.tasklists().list().execute().get("items", []):
        if tl["id"] == skip_list_id:
            continue  # agent tasks duplicate the DUE: events, which carry the exact time
        token = None
        while True:
            resp = tasks.tasks().list(tasklist=tl["id"], showCompleted=False, showHidden=False,
                                      dueMax=f"{due_before.isoformat()}T00:00:00.000Z", pageToken=token).execute()
            for t in resp.get("items", []):
                if t.get("due") and t.get("title"):
                    found.append((date.fromisoformat(t["due"][:10]), t["title"], tl["title"]))
            token = resp.get("nextPageToken")
            if not token:
                break
    return sorted(found)


def has_work_block(deadline, blocks):
    """A planner event before the deadline that is linked to it (Phase 3 sets deadline_event_id) or names it."""
    title = _norm(deadline["summary"][len(DUE_PREFIX):])
    for b in blocks:
        props = b.get("extendedProperties", {}).get("private", {})
        if props.get("deadline_event_id") == deadline["id"] or (title and title in _norm(b.get("summary", ""))):
            return True
    return False


def build_digest(cfg, now, skip_planner_blocks=False):
    """(title, sections). skip_planner_blocks leaves the planner's own blocks out of "Today" (morning.py lists
    the plan separately)."""
    tz = now.tzinfo
    days = cfg.get("digest", {}).get("days_ahead", 7)
    today = now.date()
    day_start = datetime.combine(today, time(), tz)
    horizon = day_start + timedelta(days=days + 1)
    creds = get_credentials()
    cal = build("calendar", "v3", credentials=creds)
    tasks = build("tasks", "v1", credentials=creds)
    calendars = {"primary": "primary", **{k: v for k, v in cfg["calendars"].items()}}

    # Today, across all calendars
    today_lines = []
    for name, cal_id in calendars.items():
        for ev in fetch_events(cal, cal_id, day_start, day_start + timedelta(days=1)):
            if skip_planner_blocks and ev.get("extendedProperties", {}).get("private", {}).get("source") == PLANNER_TAG:
                continue
            start, end, all_day = _event_times(ev, tz)
            when = "all day" if all_day else f"{start:%H:%M}-{end:%H:%M}"
            sort_key = "" if all_day else f"{start:%H:%M}"
            today_lines.append((sort_key, f"- {when}  {ev.get('summary', '(no title)')}  ({name})"))
    today_lines = [line for _, line in sorted(today_lines)] or ["- Nothing scheduled"]

    # Deadlines the agent created (DUE: events end at the due time), and work blocks on the planner
    deadlines = [ev for ev in fetch_events(cal, calendars["college"], now, horizon)
                 if ev.get("summary", "").startswith(DUE_PREFIX)]
    blocks = fetch_events(cal, calendars["planner"], now, horizon) if deadlines else []
    due_lines, unplanned = [], []
    for ev in deadlines:
        _, due, all_day = _event_times(ev, tz)
        stamp = f"{due:%a %d %b}" if all_day else f"{due:%a %d %b %H:%M}"
        name = ev["summary"][len(DUE_PREFIX):]
        due_lines.append(f"- {stamp}  {name}")
        if not has_work_block(ev, [b for b in blocks if _event_times(b, tz)[0] < due]):
            unplanned.append(f"- {name} (due {stamp})")

    # Tasks from your own lists, including overdue ones
    task_lines = []
    for due, title, list_name in fetch_tasks(tasks, cfg.get("tasklist"), today + timedelta(days=days + 1)):
        label = f"OVERDUE since {due:%a %d %b}" if due < today else f"{due:%a %d %b}"
        task_lines.append(f"- {label}  {title}  ({list_name})")

    # Items still waiting for Add / Skip
    waiting = []
    for row in State().open_pending():
        item = row["item"]
        when = item["due"] or item["start"]
        if (when if isinstance(when, datetime) else datetime.combine(when, time(23, 59), tz)) >= now:
            stamp = f"{when:%a %d %b %H:%M}" if isinstance(when, datetime) else f"{when:%a %d %b}"
            waiting.append(f"- {item['type'].capitalize()}: {item['title']} ({stamp})")

    sections = []
    paused = State().paused_since()
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
    return f"Calendar digest - {now:%a %d %b}", sections


def render(title, sections, markdown=False):
    if markdown:
        return f"# {title}\n\n" + "\n\n".join(f"## {h}\n" + "\n".join(lines) for h, lines in sections) + "\n"
    return "\n\n".join(f"{h}\n" + "\n".join(lines) for h, lines in sections)


def setup_logging():
    LOG_DIR.mkdir(exist_ok=True)
    handler = logging.FileHandler(LOG_DIR / "digest.log")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[handler, logging.StreamHandler()])
    for noisy in ("googleapiclient", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--print", action="store_true", help="print the digest only; send and write nothing")
    args = parser.parse_args()

    setup_logging()
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
