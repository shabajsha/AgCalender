"""Parses .ics calendar invites into items (no LLM involved)."""
import logging
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from icalendar import Calendar

from extractor import clean_text

log = logging.getLogger(__name__)


def _to_local(value, tz):
    """icalendar gives date (all-day) or datetime (maybe naive). Datetimes -> aware in tz."""
    if isinstance(value, datetime):
        return value.replace(tzinfo=tz) if value.tzinfo is None else value.astimezone(tz)
    return value  # plain date


def _exdates(ev, tz):
    """Dates removed from a repeating invite, as Google Calendar EXDATE lines (kept so they stay removed)."""
    lines = []
    raw = ev.get("exdate")
    for group in (raw if isinstance(raw, list) else [raw] if raw else []):
        for d in group.dts:
            value = _to_local(d.dt, tz)
            if isinstance(value, datetime):
                lines.append("EXDATE:" + value.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
            else:
                lines.append("EXDATE;VALUE=DATE:" + value.strftime("%Y%m%d"))
    return lines


DAY_NAMES = {"MO": "Mon", "TU": "Tue", "WE": "Wed", "TH": "Thu", "FR": "Fri", "SA": "Sat", "SU": "Sun"}


def describe_recurrence(recurrence):
    """['RRULE:FREQ=WEEKLY;BYDAY=TU,TH;UNTIL=20261130T000000Z'] -> 'weekly on Tue, Thu until 30 Nov 2026'."""
    rule = next((r[len("RRULE:"):] for r in recurrence if r.startswith("RRULE:")), "")
    parts = dict(p.split("=", 1) for p in rule.split(";") if "=" in p)
    text = {"DAILY": "daily", "WEEKLY": "weekly", "MONTHLY": "monthly", "YEARLY": "yearly"}.get(parts.get("FREQ"), rule)
    if parts.get("INTERVAL", "1") != "1":
        text = f"every {parts['INTERVAL']} {parts.get('FREQ', '').lower().rstrip('ly').replace('dai', 'day')}s"
    if "BYDAY" in parts:
        text += " on " + ", ".join(DAY_NAMES.get(d[-2:], d) for d in parts["BYDAY"].split(","))
    if "UNTIL" in parts:
        try:
            text += " until " + datetime.strptime(parts["UNTIL"][:8], "%Y%m%d").strftime("%d %b %Y")
        except ValueError:
            pass
    elif "COUNT" in parts:
        text += f", {parts['COUNT']} times"
    else:
        text += ", with no end date"
    return text


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
            "recurrence": (["RRULE:" + rrule.to_ical().decode()] + _exdates(ev, tz)) if rrule else None,
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
