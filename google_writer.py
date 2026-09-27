"""Creates Google Calendar events and Google Tasks from items."""
from datetime import datetime, timedelta, timezone

SOURCE_TAG = "calendar-agent"
DUE_PREFIX = "DUE: "  # deadline events are titled "DUE: <title>"; the digest and planner find them by this
PLANNER_TAG = "calendar-agent-planner"  # habits and work blocks; see list_blocks()


def _when(value, all_day, tz_name):
    if all_day:
        return {"date": value.isoformat()}
    return {"dateTime": value.isoformat(), "timeZone": tz_name}


def _gmail_link(msg_id):
    return f"https://mail.google.com/mail/u/0/#all/{msg_id}"


def event_title(item):
    return f"{DUE_PREFIX}{item['title']}" if item["type"] == "deadline" else item["title"]


def create_event(calendar, calendar_id, item, msg, tz_name):
    lines = [f"Course: {item['course']}"] if item.get("course") else []
    if item.get("description"):
        lines.append(item["description"])
    lines += ([f"From email: {msg['subject']}", _gmail_link(msg["id"])] if msg.get("id") else [f"From: {msg['subject']}"])
    lines.append(f"(created by {SOURCE_TAG})")

    body = {
        "summary": event_title(item),
        "description": "\n".join(lines),
        "start": _when(item["start"], item["all_day"], tz_name),
        "end": _when(item["end"], item["all_day"], tz_name),
        # Lets us find (or wipe) everything this agent created.
        "extendedProperties": {"private": {"source": SOURCE_TAG, "gmail_id": msg.get("id") or "",
                                           **({"ics_uid": item["uid"][:900]} if item.get("uid") else {})}},
    }
    if item["type"] == "deadline":
        body["transparency"] = "transparent"  # the 30-min DUE marker isn't busy time
    if item.get("location"):
        body["location"] = item["location"]
    if item.get("recurrence"):
        body["recurrence"] = item["recurrence"]
    return calendar.events().insert(calendarId=calendar_id, body=body).execute()["id"]


def create_task(tasks, tasklist, item, msg):
    due = item["due"]
    notes = [f"Due {due:%a %d %b %Y, %H:%M} IST"]
    if item.get("course"):
        notes.append(f"Course: {item['course']}")
    notes += [f"From email: {msg['subject']}", _gmail_link(msg["id"])] if msg.get("id") else [f"From: {msg['subject']}"]
    body = {
        "title": item["title"],
        "notes": "\n".join(notes),
        # Tasks API stores only the date part of `due`.
        "due": f"{due.date().isoformat()}T00:00:00.000Z",
    }
    return tasks.tasks().insert(tasklist=tasklist, body=body).execute()["id"]


def create_item(calendar, tasks, cfg, item, msg):
    """Event on the college calendar, plus a task for deadlines. Returns (event_id, task_id).
    If the task can't be created, the event is removed again so a retry doesn't leave a duplicate."""
    event_id = create_event(calendar, cfg["calendars"]["college"], item, msg, cfg["timezone"])
    if item["type"] != "deadline":
        return event_id, None
    try:
        task_id = create_task(tasks, cfg.get("tasklist", "@default"), item, msg)
    except Exception:
        delete_event(calendar, cfg["calendars"]["college"], event_id)
        raise
    return event_id, task_id



def create_block(calendar, calendar_id, title, start, end, tz_name, kind, work_key=None, note=None):
    """A planner-made habit or work block, tagged so it can be found per day and per deadline."""
    private = {"source": PLANNER_TAG, "plan_date": start.date().isoformat(), "kind": kind}
    if work_key:
        private["work_key"] = work_key
        if work_key.startswith("event:"):
            private["deadline_event_id"] = work_key[len("event:"):]  # what digest.has_work_block() looks for
    body = {
        "summary": title,
        "description": ((note + "\n") if note else "") + f"(planned by {SOURCE_TAG}; send /clear in Telegram to remove today's plan)",
        "start": {"dateTime": start.isoformat(), "timeZone": tz_name},
        "end": {"dateTime": end.isoformat(), "timeZone": tz_name},
        "extendedProperties": {"private": private},
        "reminders": {"useDefault": False},
    }
    return calendar.events().insert(calendarId=calendar_id, body=body).execute()["id"]


def list_blocks(calendar, calendar_id, time_min, time_max):
    """Planner-made blocks overlapping [time_min, time_max)."""
    events, token = [], None
    while True:
        resp = calendar.events().list(calendarId=calendar_id, timeMin=time_min.isoformat(), timeMax=time_max.isoformat(),
                                      privateExtendedProperty=f"source={PLANNER_TAG}", singleEvents=True,
                                      maxResults=250, pageToken=token).execute()
        events += [e for e in resp.get("items", []) if e.get("status") != "cancelled"]
        token = resp.get("nextPageToken")
        if not token:
            return events


def delete_event(calendar, calendar_id, event_id):
    from googleapiclient.errors import HttpError
    try:
        calendar.events().delete(calendarId=calendar_id, eventId=event_id).execute()
    except HttpError as e:
        if e.resp.status not in (404, 410):
            raise


