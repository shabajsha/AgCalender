"""Works out today's work: deadlines, exam preparation, your to-dos and dated tasks.

    python planner.py                  # rest of today: send each task's free slots to pick from  (Telegram: /plan)
    python planner.py --suggest-new    # the same, only for tasks that don't have a "when?" message yet (a new /todo)
    python planner.py --print          # preview an automatic plan without touching the calendar
    python planner.py --place          # old behaviour: place the blocks automatically, without asking
    python planner.py --auto           # timer mode for --place (the timer is disabled; morning.py asks instead)
    python planner.py --clear          # remove today's planner-made blocks that haven't started  (Telegram: /clear)
    python planner.py --clear --date 2026-10-02

Free time is computed in Python (slots.py); the LLM only ranks the work (ranker.py).
Only blocks this script made (tagged calendar-agent-planner) are ever moved or deleted.
"""
import argparse
import fcntl
import logging
import math
import re
import time
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from googleapiclient.discovery import build

import alerts
import calwatch
import google_writer
import gtasks
import logsetup
import ranker
import slots
from auth import get_credentials
from config import load_config
from google_writer import DUE_PREFIX, list_events as fetch_events
from notifiers import deliver
from state import State

LOCK_FILE = logsetup.LOG_DIR / ".planner.lock"
LOCK_WAIT_S = 420   # longer than the model's timeout (ollama.timeout_s = 300): a plan can be ranking that long
REPLAN_FLAG = "replan_requested"  # set by the listener when Done is tapped while a plan is running
DAY_NAMES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
# Exams on College (from email, the timetable or Moodle) get preparation time before them.
EXAM_RE = re.compile(r"\b(mid[\s-]?sem(?:ester)?s?|end[\s-]?sem(?:ester)?s?|quiz(?:zes)?|exams?|examinations?|vivas?"
                     r"|(?:class\s+)?tests?)\b", re.IGNORECASE)
DONE_WEIGHT = {"done": 1.0, "partly": 0.5, "notdone": 0.0}  # your answers to "Did you finish ...?"
log = logging.getLogger("planner")


def exam_kind(title):
    """'SDET midsem' -> 'midsem'; 'Quiz 2' -> 'quiz'; not an exam -> None."""
    m = EXAM_RE.search(title or "")
    if not m:
        return None
    word = re.sub(r"[\s-]", "", m.group(1).lower())
    return "midsem" if word.startswith("midsem") else "endsem" if word.startswith("endsem") else \
        "quiz" if word.startswith("quiz") else "exam"


def exam_prep_hours(cfg, kind):
    prep = cfg["planner"].get("exam_prep_hours") or {}
    return prep.get(kind, prep.get("exam", 6))


def _effort(state, key, default):
    """Your chosen effort (0 = "No prep" / nothing to do), else the default."""
    chosen = state.get_effort(key)
    return default if chosen is None else chosen


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
    """Deadlines (DUE events), exams (prep before them) and your own dated tasks, each with its effort in hours.
    Deadlines whose task you've ticked off are left out; tasks overdue by up to carry_over_days are planned today."""
    pc, tz, today = cfg["planner"], now.tzinfo, now.date()
    items = []
    finished = gtasks.completed_ids(tasks_api, cfg["tasklist"]) if cfg.get("tasklist") else set()
    for ev in fetch_events(cal, cfg["calendars"]["college"], now, horizon):
        title = ev.get("summary", "")
        key = f"event:{ev['id']}"
        if not title.startswith(DUE_PREFIX):
            kind = exam_kind(title)
            start = _local(ev["start"], tz) if kind else None
            if kind and start is None:  # all-day exam: prepare before that morning
                start = datetime.combine(date.fromisoformat(ev["start"]["date"]), slots.hm(pc["work_window"][0]), tz)
            if kind and start > now:
                items.append({"key": key, "title": f"Prepare for {title}", "due": start, "kind": "exam",
                              "exam": kind, "effort_h": _effort(state, key, exam_prep_hours(cfg, kind))})
            continue
        if state.task_for_event(ev["id"]) in finished:
            continue  # you've completed its task: stop planning it
        due = _local(ev["end"], tz)
        if due is None:  # an all-day "DUE:" event (made by hand): due at the end of that day
            due = datetime.combine(date.fromisoformat(ev["start"]["date"]), slots.hm("23:59"), tz)
        items.append({"key": key, "title": title[len(DUE_PREFIX):], "due": due, "kind": "deadline",
                      "effort_h": _effort(state, key, pc["default_effort_hours"])})
    carry = timedelta(days=pc.get("carry_over_days", 3))
    for t in gtasks.open_dated_tasks(tasks_api, cfg.get("tasklist"), horizon.date()):
        if t["due"] < today - carry:
            continue  # long overdue: the digest lists it; planning it today would crowd everything else out
        due = datetime.combine(max(t["due"], today), slots.hm("23:59"), tz)  # recently overdue -> today
        key = f"task:{t['id']}"
        items.append({"key": key, "title": t["title"] + (" (overdue)" if t["due"] < today else ""), "due": due,
                      "kind": "task", "list_id": t.get("list_id"), "effort_h": _effort(state, key, pc["task_effort_hours"])})
    return items


