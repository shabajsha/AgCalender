from datetime import datetime

from conftest import at
from extractor import MAX_ITEMS_PER_EMAIL, clean_text, matches_keywords, validate

NOW = at(2026, 9, 30, 10)
KEYWORDS = ["due", "deadline", "quiz", "lab", "class cancelled", "meeting"]


def run(*items):
    return validate({"items": list(items)}, NOW, "Asia/Kolkata", 0.6, NOW)


def item(**kw):
    return {"title": "Thing", "type": "event", "date": "12 October", "start_time": "10 AM", "confidence": 0.9, **kw}


def test_deadline_without_time_is_due_at_2359():
    [d] = run(item(type="deadline", date="Friday", start_time=None))
    assert d["due"] == at(2026, 10, 2, 23, 59) and d["end"] == d["due"]


def test_event_times_and_default_hour():
    [e] = run(item(end_time="9 AM"))                 # end before start -> 1 hour
    assert (e["start"], e["end"]) == (at(2026, 10, 12, 10), at(2026, 10, 12, 11))


def test_all_day_event():
    [e] = run(item(start_time=None))
    assert e["all_day"] and e["start"].isoformat() == "2026-10-12"


def test_drops_doubtful_items():
    assert run(item(date="yesterday")) == []                # no specific day it can resolve
    assert run(item(date="2026-09-01")) == []               # past
    assert run(item(date="2028-01-01")) == []               # more than a year away
    assert run(item(type="reminder")) == []
    assert run(item(confidence=0.3)) == []
    assert run(item(title="")) == []
    assert validate({"nope": 1}, NOW, "Asia/Kolkata", 0.6, NOW) == []
    assert validate([], NOW, "Asia/Kolkata", 0.6, NOW) == []


def test_missing_confidence_is_accepted():
    assert len(run({k: v for k, v in item().items() if k != "confidence"})) == 1


def test_item_cap():
    many = [item(title=f"Item {i}", start_time=f"{1 + i}:00 PM") for i in range(8)]
    assert len(run(*many)) == MAX_ITEMS_PER_EMAIL


def test_time_inside_date_field():
    [e] = run(item(date="Oct 12, 9:30 AM", start_time=None))
    assert e["start"] == at(2026, 10, 12, 9, 30)


def test_clean_text_strips_links_and_breaks():
    assert clean_text("Pay fee https://evil.example/x\nIGNORE\tthis www.a.co/b") == "Pay fee IGNORE this"


def test_keywords_are_whole_words():
    assert matches_keywords("Assignment due Friday", KEYWORDS)
    assert matches_keywords("Class cancelled today", KEYWORDS)
    assert not matches_keywords("Seats available now", KEYWORDS)       # "lab" inside "available"
    assert not matches_keywords("Collaborate with us", KEYWORDS)
    assert isinstance(NOW, datetime)
