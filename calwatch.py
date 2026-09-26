"""Watches your other calendars (timetable, contests, Outlook, Moodle) and asks on Telegram whether each new
event matters to you.

Per calendar (config.yaml -> calendar_watch; the bot's buttons and /calendars override it):
  ask    - a card per new event (one per repeating series): Track copies it into College, Ignore drops it.
           Until you answer, the event still blocks planning time.
  copy   - copy every new event into College without asking ("Always track calendar").
  show   - count it and show it, never copy (your own calendar, holidays).
  ignore - behave as if the calendar didn't exist ("Never ask this calendar").
Copies follow their original: if it moves, the copy moves; if it's cancelled, the copy is removed. You're told
about both in one short "Calendar updates" message.
"""
import json
import logging
import re
import time as time_module
from collections import Counter
from datetime import date, datetime, time, timedelta, timezone
from urllib.parse import urlparse

from googleapiclient.errors import HttpError

import google_writer
from extractor import clean_text
from ics_import import describe_recurrence
from state import dedupe_key

log = logging.getLogger(__name__)

POLICIES = ("ask", "copy", "show", "ignore")
POLICY_TEXT = {"ask": "ask me per event", "copy": "always copy into College", "show": "show, never copy",
               "ignore": "ignore"}
LIVE = ("pending", "tracked", "deadline", "linked")  # rows still attached to an upcoming event
DEADLINE_RE = re.compile(r"\b(due|deadline|submission|submit|quiz|exam|midsem|endsem|viva|assignment)\b", re.I)
BULK_LIST = 10       # events listed on a summary card
MAX_ONE_BY_ONE = 15  # cards sent at once after "One by one"


# --- calendars and policies -----------------------------------------------------------------------

def label(entry):
    """A calendar's display name. Subscribed feeds are named after their URL, which can hold a private token
    (the Moodle export URL does), so only the host is ever shown."""
    if entry.get("primary"):
        return "your calendar"
    name = entry.get("summaryOverride") or entry.get("summary") or entry["id"]
    if name.startswith(("http://", "https://", "webcal://")):
        return urlparse(name).hostname or "a subscribed calendar"
    return name


_ENTRY_CACHE = {}  # id(client) -> (monotonic time, client, entries)
ENTRY_CACHE_S = 300


def _calendar_entries(cal):
    """calendarList, cached for a few minutes per client so each button press doesn't cost a Google call."""
    hit = _ENTRY_CACHE.get(id(cal))
    if hit and hit[1] is cal and time_module.monotonic() - hit[0] < ENTRY_CACHE_S:
        return hit[2]
    entries = cal.calendarList().list(maxResults=250, showHidden=True).execute().get("items", [])
    _ENTRY_CACHE[id(cal)] = (time_module.monotonic(), cal, entries)
    return entries


def load_calendars(cal, cfg, state):
    """[{id, key, label, policy, selected}] for every calendar in your list; the agent's own are 'internal'."""
    wc = cfg.get("calendar_watch", {})
    configured, own = wc.get("calendars") or {}, set(cfg["calendars"].values())
    out = []
    for entry in _calendar_entries(cal):
        key = "primary" if entry.get("primary") else entry["id"]
        if entry["id"] in own:
            policy = "internal"
        else:
            policy = state.get_meta(f"calpolicy:{key}") or configured.get(key) or wc.get("default", "ask")
        out.append({"id": entry["id"], "key": key, "label": label(entry), "policy": policy,
                    "selected": bool(entry.get("selected")) and not entry.get("hidden")})
    return out


def event_key(ev):
    return ev.get("recurringEventId") or ev["id"]


def counts_as_busy(policy, status):
    """Unanswered events block time too (until you tap Ignore); ignored, cancelled and past ones don't."""
    return policy != "ignore" and status not in ("ignored", "gone", "expired")


def shown_today(policy, status):
    """Tracked events show up through their College copy, so only undecided ones are listed from the source."""
    if policy in ("internal", "show"):
        return True
    return policy in ("ask", "copy") and status in (None, "pending")


# --- small helpers on Google event dicts ------------------------------------------------------------

def _start_value(ev):
    return ev["start"].get("dateTime") or ev["start"].get("date")


