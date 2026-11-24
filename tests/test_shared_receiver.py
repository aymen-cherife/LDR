"""Speed step 3: a receiver shared with the calling call is read by that call only."""

import json
import marshal
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from ldr import processes

REPO = Path(__file__).resolve().parents[1]

APP = """\
class Account:
    def __init__(self):
        self.total = 0

    def deposit(self, amount):
        self._add(amount)
        return self.total

    def _add(self, amount):
        self.total += amount

    def merge(self, other):
        other._add(self.total)


first, second = Account(), Account()
first.deposit(5)
first.merge(second)
"""


def run(project):
    (project / "app.py").write_text(APP, encoding="utf-8")
    result = processes.run(
        [sys.executable, "-m", "ldr", "run", "app.py"],
        cwd=project, env=dict(os.environ, PYTHONPATH=str(REPO)),
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    with sqlite3.connect(project / ".ldr" / "traces.sqlite3") as connection:
        rows = connection.execute(
            "SELECT frame_id, parts FROM call_records ORDER BY ordinal"
        ).fetchall()
        (summary,) = connection.execute("SELECT summary FROM stream_summary").fetchone()
    names = {
        frame_id: [(role, name) for role, name, _ in marshal.loads(parts)]
        for frame_id, parts in rows
    }
    return names, json.loads(summary), result.stdout


def test_an_inner_call_on_the_same_object_does_not_read_its_receiver_again(tmp_path):
    names, summary, _ = run(tmp_path)
    # The outer call reads self on the way in and out, so the change is found there.
    assert ("in", "self") in names["app.py:Account.deposit#1"]
    assert ("out", "self") in names["app.py:Account.deposit#1"]
    # The inner call has the same self: neither read.
    assert [part for part in names["app.py:Account.deposit#1/app.py:Account._add#1"] if part[1] == "self"] == []
    assert ("in", "amount") in names["app.py:Account.deposit#1/app.py:Account._add#1"]
    assert summary["fingerprint"]["receivers_shared"] == 1


def test_a_different_receiver_is_still_read(tmp_path):
    names, summary, _ = run(tmp_path)
    inner = names["app.py:Account.merge#1/app.py:Account._add#1"]  # other._add: another object
    assert ("in", "self") in inner and ("out", "self") in inner
    # A call with no caller on the same object is read as before.
    assert ("in", "self") in names["app.py:Account.merge#1"]
    assert summary["fingerprint"]["receivers_shared"] == 1


def test_the_gap_is_stated_in_the_output(tmp_path):
    _, _, output = run(tmp_path)
    assert "1 call(s) share their receiver" in output and "one level up" in output


FREED = """\
import weakref


class Thing:
    def method(self):
        return self.helper()

    def helper(self):
        return 1


def plain(value):
    return 1


def make():
    thing = Thing()
    thing.method()
    plain(thing)
    return weakref.ref(thing)


reference = make()
assert reference() is None, "a recorded call kept its receiver alive"
thing = Thing()
thing.method()
plain(thing)
reference = weakref.ref(thing)
del thing
assert reference() is None, "a recorded call kept its argument alive"
print("FREED")
"""


def test_recording_a_call_does_not_keep_its_objects_alive(tmp_path):
    (tmp_path / "app.py").write_text(FREED, encoding="utf-8")
    result = processes.run(
        [sys.executable, "-m", "ldr", "run", "app.py"], cwd=tmp_path,
        env=dict(os.environ, PYTHONPATH=str(REPO)), capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0 and "FREED" in result.stdout, result.stdout + result.stderr
