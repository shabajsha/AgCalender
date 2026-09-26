"""Free-time arithmetic for the planner. Pure functions on (start, end) datetime pairs: no I/O, no LLM.

All datetimes are timezone-aware. "free" is always a sorted list of non-overlapping intervals.
"""
from datetime import datetime, time, timedelta


def hm(text):
    """'23:30' -> time(23, 30)"""
    h, m = text.split(":")
    return time(int(h), int(m))


def at(day, hhmm, tz):
    return datetime.combine(day, hm(hhmm), tz)


def round_up(dt, minutes=15):
    """Next multiple of `minutes` past the hour (or dt itself if already on one)."""
    dt = dt.replace(second=0, microsecond=0) + (timedelta(minutes=1) if dt.second or dt.microsecond else timedelta())
    extra = (-dt.minute) % minutes
    return dt + timedelta(minutes=extra)


def merge(intervals):
    out = []
    for s, e in sorted(i for i in intervals if i[1] > i[0]):
        if out and s <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((s, e))
    return out


def subtract(free, busy):
    """free minus busy."""
    result = []
    busy = merge(busy)
    for fs, fe in free:
        cursor = fs
        for bs, be in busy:
            if be <= cursor or bs >= fe:
                continue
            if bs > cursor:
                result.append((cursor, bs))
            cursor = max(cursor, be)
        if cursor < fe:
            result.append((cursor, fe))
    return result


def intersect(free, window):
    ws, we = window
    return [(max(s, ws), min(e, we)) for s, e in free if min(e, we) > max(s, ws)]


def sleep_intervals(day, sleep, tz):
    """Sleep window like ['23:30', '07:00'] as intervals touching `day` (it may cross midnight)."""
    start, end = hm(sleep[0]), hm(sleep[1])
    if start < end:  # e.g. 01:00-08:00, same day
        return [(datetime.combine(day, start, tz), datetime.combine(day, end, tz))]
    prev, nxt = day - timedelta(days=1), day + timedelta(days=1)
    return [(datetime.combine(prev, start, tz), datetime.combine(day, end, tz)),
            (datetime.combine(day, start, tz), datetime.combine(nxt, end, tz))]


def pad(intervals, minutes):
    d = timedelta(minutes=minutes)
    return [(s - d, e + d) for s, e in intervals]


def quarter(free):
    """Moves every free interval's start up to the next quarter hour, so blocks start at :00/:15/:30/:45."""
    return [(round_up(s), e) for s, e in free if e > round_up(s)]


def place_one(free, minutes, gap, window=None):
    """Earliest slot of `minutes` (inside `window` if given). Returns (block, new_free) or (None, free)."""
    need = timedelta(minutes=minutes)
    for s, e in quarter(intersect(free, window) if window else free):
        if e - s >= need:
            block = (s, s + need)
            return block, subtract(free, pad([block], gap))
    return None, free


def fill(free, target_minutes, block_minutes, min_block_minutes, gap):
    """Earliest-first chunks totalling up to target_minutes. Returns (blocks, new_free)."""
    blocks, remaining = [], target_minutes
    while remaining >= min_block_minutes:
        size = min(block_minutes, remaining)
        # use the biggest chunk that fits in the earliest slot that can take at least min_block_minutes
        slot = next(((s, e) for s, e in quarter(free) if (e - s) >= timedelta(minutes=min_block_minutes)), None)
        if slot is None:
            break
        size = min(size, int((slot[1] - slot[0]).total_seconds() // 60))
        size -= size % 15  # keep blocks on quarter hours
        if size < min_block_minutes:
            break
        block = (slot[0], slot[0] + timedelta(minutes=size))
        blocks.append(block)
        free = subtract(free, pad([block], gap))
        remaining -= size
    return blocks, free