def work_days_left(due, today, work_start, gap, min_block):
    """Days from today up to the last one that still has at least `min_block` of work time before the due time.
    (A deadline at 09:00 or midnight leaves no time on its own day; counting that day under-planned it.)"""
    last = due.date()
    if due - timedelta(minutes=gap) - datetime.combine(last, work_start, due.tzinfo) < timedelta(minutes=min_block):
        last -= timedelta(days=1)
    return max(1, (last - today).days + 1)


def minutes_today(item, done_min, today, min_block, work_start=slots.hm("08:00"), gap=15):
    """Even share of the remaining work over the days left (today included), in whole min-block units."""
    remaining = max(0, item["effort_h"] * 60 - done_min)
    if remaining < min_block:
        return 0, remaining
    days_left = work_days_left(item["due"], today, work_start, gap, min_block)
    share = math.ceil(remaining / days_left / min_block) * min_block
    return min(remaining, max(share, min_block)), remaining


def item_min_block(item, pc):
    """Deadlines are worked on in blocks of at least min_block_minutes; a short to-do ("Call bank 15m") gets a
    block of its own length, rounded up to 15 min (it used to be dropped from the plan)."""
    if item["key"].startswith("event:"):
        return pc["min_block_minutes"]
    return min(pc["min_block_minutes"], 15 * max(1, math.ceil(round(item["effort_h"] * 60) / 15)))


class PlannerBusy(Exception):
    """Another plan was still running after LOCK_WAIT_S."""


@contextmanager
def planning_lock(wait_s=LOCK_WAIT_S):
    """Only one plan at a time (two taps on Plan, or Done during the morning run, used to double-book)."""
    LOCK_FILE.parent.mkdir(exist_ok=True)
    with open(LOCK_FILE, "w") as f:
        deadline = time.monotonic() + wait_s
        while True:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() > deadline:
                    raise PlannerBusy("another plan is still running")
                time.sleep(1)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def plan_today(cfg, state, now, dry_run=False, how="manual"):
    """Plans the rest of today. Returns (title, text, number of things placed or reported)."""
    with planning_lock():
        return _plan_today(cfg, state, now, dry_run, how)


def _plan_today(cfg, state, now, dry_run, how):
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
    removed = {b["id"] for cid in earlier for b in earlier[cid]} - {b["id"] for cid in kept for b in kept[cid]}
    free = free_today(cal, cfg, state, now, removed)

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
    done_by_key, _, worked_today, _ = block_minutes(cal, cfg, state, now, ignore_ids=removed)
    items = needed_today(cal, tasks_api, cfg, state, now, done_by_key)
    ranked, used_llm = ranker.rank(items, cfg["ollama"], today)

    work_free = slots.intersect(free, (slots.at(today, pc["work_window"][0], tz), slots.at(today, pc["work_window"][1], tz)))
    cap = max(0, pc["max_work_hours_per_day"] * 60 - worked_today)
    work_blocks, short = [], []
    for it in ranked:
        want = min(it["today_min"], cap)
        before_due = slots.intersect(work_free, (day_start, it["due"] - timedelta(minutes=gap)))  # never after it's due
        blocks, _ = slots.fill(before_due, want, pc["block_minutes"], it["min_block"], gap)
        work_free = slots.subtract(work_free, slots.pad(blocks, gap))
        got = sum((e - s).total_seconds() / 60 for s, e in blocks)
        cap -= got
        work_blocks += [(it, b) for b in blocks]
        if it["today_min"] - got >= it["min_block"]:  # smaller leftovers just roll into tomorrow
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


