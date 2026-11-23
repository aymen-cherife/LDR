"""Close-list item 3: every change is shown; one that also varied with no edit carries a note."""

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

SCRIPT = """\
import random


class Key:
    def __hash__(self):
        return id(self) % 1000003


def pick():
    return random.random()


def stable(x):
    return x + 1


pick()
stable(1)
hash(Key())
"""


def ldr(project, *args):
    return subprocess.run(
        [sys.executable, "-m", "ldr", "run", "app.py", *args], cwd=project,
        env=dict(os.environ, PYTHONPATH=str(REPO)), capture_output=True, text=True, timeout=300,
    )


def report(project):
    return json.loads((project / ".ldr" / "last.json").read_text(encoding="utf-8"))


def test_a_call_that_varied_between_identical_runs_is_reported_with_a_note(tmp_path):
    (tmp_path / "app.py").write_text(SCRIPT, encoding="utf-8")
    assert ldr(tmp_path, "--golden", "--learn-volatility").returncode == 0
    with sqlite3.connect(tmp_path / ".ldr" / "traces.sqlite3") as connection:
        (stored,) = connection.execute("SELECT payload FROM run_sections WHERE name = 'varied_calls'").fetchone()
    learned = json.loads(stored)
    assert "app.py:pick#1" in learned["addresses"] and "app.py:stable#1" not in learned["addresses"]
    assert learned["runs"] == 2

    result = ldr(tmp_path, "--diff", "--fail-on-change")

    assert result.returncode == 3  # shown and counted: nothing is left out
    detail = report(tmp_path)["status"]["reference"]["changed_detail"]
    assert detail["app.py:pick"] == [1, 1]
    assert "app.py:stable" not in detail
    assert "app.py:pick (1 of 1 call(s) also varied across 2 runs with no edit)" in result.stdout
    assert "a __hash__ result" in result.stdout  # the reason, when it is known
    assert report(tmp_path)["behavior"]["changed"] is True


def test_a_real_edit_in_another_function_carries_no_note(tmp_path):
    (tmp_path / "app.py").write_text(SCRIPT, encoding="utf-8")
    ldr(tmp_path, "--golden", "--learn-volatility")
    (tmp_path / "app.py").write_text(SCRIPT.replace("return x + 1", "return x + 2"), encoding="utf-8")

    result = ldr(tmp_path, "--diff")

    detail = report(tmp_path)["status"]["reference"]["changed_detail"]
    assert detail["app.py:stable"] == [1, 0]
    assert "app.py:stable (" not in result.stdout  # no note
    assert "stable" in result.stdout


def test_without_learning_nothing_is_noted(tmp_path):
    (tmp_path / "app.py").write_text(SCRIPT, encoding="utf-8")
    ldr(tmp_path, "--golden")
    result = ldr(tmp_path, "--diff")
    assert "also varied" not in result.stdout
    assert report(tmp_path)["status"]["reference"]["changed_detail"]["app.py:pick"] == [1, 0]
