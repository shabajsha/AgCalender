from datetime import date, timedelta

from conftest import TZ, at, minutes
from slots import fill, merge, pad, place_one, round_up, sleep_intervals, subtract


def t(h, m=0):
    return at(2026, 9, 28, h, m)


def test_round_up():
    assert round_up(t(14, 7)) == t(14, 15)
    assert round_up(t(14, 15)) == t(14, 15)


def test_merge_and_subtract():
    assert merge([(t(11), t(12)), (t(11, 30), t(13))]) == [(t(11), t(13))]
    assert subtract([(t(8), t(23))], [(t(9), t(10)), (t(12), t(13))]) == [(t(8), t(9)), (t(10), t(12)), (t(13), t(23))]


def test_sleep_crossing_midnight():
    assert sleep_intervals(date(2026, 9, 28), ["23:30", "07:00"], TZ) == [
        (at(2026, 9, 27, 23, 30), t(7)), (t(23, 30), at(2026, 9, 29, 7))]


def test_gaps_around_events_and_habit_window():
    free = subtract([(t(8), t(23))], pad([(t(8, 30), t(9, 55))], 15))
    gym, _ = place_one(free, 60, 15, window=(t(6, 30), t(9)))
    assert gym is None                                   # only 08:00-08:15 free in the window
    reading, _ = place_one(free, 30, 15, window=(t(20), t(22)))
    assert reading == (t(20), t(20, 30))


def test_fill_splits_and_keeps_quarter_hours():
    blocks, free = fill([(t(10, 10), t(10, 45)), (t(13, 15), t(23))], 240, 90, 30, 15)
    assert blocks[0] == (t(10, 15), t(10, 45))           # start snapped to a quarter hour
    assert sum(minutes(e - s) for s, e in blocks) == 240
    assert all(minutes(e - s) <= 90 for s, e in blocks)
    assert all(blocks[i][1] + timedelta(minutes=15) <= blocks[i + 1][0] for i in range(len(blocks) - 1))


def test_fill_ignores_gaps_too_small():
    assert fill([(t(8), t(8, 20))], 90, 90, 30, 15)[0] == []
