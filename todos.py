"""To-dos you send the bot: parse the text (no LLM), add them to Google Tasks due today, undo them.

"Lab report 2h", "Call bank 15m", "- Gym 1.5 hours", "Revise OS 1h30m" -> (title, minutes).
Each to-do's minutes are stored as its effort, so the planner gives it exactly that much time today.
"""
import re

from googleapiclient.errors import HttpError

BULLET_RE = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s*")
DURATION_RE = re.compile(
    r"(?:(?P<h>\d+(?:\.\d+)?)\s*(?:hours|hour|hrs|hr|h)(?![a-z])\s*(?:(?P<hm>\d+)\s*(?:minutes|minute|mins|min|m)(?![a-z]))?"
    r"|(?P<m>\d+)\s*(?:minutes|minute|mins|min|m)(?![a-z]))", re.IGNORECASE)
MAX_MINUTES = 12 * 60
MAX_TITLE = 200


def parse(text, default_minutes):
    """One to-do per non-empty line. Returns [(title, minutes)]."""
    todos = []
    for line in text.splitlines():
        line = BULLET_RE.sub("", line).strip()
        if not line:
            continue
        minutes = default_minutes
        match = None
        for match in DURATION_RE.finditer(line):
            pass  # the last duration in the line wins ("Read ch 3 for 2h")
        if match:
            if match["h"]:
                minutes = round(float(match["h"]) * 60) + int(match["hm"] or 0)
            else:
                minutes = int(match["m"])
            line = (line[:match.start()] + line[match.end():])
            line = re.sub(r"\s+(for|-|,)?\s*$", "", line.strip()).strip(" -,:")
        minutes = max(5, min(MAX_MINUTES, minutes))
        if line:
            todos.append((line[:MAX_TITLE], minutes))
    return todos


def fmt_minutes(minutes):
    h, m = divmod(minutes, 60)
    return f"{h} h {m} min" if h and m else f"{h} h" if h else f"{m} min"


def add(tasks_api, tasklist, state, day, todos):
    """Creates the tasks (due `day`), stores their effort, and returns (batch, [(title, minutes)]).
    Each task is recorded as soon as it exists, so Undo covers it even if a later one fails."""
    batch, created = state.next_todo_batch(), []
    for title, minutes in todos:
        task = tasks_api.tasks().insert(tasklist=tasklist, body={
            "title": title,
            "notes": f"About {fmt_minutes(minutes)} (added from Telegram)",
            "due": f"{day.isoformat()}T00:00:00.000Z",
        }).execute()
        state.add_todo(batch, task["id"], title, minutes, day)
        state.set_effort(f"task:{task['id']}", minutes / 60)
        created.append((title, minutes))
    return batch, created


def undo(tasks_api, tasklist, state, batch):
    """Deletes a batch's tasks from Google Tasks. Returns the titles removed."""
    removed = []
    for row in state.todo_batch(batch):
        try:
            tasks_api.tasks().delete(tasklist=tasklist, task=row["task_id"]).execute()
        except HttpError as e:
            if e.resp.status not in (404, 410):
                raise
        removed.append(row["title"])
    state.mark_todos_undone(batch)
    return removed
