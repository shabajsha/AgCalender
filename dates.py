"""Turns date/time words copied from an email ("next Friday", "12th Oct", "11:59 PM") into real values.

The LLM only copies the words; all date arithmetic happens here, deterministically.
"""
import calendar
import re
from datetime import date, datetime, time, timedelta

from dateutil import parser as dparser

WEEKDAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
# Whole weekday words only: an older "mon[a-z]*" pattern turned "month" and "monitor" into Monday.
WEEKDAY_RE = re.compile(r"\b(?:(this|next|coming)\s+)?(monday|mon|tuesday|tues|tue|wednesday|wed|thursday|thurs|thur|thu"
                        r"|friday|fri|saturday|sat|sunday|sun)\b")
END_OF_MONTH_RE = re.compile(r"\bend of (?:the |this )?(next )?month\b")
ISO_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
YEAR_RE = re.compile(r"\b\d{4}\b")


def resolve_date(text, received):
    """`received` is a date. Returns a date or None if the words don't name a specific day.

    Weekdays resolve to the nearest upcoming one: "Friday" / "this Friday" written on a Friday means
    today; "next Friday" written on a Friday means a week later. Mid-week, "next Friday" is ambiguous
    and the earlier date is chosen, because an early reminder is safer than a missed deadline.
    "End of the month" is the month's last day.
    """
    if not isinstance(text, str) or not text.strip():
        return None
    t = text.lower().strip()

    if m := ISO_RE.search(t):
        try:
            return date(int(m[1]), int(m[2]), int(m[3]))
        except ValueError:
            return None
    if "day after tomorrow" in t:
        return received + timedelta(days=2)
    if "tomorrow" in t:
        return received + timedelta(days=1)
    if re.search(r"\b(today|tonight|this (morning|afternoon|evening))\b", t):
        return received
    if m := re.search(r"\bin (\d+) days?\b", t):
        return received + timedelta(days=int(m[1]))

    if m := END_OF_MONTH_RE.search(t):
        year, month = received.year, received.month
        if m[1]:  # "end of next month"
            year, month = (year + 1, 1) if month == 12 else (year, month + 1)
        return date(year, month, calendar.monthrange(year, month)[1])

    has_digit = re.search(r"\d", t)
    if (m := WEEKDAY_RE.search(t)) and not has_digit:
        ahead = (WEEKDAYS[m[2][:3]] - received.weekday()) % 7
        if ahead == 0 and m[1] == "next":
            ahead = 7
        return received + timedelta(days=ahead)

    if not has_digit:
        return None  # "next week", "soon", "end of semester" -> not a specific day
    try:
        default = datetime.combine(received, time())
        parsed = dparser.parse(re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", t), default=default, dayfirst=True, fuzzy=True)
    except (ValueError, OverflowError):
        return None
    result = parsed.date()
    # "5 Jan" written in December means next year.
    if not YEAR_RE.search(t) and result < received - timedelta(days=30):
        result = result.replace(year=result.year + 1)
    return result


def resolve_time(text):
    """Returns a time or None ("11:59 PM", "10 AM", "14:00", "noon", "midnight")."""
    if not isinstance(text, str) or not text.strip():
        return None
    t = text.lower().strip()
    if "noon" in t:
        return time(12, 0)
    if "midnight" in t:
        return time(23, 59)  # "midnight" deadlines mean end of that day
    if not re.search(r"\d", t):
        return None
    t = re.sub(r"\b(\d{1,2})\.(\d{2})\b", r"\1:\2", t)  # "5.30 pm" -> "5:30 pm"
    try:
        return dparser.parse(t, fuzzy=True, default=datetime(2000, 1, 1)).time()
    except (ValueError, OverflowError):
        return None
