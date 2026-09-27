"""An email (or invite) that moves or cancels something you already have, instead of adding a duplicate.

  "SDET midsem postponed to Friday"  -> Changed? Was Wed 30 Sep 15:00, now Fri 02 Oct 10:00   [Move it] [Add as new] [Ignore]
  "Tomorrow's quiz is cancelled"      -> Cancelled? SDET quiz (Thu 01 Oct 10:00)                [Remove it] [Keep it]

Matching is by title: close enough (difflib), and any numbers must be equal, so "Assignment 2" is never taken
for "Assignment 3". An updated or cancelled .ics invite is matched by its UID instead. Nothing changes until you tap.
"""
import difflib
import logging
import re
from datetime import datetime

from googleapiclient.errors import HttpError

import actions
import google_writer
from telegram_bot import TelegramError

log = logging.getLogger("changes")
CANCEL_RE = re.compile(r"\b(cancel+ed|cancel+ation|called off|will not be (held|conducted)|won'?t be (held|conducted)"
                       r"|stands cancel+ed|postponed indefinitely|not be taking place)\b", re.IGNORECASE)
SIMILAR = 0.8
STOP = {"the", "a", "an", "of", "for", "and", "on", "in", "to", "due", "class", "lecture", "session", "meeting", "exam"}


def _norm(title):
    return re.sub(r"[^a-z0-9]+", " ", (title or "").lower()).strip()


def _numbers(title):
    return set(re.findall(r"\d+", title or ""))


def same_thing(a, b):
    """Two titles for the same item? ("SDET midsem" / "SDET Midsem exam": yes; "DSA Assignment 2" / "... 3": no)."""
    na, nb = _norm(a), _norm(b)
    if not na or not nb or _numbers(na) != _numbers(nb):
        return False
    return na == nb or difflib.SequenceMatcher(None, na, nb).ratio() >= SIMILAR or (
        len(na.split()) >= 2 and len(nb.split()) >= 2 and (na in nb or nb in na))


def _kind_group(kind):
    return "deadline" if kind == "deadline" else "event"


def _when(item):
    return item.get("due") or item["start"]


def similar(state, item, now):
    """An existing item (made from an earlier email) this one looks like, at a different time; else None."""
    when = _when(item)
    for old in state.upcoming_items(now):
        if _kind_group(old["kind"]) != _kind_group(item["type"]) or not same_thing(old["title"], item["title"]):
            continue
        if not isinstance(when, datetime) or abs((old["when"] - when).total_seconds()) >= 60:
            return old
    return None


def same_or_similar(state, item, now):
    """For a cancellation: the existing item this names, whether or not the time matches."""
    for old in state.upcoming_items(now):
        if same_thing(old["title"], item["title"]):
            return old
    return None


def mentioned(state, text, now):
    """Existing items a cancellation email names without the model finding an item: every significant word of the
    title (at least two) appears in the email, numbers included."""
    words = set(_norm(text).split())
    found = []
    for old in state.upcoming_items(now):
        title_words = [w for w in _norm(old["title"]).split() if w not in STOP and len(w) > 1]
        if len(title_words) >= 2 and all(w in words for w in title_words):
            found.append(old)
    return found


def _stamp(when):
    return f"{when:%a %d %b %H:%M}" if isinstance(when, datetime) else f"{when:%a %d %b} (all day)"


def ask_change(tg, state, key, item, msg, sender, old):
    item = {**item, "replaces": {"event_id": old["event_id"], "task_id": old.get("task_id"), "kind": old["kind"],
                                 "title": old["title"], "when": old["when"].isoformat()}}
    pending_id = state.add_pending(key, item, msg, None)  # sender left out: these don't count as skips of a sender
    text = (f"Changed? {item['title']}\nWas: {_stamp(old['when'])}\nNow: {_stamp(_when(item))}\n"
            f"From: {sender}\nEmail: {msg['subject']}")
    try:
        message_id = tg.send(text, [("Move it", f"mv:{pending_id}"), ("Add as new", f"add:{pending_id}"),
                                    ("Ignore", f"skip:{pending_id}")])
    except TelegramError:
        state.delete_pending(pending_id)
        raise
    state.set_pending_message(pending_id, message_id)


