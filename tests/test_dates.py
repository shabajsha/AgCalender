from datetime import date, time

import pytest

from dates import resolve_date, resolve_time

SAT, WED, FRI, DEC = date(2026, 9, 26), date(2026, 9, 30), date(2026, 10, 2), date(2026, 12, 20)


@pytest.mark.parametrize("text, received, expected", [
    ("next Friday", SAT, date(2026, 10, 2)),
    ("tomorrow", SAT, date(2026, 9, 27)),
    ("this Monday", SAT, date(2026, 9, 28)),
    ("Friday", WED, date(2026, 10, 2)),
    ("next Tuesday", WED, date(2026, 10, 6)),
    ("Thurs", WED, date(2026, 10, 1)),
    ("Oct 12", WED, date(2026, 10, 12)),
    ("12th October", WED, date(2026, 10, 12)),
    ("Monday, 12 October", WED, date(2026, 10, 12)),
    ("15/10", WED, date(2026, 10, 15)),           # day first, as written in India
    ("05/10/2026", WED, date(2026, 10, 5)),
    ("2026-10-12", WED, date(2026, 10, 12)),
    ("5 Jan", DEC, date(2027, 1, 5)),             # January mentioned in December = next year
    ("today", WED, WED),
    ("day after tomorrow", WED, date(2026, 10, 2)),
    ("in 3 days", WED, date(2026, 10, 3)),
    # fixed in Phase 4:
    ("due Friday", FRI, FRI),                     # same weekday = today, not a week later
    ("this Friday", FRI, FRI),
    ("next Friday", FRI, date(2026, 10, 9)),      # "next" on the same weekday = a week later
    ("end of the month", FRI, date(2026, 10, 31)),
    ("end of next month", DEC, date(2027, 1, 31)),
])
def test_resolve_date(text, received, expected):
    assert resolve_date(text, received) == expected


@pytest.mark.parametrize("text", ["next week", "soon", "", None, "bring your monitor", "after the wedding",
                                  "end of semester"])
def test_no_specific_day(text):
    assert resolve_date(text, FRI) is None


@pytest.mark.parametrize("text, expected", [
    ("11:59 PM", time(23, 59)), ("10 AM", time(10, 0)), ("14:00", time(14, 0)), ("5.30 pm", time(17, 30)),
    ("noon", time(12, 0)), ("midnight", time(23, 59)), ("EOD", None), (None, None),
])
def test_resolve_time(text, expected):
    assert resolve_time(text) == expected
