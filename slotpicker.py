"""You choose when: free slots per task, a check after each slot, and an evening check.

Nothing goes on your calendar until you tap a time. After the morning check-in (and on "Plan rest of today"),
every task that needs time today gets its own message with free slots as buttons:

    Study SDET MidSem
    2 h today (due Sun 27 Sep 23:59). Pick a time for the first 1 h 30 min:
    [11:00-12:30] [14:00-15:30] [19:00-20:30]   [More times] [Not today]

Tapping a time puts a block on the Planner calendar and refreshes the other messages (that time is taken now).
A task you don't give a time stays unbooked; you're reminded once, `morning.remind_after_minutes` later, with
fresh slots. When a booked slot ends you're asked "Did you finish?" (Done / Partly / Not done): Done ticks the
to-do off in Google Tasks, anything else offers new slots. At `morning.evening_check` you get the day's summary
with "Move unfinished to tomorrow".

Free time and amounts come from planner.work_context (pure Python); the model never picks times. Exams on College
get preparation time before them (planner.exam_kind); /exams sets how much.
"""
import logging
import math
from datetime import date, datetime, timedelta

from googleapiclient.errors import HttpError

import google_writer
import planner
import slots

log = logging.getLogger("slotpicker")
SHOWN, MORE = 3, 8                        # slot buttons at first / after "More times"
STEP = timedelta(minutes=30)
PERIODS = ((0, 12), (12, 17), (17, 24))   # one suggestion each in the morning, afternoon and evening if possible
EVENING_SENT = "evening_check_sent"       # state.meta: date of the last evening check
EXAM_CHOICES = [2, 4, 8, 12, 15]


def fmt(minutes):
    h, m = divmod(int(round(minutes)), 60)
    return f"{h} h {m} min" if h and m else f"{h} h" if h else f"{m} min"


def _round15(minutes):
    return 15 * max(1, math.ceil(minutes / 15))


def options(work_free, minutes, due, gap, count=SHOWN, spread=True):
    """(start, end) pairs for a block of `minutes` in free time before `due - gap`: spread over morning, afternoon
    and evening when possible (you choose), else the earliest ones. `spread=False`: hourly from the earliest."""
    need, last = timedelta(minutes=minutes), due - timedelta(minutes=gap)
    starts = []
    for s, e in slots.quarter([(s, min(e, last)) for s, e in work_free if min(e, last) > s]):
        t = s
        while t + need <= e:
            starts.append(t)
            t += STEP
    if not spread:
        picked, t_last = [], None
        for t in starts:
            if t_last is None or t - t_last >= timedelta(hours=1):
                picked.append(t)
                t_last = t
        return [(t, t + need) for t in picked[:count]]
    picked = []
    for lo, hi in PERIODS:
        first = next((t for t in starts if lo <= t.hour < hi), None)
        if first is not None:
            picked.append(first)
    for t in starts:  # fewer than `count` periods have room: add the next ones, an hour apart
        if len(picked) >= count:
            break
        if all(abs(t - p) >= timedelta(hours=1) for p in picked):
            picked.append(t)
    return sorted((t, t + need) for t in picked[:count])


def _chunk(row, pc):
    """How long the next block should be: the rest of today's time, at most block_minutes."""
    return min(_round15(row["minutes"]), pc["block_minutes"])


def window_free(free, row, pc, now):
    """The free time this task may use: its habit window, else work hours."""
    due = datetime.fromisoformat(row["due"])
    start = row.get("window_start") or pc["work_window"][0]
    end = due if row["kind"] == "habit" else slots.at(now.date(), pc["work_window"][1], now.tzinfo)
    return slots.intersect(free, (slots.at(now.date(), start, now.tzinfo), end))


def _pick(free, row, pc, now, spread=True, count=SHOWN):
    """(chunk, options): shorter blocks if nothing of the usual length is free."""
    due, gap = datetime.fromisoformat(row["due"]), pc["gap_minutes"]
    work_free = window_free(free, row, pc, now)
    if row["kind"] == "habit":
        gap = 0  # a habit may run to the end of its window
    chunk = _chunk(row, pc) if row["kind"] != "habit" else _round15(row["minutes"])
    for size in dict.fromkeys([chunk, min(chunk, 60), min(chunk, pc["min_block_minutes"])]):
        found = options(work_free, size, due, gap, count, spread)
        if found:
            return size, found
    return chunk, []