def ask_cancel(tg, state, old, msg, sender):
    key = f"cancel|{old['event_id']}"
    if state.pending_exists(key):
        return False
    when = old["when"]
    item = {"type": old["kind"] if old["kind"] in ("deadline", "meeting", "event") else "event", "title": old["title"],
            "start": when, "end": when, "all_day": False, "due": when if old["kind"] == "deadline" else None,
            "course": None, "location": None, "description": None, "recurrence": None,
            "cancels": {"event_id": old["event_id"], "task_id": old.get("task_id")}}
    pending_id = state.add_pending(key, item, msg, None)
    text = (f"Cancelled? {old['title']} ({_stamp(when)})\nThe email says it's off.\nFrom: {sender}\nEmail: {msg['subject']}")
    try:
        message_id = tg.send(text, [("Remove it", f"cx:{pending_id}"), ("Keep it", f"skip:{pending_id}")])
    except TelegramError:
        state.delete_pending(pending_id)
        raise
    state.set_pending_message(pending_id, message_id)
    return True


def event_as_old(state, ev, tz):
    """An existing College event (found by invite UID) in the form similar() returns."""
    known = state.item_by_event(ev["id"]) or {}
    start = ev["start"].get("dateTime") or ev["start"].get("date")
    when = datetime.fromisoformat(start)
    when = when.astimezone(tz) if when.tzinfo else when.replace(tzinfo=tz)
    return {"event_id": ev["id"], "task_id": known.get("task_id"), "kind": known.get("kind", "event"),
            "title": known.get("title") or ev.get("summary", ""), "when": when, "dedupe_key": known.get("dedupe_key")}


def handle(listener, row, action, now):
    """mv:<pending id> (move the existing one), cx:<pending id> (remove the cancelled one)."""
    cfg, state, tg, cal = listener.cfg, listener.state, listener.tg, listener.calendar
    if row["status"] != "pending":
        return
    item = row["item"]
    if action == "mv" and item.get("replaces"):
        old = item["replaces"]
        if old["kind"] == "deadline":
            google_writer.move_deadline(cal, listener.tasks, cfg, old["event_id"], old.get("task_id"), item["due"])
        else:
            google_writer.move_item_event(cal, cfg["calendars"]["college"], old["event_id"], item, cfg["timezone"])
        state.delete_item_by_event(old["event_id"])
        state.record_item(row["dedupe_key"], old["kind"], old["event_id"], old.get("task_id"), item["title"],
                          str(_when(item)), row["msg_id"])
        state.set_pending_status(row["id"], "moved")
        tg.edit(row["tg_message_id"], f"Moved: {item['title']} is now {_stamp(_when(item))}"
                + (" (event and task)." if old.get("task_id") else "."))
        log.info("moved %r to %s", item["title"], _when(item))
    elif action == "cx" and item.get("cancels"):
        gone = item["cancels"]
        actions._remove_future_work(cfg, state, cal, f"event:{gone['event_id']}", now)
        google_writer.delete_event(cal, cfg["calendars"]["college"], gone["event_id"])
        if gone.get("task_id"):
            try:
                listener.tasks.tasks().delete(tasklist=cfg.get("tasklist", "@default"), task=gone["task_id"]).execute()
            except HttpError as e:
                if e.resp.status not in (404, 410):
                    raise
        state.delete_item_by_event(gone["event_id"])
        state.set_pending_status(row["id"], "cancelled")
        tg.edit(row["tg_message_id"], f"Removed: {item['title']} (cancelled).")
        log.info("removed cancelled %r", item["title"])
