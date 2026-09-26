from conftest import TZ, at
from gmail_reader import fingerprint, parse_sent, real_sender, sender_address, split_forward_header
from ics_import import describe_recurrence, is_past, parse_ics

INVITE = b"""BEGIN:VCALENDAR
VERSION:2.0
METHOD:REQUEST
BEGIN:VEVENT
UID:1
SUMMARY:DSA Evaluation
DTSTART;TZID=India Standard Time:20261001T140000
DTEND;TZID=India Standard Time:20261001T150000
LOCATION:Himalaya 105
END:VEVENT
BEGIN:VEVENT
UID:2
SUMMARY:Holiday
DTSTART;VALUE=DATE:20261002
DTEND;VALUE=DATE:20261003
END:VEVENT
BEGIN:VEVENT
UID:3
SUMMARY:Weekly lab
DTSTART:20260901T090000
DTEND:20260901T110000
RRULE:FREQ=WEEKLY;BYDAY=TU
EXDATE:20261006T090000
END:VEVENT
BEGIN:VEVENT
UID:4
SUMMARY:Cancelled one
STATUS:CANCELLED
DTSTART:20261005T090000
END:VEVENT
END:VCALENDAR
"""


def test_parse_ics():
    items = {i["title"]: i for i in parse_ics(INVITE, "Asia/Kolkata")}
    assert set(items) == {"DSA Evaluation", "Holiday", "Weekly lab"}          # cancelled one skipped
    assert items["DSA Evaluation"]["start"] == at(2026, 10, 1, 14)             # Outlook's Windows TZ name
    assert items["Holiday"]["all_day"]
    lab = items["Weekly lab"]["recurrence"]
    assert lab[0] == "RRULE:FREQ=WEEKLY;BYDAY=TU" and lab[1] == "EXDATE:20261006T033000Z"
    assert not is_past(items["Weekly lab"], at(2026, 12, 1))                   # recurring: kept


def test_cancel_method():
    assert parse_ics(b"BEGIN:VCALENDAR\nMETHOD:CANCEL\nBEGIN:VEVENT\nSUMMARY:x\nDTSTART:20261001T090000\n"
                     b"END:VEVENT\nEND:VCALENDAR\n", "Asia/Kolkata") == []


def test_describe_recurrence():
    assert describe_recurrence(["RRULE:FREQ=WEEKLY;BYDAY=TU,TH;UNTIL=20261130T000000Z"]) == \
        "weekly on Tue, Thu until 30 Nov 2026"
    assert describe_recurrence(["RRULE:FREQ=DAILY;COUNT=5"]) == "daily, 5 times"


PA_BODY = ("\n____\nFrom: life@lists.iiit.ac.in <life@lists.iiit.ac.in> on behalf of gaming club <tgc@students.iiit.ac.in>\n"
           "Sent: Saturday, 26 September 2026 04:30:00\nTo: life\nSubject: GameDev101\n\nWorkshop on Sunday.")
OUTLOOK_BODY = ("From: life@lists.iiit.ac.in <life@lists.iiit.ac.in>On Behalf Ofgaming club <tgc@students.iiit.ac.in>\n"
                "Sent: Saturday, September 26, 2026 10:00:00 AM (UTC+05:30) Chennai, Kolkata, Mumbai, New Delhi\n"
                "To: life\nSubject: GameDev101\n\nWorkshop on Sunday.")


def test_both_forward_formats_split_and_fingerprint_alike():
    a, b = split_forward_header(PA_BODY), split_forward_header(OUTLOOK_BODY)
    assert a[2] == b[2] == "Workshop on Sunday."
    assert sender_address(real_sender(a[0])) == sender_address(real_sender(b[0])) == "tgc@students.iiit.ac.in"
    msg = lambda body: {"subject": "GameDev101", "body": body}
    assert fingerprint(msg(a[2])) == fingerprint(msg(b[2]))


def test_parse_sent_both_formats():
    received = at(2026, 9, 26, 10, 9)
    assert parse_sent("Saturday, 26 September 2026 04:36:57", received) == at(2026, 9, 26, 10, 6).replace(second=57)
    assert parse_sent(split_forward_header(OUTLOOK_BODY)[1], received) == at(2026, 9, 26, 10, 0)
    assert parse_sent("garbage", received) is None
    assert parse_sent("Monday, 1 January 2024 10:00:00", received) is None  # implausibly far: ignored
    assert TZ is not None


def test_sender_address_resists_display_name_spoofing():
    assert sender_address('"boss@iiit.ac.in" <attacker@evil.com>') == "attacker@evil.com"
    assert sender_address("Prof X <X@IIIT.ac.in>") == "x@iiit.ac.in"
    assert sender_address("") == "unknown"
