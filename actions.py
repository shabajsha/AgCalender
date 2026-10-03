"""Changes to your plan that the bot, the plain-language commands and the web page all make the same way.

Every function checks before it changes anything (a new time must be free: not in the sleep window, not over
another event or block, not in the past) and returns (ok, message). Only blocks the agent made are ever moved or
deleted; your classes and other events are never touched.
"""
from datetime import datetime, timedelta

from googleapiclient.errors import HttpError

import google_writer
import planner
import slots


def _cal_of(cfg, block):
    return block.get("calendar") or cfg["calendars"]["planner"]


def free_on(cfg, state, cal, day, now, ignore_ids=frozenset()):
    """Free time on `day` (from now if it's today): busy time with gaps, sleep and other blocks taken out."""
    start_of_day = slots.at(day, "00:00", now.tzinfo)
    return planner.free_today(cal, cfg, state, max(now, start_of_day), ignore_ids)


def fits(free, start, end):
    return any(s <= start and end <= e for s, e in free)


def nearest_free(free, minutes, around, count=3):
    """Up to `count` free starts closest to `around` for a block of `minutes`."""
    need, starts = timedelta(minutes=minutes), []
    for s, e in slots.quarter(free):
        t = s
        while t + need <= e:
            starts.append(t)
            t += timedelta(minutes=15)
    starts.sort(key=lambda t: abs(t - around))
    picked = []
    for t in starts:
        if all(abs(t - p) >= timedelta(minutes=45) for p in picked):
            picked.append(t)
        if len(picked) == count:
            break
    return sorted((t, t + need) for t in picked)


OPEN = ("booked", "asked")  # blocks that haven't been answered or removed


