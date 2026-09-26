"""Google Tasks helpers shared by the planner and the digest (paged, so no list is silently cut at 20)."""
from datetime import date


def iter_tasks(tasks_api, tasklist_id, **params):
    token = None
    while True:
        resp = tasks_api.tasks().list(tasklist=tasklist_id, maxResults=100, pageToken=token, **params).execute()
        yield from resp.get("items", [])
        token = resp.get("nextPageToken")
        if not token:
            return


def task_lists(tasks_api):
    return tasks_api.tasklists().list(maxResults=100).execute().get("items", [])


def open_dated_tasks(tasks_api, skip_list_id, due_before):
    """Open tasks with a due date before `due_before` from every list except `skip_list_id`.
    Returns dicts: id, title, due (date), list_id, list_title."""
    found = []
    for tl in task_lists(tasks_api):
        if tl["id"] == skip_list_id:
            continue
        for t in iter_tasks(tasks_api, tl["id"], showCompleted=False, showHidden=False,
                            dueMax=f"{due_before.isoformat()}T00:00:00.000Z"):
            if t.get("due") and t.get("title"):
                found.append({"id": t["id"], "title": t["title"], "due": date.fromisoformat(t["due"][:10]),
                              "list_id": tl["id"], "list_title": tl["title"]})
    return sorted(found, key=lambda t: (t["due"], t["title"]))


def completed_ids(tasks_api, tasklist_id):
    """IDs of completed tasks in one list (used to stop planning deadlines you've already finished)."""
    return {t["id"] for t in iter_tasks(tasks_api, tasklist_id, showCompleted=True, showHidden=True)
            if t.get("status") == "completed"}