def _dt(value, tz):
    if "T" in value:
        return datetime.fromisoformat(value).astimezone(tz)
    return date.fromisoformat(value)


def when_text(ev, tz):
    s, e = ev["start"], ev["end"]
    if "dateTime" in s:
        start, end = _dt(s["dateTime"], tz), _dt(e["dateTime"], tz)
        if start.date() == end.date():
            return f"{start:%a %d %b, %H:%M}-{end:%H:%M}"
        return f"{start:%a %d %b %H:%M} - {end:%a %d %b %H:%M}"
    return f"{date.fromisoformat(s['date']):%a %d %b} (all day)"


def core(ev, is_series, tz):
    """What counts as 'the event changed'. A series' next occurrence moves every week, so for a series only
    the time of day and length are compared."""
    s, e = ev["start"], ev["end"]
    if "dateTime" in s:
        start, end = _dt(s["dateTime"], tz), _dt(e["dateTime"], tz)
        when = [f"{start:%H:%M}", str(end - start)] if is_series else \
               [start.astimezone(timezone.utc).isoformat(), end.astimezone(timezone.utc).isoformat()]
    else:
        when = ["all-day"] if is_series else [s["date"], e["date"]]
    return {"summary": ev.get("summary", ""), "when": when, "location": ev.get("location", "")}


def due_of(ev, tz):
    """For 'It's a deadline': the end of a timed event (or its start if it has no length); all-day -> 23:59."""
    s, e = ev["start"], ev["end"]
    if "dateTime" in s:
        start, end = _dt(s["dateTime"], tz), _dt(e["dateTime"], tz)
        return end if end > start else start
    return datetime.combine(date.fromisoformat(s["date"]), time(23, 59), tz)


# --- grouping: a course with two weekly slots is two series but one question ----------------------------

WEEKDAY_ORDER = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def norm_title(title):
    return re.sub(r"[^a-z0-9]+", " ", (title or "").lower()).strip()


def group_rows(rows):
    """Series with the same title on the same calendar form one group; everything else stands alone."""
    groups = {}
    for r in rows:
        key = ("series", r["cal_id"], norm_title(r["title"])) if r["is_series"] else ("event", r["id"])
        groups.setdefault(key, []).append(r)
    return list(groups.values())


def siblings(state, row, statuses):
    """The row plus the other series of the same course (same calendar and title) in one of `statuses`."""
    if not row["is_series"]:
        return [row]
    same = [r for r in state.watch_rows(cal_id=row["cal_id"], statuses=statuses)
            if r["is_series"] and norm_title(r["title"]) == norm_title(row["title"])]
    return same or [row]


def repeats_text(master, instance, tz):
    """describe_recurrence, plus the weekday when the rule leaves it implicit (timetable feeds do)."""
    text = describe_recurrence(master.get("recurrence", [])) if master else ""
    day = _dt(_start_value(instance), tz)
    day = f"{day:%a}" if isinstance(day, (date, datetime)) else ""
    if text.startswith("weekly") and " on " not in text.split(" until ")[0].split(",")[0]:
        return text.replace("weekly", f"weekly on {day}", 1), day, text
    return text, "", text


def group_repeats(rows):
    snaps = [json.loads(r["snapshot"]) for r in rows]
    bases = {sn.get("repeat_base", "") for sn in snaps}
    days = sorted({sn["repeat_day"] for sn in snaps if sn.get("repeat_day")}, key=WEEKDAY_ORDER.index)
    if len(bases) == 1 and days and next(iter(bases)).startswith("weekly"):
        return next(iter(bases)).replace("weekly", f"weekly on {', '.join(days)}", 1)
    return "; ".join(dict.fromkeys(sn.get("repeats", "") for sn in snaps if sn.get("repeats")))


# --- cards --------------------------------------------------------------------------------------------

def card_text(cal_label, rows):
    rows = rows if isinstance(rows, list) else [rows]
    snap = json.loads(rows[0]["snapshot"])
    lines = [f"New on {cal_label}", clean_text(rows[0]["title"], 200) or "(no title)"]
    if rows[0]["is_series"]:
        lines.append(f"Repeats {group_repeats(rows)}" + (f" ({len(rows)} weekly slots)" if len(rows) > 1 else ""))
    else:
        lines.append(snap.get("when", ""))
    lines.append("Track = copy it into College and plan around it.")
    return "\n".join(line for line in lines if line)


