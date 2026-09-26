"""Prints the 5 most recent emails matching gmail_query from config.yaml."""
from googleapiclient.discovery import build

from auth import get_credentials
from config import load_config


def main():
    query = load_config()["gmail_query"]
    gmail = build("gmail", "v1", credentials=get_credentials())
    messages = gmail.users().messages().list(userId="me", q=query, maxResults=5).execute().get("messages", [])

    if not messages:
        print(f"No messages match: {query!r}")
        print("Try the fallback query in config.yaml (subject:FW from:<your IIITH address>).")
        return

    for m in messages:
        msg = gmail.users().messages().get(
            userId="me", id=m["id"], format="metadata", metadataHeaders=["Subject", "Date"]
        ).execute()
        headers = {h["name"]: h["value"] for h in msg["payload"]["headers"]}
        print(f"{headers.get('Date', '?')[:31]:<32} {headers.get('Subject', '(no subject)')}")


if __name__ == "__main__":
    main()
