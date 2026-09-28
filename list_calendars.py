"""Prints your Google calendars and Google Tasks lists with their IDs, to fill in config.yaml.

    venv/bin/python list_calendars.py
A subscribed calendar named after its URL (e.g. a Moodle export link with a private token) is shown by its host only.
"""
from googleapiclient.discovery import build

import calwatch
from auth import get_credentials


def main():
    creds = get_credentials()
    service = build("calendar", "v3", credentials=creds)
    items = service.calendarList().list(showHidden=True).execute().get("items", [])
    print("Calendars (config.yaml -> calendars / calendar_watch.calendars):")
    for cal in sorted(items, key=lambda c: calwatch.label(c)):
        print(f"  {calwatch.label(cal):<35} {'primary' if cal.get('primary') else cal['id']}")
    tasks = build("tasks", "v1", credentials=creds)
    print("\nGoogle Tasks lists (config.yaml -> tasklist / morning.todo_tasklist):")
    for tl in tasks.tasklists().list(maxResults=100).execute().get("items", []):
        print(f"  {tl['title']:<35} {tl['id']}")


if __name__ == "__main__":
    main()
