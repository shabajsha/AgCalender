"""Parses .ics calendar invites into items (no LLM involved)."""
import logging
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from icalendar import Calendar

from extractor import clean_text

log = logging.getLogger(__name__)


def _to_local(value, tz):
    """icalendar gives date (all-day) or datetime (maybe naive). Datetimes -> aware in tz."""
    if isinstance(value, datetime):
        return value.replace(tzinfo=tz) if value.tzinfo is None else value.astimezone(tz)
    return value  # plain date


def parse_ics(raw, tz_name):
    tz = ZoneInfo(tz_name)
    cal = Calendar.from_ical(raw)
    if str(cal.get("method", "")).upper() == "CANCEL":
        log.info("ics: skipping cancellation invite")
        return []

    items = []
    for ev in cal.walk("VEVENT"):
        title = clean_text(ev.get("summary", "")) or "(untitled invite)"
        if str(ev.get("status", "")).upper() == "CANCELLED":
            log.info("ics: skipping cancelled event %r", title)
            continue
        if not ev.get("dtstart"):
            continue
        start = _to_local(ev.get("dtstart").dt, tz)
        all_day = not isinstance(start, datetime)
        if ev.get("dtend"):
            end = _to_local(ev.get("dtend").dt, tz)
        else:
            end = start + (timedelta(days=1) if all_day else timedelta(hours=1))

        rrule = ev.get("rrule")
        items.append({
            "type": "event",
            "title": title,
            "start": start,
            "end": end,
            "all_day": all_day,
            "due": None,
            "course": None,
            "location": str(ev.get("location", "")) or None,
            "description": str(ev.get("description", ""))[:2000] or None,
            "recurrence": ["RRULE:" + rrule.to_ical().decode()] if rrule else None,
        })
    return items


def is_past(item, now):
    """A one-off event that already ended. Recurring events are kept."""
    if item.get("recurrence"):
        return False
    end = item["end"]
    if isinstance(end, datetime):
        return end < now
    return end <= now.date() if isinstance(end, date) else False
