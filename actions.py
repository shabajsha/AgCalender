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


def move_block(cfg, state, cal, block, start, end, now):
    """Moves a booked block (by its booked_blocks row). Returns (ok, message, alternatives)."""
    if start < now - timedelta(minutes=5):
        return False, "That time has already passed.", []
    free = free_on(cfg, state, cal, start.date(), now, ignore_ids={block["event_id"]})
    minutes = int((end - start).total_seconds() // 60)
    if not fits(free, start, end):
        return False, f"{start:%a %H:%M}-{end:%H:%M} isn't free.", nearest_free(free, minutes, start)
    google_writer.move_event(cal, _cal_of(cfg, block), block["event_id"], start, end, cfg["timezone"])
    state.block_set(block["id"], start=start.isoformat(), end=end.isoformat(), status="booked", headsup=0)
    return True, f"Moved {block['title']} to {start:%a %H:%M}-{end:%H:%M}.", []


def skip_block(cfg, state, cal, block):
    """You won't do this block: it's removed from the calendar and counts as not done (so it's planned again)."""
    google_writer.delete_event(cal, _cal_of(cfg, block), block["event_id"])
    state.block_set(block["id"], status="notdone")


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
