"""Habits (gym, reading, ...): set up from Telegram with /habits, offered as free slots on their days inside their
window like any other task, with a done-check afterwards and a streak.

A streak counts the habit's scheduled days in a row that you marked Done (days it isn't scheduled don't break it;
today only counts once it's done).
"""
from datetime import datetime, timedelta

from dates import resolve_time_range

DAY_KEYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
DAY_LABELS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
PRESET_DAYS = {"all": DAY_KEYS, "wk": DAY_KEYS[:5], "we": DAY_KEYS[5:]}
MINUTE_CHOICES = [15, 30, 45, 60, 90, 120]
WINDOWS = [("Early 06-09", "06:00", "09:00"), ("Morning 09-12", "09:00", "12:00"), ("Afternoon 12-17", "12:00", "17:00"),
           ("Evening 17-21", "17:00", "21:00"), ("Night 21-23", "21:00", "23:00"), ("Any time", "06:00", "23:00")]
IMPORTED = "habits_imported"


def ensure_imported(cfg, state):
    """Habits written in config.yaml become database habits, once."""
    if state.get_meta(IMPORTED):
        return
    for h in cfg.get("habits") or []:
        days = ",".join(d.lower()[:3] for d in h.get("days", DAY_KEYS))
        state.habit_add(h["name"], int(h["minutes"]), days, h["window"][0], h["window"][1])
    state.set_meta(IMPORTED, "1")


def for_day(cfg, state, day):
    """Active habits scheduled on `day`: [{id, name, minutes, window: [start, end]}]."""
    ensure_imported(cfg, state)
    key = DAY_KEYS[day.weekday()]
    return [{"id": h["id"], "name": h["name"], "minutes": h["minutes"], "window": [h["window_start"], h["window_end"]]}
            for h in state.habits(active_only=True) if key in (h["days"] or "").split(",")]


def streak(state, habit, today):
    done_days = {datetime.fromisoformat(b["start"]).date() for b in state.blocks(statuses=["done"], work_key=f"habit:{habit['id']}")}
    scheduled = set((habit["days"] or "").split(","))
    created = datetime.fromisoformat(habit["created_at"]).date() if habit.get("created_at") else today - timedelta(days=365)
    day, count = today, 0
    if today not in done_days:
        day -= timedelta(days=1)  # today isn't over: it only counts once it's done
    while day >= created:
        if DAY_KEYS[day.weekday()] in scheduled:
            if day not in done_days:
                break
            count += 1
        day -= timedelta(days=1)
    return count


def describe(h):
    days = (h["days"] or "").split(",")
    when = "every day" if set(days) == set(DAY_KEYS) else "weekdays" if days == DAY_KEYS[:5] else \
        "weekends" if days == DAY_KEYS[5:] else " ".join(DAY_LABELS[DAY_KEYS.index(d)] for d in days if d in DAY_KEYS)
    return f"{h['minutes']} min, {when}, {h['window_start']}-{h['window_end']}"


# --- Telegram: /habits and the "new habit" flow -----------------------------------------------------------

def list_message(cfg, state, today):
    ensure_imported(cfg, state)
    habits = state.habits()
    if not habits:
        return "No habits yet. Add one and I'll suggest free slots for it on its days.", [[("New habit", "hbn")]]
    lines, buttons = ["Your habits:"], []
    for n, h in enumerate(habits, 1):
        paused = "" if h["active"] else " (paused)"
        run = streak(state, h, today)
        lines.append(f"{n}. {h['name']}: {describe(h)}{paused}" + (f" - streak {run} day{'s' if run != 1 else ''}" if run else ""))
        buttons.append([(f"{n} {'Pause' if h['active'] else 'Resume'}", f"hbp:{h['id']}"), (f"{n} Delete", f"hbr:{h['id']}")])
    buttons.append([("New habit", "hbn")])
    return "\n".join(lines), buttons


def _flow_message(data):
    step = data["step"]
    if step == "minutes":
        return (f"{data['name']}: how long each time? (or type it, e.g. 50m)",
                [[(f"{m} min", f"hbm:{m}") for m in MINUTE_CHOICES[:3]], [(f"{m} min", f"hbm:{m}") for m in MINUTE_CHOICES[3:]],
                 [("Cancel", "hbx")]])
    if step == "days":
        chosen = set(data.get("days", []))
        toggles = [(("✓ " if k in chosen else "") + label, f"hbd:{k}") for k, label in zip(DAY_KEYS, DAY_LABELS)]
        return (f"{data['name']}, {data['minutes']} min: which days? Tap to toggle, then Next.",
                [toggles[:4], toggles[4:], [("Every day", "hbd:all"), ("Weekdays", "hbd:wk"), ("Weekends", "hbd:we")],
                 [("Next", "hbd:ok"), ("Cancel", "hbx")]])
    if step == "window":
        return (f"{data['name']}: at what time of day? (or type it, e.g. 18:00-20:00)",
                [[(label, f"hbw:{i}") for i, (label, _, _) in enumerate(WINDOWS[:3])],
                 [(label, f"hbw:{i}") for i, (label, _, _) in enumerate(WINDOWS[3:], 3)], [("Cancel", "hbx")]])
    habit = {"minutes": data["minutes"], "days": ",".join(data["days"]), "window_start": data["window"][0],
             "window_end": data["window"][1]}
    return (f"New habit: {data['name']}, {describe(habit)}. Save it?", [[("Save", "hbs"), ("Cancel", "hbx")]])


