"""Settings you can change from Telegram (/settings) or the web page, without editing config.yaml.

Each setting is described once here (label, kind, choices, limits) and validated here; changes are stored in
state.db (settings table) and config.load_config() lays them over config.yaml, so every script picks them up on
its next run. "Reset" goes back to what config.yaml says.
"""
from dataclasses import dataclass, field

from dates import resolve_time, resolve_time_range


@dataclass(frozen=True)
class Setting:
    key: str                  # dotted config key
    label: str
    kind: str                 # time / range / int / float / bool / channels
    choices: tuple = ()
    low: float = None
    high: float = None
    help: str = ""
    examples: tuple = field(default=())


CHANNELS = (("telegram",), ("desktop", "telegram"))
SETTINGS = [
    Setting("planner.plan_after", "Morning starts at", "time", ("06:00", "06:45", "07:30", "08:00", "09:00"),
            help="the check-in is sent the first time the laptop is on after this"),
    Setting("morning.checkin", "Morning to-do question", "bool", (True, False)),
    Setting("morning.wait_minutes", "Wait for your check-in answer (min)", "int", (15, 30, 45, 60, 90), 5, 240),
    Setting("morning.latest_checkin", "No morning question after", "time", ("12:00", "15:00", "18:00", "21:00")),
    Setting("planner.work_window", "Work hours", "range", (("08:00", "23:00"), ("09:00", "21:00"), ("07:00", "22:00")),
            help="free slots are only suggested inside these hours"),
    Setting("planner.sleep", "Sleep (never planned)", "range", (("23:30", "07:00"), ("00:30", "08:00"), ("23:00", "06:30"))),
    Setting("planner.max_work_hours_per_day", "Daily work limit (h)", "float", (4, 5, 6, 7, 8, 10), 1, 16),
    Setting("planner.block_minutes", "Longest work block (min)", "int", (45, 60, 90, 120), 15, 240),
    Setting("planner.default_effort_hours", "Default work per deadline (h)", "float", (1, 2, 3, 4, 6, 8), 0.5, 40),
    Setting("planner.exam_prep_hours.quiz", "Prep for a quiz (h)", "float", (1, 2, 3, 4), 0, 40),
    Setting("planner.exam_prep_hours.midsem", "Prep for a midsem (h)", "float", (4, 6, 8, 10, 12), 0, 60),
    Setting("planner.exam_prep_hours.endsem", "Prep for an endsem (h)", "float", (8, 10, 12, 15, 20), 0, 80),
    Setting("morning.heads_up_minutes", "Heads-up before a block (min, 0 = off)", "int", (0, 5, 10, 15), 0, 60),
    Setting("morning.remind_after_minutes", "Reminder for tasks without a time (min)", "int", (60, 120, 180, 240), 15, 600),
    Setting("morning.evening_check", "Evening check at", "time", ("20:30", "21:30", "22:00", "22:30")),
    Setting("digest.channels", "Morning message goes to", "channels", CHANNELS),
]
BY_KEY = {s.key: s for s in SETTINGS}


def current(cfg, key):
    node = cfg
    for part in key.split("."):
        node = (node or {}).get(part) if isinstance(node, dict) else None
    return node


def show(setting, value):
    if value is None:
        return "not set"
    if setting.kind == "bool":
        return "on" if value else "off"
    if setting.kind == "range":
        return f"{value[0]}-{value[1]}"
    if setting.kind == "channels":
        return " + ".join(value)
    if isinstance(value, float) and value.is_integer():
        return f"{int(value)}"
    return str(value)


def parse(setting, text):
    """A typed value -> the stored value; ValueError with a short reason if it isn't valid."""
    text = (text or "").strip()
    if setting.kind == "time":
        t = resolve_time(text)
        if t is None:
            raise ValueError("send a time like 07:30 or 7:30 am")
        return f"{t:%H:%M}"
    if setting.kind == "range":
        start, end = resolve_time_range(text.replace(" to ", "-"))
        if start is None or end is None or start == end:
            raise ValueError("send two times like 09:00-21:00")
        if setting.key == "planner.work_window" and start > end:
            raise ValueError("work hours must end after they start")
        return [f"{start:%H:%M}", f"{end:%H:%M}"]
    if setting.kind in ("int", "float"):
        try:
            number = float(text.lower().replace("h", "").replace("min", "").strip())
        except ValueError:
            raise ValueError("send a number") from None
        if (setting.low is not None and number < setting.low) or (setting.high is not None and number > setting.high):
            raise ValueError(f"between {setting.low:g} and {setting.high:g}")
        return int(number) if setting.kind == "int" else number
    if setting.kind == "bool":
        if text.lower() in ("on", "yes", "true", "1"):
            return True
        if text.lower() in ("off", "no", "false", "0"):
            return False
        raise ValueError("send on or off")
    raise ValueError("pick one of the buttons")