def free_today(cal, cfg, state, now, ignore_ids=frozenset()):
    """Free time from now to midnight: every calendar's busy time (with gaps) and the sleep window removed.
    Planner blocks already booked count as busy; `ignore_ids` are blocks about to be removed."""
    tz, today, pc, cals = now.tzinfo, now.date(), cfg["planner"], cfg["calendars"]
    day_start, day_end = slots.at(today, "00:00", tz), slots.at(today + timedelta(days=1), "00:00", tz)
    busy = []
    policies = {c["id"]: c["policy"] for c in calwatch.load_calendars(cal, cfg, state)}
    statuses = state.watch_statuses()
    for cid in google_writer.busy_calendar_ids(cal, ["primary", cals["college"], cals["planner"], cals["habits"]]):
        if policies.get(cid) == "ignore":
            continue
        events = [e for e in fetch_events(cal, cid, day_start, day_end)   # events you tapped Ignore on don't block time
                  if calwatch.counts_as_busy(policies.get(cid, "internal"), statuses.get((cid, calwatch.event_key(e))))
                  and e["id"] not in ignore_ids]
        busy += busy_intervals(events, tz)
    return slots.subtract([(max(slots.round_up(now), day_start), day_end)],
                          slots.pad(busy, cfg["planner"]["gap_minutes"]) + slots.sleep_intervals(today, pc["sleep"], tz))


def block_minutes(cal, cfg, state, now, ignore_ids=frozenset()):
    """(done_by_key, booked_today_by_key, worked_today, booked_today): minutes from planner blocks.
    Started blocks count as done, weighted by your answer to "Did you finish?" (done 100%, partly 50%,
    not done 0; not answered yet 100%). Blocks later today that you booked count as booked."""
    tz, today = now.tzinfo, now.date()
    day_end = slots.at(today + timedelta(days=1), "00:00", tz)
    answers = state.block_answers()
    done, booked, worked, booked_total = {}, {}, 0.0, 0.0
    for b in google_writer.list_blocks(cal, cfg["calendars"]["planner"], now - timedelta(days=60), day_end):
        key = b.get("extendedProperties", {}).get("private", {}).get("work_key")
        s, e = _local(b["start"], tz), _local(b["end"], tz)
        if not key or s is None or b["id"] in ignore_ids:
            continue
        minutes = max(0, (e - s).total_seconds() / 60)
        if s < now:
            got = minutes * DONE_WEIGHT.get(answers.get(b["id"]), 1.0)
            done[key] = done.get(key, 0) + got
            if s.date() == today:
                worked += got
        elif s.date() == today:
            booked[key] = booked.get(key, 0) + minutes
            booked_total += minutes
    return done, booked, worked, booked_total


def needed_today(cal, tasks_api, cfg, state, now, done_by_key):
    """Open work with today's share (`today_min`), its smallest block and hours left, in due order."""
    pc, today = cfg["planner"], now.date()
    horizon = slots.at(today, "00:00", now.tzinfo) + timedelta(days=pc["horizon_days"] + 1)
    work_start, gap = slots.hm(pc["work_window"][0]), pc["gap_minutes"]
    items = []
    for it in open_work(cal, tasks_api, cfg, state, now, horizon):
        if it["effort_h"] <= 0:
            continue  # "No prep" / nothing to do
        it["min_block"] = item_min_block(it, pc)
        it["effort_h"] = max(it["effort_h"], it["min_block"] / 60)
        it["today_min"], remaining = minutes_today(it, done_by_key.get(it["key"], 0), today, it["min_block"],
                                                   work_start, gap)
        it["remaining_h"] = round(remaining / 60, 1)
        if it["today_min"]:
            items.append(it)
    return sorted(items, key=lambda it: it["due"])


def work_context(cfg, state, now, cal, tasks_api, rank=False):
    """What the slot suggestions need: (work_free, items, cap_left). work_free = free time inside the work
    window (booked blocks already taken out); each item's `need` = today's share minus what's already booked today."""
    pc, today, tz = cfg["planner"], now.date(), now.tzinfo
    done, booked, worked, booked_total = block_minutes(cal, cfg, state, now)
    free = free_today(cal, cfg, state, now)
    work_free = slots.intersect(free, (slots.at(today, pc["work_window"][0], tz), slots.at(today, pc["work_window"][1], tz)))
    items = needed_today(cal, tasks_api, cfg, state, now, done)
    for it in items:
        it["need"] = max(0, round(it["today_min"] - booked.get(it["key"], 0)))
    if rank:
        items, _ = ranker.rank(items, cfg["ollama"], today)
    cap_left = pc["max_work_hours_per_day"] * 60 - worked - booked_total
    return work_free, items, cap_left


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


