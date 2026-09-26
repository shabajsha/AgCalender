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
import fcntl
import json
import logging
import re
import time as time_module
from collections import Counter
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta, timezone
from urllib.parse import urlparse

from dateutil.rrule import rrulestr
from googleapiclient.errors import HttpError

import google_writer
import logsetup
from extractor import clean_text
from ics_import import describe_recurrence
from state import dedupe_key
from telegram_bot import TelegramError

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


LOCK_FILE = logsetup.LOG_DIR / ".calwatch.lock"


class WatchBusy(Exception):
    """Another calendar scan (the mail check or the daily review) is still running."""


@contextmanager
def watch_lock(wait_s=0):
    """One calendar scan at a time. At wake-up the mail check and the morning review start together; two scans
    used to insert the same event twice (a crash and a false alert) or announce one change twice."""
    LOCK_FILE.parent.mkdir(exist_ok=True)
    with open(LOCK_FILE, "w") as f:
        deadline = time_module.monotonic() + wait_s
        while True:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time_module.monotonic() >= deadline:
                    raise WatchBusy("a calendar scan is already running") from None
                time_module.sleep(1)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


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


def series_core(instances, tz):
    """A series' usual time of day, length, title and place: the most common across its upcoming occurrences,
    so one moved or cancelled class isn't mistaken for a change to the whole series."""
    cores = [json.dumps(core(ev, True, tz), sort_keys=True) for ev in instances]
    return json.loads(Counter(cores).most_common(1)[0][0])


def series_ended(master, now):
    """True if a repeating event has no occurrence left after `now` (its UNTIL / COUNT has run out)."""
    start = master.get("start", {})
    try:
        first = _dt(start.get("dateTime") or start.get("date"), now.tzinfo)
        if not isinstance(first, datetime):
            first = datetime.combine(first, time(), now.tzinfo)
        rule = rrulestr("\n".join(master.get("recurrence") or []), dtstart=first, forceset=True)
        return rule.after(now) is None
    except (ValueError, TypeError, KeyError, AttributeError):
        return False  # can't tell: keep asking rather than drop it


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
        return [row] if row["status"] in statuses else []
    same = [r for r in state.watch_rows(cal_id=row["cal_id"], statuses=statuses)
            if r["is_series"] and norm_title(r["title"]) == norm_title(row["title"])]
    return same or ([row] if row["status"] in statuses else [])


def _card_rows(state, row, message_id):
    """The rows shown on the tapped card (e.g. both weekly slots of a course). Not every series with that title:
    Ignore on a card for a newly added third slot used to untrack the two slots you had already tracked."""
    rows = [r for r in state.watch_rows(message_id=message_id) if r["cal_id"] == row["cal_id"]] if message_id else []
    return rows if any(r["id"] == row["id"] for r in rows) else [row]


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


def change_text(cal_label, row, detail):
    snap = json.loads(row["snapshot"])
    question = "Track it now?" if row["status"] == "ignored" else "Still want it?"
    return f"Changed on {cal_label}: {clean_text(row['title'], 200)}\n{detail[:1].upper() + detail[1:]}.\n{question}" \
        if detail else f"Changed on {cal_label}: {clean_text(row['title'], 200)}\n{snap.get('when', '')}\n{question}"


def change_buttons(row, prefix=""):
    """Buttons for a changed event (also used, numbered, in the daily review)."""
    rid = row["id"]
    if row["status"] == "ignored":
        return [[(f"{prefix}Track now", f"crv:t:{rid}"), (f"{prefix}Keep ignoring", f"crv:k:{rid}")]]
    remove = "Remove deadline" if row["status"] == "deadline" else "Remove"
    return [[(f"{prefix}Keep", f"crv:k:{rid}"), (f"{prefix}{remove}", f"crv:r:{rid}")]]