def render(row, chunk, opts, note=""):
    due = datetime.fromisoformat(row["due"])
    what = "prep" if row["kind"] == "exam" else "to do"
    head = (f"{fmt(row['minutes'])} today, between {row['window_start']} and {due:%H:%M}." if row["kind"] == "habit"
            else f"{fmt(row['minutes'])} {what} today (due {due:%a %d %b %H:%M}).")
    lines = ([note, ""] if note else []) + [row["title"] + (" (habit)" if row["kind"] == "habit" else ""), head]
    if not opts:
        lines.append("No free slot is left today before it's due. Tap Not today to move it to tomorrow.")
    elif chunk < row["minutes"]:
        lines.append(f"Pick a time for the first {fmt(chunk)}:")
    else:
        lines.append("When? Free slots today:")
    rows = [[(f"{s:%H:%M}-{e:%H:%M}", f"sgb:{row['id']}:{s:%H%M}:{chunk}") for s, e in opts[i:i + 3]]
            for i in range(0, len(opts), 3)]
    rows.append([("More times", f"sgm:{row['id']}"), ("Not today", f"sgn:{row['id']}")])
    return "\n".join(lines), rows


def _send(tg, row, work_free, pc, now, state, note=""):
    chunk, opts = _pick(work_free, row, pc, now)
    text, buttons = render(row, chunk, opts, note)
    message_id = tg.send(text, buttons)
    state.plan_item_set(row["id"], tg_message_id=message_id, sent_at=now.isoformat())


def _refresh(tg, state, today, work_free, pc, now, skip_id=None):
    """Redraws the other waiting messages: a time you just booked isn't offered on them any more."""
    for row in state.plan_items(today, statuses=["open"]):
        if row["id"] != skip_id and row["tg_message_id"] and row["minutes"] > 0:
            chunk, opts = _pick(work_free, row, pc, now)
            tg.edit(row["tg_message_id"], *render(row, chunk, opts))


def prepare(cfg, state, now, cal, tasks_api, rank=True):
    """Works out today's tasks (rows in plan_items) without sending anything. Returns (items, work_free, cap_left)."""
    work_free, items, cap_left = planner.work_context(cfg, state, now, cal, tasks_api, rank=rank)
    today, wanted = now.date(), set()
    for it in items:
        wanted.add(it["key"])
        row = state.plan_item_upsert(today, it["key"], it["title"], it.get("kind", "task"), it["due"].isoformat(),
                                     it.get("list_id"), it["need"])
        if it.get("window_start") and row.get("window_start") != it["window_start"]:
            state.plan_item_set(row["id"], window_start=it["window_start"])
        if row["status"] == "open" and it["need"] <= 0:
            state.plan_item_set(row["id"], status="booked")
        elif row["status"] == "booked" and it["need"] > 0:
            state.plan_item_set(row["id"], status="open")  # e.g. a slot came back as Not done
    for row in state.plan_items(today, statuses=["open"]):
        if row["work_key"] not in wanted:  # ticked off in Tasks meanwhile, or no longer due
            state.plan_item_set(row["id"], status="booked", minutes=0)
    return items, work_free, cap_left


def morning_lines(state, today, cap_left, pc):
    """The "Your tasks today" section of the morning message."""
    rows = state.plan_items(today, statuses=["open"])
    if not rows:
        return ["- Nothing needs time today."]
    lines = [f"- {r['title']}: {fmt(r['minutes'])}" + {"exam": " (exam prep)", "habit": " (habit)"}.get(r["kind"], "")
             for r in rows]
    total = sum(r["minutes"] for r in rows if r["kind"] != "habit")  # habits don't count toward the work limit
    if cap_left <= 0:
        lines.append(f"Today already has {pc['max_work_hours_per_day']:g} h of work planned (your daily limit), so this "
                     "would be extra. Pick what you really want to do and tap Not today on the rest.")
    elif total > cap_left:
        lines.append(f"That's {fmt(total)}, but only {fmt(cap_left)} fits under your daily limit of "
                     f"{pc['max_work_hours_per_day']:g} h. Give times to what matters most and tap Not today on the rest.")
    lines.append("Pick a time for each in the messages below. Nothing is booked until you tap a time.")
    return lines


