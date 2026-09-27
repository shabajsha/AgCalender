"""/deadlines: your upcoming deadlines with Done / Effort / Date / Not doing for each.

Done ticks the task off and removes its upcoming work blocks (the planner stops planning it); Effort changes how
much work it needs; Date moves the DUE event and its task (+1 day, +2 days, +1 week, or a date you type);
Not doing deletes the deadline, its task and its work blocks after you confirm.
"""
from datetime import datetime, timedelta

import actions
import google_writer
import planner
from dates import resolve_date, resolve_time

SHOWN = 10
EFFORT_CHOICES = [1, 2, 4, 8, 12, 20]


def upcoming(cfg, cal, now, days=14):
    evs = [ev for ev in google_writer.list_events(cal, cfg["calendars"]["college"], now, now + timedelta(days=days))
           if ev.get("summary", "").startswith(google_writer.DUE_PREFIX)]
    return evs[:SHOWN]


def _due(ev, tz):
    return planner._local(ev["end"], tz) or datetime.combine(
        datetime.fromisoformat(ev["start"]["date"]).date(), datetime.strptime("23:59", "%H:%M").time(), tz)


def list_message(cfg, state, cal, now, note=""):
    evs = upcoming(cfg, cal, now)
    if not evs:
        return (note + "\n\n" if note else "") + "No deadlines in the next 2 weeks.", None
    lines = ([note, ""] if note else []) + ["Deadlines coming up:"]
    buttons = []
    for n, ev in enumerate(evs, 1):
        key = f"event:{ev['id']}"
        effort = planner._effort(state, key, cfg["planner"]["default_effort_hours"])
        lines.append(f"{n}. {_due(ev, now.tzinfo):%a %d %b %H:%M}  {ev['summary'][len(google_writer.DUE_PREFIX):]} "
                     f"({effort:g} h of work)")
        buttons.append([(f"{n} Done", f"dl:d:{ev['id']}"), (f"{n} Effort", f"dl:e:{ev['id']}"),
                        (f"{n} Date", f"dl:m:{ev['id']}"), (f"{n} Not doing", f"dl:x:{ev['id']}")])
    return "\n".join(lines), buttons


def _title(cal, cfg, event_id):
    try:
        ev = cal.events().get(calendarId=cfg["calendars"]["college"], eventId=event_id).execute()
    except Exception:  # noqa: BLE001 - only for a label
        return None, None
    return ev, ev.get("summary", "")[len(google_writer.DUE_PREFIX):]


def handle(listener, cq, action, rest, now):
    """dl:<d|e|m|x>:<event>   dle:<event>:<h>   dlm:<event>:<days>   dlt:<event> (type a date)   dlx:<event> (yes)"""
    cfg, state, tg, cal = listener.cfg, listener.state, listener.tg, listener.calendar
    message_id = cq["message"]["message_id"]
    if action == "dl":
        op, _, event_id = rest.partition(":")
    else:
        event_id, _, arg = rest.partition(":")
    ev, title = _title(cal, cfg, event_id)
    if ev is None:
        tg.edit(message_id, *list_message(cfg, state, cal, now, "That deadline no longer exists."))
        return
    if action == "dl" and op == "d":
        removed = actions.finish_deadline(cfg, state, cal, listener.tasks, event_id, now)
        note = f"Done: {title}. Its task is ticked off" + (f" and {removed} upcoming block(s) removed." if removed else ".")
        tg.edit(message_id, *list_message(cfg, state, cal, now, note))
    elif action == "dl" and op == "e":
        choices = [(f"{h} h", f"dle:{event_id}:{h}") for h in EFFORT_CHOICES]
        tg.edit(message_id, f"{title}: how much work does it need in total?", [choices[:3], choices[3:], [("Back", "dlb:")]])
    elif action == "dle":
        state.set_effort(f"event:{event_id}", float(arg))
        tg.edit(message_id, *list_message(cfg, state, cal, now, f"{title}: {float(arg):g} h of work."))
    elif action == "dl" and op == "m":
        tg.edit(message_id, f"{title} is due {_due(ev, now.tzinfo):%a %d %b %H:%M}. Move it to:",
                [[("+1 day", f"dlm:{event_id}:1"), ("+2 days", f"dlm:{event_id}:2"), ("+1 week", f"dlm:{event_id}:7")],
                 [("Type a date", f"dlt:{event_id}"), ("Back", "dlb:")]])
    elif action == "dlm":
        _move(listener, message_id, ev, title, _due(ev, now.tzinfo) + timedelta(days=int(arg)), now)
    elif action == "dlt":
        state.set_conv(now, "deadline_date", event_id=event_id, message_id=message_id)
        tg.edit(message_id, f"{title}: send the new due date (and time if it changed), e.g. 'Friday 5pm' or '12 Oct'.")
    elif action == "dl" and op == "x":
        tg.edit(message_id, f"Not doing {title}? This deletes the deadline, its task and its work blocks.",
                [[("Yes, delete it", f"dlx:{event_id}"), ("Cancel", "dlb:")]])
    elif action == "dlx":
        actions.drop_deadline(cfg, state, cal, listener.tasks, event_id, now)
        tg.edit(message_id, *list_message(cfg, state, cal, now, f"Deleted: {title}."))


def back(listener, cq, now):
    listener.tg.edit(cq["message"]["message_id"], *list_message(listener.cfg, listener.state, listener.calendar, now))


def _move(listener, message_id, ev, title, due, now):
    cfg = listener.cfg
    google_writer.move_deadline(listener.calendar, listener.tasks, cfg, ev["id"], listener.state.task_for_event(ev["id"]), due)
    listener.tg.edit(message_id, *list_message(cfg, listener.state, listener.calendar, now,
                                               f"{title}: now due {due:%a %d %b %H:%M} (the task moved too)."))


def typed(listener, conv, text, now):
    ev, title = _title(listener.calendar, listener.cfg, conv["event_id"])
    if ev is None:
        listener.state.clear_conv()
        return
    old = _due(ev, now.tzinfo)
    day = resolve_date(text, now.date())
    if day is None:
        listener.tg.send("I couldn't read a date in that. Try 'Friday', '12 Oct' or '15/10 5pm'.")
        return
    t = resolve_time(text) or old.time()
    listener.state.clear_conv()
    _move(listener, conv["message_id"], ev, title, datetime.combine(day, t, now.tzinfo), now)
