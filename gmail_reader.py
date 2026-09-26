"""Fetches forwarded IIITH emails from Gmail and turns them into simple dicts."""
import base64
import hashlib
import html
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from dateutil import parser as dparser

ICS_MIME_TYPES = {"text/calendar", "application/ics"}
# Mailman footer appended to list mail, e.g. "____\nStudents mailing list -- ..."
FOOTER_RE = re.compile(r"\n_{5,}\s*\n[^\n]*mailing list.*", re.DOTALL | re.IGNORECASE)
FW_PREFIX_RE = re.compile(r"^\s*((fw|fwd)\s*:\s*)+", re.IGNORECASE)


def _decode(data):
    return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")


def _walk(part):
    yield part
    for child in part.get("parts", []):
        yield from _walk(child)


def _html_to_text(raw):
    raw = re.sub(r"(?is)<(script|style).*?</\1>", "", raw)
    raw = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>", "\n", raw)
    return html.unescape(re.sub(r"<[^>]+>", "", raw))


def _body_text(payload):
    plain, rich = None, None
    for part in _walk(payload):
        if part.get("filename"):
            continue  # attachment, not the body
        data = part.get("body", {}).get("data")
        if not data:
            continue
        if part.get("mimeType") == "text/plain" and plain is None:
            plain = _decode(data)
        elif part.get("mimeType") == "text/html" and rich is None:
            rich = _html_to_text(_decode(data))
    text = plain or rich or ""
    return FOOTER_RE.sub("", text).strip()


def split_forward_header(body):
    """Removes the 'From: / Sent: / To: / Subject:' block Outlook puts at the top of a forward.

    The two forwarders produce this block in different formats (IST vs UTC 'Sent:'), so it must be
    removed before fingerprinting. Returns (from_line, sent_line, body_without_header). from_line keeps the
    mailing-list address, e.g. 'life@lists.iiit.ac.in <...> On Behalf Of gaming club <...>'.
    """
    lines = body.splitlines()
    head = lines[:15]
    from_idx = next((i for i, l in enumerate(head) if l.startswith("From:")), None)
    subj_idx = next((i for i, l in enumerate(head) if l.startswith("Subject:")), None)
    if from_idx is None or subj_idx is None or subj_idx < from_idx:
        return "", "", body
    from_line = lines[from_idx][len("From:"):].strip()
    sent_line = next((l[len("Sent:"):].strip() for l in lines[from_idx:subj_idx] if l.startswith("Sent:")), "")
    return from_line, sent_line, "\n".join(lines[subj_idx + 1:]).strip()


UTC_OFFSET_RE = re.compile(r"\(UTC([+-])(\d{1,2}):?(\d{2})\)")


def parse_sent(sent_line, received):
    """When the original email was sent, from the forward's 'Sent:' line. Two forms arrive:
    'Saturday, 26 September 2026 04:36:57' (UTC, from Power Automate) and
    'Saturday, September 26, 2026 10:00:00 AM (UTC+05:30) Chennai, Kolkata, ...' (Outlook).
    Returns an aware datetime, or None if it can't be parsed or is implausibly far from `received`."""
    if not sent_line:
        return None
    tz = timezone.utc
    if m := UTC_OFFSET_RE.search(sent_line):
        sign = 1 if m[1] == "+" else -1
        tz = timezone(sign * timedelta(hours=int(m[2]), minutes=int(m[3])))
        sent_line = sent_line[:m.start()]
    try:
        sent = dparser.parse(sent_line.strip(), fuzzy=True).replace(tzinfo=tz)
    except (ValueError, OverflowError):
        return None
    if abs(sent - received) > timedelta(days=3):
        return None  # a mangled header shouldn't move dates around
    return sent.astimezone(received.tzinfo)


def real_sender(from_line):
    """'list@... On Behalf Of Prof X <x@...>' -> 'Prof X <x@...>'."""
    return re.split(r"\s*on behalf of\s*", from_line, flags=re.IGNORECASE)[-1]


def _ics_attachments(service, msg_id, payload):
    found = []
    for part in _walk(payload):
        is_ics = part.get("mimeType") in ICS_MIME_TYPES or part.get("filename", "").lower().endswith(".ics")
        if not is_ics:
            continue
        body = part.get("body", {})
        if body.get("data"):
            found.append(base64.urlsafe_b64decode(body["data"]))
        elif body.get("attachmentId"):
            att = service.users().messages().attachments().get(
                userId="me", messageId=msg_id, id=body["attachmentId"]).execute()
            found.append(base64.urlsafe_b64decode(att["data"]))
    return found


def fetch_messages(service, base_query, after, tz_name, skip=None):
    """Returns messages matching base_query received after `after` (datetime), oldest first.
    skip(msg_id) -> True drops a message before it is downloaded (e.g. already processed)."""
    tz = ZoneInfo(tz_name)
    query = f"{base_query} after:{int(after.timestamp())}"
    ids, page_token = [], None
    while True:
        resp = service.users().messages().list(userId="me", q=query, pageToken=page_token).execute()
        ids += [m["id"] for m in resp.get("messages", [])]
        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    messages = []
    for msg_id in ids:
        if skip and skip(msg_id):
            continue
        full = service.users().messages().get(userId="me", id=msg_id, format="full").execute()
        payload = full["payload"]
        headers = {h["name"].lower(): h["value"] for h in payload.get("headers", [])}
        received = datetime.fromtimestamp(int(full["internalDate"]) / 1000, timezone.utc).astimezone(tz)
        from_line, sent_line, body = split_forward_header(_body_text(payload))
        messages.append({
            "id": msg_id,
            "subject": FW_PREFIX_RE.sub("", headers.get("subject", "")).strip(),
            "received": received,
            # relative dates ("tomorrow") count from when the original was sent, not when it was forwarded
            "reference": parse_sent(sent_line, received) or received,
            "from_line": from_line,
            "sender": real_sender(from_line),
            "body": body,
            "ics": _ics_attachments(service, msg_id, payload),
        })
    messages.sort(key=lambda m: m["received"])
    return messages


def fingerprint(msg):
    """Same email forwarded twice -> same fingerprint (Power Automate sometimes double-forwards)."""
    normalised = re.sub(r"\s+", " ", (msg["subject"] + "\n" + msg["body"]).lower()).strip()
    return hashlib.sha256(normalised.encode()).hexdigest()


def sender_address(sender):
    """'Prof X <x@iiit.ac.in>' -> 'x@iiit.ac.in' (lowercase); used to remember your choices per sender.
    The address in the last <...> wins, so an address typed into the display name can't impersonate someone."""
    sender = sender or ""
    bracketed = re.findall(r"<([^<>\s]+@[^<>\s]+)>", sender)
    if bracketed:
        return bracketed[-1].lower()
    m = re.search(r"[\w.+-]+@[\w.-]+", sender)
    return m[0].lower() if m else (sender.strip().lower() or "unknown")