def normalise(setting, value):
    """A chosen button value in the form config.yaml uses."""
    if setting.kind in ("range", "channels"):
        return list(value)
    return value


def set_value(state, setting, value):
    if setting.kind in ("int", "float") and not isinstance(value, bool):
        if (setting.low is not None and value < setting.low) or (setting.high is not None and value > setting.high):
            raise ValueError(f"between {setting.low:g} and {setting.high:g}")
    state.set_setting(setting.key, normalise(setting, value))


# --- Telegram: /settings ------------------------------------------------------------------------------

def menu(cfg):
    lines = ["Settings - tap a number to change it:"]
    lines += [f"{i}. {s.label}: {show(s, current(cfg, s.key))}" for i, s in enumerate(SETTINGS, 1)]
    numbers = [(str(i), f"set:{i - 1}") for i in range(1, len(SETTINGS) + 1)]
    return "\n".join(lines), [numbers[i:i + 6] for i in range(0, len(numbers), 6)]


def detail(cfg, index, note=""):
    s = SETTINGS[index]
    lines = ([note, ""] if note else []) + [f"{s.label}: {show(s, current(cfg, s.key))}"]
    if s.help:
        lines.append(f"({s.help})")
    buttons = [[(show(s, normalise(s, c)), f"setv:{index}:{j}") for j, c in enumerate(s.choices)][k:k + 3]
               for k in range(0, len(s.choices), 3)]
    extra = [("Type a value", f"sett:{index}")] if s.kind not in ("bool", "channels") else []
    buttons.append(extra + [("Reset", f"setr:{index}"), ("Back", "setb")])
    return "\n".join(lines), buttons


def handle(listener, cq, action, rest, now):
    """setb, set:<i> (open), setv:<i>:<j> (a choice), sett:<i> (type it), setr:<i> (back to config.yaml)."""
    from config import load_config
    tg, state, message_id = listener.tg, listener.state, cq["message"]["message_id"]
    if action == "setb":
        tg.edit(message_id, *menu(listener.cfg))
        return
    index_s, _, arg = rest.partition(":")
    if not index_s.isdigit() or int(index_s) >= len(SETTINGS):
        return
    index, s = int(index_s), SETTINGS[int(index_s)]
    note = ""
    if action == "setv" and arg.isdigit() and int(arg) < len(s.choices):
        set_value(state, s, s.choices[int(arg)])
        note = "Saved."
    elif action == "setr":
        state.clear_setting(s.key)
        note = "Back to the value in config.yaml."
    elif action == "sett":
        state.set_conv(now, "setting", index=index, message_id=message_id)
        note = f"Send the new value as a message, e.g. {_example(s)}."
    listener.cfg = load_config()
    tg.edit(message_id, *detail(listener.cfg, index, note))


def _example(s):
    return {"time": "07:30", "range": "09:00-21:00", "int": f"{s.choices[1] if len(s.choices) > 1 else 30}",
            "float": f"{s.choices[1] if len(s.choices) > 1 else 3}"}.get(s.kind, "a value")


def typed(listener, conv, text, now):
    """The value typed after "Type a value"."""
    from config import load_config
    s = SETTINGS[conv["index"]]
    try:
        value = parse(s, text)
    except ValueError as e:
        listener.tg.send(f"{s.label}: that didn't work ({e}). Try again, or tap Back.")
        return
    set_value(listener.state, s, value)
    listener.state.clear_conv()
    listener.cfg = load_config()
    listener.tg.edit(conv["message_id"], *detail(listener.cfg, conv["index"], "Saved."))
    listener.tg.send(f"{s.label}: now {show(s, value)}.")