def send_waiting(cfg, state, now, tg, work_free, resend=False):
    """One "when?" message per open task that doesn't have one yet (or every open task, with resend)."""
    pc, sent = cfg["planner"], 0
    for row in state.plan_items(now.date(), statuses=["open"]):
        if row["minutes"] <= 0 or (row["tg_message_id"] and not resend):
            continue
        if row["tg_message_id"]:
            tg.edit(row["tg_message_id"], f"{row['title']}: new times below.")
        _send(tg, row, work_free, pc, now, state)
        sent += 1
    return sent


# --- button taps (called by the listener; it has answered the tap already) ------------------------------------

def handle(listener, cq, action, rest, now):
    cfg, state, tg = listener.cfg, listener.state, listener.tg
    pc, today, message_id = cfg["planner"], now.date(), cq["message"]["message_id"]
    if action in ("bkd", "eve", "prep", "hu", "mvb"):
        return {"bkd": _answer_block, "eve": _evening_answer, "prep": _set_prep, "hu": _heads_up_answer,
                "mvb": _move_to}[action](listener, message_id, rest, now)
    raw_id, _, arg = rest.partition(":")
    row = state.plan_item(int(raw_id)) if raw_id.isdigit() else None
    if row is None:
        return
    if row["plan_date"] != today.isoformat():
        tg.edit(message_id, f"{row['title']}: this was for {row['plan_date']}. Tap 'Plan rest of today' for today's times.")
        return
    if action == "sgn":  # Not today
        state.plan_item_set(row["id"], status="skipped")
        moved = _move_task(listener, row, today + timedelta(days=1))
        tg.edit(message_id, f"{row['title']}: not today." + (" Moved to tomorrow in your tasks." if moved else
                                                             " It'll be suggested again tomorrow if it's still open."))
        return
    work_free, _, _ = planner.work_context(cfg, state, now, listener.calendar, listener.tasks)
    if action == "sgm":  # More times
        chunk, opts = _pick(work_free, row, pc, now, spread=False, count=MORE)
        tg.edit(message_id, *render(row, chunk, opts))
        return
    if action != "sgb" or row["status"] != "open":
        if row["status"] == "booked":
            tg.edit(message_id, f"{row['title']}: already has its time today.")
        return
    hhmm, _, minutes = arg.partition(":")
    start = slots.at(today, f"{hhmm[:2]}:{hhmm[2:]}", now.tzinfo)
    end = start + timedelta(minutes=int(minutes))
    if not any(s <= start and end <= e for s, e in work_free):
        chunk, opts = _pick(work_free, row, pc, now)
        tg.edit(message_id, *render(row, chunk, opts, note="That time isn't free any more. Current free slots:"))
        return
    habit = row["kind"] == "habit"
    calendar_id = cfg["calendars"]["habits" if habit else "planner"]
    event_id = google_writer.create_block(listener.calendar, calendar_id, row["title"] if habit else f"Work: {row['title']}",
                                          start, end, cfg["timezone"], "habit" if habit else "work", work_key=row["work_key"],
                                          note=f"Due {datetime.fromisoformat(row['due']):%a %d %b %H:%M}; you picked this time")
    state.block_add(row["id"], event_id, row["work_key"], row["title"], start, end, calendar=calendar_id)
    left = max(0, row["minutes"] - int(minutes))
    state.plan_item_set(row["id"], minutes=left, status="open" if left > 0 else "booked")
    work_free = slots.subtract(work_free, slots.pad([(start, end)], pc["gap_minutes"]))
    booked = f"Booked {start:%H:%M}-{end:%H:%M}: {row['title']}. I'll ask afterwards whether you finished."
    if left > 0:
        row = state.plan_item(row["id"])
        chunk, opts = _pick(work_free, row, pc, now)
        tg.edit(message_id, *render(row, chunk, opts, note=booked))
    else:
        tg.edit(message_id, booked)
    _refresh(tg, state, today, work_free, pc, now, skip_id=row["id"])
    log.info("booked %s-%s for %r", f"{start:%H:%M}", f"{end:%H:%M}", row["title"])


