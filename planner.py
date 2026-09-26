"""Plans the rest of today: habits first, then work blocks for open deadlines and tasks.

    python planner.py                  # (re)plan the rest of today  (Telegram: /plan)
    python planner.py --auto           # timer mode: plan once a day, the first time the laptop is on after plan_after
    python planner.py --print          # show the plan without touching the calendar
    python planner.py --clear          # remove today's planner-made blocks  (Telegram: /clear)
    python planner.py --clear --date 2026-10-02

Free time is computed in Python (slots.py); the LLM only ranks the work (ranker.py).
Only blocks this script made (tagged calendar-agent-planner) are ever moved or deleted.
"""
import argparse
import logging
import math
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from googleapiclient.discovery import build

import google_writer
import ranker
import slots
from auth import get_credentials
from config import load_config
from digest import DUE_PREFIX, fetch_events
from notifiers import deliver
from state import State

LOG_DIR = Path(__file__).parent / "logs"
DAY_NAMES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
log = logging.getLogger("planner")


def _local(when, tz):
    return datetime.fromisoformat(when["dateTime"]).astimezone(tz) if "dateTime" in when else None


def busy_intervals(events, tz):
    """Timed, opaque events. All-day events, 'free' events and DUE markers don't block time."""
    busy = []
    for ev in events:
        s, e = _local(ev["start"], tz), _local(ev["end"], tz)
        if s is None or ev.get("transparency") == "transparent" or ev.get("summary", "").startswith(DUE_PREFIX):
            continue
        busy.append((s, e))
    return busy


def open_work(cal, tasks_api, cfg, state, now, horizon):
    """Deadlines (DUE events) and your own dated tasks, each with its effort in hours."""
    pc, tz = cfg["planner"], now.tzinfo
    items = []
    for ev in fetch_events(cal, cfg["calendars"]["college"], now, horizon):
        if ev.get("summary", "").startswith(DUE_PREFIX):
            key = f"event:{ev['id']}"
            items.append({"key": key, "title": ev["summary"][len(DUE_PREFIX):], "due": _local(ev["end"], tz),
                          "effort_h": state.get_effort(key) or pc["default_effort_hours"]})
    for tl in tasks_api.tasklists().list().execute().get("items", []):
        if tl["id"] == cfg.get("tasklist"):
            continue  # agent tasks mirror the DUE events above
        resp = tasks_api.tasks().list(tasklist=tl["id"], showCompleted=False, showHidden=False,
                                      dueMax=f"{horizon.date().isoformat()}T00:00:00.000Z").execute()
        for t in resp.get("items", []):
            if not t.get("due") or not t.get("title"):
                continue
            due = datetime.combine(date.fromisoformat(t["due"][:10]), slots.hm("23:59"), tz)
            if due < now:
                continue  # overdue tasks show in the digest; there's no window left to plan them into
            key = f"task:{t['id']}"
            items.append({"key": key, "title": t["title"], "due": due,
                          "effort_h": state.get_effort(key) or pc["task_effort_hours"]})
    return items


def minutes_today(item, done_min, today, min_block):
    """Even share of the remaining work over the days left (today included), in whole min-block units."""
    remaining = max(0, item["effort_h"] * 60 - done_min)
    if remaining < min_block:
        return 0, remaining
    days_left = max(1, (item["due"].date() - today).days + 1)
    share = math.ceil(remaining / days_left / min_block) * min_block
    return min(remaining, max(share, min_block)), remaining