def card_buttons(row, deadline_like):
    rows = [[("Track", f"cal:t:{row['id']}"), ("Ignore", f"cal:i:{row['id']}")]]
    if deadline_like:
        rows.append([("It's a deadline", f"cal:d:{row['id']}")])
    rows.append([("Always track calendar", f"cal:A:{row['id']}"), ("Never ask this calendar", f"cal:N:{row['id']}")])
    return rows


class Watcher:
    """One pass over your calendars, or one button press. `tg` may be None only in a dry run."""

    def __init__(self, cfg, state, cal, tasks_api, tg, now, dry_run=False):
        self.cfg, self.state, self.cal, self.tasks, self.tg = cfg, state, cal, tasks_api, tg
        self.now, self.tz, self.dry_run = now, now.tzinfo, dry_run
        self.college = cfg["calendars"]["college"]
        self.calendars = load_calendars(cal, cfg, state)
        self.by_id = {c["id"]: c for c in self.calendars}
        self.notes, self.stats = [], Counter()

    def _label(self, cal_id):
        return self.by_id[cal_id]["label"] if cal_id in self.by_id else "a calendar you removed"

    # --- the regular pass (called from ingest.py every 30 min and by "Check mail now") ---------------

    def run(self):
        wc = self.cfg.get("calendar_watch", {})
        horizon = self.now + timedelta(days=wc.get("days_ahead", 14))
        for c in self.calendars:
            if c["policy"] not in ("ask", "copy") or not c["selected"]:
                continue
            first = {}
            for ev in google_writer.list_events(self.cal, c["id"], self.now, horizon):
                first.setdefault(event_key(ev), ev)  # a series is represented by its next occurrence
            known = {r["event_key"]: r for r in self.state.watch_rows(cal_id=c["id"])}
            new = [(key, ev) for key, ev in first.items() if key not in known]
            for key, ev in first.items():
                if key in known and known[key]["status"] in LIVE + ("ignored",):
                    self._sync(c, known[key], ev)
            for key, row in known.items():
                if key not in first and row["status"] in LIVE and self._row_start(row) > self.now:
                    self._check_gone(c, row)
            if new:
                self._announce(c, new, wc.get("bulk_after", 5))
        self._expire()
        if self.notes and not self.dry_run:
            self.tg.send("Calendar updates:\n" + "\n".join(self.notes))
        return self.stats

    def _row_start(self, row):
        value = _dt(row["start"], self.tz)
        return value if isinstance(value, datetime) else datetime.combine(value, time(23, 59), self.tz)

    def _announce(self, c, new, bulk_after):
        rows = []
        for key, ev in new:
            is_series = bool(ev.get("recurringEventId"))
            repeats = repeat_day = repeat_base = ""
            if is_series:
                repeats, repeat_day, repeat_base = repeats_text(self._get(c["id"], key), ev, self.tz)
            title = ev.get("summary", "") or "(no title)"
            start = _dt(_start_value(ev), self.tz)
            already = self.state.pending_or_created(dedupe_key({"title": title, "start": start}))
            deadline_like = bool(DEADLINE_RE.search(title)) or any(w in c["label"] for w in ("courses.", "moodle"))
            snap = {"core": core(ev, is_series, self.tz), "when": when_text(ev, self.tz), "repeats": repeats,
                    "repeat_day": repeat_day, "repeat_base": repeat_base, "deadline_like": deadline_like and not is_series}
            if self.dry_run:
                print(f"WOULD ASK ({c['label']}): {title} - {repeats or snap['when']}"
                      + ("  [already added from an email]" if already else ""), flush=True)
            row_id = self.state.watch_add(c["id"], key, is_series, title, _start_value(ev), snap,
                                          "linked" if already else "pending")
            self.stats["new"] += 1
            if not already:
                rows.append(self.state.watch_row(row_id))
        if not rows or self.dry_run:
            return
        if c["policy"] == "copy":
            titles = [r["title"] for r in rows if self.track(r)]
            if titles:
                self.notes.append(f"- Copied {len(titles)} new event(s) from {c['label']} into College: "
                                  + ", ".join(titles[:5]) + (" ..." if len(titles) > 5 else ""))
        elif len(group_rows(rows)) > bulk_after:
            self._bulk_card(c, rows)
        else:
            for group in group_rows(rows):
                self._send_card(group)

    def _send_card(self, group):
        group = group if isinstance(group, list) else [group]
        snap = json.loads(group[0]["snapshot"])
        message_id = self.tg.send(card_text(self._label(group[0]["cal_id"]), group),
                                  card_buttons(group[0], snap.get("deadline_like")))
        for row in group:
            self.state.watch_set(row["id"], tg_message_id=message_id, batch=None)
        self.stats["asked"] += 1

    def _bulk_card(self, c, rows):
        batch = self.state.next_watch_batch()
        text, buttons = render_bulk(self, rows, batch)
        message_id = self.tg.send(text, buttons)
        for r in rows:
            self.state.watch_set(r["id"], batch=batch, tg_message_id=message_id)
        self.stats["asked"] += len(rows)

    def _sync(self, c, row, ev):
        """The original changed? Update our copy (or deadline) and tell you."""
        snap = json.loads(row["snapshot"])
        new_core, new_start = core(ev, row["is_series"], self.tz), _start_value(ev)
        if new_core == snap["core"]:
            if new_start != row["start"]:
                self.state.watch_set(row["id"], start=new_start)  # a series moved on to its next occurrence
            return
        snap.update(core=new_core, when=when_text(ev, self.tz))
        title = ev.get("summary") or row["title"]
        self.state.watch_set(row["id"], snapshot=snap, start=new_start, title=title)
        row = self.state.watch_row(row["id"])
        self.stats["changed"] += 1
        if self.dry_run:
            print(f"WOULD UPDATE ({c['label']}): {title} now {snap['when']}", flush=True)
        elif row["status"] == "tracked" and row["copy_id"]:
            source = self._get(c["id"], row["event_key"]) if row["is_series"] else ev
            if source:
                google_writer.update_copy(self.cal, self.college, row["copy_id"], source)
            self.notes.append(f"- {title} ({c['label']}) changed: now {snap['when']}. The College copy was updated.")
        elif row["status"] == "deadline" and row["copy_id"]:
            due = due_of(ev, self.tz)
            google_writer.move_deadline(self.cal, self.tasks, self.cfg, row["copy_id"],
                                        self.state.task_for_event(row["copy_id"]), due)
            self.notes.append(f"- Deadline {title} ({c['label']}) moved to {due:%a %d %b %H:%M}. Your deadline and task moved too.")
        elif row["status"] == "pending" and row["tg_message_id"] and not row["batch"]:
            self.tg.edit(row["tg_message_id"], card_text(c["label"], row), card_buttons(row, snap.get("deadline_like")))

    def _check_gone(self, c, row):
        ev = self._get(c["id"], row["event_key"])
        if ev is not None and ev.get("status") != "cancelled":
            if not row["is_series"]:
                self._sync(c, row, ev)  # still there, just moved outside the next two weeks
            return
        title = row["title"]
        self.stats["gone"] += 1
        if self.dry_run:
            print(f"WOULD NOTE CANCELLED ({c['label']}): {title}", flush=True)
            return
        if row["status"] == "tracked" and row["copy_id"]:
            google_writer.delete_event(self.cal, self.college, row["copy_id"])
            self.notes.append(f"- {title} was cancelled on {c['label']}; its copy was removed from College.")
        elif row["status"] == "deadline":
            self.notes.append(f"- {title} disappeared from {c['label']}. Your deadline is still there; "
                              "delete it if the deadline was dropped.")
        elif row["status"] == "pending" and row["tg_message_id"] and not row["batch"]:
            self.tg.edit(row["tg_message_id"], card_text(c["label"], row) + f"\n\nCancelled on {c['label']}.")
        self.state.watch_set(row["id"], status="gone", decided_at=self.now.isoformat())

    def _expire(self):
        for row in self.state.watch_rows(statuses=["pending"]):
            if self._row_start(row) < self.now:
                self.state.watch_set(row["id"], status="expired", decided_at=self.now.isoformat())
                if row["tg_message_id"] and not row["batch"] and not self.dry_run:
                    self.tg.edit(row["tg_message_id"], card_text(self._label(row["cal_id"]), row)
                                 + "\n\nThe event has passed; nothing was added.")

    def _get(self, cal_id, event_id):
        try:
            return self.cal.events().get(calendarId=cal_id, eventId=event_id).execute()
        except HttpError as e:
            if e.resp.status in (404, 410):
                return None
            raise

    # --- decisions (buttons) ------------------------------------------------------------------------

    def track(self, row):
        """Copies the event (or the whole series) into College. Returns False if it no longer exists."""
        source = self._get(row["cal_id"], row["event_key"])
        if source is None or source.get("status") == "cancelled":
            self.state.watch_set(row["id"], status="gone", decided_at=self.now.isoformat())
            return False
        copy_id = google_writer.create_copy(self.cal, self.college, source, self._label(row["cal_id"]),
                                            f"{row['cal_id']}|{row['event_key']}")
        self.state.watch_set(row["id"], status="tracked", copy_id=copy_id, decided_at=self.now.isoformat())
        self.stats["tracked"] += 1
        return True

    def ignore(self, row):
        """Ignore it; if it was tracked, its College copy is removed."""
        if row["status"] == "tracked" and row["copy_id"]:
            google_writer.delete_event(self.cal, self.college, row["copy_id"])
        self.state.watch_set(row["id"], status="ignored", copy_id=None, decided_at=self.now.isoformat())

    def as_deadline(self, row):
        """Makes it a deadline (DUE event + task), like an Added deadline card from an email."""
        ev = self._get(row["cal_id"], row["event_key"])
        if ev is None:
            return None
        due = due_of(ev, self.tz)
        item = {"type": "deadline", "title": clean_text(row["title"]) or "Deadline", "start": due - timedelta(minutes=30),
                "end": due, "all_day": False, "due": due, "course": None, "location": None, "description": None,
                "recurrence": None}
        msg = {"id": None, "subject": f"your {self._label(row['cal_id'])} calendar"}
        event_id, task_id = google_writer.create_item(self.cal, self.tasks, self.cfg, item, msg)
        self.state.record_item(dedupe_key(item), "deadline", event_id, task_id, item["title"], str(due), f"watch:{row['id']}")
        self.state.watch_set(row["id"], status="deadline", copy_id=event_id, decided_at=self.now.isoformat())
        return event_id

    def set_policy(self, cal_id, policy):
        key = self.by_id[cal_id]["key"] if cal_id in self.by_id else cal_id
        self.state.set_meta(f"calpolicy:{key}", policy)
        if cal_id in self.by_id:
            self.by_id[cal_id]["policy"] = policy


