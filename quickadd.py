"""Add a calendar event or a deadline by typing it to the bot:

    "meeting with Harsha tomorrow at 3pm"      -> event on College, 15:00-16:00
    "SMAI quiz on 5 Oct 10am for 2h"           -> event, and exam prep is planned before it (/exams to change)
    "add event dentist on Friday 11am"         -> event
    "DSA assignment due Friday 11:59pm"        -> deadline: DUE event + task (with effort buttons)
    "/event Megathon demo 12 Oct 2-4pm"        -> event
The words become an item exactly like one read from an email (dates.py does all the date/time work); you confirm it
first (nlcommands), then it's created like an approved email card, with Undo.
"""
import re
from datetime import datetime, time, timedelta

import google_writer
import planner
from dates import resolve_date, resolve_time_range
from extractor import clean_text
from state import dedupe_key

EVENT_WORDS = re.compile(r"\b(meeting|meet|call|exams?|quiz(?:zes)?|mid[\s-]?sems?|end[\s-]?sems?|viva|test|class|lecture|lab"
                         r"|talk|seminar|workshop|appointment|interview|presentation|demo|event|party|dinner|lunch|trip"
                         r"|match|contest|hackathon|session|tutorial|office hours)\b", re.I)
MEETING_WORDS = re.compile(r"\b(meeting|meet|call|viva|interview|office hours|discussion)\b", re.I)
WHEN_START = re.compile(r"\s+(?=(?:on|at|from|by|today|tomorrow|tonight|this|next|in)\b|\d{1,2}(?:st|nd|rd|th)?\b"
                        r"|(?:mon|tue|wed|thu|fri|sat|sun)[a-z]*\b|(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\b)", re.I)
DUR_RE = re.compile(r"\bfor\s+(\d+(?:\.\d+)?)\s*(hours?|hrs?|h|minutes?|mins?|m)\b", re.I)
DEFAULT_MINUTES = 60


def is_event(title):
    return bool(EVENT_WORDS.search(title or ""))


def split(text):
    """ "Megathon demo 12 Oct 2-4pm" -> ("Megathon demo", "12 Oct 2-4pm"): the title is everything before the first
    date/time word."""
    m = WHEN_START.search(" " + text.strip())
    if not m or m.start() == 0:
        return text.strip(), ""
    return text.strip()[:m.start() - 1].strip(), text.strip()[m.start() - 1:].strip()


def _bare_hour(when):
    """ "at 3" -> 15:00, "at 10" -> 10:00: an event time without am/pm is read as a daytime hour."""
    m = re.search(r"\bat\s+(\d{1,2})(?::(\d{2}))?\b(?!\s*[ap]m)", when or "", re.I)
    if not m or not 1 <= int(m[1]) <= 12:
        return None
    h = int(m[1])
    return time(h + 12 if h <= 7 else h, int(m[2] or 0))