def plan_today(cfg, state, now, dry_run=False, how="manual"):
    tz, today, pc = now.tzinfo, now.date(), cfg["planner"]
    gap = pc["gap_minutes"]
    creds = get_credentials()
    cal = build("calendar", "v3", credentials=creds)
    tasks_api = build("tasks", "v1", credentials=creds)
    cals = cfg["calendars"]
    day_start, day_end = slots.at(today, "00:00", tz), slots.at(today + timedelta(days=1), "00:00", tz)

    # 1. Forget the not-yet-started part of any earlier plan for today (blocks already begun or done stay).
    earlier = {cid: google_writer.list_blocks(cal, cid, day_start, day_end) for cid in (cals["planner"], cals["habits"])}
    kept = {cid: [] for cid in earlier}
    for cid, blocks in earlier.items():
        for b in blocks:
            if _local(b["start"], tz) >= now:
                if not dry_run:
                    google_writer.delete_event(cal, cid, b["id"])
            else:
                kept[cid].append(b)

    # 2. Free time from now to midnight: minus every calendar's busy time (with gaps) and sleep.
    busy = []
    for cid in ("primary", cals["college"], cals["planner"], cals["habits"]):
        events = fetch_events(cal, cid, day_start, day_end)
        if cid in earlier:
            removed = {b["id"] for b in earlier[cid]} - {b["id"] for b in kept[cid]}
            events = [e for e in events if e["id"] not in removed]
        busy += busy_intervals(events, tz)
    free = slots.subtract([(max(slots.round_up(now), day_start), day_end)],
                          slots.pad(busy, gap) + slots.sleep_intervals(today, pc["sleep"], tz))

    # 3. Habits first, inside their own windows.
    done_habits = {b.get("summary") for b in kept[cals["habits"]]}
    habit_blocks, missed_habits = [], []
    for h in cfg.get("habits") or []:
        if DAY_NAMES[today.weekday()] not in [d.lower()[:3] for d in h.get("days", DAY_NAMES)] or h["name"] in done_habits:
            continue
        window = (slots.at(today, h["window"][0], tz), slots.at(today, h["window"][1], tz))
        block, free = slots.place_one(free, h["minutes"], gap, window=window)
        (habit_blocks.append((h["name"], block)) if block else missed_habits.append(h["name"]))

    # 4. Work: how much each item needs today, ranked, then placed earliest-first inside the work window.
    horizon = day_start + timedelta(days=pc["horizon_days"] + 1)
    history = google_writer.list_blocks(cal, cals["planner"], now - timedelta(days=60), now)
    done_by_key = {}
    for b in history:
        key = b.get("extendedProperties", {}).get("private", {}).get("work_key")
        if key:
            s, e = _local(b["start"], tz), min(_local(b["end"], tz), now)
            done_by_key[key] = done_by_key.get(key, 0) + max(0, (e - s).total_seconds() / 60)
    items = []
    for it in open_work(cal, tasks_api, cfg, state, now, horizon):
        it["today_min"], remaining = minutes_today(it, done_by_key.get(it["key"], 0), today, pc["min_block_minutes"])
        it["remaining_h"] = round(remaining / 60, 1)
        if it["today_min"]:
            items.append(it)
    ranked, used_llm = ranker.rank(items, cfg["ollama"], today)

    work_free = slots.intersect(free, (slots.at(today, pc["work_window"][0], tz), slots.at(today, pc["work_window"][1], tz)))
    worked_today = sum((min(_local(b["end"], tz), now) - _local(b["start"], tz)).total_seconds() / 60
                       for b in kept[cals["planner"]])
    cap = max(0, pc["max_work_hours_per_day"] * 60 - worked_today)
    work_blocks, short = [], []
    for it in ranked:
        want = min(it["today_min"], cap)
        blocks, work_free = slots.fill(work_free, want, pc["block_minutes"], pc["min_block_minutes"], gap)
        got = sum((e - s).total_seconds() / 60 for s, e in blocks)
        cap -= got
        work_blocks += [(it, b) for b in blocks]
        if it["today_min"] - got >= pc["min_block_minutes"]:  # smaller leftovers just roll into tomorrow
            short.append((it, it["today_min"] - got))

    # 5. Write it.
    if not dry_run:
        for name, (s, e) in habit_blocks:
            google_writer.create_block(cal, cals["habits"], name, s, e, cfg["timezone"], "habit")
        for it, (s, e) in work_blocks:
            google_writer.create_block(cal, cals["planner"], f"Work: {it['title']}", s, e, cfg["timezone"], "work",
                                       work_key=it["key"], note=f"Due {it['due']:%a %d %b %H:%M}")
        state.record_plan(today, how)
    title, text = summarize(now, habit_blocks, missed_habits, work_blocks, short, used_llm, len(items) > 1)
    return title, text, len(habit_blocks) + len(work_blocks) + len(short) + len(missed_habits)


