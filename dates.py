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
MONTH = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*"
# The dateutil fallback only runs on something that really is a date: a day with a month name, or 15/10(/2026).
# ("Week 7", "Quiz 1", "in 2 weeks" used to become the 7th, the 1st and the 2nd of the month.)
REAL_DATE_RE = re.compile(rf"\b\d{{1,2}}(?:st|nd|rd|th)?\s*(?:of\s+)?{MONTH}\b(?:,?\s*\d{{4}}\b)?"
                          rf"|\b{MONTH}\s*\d{{1,2}}(?:st|nd|rd|th)?\b(?:,?\s*\d{{4}}\b)?"
                          r"|\b\d{1,2}[/.-]\d{1,2}(?:[/.-]\d{2,4})?\b")

# Clock times: "10 AM", "9:30 am", "14:00", "1700 hrs"; "noon"/"midnight" are rewritten into these first.
TIME_TOKEN_RE = re.compile(r"(?<![\d:])(\d{1,2})(?::(\d{2}))?\s*([ap])m\b"
                           r"|(?<![\d:])(\d{1,2}):(\d{2})(?!\d)"
                           r"|(?<![\d:])(\d{3,4})\s*(?:hrs|hours|hr|h)\b")
# "2-4 PM" / "11 to 1 pm": the first time borrows am/pm from the second. "1100-1200 hrs" likewise.
RANGE_BARE_RE = re.compile(r"(?<![\d:])(\d{1,2})(?::(\d{2}))?\s*(?:-|–|to)\s*(?=\d{1,2}(?::\d{2})?\s*[ap]m\b)"
                           r"|(?<![\d:])(\d{3,4})\s*(?:-|–|to)\s*(?=\d{3,4}\s*(?:hrs|hours|hr|h)\b)")


def _normalise_time_words(t, time_field=True):
    t = t.lower().replace("a.m.", "am").replace("p.m.", "pm")
    t = re.sub(r"\bnoon\b", "12:00 pm", t)
    t = re.sub(r"\bmidnight\b", "11:59 pm", t)  # "midnight" deadlines mean end of that day
    if time_field:  # "5.30 pm" / "17.30" -> "5:30 pm" / "17:30"
        return re.sub(r"\b(\d{1,2})\.(\d{2})\b", r"\1:\2", t)
    return re.sub(r"\b(\d{1,2})\.(\d{2})(?=\s*[ap]m\b)", r"\1:\2", t)  # in a date, 15.10 is a date


def _clock(h, m, ap=None):
    h, m = int(h), int(m or 0)
    if ap:
        if not 1 <= h <= 12:
            return None
        h = (0 if h == 12 else h) + (12 if ap == "p" else 0)
    return time(h, m) if 0 <= h < 24 and 0 <= m < 60 else None


def _hhmm(text):
    text = text.zfill(4)
    return _clock(text[:2], text[2:])


def _tokens(t):
    """[(start index, time, had am/pm)] for every clock time in normalised text."""
    out = []
    for m in TIME_TOKEN_RE.finditer(t):
        if m.group(3):
            value, ap = _clock(m.group(1), m.group(2), m.group(3)), m.group(3)
        elif m.group(4):
            value, ap = _clock(m.group(4), m.group(5)), None
        else:
            value, ap = _hhmm(m.group(6)), "hrs"
        if value is not None:
            out.append((m.start(), m.end(), value, ap))
    return out


def resolve_time_range(text):
    """(start, end) from time words; end is None if only one time is given. (None, None) when there is no
    real clock time - "at 5", "EOD" or a date copied into the time field must not become midnight."""
    if not isinstance(text, str) or not text.strip():
        return None, None
    t = _normalise_time_words(text)
    tokens = _tokens(t)
    if not tokens:
        return None, None
    bare = RANGE_BARE_RE.search(t)
    if bare and bare.start() < tokens[0][0]:
        end_value, ap = tokens[0][2], tokens[0][3]
        if bare.group(3):
            start_value = _hhmm(bare.group(3))
        else:
            start_value = _clock(bare.group(1), bare.group(2), ap[0] if ap in ("a", "p") else None)
            if start_value and end_value and start_value > end_value and ap == "p":  # "11-1 pm" = 11 am to 1 pm
                start_value = _clock(bare.group(1), bare.group(2), "a")
        if start_value is not None:
            return start_value, end_value
    start = tokens[0][2]
    end = tokens[1][2] if len(tokens) > 1 else None
    return start, end


def resolve_time(text):
    """Returns a time or None ("11:59 PM", "10 AM", "14:00", "1700 hrs", "noon", "midnight")."""
    return resolve_time_range(text)[0]


def _strip_times(t):
    t = _normalise_time_words(t, time_field=False)
    t = RANGE_BARE_RE.sub(" ", t)
    return TIME_TOKEN_RE.sub(" ", t)


def resolve_date(text, received):
    """`received` is a date. Returns a date or None if the words don't name a specific day.

    Weekdays resolve to the nearest upcoming one: "Friday" / "this Friday" written on a Friday means
    today; "next Friday" written on a Friday means a week later. Mid-week, "next Friday" is ambiguous
    and the earlier date is chosen, because an early reminder is safer than a missed deadline.
    "End of the month" is the month's last day. Clock times in the words ("next Friday 5pm") are ignored.
    """
    if not isinstance(text, str) or not text.strip():
        return None
    t = _strip_times(text.lower().strip())

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
    if m := re.search(r"\bin (\d+|a|one) weeks?\b", t):
        return received + timedelta(weeks=1 if m[1] in ("a", "one") else int(m[1]))

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

    if not (m := REAL_DATE_RE.search(t)):
        return None  # "next week", "soon", "Week 7", "in 2 weeks' time" -> not a specific day
    found = m.group(0)  # parse only the date itself: other numbers ("Quiz 1") must not become the year
    try:
        default = datetime.combine(received, time())
        parsed = dparser.parse(re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", found), default=default, dayfirst=True)
    except (ValueError, OverflowError):
        return None
    result = parsed.date()
    # "5 Jan" written in December means next year; "10th August" written in September is simply in the past.
    if not YEAR_RE.search(found) and result < received - timedelta(days=183):
        try:
            result = result.replace(year=result.year + 1)
        except ValueError:  # 29 Feb
            return None
    return result