# --- Telegram button handling (called by the listener) ---------------------------------------------

def render_bulk(w, rows, batch):
    """Text and buttons of a summary card for the still-undecided `rows` of `batch`."""
    groups = group_rows(rows)
    repeating = sum(1 for g in groups if g[0]["is_series"])
    lines = [f"{w._label(rows[0]['cal_id'])}: {len(groups)} new event{'s' if len(groups) != 1 else ''}"
             + (f" ({repeating} repeating)" if repeating else "")]
    for g in groups[:BULK_LIST]:
        when = group_repeats(g) if g[0]["is_series"] else json.loads(g[0]["snapshot"])["when"]
        lines.append(f"- {clean_text(g[0]['title'], 80)}: {when}")
    if len(groups) > BULK_LIST:
        lines.append(f"...and {len(groups) - BULK_LIST} more")
    lines.append("\nTrack all = copy them into College. Until you decide, they still block planning time.")
    buttons = [[("Track all", f"calb:t:{batch}"), ("Ignore all", f"calb:i:{batch}")],
               [("One by one", f"calb:o:{batch}")],
               [("Always track calendar", f"calb:A:{batch}"), ("Never ask this calendar", f"calb:N:{batch}")]]
    return "\n".join(lines), buttons


def _decided_text(w, rows, outcome):
    rows = rows if isinstance(rows, list) else [rows]
    return card_text(w._label(rows[0]["cal_id"]), [w.state.watch_row(r["id"]) for r in rows]) + f"\n\n{outcome}"