def build(title, when, kind, now, tz_name):
    """(item, None) or (None, reason). kind: 'deadline' or 'event' (a meeting is detected from the title)."""
    title = re.sub(r"^(?:add|schedule|create|put)\s+(?:an?\s+)?(?:event|appointment|deadline)?\s*:?\s*", "", title or "", flags=re.I)
    title = re.sub(r"^(?:i|we)\s+(?:have|got)\s+(?:an?\s+|my\s+)?|^there(?:'s| is)\s+(?:an?\s+)?", "", title, flags=re.I)
    title = clean_text(title)
    title = title[:1].upper() + title[1:]
    if not title:
        return None, "What's it called? e.g. 'meeting with Harsha tomorrow at 3pm'."
    tz = now.tzinfo
    day = resolve_date(when or "", now.date()) if when else None
    start_t, end_t = resolve_time_range(when or "")
    guessed = start_t is None
    start_t = start_t or _bare_hour(when)
    if day is None and start_t is None:
        return None, f"When is {title}? e.g. '{title} on Friday at 3pm'."
    day = day or now.date()
    base = {"title": title[:100], "course": None, "location": None, "description": "Added from Telegram",
            "recurrence": None, "due": None}
    if kind == "deadline":
        due = datetime.combine(day, end_t or start_t or time(23, 59), tz)
        if due < now:
            return None, f"{due:%a %d %b %H:%M} has already passed."
        return {**base, "type": "deadline", "due": due, "start": due - timedelta(minutes=30), "end": due, "all_day": False}, None
    kind = "meeting" if MEETING_WORDS.search(title) else "event"
    if start_t is None:
        if day < now.date():
            return None, f"{day:%a %d %b} has already passed."
        return {**base, "type": kind, "start": day, "end": day + timedelta(days=1), "all_day": True}, None
    start = datetime.combine(day, start_t, tz)
    if guessed and start < now and start_t.hour < 12 and start + timedelta(hours=12) > now:
        start, start_t = start + timedelta(hours=12), time(start_t.hour + 12, start_t.minute)  # "at 8" at noon = 8 pm
    if end_t and end_t > start_t:
        end = datetime.combine(day, end_t, tz)
    else:
        m = DUR_RE.search(when or "")
        minutes = (float(m[1]) * 60 if m[2].lower().startswith("h") else float(m[1])) if m else DEFAULT_MINUTES
        end = start + timedelta(minutes=minutes)
    if start < now:
        return None, f"{start:%a %d %b %H:%M} has already passed."
    return {**base, "type": kind, "start": start, "end": end, "all_day": False}, None


def describe(item, cfg):
    if item["type"] == "deadline":
        return f"Add deadline: {item['title']}, due {item['due']:%a %d %b %H:%M} (calendar event + task)?"
    when = f"{item['start']:%a %d %b} (all day)" if item["all_day"] else f"{item['start']:%a %d %b, %H:%M}-{item['end']:%H:%M}"
    text = f"Add to your calendar: {item['title']}, {when}?"
    exam = planner.exam_kind(item["title"])
    if exam:
        text += f" It's an exam, so {planner.exam_prep_hours(cfg, exam):g} h of prep will be planned before it."
    return text


def create(cfg, state, cal, tasks_api, item):
    """Creates it like an approved email card. Returns (event_id, message, buttons) or (None, reason, None)."""
    key = dedupe_key(item)
    if state.item_exists(key):
        return None, f"{item['title']} is already on your calendar.", None
    event_id, task_id = google_writer.create_item(cal, tasks_api, cfg, item, {"id": None, "subject": "added from Telegram"})
    when = item["due"] or item["start"]
    state.record_item(key, item["type"], event_id, task_id, item["title"], str(when), "telegram")
    undo = [("Undo", f"evu:{event_id}")]
    if item["type"] == "deadline":
        choices = [(f"{h:g} h", f"dle:{event_id}:{h}") for h in cfg["planner"].get("effort_choices_hours", [2, 4, 8, 12])]
        return event_id, (f"Added: {item['title']}, due {item['due']:%a %d %b %H:%M} (calendar + task). "
                          "How much work does it need?"), [choices[:3], choices[3:6], undo]
    if planner.exam_kind(item["title"]):
        choices = [(f"{h} h prep", f"prep:{event_id}:{h}") for h in (2, 4, 8, 12)]
        return event_id, f"Added: {item['title']}. Prep is planned before it; how much?", [choices, undo]
    return event_id, f"Added to your calendar: {item['title']}.", [undo]


def undo(cfg, state, cal, tasks_api, event_id):
    task_id = state.task_for_event(event_id)
    google_writer.delete_event(cal, cfg["calendars"]["college"], event_id)
    if task_id:
        try:
            tasks_api.tasks().delete(tasklist=cfg.get("tasklist", "@default"), task=task_id).execute()
        except Exception as e:  # noqa: BLE001 - already gone is fine
            if getattr(getattr(e, "resp", None), "status", None) not in (404, 410):
                raise
    state.delete_item_by_event(event_id)
