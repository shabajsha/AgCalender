"""Your day doesn't always follow the usual times: woke up late, going to bed late, or getting up later tomorrow.

    "just woke up" / "woke up at 10"   -> today starts now: the morning runs (or you get free slots from now)
    "sleeping at 2am" / "bed at 11"     -> tonight's bedtime: free slots, heads-ups and questions follow it
    "up at 9 tomorrow"                  -> tomorrow's check-in waits until 9, and nothing pings you before
Also as buttons on the evening check. Each override is for that one day (state.meta `day:<date>` = {wake, sleep});
the usual times (/settings -> Sleep, Morning starts at, Work hours) come back the next day.

config.load_config() calls apply(): it lays today's overrides over the planner settings, so every script (and the
listener, which reloads its config every few minutes) uses them without knowing about this module.
"""
import json
from datetime import datetime, time, timedelta

GAP = timedelta(minutes=30)  # between waking up / going to bed and the first / last suggested work


def _t(hhmm):
    h, m = hhmm.split(":")
    return time(int(h), int(m))


def _night_start(day, hhmm):
    """A bedtime belongs to the evening of `day`; one after midnight (e.g. 02:00) is early on the next date."""
    t = _t(hhmm)
    return datetime.combine(day + timedelta(days=1) if t < time(12) else day, t)


def get(meta, day):
    raw = meta(f"day:{day.isoformat()}")
    return json.loads(raw) if raw else {}


def set_time(state, day, field, hhmm):
    """field: 'wake' or 'sleep'; hhmm None clears it (back to the usual time)."""
    data = get(state.get_meta, day)
    if hhmm:
        data[field] = hhmm
    else:
        data.pop(field, None)
    state.set_meta(f"day:{day.isoformat()}", json.dumps(data) if data else "")


def nights(sleep, meta, day):
    """The two sleeps touching `day` as naive (start, end) datetimes: last night and tonight, overrides included."""
    usual_start, usual_end = sleep
    yesterday, tomorrow = get(meta, day - timedelta(days=1)), get(meta, day + timedelta(days=1))
    today = get(meta, day)
    last = (_night_start(day - timedelta(days=1), yesterday.get("sleep", usual_start)),
            datetime.combine(day, _t(today.get("wake", usual_end))))
    tonight = (_night_start(day, today.get("sleep", usual_start)),
               datetime.combine(day + timedelta(days=1), _t(tomorrow.get("wake", usual_end))))
    return [last, tonight]


def apply(cfg, meta, day):
    """Lays the day's overrides over cfg['planner'] (in place): the sleep intervals, when the morning may start,
    and the work window. `meta(key)` reads state.meta."""
    pc = cfg.get("planner") or {}
    if "sleep" not in pc:
        return cfg
    today = get(meta, day)
    tonight_starts = nights(pc["sleep"], meta, day)[1][0]
    pc["sleep_nights"] = [[s.isoformat(), e.isoformat()] for s, e in nights(pc["sleep"], meta, day)]
    pc["sleep_nights_day"] = day.isoformat()
    if today.get("wake"):
        pc["plan_after"] = today["wake"]
    if "work_window" in pc:
        start, end = pc["work_window"]
        if today.get("wake"):
            start = max(start, (datetime.combine(day, _t(today["wake"])) + GAP).strftime("%H:%M"))
        if today.get("sleep"):
            last = tonight_starts - GAP
            end = "23:59" if last.date() > day else last.strftime("%H:%M")
        pc["work_window"] = [start, end]
    pc["day_times"] = today
    return cfg


def sleep_for(pc, day, tz, sleep_intervals):
    """Sleep intervals for `day`: the overridden ones apply() prepared, else the usual window."""
    if pc.get("sleep_nights_day") == day.isoformat():
        return [(datetime.fromisoformat(s).replace(tzinfo=tz), datetime.fromisoformat(e).replace(tzinfo=tz))
                for s, e in pc["sleep_nights"]]
    return sleep_intervals(day, pc["sleep"], tz)


def describe(pc, day):
    """ "Up since 10:05; bed tonight at 02:00" for /today, or "" when the usual times apply."""
    today = pc.get("day_times") or {}
    parts = []
    if today.get("wake"):
        parts.append(f"up since {today['wake']}")
    if today.get("sleep"):
        parts.append(f"bed tonight at {today['sleep']}")
    return "; ".join(parts).capitalize()