def _rerender(w, row_ids, skip_message=None):
    """Redraw every card these rows appear on, after Undo or a calendar-wide change: one edit per message."""
    rows = [w.state.watch_row(i) for i in row_ids]
    done = {skip_message}
    for r in rows:
        mid = r["tg_message_id"]
        if not mid or mid in done:
            continue
        done.add(mid)
        if r["batch"]:
            waiting = w.state.watch_rows(batch=r["batch"], statuses=["pending"])
            if waiting:
                w.tg.edit(mid, *render_bulk(w, waiting, r["batch"]))
            else:
                counts = Counter(x["status"] for x in w.state.watch_rows(batch=r["batch"]))
                w.tg.edit(mid, f"{w._label(r['cal_id'])}: " + ", ".join(f"{n} {st}" for st, n in counts.items()) + ".")
            continue
        group = [x for x in rows if x["tg_message_id"] == mid] or [r]
        if all(x["status"] == "pending" for x in group):
            w.tg.edit(mid, card_text(w._label(r["cal_id"]), group),
                      card_buttons(group[0], json.loads(group[0]["snapshot"]).get("deadline_like")))
        else:
            w.tg.edit(mid, _decided_text(w, group, {"tracked": "Tracked: copied into College.", "ignored": "Ignored.",
                                                    "deadline": "Added as a deadline."}.get(group[0]["status"], "")))


