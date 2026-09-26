"""Creates Google Calendar events and Google Tasks from items."""

SOURCE_TAG = "calendar-agent"
PLANNER_TAG = "calendar-agent-planner"  # habits and work blocks; see list_blocks()


def _when(value, all_day, tz_name):
    if all_day:
        return {"date": value.isoformat()}
    return {"dateTime": value.isoformat(), "timeZone": tz_name}


def _gmail_link(msg_id):
    return f"https://mail.google.com/mail/u/0/#all/{msg_id}"


def event_title(item):
    return f"DUE: {item['title']}" if item["type"] == "deadline" else item["title"]


def create_event(calendar, calendar_id, item, msg, tz_name):
    lines = [f"Course: {item['course']}"] if item.get("course") else []
    if item.get("description"):
        lines.append(item["description"])
    lines += [f"From email: {msg['subject']}", _gmail_link(msg["id"]), f"(created by {SOURCE_TAG})"]

    body = {
        "summary": event_title(item),
        "description": "\n".join(lines),
        "start": _when(item["start"], item["all_day"], tz_name),
        "end": _when(item["end"], item["all_day"], tz_name),
        # Lets us find (or wipe) everything this agent created.
        "extendedProperties": {"private": {"source": SOURCE_TAG, "gmail_id": msg["id"]}},
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
    notes += [f"From email: {msg['subject']}", _gmail_link(msg["id"])]
    body = {
        "title": item["title"],
        "notes": "\n".join(notes),
        # Tasks API stores only the date part of `due`.
        "due": f"{due.date().isoformat()}T00:00:00.000Z",
    }
    return tasks.tasks().insert(tasklist=tasklist, body=body).execute()["id"]


def create_item(calendar, tasks, cfg, item, msg):
    """Event on the college calendar, plus a task for deadlines. Returns (event_id, task_id)."""
    event_id = create_event(calendar, cfg["calendars"]["college"], item, msg, cfg["timezone"])
    task_id = create_task(tasks, cfg.get("tasklist", "@default"), item, msg) if item["type"] == "deadline" else None
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