class Watcher:
    """One pass over your calendars, or one button press. `tg` may be None only in a dry run."""

    def __init__(self, cfg, state, cal, tasks_api, tg, now, dry_run=False):
        self.cfg, self.state, self.cal, self.tasks, self.tg = cfg, state, cal, tasks_api, tg
        self.now, self.tz, self.dry_run = now, now.tzinfo, dry_run
        self.college = cfg["calendars"]["college"]
        self.calendars = load_calendars(cal, cfg, state)
        self.by_id = {c["id"]: c for c in self.calendars}
        self.notes, self.stats = [], Counter()
        wc = cfg.get("calendar_watch", {})
        self.horizon = now + timedelta(days=wc.get("days_ahead", 14))
        # daily review: findings wait for one daily message, except events in the next `urgent_hours`
        self.review_mode = bool(wc.get("daily_review"))
        self.urgent = timedelta(hours=wc.get("urgent_hours", 24))

    def _label(self, cal_id):
        return self.by_id[cal_id]["label"] if cal_id in self.by_id else "a calendar you removed"

    def _started(self, ev):
        value = _dt(_start_value(ev), self.tz)
        start = value if isinstance(value, datetime) else datetime.combine(value, time(), self.tz)
        return start <= self.now

    def _fresh(self, row):
        return self.state.watch_row(row["id"]) or row

    # --- the regular pass (called from ingest.py every 30 min and by "Check mail now") ---------------

    def run(self):
        wc = self.cfg.get("calendar_watch", {})
        for c in self.calendars:
            scan = c["policy"] in ("ask", "copy") and c["selected"]
            known = {r["event_key"]: r for r in self.state.watch_rows(cal_id=c["id"])}
            # copies you decided on keep following their original, even after "Never ask" or /calendars -> show
            followed = {k: r for k, r in known.items() if scan or r["status"] in ("tracked", "deadline")}
            if not scan and not followed:
                continue
            first, instances = {}, {}
            for ev in google_writer.list_events(self.cal, c["id"], self.now, self.horizon):
                key = event_key(ev)
                instances.setdefault(key, []).append(ev)
                # a series is represented by its next occurrence that hasn't started yet: an occurrence in
                # progress used to make an unanswered series look "passed" and expire it
                if key not in first or (self._started(first[key]) and not self._started(ev)):
                    first[key] = ev
            for key, ev in first.items():
                row = followed.get(key)
                if row is None:
                    continue
                if row["status"] == "gone":
                    self._revive(c, row, ev)
                elif row["status"] in LIVE + ("ignored",):
                    self._sync(c, row, ev, instances[key])
            for key, row in followed.items():
                if key not in first and row["status"] in LIVE and (row["is_series"] or self._row_start(row) > self.now):
                    self._check_gone(c, row)
            new = [(key, ev) for key, ev in first.items() if key not in known] if scan else []
            if new:
                self._announce(c, new, wc.get("bulk_after", 5), instances)
        self._expire()
        self._requeue_unsent()
        if self.notes and not self.dry_run:
            try:
                self.tg.send("Calendar updates:\n" + "\n".join(self.notes))
            except TelegramError as e:
                log.warning("couldn't send calendar updates (%s): %s", e, " ".join(self.notes))
        return self.stats

    def _row_start(self, row):
        value = _dt(row["start"], self.tz)
        return value if isinstance(value, datetime) else datetime.combine(value, time(23, 59), self.tz)

    def _announce(self, c, new, bulk_after, instances=None):
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
            series = (instances or {}).get(key, [ev])
            snap = {"core": series_core(series, self.tz) if is_series else core(ev, False, self.tz),
                    "when": when_text(ev, self.tz), "repeats": repeats,
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
            tracked = []
            for r in rows:
                try:
                    if self.track(r, (instances or {}).get(r["event_key"])):
                        tracked.append(r)
                except Exception:  # noqa: BLE001 - one failed copy mustn't strand the rest
                    log.exception("couldn't copy %r into College; you'll be asked about it instead", r["title"])
                    if self.review_mode:
                        self.state.watch_set(r["id"], review="new")
            if self.review_mode and tracked and not any(self._urgent(r) for r in tracked):
                for r in tracked:
                    self.state.watch_set(r["id"], review="copied")
            elif tracked:
                titles = [r["title"] for r in tracked]
                self.notes.append(f"- Copied {len(titles)} new event(s) from {c['label']} into College: "
                                  + ", ".join(titles[:5]) + (" ..." if len(titles) > 5 else ""))
            return
        now_rows = []
        for group in group_rows(rows):
            if self.review_mode and not any(self._urgent(r) for r in group):
                for r in group:
                    self.state.watch_set(r["id"], review="new")  # asked about in the daily review
                self.stats["queued"] += 1
            else:
                now_rows += group
        try:
            if len(group_rows(now_rows)) > bulk_after:
                self._bulk_card(c, now_rows)
            else:
                for group in group_rows(now_rows):
                    self._send_card(group)
        except TelegramError as e:  # rows saved but never asked used to block planning time forever
            log.warning("couldn't send calendar cards (%s); they'll be asked about again", e)
            if self.review_mode:  # otherwise _requeue_unsent sends the cards again at the next check
                for r in now_rows:
                    if not self._fresh(r)["tg_message_id"]:
                        self.state.watch_set(r["id"], review="new")

    def _requeue_unsent(self):
        """Undecided events that never got a card (a send failed earlier) are asked about again."""
        if self.dry_run:
            return
        stranded = [r for r in self.state.watch_rows(statuses=["pending"])
                    if not r["tg_message_id"] and not r["batch"] and not r["review"]
                    and self.by_id.get(r["cal_id"], {}).get("policy") in ("ask", "copy")]
        if not stranded:
            return
        if self.review_mode:
            for r in stranded:
                self.state.watch_set(r["id"], review="new")
            return
        try:
            for group in group_rows(stranded):
                self._send_card(group)
        except TelegramError as e:
            log.warning("couldn't send calendar cards (%s); trying again at the next check", e)

    def _urgent(self, row):
        """Starts (or started) within the next `urgent_hours`: too soon to wait for tomorrow's review."""
        return self._row_start(self.state.watch_row(row["id"]) or row) < self.now + self.urgent

    def _changed(self, c, row, detail, old_style_note=None):
        """A decided event changed. Review mode: ask again (now if it's soon, else in the daily review)."""
        if not self.review_mode:
            if old_style_note:
                self.notes.append(old_style_note)
            return
        snap = json.loads(self.state.watch_row(row["id"])["snapshot"])
        snap["change"] = detail
        if self._urgent(row):
            fresh = self.state.watch_row(row["id"])
            try:
                message_id = self.tg.send(change_text(self._label(row["cal_id"]), fresh, detail), change_buttons(fresh))
            except TelegramError as e:
                log.warning("couldn't send a change card (%s); it goes to the review", e)
                self.state.watch_set(row["id"], snapshot=snap, review="changed")
                return
            # review_batch cleared: taps on this card must update this card, not an old review
            self.state.watch_set(row["id"], snapshot=snap, tg_message_id=message_id, batch=None, review=None,
                                 review_batch=None)
        else:
            self.state.watch_set(row["id"], snapshot=snap, review="changed")

    def _cancelled(self, c, row, detail):
        if self.review_mode and not self._urgent(row):
            snap = json.loads(self._fresh(row)["snapshot"])
            snap["change"] = detail
            self.state.watch_set(row["id"], snapshot=snap, review="cancelled")
        else:
            self.notes.append(f"- {row['title']} was cancelled on {c['label']}; {detail}.")

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

    def _sync(self, c, row, ev, instances=None):
        """The original changed? Update our copy (or deadline) and tell you."""
        snap = json.loads(row["snapshot"])
        if snap.pop("missing", None):  # back after one scan without it: a feed hiccup, not a cancellation
            self.state.watch_set(row["id"], snapshot=snap)
        instances = instances or [ev]
        if row["is_series"] and row["status"] == "tracked" and not self.dry_run:
            self._sync_copies(row, instances)
        new_core = series_core(instances, self.tz) if row["is_series"] else core(ev, False, self.tz)
        new_start = _start_value(ev)
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
        elif row["status"] == "tracked" and (row["copy_id"] or row["is_series"]):
            if not row["is_series"]:
                try:
                    google_writer.update_copy(self.cal, self.college, row["copy_id"], ev)
                except HttpError as e:
                    if e.resp.status not in (404, 410):
                        raise
                    self.state.watch_set(row["id"], status="ignored", copy_id=None, decided_at=self.now.isoformat())
                    self.notes.append(f"- {title} ({c['label']}) changed, but you had deleted its College copy, "
                                      "so I stopped tracking it.")
                    return
            self._changed(c, row, f"now {snap['when']}; your College copy was updated",
                          f"- {title} ({c['label']}) changed: now {snap['when']}. The College copy was updated.")
        elif row["status"] == "deadline" and row["copy_id"]:
            due = due_of(ev, self.tz)
            google_writer.move_deadline(self.cal, self.tasks, self.cfg, row["copy_id"],
                                        self.state.task_for_event(row["copy_id"]), due)
            self._changed(c, row, f"now due {due:%a %d %b %H:%M}; your deadline and task moved too",
                          f"- Deadline {title} ({c['label']}) moved to {due:%a %d %b %H:%M}. Your deadline and task moved too.")
        elif row["status"] == "ignored":
            self._changed(c, row, f"now {snap['when']}")  # you ignored it before; maybe it suits you now
        elif row["status"] == "pending" and row["tg_message_id"] and not row["batch"]:
            self.tg.edit(row["tg_message_id"], card_text(c["label"], row), card_buttons(row, snap.get("deadline_like")))

    def _sync_copies(self, row, instances):
        """A tracked repeating event is copied occurrence by occurrence for the next `days_ahead` days, so a
        cancelled or moved class shows up in College exactly (one repeating copy of the series couldn't)."""
        label, title = self._label(row["cal_id"]), row["title"]
        if row["copy_id"]:  # made by an older version: one repeating copy for the whole series; end it now
            google_writer.end_series_copy(self.cal, self.college, row["copy_id"], self.now)
            self.state.watch_set(row["id"], copy_id=None)
        existing = {r["instance_key"]: r for r in self.state.series_copies(row["id"])}
        seen = set()
        for ev in instances:
            key, snap = ev["id"], core(ev, False, self.tz)
            seen.add(key)
            cur = existing.get(key)
            if cur is None:
                if self._started(ev):
                    continue
                copy_id = google_writer.create_copy(self.cal, self.college, ev, label,
                                                    f"{row['cal_id']}|{row['event_key']}|{key}")
                self.state.series_copy_set(row["id"], key, copy_id, _start_value(ev), snap)
                continue
            if json.loads(cur["snapshot"]) != snap and cur["copy_id"]:
                try:
                    google_writer.update_copy(self.cal, self.college, cur["copy_id"], ev)
                    self.notes.append(f"- {title} ({label}): the {when_text(ev, self.tz)} class moved; College updated.")
                except HttpError as e:
                    if e.resp.status not in (404, 410):
                        raise
                    cur["copy_id"] = ""  # you deleted this one copy: leave it deleted
            if json.loads(cur["snapshot"]) != snap or cur["missing"]:
                self.state.series_copy_set(row["id"], key, cur["copy_id"], _start_value(ev), snap)
        for key, cur in existing.items():
            if key in seen:
                continue
            start = self._row_start({"start": cur["start"]})
            if start <= self.now:  # already happened: the copy stays in College as history
                if start < self.now - timedelta(days=1):
                    self.state.series_copy_delete(row["id"], key)
                continue
            if cur["missing"] + 1 < 2:  # wait for a second scan: a feed can come back empty once
                self.state.series_copy_missing(row["id"], key, cur["missing"] + 1)
                continue
            if cur["copy_id"]:
                google_writer.delete_event(self.cal, self.college, cur["copy_id"])
            self.state.series_copy_delete(row["id"], key)
            self.notes.append(f"- {title} ({label}): the {start:%a %d %b %H:%M} one was cancelled; removed from College.")

    def _check_gone(self, c, row):
        ev = self._get(c["id"], row["event_key"])
        if ev is not None and ev.get("status") != "cancelled":
            snap = json.loads(row["snapshot"])
            if snap.pop("missing", None):
                self.state.watch_set(row["id"], snapshot=snap)
            if not row["is_series"]:
                self._sync(c, row, ev)  # still there, just moved outside the next two weeks
            elif row["status"] == "pending" and series_ended(ev, self.now):
                if not self.dry_run and self.state.watch_set_if(row["id"], "pending", status="expired",
                                                                decided_at=self.now.isoformat(), review=None):
                    self._edit_card_after_removal(row, "Its last occurrence has passed; nothing was added.")
            elif row["status"] == "tracked" and not self.dry_run:
                self._sync_copies(row, [])  # no occurrence in the window any more: upcoming copies go
            return
        snap = json.loads(row["snapshot"])
        misses = snap.get("missing", 0) + 1
        if misses < 2:
            if not self.dry_run:
                self.state.watch_set(row["id"], snapshot={**snap, "missing": misses})
            return  # a subscribed feed can come back empty for one refresh; wait for a second scan
        title = row["title"]
        self.stats["gone"] += 1
        if self.dry_run:
            print(f"WOULD NOTE CANCELLED ({c['label']}): {title}", flush=True)
            return
        snap.pop("missing", None)
        snap["before_gone"] = row["status"]  # so it can be put back if it reappears
        if not self.state.watch_set_if(row["id"], row["status"], status="gone", snapshot=snap,
                                       decided_at=self.now.isoformat(), review=None):
            return  # decided on a card while this scan ran
        if row["status"] == "tracked":
            self._remove_copies(row)
            self._cancelled(c, row, "its copy was removed from College")
        elif row["status"] == "deadline":
            self._cancelled(c, row, "your deadline is still there; delete it if the deadline was dropped")
        elif row["status"] == "pending":
            self._edit_card_after_removal(row, f"Cancelled on {c['label']}.")

    def _revive(self, c, row, ev):
        """An event marked cancelled is back (e.g. the feed was briefly empty): put it back as it was."""
        snap = json.loads(row["snapshot"])
        before = snap.pop("before_gone", "pending")
        snap.pop("missing", None)
        if self.dry_run:
            print(f"WOULD RESTORE ({c['label']}): {row['title']}", flush=True)
            return
        status = "pending" if before in ("tracked", "pending", "linked") else before
        self.state.watch_set(row["id"], status=status, snapshot=snap, start=_start_value(ev), decided_at=None,
                             review="new" if status == "pending" and before != "tracked" and self.review_mode else None)
        self.stats["back"] += 1
        if before == "tracked" and self.track(self.state.watch_row(row["id"])):
            self.notes.append(f"- {row['title']} is back on {c['label']}; copied into College again.")

    def _edit_card_after_removal(self, row, text):
        if not row["tg_message_id"] or row["batch"] or self.dry_run:
            return
        others = [r for r in self.state.watch_rows(message_id=row["tg_message_id"], statuses=["pending"])
                  if r["id"] != row["id"]]
        if others:  # the course's other weekly slot is still waiting: keep its buttons
            self.tg.edit(row["tg_message_id"], card_text(self._label(row["cal_id"]), others),
                         card_buttons(others[0], json.loads(others[0]["snapshot"]).get("deadline_like")))
        else:
            self.tg.edit(row["tg_message_id"], card_text(self._label(row["cal_id"]), row) + f"\n\n{text}")

    def _expire(self):
        for row in self.state.watch_rows(statuses=["pending"]):
            if row["is_series"]:
                continue  # a series only expires once its last occurrence has passed (_check_gone)
            if self._row_start(row) < self.now:
                if self.dry_run or not self.state.watch_set_if(row["id"], "pending", status="expired",
                                                               decided_at=self.now.isoformat(), review=None):
                    continue
                if row["tg_message_id"] and not row["batch"]:
                    self._edit_card_after_removal(row, "The event has passed; nothing was added.")

    def _get(self, cal_id, event_id):
        try:
            return self.cal.events().get(calendarId=cal_id, eventId=event_id).execute()
        except HttpError as e:
            if e.resp.status in (404, 410):
                return None
            raise

    # --- decisions (buttons) ------------------------------------------------------------------------
    # Each re-reads the row first, so a second tap (or a tap on an old card) never makes a second copy,
    # and switching between Track / Deadline / Ignore removes whatever the previous choice created.

    def _remove_copies(self, row):
        """Deletes a tracked row's College copy, or a series' upcoming copies (past ones stay as history)."""
        if row["copy_id"]:
            if row["is_series"]:
                google_writer.end_series_copy(self.cal, self.college, row["copy_id"], self.now)
            else:
                google_writer.delete_event(self.cal, self.college, row["copy_id"])
        for cp in self.state.series_copies(row["id"]):
            if cp["copy_id"] and self._row_start({"start": cp["start"]}) > self.now:
                google_writer.delete_event(self.cal, self.college, cp["copy_id"])
            self.state.series_copy_delete(row["id"], cp["instance_key"])

    def track(self, row, instances=None):
        """Copies the event (or a series' upcoming occurrences) into College. Returns False if it no longer exists."""
        row = self._fresh(row)
        if row["status"] == "tracked":
            return True
        source = self._get(row["cal_id"], row["event_key"])
        if source is None or source.get("status") == "cancelled":
            self.state.watch_set(row["id"], status="gone", decided_at=self.now.isoformat())
            return False
        if row["status"] == "deadline" and row["copy_id"]:
            _remove_deadline(self, row)
        if row["is_series"]:
            self.state.watch_set(row["id"], status="tracked", copy_id=None, decided_at=self.now.isoformat())
            if instances is None:
                instances = google_writer.list_instances(self.cal, row["cal_id"], row["event_key"], self.now, self.horizon)
            self._sync_copies(self.state.watch_row(row["id"]), instances)
        else:
            copy_id = google_writer.create_copy(self.cal, self.college, source, self._label(row["cal_id"]),
                                                f"{row['cal_id']}|{row['event_key']}")
            self.state.watch_set(row["id"], status="tracked", copy_id=copy_id, decided_at=self.now.isoformat())
        self.stats["tracked"] += 1
        return True

    def ignore(self, row):
        """Ignore it; its College copy, or a deadline made from it, is removed."""
        row = self._fresh(row)
        if row["status"] == "tracked":
            self._remove_copies(row)
        elif row["status"] == "deadline" and row["copy_id"]:
            _remove_deadline(self, row)
        self.state.watch_set(row["id"], status="ignored", copy_id=None, decided_at=self.now.isoformat())

    def as_deadline(self, row):
        """Makes it a deadline (DUE event + task), like an Added deadline card from an email."""
        row = self._fresh(row)
        if row["status"] == "deadline" and row["copy_id"]:
            return row["copy_id"]
        ev = self._get(row["cal_id"], row["event_key"])
        if ev is None or ev.get("status") == "cancelled":
            return None
        if row["status"] == "tracked":
            self._remove_copies(row)
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
    for n in {r["review_batch"] for r in rows if r.get("review_batch")}:
        _refresh_review(w, n)
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


def _remove_deadline(w, row):
    """Deletes a deadline made from a calendar event: its DUE event, its task, and our record of it."""
    task_id = w.state.task_for_event(row["copy_id"])
    google_writer.delete_event(w.cal, w.college, row["copy_id"])
    if task_id:
        try:
            w.tasks.tasks().delete(tasklist=w.cfg.get("tasklist", "@default"), task=task_id).execute()
        except HttpError as e:
            if e.resp.status not in (404, 410):
                raise
    w.state.delete_item_by_event(row["copy_id"])


def _save_undo(state, record):
    """record: {cal_id, policy (previous override or None), rows: [[id, status, copy_id, review]], after: the status
    the press gave the rows, policy_after: the override it set}. Undo only restores what is still that way."""
    n = int(state.get_meta("calundo_seq") or 0) + 1
    state.set_meta("calundo_seq", str(n))
    state.set_meta(f"calundo:{n}", json.dumps(record))
    return n


def _undo(w, n):
    """Puts rows (and the calendar's setting) back as they were before one button press, removing any
    copies or deadlines that press created. Rows you've decided differently since are left alone.
    Returns the restored row ids, or None if already undone."""
    raw = w.state.get_meta(f"calundo:{n}")
    if not raw:
        return None
    rec = json.loads(raw)
    if rec.get("policy") is not None:
        key = w.by_id[rec["cal_id"]]["key"] if rec["cal_id"] in w.by_id else rec["cal_id"]
        if "policy_after" not in rec or (w.state.get_meta(f"calpolicy:{key}") or "") == rec["policy_after"]:
            w.state.set_meta(f"calpolicy:{key}", rec["policy"])  # "" = back to what config.yaml says
    restored = []
    for entry in rec["rows"]:
        row_id, status, copy_id = entry[:3]
        row = w.state.watch_row(row_id)
        if rec.get("after") and row["status"] != rec["after"]:
            continue  # changed since (e.g. you tapped Track now on it later): leave your newer choice
        if row["status"] == "tracked" and (status != "tracked" or row["copy_id"] != copy_id):
            w._remove_copies(row)  # the copies (or a series' occurrence copies) this press made
        elif row["status"] == "deadline" and row["copy_id"]:
            _remove_deadline(w, row)
        w.state.watch_set(row_id, status="pending" if status == "tracked" else status,
                          copy_id=None if status == "tracked" else copy_id, decided_at=None,
                          **({"review": entry[3]} if len(entry) > 3 else {}))
        if status == "tracked":
            w.track(w.state.watch_row(row_id))
        restored.append(row_id)
    w.state.set_meta(f"calundo:{n}", "")  # one Undo per press
    return restored


def _snapshot_rows(rows):
    return [[r["id"], r["status"], r["copy_id"], r.get("review")] for r in rows]


def handle_callback(listener, cq, action, rest, now):
    """cal:<op>:<row>  one event        calb:<op>:<batch>  summary card     cale:<row>:<hours>  deadline effort
    calc:<op>:<ref>  confirm/cancel a calendar-wide change (ref = r<row> or b<batch>)
    calu:<n>         undo                                  calp:<index>  /calendars menu
    The listener has already answered the tap; results show by editing the message."""
    w = Watcher(listener.cfg, listener.state, listener.calendar, listener.tasks, listener.tg, now)
    tg, state, message_id = listener.tg, listener.state, cq["message"]["message_id"]
    log.info("calendar button %s:%s", action, rest)

    if action == "calp":
        return _cycle_policy(w, cq, rest)

    if action == "calu":
        restored = _undo(w, int(rest)) if rest.isdigit() else None
        if restored is None:
            return  # a second tap: the message already shows the restored state; don't wipe it
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
                                  "policy_after": policy, "after": "tracked" if policy == "copy" else "ignored",
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
        undo = _save_undo(state, {"cal_id": rows[0]["cal_id"], "policy": None,
                                  "after": "tracked" if op == "t" else "ignored", "rows": _snapshot_rows(rows)})
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
        if not row or not row["copy_id"] or row["status"] != "deadline":
            return
        state.set_effort(f"event:{row['copy_id']}", float(hours))
        tg.edit(message_id, _decided_text(w, row, f"Added as a deadline. Work needed: {float(hours):g} h"))
        return

    op, _, raw_id = rest.partition(":")
    row = state.watch_row(int(raw_id)) if raw_id.isdigit() else None
    if row is None:
        return
    on_card = _card_rows(state, row, message_id)
    if op == "t":
        group = [r for r in on_card if r["status"] in ("pending", "ignored", "deadline")]
        if not group and row["status"] == "tracked":  # a second tap: nothing more to copy
            group, ok = [r for r in on_card if r["status"] == "tracked"], [True]
        else:
            ok = [w.track(r) for r in group]
        tg.edit(message_id, _decided_text(w, group or [row], "Tracked: copied into College." if any(ok) else
                                          "That event no longer exists; nothing was added."),
                [[("Untrack", f"cal:u:{row['id']}")]] if any(ok) else None)
    elif op in ("i", "u"):
        group = [r for r in on_card if r["status"] in (("tracked",) if op == "u" else ("pending", "tracked", "deadline"))]
        for r in group:
            w.ignore(r)
        tg.edit(message_id, _decided_text(w, group or [row], "Untracked: the College copy was removed." if op == "u"
                                          else "Ignored."),
                [[("Track instead", f"cal:t:{row['id']}")]])
    elif op == "d":
        if row["status"] == "deadline":  # a second tap
            return
        undo = _save_undo(state, {"cal_id": row["cal_id"], "policy": None, "after": "deadline",
                                  "rows": _snapshot_rows([row])})
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


# --- the daily calendar review ---------------------------------------------------------------------------

REVIEW_BUTTONS = 8  # items with their own buttons in one review message; the rest via "One by one"
OUTCOME = {"tracked": "tracked", "ignored": "ignored", "deadline": "deadline", "gone": "cancelled",
           "expired": "passed", "pending": "waiting - card below"}
SECTION = {"new": "New", "changed": "Changed", "cancelled": "Cancelled", "copied": "Copied automatically"}


def daily_review(cfg, state, cal, tasks_api, tg, now, wait_s=180):
    """One message with everything new, changed or cancelled on your calendars since the last review, with
    Track / Ignore (or Keep / Remove) per item. Returns True if there was anything to send. Raises WatchBusy
    if another calendar scan kept running (the caller tries again later)."""
    with watch_lock(wait_s):
        return _daily_review(cfg, state, cal, tasks_api, tg, now)


def _daily_review(cfg, state, cal, tasks_api, tg, now):
    w = Watcher(cfg, state, cal, tasks_api, tg, now)
    w.run()  # a fresh look first; non-urgent findings are only queued by it
    waiting = state.watch_rows(review=True)
    items = [["new", [r["id"] for r in g]]
             for g in group_rows([r for r in waiting if r["review"] == "new" and r["status"] == "pending"])]
    items += [["changed", [r["id"] for r in g]] for g in group_rows(
        [r for r in waiting if r["review"] == "changed" and r["status"] in ("tracked", "ignored", "deadline")])]
    items += [[r["review"], [r["id"]]] for r in waiting if r["review"] in ("cancelled", "copied")]
    for r in waiting:  # flags that no longer apply (e.g. decided on a card in between)
        if not any(r["id"] in ids for _, ids in items):
            state.watch_set(r["id"], review=None)
    if not items:
        return False
    n = int(state.get_meta("review_seq") or 0) + 1
    record = {"items": items, "date": now.date().isoformat()}
    text, buttons = render_review(w, n, record)
    # Send first: nothing is marked until the message is out, so a failed send loses no news and is retried.
    record["message_id"] = tg.send(text, buttons)
    state.set_meta("review_seq", str(n))
    state.set_meta(f"review:{n}", json.dumps(record))
    for kind, ids in items:
        for i in ids:  # cancelled / copied are just news: shown once
            state.watch_set(i, review_batch=n, **({"review": None} if kind in ("cancelled", "copied") else {}))
    previous = state.get_meta("review_last")
    old = json.loads(state.get_meta(f"review:{previous}") or "{}") if previous else {}
    still_open = any(_item_open(k, state.watch_row(ids[0])) for k, ids in old.get("items", []) if k in ("new", "changed"))
    if old.get("message_id") and still_open:  # its open items moved to the new review (a deleted one is fine)
        tg.edit(old["message_id"], "This review was replaced by a newer one below.")
    state.set_meta("review_last", str(n))
    return True


def _item_open(kind, row):
    return (kind == "new" and row["status"] == "pending" and row["review"] == "new") or \
           (kind == "changed" and row["review"] == "changed")


def render_review(w, n, record):
    lines, buttons, number, open_new, open_total = [f"Calendar review - {date.fromisoformat(record['date']):%a %d %b}"], [], 0, [], 0
    for kind in ("new", "changed", "cancelled", "copied"):
        entries = [ids for k, ids in record["items"] if k == kind]
        if not entries:
            continue
        lines.append(f"\n{SECTION[kind]}")
        for ids in entries:
            rows = [w.state.watch_row(i) for i in ids]
            r0, snap = rows[0], json.loads(rows[0]["snapshot"])
            when = group_repeats(rows) if r0["is_series"] else snap.get("when", "")
            title, where = clean_text(r0["title"], 70), w._label(r0["cal_id"])
            if kind in ("cancelled", "copied"):
                lines.append(f"- {title} ({where}): {snap.get('change', 'copied into College') if kind == 'cancelled' else when}")
                continue
            number += 1
            is_open = _item_open(kind, r0)
            detail = f"; {snap['change']}" if kind == "changed" and snap.get("change") else ""
            lines.append(f"{number}. {title} ({where}): {when if kind == 'new' else ''}{detail.lstrip('; ') if kind == 'changed' else ''}"
                         + ("" if is_open else f"  [{OUTCOME.get(r0['status'], r0['status'])}]"))
            if not is_open:
                continue
            open_total += 1
            if kind == "new":
                open_new.append(r0)
            if len(buttons) < REVIEW_BUTTONS:
                if kind == "new":
                    row_buttons = [(f"{number} Track", f"crv:t:{r0['id']}"), (f"{number} Ignore", f"crv:i:{r0['id']}")]
                    if snap.get("deadline_like"):
                        row_buttons.append((f"{number} Deadline", f"crv:d:{r0['id']}"))
                    buttons.append(row_buttons)
                else:
                    buttons += change_buttons(r0, prefix=f"{number} ")
    if len(open_new) >= 2:
        buttons.append([("Track all new", f"crva:t:{n}"), ("Ignore all new", f"crva:i:{n}")])
    if open_total > REVIEW_BUTTONS:
        buttons.append([("One by one", f"crva:o:{n}")])
    if record.get("undo") and w.state.get_meta(f"calundo:{record['undo']}"):
        buttons.append([("Undo", f"calu:{record['undo']}")])
    if not open_total:
        lines.append("\nAll done.")
    return "\n".join(lines), buttons or None


def _refresh_review(w, n):
    record = json.loads(w.state.get_meta(f"review:{n}") or "{}")
    if record.get("message_id"):
        w.tg.edit(record["message_id"], *render_review(w, n, record))


def _review_item(state, row):
    """The rows shown together with `row` in its review (e.g. both weekly slots of a course)."""
    record = json.loads(state.get_meta(f"review:{row['review_batch']}") or "{}") if row.get("review_batch") else {}
    for _, ids in record.get("items", []):
        if row["id"] in ids:
            return [state.watch_row(i) for i in ids]
    return siblings(state, row, ["pending", "ignored", "tracked", "deadline"])


def handle_review(listener, cq, action, rest, now):
    """crv:<op>:<row>  one review item or change card: t track, i ignore, k keep, r remove, d deadline
    crva:<op>:<n>    whole review: t track all new, i ignore all new, o one by one"""
    w = Watcher(listener.cfg, listener.state, listener.calendar, listener.tasks, listener.tg, now)
    tg, state = listener.tg, listener.state
    log.info("review button %s:%s", action, rest)
    op, _, raw = rest.partition(":")
    if action == "crva":
        n = int(raw) if raw.isdigit() else 0
        record = json.loads(state.get_meta(f"review:{n}") or "{}")
        rows = [r for r in state.watch_rows(review="new", review_batch=n) if r["status"] == "pending"]
        if not record or not rows:
            return
        if op == "o":
            for g in group_rows(rows)[:MAX_ONE_BY_ONE]:
                w._send_card(g)  # sent first: if it fails, the item is still in the review
                for r in g:
                    state.watch_set(r["id"], review=None)
        else:
            record["undo"] = _save_undo(state, {"cal_id": rows[0]["cal_id"], "policy": None,
                                                "after": "tracked" if op == "t" else "ignored",
                                                "rows": _snapshot_rows(rows)})
            state.set_meta(f"review:{n}", json.dumps(record))
            for r in rows:
                w.track(r) if op == "t" else w.ignore(r)
                state.watch_set(r["id"], review=None)
        _refresh_review(w, n)
        return

    row = state.watch_row(int(raw)) if raw.isdigit() else None
    if row is None:
        return
    group = _review_item(state, row)
    if op == "d":
        if row["status"] == "deadline":
            return  # a second tap
        if w.as_deadline(row) is not None:
            fresh = state.watch_row(row["id"])
            choices = [(f"{h:g} h", f"cale:{row['id']}:{h}") for h in listener.cfg["planner"]["effort_choices_hours"]]
            tg.send(f"Added {clean_text(row['title'], 100)} as a deadline (calendar event + task). How much work does it need?",
                    [choices[:3], choices[3:]])
            group = [fresh]
    for r in group:
        r = state.watch_row(r["id"])
        if op == "t" and r["status"] in ("pending", "ignored"):
            w.track(r)
        elif op == "i" and r["status"] in ("pending", "tracked"):
            w.ignore(r)
        elif op == "r":
            if r["status"] == "tracked":
                w.ignore(r)
            elif r["status"] == "deadline":
                _remove_deadline(w, r)
                state.watch_set(r["id"], status="ignored", copy_id=None)
        state.watch_set(r["id"], review=None)  # "k" (keep) only clears the question
    if row.get("review_batch"):
        _refresh_review(w, row["review_batch"])
    else:  # a change card sent right away (the event was less than a day off)
        fresh = state.watch_row(row["id"])
        outcome = {"t": "Tracked: copied into College.", "i": "Ignored.", "k": "Kept as it is.",
                   "r": "Removed.", "d": "Added as a deadline."}.get(op, "")
        tg.edit(cq["message"]["message_id"], change_text(w._label(row["cal_id"]), fresh, "") .split("\n")[0]
                + f"\n\n{outcome}")


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
    message_id = cq["message"]["message_id"]
    if not raw_index.isdigit() or int(raw_index) >= len(ids) or ids[int(raw_index)] not in w.by_id:
        w.tg.edit(message_id, "This menu is out of date; send /calendars again.")
        return
    c = w.by_id[ids[int(raw_index)]]
    new = POLICIES[(POLICIES.index(c["policy"]) + 1) % len(POLICIES)] if c["policy"] in POLICIES else "ask"
    w.set_policy(c["id"], new)
    menu = [w.by_id[i] for i in ids if i in w.by_id]
    w.tg.edit(message_id, _menu_text(menu), _menu_buttons(menu))