def list_events(calendar, calendar_id, start, end):
    """All non-cancelled events overlapping [start, end), recurring ones expanded."""
    events, token = [], None
    while True:
        resp = calendar.events().list(calendarId=calendar_id, timeMin=start.isoformat(), timeMax=end.isoformat(),
                                      singleEvents=True, orderBy="startTime", maxResults=250, pageToken=token).execute()
        events += [e for e in resp.get("items", []) if e.get("status") != "cancelled"]
        token = resp.get("nextPageToken")
        if not token:
            return events


def busy_calendar_ids(calendar, configured):
    """Every calendar you have switched on in Google Calendar, plus the agent's own ones."""
    ids = set(configured)
    for c in calendar.calendarList().list(maxResults=250).execute().get("items", []):
        if c.get("selected") and not c.get("hidden") and not c.get("deleted"):
            ids.add(c["id"])
    return sorted(ids)


COPY_FIELDS = ("summary", "location", "start", "end", "recurrence")


def create_copy(calendar, calendar_id, source, source_label, mirror_key):
    """Copies another calendar's event (or a repeating series' master) onto `calendar_id`, tagged so the
    watcher can keep it in step with the original."""
    body = {k: source[k] for k in COPY_FIELDS if source.get(k)}
    for side in ("start", "end"):  # keep only what insert accepts
        body[side] = {k: v for k, v in body[side].items() if k in ("date", "dateTime", "timeZone")}
    original = (source.get("description") or "").strip()
    body["description"] = (f"Copied from your {source_label} calendar by {SOURCE_TAG}; it follows changes to the original."
                           + (f"\n\n{original[:1500]}" if original else ""))
    body["extendedProperties"] = {"private": {"source": SOURCE_TAG, "kind": "copy", "mirror_of": mirror_key}}
    return calendar.events().insert(calendarId=calendar_id, body=body).execute()["id"]


def update_copy(calendar, calendar_id, copy_id, source):
    """Brings a copy back in line with its (changed) original. A title or place removed at the source is
    cleared on the copy too."""
    patch = {k: source[k] for k in COPY_FIELDS if source.get(k)}
    for k in ("summary", "location"):
        patch.setdefault(k, "")
    for side in ("start", "end"):
        if side in patch:
            patch[side] = {k: v for k, v in patch[side].items() if k in ("date", "dateTime", "timeZone")}
    calendar.events().patch(calendarId=calendar_id, eventId=copy_id, body=patch).execute()


def list_instances(calendar, calendar_id, series_id, start, end):
    """The occurrences of one repeating event overlapping [start, end), cancelled ones left out."""
    events, token = [], None
    while True:
        resp = calendar.events().instances(calendarId=calendar_id, eventId=series_id, timeMin=start.isoformat(),
                                           timeMax=end.isoformat(), maxResults=250, pageToken=token).execute()
        events += [e for e in resp.get("items", []) if e.get("status") != "cancelled"]
        token = resp.get("nextPageToken")
        if not token:
            return events


def end_series_copy(calendar, calendar_id, copy_id, until):
    """Stops an old-style repeating copy at `until` (its past occurrences stay as history). If that isn't
    possible (e.g. it hadn't started yet), the copy is deleted."""
    from googleapiclient.errors import HttpError
    try:
        ev = calendar.events().get(calendarId=calendar_id, eventId=copy_id).execute()
    except HttpError as e:
        if e.resp.status in (404, 410):
            return
        raise
    stamp = until.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    recurrence = []
    for line in ev.get("recurrence") or []:
        if line.startswith("RRULE:"):
            parts = [p for p in line[len("RRULE:"):].split(";") if not p.startswith(("UNTIL=", "COUNT="))]
            line = "RRULE:" + ";".join(parts + [f"UNTIL={stamp}"])
        recurrence.append(line)
    start = ev.get("start", {}).get("dateTime")
    if not recurrence or (start and datetime.fromisoformat(start) >= until):
        delete_event(calendar, calendar_id, copy_id)
        return
    try:
        calendar.events().patch(calendarId=calendar_id, eventId=copy_id, body={"recurrence": recurrence}).execute()
    except HttpError as e:
        if e.resp.status in (404, 410):
            return
        delete_event(calendar, calendar_id, copy_id)


def move_deadline(calendar, tasks, cfg, event_id, task_id, due):
    """Moves a DUE event (30 min ending at `due`) and its task to a new due time."""
    tz = cfg["timezone"]
    calendar.events().patch(calendarId=cfg["calendars"]["college"], eventId=event_id, body={
        "start": {"dateTime": (due - timedelta(minutes=30)).isoformat(), "timeZone": tz},
        "end": {"dateTime": due.isoformat(), "timeZone": tz}}).execute()
    if task_id:
        tasks.tasks().patch(tasklist=cfg.get("tasklist", "@default"), task=task_id,
                            body={"due": f"{due.date().isoformat()}T00:00:00.000Z"}).execute()


def move_event(calendar, calendar_id, event_id, start, end, tz_name):
    """Moves one of the agent's blocks to a new time."""
    calendar.events().patch(calendarId=calendar_id, eventId=event_id, body={
        "start": {"dateTime": start.isoformat(), "timeZone": tz_name},
        "end": {"dateTime": end.isoformat(), "timeZone": tz_name}}).execute()