def _answer_block(listener, message_id, rest, now):
    """bkd:<block id>:<d|p|x> - the answer to "Did you finish ...?"."""
    state, tg = listener.state, listener.tg
    raw_id, _, answer = rest.partition(":")
    block = state.block(int(raw_id)) if raw_id.isdigit() else None
    if block is None or block["status"] not in ("booked", "asked"):
        return  # already answered
    status = {"d": "done", "p": "partly", "x": "notdone"}.get(answer)
    if status is None:
        return
    state.block_set(block["id"], status=status)
    if status == "done":
        ticked = _maybe_complete(listener, block)
        note = " Ticked off in your tasks." if ticked else ""
        if (block["work_key"] or "").startswith("habit:"):
            import habits
            habit = state.habit(int(block["work_key"].split(":")[1]))
            run = habits.streak(state, habit, now.date()) if habit else 0
            note = f" Streak: {run} day{'s' if run != 1 else ''}." if run else ""
        tg.edit(message_id, f"Done: {block['title']}.{note}")
        return
    # Partly / Not done: offer new times for what's left (today if there's room, else Not today = tomorrow)
    _reoffer(listener, message_id, block, now, "Partly done" if status == "partly" else "Not done")


def _task_ref(listener, work_key, list_id):
    """(task list, task id) of a to-do or task, or None for deadlines and exam prep."""
    if not work_key.startswith("task:"):
        return None
    task_id = work_key[len("task:"):]
    if not list_id and task_id in listener.state.todo_task_ids():  # a /todo: it lives in DAILY TASKS
        list_id = listener.cfg["morning"].get("todo_tasklist")
    return (list_id, task_id) if list_id else None


def _maybe_complete(listener, block):
    """A to-do whose time is all done is ticked off in Google Tasks (deadlines stay yours to tick off)."""
    state, cfg = listener.state, listener.cfg
    row = state.plan_item(block["item_id"]) if block["item_id"] else None
    ref = _task_ref(listener, block["work_key"], row and row["list_id"])
    if ref is None:
        return False
    effort = state.get_effort(block["work_key"])
    effort_min = (cfg["planner"]["task_effort_hours"] if effort is None else effort) * 60
    done = sum((datetime.fromisoformat(b["end"]) - datetime.fromisoformat(b["start"])).total_seconds() / 60
               * planner.DONE_WEIGHT.get(b["status"], 0) for b in state.blocks(work_key=block["work_key"]))
    if done + 1 < effort_min:
        return False
    try:
        listener.tasks.tasks().patch(tasklist=ref[0], task=ref[1], body={"status": "completed"}).execute()
    except HttpError as e:
        if e.resp.status not in (404, 410):
            raise
        return False
    return True


def _move_task(listener, row, day):
    """A to-do left for another day gets that due date in Google Tasks (so it isn't shown as overdue)."""
    ref = _task_ref(listener, row["work_key"], row["list_id"])
    if ref is None:
        return False
    try:
        listener.tasks.tasks().patch(tasklist=ref[0], task=ref[1], body={"due": f"{day.isoformat()}T00:00:00.000Z"}).execute()
    except HttpError as e:
        if e.resp.status not in (404, 410):
            raise
        return False
    return True


# --- every 15 minutes (morning.py --tick): "Did you finish?", one reminder, the evening check ---------------------

def _asleep(cfg, now):
    return any(s <= now < e for s, e in slots.sleep_intervals(now.date(), cfg["planner"]["sleep"], now.tzinfo))