def move_block(cfg, state, cal, block, start, end, now):
    """Moves a booked block (by its booked_blocks row). Returns (ok, message, alternatives)."""
    block = state.block(block["id"]) or block
    if block["status"] not in OPEN:
        return False, f"{block['title']} was already {_answered(block)}, so it wasn't moved.", []
    if start < now - timedelta(minutes=5):
        return False, "That time has already passed.", []
    free = free_on(cfg, state, cal, start.date(), now, ignore_ids={block["event_id"]})
    minutes = int((end - start).total_seconds() // 60)
    if not fits(free, start, end):
        return False, f"{start:%a %H:%M}-{end:%H:%M} isn't free.", nearest_free(free, minutes, start)
    google_writer.move_event(cal, _cal_of(cfg, block), block["event_id"], start, end, cfg["timezone"])
    state.block_set(block["id"], start=start.isoformat(), end=end.isoformat(), status="booked", headsup=0)
    return True, f"Moved {block['title']} to {start:%a %H:%M}-{end:%H:%M}.", []


def _answered(block):
    return {"done": "marked done", "partly": "marked partly done", "notdone": "marked not done",
            "cleared": "removed", "busy": "busy time"}.get(block["status"], block["status"])


def skip_block(cfg, state, cal, block):
    """You won't do this block: it's removed from the calendar and counts as not done (so it's planned again).
    Returns False (and changes nothing) if it was already answered or removed - an old button mustn't delete the
    record of work you did."""
    block = state.block(block["id"]) or block
    if block["status"] not in OPEN:
        return False
    google_writer.delete_event(cal, _cal_of(cfg, block), block["event_id"])
    state.block_set(block["id"], status="notdone")
    return True


def book(cfg, state, cal, item, start, end, now):
    """Books a time for a plan item (a row of plan_items). Returns (ok, message, alternatives)."""
    free = free_on(cfg, state, cal, start.date(), now)
    if not fits(free, start, end):
        return False, f"{start:%a %H:%M}-{end:%H:%M} isn't free.", nearest_free(free, int((end - start).total_seconds() // 60), start)
    habit = item["kind"] == "habit"
    calendar_id = cfg["calendars"]["habits" if habit else "planner"]
    event_id = google_writer.create_block(cal, calendar_id, item["title"] if habit else f"Work: {item['title']}", start, end,
                                          cfg["timezone"], "habit" if habit else "work", work_key=item["work_key"],
                                          note="you picked this time")
    state.block_add(item["id"], event_id, item["work_key"], item["title"], start, end, calendar=calendar_id)
    if start.date() == now.date():
        left = max(0, item["minutes"] - int((end - start).total_seconds() // 60))
        state.plan_item_set(item["id"], minutes=left, status="open" if left else "booked")
    return True, f"Booked {start:%a %H:%M}-{end:%H:%M}: {item['title']}.", []


def busy(cfg, state, cal, start, end, now, label="Busy"):
    """Blocks out time you're not free (no suggestions go there). Returns (message, blocks that were in the way)."""
    event_id = google_writer.create_block(cal, cfg["calendars"]["planner"], label, start, end, cfg["timezone"], "busy",
                                          note="time you said you're not free")
    state.block_add(None, event_id, None, label, start, end, calendar=cfg["calendars"]["planner"])
    state.block_set(state.block_by_event(event_id)["id"], status="busy")
    clashes = [b for b in state.blocks(statuses=["booked"]) if b["event_id"] != event_id
               and datetime.fromisoformat(b["start"]) < end and datetime.fromisoformat(b["end"]) > start]
    return f"Blocked out {start:%a %H:%M}-{end:%H:%M} as {label}.", clashes


def future_blocks_for(cfg, cal, key, now, days=30):
    return [b for b in google_writer.list_blocks(cal, cfg["calendars"]["planner"], now, now + timedelta(days=days))
            if b.get("extendedProperties", {}).get("private", {}).get("work_key") == key
            and planner._local(b["start"], now.tzinfo) and planner._local(b["start"], now.tzinfo) >= now]


def finish_deadline(cfg, state, cal, tasks_api, event_id, now):
    """A deadline you're done with: its task is ticked off and its upcoming work blocks are removed."""
    task_id = state.task_for_event(event_id)
    if task_id:
        try:
            tasks_api.tasks().patch(tasklist=cfg.get("tasklist", "@default"), task=task_id,
                                    body={"status": "completed"}).execute()
        except HttpError as e:
            if e.resp.status not in (404, 410):
                raise
    removed = _remove_future_work(cfg, state, cal, f"event:{event_id}", now)
    return removed


def drop_deadline(cfg, state, cal, tasks_api, event_id, now):
    """Not doing it: the DUE event, its task and its upcoming work blocks are deleted."""
    task_id = state.task_for_event(event_id)
    _remove_future_work(cfg, state, cal, f"event:{event_id}", now)
    google_writer.delete_event(cal, cfg["calendars"]["college"], event_id)
    if task_id:
        try:
            tasks_api.tasks().delete(tasklist=cfg.get("tasklist", "@default"), task=task_id).execute()
        except HttpError as e:
            if e.resp.status not in (404, 410):
                raise
    state.delete_item_by_event(event_id)


def drop_question(kind, title):
    """The confirmation asked before `discard` (it deletes things in Google, so it's never done on one tap)."""
    return {"task": f"Drop {title} for good? The to-do is deleted from Google Tasks and its planned times are removed.",
            "deadline": f"Drop {title} for good? This deletes the deadline, its task and its planned work.",
            "exam": f"No preparation at all for {title}? Its prep blocks are removed; the exam stays on your calendar.",
            }.get(kind, f"Drop {title} for good? It won't be suggested again and its planned times are removed.")


def discard(cfg, state, cal, tasks_api, key, now, list_id=None):
    """You're not doing it at all (after confirming drop_question). A to-do is deleted from Google Tasks, a deadline
    loses its DUE event and task, exam prep is set to none; its upcoming work blocks are deleted and it leaves today's
    list. Blocks you already worked on stay as history. Returns a short note on what happened."""
    what, _, ident = (key or "").partition(":")
    if what == "task":
        _remove_future_work(cfg, state, cal, key, now)
        if not list_id and ident in state.todo_task_ids():
            list_id = cfg["morning"].get("todo_tasklist")
        note = "Deleted from your tasks." if list_id else "Its planned times are removed."
        if list_id:
            try:
                tasks_api.tasks().delete(tasklist=list_id, task=ident).execute()
            except HttpError as e:
                if e.resp.status not in (404, 410):
                    raise
    elif what == "event":
        ev = _get(cal, cfg["calendars"]["college"], ident)
        if ev is not None and not ev.get("summary", "").startswith(google_writer.DUE_PREFIX):
            state.set_effort(key, 0)  # an exam: no prep (the exam itself is yours, never deleted)
            _remove_future_work(cfg, state, cal, key, now)
            note = "No prep will be planned for it."
        else:
            drop_deadline(cfg, state, cal, tasks_api, ident, now)
            note = "The deadline, its task and its planned work are deleted."
    else:
        return None  # habits are paused or deleted in /habits
    for row in state.plan_items(now.date()):
        if row["work_key"] == key:
            state.plan_item_set(row["id"], status="dropped", minutes=0)
    return note


def _remove_future_work(cfg, state, cal, key, now):
    removed = 0
    for b in future_blocks_for(cfg, cal, key, now):
        google_writer.delete_event(cal, cfg["calendars"]["planner"], b["id"])
        row = state.block_by_event(b["id"])
        if row:
            state.block_set(row["id"], status="cleared")
        removed += 1
    for item in state.plan_items(now.date(), statuses=["open"]):
        if item["work_key"] == key:
            state.plan_item_set(item["id"], status="booked", minutes=0)
    return removed


def _get(cal, calendar_id, event_id):
    try:
        return cal.events().get(calendarId=calendar_id, eventId=event_id).execute()
    except HttpError as e:
        if e.resp.status in (404, 410):
            return None
        raise


def sync_blocks(cfg, state, cal, now, days=3):
    """Brings the bot's record of its blocks in line with Google Calendar, so a block you moved or deleted in the
    Calendar app (or on calendar.google.com) is followed: heads-up, "Did you finish?", /today, typed changes and the
    web page all use the time it has now. Agent blocks the bot didn't track yet are picked up. Returns change notes."""
    tz = now.tzinfo
    window_start = slots.at(now.date(), "00:00", tz)
    window_end = window_start + timedelta(days=days)
    calendars = [cfg["calendars"]["planner"], cfg["calendars"]["habits"]]
    live = {}
    for cid in calendars:
        for ev in google_writer.list_blocks(cal, cid, window_start, window_end):
            live[ev["id"]] = (cid, ev)
    notes = []
    for b in state.blocks(statuses=["booked", "asked", "busy", "notdone", "partly"]):
        found = live.pop(b["event_id"], None)
        ev = found[1] if found else None
        answered = b["status"] in ("notdone", "partly")  # you said it didn't happen (fully)...
        if ev is None:
            if datetime.fromisoformat(b["start"]) < window_start - timedelta(days=1):
                continue  # long past: history, not worth a Google call
            ev = _get(cal, b.get("calendar") or calendars[0], b["event_id"])  # moved out of the window, or deleted
        if ev is None or ev.get("status") == "cancelled":
            if not answered:
                state.block_set(b["id"], status="cleared")
                notes.append(f"{b['title']}: deleted in Google Calendar")
            continue
        start, end = planner._local(ev["start"], tz), planner._local(ev["end"], tz)
        if start is None:
            continue
        if answered:
            if start > now and start != datetime.fromisoformat(b["start"]):  # ...then moved it to a later time
                state.block_set(b["id"], start=start.isoformat(), end=end.isoformat(), status="booked", headsup=0)
                notes.append(f"{b['title']}: rescheduled by you to {start:%a %H:%M}-{end:%H:%M}")
            continue
        fields = {}
        title = (ev.get("summary") or b["title"]).removeprefix("Work: ")
        if title != b["title"]:
            fields["title"] = title
        if start != datetime.fromisoformat(b["start"]) or end != datetime.fromisoformat(b["end"]):
            fields.update(start=start.isoformat(), end=end.isoformat())
            if start > now:
                fields["headsup"] = 0
            if b["status"] == "asked" and end > now:
                fields["status"] = "booked"  # moved to later: ask again when it really ends
            notes.append(f"{title}: moved to {start:%a %H:%M}-{end:%H:%M}")
        if fields:
            state.block_set(b["id"], **fields)
    for event_id, (cid, ev) in live.items():  # agent blocks the bot didn't know (e.g. from an older version)
        if state.block_by_event(event_id):
            continue
        created = ev.get("created")
        if created and now - datetime.fromisoformat(created.replace("Z", "+00:00")) < timedelta(minutes=2):
            continue  # just made (by a tap or the web page), and being recorded by that process right now
        start, end = planner._local(ev["start"], tz), planner._local(ev["end"], tz)
        if start is None or end < now - timedelta(hours=12):
            continue
        private = ev.get("extendedProperties", {}).get("private", {})
        block_id = state.block_add(None, event_id, private.get("work_key"), (ev.get("summary") or "").removeprefix("Work: "),
                                   start, end, calendar=cid)
        if private.get("kind") == "busy":
            state.block_set(block_id, status="busy")
    state.set_meta("blocks_synced_at", now.isoformat())
    return notes


def sync_if_stale(cfg, state, cal, now, max_age=timedelta(minutes=1)):
    """sync_blocks unless it ran very recently (several callers may ask within one tap)."""
    last = state.get_meta("blocks_synced_at")
    if last and now - datetime.fromisoformat(last) < max_age:
        return []
    return sync_blocks(cfg, state, cal, now)
