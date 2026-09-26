from datetime import date

import pytest

import todos
from conftest import FakeTasks


@pytest.mark.parametrize("line, expected", [
    ("Finish lab report 2h", ("Finish lab report", 120)),
    ("- Call bank 15m", ("Call bank", 15)),
    ("2) Gym 1.5 hours", ("Gym", 90)),
    ("Revise OS 1h30m", ("Revise OS", 90)),
    ("Read chapter 3 for 45 min", ("Read chapter 3", 45)),
    ("Watch lecture 2 - 1 hr", ("Watch lecture 2", 60)),
    ("Meet 2 friends 3hrs", ("Meet 2 friends", 180)),
    ("Buy groceries", ("Buy groceries", 30)),
])
def test_parse(line, expected):
    assert todos.parse(line, 30) == [expected]


def test_parse_many_lines_skips_blanks():
    assert [t for t, _ in todos.parse("A 1h\n\n  \nB", 30)] == ["A", "B"]


def test_add_and_undo(db):
    tasks = FakeTasks()
    batch, added = todos.add(tasks, "DAILY", db, date(2026, 9, 28), [("Lab report", 120), ("Call bank", 15)])
    assert added == [("Lab report", 120), ("Call bank", 15)]
    assert db.get_effort("task:t1") == 2.0
    assert all(body["due"].startswith("2026-09-28") for body in tasks.store.values())
    assert todos.undo(tasks, "DAILY", db, batch) == ["Lab report", "Call bank"]
    assert tasks.store == {}


def test_partial_failure_is_still_undoable(db):
    tasks = FakeTasks(fail_on=2)
    with pytest.raises(RuntimeError):
        todos.add(tasks, "DAILY", db, date(2026, 9, 28), [("First", 30), ("Second", 30)])
    assert todos.undo(tasks, "DAILY", db, 1) == ["First"]   # the task created before the error isn't orphaned