def _advance(listener, now, data, message_id=None):
    data = {k: v for k, v in data.items() if k not in ("flow", "expires")}
    listener.state.set_conv(now, "habit", **data)
    text, buttons = _flow_message(data)
    if message_id:
        listener.tg.edit(message_id, text, buttons)
    else:
        data["message_id"] = listener.tg.send(text, buttons)
        listener.state.set_conv(now, "habit", **data)


def handle(listener, cq, action, rest, now):
    state, tg, message_id = listener.state, listener.tg, cq["message"]["message_id"]
    if action == "hbn":
        state.set_conv(now, "habit", step="name")
        tg.send("What's the habit called? (e.g. Gym, Reading, Walk)")
        return
    if action in ("hbp", "hbr", "hby"):
        habit = state.habit(int(rest)) if rest.isdigit() else None
        if habit is None:
            tg.edit(message_id, *list_message(listener.cfg, state, now.date()))
            return
        if action == "hbp":
            state.habit_set(habit["id"], active=0 if habit["active"] else 1)
        elif action == "hbr":
            tg.edit(message_id, f"Delete the habit {habit['name']}? Its past blocks stay in your calendar.",
                    [[("Yes, delete", f"hby:{habit['id']}"), ("Cancel", "hbl")]])
            return
        else:
            state.habit_delete(habit["id"])
        tg.edit(message_id, *list_message(listener.cfg, state, now.date()))
        return
    if action == "hbl":
        tg.edit(message_id, *list_message(listener.cfg, state, now.date()))
        return
    conv = state.conv(now)
    if not conv or conv.get("flow") != "habit":
        tg.edit(message_id, "That habit set-up has expired; send /habits to start again.")
        return
    if action == "hbx":
        state.clear_conv()
        tg.edit(message_id, "OK, no new habit.")
        return
    if action == "hbm" and rest.isdigit():
        _advance(listener, now, {**conv, "minutes": int(rest), "step": "days", "days": []}, message_id)
    elif action == "hbd":
        days = list(conv.get("days", []))
        if rest in PRESET_DAYS:
            days = list(PRESET_DAYS[rest])
        elif rest in DAY_KEYS:
            days = [d for d in DAY_KEYS if (d in days) != (d == rest)]
        elif rest == "ok":
            if not days:
                tg.edit(message_id, *_flow_message(conv))
                return
            _advance(listener, now, {**conv, "step": "window"}, message_id)
            return
        _advance(listener, now, {**conv, "days": days}, message_id)
    elif action == "hbw" and rest.isdigit() and int(rest) < len(WINDOWS):
        _, start, end = WINDOWS[int(rest)]
        _advance(listener, now, {**conv, "window": [start, end], "step": "confirm"}, message_id)
    elif action == "hbs" and conv.get("step") == "confirm":
        state.habit_add(conv["name"], conv["minutes"], ",".join(conv["days"]), *conv["window"])
        state.clear_conv()
        tg.edit(message_id, f"Saved: {conv['name']}. On its days you'll get free slots for it with the other tasks.")


def typed(listener, conv, text, now):
    """Typed answers during the flow: the name, a length ("50m") or a window ("18:00-20:00")."""
    import todos
    step = conv.get("step")
    if step == "name":
        name = text.strip()[:60]
        if not name:
            return
        _advance(listener, now, {"step": "minutes", "name": name})
    elif step == "minutes":
        parsed = todos.parse(f"x {text}", 0)
        minutes = parsed[0][1] if parsed else 0
        if minutes < 5:
            listener.tg.send("Send a length like 45m or 1h, or tap one of the buttons.")
            return
        _advance(listener, now, {**conv, "minutes": minutes, "step": "days", "days": []}, conv.get("message_id"))
    elif step == "window":
        start, end = resolve_time_range(text.replace(" to ", "-"))
        if start is None or end is None or start >= end:
            listener.tg.send("Send a time range like 18:00-20:00, or tap one of the buttons.")
            return
        _advance(listener, now, {**conv, "window": [f"{start:%H:%M}", f"{end:%H:%M}"], "step": "confirm"},
                 conv.get("message_id"))
    else:
        listener.tg.send("Use the buttons above to finish the habit, or tap Cancel.")


def today_items(cfg, state, now, booked_minutes):
    """Habits for the slot picker: [{key, title, kind: habit, due, window_start, need}] for today.
    `booked_minutes(key)` is what's already booked or done today for that habit."""
    today, out = now.date(), []
    for h in for_day(cfg, state, today):
        end = datetime.combine(today, datetime.strptime(h["window"][1], "%H:%M").time(), now.tzinfo)
        need = max(0, h["minutes"] - booked_minutes(f"habit:{h['id']}"))
        if end > now and need > 0:
            out.append({"key": f"habit:{h['id']}", "title": h["name"], "kind": "habit", "due": end, "need": need,
                        "window_start": h["window"][0], "today_min": h["minutes"], "list_id": None})
    return out


def minutes_on(state, key, day):
    """Minutes booked or done for `key` on `day` (not counting blocks answered Not done)."""
    total = 0.0
    for b in state.blocks(work_key=key):
        start = datetime.fromisoformat(b["start"])
        if start.date() == day and b["status"] not in ("notdone", "cleared"):
            total += (datetime.fromisoformat(b["end"]) - start).total_seconds() / 60
    return total