def clear_day(cfg, day, now):
    """Removes the day's planned blocks that haven't started. Blocks already begun or done stay: they are the
    record of work done that later plans count. Returns the number removed, or None for a day that is over."""
    if day < now.date():
        return None
    tz = ZoneInfo(cfg["timezone"])
    with planning_lock():
        cal = build("calendar", "v3", credentials=get_credentials())
        start, end = slots.at(day, "00:00", tz), slots.at(day + timedelta(days=1), "00:00", tz)
        removed = 0
        for cid in (cfg["calendars"]["planner"], cfg["calendars"]["habits"]):
            for b in google_writer.list_blocks(cal, cid, start, end):
                begins = _local(b["start"], tz)
                if (b.get("extendedProperties", {}).get("private", {}).get("plan_date") == day.isoformat()
                        and (begins is None or begins >= now)):
                    google_writer.delete_event(cal, cid, b["id"])
                    removed += 1
    return removed


def suggest(cfg, state, now, only_new=False):
    """Telegram: each task that needs time today, with free slots to pick from (slotpicker.py)."""
    import slotpicker
    from telegram_bot import Telegram
    creds = get_credentials()
    cal, tasks_api = build("calendar", "v3", credentials=creds), build("tasks", "v1", credentials=creds)
    _, work_free, cap_left = slotpicker.prepare(cfg, state, now, cal, tasks_api, rank=not only_new)
    tg = Telegram.from_file()
    if not only_new:
        tg.send(f"Rest of today ({now:%a %d %b}, from {slots.round_up(now):%H:%M}):\n"
                + "\n".join(slotpicker.morning_lines(state, now.date(), cap_left, cfg["planner"])))
    sent = slotpicker.send_waiting(cfg, state, now, tg, work_free, resend=not only_new)
    state.record_plan(now.date(), "manual")
    log.info("sent free slots for %d task(s)", sent)


@alerts.guard("planner")
def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--auto", action="store_true", help="timer mode: plan once per day after plan_after")
    parser.add_argument("--print", action="store_true", help="show the plan; change nothing")
    parser.add_argument("--clear", action="store_true", help="remove planner-made blocks for --date (default today)")
    parser.add_argument("--date", type=date.fromisoformat, help="YYYY-MM-DD for --clear")
    parser.add_argument("--place", action="store_true", help="place blocks automatically instead of asking")
    parser.add_argument("--suggest-new", action="store_true", help="only send times for tasks without a message yet")
    args = parser.parse_args()

    logsetup.setup("planner")
    cfg = load_config()
    state = State()
    now = datetime.now(ZoneInfo(cfg["timezone"]))
    channels = cfg.get("digest", {}).get("channels", ["desktop"])

    if args.clear:
        day = args.date or now.date()
        removed = clear_day(cfg, day, now)
        if removed is None:
            deliver(f"Plan for {day:%a %d %b}", "That day is over, so its blocks stay: they're the record of work "
                                               "you did, which later plans count.", channels)
            return
        state.record_plan(day, "cleared")
        for b in state.blocks(statuses=["booked"]):  # no "Did you finish?" for blocks that are gone
            if datetime.fromisoformat(b["start"]) >= now and datetime.fromisoformat(b["start"]).date() == day:
                state.block_set(b["id"], status="cleared")
        log.info("cleared %d planner block(s) on %s", removed, day)
        deliver(f"Plan for {day:%a %d %b} cleared", f"Removed {removed} planned block(s) that hadn't started. "
                "Send /plan to plan again.", channels)
        return

    if not (args.auto or args.print or args.place):
        suggest(cfg, state, now, only_new=args.suggest_new)
        while state.get_meta(REPLAN_FLAG):  # Done was tapped while this ran: once more with the new to-dos
            state.set_meta(REPLAN_FLAG, "")
            suggest(cfg, state, datetime.now(ZoneInfo(cfg["timezone"])), only_new=True)
        return

    plan_after = slots.at(now.date(), cfg["planner"]["plan_after"], now.tzinfo)
    if args.auto and (now < plan_after or state.plan_status(now.date())):
        return  # too early, or today already planned / cleared: nothing to do

    title, text, placed = plan_today(cfg, state, now, dry_run=args.print, how="auto" if args.auto else "manual")
    if not args.print and not args.auto and state.get_meta(REPLAN_FLAG):
        state.set_meta(REPLAN_FLAG, "")  # Done was tapped while this plan ran: plan once more with the new to-dos
        now = datetime.now(ZoneInfo(cfg["timezone"]))
        title, text, placed = plan_today(cfg, state, now, how="manual")
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