def tick(cfg, state, now, tg, services):
    """`services()` returns (calendar, tasks); only called when Google is needed (reminders)."""
    if _asleep(cfg, now):
        return  # after sleep, whatever is due arrives at once
    ended = [b for b in state.blocks(statuses=["booked"]) if datetime.fromisoformat(b["end"]) <= now]
    if ended:  # moved or deleted in the Calendar app since it was booked? Ask about the time it really has
        import actions
        cal, _ = services()
        actions.sync_if_stale(cfg, state, cal, now)
    for b in state.blocks(statuses=["booked"]):
        if datetime.fromisoformat(b["end"]) <= now:
            start, end = datetime.fromisoformat(b["start"]).astimezone(now.tzinfo), datetime.fromisoformat(b["end"]).astimezone(now.tzinfo)
            day = "" if start.date() == now.date() else f"{start:%a} "
            message_id = tg.send(f"Did you finish {b['title']} ({day}{start:%H:%M}-{end:%H:%M})?",
                                 [[("Done", f"bkd:{b['id']}:d"), ("Partly", f"bkd:{b['id']}:p"), ("Not done", f"bkd:{b['id']}:x")]])
            state.block_set(b["id"], status="asked", tg_message_id=message_id)
    mc, today = cfg["morning"], now.date()
    remind_after = timedelta(minutes=mc.get("remind_after_minutes", 120))
    work_end = slots.at(today, cfg["planner"]["work_window"][1], now.tzinfo)
    waiting = [r for r in state.plan_items(today, statuses=["open"])
               if r["minutes"] > 0 and not r["reminded"] and r["tg_message_id"] and r["sent_at"]
               and now - datetime.fromisoformat(r["sent_at"]) >= remind_after]
    if waiting and now < work_end:
        cal, tasks_api = services()
        work_free, _, _ = planner.work_context(cfg, state, now, cal, tasks_api)
        for r in waiting:
            state.plan_item_set(r["id"], reminded=1)
            tg.edit(r["tg_message_id"], f"{r['title']}: reminder below.")
            _send(tg, state.plan_item(r["id"]), work_free, cfg["planner"], now, state,
                  note="Still no time picked for this today:")
    evening = slots.at(today, mc.get("evening_check", "21:30"), now.tzinfo)
    if now >= evening and state.get_meta(EVENING_SENT) != today.isoformat():
        rows = state.plan_items(today)
        if rows:
            text, buttons = evening_text(state, rows, today)
            tg.send(text, buttons)
        state.set_meta(EVENING_SENT, today.isoformat())


def evening_text(state, rows, today):
    done, notdone, unanswered, untimed, movable = [], [], [], [], 0
    for r in rows:
        blocks = [b for b in state.blocks(item_id=r["id"]) if b["status"] != "cleared"]
        statuses = {b["status"] for b in blocks}
        if blocks and statuses <= {"done"} and r["minutes"] <= 0:
            done.append(r["title"])
            continue
        if statuses & {"booked", "asked"}:
            unanswered.append(r["title"])
        elif statuses & {"partly", "notdone"}:
            notdone.append(r["title"])
        else:
            untimed.append(r["title"])
        movable += 1 if r["list_id"] else 0
    lines = [f"Evening check - {today:%a %d %b}"]
    for head, names in (("Done", done), ("Not done / partly", notdone), ("Not answered yet (buttons above)", unanswered),
                        ("No time picked", untimed)):
        if names:
            lines.append(f"{head}: " + ", ".join(names))
    buttons = None
    if movable:
        lines.append("\nMove your unfinished to-dos to tomorrow? (Deadline and exam work carries over by itself.)")
        buttons = [[("Move to tomorrow", f"eve:m:{today.isoformat()}"), ("Leave as is", f"eve:k:{today.isoformat()}")]]
    return "\n".join(lines), buttons


def _evening_answer(listener, message_id, rest, now):
    op, _, day = rest.partition(":")
    state, tg = listener.state, listener.tg
    if op != "m":
        tg.edit(message_id, "OK, your to-dos are left as they are.")
        return
    moved = 0
    for r in state.plan_items(date.fromisoformat(day)):
        blocks = state.blocks(item_id=r["id"])
        finished = blocks and all(b["status"] == "done" for b in blocks) and r["minutes"] <= 0
        if not finished and _move_task(listener, r, date.fromisoformat(day) + timedelta(days=1)):
            moved += 1
    tg.edit(message_id, f"Moved {moved} to-do(s) to tomorrow. You'll get times for them after tomorrow's check-in.")


# --- exams: how much preparation (/exams) --------------------------------------------------------------------