def _save_undo(state, record):
    n = int(state.get_meta("calundo_seq") or 0) + 1
    state.set_meta("calundo_seq", str(n))
    state.set_meta(f"calundo:{n}", json.dumps(record))
    return n


def _undo(w, n):
    """Puts rows (and the calendar's setting) back as they were before one button press, removing any
    copies or deadlines that press created. Returns the restored row ids, or None if already undone."""
    raw = w.state.get_meta(f"calundo:{n}")
    if not raw:
        return None
    rec = json.loads(raw)
    if rec.get("policy") is not None:
        key = w.by_id[rec["cal_id"]]["key"] if rec["cal_id"] in w.by_id else rec["cal_id"]
        w.state.set_meta(f"calpolicy:{key}", rec["policy"])  # "" = back to what config.yaml says
    for row_id, status, copy_id in rec["rows"]:
        row = w.state.watch_row(row_id)
        if row["status"] == "tracked" and row["copy_id"] and row["copy_id"] != copy_id:
            google_writer.delete_event(w.cal, w.college, row["copy_id"])
        elif row["status"] == "deadline" and row["copy_id"]:
            task_id = w.state.task_for_event(row["copy_id"])
            google_writer.delete_event(w.cal, w.college, row["copy_id"])
            if task_id:
                try:
                    w.tasks.tasks().delete(tasklist=w.cfg.get("tasklist", "@default"), task=task_id).execute()
                except HttpError as e:
                    if e.resp.status not in (404, 410):
                        raise
            w.state.delete_item_by_event(row["copy_id"])
        w.state.watch_set(row_id, status=status, copy_id=copy_id, decided_at=None)
    w.state.set_meta(f"calundo:{n}", "")  # one Undo per press
    return [r[0] for r in rec["rows"]]


def _snapshot_rows(rows):
    return [[r["id"], r["status"], r["copy_id"]] for r in rows]


