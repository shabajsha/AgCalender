"""Prints your Google calendars' names and IDs so you can fill in config.yaml."""
from googleapiclient.discovery import build

from auth import get_credentials


def main():
    service = build("calendar", "v3", credentials=get_credentials())
    items = service.calendarList().list().execute().get("items", [])
    for cal in sorted(items, key=lambda c: c.get("summary", "")):
        print(f"{cal.get('summary', '(no name)'):<35} {cal['id']}")


if __name__ == "__main__":
    main()