def upcoming_exams(cfg, state, now, cal, days=14):
    """[(event, kind, start, prep hours)] for exams on College in the next `days` days."""
    tz, out = now.tzinfo, []
    for ev in google_writer.list_events(cal, cfg["calendars"]["college"], now, now + timedelta(days=days)):
        kind = planner.exam_kind(ev.get("summary", ""))
        if not kind or ev.get("summary", "").startswith(google_writer.DUE_PREFIX):
            continue
        start = planner._local(ev["start"], tz) or datetime.combine(date.fromisoformat(ev["start"]["date"]),
                                                                    slots.hm(cfg["planner"]["work_window"][0]), tz)
        if start > now:
            out.append((ev, kind, start, planner._effort(state, f"event:{ev['id']}", planner.exam_prep_hours(cfg, kind))))
    return out


def exams_message(cfg, state, now, cal):
    exams = upcoming_exams(cfg, state, now, cal)
    if not exams:
        return "No exams on your College calendar in the next 2 weeks.", None
    lines, buttons = ["Exams coming up - preparation time is planned before each. Tap to change it:"], []
    for n, (ev, kind, start, hours) in enumerate(exams, 1):
        lines.append(f"{n}. {start:%a %d %b %H:%M}  {ev['summary']}: " + (f"{hours:g} h prep" if hours else "no prep"))
        buttons.append([(f"{n}: {h} h", f"prep:{ev['id']}:{h}") for h in EXAM_CHOICES[:4]] + [(f"{n}: none", f"prep:{ev['id']}:0")])
    return "\n".join(lines), buttons[:12]


def _set_prep(listener, message_id, rest, now):
    event_id, _, hours = rest.rpartition(":")
    listener.state.set_effort(f"event:{event_id}", float(hours))
    listener.tg.edit(message_id, *exams_message(listener.cfg, listener.state, now, listener.calendar))


# --- heads-up before a booked block (the listener checks every minute) ---------------------------------------

def heads_up(listener, now):
    """ "Next at 15:00: Study SDET MidSem (1 h 30 min)" with Start / Push 30 min / Skip, heads_up_minutes before."""
    import actions
    lead = listener.cfg["morning"].get("heads_up_minutes", 5)
    if not lead or _asleep(listener.cfg, now):
        return
    soon = [b for b in listener.state.blocks(statuses=["booked"]) if not b["headsup"]
            and now < datetime.fromisoformat(b["start"]) <= now + timedelta(minutes=lead + 15)]
    if soon:  # you may have moved it in the Calendar app: use the time it has now
        actions.sync_if_stale(listener.cfg, listener.state, listener.calendar, now)
    for b in listener.state.blocks(statuses=["booked"]):
        start = datetime.fromisoformat(b["start"]).astimezone(now.tzinfo)
        if b["headsup"] or not now < start <= now + timedelta(minutes=lead):
            continue
        end = datetime.fromisoformat(b["end"]).astimezone(now.tzinfo)
        listener.tg.send(f"Next at {start:%H:%M}: {b['title']} ({fmt((end - start).total_seconds() / 60)})",
                         [[("Start", f"hu:{b['id']}:s"), ("Push 30 min", f"hu:{b['id']}:p"), ("Skip", f"hu:{b['id']}:k")]])
        listener.state.block_set(b["id"], headsup=1)


def _heads_up_answer(listener, message_id, rest, now):
    import actions
    raw_id, _, op = rest.partition(":")
    block = listener.state.block(int(raw_id)) if raw_id.isdigit() else None
    if block is None or block["status"] != "booked":
        return
    if op == "s":
        listener.tg.edit(message_id, f"Started: {block['title']}. I'll ask how it went when it ends.")
        return
    if op == "k":
        actions.skip_block(listener.cfg, listener.state, listener.calendar, block)
        _reoffer(listener, message_id, block, now, "Skipped")
        return
    start, end = (datetime.fromisoformat(block[k]).astimezone(now.tzinfo) for k in ("start", "end"))
    ok, text, alternatives = actions.move_block(listener.cfg, listener.state, listener.calendar, block,
                                                start + timedelta(minutes=30), end + timedelta(minutes=30), now)
    listener.tg.edit(message_id, text if ok else f"{text} Move it to:", None if ok else _move_buttons(block, alternatives))