def handle_callback(listener, cq, action, rest, now):
    """cal:<op>:<row>  one event        calb:<op>:<batch>  summary card     cale:<row>:<hours>  deadline effort
    calc:<op>:<ref>  confirm/cancel a calendar-wide change (ref = r<row> or b<batch>)
    calu:<n>         undo                                  calp:<index>  /calendars menu"""
    w = Watcher(listener.cfg, listener.state, listener.calendar, listener.tasks, listener.tg, now)
    tg, state, message_id = listener.tg, listener.state, cq["message"]["message_id"]
    log.info("calendar button %s:%s", action, rest)

    if action == "calp":
        return _cycle_policy(w, cq, rest)

    if action == "calu":
        restored = _undo(w, int(rest)) if rest.isdigit() else None
        if restored is None:
            tg.edit(message_id, "Already undone.")
            return
        _rerender(w, restored)
        log.info("undid calendar change %s (%d events restored)", rest, len(restored))
        return

    if action == "calc":  # the answer to "Stop asking about ...?" / "Copy everything from ...?"
        op, _, ref = rest.partition(":")
        rows = _ref_rows(state, ref)
        if not rows:
            tg.edit(message_id, "Nothing left to change here.")
            return
        cal_id = rows[0]["cal_id"]
        if op == "x":
            _rerender(w, [r["id"] for r in (state.watch_rows(batch=int(ref[1:])) if ref[0] == "b" else rows)])
            return
        policy = "copy" if op == "A" else "ignore"
        key = w.by_id[cal_id]["key"] if cal_id in w.by_id else cal_id
        before = state.watch_rows(cal_id=cal_id, statuses=["pending"])
        undo = _save_undo(state, {"cal_id": cal_id, "policy": state.get_meta(f"calpolicy:{key}") or "",
                                  "rows": _snapshot_rows(before)})
        _apply_policy_to_calendar(w, cal_id, policy, skip_message=message_id)
        label_ = w._label(cal_id)
        tg.edit(message_id, (f"{label_}: every event will be copied into College from now on"
                             f" ({len(before)} waiting event(s) copied)." if policy == "copy" else
                             f"{label_}: I won't ask about this calendar again ({len(before)} waiting event(s) ignored)."),
                [[("Undo", f"calu:{undo}")]])
        log.info("calendar %s set to %s", label_, policy)
        return

    if action == "calb":
        op, _, raw = rest.partition(":")
        rows = state.watch_rows(batch=int(raw), statuses=["pending"]) if raw.isdigit() else []
        if not rows:
            tg.edit(message_id, "Already handled.")
            return
        c_label = w._label(rows[0]["cal_id"])
        if op in ("A", "N"):
            return _confirm(w, message_id, rows[0]["cal_id"], op, f"b{raw}")
        if op == "o":
            groups = group_rows(rows)
            for g in groups[:MAX_ONE_BY_ONE]:
                w._send_card(g)
            left = len(groups) - MAX_ONE_BY_ONE
            tg.edit(message_id, f"{c_label}: sent {min(len(groups), MAX_ONE_BY_ONE)} one by one below."
                    + (f" {left} more are waiting; tap One by one again." if left > 0 else ""),
                    [[("One by one", f"calb:o:{raw}")]] if left > 0 else None)
            return
        undo = _save_undo(state, {"cal_id": rows[0]["cal_id"], "policy": None, "rows": _snapshot_rows(rows)})
        if op == "t":
            done = sum(1 for r in rows if w.track(r))
            tg.edit(message_id, f"{c_label}: tracked {done} event(s); copied into College.", [[("Undo", f"calu:{undo}")]])
        elif op == "i":
            for r in rows:
                w.ignore(r)
            tg.edit(message_id, f"{c_label}: ignored {len(rows)} event(s).", [[("Undo", f"calu:{undo}")]])
        return

    if action == "cale":  # effort for a deadline made from a calendar event
        raw_id, _, hours = rest.partition(":")
        row = state.watch_row(int(raw_id)) if raw_id.isdigit() else None
        if not row or not row["copy_id"]:
            return
        state.set_effort(f"event:{row['copy_id']}", float(hours))
        tg.edit(message_id, _decided_text(w, row, f"Added as a deadline. Work needed: {float(hours):g} h"))
        return

    op, _, raw_id = rest.partition(":")
    row = state.watch_row(int(raw_id)) if raw_id.isdigit() else None
    if row is None:
        return
    if op == "t":
        group = siblings(state, row, ["pending", "ignored"])
        ok = [w.track(r) for r in group]
        tg.edit(message_id, _decided_text(w, group, "Tracked: copied into College." if any(ok) else
                                          "That event no longer exists; nothing was added."),
                [[("Untrack", f"cal:u:{row['id']}")]] if any(ok) else None)
    elif op in ("i", "u"):
        group = siblings(state, row, ["pending", "tracked"])
        for r in group:
            w.ignore(r)
        tg.edit(message_id, _decided_text(w, group, "Untracked: the College copy was removed." if op == "u" else "Ignored."),
                [[("Track instead", f"cal:t:{row['id']}")]])
    elif op == "d":
        undo = _save_undo(state, {"cal_id": row["cal_id"], "policy": None, "rows": _snapshot_rows([row])})
        if w.as_deadline(row) is None:
            tg.edit(message_id, _decided_text(w, row, "That event no longer exists; nothing was added."))
        else:
            row = state.watch_row(row["id"])
            choices = [(f"{h:g} h", f"cale:{row['id']}:{h}") for h in listener.cfg["planner"]["effort_choices_hours"]]
            tg.edit(message_id, _decided_text(w, row, "Added as a deadline (calendar event + task). How much work does it need?"),
                    [choices[:3], choices[3:], [("Undo", f"calu:{undo}")]])
    elif op in ("A", "N"):
        _confirm(w, message_id, row["cal_id"], op, f"r{row['id']}")
    log.info("calendar %s on %r (%s)", {"t": "track", "i": "ignore", "u": "untrack", "d": "deadline",
                                        "A": "always-track?", "N": "never-ask?"}.get(op, op), row["title"], w._label(row["cal_id"]))