def summarize(now, habit_blocks, missed_habits, work_blocks, short, used_llm, ranked_several):
    lines = []
    if habit_blocks or missed_habits:
        lines.append("Habits")
        lines += [f"- {s:%H:%M}-{e:%H:%M}  {name}" for name, (s, e) in sorted(habit_blocks, key=lambda x: x[1])]
        lines += [f"- {name}: no free slot in its window today" for name in missed_habits]
        lines.append("")
    lines.append("Work")
    if work_blocks:
        for it, (s, e) in sorted(work_blocks, key=lambda x: x[1]):
            lines.append(f"- {s:%H:%M}-{e:%H:%M}  {it['title']} (due {it['due']:%a %d %b}, {it['remaining_h']:g} h left)")
    else:
        lines.append("- Nothing to plan" if not short else "- No free time left today")
    if short:
        lines += ["", "Didn't fit today"] + [f"- {it['title']}: {m / 60:g} h" for it, m in short]
    if ranked_several:
        lines += ["", "Order chosen by the model." if used_llm else "Ordered by due date (model unavailable)."]
    return f"Plan for {now:%a %d %b} (from {slots.round_up(now):%H:%M})", "\n".join(lines)


def clear_day(cfg, day):
    tz = ZoneInfo(cfg["timezone"])
    cal = build("calendar", "v3", credentials=get_credentials())
    start, end = slots.at(day, "00:00", tz), slots.at(day + timedelta(days=1), "00:00", tz)
    removed = 0
    for cid in (cfg["calendars"]["planner"], cfg["calendars"]["habits"]):
        for b in google_writer.list_blocks(cal, cid, start, end):
            if b.get("extendedProperties", {}).get("private", {}).get("plan_date") == day.isoformat():
                google_writer.delete_event(cal, cid, b["id"])
                removed += 1
    return removed


def setup_logging():
    LOG_DIR.mkdir(exist_ok=True)
    handler = logging.FileHandler(LOG_DIR / "planner.log")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[handler, logging.StreamHandler()])
    for noisy in ("googleapiclient", "urllib3", "httpx"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--auto", action="store_true", help="timer mode: plan once per day after plan_after")
    parser.add_argument("--print", action="store_true", help="show the plan; change nothing")
    parser.add_argument("--clear", action="store_true", help="remove planner-made blocks for --date (default today)")
    parser.add_argument("--date", type=date.fromisoformat, help="YYYY-MM-DD for --clear")
    args = parser.parse_args()

    setup_logging()
    cfg = load_config()
    state = State()
    now = datetime.now(ZoneInfo(cfg["timezone"]))
    channels = cfg.get("digest", {}).get("channels", ["desktop"])

    if args.clear:
        day = args.date or now.date()
        removed = clear_day(cfg, day)
        state.record_plan(day, "cleared")
        log.info("cleared %d planner block(s) on %s", removed, day)
        deliver(f"Plan for {day:%a %d %b} cleared", f"Removed {removed} planned block(s). Send /plan to plan again.", channels)
        return

    plan_after = slots.at(now.date(), cfg["planner"]["plan_after"], now.tzinfo)
    if args.auto and (now < plan_after or state.plan_status(now.date())):
        return  # too early, or today already planned / cleared: nothing to do

    title, text, placed = plan_today(cfg, state, now, dry_run=args.print, how="auto" if args.auto else "manual")
    if args.print:
        print(title + "\n\n" + text)
        return
    if args.auto and now > plan_after + timedelta(minutes=30):
        text = f"The laptop was off or asleep at {cfg['planner']['plan_after']}, so today's plan starts now.\n\n" + text
    log.info("%s\n%s", title, text)
    if args.auto and not placed:
        return  # nothing to do today: don't send a message about it
    deliver(title, text, channels, buttons=[("Clear today's plan", f"clear:{now.date().isoformat()}")])


if __name__ == "__main__":
    main()