def _move_buttons(block, alternatives):
    rows = [[(f"{s:%H:%M}-{e:%H:%M}", f"mvb:{block['id']}:{s:%Y%m%d%H%M}") for s, e in alternatives]]
    return rows + [[("Skip it", f"hu:{block['id']}:k")]]


def _move_to(listener, message_id, rest, now):
    """mvb:<block>:<YYYYMMDDHHMM> - move a block to one of the offered times."""
    import actions
    raw_id, _, stamp = rest.partition(":")
    block = listener.state.block(int(raw_id)) if raw_id.isdigit() else None
    if block is None or len(stamp) != 12:
        return
    start = datetime.strptime(stamp, "%Y%m%d%H%M").replace(tzinfo=now.tzinfo)
    length = datetime.fromisoformat(block["end"]) - datetime.fromisoformat(block["start"])
    ok, text, alternatives = actions.move_block(listener.cfg, listener.state, listener.calendar, block, start,
                                                start + length, now)
    listener.tg.edit(message_id, text if ok else f"{text} Try one of these:", None if ok else _move_buttons(block, alternatives))


def _reoffer(listener, message_id, block, now, label):
    """After Skip / Not done: new times for what's left today."""
    cfg, state = listener.cfg, listener.state
    free, items, _ = planner.work_context(cfg, state, now, listener.calendar, listener.tasks)
    item = next((it for it in items if it["key"] == block["work_key"]), None)
    if item is None or item["need"] <= 0:
        listener.tg.edit(message_id, f"{label}: {block['title']}. It'll be in tomorrow's suggestions.")
        return
    row = state.plan_item_upsert(now.date(), item["key"], item["title"], item.get("kind", "task"), item["due"].isoformat(),
                                 item.get("list_id"), item["need"])
    state.plan_item_set(row["id"], status="open", **({"window_start": item["window_start"]} if item.get("window_start") else {}))
    row = state.plan_item(row["id"])
    chunk, opts = _pick(free, row, cfg["planner"], now)
    listener.tg.edit(message_id, *render(row, chunk, opts, note=f"{label}: {block['title']}. Pick a new time?"))
    state.plan_item_set(row["id"], tg_message_id=message_id)


# --- /today ---------------------------------------------------------------------------------------------

STATUS_TEXT = {"booked": "", "asked": " - how did it go? (question above)", "done": " - done", "partly": " - partly done",
               "notdone": " - not done", "busy": " - busy"}


def today_message(cfg, state, cal, now, day=None):
    """What's scheduled on `day` (default today): events, your blocks (as they are in Google Calendar now, also if
    you moved them in the Calendar app), and today's tasks still without a time."""
    import actions
    import digest
    today = now.date()
    day = day or today
    actions.sync_if_stale(cfg, state, cal, now)
    head = "Today" if day == today else "Tomorrow" if day == today + timedelta(days=1) else f"{day:%A}"
    lines = [f"{head} - {day:%a %d %b}", "", "Events"] + digest.events_today(cfg, cal, state, now, day=day)
    blocks = sorted((b for b in state.blocks() if datetime.fromisoformat(b["start"]).astimezone(now.tzinfo).date() == day
                     and b["status"] != "cleared"), key=lambda b: b["start"])
    lines += ["", "Your blocks"] + ([f"- {datetime.fromisoformat(b['start']).astimezone(now.tzinfo):%H:%M}-"
                                    f"{datetime.fromisoformat(b['end']).astimezone(now.tzinfo):%H:%M}  {b['title']}"
                                    f"{STATUS_TEXT.get(b['status'], '')}" for b in blocks] or ["- None booked yet"])
    waiting = [r for r in state.plan_items(today, statuses=["open"]) if r["minutes"] > 0] if day == today else []
    if waiting:
        lines += ["", "No time picked yet"] + [f"- {r['title']}: {fmt(r['minutes'])}" for r in waiting]
    buttons = [[("Plan rest of today", "tdp:"), ("Clear today's plan", f"clear:{today.isoformat()}")]] if day == today else None
    return "\n".join(lines), buttons