def _ref_rows(state, ref):
    if ref[:1] == "r" and ref[1:].isdigit():
        row = state.watch_row(int(ref[1:]))
        return siblings(state, row, ["pending"]) if row and row["status"] == "pending" else []
    if ref[:1] == "b" and ref[1:].isdigit():
        return state.watch_rows(batch=int(ref[1:]), statuses=["pending"])
    return []


def _confirm(w, message_id, cal_id, op, ref):
    """Calendar-wide buttons touch many events, so they ask once more (a misclick used to be final)."""
    waiting = len(w.state.watch_rows(cal_id=cal_id, statuses=["pending"]))
    label_ = w._label(cal_id)
    if op == "N":
        text = (f"Stop asking about {label_}?\nIts {waiting} waiting event(s) will be ignored, and new ones won't "
                "be asked about. You can undo this, or change it later with /calendars.")
        yes = "Yes, never ask"
    else:
        text = (f"Copy every event from {label_} into College from now on?\nIts {waiting} waiting event(s) will be "
                "copied too. You can undo this, or change it later with /calendars.")
        yes = "Yes, always copy"
    w.tg.edit(message_id, text, [[(yes, f"calc:{op}:{ref}"), ("Cancel", f"calc:x:{ref}")]])


def _apply_policy_to_calendar(w, cal_id, policy, skip_message=None):
    """Always track -> copy this calendar's waiting events too; Never ask -> ignore them. Each affected card is
    edited once."""
    w.set_policy(cal_id, policy)
    touched = []
    for r in w.state.watch_rows(cal_id=cal_id, statuses=["pending"]):
        if policy == "copy":
            w.track(r)
        else:
            w.ignore(r)
        touched.append(r["id"])
    _rerender(w, touched, skip_message=skip_message)


# --- /calendars -------------------------------------------------------------------------------------

def show_menu(listener, now):
    w = Watcher(listener.cfg, listener.state, listener.calendar, listener.tasks, listener.tg, now)
    menu = [c for c in w.calendars if c["policy"] != "internal"]
    listener.state.set_meta("calendars_menu", json.dumps([c["id"] for c in menu]))
    listener.tg.send(_menu_text(menu), _menu_buttons(menu))


def _menu_text(menu):
    return ("Your calendars. Tap one to change what I do with it:\n"
            "ask = card per event, copy = always copy into College, show = count it but never copy, ignore\n\n"
            + "\n".join(f"- {c['label']}: {POLICY_TEXT[c['policy']]}" for c in menu))


def _menu_buttons(menu):
    return [[(f"{c['label'][:28]}: {c['policy']}", f"calp:{i}")] for i, c in enumerate(menu)]


def _cycle_policy(w, cq, raw_index):
    ids = json.loads(w.state.get_meta("calendars_menu") or "[]")
    if not raw_index.isdigit() or int(raw_index) >= len(ids) or ids[int(raw_index)] not in w.by_id:
        w.tg.answer(cq["id"], "Menu is out of date; send /calendars again")
        return
    c = w.by_id[ids[int(raw_index)]]
    new = POLICIES[(POLICIES.index(c["policy"]) + 1) % len(POLICIES)] if c["policy"] in POLICIES else "ask"
    w.set_policy(c["id"], new)
    menu = [w.by_id[i] for i in ids if i in w.by_id]
    w.tg.edit(cq["message"]["message_id"], _menu_text(menu), _menu_buttons(menu))
    w.tg.answer(cq["id"], f"{c['label'][:30]}: {new}")
